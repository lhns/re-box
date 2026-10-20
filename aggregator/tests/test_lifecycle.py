"""The on-demand Ghidra lifecycle (Headless) against a fake `java` (fakes/fake_headless.py)."""

import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import anyio
import httpx2
import pytest

from conftest import wait_until
from rebox_aggregator import headless as headless_mod
from rebox_aggregator.headless import Headless
from rebox_aggregator.supervisor import validate

pytestmark = pytest.mark.anyio
FAKES = Path(__file__).parent / "fakes"


@contextlib.asynccontextmanager
async def headless(tmp_path, monkeypatch, java_body=None, backoff=1.0, **env):
    monkeypatch.setattr(Headless, "BACKOFF_START", backoff)  # restart delay after a crash, kept short
    monkeypatch.setattr(Headless, "BACKOFF_MAX", backoff * 2)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    java = bindir / "java"
    java_body = java_body or f'exec {sys.executable} {FAKES / "fake_headless.py"} "$@"'
    java.write_text(f"#!/bin/sh\n{java_body}\n")
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
        await anyio.sleep(0.5)
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


async def test_on_ready_fires_again_when_a_failed_poll_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(headless_mod, "POLL_INTERVAL", 0.2)
    calls = []
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="300") as h:
        h.on_ready = lambda: calls.append(h.state)
        h.want(True)
        await ready(h)
        up = h._check
        down = True

        async def flaky():
            return False if down else await up()

        h._check = flaky  # type: ignore[method-assign]
        assert await wait_until(lambda: h.state == "failed", 5)
        down = False
        assert await wait_until(lambda: h.state == "ready", 5)
        assert calls == ["ready", "ready"]  # and not once more per poll
        await anyio.sleep(0.5)
        assert len(calls) == 2


async def test_start_timeout_reason_survives_the_exit_of_the_killed_jvm(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch, java_body="exec sleep 300", backoff=10, REBOX_HEADLESS_START_TIMEOUT="1") as h:
        h.want(True)
        assert await wait_until(lambda: h.state == "failed", 10)
        assert await wait_until(lambda: h._proc.returncode is not None, 10)
        await anyio.sleep(0.3)  # run() has seen the exit by now
        assert h.health() == (False, "headless server did not start in time", "failed")


async def test_idle_stop_saves_then_exits_and_does_not_restart(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="1") as h:
        h.want(True)
        await ready(h)
        h.want(False)
        assert await wait_until(lambda: h.state == "stopped", 20)
        text = h.marker()
        assert text.index("/save_all_programs") < text.index("/exit_ghidra")
        await anyio.sleep(0.5)
        assert h.starts() == 1 and h.state == "stopped"


async def test_calls_to_the_server_carry_the_auth_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GHIDRA_MCP_AUTH_TOKEN", "s3cret")  # the JVM inherits it, the fake enforces it
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="1") as h:
        h.want(True)
        await ready(h)  # needs the authorized GET /get_project_info
        async with httpx2.AsyncClient() as http:
            assert (await http.post(f"{h.base}/save_all_programs")).status_code == 401
            assert (await http.get(f"{h.base}/check_connection")).status_code == 200  # exempt
        h.want(False)
        assert await wait_until(lambda: "/exit_ghidra" in h.marker(), 20)
        lines = {line.split()[0]: line for line in h.marker().splitlines() if line.startswith("/")}
        assert "auth_ok=True" in lines["/exit_ghidra"]
        assert "auth_ok=False" in h.marker()  # only the unauthorized call above
        assert h.marker().count("auth_ok=False") == 1 and "s3cret" not in h.marker()


async def test_session_returning_before_the_idle_timeout_keeps_the_server(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="1") as h:
        h.want(True)
        await ready(h)
        h.want(False)
        await anyio.sleep(0.5)
        h.want(True)
        await anyio.sleep(1.5)  # past the original deadline
        assert h.starts() == 1 and h.state == "ready" and "/exit_ghidra" not in h.marker()


async def test_idle_timeout_zero_never_stops(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="0") as h:
        h.want(True)
        await ready(h)
        h.want(False)
        await anyio.sleep(1.2)
        assert h.state == "ready" and "/exit_ghidra" not in h.marker()


async def test_session_opening_while_stopping_gives_exactly_two_starts(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT_DELAY", "1")  # the old JVM takes its time to exit
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
        await anyio.sleep(0.5)
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
    async with headless(tmp_path, monkeypatch, backoff=30, REBOX_GHIDRA_IDLE_TIMEOUT="300") as h:
        h.want(True)
        await ready(h)
        os.killpg(h._proc.pid, signal.SIGKILL)
        assert await wait_until(lambda: h.state == "failed", 10)
        h.want(False)
        assert await wait_until(lambda: h.state == "stopped", 3)  # not after the 30 s backoff
        assert h.starts() == 1


async def test_rea_attach_script_installed_at_each_start(tmp_path, monkeypatch):
    src = tmp_path / "attach"
    src.mkdir()
    (src / "ReboxReaAttach.java").write_text("v1")
    installed = tmp_path / "re-box-state" / "home" / "ghidra_scripts" / "ReboxReaAttach.java"
    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="300", REBOX_REA_ATTACH_DIR=str(src)) as h:
        h.want(True)
        await ready(h)
        assert installed.read_text() == "v1"
        (src / "ReboxReaAttach.java").write_text("v2")  # an image upgrade
        os.killpg(h._proc.pid, signal.SIGKILL)
        assert await wait_until(lambda: h.starts() == 2 and h.ready(), 30)
        assert installed.read_text() == "v2"


async def test_jvm_exit_kills_this_instances_rea_attach_helpers(tmp_path, monkeypatch):
    def fake_helper(instance):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", "/opt/rebox/rea-attach/helper.mjs"],
            env={**os.environ, "REBOX_INSTANCE": instance},
        )

    async with headless(tmp_path, monkeypatch, REBOX_GHIDRA_IDLE_TIMEOUT="300") as h:
        h.want(True)
        await ready(h)
        mine, other = fake_helper(h.instance), fake_helper("someone-else")
        try:
            os.killpg(h._proc.pid, signal.SIGKILL)
            assert await wait_until(lambda: mine.poll() is not None, 10)
            assert other.poll() is None
        finally:
            for p in (mine, other):
                p.kill()
                p.wait()


async def test_starting_past_the_start_timeout_is_unhealthy(tmp_path, monkeypatch):
    async with headless(tmp_path, monkeypatch, REBOX_HEADLESS_START_TIMEOUT="5") as h:
        h._set("starting")
        assert h.health() == (True, "", "starting")
        h._starting_since = time.monotonic() - 6
        ok, reason, state = h.health()
        assert (ok, state) == (False, "starting") and reason
