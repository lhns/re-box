"""Regression tests for the adversarial-review findings."""

import os
import signal
import subprocess
import sys

import anyio
import pytest

from conftest import (
    INIT, RAW_HEADERS, fake_spec, pid_alive, raw_call, raw_init, run_app, wait_until,
)
from rebox_aggregator import session as session_mod
from rebox_aggregator.children import Child, ChildError
from rebox_aggregator.config import ChildSpec, ConfigError, read_token
from rebox_aggregator.session import Session

pytestmark = pytest.mark.anyio


async def tool_text(http, url, sid, name, args=None, rid=2):
    msgs = await raw_call(http, url, sid, "tools/call", {"name": name, "arguments": args or {}}, rid)
    return msgs[-1]["result"]["content"][0]["text"]


def headers_for(sid):
    return {**RAW_HEADERS, "mcp-session-id": sid, "mcp-protocol-version": "2025-11-25"}


# 1. malformed ids and tokens stay session-local

async def test_hostile_ids_do_not_break_anything(running):
    async with running.raw() as http:
        other, _ = await raw_init(http, running.url)
        sid, _ = await raw_init(http, running.url)
        r = await http.post(running.url, headers=headers_for(sid), json={
            "jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": [1]}})
        assert r.status_code == 202
        assert await tool_text(http, running.url, sid, "a_bad_msgs", rid=3) == "sent"  # child sends list ids/tokens
        assert await tool_text(http, running.url, sid, "b_echo", {"text": "alive"}, rid=4) == "alive"
        assert await tool_text(http, running.url, other, "a_echo", {"text": "other"}, rid=3) == "other"
        await raw_init(http, running.url)  # new sessions still open
    assert not running.logged("internal_error")


async def test_unexpected_exception_closes_only_that_session(running, monkeypatch):
    async with running.raw() as http:
        other, _ = await raw_init(http, running.url)
        sid, _ = await raw_init(http, running.url)

        async def boom(self, msg):
            raise RuntimeError("boom")

        monkeypatch.setattr(Session, "_handle_notification", boom)
        await http.post(running.url, headers=headers_for(sid), json={"jsonrpc": "2.0", "method": "notifications/x"})
        assert await wait_until(lambda: sid not in {s.sid for s in running.app.sessions.values()}, 10)
        monkeypatch.undo()
        assert running.logged("internal_error")
        assert await tool_text(http, running.url, other, "a_echo", {"text": "ok"}, rid=3) == "ok"
        await raw_init(http, running.url)


# 2. token file and dumpable

def test_token_file(tmp_path):
    f = tmp_path / "t"
    f.write_text("from-file\n")
    assert read_token({"REBOX_TOKEN_FILE": str(f), "REBOX_TOKEN": "env"}) == "from-file"
    assert read_token({"REBOX_TOKEN": "env"}) == "env"
    f.write_text("\n")
    for env in ({}, {"REBOX_TOKEN": ""}, {"REBOX_TOKEN_FILE": str(f)}, {"REBOX_TOKEN_FILE": str(tmp_path / "missing")}):
        with pytest.raises(ConfigError):
            read_token(env)


def test_harden_keeps_children_and_sweep_working():
    code = """
import os, subprocess, sys, time
from rebox_aggregator.config import harden
from rebox_aggregator.children import kill_leftovers
harden()
assert open('/proc/self/status').read().count('\\n') and os.stat('/proc/self/environ').st_uid == 0
env = {**os.environ, 'REBOX_INSTANCE': 'hard-1', 'REBOX_SESSION': 'abc'}
p = subprocess.Popen(['sleep', '60'], env=env)
time.sleep(0.2)
# a hardened parent can still read its child's environ (dumpable resets on exec), so the sweep works
assert b'REBOX_SESSION=abc' in open(f'/proc/{p.pid}/environ', 'rb').read()
assert kill_leftovers('abc', 'hard-1') == 1
assert p.wait(5) == -9
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)


def test_harden_drops_dumpable():
    code = "import ctypes; from rebox_aggregator.config import harden; harden(); print(ctypes.CDLL(None).prctl(3,0,0,0,0))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "0"


# 3. flow control

async def test_child_stdin_writes_never_block():
    class Handler:
        async def on_request(self, child, msg): ...
        async def on_notification(self, child, msg): ...
        async def on_exit(self, child): ...

    spec = ChildSpec("sleeper", "s_", ("/bin/sleep", "30"))
    child = Child(spec, "t", dict(os.environ), 1.0, Handler())
    async with anyio.create_task_group() as tg:
        await child.start(tg)
        with anyio.fail_after(3):  # the child never reads; a direct pipe write would block after ~64 KiB
            for _ in range(20):
                await child.notify("x", {"pad": "p" * 1_000_000})
        await child.close()
        tg.cancel_scope.cancel()
    assert not pid_alive(child.pid)


async def test_notifications_dropped_when_backlog_is_large(running):
    s = Session("sid", object(), running.settings, "i", lambda s: None)  # type: ignore[arg-type]
    s._queued = session_mod.MAX_QUEUED_NOTIFICATIONS + 1
    await s._notify_down("notifications/progress", {})
    await s._notify_down("notifications/progress", {})
    assert s._queued == session_mod.MAX_QUEUED_NOTIFICATIONS + 1  # dropped
    await s._notify_down("notifications/tools/list_changed", None, droppable=False)
    assert s._queued == session_mod.MAX_QUEUED_NOTIFICATIONS + 2  # list_changed is kept
    assert len(running.logged("downstream_backpressure")) == 1


# 4. bounded initialize, idle with in-flight requests

async def test_hung_child_initialize_times_out():
    with run_app([fake_spec("a", "a_"), fake_spec("b", "b_", FAKE_HANG_INIT="1")], init_timeout=1.5) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            names = {t["name"] for t in (await raw_call(http, r.url, sid, "tools/list", {}))[-1]["result"]["tools"]}
        assert "a_echo" in names and "b_echo" not in names
        failed = r.logged("child_init_failed")
        assert failed and failed[0]["child"] == "b"
        hung = [c for c in r.app.sessions[sid].children.values() if c.name == "b"][0]
        assert not pid_alive(hung.pid)


async def test_session_with_stuck_request_is_reaped_after_4x_idle():
    with run_app(idle_timeout=0.5) as r:
        async with r.raw() as http:
            sid, _ = await raw_init(http, r.url)
            async with anyio.create_task_group() as tg:
                tg.start_soon(raw_call, http, r.url, sid, "tools/call", {"name": "a_slow"}, 5)
                await anyio.sleep(1.2)
                assert sid in r.app.sessions  # in flight and only 2.4x idle
                assert await wait_until(lambda: not r.app.sessions, 10)
                tg.cancel_scope.cancel()
        assert r.logged("session_close")[0]["reason"] == "idle"


# 5. bounded input

async def test_oversize_bodies_get_413():
    with run_app(max_body=2000) as r:
        async with r.raw() as http:
            big = {**INIT, "params": {**INIT["params"], "pad": "x" * 5000}}
            res = await http.post(r.url, json=big, headers=RAW_HEADERS)
            assert res.status_code == 413 and res.json()["error"]["code"] == -32600
            sid, _ = await raw_init(http, r.url)
            res = await http.post(r.url, headers=headers_for(sid), json={
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "a_echo", "arguments": {"text": "x" * 5000}}})
            assert res.status_code == 413
            assert await tool_text(http, r.url, sid, "a_echo", {"text": "small"}, rid=6) == "small"


async def test_too_many_concurrent_requests(running, monkeypatch):
    monkeypatch.setattr(session_mod, "MAX_INFLIGHT", 3)
    async with running.raw() as http:
        sid, _ = await raw_init(http, running.url)
        async with anyio.create_task_group() as tg:
            for i in range(3):
                tg.start_soon(raw_call, http, running.url, sid, "tools/call", {"name": "a_slow"}, 10 + i)
            await anyio.sleep(1)
            msgs = await raw_call(http, running.url, sid, "tools/call", {"name": "a_echo", "arguments": {}}, 20)
            assert msgs[-1]["error"]["code"] == -32000 and "too many" in msgs[-1]["error"]["message"]
            tg.cancel_scope.cancel()


# 6. close racing spawn

async def test_closed_child_refuses_to_spawn(tmp_path):
    marker = tmp_path / "spawned"
    spec = ChildSpec("x", "x_", ("/bin/sh", "-c", f"touch {marker}; sleep 5"))
    child = Child(spec, "t", dict(os.environ), 1.0, object())  # type: ignore[arg-type]
    await child.close()  # session started closing before this child was spawned
    async with anyio.create_task_group() as tg:
        with pytest.raises(ChildError):
            await child.start(tg)
    await anyio.sleep(0.3)
    assert child.proc is None and not marker.exists()


# 7. duplicate request ids

async def test_duplicate_request_id_rejected(running):
    async with running.raw() as http:
        sid, _ = await raw_init(http, running.url)
        async with anyio.create_task_group() as tg:
            tg.start_soon(raw_call, http, running.url, sid, "tools/call", {"name": "a_slow"}, 77)
            await anyio.sleep(1)
            msgs = await raw_call(http, running.url, sid, "tools/call", {"name": "a_echo", "arguments": {}}, 77)
            assert msgs[-1]["error"]["code"] == -32600
            tg.cancel_scope.cancel()


# 8, 9. supervisor

async def test_health_reason_is_fixed_text(tmp_path):
    from rebox_aggregator.supervisor import Headless, validate

    cfg = validate({"REBOX_TOKEN": "t", "RE_WORKDIR": str(tmp_path), "REBOX_HEADLESS_START_TIMEOUT": "5"})
    assert Headless(cfg, "i").health() == (True, "", "stopped")  # nothing runs until a session wants it

    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): ...

        def do_GET(self):
            if self.path == "/get_project_info":
                self.send_response(500)
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"SECRET")
            else:
                raw = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        h = Headless(cfg, "i", base=f"http://127.0.0.1:{server.server_port}")
        proc = await anyio.open_process(["sleep", "60"], start_new_session=True)
        await h._monitor(proc, anyio.CancelScope())
        assert h.health() == (False, "project not open", "failed")
        await proc.wait()
        assert proc.returncode == -signal.SIGKILL
    finally:
        server.shutdown()


async def test_old_jvm_group_killed_before_restart(tmp_path, monkeypatch):
    from rebox_aggregator.supervisor import Headless, validate

    bindir = tmp_path / "bin"
    bindir.mkdir()
    pidfile = tmp_path / "pid"
    java = bindir / "java"
    java.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > {pidfile}\nexit 1\n")
    java.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    cfg = validate({"REBOX_TOKEN": "t", "RE_WORKDIR": str(tmp_path), "REBOX_HEADLESS_START_TIMEOUT": "5"})
    (cfg.projects_dir / "rebox.gpr").write_text("")  # skip project creation
    h = Headless(cfg, "i")
    h.want(True)
    with anyio.move_on_after(3):
        await h.run()  # returns only on stop; it sleeps in backoff after the JVM "exits"
    straggler = int(pidfile.read_text())
    assert await wait_until(lambda: not pid_alive(straggler), 5)
