"""The on-demand Ghidra lifecycle (Headless) against a fake `java` (fakes/fake_headless.py)."""

import contextlib
import os
import signal
import sys
import time
import uuid
from pathlib import Path

import anyio
import pytest

from conftest import wait_until
from rebox_aggregator.supervisor import Headless, validate

pytestmark = pytest.mark.anyio
FAKES = Path(__file__).parent / "fakes"


@contextlib.asynccontextmanager
async def headless(tmp_path, monkeypatch, **env):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    java = bindir / "java"
    java.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKES / "fake_headless.py"} "$@"\n')
    java.chmod(0o755)
    ghidra = tmp_path / "ghidra"
    (ghidra / "support").mkdir(parents=True)
    ah = ghidra / "support" / "analyzeHeadless"
    ah.write_text('#!/bin/sh\ntouch "$1/$2.gpr"\n')
    ah.chmod(0o755)
    marker = tmp_path / "marker"
    marker.write_text("")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_MARKER", str(marker))
    cfg = validate({
        "REBOX_TOKEN": "t", "RE_WORKDIR": str(tmp_path), "GHIDRA_INSTALL_DIR": str(ghidra),
        "REBOX_HEADLESS_START_TIMEOUT": "30", "REBOX_AUTOSAVE_INTERVAL": "0", **env,
    })
    h = Headless(cfg, f"lifecycle-{uuid.uuid4().hex}")
    h.starts = lambda: marker.read_text().count("start project=")  # type: ignore[attr-defined]
    h.marker = lambda: marker.read_text()  # type: ignore[attr-defined]
    async with anyio.create_task_group() as tg:
        tg.start_soon(h.run)
        try:
            yield h
        finally:
            await h.stop(anyio.Event())
            tg.cancel_scope.cancel()


async def ready(h):
    assert await wait_until(h.ready, 20)


async def test_nothing_starts_until_a_session_wants_it(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch) as h:
        await anyio.sleep(1.5)
        assert h.starts() == 0 and h.state == "stopped" and h.health() == (True, "", "stopped")


async def test_start_on_want_and_on_ready_called(tmp_path, monkeypatch):
    calls = []
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="300") as h:
        h.on_ready = lambda: calls.append(h.state)
        h.want(True)
        assert h.health()[2] in ("stopped", "starting", "ready")
        await ready(h)
        assert (h.starts(), h.state, calls) == (1, "ready", ["ready"])
        assert h.health() == (True, "", "ready")


async def test_idle_stop_saves_then_exits_and_does_not_restart(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="1") as h:
        h.want(True)
        await ready(h)
        h.want(False)
        assert await wait_until(lambda: h.state == "stopped", 20)
        text = h.marker()
        assert text.index("/save_all_programs") < text.index("/exit_ghidra")
        await anyio.sleep(1.5)
        assert h.starts() == 1 and h.state == "stopped"


async def test_session_returning_before_the_idle_timeout_keeps_the_server(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="2") as h:
        h.want(True)
        await ready(h)
        h.want(False)
        await anyio.sleep(1)
        h.want(True)
        await anyio.sleep(3)
        assert h.starts() == 1 and h.state == "ready" and "/exit_ghidra" not in h.marker()


async def test_idle_timeout_zero_never_stops(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="0") as h:
        h.want(True)
        await ready(h)
        h.want(False)
        await anyio.sleep(2)
        assert h.state == "ready" and "/exit_ghidra" not in h.marker()


async def test_session_opening_while_stopping_gives_exactly_two_starts(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT_DELAY", "2")  # the old JVM takes its time to exit
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="1") as h:
        h.want(True)
        await ready(h)
        h.want(False)
        assert await wait_until(lambda: h.state == "stopping", 10)
        assert h.health() == (True, "", "stopping")
        h.want(True)
        assert await wait_until(lambda: h.starts() == 2 and h.ready(), 30)
        await anyio.sleep(1)
        assert h.starts() == 2


async def test_crash_without_sessions_does_not_restart(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="300") as h:
        h.want(True)
        await ready(h)
        h.want(False)
        os.killpg(h._proc.pid, signal.SIGKILL)
        assert await wait_until(lambda: h.state == "stopped", 10)
        await anyio.sleep(1.5)
        assert h.starts() == 1


async def test_crash_with_sessions_restarts_after_backoff(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="300") as h:
        h.want(True)
        await ready(h)
        os.killpg(h._proc.pid, signal.SIGKILL)
        assert await wait_until(lambda: h.state == "failed", 10)
        assert h.health() == (False, "headless server down", "failed")
        assert await wait_until(lambda: h.starts() == 2 and h.ready(), 30)


async def test_last_session_leaving_cancels_the_restart_backoff(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="300") as h:
        h.want(True)
        await ready(h)
        os.killpg(h._proc.pid, signal.SIGKILL)
        assert await wait_until(lambda: h.state == "failed", 10)
        h.want(False)
        assert await wait_until(lambda: h.state == "stopped", 10)
        await anyio.sleep(1)
        assert h.starts() == 1


async def test_starting_past_the_start_timeout_is_unhealthy(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch, REBOX_HEADLESS_START_TIMEOUT="5") as h:
        h._set("starting")
        assert h.health() == (True, "", "starting")
        h._starting_since = time.monotonic() - 6
        ok, reason, state = h.health()
        assert (ok, state) == (False, "starting") and reason
