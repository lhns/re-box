"""Helpers shared by the e2e tests: fixtures, this instance's processes, ghidra-mcp's HTTP API and
the shared project (mcpclient.py and stdiomcp.py are the two MCP clients)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import urllib.request
from pathlib import Path

from conftest import INSTANCE, WORKDIR
from mcpclient import McpError

FIXTURES = Path("/opt/rebox/fixtures")
GHIDRA_URL = "http://127.0.0.1:8089"


def copy_fixture(name: str, dest: str) -> str:
    path = WORKDIR / dest
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / name, path)
    return str(path)


def sha256_of(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- processes of this instance (REBOX_INSTANCE in their environment; tests/run-tests.sh)
def instance_procs():
    """(pid, environment entries) of every process of this instance that the test user can read."""
    want = f"REBOX_INSTANCE={INSTANCE}".encode()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            env = set((proc / "environ").read_bytes().split(b"\0"))
        except OSError:
            continue
        if want in env:
            yield int(proc.name), env


def instance_processes(marker: str) -> list[int]:
    """pids of this instance whose command line contains `marker`."""
    found = []
    for pid, _ in instance_procs():
        try:
            if marker.encode() in Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" "):
                found.append(pid)
        except OSError:
            continue
    return found


def session_processes(tag: str) -> dict[int, int]:
    """pid -> pgid of every process of this instance carrying the session's tag."""
    found = {}
    for pid, env in instance_procs():
        if f"REBOX_SESSION={tag}".encode() in env:
            try:
                found[pid] = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[2])
            except OSError:
                continue
    return found


def processes_in_groups(pgids: set[int]) -> list[int]:
    pids = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                if int((proc / "stat").read_text().rsplit(")", 1)[1].split()[2]) in pgids:
                    pids.append(int(proc.name))
            except OSError:
                continue
    return pids


def tag_of(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def ghidra_mcp_jvm() -> list[int]:
    return instance_processes("GhidraMCPHeadlessServer")


def rea_jvms() -> list[int]:
    return instance_processes("ghidra.app.util.headless.AnalyzeHeadless")


def helpers() -> list[int]:
    return instance_processes("rea-attach/helper.mjs")


# --- ghidra-mcp's own HTTP API on 8089
def ghidra_token() -> str | None:
    """The GHIDRA_MCP_AUTH_TOKEN the supervisor gave the ghidra-mcp JVM; None when it sets none."""
    for pid, env in instance_procs():
        if pid in ghidra_mcp_jvm():
            for entry in env:
                if entry.startswith(b"GHIDRA_MCP_AUTH_TOKEN="):
                    return entry.split(b"=", 1)[1].decode() or None
    return os.environ.get("GHIDRA_MCP_AUTH_TOKEN") or None


def ghidra_post(path: str, timeout: float = 600) -> bytes:
    token = ghidra_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(GHIDRA_URL + path, data=b"", method="POST", headers=headers)
    return urllib.request.urlopen(request, timeout=timeout).read()


# --- MCP results
def ok(call):
    assert not call.result.get("isError"), call.text()[:2000]
    return call


def jtext(call):
    return json.loads(call.text())


def wait_for(predicate, seconds: float, what: str):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def rea_programs(gh) -> list[str]:
    """Paths of the programs under /rea/ in the shared project, listed through a ghidra_ session."""

    def listing(folder: str) -> dict:
        try:
            call = gh.tool("ghidra_list_project_files", {"folder": folder})
        except McpError:
            return {}
        if call.result.get("isError"):  # the folder does not exist yet
            return {}
        return json.loads(call.text())

    paths = []
    for entry in listing("/rea").get("folders", []):
        name = entry if isinstance(entry, str) else entry.get("path") or entry.get("name", "")
        folder = name if name.startswith("/rea/") else f"/rea/{name.strip('/')}"
        paths += [f["path"] for f in listing(folder).get("files", [])]
    return paths


def rea_program_path(gh, target: str) -> str | None:
    """The shared-project path of REA's program for `target`: /rea/<sha256[:12]>-<profile digest[:8]>/<name>."""
    prefix = f"/rea/{sha256_of(target)[:12]}-"
    matches = [p for p in rea_programs(gh) if p.startswith(prefix) and p.endswith(f"/{Path(target).name}")]
    assert len(matches) <= 1, matches
    return matches[0] if matches else None
