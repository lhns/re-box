"""Run the real rebox-supervisor against a fake `java` (the real JVM path is covered by the e2e tests)."""

import contextlib
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


@contextlib.contextmanager
def supervisor(tmp_path, **extra):
    """Yields (proc, base url, work dir, marker file) of a supervisor with fake java and analyzeHeadless."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    java = bindir / "java"
    java.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKES / "fake_headless.py"} "$@"\n')
    java.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    ghidra = tmp_path / "ghidra"
    (ghidra / "support").mkdir(parents=True)
    ah = ghidra / "support" / "analyzeHeadless"  # stand-in: creates <dir>/<name>.gpr like the real one
    ah.write_text('#!/bin/sh\ntouch "$1/$2.gpr"\necho "$HOME" > "$1/analyze-home"\n')
    ah.chmod(0o755)
    marker = tmp_path / "marker"
    marker.write_text("")
    cfg = tmp_path / "agg.toml"
    cfg.write_text(f'[[child]]\nname = "a"\nprefix = "a_"\ncommand = ["{sys.executable}", "{FAKES / "fake_server.py"}", "a"]\n')
    port = free_port()
    tmp = tmp_path / "t"  # the supervisor sweeps its temp dir at start: never the real one
    tmp.mkdir(exist_ok=True)
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("GHIDRA_PROJECTS_DIR", "REBOX_STATE_DIR")},
        "PATH": f"{bindir}:{os.environ['PATH']}", "REBOX_TOKEN": "tok", "REBOX_PORT": str(port),
        "REBOX_BIND": "127.0.0.1", "REBOX_CONFIG": str(cfg), "RE_WORKDIR": str(work), "FAKE_MARKER": str(marker),
        "REBOX_INSTANCE": f"supervisor-test-{port}", "GHIDRA_INSTALL_DIR": str(ghidra), "REBOX_AUTOSAVE_INTERVAL": "1",
        "REBOX_HEADLESS_START_TIMEOUT": "30", "TMPDIR": str(tmp), **extra,
    }
    proc = subprocess.Popen([SUPERVISOR], env=env, stdout=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx2.get(f"{base}/healthz").status_code == 200:
                    break
            except httpx2.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("supervisor never answered /healthz")
        yield proc, base, work, marker
    finally:
        if proc.poll() is None:
            proc.kill()


def wait_for(pred, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


def open_session(base):
    r = httpx2.post(f"{base}/mcp", json=INIT, headers={**RAW_HEADERS, "Authorization": "Bearer tok"})
    assert r.status_code == 200 and r.headers["mcp-session-id"]
    return r.headers["mcp-session-id"]


def close_session(base, sid):
    httpx2.delete(f"{base}/mcp", headers={"Authorization": "Bearer tok", "mcp-session-id": sid})


def test_supervisor_lifecycle(tmp_path):
    with supervisor(tmp_path, REBOX_GHIDRA_IDLE_TIMEOUT="3") as (proc, base, work, marker):
        # The port is open and healthy with no JVM until a session asks for one.
        assert httpx2.get(f"{base}/healthz").json() == {"status": "ok", "ghidra": "stopped"}
        time.sleep(1.5)
        assert "start project=" not in marker.read_text()

        sid = open_session(base)
        state = work / "re-box-state"
        assert wait_for(lambda: "start project=" in marker.read_text())
        assert f"start project={state}/ghidra-projects/rebox.gpr home={state}/home user.home=['-Duser.home={state}/home'] cache={state}/cache" in marker.read_text()
        assert (state / "ghidra-projects" / "analyze-home").read_text().strip() == f"{state}/home"
        assert (state / ".gitignore").read_text() == "*\n"
        assert wait_for(lambda: httpx2.get(f"{base}/healthz").json().get("ghidra") == "ready")
        assert wait_for(lambda: "/save_all_programs" in marker.read_text(), 15)
        assert "env_token=False" in marker.read_text()

        # Last session gone: saved and stopped after the idle timeout, and it stays down.
        close_session(base, sid)
        assert wait_for(lambda: "/exit_ghidra" in marker.read_text(), 20)
        assert wait_for(lambda: httpx2.get(f"{base}/healthz").json().get("ghidra") == "stopped", 20)
        time.sleep(1.5)
        assert marker.read_text().count("start project=") == 1

        # A new session starts it again.
        open_session(base)
        assert wait_for(lambda: marker.read_text().count("start project=") == 2)

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(30) == 0
        with pytest.raises(httpx2.HTTPError):
            httpx2.get(f"{base}/healthz")
    out = proc.stdout.read()
    assert '"event": "session_close"' in out and '"reason": "shutdown"' in out
    assert '"event": "project_size"' in out


def test_sigterm_while_stopped_exits_cleanly(tmp_path):
    with supervisor(tmp_path) as (proc, base, work, marker):
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(30) == 0
        assert "start project=" not in marker.read_text()


def test_supervisor_prunes_old_evidence_at_start(tmp_path):
    state = tmp_path / "work" / "re-box-state" / "rea" / "evidence"
    state.mkdir(parents=True)
    old, fresh = state / "old.json", state / "fresh.json"
    old.write_text("{}")
    fresh.write_text("{}")
    os.utime(old, (1, 1))
    with supervisor(tmp_path, REBOX_EVIDENCE_RETENTION_DAYS="30") as _:
        assert wait_for(lambda: not old.exists(), 10)
        assert fresh.exists()


def test_supervisor_sweeps_leftover_tmp_at_start(tmp_path):
    t = tmp_path / "t"
    (t / "rea-ghidra-x").mkdir(parents=True)
    (t / "rea-ghidra-x" / "target.bin").write_bytes(b"x" * 10)
    (t / "rebox-attach-locks").mkdir()
    (t / "rebox-attach-locks" / "a.lock").write_text("")
    (t / "unrelated").mkdir()
    with supervisor(tmp_path) as (proc, base, work, marker):
        assert not (t / "rea-ghidra-x").exists() and not (t / "rebox-attach-locks" / "a.lock").exists()
        assert (t / "unrelated").exists()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(30) == 0
    out = proc.stdout.read()
    assert '"event": "tmp_swept"' in out and '"removed": 2' in out and '"bytes": 10' in out


def test_supervisor_removes_partial_exports_at_start(tmp_path):
    ev = tmp_path / "work" / "re-box-state" / "rea" / "evidence"
    (ev / ".rea-write-abc").mkdir(parents=True)
    with supervisor(tmp_path) as _:
        assert wait_for(lambda: not (ev / ".rea-write-abc").exists(), 10)


def test_supervisor_refuses_without_token(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "REBOX_TOKEN"}
    p = subprocess.run([SUPERVISOR], env=env, capture_output=True, text=True)
    assert p.returncode == 2 and "REBOX_TOKEN" in p.stderr and len(p.stderr.strip().splitlines()) == 1
