import json
import sys

import anyio
import pytest

from conftest import FAKE, raw_call, raw_init, run_app, wait_until
from rebox_aggregator.config import Autosave, ChildSpec

pytestmark = pytest.mark.anyio

ARGS = {"path": "{state_dir}/ev/{session}.json"}


def spec(name="a", prefix="a_", autosave=True, **env):
    return ChildSpec(name, prefix, (sys.executable, FAKE, name), env, Autosave("save", ARGS) if autosave else None)


def saved_file(state, sid):
    import hashlib

    return state / "ev" / f"{hashlib.sha256(sid.encode()).hexdigest()[:16]}.json"


async def call(http, r, sid, name, rid=2):
    return await raw_call(http, r.url, sid, "tools/call", {"name": name, "arguments": {}}, rid)


async def test_autosave_on_delete_writes_file_and_logs(tmp_path):
    with run_app([spec(), spec("b", "b_", autosave=False)], state_dir=str(tmp_path), autosave_interval=0) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            await call(http, r, sid, "a_echo")
            assert not saved_file(tmp_path, sid).exists()
            assert (await http.delete(r.url, headers={"mcp-session-id": sid})).status_code == 200
        assert json.loads(saved_file(tmp_path, sid).read_text()) == {"saves": 1}
        (e,) = r.logged("autosave")
        assert (e["child"], e["reason"], e["outcome"]) == ("a", "close", "ok") and "path" not in e


async def test_not_dirty_means_no_call(tmp_path):
    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            await http.delete(r.url, headers={"mcp-session-id": sid})
        assert not r.logged("autosave") and not (tmp_path / "ev").exists()


async def test_disabled_without_state_dir():
    with run_app([spec()], state_dir="") as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            await call(http, r, sid, "a_echo")
            await http.delete(r.url, headers={"mcp-session-id": sid})
        assert not r.logged("autosave")


async def test_periodic_autosave_skips_clean_children(tmp_path):
    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0.3) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            await anyio.sleep(0.8)
            assert not r.logged("autosave")
            await call(http, r, sid, "a_echo")
            assert await wait_until(lambda: saved_file(tmp_path, sid).exists(), 5)
            await anyio.sleep(0.8)
            assert [e["reason"] for e in r.logged("autosave")] == ["interval"]  # clean again: no second save


async def test_tool_error_is_logged_and_does_not_block_close(tmp_path):
    with run_app([spec(FAKE_SAVE_FAIL="1")], state_dir=str(tmp_path), autosave_interval=0) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            await call(http, r, sid, "a_echo")
            await http.delete(r.url, headers={"mcp-session-id": sid})
        (e,) = r.logged("autosave")
        assert e["outcome"] == "tool_error" and e["error"] == "disk full" and e["level"] == "warn"


async def test_timeout_bounds_close(tmp_path):
    import time

    with run_app([spec(FAKE_SAVE_SLEEP="30")], state_dir=str(tmp_path), autosave_interval=0, autosave_timeout=1.0) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            await call(http, r, sid, "a_echo")
            t = time.monotonic()
            assert (await http.delete(r.url, headers={"mcp-session-id": sid})).status_code == 200
            assert time.monotonic() - t < 12
        (e,) = r.logged("autosave")
        assert e["outcome"] == "timeout"


async def test_autosave_does_not_refresh_idle_clock(tmp_path):
    with run_app([spec()], state_dir=str(tmp_path), autosave_interval=0.2) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            await call(http, r, sid, "a_echo")
            session = r.app.sessions[sid]
            before = session.last_activity
            assert await wait_until(lambda: r.logged("autosave"), 5)
            assert session.last_activity == before
