"""Run the real rebox-supervisor against a fake `java` (the real JVM path is covered by the e2e tests)."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest

from conftest import INIT, RAW_HEADERS, free_port

FAKES = Path(__file__).parent / "fakes"
SUPERVISOR = str(Path(sys.executable).parent / "rebox-supervisor")


def test_supervisor_lifecycle(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    java = bindir / "java"
    java.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKES / "fake_headless.py"} "$@"\n')
    java.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    ghidra = tmp_path / "ghidra"
    (ghidra / "support").mkdir(parents=True)
    ah = ghidra / "support" / "analyzeHeadless"  # stand-in: creates <dir>/<name>.gpr like the real one
    ah.write_text('#!/bin/sh\ntouch "$1/$2.gpr"\necho "$HOME" > "$1/analyze-home"\n')
    ah.chmod(0o755)
    marker = tmp_path / "marker"
    cfg = tmp_path / "agg.toml"
    cfg.write_text(f'[[child]]\nname = "a"\nprefix = "a_"\ncommand = ["{sys.executable}", "{FAKES / "fake_server.py"}", "a"]\n')
    port = free_port()
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("GHIDRA_PROJECTS_DIR", "REBOX_STATE_DIR")}, "PATH": f"{bindir}:{os.environ['PATH']}", "REBOX_TOKEN": "tok", "REBOX_PORT": str(port),
        "REBOX_BIND": "127.0.0.1", "REBOX_CONFIG": str(cfg), "RE_WORKDIR": str(work), "FAKE_MARKER": str(marker),
        "REBOX_INSTANCE": "supervisor-test", "GHIDRA_INSTALL_DIR": str(ghidra), "REBOX_AUTOSAVE_INTERVAL": "1", "REBOX_HEADLESS_START_TIMEOUT": "30",
    }
    proc = subprocess.Popen([SUPERVISOR], env=env, stdout=subprocess.PIPE, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx2.get(f"{base}/healthz").status_code == 200:
                    break
            except httpx2.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("supervisor never became healthy")
        state = work / "re-box-state"
        assert f"start project={state}/ghidra-projects/rebox.gpr home={state}/home user.home=['-Duser.home={state}/home'] cache={state}/cache" in marker.read_text()
        assert (state / "ghidra-projects" / "analyze-home").read_text().strip() == f"{state}/home"
        deadline = time.time() + 15
        while "/save_all_programs" not in marker.read_text() and time.time() < deadline:
            time.sleep(0.2)
        assert "/save_all_programs" in marker.read_text()
        assert "env_token=False" in marker.read_text()

        r = httpx2.post(f"{base}/mcp", json=INIT, headers={**RAW_HEADERS, "Authorization": "Bearer tok"})
        assert r.status_code == 200
        assert r.headers["mcp-session-id"]

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(30) == 0
        assert "/exit_ghidra" in marker.read_text()
        with pytest.raises(httpx2.HTTPError):
            httpx2.get(f"{base}/healthz")
    finally:
        if proc.poll() is None:
            proc.kill()
    out = proc.stdout.read()
    assert '"event": "session_close"' in out and '"reason": "shutdown"' in out


def test_supervisor_refuses_without_token(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "REBOX_TOKEN"}
    p = subprocess.run([SUPERVISOR], env=env, capture_output=True, text=True)
    assert p.returncode == 2 and "REBOX_TOKEN" in p.stderr and len(p.stderr.strip().splitlines()) == 1
