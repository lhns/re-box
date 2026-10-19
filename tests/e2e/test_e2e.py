"""End-to-end suite (spec 8.3) against the running container services, over streamable HTTP
with the Bearer token, exactly as a harness connects. Numbers in the test names follow the spec.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import httpx2 as httpx
import pytest

from conftest import (COMPUTE_MARKERS, COMPUTE_MULTIPLIER, INSTANCE, PROJECTS, STATE, TOKEN, URL, WORKDIR,
                      fixture_path, keeper, record, release_keeper)
from mcpclient import McpError, Session

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def session_processes(tag: str) -> dict[int, int]:
    """pid -> pgid of every process carrying this session's tag (and this instance's id)."""
    found = {}
    want = {f"REBOX_SESSION={tag}".encode(), f"REBOX_INSTANCE={INSTANCE}".encode()}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            env = set((proc / "environ").read_bytes().split(b"\0"))
            if want <= env:
                stat = (proc / "stat").read_text()
                found[int(proc.name)] = int(stat.rsplit(")", 1)[1].split()[2])
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


# --- 1. catalog
def test_01_catalog():
    """First session of the run: Ghidra is cold. The full ghidra_ catalog must be in the very first
    tools/list (no waiting for tools/list_changed, which clients may ignore), and a ghidra_ call made
    right away must wait for Ghidra and succeed without the client listing again. This session stays
    open for the rest of the run (conftest.keeper) so Ghidra stays up."""
    s = keeper()
    names = s.tool_names()
    gh = [n for n in names if n.startswith("ghidra_")]
    assert len(gh) > 150, f"only {len(gh)} ghidra_ tools in the first tools/list (GHIDRA_MCP_LAZY=0)"
    first_call = s.tool("ghidra_list_open_programs", {})
    record("01_catalog", "first ghidra_ call after initialize (waits for the cold Ghidra start)", first_call.seconds)
    assert not first_call.result.get("isError"), first_call.text()[:500]
    for required in ("rea_open_binary", "rea_binary_overview", "ghidra_get_metadata", "ghidra_get_functions",
                     "ghidra_search_tools", "ghidra_import_file", "ghidra_find_functions", "ghidra_load_tool_group"):
        assert required in names, required
    assert all(n.startswith(("rea_", "ghidra_")) for n in names), [n for n in names if not n.startswith(("rea_", "ghidra_"))]
    assert all(NAME_RE.match(n) for n in names)
    assert len(names) == len(set(names))
    prompts = s.request("prompts/list", {}).result["prompts"]
    assert prompts and all(p["name"].startswith("rea_") for p in prompts)
    caps = s.init_result["capabilities"]
    assert caps["tools"]["listChanged"] is True
    instructions = s.init_result["instructions"]
    assert s.session_id and tag_of(s.session_id) in instructions and str(STATE) in instructions
    assert not {"ghidra_run_ghidra_script", "ghidra_run_script_inline"} & set(names)  # denied in aggregator.toml


def test_01b_official_sdk_client_interop():
    """The same catalog through the official MCP Python SDK client."""
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def run():
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"},
                                     timeout=httpx.Timeout(300, connect=10)) as http:
            # The SDK caps one SSE event at 1 MiB by default; REA's full-profile tools/list is ~3 MB
            # in one event (docs/CLIENTS.md).
            async with streamable_http_client(URL, http_client=http, max_sse_event_size=None) as (read, write):
                async with ClientSession(read, write) as cs:
                    await cs.initialize()
                    tools = (await cs.list_tools()).tools
                    result = await cs.call_tool("ghidra_list_open_programs", {})
                    return [t.name for t in tools], result

    names, result = anyio.run(run)
    assert "rea_open_binary" in names and "ghidra_get_metadata" in names
    assert not result.is_error


# --- 2. REA flow
def test_02_rea_flow(session):
    s = session()
    opened = s.tool("rea_open_binary", {"path": fixture_path("hello_debug"), "provider_id": "ghidra"})
    assert not opened.result.get("isError"), opened.text()
    overview = s.tool("rea_binary_overview", {})
    record("02_rea_flow", "rea_binary_overview (cold Ghidra start)", overview.seconds)
    assert not overview.result.get("isError"), overview.text()[:2000]
    analysis = s.tool("rea_analyze_function", {"procedure": "compute"})
    record("02_rea_flow", "rea_analyze_function compute", analysis.seconds)
    text = analysis.text()
    assert any(m in text for m in COMPUTE_MARKERS) and any(m in text for m in COMPUTE_MULTIPLIER), text[:2000]


# --- 3. ghidra-mcp flow, with persistence in GHIDRA_PROJECTS_DIR
def _json(call):
    return json.loads(call.text())


def test_03_ghidra_mcp_flow(session):
    s = session()
    imported = s.tool("ghidra_import_file", {"file_path": fixture_path("hello_stripped")})
    record("03_ghidra_mcp_flow", "ghidra_import_file + auto-analysis", imported.seconds)
    info = _json(imported)
    assert info.get("success"), info
    program = info["name"]

    functions = _json(s.tool("ghidra_find_functions", {"program": program, "name_pattern": "FUN_", "limit": 200}))["functions"]
    assert functions
    compute = None
    for fn in functions:
        code = _json(s.tool("ghidra_get_functions", {"program": program, "function": fn["address"],
                                                      "fields": "decompiled_code"})).get("decompiled_code", "")
        if any(m in code for m in COMPUTE_MARKERS):
            compute = fn
            break
    assert compute, "no stripped function decompiles to compute()"

    renamed = _json(s.tool("ghidra_rename_function", {"program": program, "old_name": compute["address"],
                                                       "new_name": "ComputeFixtureValue", "strict_mode": "off"}))
    assert renamed.get("status") == "success" or renamed.get("success"), renamed
    assert _json(s.tool("ghidra_save_program", {"program": program})).get("success")
    assert _json(s.tool("ghidra_close_program", {"name": program, "save": True})).get("success", True)

    # Reopen from the project database on disk.
    reopened = _json(s.tool("ghidra_open_program", {"path": info["path"]}))
    assert reopened.get("success"), reopened
    found = _json(s.tool("ghidra_find_functions", {"program": program, "name_pattern": "ComputeFixtureValue"}))
    assert [f["address"] for f in found["functions"]] == [compute["address"]]
    assert (PROJECTS / "rebox.gpr").is_file() and (PROJECTS / "rebox.rep").is_dir()


def test_03b_delete_program(session):
    """ghidra_delete_file removes a program from the project (open or not), not the source file; a
    re-import then works. A copy keeps hello_stripped, which later tests and the restart phase use."""
    src = Path(fixture_path("hello_stripped"))
    copy = WORKDIR / "hello_delete"
    shutil.copyfile(src, copy)
    s = session()
    first = _json(s.tool("ghidra_import_file", {"file_path": str(copy)}))
    assert first.get("success"), first
    name = first["name"]
    assert name in s.tool("ghidra_list_open_programs", {}).text()
    assert name in s.tool("ghidra_list_project_files", {"folder": "/"}).text()

    deleted = _json(s.tool("ghidra_delete_file", {"filePath": first["path"]}))
    assert deleted.get("success") and deleted.get("deleted") is True, deleted
    assert copy.is_file(), "the source file must stay"
    assert name not in s.tool("ghidra_list_open_programs", {}).text()
    assert name not in s.tool("ghidra_list_project_files", {"folder": "/"}).text()
    assert _json(s.tool("ghidra_delete_file", {"filePath": first["path"]})).get("deleted") is False

    again = _json(s.tool("ghidra_import_file", {"file_path": str(copy)}))
    assert again.get("success"), again
    assert _json(s.tool("ghidra_delete_file", {"filePath": again["path"]})).get("deleted") is True


def test_03c_delete_project_is_hidden(session):
    s = session()
    assert "ghidra_delete_project" not in s.tool_names()


# --- 4. lazy tool groups and list_changed. Lazy mode is opt-in now (the image sets GHIDRA_MCP_LAZY=0:
# clients cannot be relied on to follow list_changed); run with GHIDRA_MCP_LAZY=1 to exercise it.
@pytest.mark.skipif(os.environ.get("GHIDRA_MCP_LAZY") != "1", reason="lazy tool groups are off by default")
def test_04_lazy_groups(session):
    s = session(listen=True)
    before = set(s.tool_names())
    assert "ghidra_get_comment" not in before, "comment group already loaded; lazy mode off?"
    load = s.tool("ghidra_load_tool_group", {"group": "comment"})
    changed = [m for m in load.notifications if m.get("method") == "notifications/tools/list_changed"]
    deadline = time.monotonic() + 10
    while not changed and time.monotonic() < deadline:
        try:
            m = s.background.get(timeout=0.5)
            if m.get("method") == "notifications/tools/list_changed":
                changed.append(m)
        except Exception:
            pass
    assert changed, "no notifications/tools/list_changed after ghidra_load_tool_group"
    after = set(s.tool_names())
    assert {"ghidra_get_comment", "ghidra_set_comment"} <= after - before
    # Routed to the bridge (a tool-level error about the missing program is fine; unknown tool is not).
    call = s.request("tools/call", {"name": "ghidra_get_comment", "arguments": {"program": "nonexistent", "address": "0x0"}})
    assert call.error is None, call.error


# --- 5. session isolation
def test_05_session_isolation(session):
    a, b = session(), session()
    targets = {a: fixture_path("hello_debug"), b: fixture_path("other_debug")}
    errors = []

    def open_target(s):
        try:
            s.tool("rea_open_binary", {"path": targets[s], "provider_id": "ghidra"})
        except Exception as e:  # surfaced below
            errors.append(e)

    threads = [threading.Thread(target=open_target, args=(s,)) for s in (a, b)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    assert not errors, errors
    for s, other in ((a, b), (b, a)):
        state = s.tool("rea_binary_session", {}).text()
        assert json.dumps(targets[s]) in state
        assert json.dumps(targets[other]) not in state


# --- 6. progress
def test_06_progress(session):
    s = session()
    s.tool("rea_open_binary", {"path": fixture_path("hello.exe"), "provider_id": "ghidra"})
    overview = s.tool("rea_binary_overview", {}, progress_token="e2e-progress")
    record("06_progress", "rea_binary_overview on hello.exe (cold)", overview.seconds)
    progress = [m for m in overview.notifications if m.get("method") == "notifications/progress"]
    assert progress, "no progress notifications on a cold REA Ghidra call"
    assert all(m["params"]["progressToken"] == "e2e-progress" for m in progress)


# --- 7. cancellation
def test_07_cancellation(session):
    s = session()
    s.tool("rea_open_binary", {"path": fixture_path("hello_debug"), "provider_id": "ghidra"})
    rid = 9001
    started = threading.Event()
    outcome = {}

    def long_call():
        try:
            outcome["call"] = s.request("tools/call", {"name": "rea_binary_overview", "arguments": {},
                                                       "_meta": {"progressToken": "e2e-cancel"}},
                                        request_id=rid, on_message=lambda m: started.set())
        except Exception as e:
            outcome["exc"] = e

    t = threading.Thread(target=long_call, daemon=True)
    t.start()
    assert started.wait(120), "long call never reported progress"
    t0 = time.monotonic()
    assert s.notify("notifications/cancelled", {"requestId": rid, "reason": "e2e"}) == 202
    t.join(60)
    assert not t.is_alive(), "cancelled request's stream did not end"
    record("07_cancellation", "stream end after cancel", time.monotonic() - t0)
    call = outcome.get("call")
    assert call is None or call.result is None, "cancelled request still produced a result"
    # The session survives: both children still answer.
    assert json.dumps(fixture_path("hello_debug")) in s.tool("rea_binary_session", {}).text()
    assert not s.tool("ghidra_list_open_programs", {}).result.get("isError")


# --- 8. auth
def test_08_auth():
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}}
    headers = {"Accept": "application/json, text/event-stream"}
    with httpx.Client(timeout=30) as http:
        assert http.post(URL, json=init, headers=headers).status_code == 401
        assert http.post(URL, json=init, headers={**headers, "Authorization": "Bearer wrong"}).status_code == 401
        assert http.post(URL, json=init, headers={**headers, "Authorization": f"Bearer {TOKEN}x"}).status_code == 401
        health = http.get(URL.rsplit("/mcp", 1)[0] + "/healthz")
        assert health.status_code == 200


# --- 9. ownership of everything the services wrote to the state dir
def test_09_ownership():
    """Runs in the PUID/PGID remap mode (container started as root). The uid-1000 mode is
    covered by tests/smoke/image.sh: there no remap happens and files belong to the runtime uid."""
    puid, pgid = int(os.environ["PUID"]), int(os.environ["PGID"])
    files = [p for p in STATE.rglob("*")]
    assert any(p.is_relative_to(PROJECTS) for p in files), "test_03 should have written the project"
    wrong = [(str(p), p.lstat().st_uid, p.lstat().st_gid) for p in files if (p.lstat().st_uid, p.lstat().st_gid) != (puid, pgid)]
    assert not wrong, wrong[:5]


# --- 10. session cleanup
def test_10_session_cleanup():
    s = Session(URL, TOKEN)
    s.initialize()
    tag = tag_of(s.session_id)
    # Start REA's Ghidra provider too (the attach helper), so the check covers grandchildren.
    s.tool("rea_open_binary", {"path": fixture_path("hello_debug"), "provider_id": "ghidra"})
    s.tool("rea_binary_overview", {})
    procs = session_processes(tag)
    assert len(procs) >= 3, f"expected rea, bridge and REA's attach helper, got {procs}"
    pgids = set(procs.values())
    assert s.delete() in (200, 204)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and (session_processes(tag) or processes_in_groups(pgids)):
        time.sleep(0.5)
    record("10_session_cleanup", "processes gone after DELETE", 30 - (deadline - time.monotonic()))
    assert not session_processes(tag)
    assert not processes_in_groups(pgids)
    s.session_id = None
    s.close()


# --- 11. Ghidra stops when the last session is gone (REBOX_GHIDRA_IDLE_TIMEOUT=5) and comes back
def ghidra_server_pids() -> list[int]:
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if b"GhidraMCPHeadlessServer" in (proc / "cmdline").read_bytes() and \
                    f"REBOX_INSTANCE={INSTANCE}".encode() in (proc / "environ").read_bytes().split(b"\0"):
                found.append(int(proc.name))
        except OSError:
            continue
    return found


def test_11_idle_stop_and_restart():
    assert ghidra_server_pids(), "the keeper session should have kept Ghidra running"
    release_keeper()  # every other session of the run is closed already
    t0 = time.monotonic()
    deadline = t0 + 90
    while ghidra_server_pids() and time.monotonic() < deadline:
        time.sleep(0.5)
    record("11_idle_stop", "Ghidra gone after the last session closed (idle timeout 5 s)", time.monotonic() - t0)
    assert not ghidra_server_pids()
    # The JVM leaves /proc a moment before the supervisor has reaped it and says "stopped"
    # (measured 0.3 s on an idle machine).
    with httpx.Client(timeout=30) as http:
        for _ in range(40):
            health = http.get(URL.rsplit("/mcp", 1)[0] + "/healthz")
            if health.status_code != 200 or health.json()["ghidra"] != "stopping":
                break
            time.sleep(0.25)
    assert health.status_code == 200 and health.json()["ghidra"] == "stopped", health.text

    # A cold session: the catalog comes from the cache at once, and a ghidra_ call waits for Ghidra.
    s = Session(URL, TOKEN)
    try:
        s.initialize()
        assert "ghidra_get_metadata" in s.tool_names()
        opened = s.tool("ghidra_open_program", {"path": "/hello_stripped"})  # imported and saved by test_03
        record("11_idle_stop", "first ghidra_ call of a cold session (restart included)", opened.seconds)
        assert _json(opened).get("success"), opened.text()[:500]
        assert ghidra_server_pids()
    finally:
        s.close()
