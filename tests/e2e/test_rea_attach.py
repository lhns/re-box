"""REA's Ghidra sessions attach to the shared ghidra-mcp JVM (deps/rea-attach, DECISIONS.md).

Each test works on its own copy of a fixture: REA's edits now land in the shared project, where
other tests would see them.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import threading
import time
import urllib.request
from pathlib import Path

from conftest import INSTANCE, WORKDIR, record
from mcpclient import McpError

FIXTURES = Path("/opt/rebox/fixtures")


def copy_fixture(name: str, dest: str) -> str:
    path = WORKDIR / dest
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / name, path)
    return str(path)


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


def test_attach_shares_the_jvm_and_project(session):
    target = copy_fixture("hello_debug", "attach/one_debug")
    s = session()
    ok(s.tool("rea_open_binary", {"path": target, "provider_id": "ghidra"}))
    analysis = ok(s.tool("rea_analyze_function", {"procedure": "compute"}))
    record("attach", "rea_analyze_function (import + analysis via ghidra-mcp)", analysis.seconds)
    ok(s.tool("rea_annotate_native_function", {"procedure": "compute", "name": "AttachCompute",
                                               "comment": "set by REA"}))
    assert len(ghidra_mcp_jvm()) == 1 and not rea_jvms(), "REA started its own JVM"
    assert len(helpers()) == 1
    found = jtext(ok(s.tool("gh_find_functions", {"program": "one_debug", "name_pattern": "AttachCompute"})))
    assert [f["name"] for f in found["functions"]] == ["AttachCompute"]
    comments = jtext(ok(s.tool("gh_get_functions", {"program": "one_debug", "function": "AttachCompute",
                                                     "fields": "comments"})))["comments"]
    assert any(c["text"] == "set by REA" for c in comments)

    ok(s.tool("rea_close_binary", {}))
    wait_for(lambda: not helpers(), 15, "the attach helper to exit")
    assert len(ghidra_mcp_jvm()) == 1
    found = jtext(ok(s.tool("gh_find_functions", {"program": "one_debug", "name_pattern": "AttachCompute"})))
    assert found["functions"], "the REA rename is gone after close"


def test_raw_binary_falls_back_to_reas_jvm(session):
    target = copy_fixture("hello_stripped", "attach/raw_blob")
    s = session()
    ok(s.tool("rea_open_binary", {"path": target, "provider_id": "ghidra", "format": "raw-binary",
                                  "architecture": "x86_64", "entry_point": "0x1000"}))
    ok(s.tool("rea_binary_overview", {}))
    assert rea_jvms(), "a raw binary should run in REA's own JVM"
    assert not helpers()
    ok(s.tool("rea_close_binary", {}))
    wait_for(lambda: not rea_jvms(), 30, "REA's JVM to exit")


def test_same_name_other_bytes_gets_its_own_folder(session):
    first = copy_fixture("hello_debug", "attach/a/twin")
    second = copy_fixture("other_debug", "attach/b/twin")
    sha = hashlib.sha256(Path(second).read_bytes()).hexdigest()
    s = session()
    for target in (first, second):
        ok(s.tool("rea_open_binary", {"path": target, "provider_id": "ghidra"}))
        ok(s.tool("rea_analyze_function", {"procedure": "compute"}))
    opened = jtext(ok(s.tool("gh_open_program", {"path": f"/rea/{sha[:12]}/twin"})))
    assert opened.get("success", True), opened
    ok(s.tool("rea_close_binary", {}))


def test_jvm_restart_while_rea_has_a_binary_open(session):
    target = copy_fixture("hello_debug", "attach/restart_debug")
    s = session()
    ok(s.tool("rea_open_binary", {"path": target, "provider_id": "ghidra"}))
    ok(s.tool("rea_analyze_function", {"procedure": "compute"}))
    # Keep the other tests' project edits: the kill skips the shutdown save.
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8089/save_all_programs", data=b"",
                                                  method="POST"), timeout=600).read()
    [jvm] = ghidra_mcp_jvm()
    os.kill(jvm, signal.SIGKILL)
    wait_for(lambda: not helpers(), 20, "the attach helper to notice the JVM is gone")
    failed = s.tool("rea_annotate_native_function", {"procedure": "compute", "name": "AfterRestart"})
    assert failed.result.get("isError") and "open_binary" in failed.text(), failed.text()[:2000]
    # The supervisor restarts the JVM (the session is still open); reopening attaches again.
    t0 = time.monotonic()
    ok(s.tool("rea_open_binary", {"path": target, "provider_id": "ghidra"}))
    ok(s.tool("rea_annotate_native_function", {"procedure": "compute", "name": "AfterRestart"}))
    record("attach", "reopen after a JVM restart", time.monotonic() - t0)
    ok(s.tool("rea_close_binary", {}))


def test_concurrent_rea_sessions_share_one_program(session):
    target = copy_fixture("hello_debug", "attach/shared_debug")
    a, b = session(), session()
    errors = []

    def open_and_read(s):
        try:
            ok(s.tool("rea_open_binary", {"path": target, "provider_id": "ghidra"}))
            ok(s.tool("rea_analyze_function", {"procedure": "main"}))
        except (AssertionError, McpError) as e:
            errors.append(e)

    threads = [threading.Thread(target=open_and_read, args=(s,)) for s in (a, b)]
    [t.start() for t in threads]
    [t.join(600) for t in threads]
    assert not errors, errors
    assert len(helpers()) == 2 and len(ghidra_mcp_jvm()) == 1 and not rea_jvms()
    ok(a.tool("rea_annotate_native_function", {"procedure": "compute", "name": "SharedCompute"}))
    # The other session's bridge serves the same program: the old name is gone there too.
    renamed = b.tool("rea_analyze_function", {"procedure": "SharedCompute"})
    ok(renamed)
    for s in (a, b):
        ok(s.tool("rea_close_binary", {}))
