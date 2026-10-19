"""REA's Ghidra sessions attach to the shared ghidra-mcp JVM (deps/rea-attach, DECISIONS.md).

Each test works on its own copy of a fixture: REA's edits now land in the shared project, where
other tests would see them.
"""
from __future__ import annotations

import os
import signal
import threading
import time
import urllib.request

from conftest import record
from mcpclient import McpError
from reahelp import (copy_fixture, ghidra_mcp_jvm, helpers, jtext, ok, rea_jvms, rea_program_path,
                     sha256_of, wait_for)


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


# The raw-binary case (formerly a fallback to REA's own JVM) is test_rea_profiles.py's.


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
    # Both opened the same new file at once: one program, not a "shared_debug.0" copy as well.
    programs = jtext(ok(a.tool("ghidra_list_open_programs", {})))["programs"]
    paths = [p["path"] for p in programs if p["name"].startswith("shared_debug")]
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
    ok(s.tool("rea_close_binary", {}))
