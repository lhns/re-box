"""REA's special Ghidra target profiles run in the shared ghidra-mcp JVM (deps/rea-attach, DECISIONS.md).

Raw binaries, DOS COM/MZ, language overrides, seeds, disabled analyzers and MIPS soft-float used to
need REA's own JVM. Each test opens one such target, checks that the profile took effect, then
checks the shared-JVM behaviour:

  AFTER C's CHANGE (marked below; they fail on an image where these profiles still run in REA's own
  JVM): the program is in the shared project under /rea/<sha256[:12]>-<profile digest[:8]>/<name>,
  and no process of this instance is a Ghidra AnalyzeHeadless JVM.

REA reads REA_GHIDRA_LANGUAGE_ID, _COMPILER_SPEC_ID, _DISABLED_ANALYZERS and _SEED_FILE at process
start, so each test runs its own `rea mcp` (stdio, tool names without the aggregator's rea_ prefix)
while an aggregator session keeps the Ghidra JVM up and lists the shared project.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from conftest import STATE
from reahelp import copy_fixture, helpers, ok, rea_jvms, rea_program_path, sha256_of
from stdiomcp import start_rea


@contextmanager
def rea_process(env: dict[str, str] | None = None):
    s = start_rea(STATE, env or {})
    try:
        yield s
    finally:
        try:
            s.tool("close_binary", {})
        except Exception:
            pass
        s.close()


def evidence(call) -> dict:
    return json.loads(ok(call).text())


def find_values(obj, key: str) -> list:
    """Every value stored under `key`, anywhere in a nested JSON value."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(v)
            found += find_values(v, key)
    elif isinstance(obj, list):
        for v in obj:
            found += find_values(v, key)
    return found


def procedures(s) -> dict[str, str]:
    """{address: name} from list_procedures."""
    rows = evidence(s.tool("list_procedures", {}))["raw_result"]
    return {row["address"]: row["value"] for row in rows}


def assert_in_shared_jvm(gh, target: str) -> None:
    """AFTER C's CHANGE: REA's program is in the shared project and REA has no JVM of its own."""
    path = rea_program_path(gh, target)
    assert path, f"no /rea/{sha256_of(target)[:12]}-*/ program for {target} in the shared project"
    assert not rea_jvms(), "REA started its own Ghidra JVM (AnalyzeHeadless)"
    assert helpers(), "no attach helper is running for the open binary"


def test_raw_binary(session):
    gh = session()  # keeps the Ghidra JVM up and lists the project
    target = copy_fixture("blob_x86_64.bin", "profiles/raw/blob.bin")
    with rea_process() as s:
        # blob_x86_64.bin: freestanding.c linked at 0 and cut with objcopy -O binary; _start at offset 0.
        opened = json.loads(ok(s.tool("open_binary", {
            "path": target, "provider_id": "ghidra", "format": "raw-binary",
            "architecture": "x86_64", "entry_point": "0x0"})).text())["result"]
        assert opened["format"] == "raw-binary" and opened["entry_point"] == "0x0", opened
        procs = procedures(s)
        assert "0x0" in procs, procs  # the entry point is a function: file offset 0 maps at the entry
        overview = evidence(s.tool("binary_overview", {}))
        assert set(find_values(overview, "language_id")) == {"x86:LE:64:default"}
        assert_in_shared_jvm(gh, target)


def test_dos_com(session):
    gh = session()
    target = copy_fixture("hello.com", "profiles/dos/hello.com")
    with rea_process() as s:
        opened = json.loads(ok(s.tool("open_binary", {
            "path": target, "provider_id": "ghidra", "format": "dos-com"})).text())["result"]
        assert opened["format"] == "dos-com", opened
        # hello.com: call 0x10b at 0x100 (CS:IP 0000:0100, Ghidra load segment 0x1000).
        procs = procedures(s)
        assert procs.get("0x10100") == "entry" and "0x1010b" in procs, procs
        overview = evidence(s.tool("binary_overview", {}))
        assert set(find_values(overview, "language_id")) == {"x86:LE:16:Real Mode"}
        assert_in_shared_jvm(gh, target)


def test_dos_mz(session):
    gh = session()
    target = copy_fixture("hello_mz.exe", "profiles/dos/hello_mz.exe")
    with rea_process() as s:
        opened = json.loads(ok(s.tool("open_binary", {"path": target, "provider_id": "ghidra"})).text())["result"]
        assert opened["format"] == "dos-mz", opened
        procs = procedures(s)
        assert len(procs) == 2, procs  # entry and the subroutine it calls
        overview = evidence(s.tool("binary_overview", {}))
        assert set(find_values(overview, "language_id")) == {"x86:LE:16:Real Mode"}
        assert_in_shared_jvm(gh, target)


def test_elf_language_override(session):
    gh = session()
    target = copy_fixture("hello_debug", "profiles/lang/hello_lang")
    env = {"REA_GHIDRA_LANGUAGE_ID": "x86:LE:64:default", "REA_GHIDRA_COMPILER_SPEC_ID": "gcc"}
    with rea_process(env) as s:
        ok(s.tool("open_binary", {"path": target, "provider_id": "ghidra"}))
        analysis = evidence(s.tool("analyze_function", {"procedure": "compute"}))
        # Without the override the profile says "auto-from-header" (the other tests' ELFs).
        assert set(find_values(analysis, "language_id")) == {"x86:LE:64:default"}
        assert set(find_values(analysis, "compiler_spec_id")) == {"gcc"}
        assert "0x539" in json.dumps(analysis) or "1337" in json.dumps(analysis)
        assert_in_shared_jvm(gh, target)
        # AFTER C's CHANGE: the imported program carries the pinned language.
        path = rea_program_path(gh, target)
        ok(gh.tool("gh_open_program", {"path": path}))
        open_programs = json.loads(ok(gh.tool("gh_list_open_programs", {})).text())["programs"]
        languages = {p["language"] for p in open_programs if p["path"] == path}
        assert languages == {"x86:LE:64:default"}, open_programs


def test_seeds(session):
    gh = session()
    target = copy_fixture("freestanding_x86_64", "profiles/seeds/stripped_seeded")
    seeds = copy_fixture("seeds.tsv", "profiles/seeds/seeds.tsv")
    # seeds.tsv: `<address of the function nothing references><TAB>function<TAB>rebox_seeded`.
    address = next(line.split("\t")[0] for line in open(seeds) if not line.startswith("#"))
    address = hex(int(address, 16))
    with rea_process({"REA_GHIDRA_SEED_FILE": seeds}) as s:
        ok(s.tool("open_binary", {"path": target, "provider_id": "ghidra"}))
        procs = procedures(s)
        # The ELF is stripped: only the seed gives this function its name.
        assert procs.get(address) == "rebox_seeded", procs
        assert_in_shared_jvm(gh, target)


def test_disabled_analyzers(session):
    gh = session()
    target = copy_fixture("hello_debug", "profiles/disabled/hello_noswitch")
    env = {"REA_GHIDRA_DISABLED_ANALYZERS": "Decompiler Switch Analysis"}
    with rea_process(env) as s:
        ok(s.tool("open_binary", {"path": target, "provider_id": "ghidra"}))
        analysis = evidence(s.tool("analyze_function", {"procedure": "classify"}))
        presets = set(find_values(analysis, "analyzer_preset"))
        assert presets == {"ghidra-default+disabled:Decompiler Switch Analysis"}, presets
        assert find_values(analysis, "disabled_analyzers") == [["Decompiler Switch Analysis"]]
        assert_in_shared_jvm(gh, target)


def test_mips_soft_float(session):
    gh = session()
    target = copy_fixture("mips_softfloat", "profiles/mips/mips_softfloat")
    with rea_process() as s:
        ok(s.tool("open_binary", {"path": target, "provider_id": "ghidra"}))
        procs = procedures(s)
        assert {"_start", "compute", "classify", "negate"} <= set(procs.values()), procs
        overview = evidence(s.tool("binary_overview", {}))
        # REA admitted the o32 soft-float ABI record (.MIPS.abiflags fp_abi 3, cpr1 size 0) and
        # runs its post-analysis soft-float prototype pass.
        assert set(find_values(overview, "mips_soft_float_storage")) == {"o32-soft-float-prototype-v1"}
        assert_in_shared_jvm(gh, target)


@pytest.mark.skip(reason="REA's only Ghidra analysis extension is NativeAOT (REA_GHIDRA_NATIVEAOT_JAR, "
                         "a third-party extension jar for x86-64 NativeAOT ELF/PE targets); neither the jar "
                         "nor such a target is in the image")
def test_analysis_extensions():
    ...
