"""REA's Ghidra sessions attach to the shared ghidra-mcp JVM (deps/rea-attach, DECISIONS.md).

Each test works on its own copy of a fixture: REA's edits now land in the shared project, where
other tests would see them.
"""
from __future__ import annotations

import os
import signal
import threading
import time

from conftest import record
from helpers import (copy_fixture, ghidra_mcp_jvm, ghidra_post, helpers, jtext, ok, rea_jvms, rea_program_path,
                     sha256_of, wait_for)
from mcpclient import McpError


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
    found = jtext(ok(s.tool("ghidra_find_functions", {"program": "one_debug", "name_pattern": "AttachCompute"})))
    assert [f["name"] for f in found["functions"]] == ["AttachCompute"]
    comments = jtext(ok(s.tool("ghidra_get_functions", {"program": "one_debug", "function": "AttachCompute",
                                                         "fields": "comments"})))["comments"]
    assert any(c["text"] == "set by REA" for c in comments)

    ok(s.tool("rea_close_binary", {}))
    wait_for(lambda: not helpers(), 15, "the attach helper to exit")
    assert len(ghidra_mcp_jvm()) == 1
    found = jtext(ok(s.tool("ghidra_find_functions", {"program": "one_debug", "name_pattern": "AttachCompute"})))
    assert found["functions"], "the REA rename is gone after close"


def test_same_name_other_bytes_gets_its_own_folder(session):
    first = copy_fixture("hello_debug", "attach/a/twin")
    second = copy_fixture("other_debug", "attach/b/twin")
    assert sha256_of(first) != sha256_of(second)
    s = session()
    for target in (first, second):
        ok(s.tool("rea_open_binary", {"path": target, "provider_id": "ghidra"}))
        ok(s.tool("rea_analyze_function", {"procedure": "compute"}))
    # REA's programs live under /rea/<sha256[:12]>-<profile digest[:8]>/ and never reuse an
    # agent's import at /, so two "twin" files with other bytes do not collide.
    for target in (first, second):
        path = rea_program_path(s, target)
        assert path, f"no /rea/{sha256_of(target)[:12]}-*/twin in the shared project"
        opened = jtext(ok(s.tool("ghidra_open_program", {"path": path})))
        assert opened.get("success", True), opened
    ok(s.tool("rea_close_binary", {}))


def test_jvm_restart_while_rea_has_a_binary_open(session):
    target = copy_fixture("hello_debug", "attach/restart_debug")
    s = session()
    ok(s.tool("rea_open_binary", {"path": target, "provider_id": "ghidra"}))
    ok(s.tool("rea_analyze_function", {"procedure": "compute"}))
    # Keep the other tests' project edits: the kill skips the shutdown save.
    ghidra_post("/save_all_programs")
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
    # Both opened the same new file at once: one program, not a "shared_debug.0" copy as well.
    programs = jtext(ok(a.tool("ghidra_list_open_programs", {})))["programs"]
    # By project path: inside Ghidra the program keeps the snapshot's name (target-<sha>.bin).
    paths = [p["path"] for p in programs if "/shared_debug" in p["path"]]
    assert paths == [rea_program_path(a, target)], programs
    ok(a.tool("rea_annotate_native_function", {"procedure": "compute", "name": "SharedCompute"}))
    # One program: b, still open since before, sees a's rename at once.
    ok(b.tool("rea_analyze_function", {"procedure": "SharedCompute"}))
    for s in (a, b):
        ok(s.tool("rea_close_binary", {}))


def test_rea_sees_a_gh_rename_without_reopening(session):
    target = copy_fixture("hello_debug", "attach/live_debug")
    s = session()
    ok(s.tool("rea_open_binary", {"path": target, "provider_id": "ghidra"}))
    ok(s.tool("rea_analyze_function", {"procedure": "compute"}))
    before = ok(s.tool("rea_list_procedures", {})).text()
    assert "compute" in before and "GhRenamedCompute" not in before
    ok(s.tool("ghidra_rename_function", {"program": "live_debug", "old_name": "compute",
                                     "new_name": "GhRenamedCompute", "strict_mode": "off"}))
    assert "GhRenamedCompute" in ok(s.tool("rea_list_procedures", {})).text()
    ok(s.tool("rea_analyze_function", {"procedure": "GhRenamedCompute"}))
    # A REA edit does not wait out a transaction of its own bridge (it would stall 60 s).
    edit = ok(s.tool("rea_annotate_native_function", {"procedure": "GhRenamedCompute", "comment": "timed"}))
    assert edit.seconds < 30, edit.seconds
    ok(s.tool("rea_close_binary", {}))


def test_clients_cannot_run_reas_bridge_as_an_import_script(session):
    """The bridge loads jars its descriptor names: it must not be reachable through an import."""
    target = copy_fixture("hello_debug", "attach/import_script_probe")
    s = session()
    for path in ("/opt/rebox/rea-attach/ghidra_scripts/ReaGhidraBridge.java",
                 "/opt/rebox/rea-attach/ReboxReaAttach.java"):
        call = s.tool("ghidra_import_file", {"file_path": target, "project_folder": "/probe",
                                             "post_scripts": [{"path": path, "args": [target]}]})
        assert call.result.get("isError") or "error" in call.text(), call.text()[:500]
        assert "IMPORT_SCRIPT_ROOTS" in call.text(), call.text()[:500]
