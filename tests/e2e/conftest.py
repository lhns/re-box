"""Shared setup for the e2e suite. tests/run-tests.sh starts the container's services and sets:

REBOX_URL, REBOX_TOKEN, RE_WORKDIR (fixtures copied in), REBOX_STATE_DIR, PUID, PGID,
REBOX_INSTANCE, REBOX_E2E_PHASE (unset for the first run, `restart` for the run after a restart).
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


@pytest.fixture
def session(request):
    """An initialized MCP session, deleted afterwards."""
    opened: list[Session] = []

    def make(listen: bool = False) -> Session:
        s = Session(URL, TOKEN)
        s.initialize()
        if listen:
            s.listen()
        opened.append(s)
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
