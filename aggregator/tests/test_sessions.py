"""Session policy: eviction at the cap, orphan timeout, keepalive progress, instructions, on_ready, deny list."""

import json
import sys

import anyio
import pytest

from conftest import FAKE, INIT, RAW_HEADERS, FakeBackend, fake_spec, raw_call, raw_init, run_app, wait_until
from rebox_aggregator.config import ChildSpec, OnReady

pytestmark = pytest.mark.anyio


async def tool_text(http, url, sid, name, args=None, rid=2):
    msgs = await raw_call(http, url, sid, "tools/call", {"name": name, "arguments": args or {}}, rid)
    return msgs[-1]["result"]["content"][0]["text"]


# -- eviction at the cap ----------------------------------------------------

async def test_idle_session_is_evicted_at_the_cap():
    with run_app(max_sessions=2, evict_min_idle=0.5) as r:
        async with r.raw() as http:
            first, _ = await raw_init(http, r.url)
            await anyio.sleep(0.8)
            second, _ = await raw_init(http, r.url)
            third, _ = await raw_init(http, r.url)  # evicts `first`, the longest idle
            assert set(r.app.sessions) == {second, third}
            res = await http.post(r.url, json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
                                  headers={**RAW_HEADERS, "mcp-session-id": first})
            assert res.status_code == 404
        assert r.logged("session_evicted") and r.logged("session_close")[0]["reason"] == "evicted"


async def test_session_with_a_call_in_flight_is_not_evicted():
    with run_app(max_sessions=1, evict_min_idle=0.2) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)

            async def slow():
                await raw_call(http, r.url, sid, "tools/call", {"name": "a_slow", "arguments": {}}, 5)

            async with anyio.create_task_group() as tg:
                tg.start_soon(slow)
                await anyio.sleep(0.8)
                res = await http.post(r.url, json=INIT, headers=RAW_HEADERS)
                assert res.status_code == 503 and "session limit" in res.json()["error"]["message"]
                assert sid in r.app.sessions
                tg.cancel_scope.cancel()


async def test_eviction_saves_the_victims_work(tmp_path):
    from test_autosave import call, saved_file, spec

    with run_app([spec()], max_sessions=1, evict_min_idle=0.2, state_dir=str(tmp_path), autosave_interval=0) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            await call(http, r, sid, "a_echo")
            await anyio.sleep(0.4)
            await raw_init(http, r.url)
        assert saved_file(tmp_path, sid).exists()


# -- GET streams and the orphan timeout -------------------------------------

async def test_orphan_session_is_closed_sooner_than_one_with_a_get_stream():
    with run_app(idle_timeout=30.0, orphan_timeout=1.0) as r:
        async with r.raw() as http:
            orphan, _ = await raw_init(http, r.url)
            attended, _ = await raw_init(http, r.url)
            async with anyio.create_task_group() as tg:
                async def stream():
                    async with http.stream("GET", r.url, headers={**RAW_HEADERS, "mcp-session-id": attended,
                                                                   "mcp-protocol-version": "2025-11-25"}) as resp:
                        async for _ in resp.aiter_lines():
                            pass

                tg.start_soon(stream)
                assert await wait_until(lambda: r.app.sessions.get(attended) and r.app.sessions[attended].get_streams == 1, 5)
                assert await wait_until(lambda: orphan not in r.app.sessions, 10)
                assert attended in r.app.sessions
                tg.cancel_scope.cancel()
            assert await wait_until(lambda: r.app.sessions[attended].get_streams == 0, 5)
        assert [e["reason"] for e in r.logged("session_close")][0] == "orphaned"
        assert [e["open"] for e in r.logged("get_stream_open")] == [1]
        assert r.logged("get_stream_closed")


def test_orphan_timeout_zero_means_idle_timeout():
    from rebox_aggregator.config import Settings
    from rebox_aggregator.session import Session

    s = Session("sid", None, Settings("t", (), idle_timeout=100.0, orphan_timeout=0.0), "i", lambda _: None)  # type: ignore[arg-type]
    assert s.idle_timeout() == 100.0
    s.settings = Settings("t", (), idle_timeout=100.0, orphan_timeout=30.0)
    assert s.idle_timeout() == 30.0
    s.get_streams = 1
    assert s.idle_timeout() == 100.0


# -- keepalive progress -----------------------------------------------------

async def test_quiet_request_gets_synthetic_progress():
    with run_app(progress_keepalive=0.4) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            msgs = await raw_call(http, r.url, sid, "tools/call", {
                "name": "a_sleep", "arguments": {"seconds": 1.6}, "_meta": {"progressToken": "tk"}})
    progress = [m["params"] for m in msgs if m.get("method") == "notifications/progress"]
    assert len(progress) >= 2 and {p["progressToken"] for p in progress} == {"tk"}
    values = [p["progress"] for p in progress]
    assert values == sorted(set(values))  # strictly increasing
    assert msgs[-1]["result"]["content"][0]["text"] == "slept"


async def test_no_synthetic_progress_without_token_or_when_child_reports():
    with run_app(progress_keepalive=0.4) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            msgs = await raw_call(http, r.url, sid, "tools/call", {"name": "a_sleep", "arguments": {"seconds": 1.2}})
            assert not [m for m in msgs if m.get("method") == "notifications/progress"]
            msgs = await raw_call(http, r.url, sid, "tools/call", {
                "name": "a_progress", "arguments": {}, "_meta": {"progressToken": "t2"}}, 3)
            assert [m["params"]["progress"] for m in msgs if m.get("method") == "notifications/progress"] == [1, 2, 3]


# -- instructions -----------------------------------------------------------

async def test_initialize_carries_instructions(tmp_path):
    from test_autosave import spec

    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0) as r:
        async with r.raw() as http:
            sid, init = await raw_init(http, r.url)
            text = init["result"]["instructions"]
            tag = r.app.sessions[sid].tag
        assert str(tmp_path) in text and tag in text and f"{tmp_path}/ev/{tag}.json" in text
        for needle in ("gh_", "rea_", "discarded", "/c/Users", "tools/list_changed", "program selector"):
            assert needle in text


async def test_instructions_without_state_dir(running):
    async with running.raw() as http:
        _, init = await raw_init(http, running.url)
    assert "State directory" not in init["result"]["instructions"]


# -- on_ready ----------------------------------------------------------------

def ready_spec(name="a", prefix="a_"):
    return ChildSpec(name, prefix, (sys.executable, FAKE, name), {}, None, OnReady("connect_instance", {"project": "rebox"}))


async def connects(http, r, sid, rid=50):
    return json.loads(await tool_text(http, r.url, sid, "a_get_connects", rid=rid))


async def test_on_ready_called_when_backend_comes_up_after_boot():
    with run_app([ready_spec(), fake_spec("b", "b_")]) as r:
        backend = r.app.backend = FakeBackend(ready=False)
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)  # booted before the backend was ready
            assert backend.wants == [True]
            assert await connects(http, r, sid) == []
            backend.is_ready = True
            r.on_loop(r.app.backend_became_ready)
            for _ in range(50):
                if await connects(http, r, sid, rid=60 + _):
                    break
                await anyio.sleep(0.1)
            assert await connects(http, r, sid, rid=200) == [{"project": "rebox"}]
        assert r.logged("on_ready")[0]["outcome"] == "ok"


async def test_on_ready_not_called_when_backend_was_already_up():
    with run_app([ready_spec()]) as r:
        r.app.backend = FakeBackend(ready=True)
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            await anyio.sleep(0.5)
            assert await connects(http, r, sid) == []


async def test_on_ready_waits_for_the_client_handshake():
    with run_app([ready_spec()]) as r:
        backend = r.app.backend = FakeBackend(ready=False)
        async with r.raw() as http:
            res = await http.post(r.url, json=INIT, headers=RAW_HEADERS)  # no notifications/initialized yet
            sid = res.headers["mcp-session-id"]
            backend.is_ready = True
            r.on_loop(r.app.backend_became_ready)
            await anyio.sleep(0.5)
            assert not r.logged("on_ready")
            await http.post(r.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                            headers={**RAW_HEADERS, "mcp-session-id": sid})
            assert await wait_until(lambda: r.logged("on_ready"), 5)


async def test_backend_wanted_while_sessions_exist():
    with run_app() as r:
        backend = r.app.backend = FakeBackend()
        async with r.raw() as http:
            first, _ = await raw_init(http, r.url)
            second, _ = await raw_init(http, r.url)
            await http.delete(r.url, headers={"mcp-session-id": first})
            assert backend.wants[-1] is True
            await http.delete(r.url, headers={"mcp-session-id": second})
            assert await wait_until(lambda: backend.wants[-1] is False, 5)


# -- deny list ---------------------------------------------------------------

def deny_spec():
    return ChildSpec("a", "a_", (sys.executable, FAKE, "a"), {"FAKE_EXTRA_TOOLS": "run_script"}, deny=("run_script", "echo"))


async def test_denied_tools_are_hidden_and_calls_fail_clearly():
    with run_app([deny_spec(), fake_spec("b", "b_", FAKE_EXTRA_TOOLS="run_script")]) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            names = {t["name"] for t in (await raw_call(http, r.url, sid, "tools/list", {}, 3))[-1]["result"]["tools"]}
            assert "a_run_script" not in names and "a_echo" not in names
            assert "a_pid" in names and "b_run_script" in names and "b_echo" in names  # per child
            err = (await raw_call(http, r.url, sid, "tools/call", {"name": "a_run_script", "arguments": {}}, 4))[-1]["error"]
            assert err["code"] == -32602 and "a_run_script is disabled" in err["message"]
            err = (await raw_call(http, r.url, sid, "tools/call", {"name": "a_nope", "arguments": {}}, 5))[-1]["error"]
            assert "Unknown tool" in err["message"]


async def test_denied_tool_stays_hidden_after_list_changed():
    with run_app([deny_spec()]) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            await raw_call(http, r.url, sid, "tools/call", {"name": "a_add_tool", "arguments": {"name": "echo2"}})
            await raw_call(http, r.url, sid, "tools/call", {"name": "a_add_tool", "arguments": {"name": "run_script"}}, 3)
            names = {t["name"] for t in (await raw_call(http, r.url, sid, "tools/list", {}, 4))[-1]["result"]["tools"]}
            assert "a_echo2" in names and "a_run_script" not in names
