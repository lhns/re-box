"""Shared setup for the e2e suite. tests/run-tests.sh starts the container's services and sets:

REBOX_URL, REBOX_TOKEN, RE_WORKDIR (fixtures copied in), REBOX_STATE_DIR, PUID, PGID,
REBOX_INSTANCE, REBOX_E2E_PHASE (unset for the first run, `restart` for the run after a restart).
"""
from __future__ import annotations

import os
import time
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


def wait_for_gh(s: Session, timeout: float = 300.0) -> tuple[bool, float]:
    """Poll tools/list until the gh_ tools are there (Ghidra starts with the first session and the
    bridge announces itself with tools/list_changed). Returns (a list_changed arrived on the session's
    GET stream, seconds waited)."""
    start = time.monotonic()
    changed = False
    while True:
        while not s.background.empty():
            changed |= s.background.get_nowait().get("method") == "notifications/tools/list_changed"
        if "gh_get_metadata" in s.tool_names():
            return changed, time.monotonic() - start
        if time.monotonic() - start > timeout:
            raise AssertionError(f"no gh_ tools {timeout:.0f} s after initialize")
        time.sleep(1)


_keeper: Session | None = None


def keeper() -> Session:
    """One session held open (with a GET stream, so it is not an orphan) for the whole run, so Ghidra
    stays up between tests despite REBOX_GHIDRA_IDLE_TIMEOUT=5. test_11 releases it."""
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
    """An initialized MCP session with the gh_ tools available, deleted afterwards."""
    opened: list[Session] = []

    def make(listen: bool = False) -> Session:
        s = Session(URL, TOKEN)
        s.initialize()
        if listen:
            s.listen()
        opened.append(s)
        changed, seconds = wait_for_gh(s)
        record(request.node.name, "gh_ tools available after initialize", seconds)
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
