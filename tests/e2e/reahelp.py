"""Helpers shared by the REA attach tests (test_rea_attach.py, test_rea_profiles.py)."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from conftest import INSTANCE, WORKDIR
from mcpclient import McpError

FIXTURES = Path("/opt/rebox/fixtures")


def copy_fixture(name: str, dest: str) -> str:
    path = WORKDIR / dest
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / name, path)
    return str(path)


def sha256_of(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def instance_processes(marker: str) -> list[int]:
    """pids of this instance whose command line contains `marker`."""
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if f"REBOX_INSTANCE={INSTANCE}".encode() not in (proc / "environ").read_bytes().split(b"\0"):
                continue
            if marker.encode() in (proc / "cmdline").read_bytes().replace(b"\0", b" "):
                found.append(int(proc.name))
        except OSError:
            continue
    return found


def ghidra_mcp_jvm() -> list[int]:
    return instance_processes("GhidraMCPHeadlessServer")


def rea_jvms() -> list[int]:
    return instance_processes("ghidra.app.util.headless.AnalyzeHeadless")


def helpers() -> list[int]:
    return instance_processes("rea-attach/helper.mjs")


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
