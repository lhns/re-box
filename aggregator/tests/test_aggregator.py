import hashlib
import json

import anyio
import httpx2
import pytest
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from conftest import (
    INIT, RAW_HEADERS, TOKEN, fake_spec, pid_alive, raw_call, raw_init, run_app, sse_messages, text_of, wait_until,
)

pytestmark = pytest.mark.anyio


async def tool_text(http, url, sid, name, args=None, rid=2):
    msgs = await raw_call(http, url, sid, "tools/call", {"name": name, "arguments": args or {}}, rid)
    return msgs[-1]["result"]["content"][0]["text"]


async def raw_init_error(http, url) -> dict:
    r = await http.post(url, json=INIT, headers=RAW_HEADERS)
    return sse_messages(r.text)[0]["error"]


# -- catalog ---------------------------------------------------------------

async def test_tools_are_prefixed(running):
    async with running.client() as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert {"a_echo", "b_echo", "a_slow", "b_slow"} <= names
    assert not {"echo", "slow"} & names


async def test_prompts_namespaced_and_routed(running):
    async with running.client() as c:
        assert {p.name for p in (await c.list_prompts()).prompts} == {"a_greet", "b_greet"}
        r = await c.get_prompt("b_greet")
        assert "hello from b: greet" in r.messages[0].content.text


async def test_unknown_tool(running):
    async with running.raw() as http:
        sid, _ = await raw_init(http, running.url)
        msgs = await raw_call(http, running.url, sid, "tools/call", {"name": "echo", "arguments": {}})
    assert msgs[-1]["error"]["code"] == -32602 and "Unknown tool: echo" in msgs[-1]["error"]["message"]


async def test_collision_fails_initialize_and_names_tools():
    with run_app([fake_spec("a", ""), fake_spec("b", "")]) as r:
        async with r.raw() as http:
            err = await raw_init_error(http, r.url)
    assert "echo" in err["message"] and "provided by both a and b" in err["message"]
    assert r.app.sessions == {}


async def test_invalid_charset_fails_initialize():
    with run_app([fake_spec("a", "a_", FAKE_EXTRA_TOOLS="bad.name")]) as r:
        async with r.raw() as http:
            err = await raw_init_error(http, r.url)
    assert "a_bad.name" in err["message"]


async def test_name_length_limit():
    ok, too_long = "x" * 62, "x" * 63  # prefix "a_" brings these to 64 and 65
    with run_app([fake_spec("a", "a_", FAKE_EXTRA_TOOLS=ok)]) as r:
        async with r.raw() as http:
            await raw_init(http, r.url)
    with run_app([fake_spec("a", "a_", FAKE_EXTRA_TOOLS=too_long)]) as r:
        async with r.raw() as http:
            err = await raw_init_error(http, r.url)
    assert "a_" + too_long in err["message"]


async def test_child_failing_to_start_leaves_the_rest():
    with run_app([fake_spec("a", "a_"), fake_spec("b", "b_", FAKE_FAIL_INIT="1")]) as r:
        async with r.client() as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert "a_echo" in names and "b_echo" not in names
        assert r.logged("child_init_failed")
    with run_app([fake_spec("b", "b_", FAKE_FAIL_INIT="1")]) as r:
        async with r.raw() as http:
            err = await raw_init_error(http, r.url)
    assert "no backend started" in err["message"]


async def test_large_tools_list_line():
    with run_app([fake_spec("a", "a_", FAKE_BIG="3000000")]) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            msgs = await raw_call(http, r.url, sid, "tools/list", {})
    big = next(t for t in msgs[-1]["result"]["tools"] if t["name"] == "a_big")
    assert len(big["description"]) == 3_000_000


async def test_structured_content_passes_through_unchanged(running):
    expected = {"n": None, "f": 1.5, "i": 2**53, "nested": {"a": [None, 0, ""]}, "u": "é☃"}
    async with running.raw() as http:
        sid, _ = await raw_init(http, running.url)
        msgs = await raw_call(http, running.url, sid, "tools/call", {"name": "b_structured", "arguments": {}})
    result = msgs[-1]["result"]
    assert result["structuredContent"] == expected
    assert result["content"] == [{"type": "text", "text": "s"}]


# -- sessions --------------------------------------------------------------

async def test_sessions_are_isolated(running):
    async with running.client() as c1, running.client() as c2:
        async def info(c):
            pid = text_of(await c.call_tool("a_pid", {}))
            env = json.loads(text_of(await c.call_tool("a_env", {})))
            return pid, env["REBOX_SESSION"]

        (p1, s1), (p2, s2) = await info(c1), await info(c2)
        assert p1 != p2 and s1 != s2
        await c1.call_tool("a_set_state", {"value": "mine"})
        assert text_of(await c1.call_tool("a_get_state", {})) == "mine"
        assert text_of(await c2.call_tool("a_get_state", {})) == ""


async def test_child_env_has_no_rebox_token_and_gets_tags(running):
    async with running.raw() as http:
        sid, _ = await raw_init(http, running.url)
        env = json.loads(await tool_text(http, running.url, sid, "b_env"))
    assert "REBOX_TOKEN" not in env and TOKEN not in json.dumps(env)
    assert env["REBOX_INSTANCE"] == "testinstance"
    assert env["REBOX_SESSION"] == hashlib.sha256(sid.encode()).hexdigest()[:16]
    assert sorted(k for k in env if k.startswith("REBOX_")) == ["REBOX_INSTANCE", "REBOX_SESSION"]


async def test_delete_kills_process_group_including_grandchild(running):
    async with running.raw() as http:
        sid, _ = await raw_init(http, running.url)
        child = int(await tool_text(http, running.url, sid, "a_pid"))
        grandchild = int(await tool_text(http, running.url, sid, "a_spawn", rid=3))
        assert pid_alive(child) and pid_alive(grandchild)
        r = await http.delete(running.url, headers={"mcp-session-id": sid})
        assert r.status_code == 200
        assert not pid_alive(child) and not pid_alive(grandchild)
        r = await http.post(running.url, json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
                            headers={**RAW_HEADERS, "mcp-session-id": sid})
        assert r.status_code == 404
    assert running.logged("session_close")[0]["reason"] == "delete"


async def test_session_cap():
    with run_app(max_sessions=2, evict_min_idle=3600) as r:  # nothing is idle long enough to evict
        async with r.raw() as http:
            sid1, _ = await raw_init(http, r.url)
            await raw_init(http, r.url)
            res = await http.post(r.url, json=INIT, headers=RAW_HEADERS)
            assert res.status_code == 503
            err = res.json()["error"]
            assert err["code"] == -32000 and "session limit reached (2 of 2 active)" in err["message"]
            assert (await http.delete(r.url, headers={"mcp-session-id": sid1})).status_code == 200
            await raw_init(http, r.url)
        assert r.logged("session_refused")


async def test_idle_session_is_reaped():
    with run_app(idle_timeout=1.0) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            pid = int(await tool_text(http, r.url, sid, "a_pid"))
            assert await wait_until(lambda: not r.app.sessions, 10)
            assert not pid_alive(pid)
            res = await http.post(r.url, json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
                                  headers={**RAW_HEADERS, "mcp-session-id": sid})
            assert res.status_code == 404
        assert r.logged("session_close")[0]["reason"] == "idle"


async def test_busy_session_is_not_reaped():
    with run_app(idle_timeout=1.0) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            async with anyio.create_task_group() as tg:
                tg.start_soon(raw_call, http, r.url, sid, "tools/call", {"name": "a_slow"}, 5)
                await anyio.sleep(3)
                assert sid in r.app.sessions
                tg.cancel_scope.cancel()


# -- auth and era ----------------------------------------------------------

async def test_auth(running):
    async with httpx2.AsyncClient() as http:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic " + TOKEN}):
            r = await http.post(running.url, json=INIT, headers={**RAW_HEADERS, **headers})
            assert r.status_code == 401
            assert r.headers["www-authenticate"] == "Bearer" and "error" in r.json()
        assert (await http.get(running.base + "/healthz")).status_code == 200
        assert (await http.get(running.base + "/other", headers={"Authorization": f"Bearer {TOKEN}"})).status_code == 404
    assert not running.app.sessions


async def test_era_probe_gets_32022(running):
    probe = {"jsonrpc": "2.0", "id": 0, "method": "server/discover", "params": {}}
    async with running.raw() as http:
        r = await http.post(running.url, json=probe, headers={**RAW_HEADERS, "mcp-protocol-version": "2026-07-28"})
        assert r.status_code == 400
        err = r.json()["error"]
        assert err["code"] == -32022
        assert err["data"] == {"supported": list(HANDSHAKE_PROTOCOL_VERSIONS), "requested": "2026-07-28"}
        r = await http.post(running.url, json=INIT, headers={**RAW_HEADERS, "mcp-protocol-version": "2026-07-28"})
        assert r.status_code == 400 and r.json()["error"]["code"] == -32022
        assert (await http.get(running.url)).status_code == 400
    assert not running.app.sessions


# -- relays ----------------------------------------------------------------

async def test_progress_relayed_with_token(running):
    async with running.raw() as http:
        sid, _ = await raw_init(http, running.url)
        msgs = await raw_call(http, running.url, sid, "tools/call",
                              {"name": "a_progress", "arguments": {}, "_meta": {"progressToken": "tok-1"}})
    progress = [m for m in msgs if m.get("method") == "notifications/progress"]
    assert [m["params"]["progress"] for m in progress] == [1, 2, 3]
    assert {m["params"]["progressToken"] for m in progress} == {"tok-1"}
    assert msgs[-1]["result"]["content"][0]["text"] == "done"


async def test_progress_reaches_sdk_client_callback(running):
    seen = []

    async def cb(progress, total, message):
        seen.append(progress)

    async with running.client() as c:
        await c.call_tool("b_progress", {}, progress_callback=cb)
    assert seen == [1, 2, 3]


async def test_list_changed_relayed_before_response_and_new_tool_callable(running):
    async with running.raw() as http:
        sid, _ = await raw_init(http, running.url)
        msgs = await raw_call(http, running.url, sid, "tools/call",
                              {"name": "a_add_tool", "arguments": {"name": "dyn"}})
        assert [m.get("method") or "response" for m in msgs] == ["notifications/tools/list_changed", "response"]
        names = {t["name"] for t in (await raw_call(http, running.url, sid, "tools/list", {}, 3))[-1]["result"]["tools"]}
        assert "a_dyn" in names
        assert await tool_text(http, running.url, sid, "a_dyn", rid=4) == "dyn:dyn"


async def test_list_changed_reaches_sdk_client(running):
    from mcp_types import ToolListChangedNotification

    got = anyio.Event()

    async def handler(message):
        if isinstance(message, ToolListChangedNotification):
            got.set()

    async with running.client(message_handler=handler) as c:
        await c.call_tool("a_add_tool", {"name": "dyn2"})
        with anyio.fail_after(10):
            await got.wait()
        assert "a_dyn2" in {t.name for t in (await c.list_tools()).tools}
        assert text_of(await c.call_tool("a_dyn2", {})) == "dyn:dyn2"


async def test_rejected_refresh_keeps_previous_catalog(running):
    async with running.raw() as http:
        sid, _ = await raw_init(http, running.url)
        await raw_call(http, running.url, sid, "tools/call", {"name": "a_add_tool", "arguments": {"name": "bad.name"}})
        names = {t["name"] for t in (await raw_call(http, running.url, sid, "tools/list", {}, 3))[-1]["result"]["tools"]}
        assert "a_bad.name" not in names and "a_echo" in names
    assert running.logged("catalog_rejected")


async def test_cancellation_relayed_with_child_request_id(running):
    async with running.raw() as http:
        sid, _ = await raw_init(http, running.url)
        for i in range(3):  # make downstream and child request ids diverge
            await tool_text(http, running.url, sid, "a_echo", rid=10 + i)
        results = []

        async def slow():
            results.append(await raw_call(http, running.url, sid, "tools/call", {"name": "b_slow", "arguments": {}}, 77))

        async with anyio.create_task_group() as tg:
            tg.start_soon(slow)
            await anyio.sleep(1)
            r = await http.post(
                running.url,
                json={"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 77, "reason": "t"}},
                headers={**RAW_HEADERS, "mcp-session-id": sid, "mcp-protocol-version": "2025-11-25"},
            )
            assert r.status_code == 202
        assert results[0][-1]["id"] == 77 and "error" in results[0][-1]  # request settled without a result
        seen = json.loads(await tool_text(http, running.url, sid, "b_get_cancelled", rid=90))
        assert seen["slow_ids"] and seen["cancelled"] == seen["slow_ids"] and 77 not in seen["cancelled"]
        assert await tool_text(http, running.url, sid, "b_echo", {"text": "alive"}, rid=91) == "alive"
    assert [e["outcome"] for e in running.logged("tool_call")][-3] == "cancelled"


async def test_child_crash_closes_only_that_session(running):
    async with running.raw() as http:
        other, _ = await raw_init(http, running.url)
        sid, _ = await raw_init(http, running.url)
        msgs = await raw_call(http, running.url, sid, "tools/call", {"name": "a_crash", "arguments": {}})
        assert msgs[-1]["error"]["code"] == -32603 and "backend 'a' is not running" in msgs[-1]["error"]["message"]
        assert await wait_until(lambda: sid not in running.app.sessions, 10)  # half-dead: the client re-initializes
        r = await http.post(running.url, json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
                            headers={**RAW_HEADERS, "mcp-session-id": sid})
        assert r.status_code == 404
        assert await tool_text(http, running.url, other, "b_echo", {"text": "ok"}, rid=4) == "ok"
    assert running.logged("child_exit")[0]["exit_code"] == 3
    assert [e["reason"] for e in running.logged("session_close")][0] == "child_exited"


async def test_session_closes_when_any_child_dies(running):
    async with running.raw() as http:
        sid, _ = await raw_init(http, running.url)
        await raw_call(http, running.url, sid, "tools/call", {"name": "b_crash", "arguments": {}})
        assert await wait_until(lambda: not running.app.sessions, 10)
    assert running.logged("session_close")[0]["reason"] == "child_exited"


async def test_child_request_forwarded_to_client(running):
    from mcp_types import ListRootsResult, Root

    async def roots(ctx):
        return ListRootsResult(roots=[Root(uri="file:///w", name="w")])

    async with running.client(list_roots_callback=roots) as c:
        await c.call_tool("a_ask_roots", {})
        for _ in range(50):
            seen = json.loads(text_of(await c.call_tool("a_get_cancelled", {})))
            if seen["replies"]:
                break
            await anyio.sleep(0.1)
    reply = seen["replies"][0]
    assert reply["id"] == 900 and reply["result"]["roots"][0]["uri"] == "file:///w"


async def test_logs_have_no_tool_arguments(running):
    async with running.client() as c:
        await c.call_tool("a_echo", {"text": "TOPSECRET"})
        await c.call_tool("a_fail", {})
    assert "TOPSECRET" not in json.dumps(running.events)
    calls = running.logged("tool_call")
    assert [(e["tool"], e["outcome"]) for e in calls] == [("a_echo", "ok"), ("a_fail", "tool_error")]
    assert running.logged("session_open")[0]["children"][0]["pid"]
