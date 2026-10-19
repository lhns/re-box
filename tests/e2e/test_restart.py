"""Second phase: the services were stopped with SIGTERM and started again on the same work and
state dirs (tests/run-tests.sh sets REBOX_E2E_PHASE=restart and runs only this file)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import PHASE, STATE
from test_e2e import _json

pytestmark = pytest.mark.skipif(PHASE != "restart", reason="runs after the restart (REBOX_E2E_PHASE=restart)")


def test_ghidra_rename_survived_restart(session):
    s = session()
    opened = _json(s.tool("ghidra_open_program", {"path": "/hello_stripped"}))
    assert opened.get("success"), opened
    found = _json(s.tool("ghidra_find_functions", {"program": "hello_stripped", "name_pattern": "ComputeFixtureValue"}))
    assert len(found["functions"]) == 1, found


def test_leftover_tmp_swept_at_start():
    planted = Path(f"/tmp/rebox-rea-{os.getuid()}/rea-ghidra-planted")  # tests/run-tests.sh
    assert not planted.exists(), "the supervisor did not sweep rea-* from REA's TMPDIR at start"


def test_rea_evidence_autosaved():
    bundles = sorted((STATE / "rea" / "evidence").glob("*.json"))
    assert bundles, "no evidence bundle was written when the phase-1 sessions closed"
    assert all(isinstance(json.loads(b.read_text()), (dict, list)) for b in bundles)


def test_state_layout_and_ownership():
    assert list((STATE / "home" / ".config").glob("ghidramcp*")), "Ghidra's settings are not under the state dir"
    assert any(STATE.joinpath("cache").iterdir()), "no cache under the state dir"
    assert (STATE / "ghidra-projects" / "rebox.gpr").is_file()
    assert not Path("/home/rebox/.config/ghidramcp").exists()
    assert not list(Path("/var/tmp").glob("*ghidramcp*"))
    puid, pgid = int(os.environ["PUID"]), int(os.environ["PGID"])
    wrong = [str(p) for p in STATE.rglob("*") if (p.lstat().st_uid, p.lstat().st_gid) != (puid, pgid)]
    assert not wrong, wrong[:5]
