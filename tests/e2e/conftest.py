"""Shared setup for the e2e suite. tests/run-tests.sh starts the container's services and sets:

REBOX_URL, REBOX_TOKEN, RE_WORKDIR (fixtures copied in), REBOX_STATE_DIR, PUID, PGID,
REBOX_INSTANCE, REBOX_REA_TMP (REA's TMPDIR), REBOX_E2E_PHASE (unset for the first run, `restart`
for the run after a restart).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcpclient import Session

# Values compiled into tests/fixtures/src/hello.c.
COMPUTE_MARKERS = ("0x2a2a", "10794")
COMPUTE_MULTIPLIER = ("0x539", "1337")

URL = os.environ["REBOX_URL"]
TOKEN = os.environ["REBOX_TOKEN"]
WORKDIR = Path(os.environ["RE_WORKDIR"])
STATE = Path(os.environ.get("REBOX_STATE_DIR") or WORKDIR / "re-box-state")
PROJECTS = Path(os.environ.get("GHIDRA_PROJECTS_DIR") or STATE / "ghidra-projects")
PHASE = os.environ.get("REBOX_E2E_PHASE", "first")
INSTANCE = os.environ["REBOX_INSTANCE"]

_durations: list[tuple[str, str, float]] = []


def record(test: str, what: str, seconds: float) -> None:
    _durations.append((test, what, seconds))


def assert_gh_listed(s: Session) -> None:
    """The ghidra_ tools are in the first tools/list even when Ghidra is cold (catalog cache or held initialize)."""
    assert "ghidra_get_metadata" in s.tool_names(), "no ghidra_ tools in the first tools/list"


_keeper: Session | None = None


def keeper() -> Session:
    """One session held open (with a GET stream, so it is not an orphan) for the whole run, so Ghidra
    stays up between tests despite REBOX_GHIDRA_IDLE_TIMEOUT=5. test_stop_when_idle.py (the last file) releases it."""
    global _keeper
    if _keeper is None:
        _keeper = Session(URL, TOKEN, timeout=None)
        _keeper.initialize()
        _keeper.listen()
    return _keeper


def release_keeper() -> None:
    global _keeper
    if _keeper is not None:
        _keeper.close()
        _keeper = None


@pytest.fixture
def session(request):
    """An initialized MCP session with the ghidra_ tools available, deleted afterwards."""
    opened: list[Session] = []

    def make(listen: bool = False) -> Session:
        s = Session(URL, TOKEN)
        s.initialize()
        if listen:
            s.listen()
        opened.append(s)
        assert_gh_listed(s)
        return s

    yield make
    for s in opened:
        s.close()


def fixture_path(name: str) -> str:
    return str(WORKDIR / name)


def pytest_terminal_summary(terminalreporter):
    if not _durations:
        return
    terminalreporter.section("measured durations")
    for test, what, seconds in _durations:
        terminalreporter.write_line(f"{seconds:8.1f} s  {test}: {what}")
