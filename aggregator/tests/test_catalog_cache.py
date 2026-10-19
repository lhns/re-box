"""Catalog cache: advertise a child's full catalog before its backend is up, hold calls until it is."""

import json
import sys
import time

import anyio
import pytest

from conftest import FAKE, FakeBackend, raw_call, raw_init, run_app, wait_until
from rebox_aggregator import catalogcache
from rebox_aggregator.catalog import ChildCatalog
from rebox_aggregator.config import CatalogCacheSpec, ChildSpec, OnReady

pytestmark = pytest.mark.anyio


def spec(cold=True, key="k1", **env):
    return ChildSpec(
        "a", "a_", (sys.executable, FAKE, "a"), {**({"FAKE_COLD": "1"} if cold else {}), **env}, None,
        OnReady("connect_instance", {"project": "rebox"}), (), CatalogCacheSpec(key),
    )


def tool(name):
    return {"name": name, "description": name, "inputSchema": {"type": "object", "properties": {}}}


def cache_file(state, s=None):
    return catalogcache.path_for(str(state), s or spec())


def seed(state, names=("echo", "pid", "ghost", "get_connects", "connect_instance")):
    catalogcache.save(str(state), spec(), ChildCatalog(tools=[tool(n) for n in names]))


async def tool_names(http, r, sid, rid=3):
    return {t["name"] for t in (await raw_call(http, r.url, sid, "tools/list", {}, rid))[-1]["result"]["tools"]}


# -- the cache itself -------------------------------------------------------

def test_key_from_env_and_file(tmp_path, monkeypatch):
    f = tmp_path / "VERSION"
    f.write_text("v7-abc\n")
    monkeypatch.setenv("SOME_FLAG", "0")
    s = ChildSpec("a", "a_", ("/bin/true",), {"OTHER": "x y"}, None, None, (),
                  CatalogCacheSpec(f"{{file:{f}}}-f{{SOME_FLAG}}-{{OTHER}}-{{MISSING}}"))
    assert catalogcache.resolve_key(s) == "v7-abc-f0-x_y-unset"
    assert catalogcache.path_for(str(tmp_path), s).name == "a-v7-abc-f0-x_y-unset.json"


def test_save_is_atomic_skips_equal_keeps_bigger_and_drops_other_keys(tmp_path):
    state = str(tmp_path)
    s1, s2 = spec(key="old"), spec(key="new")
    big = ChildCatalog(tools=[tool("a"), tool("b")])
    assert catalogcache.save(state, s1, big) and not catalogcache.save(state, s1, big)
    assert catalogcache.load(state, s1).tools == big.tools
    assert not catalogcache.save(state, s1, ChildCatalog(tools=[tool("a")]))  # a smaller one is a cold bridge
    assert len(catalogcache.load(state, s1).tools) == 2
    assert catalogcache.save(state, s2, big)
    assert catalogcache.load(state, s1) is None and catalogcache.load(state, s2) is not None
    assert not list(cache_file(tmp_path, s2).parent.glob("*.tmp"))


def test_concurrent_saves_leave_a_valid_file(tmp_path):
    import threading
    s = spec()
    parts = [ChildCatalog(tools=[tool(f"t{i}_{j}") for j in range(10 + i)]) for i in range(8)]
    threads = [threading.Thread(target=catalogcache.save, args=(str(tmp_path), s, p)) for p in parts]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(json.loads(cache_file(tmp_path).read_text())["tools"]) == 17  # the biggest one won
    assert not list(cache_file(tmp_path).parent.glob("*.tmp"))


def test_load_rejects_garbage(tmp_path):
    p = cache_file(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert catalogcache.load(str(tmp_path), spec()) is None


# -- a session that opens while the backend is down ---------------------------

async def test_cached_catalog_is_served_cold_and_a_held_call_is_released(tmp_path):
    seed(tmp_path)
    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0, start_timeout=30.0) as r:
        backend = r.app.backend = FakeBackend(ready=False)
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            assert {"a_echo", "a_pid", "a_ghost"} <= await tool_names(http, r, sid)  # advertised from the cache
            started = time.monotonic()
            results = []

            async def held():
                results.append(await raw_call(http, r.url, sid, "tools/call", {"name": "a_echo", "arguments": {"text": "hi"}}, 7))

            async with anyio.create_task_group() as tg:
                tg.start_soon(held)
                await anyio.sleep(0.8)
                assert not results  # the bridge has no echo yet: the call waits
                backend.is_ready, backend.state = True, "ready"
                r.on_loop(r.app.backend_became_ready)
            assert results[0][-1]["result"]["content"][0]["text"] == "hi"
            assert time.monotonic() - started >= 0.8
            names = await tool_names(http, r, sid, 8)
            assert "a_echo" in names and "a_ghost" not in names  # live catalog replaced the cache
        assert await wait_until(lambda: "ghost" not in cache_file(tmp_path).read_text(), 5)  # cache updated
        assert json.loads(cache_file(tmp_path).read_text())["tools"].__len__() > 5


async def test_ready_backend_but_unconnected_bridge_serves_the_cache(tmp_path):
    # Ghidra is up, but the bridge booted before it connected: its listing is the static one.
    seed(tmp_path)
    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0, start_timeout=30.0) as r:
        r.app.backend = FakeBackend(ready=True)
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            assert "a_echo" in await tool_names(http, r, sid)  # the cache or the connected listing, never the static one
            msgs = await raw_call(http, r.url, sid, "tools/call", {"name": "a_echo", "arguments": {"text": "hi"}}, 7)
            assert msgs[-1]["result"]["content"][0]["text"] == "hi"  # held until connected, then served


async def test_static_tools_are_callable_while_cold(tmp_path):
    seed(tmp_path)
    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0, start_timeout=30.0) as r:
        r.app.backend = FakeBackend(ready=False)
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            t = time.monotonic()
            msgs = await raw_call(http, r.url, sid, "tools/call", {"name": "a_get_connects", "arguments": {}}, 5)
            assert msgs[-1]["result"]["content"][0]["text"] == "[]" and time.monotonic() - t < 3


async def test_held_call_times_out_with_a_clear_error(tmp_path):
    seed(tmp_path)
    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0, start_timeout=1.0) as r:
        r.app.backend = FakeBackend(ready=False)
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            res = (await raw_call(http, r.url, sid, "tools/call", {"name": "a_echo", "arguments": {}}, 5))[-1]["result"]
            assert res["isError"] and "still starting" in res["content"][0]["text"]


async def test_held_call_fails_fast_with_the_backend_reason(tmp_path):
    seed(tmp_path)
    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0, start_timeout=30.0) as r:
        backend = r.app.backend = FakeBackend(ready=False)
        backend.state, backend.reason = "failed", "project locked by another Ghidra instance"
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            t = time.monotonic()
            res = (await raw_call(http, r.url, sid, "tools/call", {"name": "a_echo", "arguments": {}}, 5))[-1]["result"]
            assert res["isError"] and "failed: project locked by another Ghidra instance" in res["content"][0]["text"]
            assert time.monotonic() - t < 5


async def test_held_call_gets_keepalive_progress(tmp_path):
    seed(tmp_path)
    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0, start_timeout=1.5, progress_keepalive=0.4) as r:
        r.app.backend = FakeBackend(ready=False)
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            msgs = await raw_call(http, r.url, sid, "tools/call", {
                "name": "a_echo", "arguments": {}, "_meta": {"progressToken": "p"}}, 5)
            assert [m for m in msgs if m.get("method") == "notifications/progress"]


async def test_replaced_cache_is_announced_on_the_standalone_stream(tmp_path):
    from mcp_types import ToolListChangedNotification

    seed(tmp_path)
    got = anyio.Event()

    async def handler(message):
        if isinstance(message, ToolListChangedNotification):
            got.set()

    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0, start_timeout=30.0) as r:
        backend = r.app.backend = FakeBackend(ready=False)
        async with r.client(message_handler=handler) as c:
            assert "a_ghost" in {t.name for t in (await c.list_tools()).tools}
            backend.is_ready, backend.state = True, "ready"
            r.on_loop(r.app.backend_became_ready)  # no request in flight: only the GET stream can carry it
            with anyio.fail_after(10):
                await got.wait()
            assert "a_ghost" not in {t.name for t in (await c.list_tools()).tools}


async def test_live_backend_at_open_writes_the_cache(tmp_path):
    with run_app([spec(cold=False)], state_dir=str(tmp_path), autosave_interval=0) as r:
        r.app.backend = FakeBackend(ready=True)
        async with r.raw() as http:
            await raw_init(http, r.url)
        assert await wait_until(cache_file(tmp_path).exists, 5)
        assert "echo" in {t["name"] for t in json.loads(cache_file(tmp_path).read_text())["tools"]}


# -- no cache yet -------------------------------------------------------------

async def test_initialize_is_held_until_the_child_is_connected_when_nothing_is_cached(tmp_path):
    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0, start_timeout=30.0) as r:
        backend = r.app.backend = FakeBackend(ready=False)

        async def come_up():
            await anyio.sleep(1.0)
            backend.is_ready, backend.state = True, "ready"

        async with r.raw() as http, anyio.create_task_group() as tg:
            tg.start_soon(come_up)
            t = time.monotonic()
            sid, _ = await raw_init(http, r.url)
            assert time.monotonic() - t >= 1.0
            assert {"a_echo", "a_pid"} <= await tool_names(http, r, sid)  # complete in the first list
        assert await wait_until(cache_file(tmp_path).exists, 5)


async def test_initialize_gives_up_waiting_with_the_static_catalog(tmp_path):
    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0, start_timeout=1.0) as r:
        r.app.backend = FakeBackend(ready=False)
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            assert await tool_names(http, r, sid) == {"a_connect_instance", "a_get_connects"}
        assert r.logged("catalog_cold")


# -- warm-up at container start -------------------------------------------------

async def test_warm_up_fills_a_missing_cache_and_releases_the_server(tmp_path):
    from rebox_aggregator.app import App
    from rebox_aggregator.config import Settings
    from rebox_aggregator.supervisor import validate, warm_up

    class StubHeadless:
        def __init__(self):
            self.calls = []

        def warm(self, on):
            self.calls.append(on)

        def ready(self):
            return True

    cfg = validate({"REBOX_TOKEN": "t", "RE_WORKDIR": str(tmp_path), "REBOX_HEADLESS_START_TIMEOUT": "20"})
    app = App(Settings("t", (spec(),), state_dir=str(cfg.state_dir)), instance="warmup-test")
    h = StubHeadless()
    await warm_up(cfg, app, h)  # type: ignore[arg-type]
    assert h.calls == [True, False]
    saved = catalogcache.load(str(cfg.state_dir), spec())
    assert saved and "echo" in {t["name"] for t in saved.tools}  # the live list, not the cold one
    h.calls.clear()
    await warm_up(cfg, app, h)  # type: ignore[arg-type]
    assert h.calls == []  # cache present: the server stays down
