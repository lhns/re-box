import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx2
import pytest

from rebox_aggregator.config import ConfigError
from rebox_aggregator.supervisor import ensure_project, headless_command, validate

pytestmark = pytest.mark.anyio


def env(tmp_path, **extra):
    return {"REBOX_TOKEN": "t", "RE_WORKDIR": str(tmp_path), **extra}


def test_valid_defaults(tmp_path):
    cfg = validate(env(tmp_path))
    assert cfg.projects_dir == tmp_path / "ghidra-projects"
    assert cfg.workdir == tmp_path


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


def test_projects_dir_outside_workdir(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ConfigError, match="inside RE_WORKDIR"):
        validate(env(tmp_path, RE_WORKDIR=str(work), GHIDRA_PROJECTS_DIR=str(tmp_path / "other")))
    with pytest.raises(ConfigError, match="inside RE_WORKDIR"):
        validate(env(tmp_path, RE_WORKDIR=str(work), GHIDRA_PROJECTS_DIR=str(work / ".." / "other")))
    assert validate(env(tmp_path, RE_WORKDIR=str(work), GHIDRA_PROJECTS_DIR=str(work / "p"))).projects_dir == work / "p"


def test_headless_command(tmp_path):
    cmd = headless_command(validate(env(tmp_path, JAVA_OPTS="-Xmx2g -Dx=y", GHIDRA_INSTALL_DIR="/g")))
    assert cmd[:3] == ["java", "-Xmx2g", "-Dx=y"]
    assert "-Dghidra.home=/g" in cmd and cmd[-5:] == [
        "com.xebyte.headless.GhidraMCPHeadlessServer", "--port", "8089", "--bind", "127.0.0.1"]


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
