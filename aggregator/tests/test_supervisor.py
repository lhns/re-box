import json
import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import anyio
import httpx2
import pytest

from rebox_aggregator import log as rlog
from rebox_aggregator.config import ConfigError
from rebox_aggregator.supervisor import Headless, apply_state_env, ensure_project, headless_command, validate

pytestmark = pytest.mark.anyio


def env(tmp_path, **extra):
    return {"REBOX_TOKEN": "t", "RE_WORKDIR": str(tmp_path), **extra}


def test_valid_defaults(tmp_path):
    cfg = validate(env(tmp_path))
    state = tmp_path / "re-box-state"
    assert (cfg.workdir, cfg.state_dir, cfg.projects_dir) == (tmp_path, state, state / "ghidra-projects")
    for sub in ("ghidra-projects", "home", "cache", "rea/evidence"):
        assert (state / sub).is_dir()


def test_empty_state_dir_means_default(tmp_path):
    assert validate(env(tmp_path, REBOX_STATE_DIR="")).state_dir == tmp_path / "re-box-state"


def test_state_dir_relative_refused(tmp_path):
    with pytest.raises(ConfigError, match="REBOX_STATE_DIR.*absolute"):
        validate(env(tmp_path, REBOX_STATE_DIR="state"))
    assert not (tmp_path / "state").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_state_dir_unwritable_is_one_line(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir(mode=0o555)
    with pytest.raises(ConfigError, match="REBOX_STATE_DIR .* is not writable by uid") as e:
        validate(env(tmp_path, REBOX_STATE_DIR=str(ro / "state")))
    assert "\n" not in str(e.value)


def test_state_dir_with_dot_element(tmp_path):
    with pytest.raises(ConfigError, match="starting with '.'"):
        validate(env(tmp_path, REBOX_STATE_DIR=str(tmp_path / ".cache" / "x")))
    assert not (tmp_path / ".cache").exists()


def test_state_dir_outside_workdir(tmp_path):
    work, state = tmp_path / "work", tmp_path / "state"
    work.mkdir()
    cfg = validate(env(tmp_path, RE_WORKDIR=str(work), REBOX_STATE_DIR=str(state)))
    assert cfg.projects_dir == state / "ghidra-projects" and (state / "home").is_dir()


def test_apply_state_env(tmp_path, monkeypatch):
    cfg = validate(env(tmp_path))
    monkeypatch.setattr(os, "environ", dict(os.environ))
    apply_state_env(cfg)
    s = str(tmp_path / "re-box-state")
    assert (os.environ["REBOX_STATE_DIR"], os.environ["HOME"], os.environ["XDG_CACHE_HOME"]) == (s, s + "/home", s + "/cache")
    assert os.environ["XDG_CONFIG_HOME"] == s + "/home/.config" and os.environ["XDG_DATA_HOME"] == s + "/home/.local/share"
    assert os.environ["XDG_STATE_HOME"] == s + "/home/.local/state"


def test_autosave_interval_validated(tmp_path):
    assert validate(env(tmp_path)).autosave_interval == 300
    assert validate(env(tmp_path, REBOX_AUTOSAVE_INTERVAL="0")).autosave_interval == 0
    for bad in ("-1", "x"):
        with pytest.raises(ConfigError, match="AUTOSAVE_INTERVAL"):
            validate(env(tmp_path, REBOX_AUTOSAVE_INTERVAL=bad))


def test_idle_and_retention_validated(tmp_path):
    cfg = validate(env(tmp_path))
    assert (cfg.idle_timeout, cfg.evidence_retention_days) == (300, 30)
    cfg = validate(env(tmp_path, REBOX_GHIDRA_IDLE_TIMEOUT="0", REBOX_EVIDENCE_RETENTION_DAYS="0"))
    assert (cfg.idle_timeout, cfg.evidence_retention_days) == (0, 0)
    for key in ("REBOX_GHIDRA_IDLE_TIMEOUT", "REBOX_EVIDENCE_RETENTION_DAYS"):
        for bad in ("-1", "x"):
            with pytest.raises(ConfigError, match=key):
                validate(env(tmp_path, **{key: bad}))


def test_state_dir_gets_a_gitignore(tmp_path):
    cfg = validate(env(tmp_path))
    assert (cfg.state_dir / ".gitignore").read_text() == "*\n"
    (cfg.state_dir / ".gitignore").write_text("mine\n")
    validate(env(tmp_path))
    assert (cfg.state_dir / ".gitignore").read_text() == "mine\n"  # never overwritten


def test_missing_token(tmp_path):
    with pytest.raises(ConfigError, match="REBOX_TOKEN"):
        validate({"RE_WORKDIR": str(tmp_path)})


def test_missing_workdir(tmp_path):
    with pytest.raises(ConfigError, match="RE_WORKDIR"):
        validate({"REBOX_TOKEN": "t"})
    with pytest.raises(ConfigError, match="RE_WORKDIR"):
        validate(env(tmp_path, RE_WORKDIR=str(tmp_path / "missing")))


def test_dot_prefixed_projects_path(tmp_path):
    with pytest.raises(ConfigError, match="starting with '.'"):
        validate(env(tmp_path, GHIDRA_PROJECTS_DIR=str(tmp_path / ".ghidra-projects")))
    with pytest.raises(ConfigError, match="starting with '.'"):
        validate(env(tmp_path, GHIDRA_PROJECTS_DIR=str(tmp_path / "a" / ".b" / "c")))


def test_projects_dir_override_anywhere(tmp_path):
    work, other = tmp_path / "work", tmp_path / "other"
    work.mkdir()
    cfg = validate(env(tmp_path, RE_WORKDIR=str(work), GHIDRA_PROJECTS_DIR=str(other)))
    assert cfg.projects_dir == other and other.is_dir()


def test_headless_command(tmp_path):
    cmd = headless_command(validate(env(tmp_path, JAVA_OPTS="-Xmx2g -Dx=y", GHIDRA_INSTALL_DIR="/g")))
    assert cmd[:3] == ["java", "-Xmx2g", "-Dx=y"]
    state = tmp_path / "re-box-state"
    assert "-Dghidra.home=/g" in cmd and f"-Duser.home={state}/home" in cmd
    assert cmd[-7:] == ["com.xebyte.headless.GhidraMCPHeadlessServer", "--port", "8089", "--bind", "127.0.0.1",
                        "--project", f"{state}/ghidra-projects/rebox.gpr"]


class FakeHeadless:
    """Tiny stand-in for the ghidra-mcp headless HTTP API; records POSTs."""

    def __init__(self, has_project: bool):
        outer = self
        self.has_project, self.posts = has_project, []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, body):
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._reply({"has_project": outer.has_project} if self.path == "/get_project_info" else {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.posts.append((self.path, body))
                self._reply({"success": True})

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


async def run_ensure(has_project: bool, projects: Path):
    fake = FakeHeadless(has_project)
    try:
        async with httpx2.AsyncClient() as http:
            await ensure_project(http, fake.base, projects)
        return fake.posts
    finally:
        fake.close()


async def test_ensure_project_already_open(tmp_path):
    assert await run_ensure(True, tmp_path) == []


async def test_ensure_project_creates_when_missing(tmp_path):
    assert await run_ensure(False, tmp_path) == [("/create_project", {"parentDir": str(tmp_path), "name": "rebox"})]


async def test_ensure_project_opens_existing_gpr(tmp_path):
    (tmp_path / "rebox.gpr").write_text("")
    assert await run_ensure(False, tmp_path) == [("/open_project", {"path": str(tmp_path / "rebox.gpr")})]


class SaveServer:
    """POST /save_all_programs stand-in: counts calls, holds each for `delay` seconds."""

    def __init__(self, delay=0.0, saved=1, status=200):
        outer = self
        self.calls, self.active, self.max_active = 0, 0, 0

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                outer.calls += 1
                outer.active += 1
                outer.max_active = max(outer.max_active, outer.active)
                time.sleep(delay)
                outer.active -= 1
                raw = json.dumps({"saved_count": saved, "errors": []}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def headless(tmp_path, srv, **extra):
    h = Headless(validate(env(tmp_path, **extra)), "inst", srv.base)
    h._healthy = True
    return h


async def test_save_all_logs_only_when_something_saved(tmp_path):
    events = []
    rlog.sink = events.append
    try:
        await headless(tmp_path, SaveServer(saved=0)).save_all("interval")
        await headless(tmp_path, SaveServer(saved=2)).save_all("interval")
    finally:
        rlog.sink = None
    assert [(e["event"], e["saved_count"], e["component"]) for e in events] == [("autosave", 2, "ghidra")]


async def test_save_all_never_overlaps_and_skips_when_unhealthy(tmp_path):
    srv = SaveServer(delay=0.5)
    h = headless(tmp_path, srv)
    async with anyio.create_task_group() as tg:
        tg.start_soon(h.save_all, "interval")
        tg.start_soon(h.save_all, "shutdown")
    assert (srv.calls, srv.max_active) == (1, 1)
    h._healthy = False
    await h.save_all("interval")
    assert srv.calls == 1


async def test_save_all_failure_warns_without_raising(tmp_path):
    events = []
    rlog.sink = events.append
    try:
        await headless(tmp_path, SaveServer(status=500)).save_all("interval")
    finally:
        rlog.sink = None
    assert events and events[0]["level"] == "warn" and events[0]["event"] == "autosave"
