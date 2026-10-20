"""The ghidra-mcp headless JVM, run on demand, and the Ghidra project it serves."""

import glob
import os
import shutil
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import anyio
import anyio.abc
import httpx2

from .config import service_env
from .housekeeping import dir_size, kill_leftovers
from .log import log, relay_lines

HEADLESS_PORT = 8089
# REA's TMPDIR (REBOX_REA_TMP, which the rea child's [child.env] in aggregator.toml passes on): its
# runtime roots, with the target snapshot the attach helper has ghidra-mcp import, live here, so it
# is a file root too. Per uid: a container started as root runs its services as PUID.
REA_TMP = Path(os.environ.get("REBOX_REA_TMP") or f"/tmp/rebox-rea-{os.getuid()}")
PROJECT_NAME = "rebox"
POLL_INTERVAL = 10.0
# `support/analyzeHeadless` (runs a no-op script) creates the project: ghidra-mcp's HTTP API only
# creates projects inside GHIDRA_MCP_FILE_ROOT (RE_WORKDIR), and REBOX_STATE_DIR may be elsewhere.
SCRIPT_PATH = "/usr/local/lib/rebox/ghidra_scripts"
AUTOSAVE_CLIENT_TIMEOUT = 3600.0  # save_all_programs waits for running auto-analysis
SHUTDOWN_SAVE_WAIT = 30.0
HEALTHY_STATES = ("stopped", "stopping", "ready")  # /healthz is 200 in these (and while starting within the timeout)


@dataclass(frozen=True)
class SupervisorConfig:
    workdir: Path
    state_dir: Path
    projects_dir: Path
    ghidra_dir: str
    jar: str
    java_opts: list[str]
    start_timeout: float
    autosave_interval: float = 0.0
    idle_timeout: float = 0.0  # stop the Ghidra server this long after the last session closed; 0: never
    evidence_retention_days: float = 0.0  # 0: keep REA evidence bundles forever
    rea_attach_dir: Path = Path("/opt/rebox/rea-attach")  # REA attach files (deps/rea-attach), when present


ATTACH_SCRIPT = "ReboxReaAttach.java"
ATTACH_HELPER = "rea-attach/helper.mjs"


def install_attach_script(cfg: SupervisorConfig) -> None:
    """Put REA's attach host script where ghidra-mcp runs scripts from (~/ghidra_scripts of the JVM).

    Copied at every start, so an image upgrade replaces it. It has to live there: ghidra-mcp copies a
    script from anywhere else into that directory and deletes it after the run, which races between
    sessions attaching at once.
    """
    src = cfg.rea_attach_dir / ATTACH_SCRIPT
    if not src.is_file():
        return
    dest = cfg.state_dir / "home" / "ghidra_scripts"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        tmp = dest / f".{ATTACH_SCRIPT}.{os.getpid()}"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest / ATTACH_SCRIPT)
    except OSError as e:
        log("rea_attach_install", "warn", error=str(e)[:300])


# The heap follows the container's memory limit (or the host's memory without one), so nobody has to
# size -Xmx; the G1 flags give heap back after a burst. JAVA_OPTS comes after: an -Xmx there wins.
JVM_FLAGS = (
    "-XX:MaxRAMPercentage=50", "-XX:+UseG1GC", "-XX:G1PeriodicGCInterval=60000",
    "-XX:G1PeriodicGCSystemLoadThreshold=0", "-XX:MinHeapFreeRatio=10", "-XX:MaxHeapFreeRatio=30",
)


def headless_command(cfg: SupervisorConfig) -> list[str]:
    jars = [cfg.jar]
    for group in ("Framework", "Features", "Processors"):
        jars += sorted(glob.glob(f"{cfg.ghidra_dir}/Ghidra/{group}/*/lib/*.jar"))
    return [
        "java", *JVM_FLAGS, *cfg.java_opts, "-Djava.awt.headless=true", f"-Dghidra.home={cfg.ghidra_dir}",
        f"-Duser.home={cfg.state_dir / 'home'}",  # the JVM takes it from passwd, not $HOME
        "-Dapplication.name=GhidraMCP", "-classpath", ":".join(jars),
        "com.xebyte.headless.GhidraMCPHeadlessServer", "--port", str(HEADLESS_PORT), "--bind", "127.0.0.1",
        "--project", str(cfg.projects_dir / f"{PROJECT_NAME}.gpr"),
    ]


async def ensure_project(http: httpx2.AsyncClient, base: str, projects_dir: Path) -> None:
    """Check that the project is open; if --project did not open it, ask ghidra-mcp to (inside its file root).
    The .gpr exists by then: Headless._create_project makes it before the JVM starts."""
    info = await http.get(f"{base}/get_project_info")
    info.raise_for_status()
    if info.json().get("has_project"):
        return
    r = await http.post(f"{base}/open_project", json={"path": str(projects_dir / f"{PROJECT_NAME}.gpr")})
    r.raise_for_status()
    # ghidra-mcp answers 200 with {"error": ...} when it cannot open the project, e.g. while another
    # container holds its lock; trust only a re-check.
    try:
        error = r.json().get("error")
    except (ValueError, AttributeError):
        error = None
    again = await http.get(f"{base}/get_project_info")
    again.raise_for_status()
    if error or not again.json().get("has_project"):
        locked = "lock" in str(error or "").lower()
        log("project_failed", "error", error=str(error or "project did not open")[:500])
        raise ProjectError("project locked by another Ghidra instance" if locked else "project not open")


class ProjectError(ValueError):
    """The Ghidra project could not be opened; the message is a fixed, client-safe reason."""


class Headless:
    """The ghidra-mcp JVM, run on demand: started when a session wants it, stopped after it has been idle.

    States: stopped, starting, ready, failed (crashed, backing off), stopping (idle stop), shutting_down.
    """

    BACKOFF_START = 5.0  # restart delay after a crash, doubling up to BACKOFF_MAX
    BACKOFF_MAX = 60.0

    def __init__(self, cfg: SupervisorConfig, instance: str, base: str | None = None):
        self.cfg, self.instance = cfg, instance
        self.base = base or f"http://127.0.0.1:{HEADLESS_PORT}"
        self.state = "stopped"
        self.reason = ""
        self.on_ready: Callable[[], None] | None = None  # called each time the server becomes ready
        self._proc: anyio.abc.Process | None = None
        self._healthy = False
        self._stopping = False  # container shutdown
        self._idle_stopped = False  # the current process is being stopped for lack of sessions
        self._sessions = False
        self._warming = False
        self._wake = anyio.Event()
        self._starting_since = 0.0
        self._saving = anyio.Lock()  # one save_all_programs at a time
        # ghidra-mcp's HTTP port wants the token the JVM inherited (main() makes sure there is one)
        token = os.environ.get("GHIDRA_MCP_AUTH_TOKEN")
        self._http = httpx2.AsyncClient(timeout=10, headers={"Authorization": f"Bearer {token}"} if token else None)

    @property
    def _want(self) -> bool:
        return self._sessions or self._warming

    def want(self, wanted: bool) -> None:
        """Sessions exist (True) or none do (False)."""
        before = self._want
        self._sessions = wanted
        if self._want != before:
            self._poke()

    def warm(self, warming: bool) -> None:
        """The catalog warm-up needs the server, with no session around."""
        before = self._want
        self._warming = warming
        if self._want != before:
            self._poke()

    def ready(self) -> bool:
        return self._healthy

    def status(self) -> tuple[str, str]:
        return self.state, self.reason

    def health(self) -> tuple[bool, str, str]:
        """(ok, reason, state): a stopped server is fine (it starts with the next session); a failed one is not."""
        if self.state in HEALTHY_STATES:
            return True, "", self.state
        if self.state == "starting" and time.monotonic() - self._starting_since <= self.cfg.start_timeout:
            return True, "", self.state
        return False, self.reason or "headless server did not start in time", self.state

    def _set(self, state: str, reason: str = "") -> None:
        if state == "starting":
            self._starting_since = time.monotonic()
        changed = state != self.state
        if changed:
            log("headless_state", state=state, **({"reason": reason} if reason else {}))
        self.state, self.reason = state, reason
        if changed and state == "ready" and self.on_ready:
            self.on_ready()

    def _poke(self) -> None:
        woken, self._wake = self._wake, anyio.Event()
        woken.set()

    async def _until(self, pred: Callable[[], bool]) -> None:
        while not pred():
            await self._wake.wait()

    async def _check(self) -> bool:
        try:
            r = await self._http.get(f"{self.base}/check_connection")
            return r.status_code == 200 and r.json().get("status") == "ok"
        except (httpx2.HTTPError, ValueError):
            return False

    async def _create_project(self, env: dict[str, str]) -> bool:
        """First start: create the empty project with the headless analyzer (see SCRIPT_PATH)."""
        if (self.cfg.projects_dir / f"{PROJECT_NAME}.gpr").exists():
            return True
        cmd = [
            f"{self.cfg.ghidra_dir}/support/analyzeHeadless", str(self.cfg.projects_dir), PROJECT_NAME,
            "-scriptPath", SCRIPT_PATH, "-preScript", "ReboxNoop.java",
        ]
        out = b""
        try:
            with anyio.fail_after(self.cfg.start_timeout):
                res = await anyio.run_process(cmd, env=env, check=False, stderr=-2)
            out, ok = res.stdout, res.returncode == 0
        except (TimeoutError, OSError) as e:
            ok = False
            out = str(e).encode()
        if not ok or not (self.cfg.projects_dir / f"{PROJECT_NAME}.gpr").exists():
            tail = out.decode(errors="replace").strip().splitlines()[-20:]
            log("project_failed", "error", error="project creation failed", output=tail)
            return False
        return True

    async def save_all(self, reason: str) -> None:
        """Save every open program; skipped while another save runs. Failures only warn."""
        if self._saving.locked() or not self._healthy:
            return
        async with self._saving:
            start = time.monotonic()
            try:
                r = await self._http.post(f"{self.base}/save_all_programs", timeout=AUTOSAVE_CLIENT_TIMEOUT)
                r.raise_for_status()
                body = r.json()
                saved, errors = int(body.get("saved_count") or 0), len(body.get("errors") or [])
            except (httpx2.HTTPError, ValueError, AttributeError, TypeError) as e:
                log("autosave", "warn", component="ghidra", reason=reason, error=(str(e) or type(e).__name__)[:300])
                return
            if saved or errors:
                log(
                    "autosave", "warn" if errors else "info", component="ghidra", reason=reason, saved_count=saved,
                    errors=errors, duration_ms=round((time.monotonic() - start) * 1000),
                )

    async def _autosave_loop(self) -> None:
        interval = self.cfg.autosave_interval
        while interval > 0:
            await anyio.sleep(interval)
            if not self._stopping:
                await self.save_all("interval")

    async def run(self) -> None:
        """Start the headless server while sessions want it; restart it with backoff when it crashes."""
        backoff = self.BACKOFF_START
        while not self._stopping:
            await self._until(lambda: self._want or self._stopping)
            if self._stopping:
                return
            env = service_env()
            env["GHIDRA_MCP_FILE_ROOT"] = f"{self.cfg.workdir}:{REA_TMP}"
            # Only REA's pre- and post-scripts may run as part of an import (any client can import):
            # their own directory, without REA's bridge, which would load jars a descriptor names.
            env["GHIDRA_MCP_IMPORT_SCRIPT_ROOTS"] = str(self.cfg.rea_attach_dir / "import_scripts")
            env["REBOX_INSTANCE"] = self.instance  # lets the shutdown sweep find a stuck JVM too
            started = time.monotonic()
            self._set("starting")
            if not await self._create_project(env):
                self._set("failed", "project not created")
                await self._backoff(backoff)
                backoff = min(self.BACKOFF_MAX, backoff * 2)
                continue
            if self._stopping:
                return
            install_attach_script(self.cfg)
            self._proc = proc = await anyio.open_process(
                headless_command(self.cfg), env=env, stdout=-1, stderr=-2, start_new_session=True
            )
            async with anyio.create_task_group() as tg:
                tg.start_soon(relay_lines, proc.stdout, lambda line: log("headless_log", line=line))
                tg.start_soon(self._monitor, proc, tg.cancel_scope)
                tg.start_soon(self._autosave_loop)
                tg.start_soon(self._idle_stop, proc)
                await proc.wait()
                tg.cancel_scope.cancel()
            self._healthy = False
            self._kill_group(proc, signal.SIGKILL)  # stragglers of the old JVM's group
            if self._stopping:
                return
            if self._idle_stopped:  # no sessions, so no REA helpers either
                self._idle_stopped = False
                self._set("stopped")
                backoff = self.BACKOFF_START
                continue
            # A crash: the REA bridges it served are gone. Their helpers would notice within seconds;
            # killing them now makes REA report it at once (reopen the binary to attach again).
            await anyio.to_thread.run_sync(kill_leftovers, None, self.instance, ATTACH_HELPER)
            log("headless_exit", "error", exit_code=proc.returncode)
            if time.monotonic() - started > 60:
                backoff = self.BACKOFF_START
            if not self._want:
                self._set("stopped")  # nobody is waiting for it; the next session starts it again
                continue
            self._set("failed", self.reason or "headless server down")  # the monitor may have said why
            await self._backoff(backoff)
            backoff = min(self.BACKOFF_MAX, backoff * 2)

    async def _backoff(self, seconds: float) -> None:
        """Sleep, but wake early when the last session goes away (no point in restarting then)."""
        with anyio.move_on_after(seconds):
            await self._until(lambda: not self._want or self._stopping)
        if not self._want and not self._stopping:
            self._set("stopped")

    async def _idle_stop(self, proc: anyio.abc.Process) -> None:
        """Stop the server once no session has wanted it for the idle timeout."""
        timeout = self.cfg.idle_timeout
        if timeout <= 0:
            return
        while True:
            await self._until(lambda: not self._want)
            with anyio.move_on_after(timeout) as idle:
                await self._until(lambda: self._want)
            if not idle.cancelled_caught:
                continue  # a session came back
            with anyio.move_on_after(SHUTDOWN_SAVE_WAIT):
                await self.save_all("idle")
            if not self._want:
                break
        log("headless_idle_stop", idle_s=timeout)
        graceful = self._healthy
        self._idle_stopped = True
        self._healthy = False
        self._set("stopping")
        await self._terminate(proc, anyio.Event(), graceful)

    async def _monitor(self, proc: anyio.abc.Process, scope: anyio.CancelScope) -> None:
        with anyio.move_on_after(self.cfg.start_timeout) as waiting:
            while not await self._check():
                await anyio.sleep(1)
        if waiting.cancelled_caught:
            log("headless_start_timeout", "error")
            self._set("failed", "headless server did not start in time")
            self._kill_group(proc, signal.SIGKILL)
            return
        try:
            await ensure_project(self._http, self.base, self.cfg.projects_dir)
        except (httpx2.HTTPError, ValueError) as e:
            self._set("failed", str(e) if isinstance(e, ProjectError) else "project not open")
            log("project_failed", "error", error=str(e)[:500])
            self._kill_group(proc, signal.SIGKILL)
            return
        if self.state != "starting":  # being stopped meanwhile
            return
        self._healthy = True
        self._set("ready")
        size, files = await anyio.to_thread.run_sync(dir_size, self.cfg.projects_dir)
        log("project_size", bytes=size, files=files)
        while True:
            await anyio.sleep(POLL_INTERVAL)
            if self.state not in ("ready", "failed"):
                continue
            ok = await self._check()
            self._healthy = ok
            if ok:
                self._set("ready")
            else:
                self._set("failed", "headless server down")

    @staticmethod
    def _kill_group(proc: anyio.abc.Process, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    async def stop(self, skip: anyio.Event) -> None:
        """Container shutdown: save and exit Ghidra, escalate, then sweep the instance's leftovers."""
        self._stopping = True
        graceful = self._healthy
        self._healthy = False
        self._set("shutting_down", "shutting down")
        self._poke()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            await self._terminate(proc, skip, graceful)
        await anyio.to_thread.run_sync(kill_leftovers, None, self.instance)

    async def _terminate(self, proc: anyio.abc.Process, skip: anyio.Event, graceful: bool) -> None:
        """exit_ghidra (saves; skipped for a server that never came up), wait 60 s, SIGTERM, SIGKILL."""
        if graceful:
            try:
                await self._http.post(f"{self.base}/exit_ghidra", timeout=30)
            except httpx2.HTTPError:
                pass
        for sig, wait in ((None, 60 if graceful else 0), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
            if sig is not None:
                self._kill_group(proc, sig)
            if wait:
                await self._wait_exit(proc, skip, wait)
            if proc.returncode is not None:
                break

    @staticmethod
    async def _wait_exit(proc: anyio.abc.Process, skip: anyio.Event, timeout: float) -> None:
        with anyio.move_on_after(timeout):
            async with anyio.create_task_group() as tg:
                async def exited() -> None:
                    await proc.wait()
                    tg.cancel_scope.cancel()

                async def skipped() -> None:
                    await skip.wait()
                    tg.cancel_scope.cancel()

                tg.start_soon(exited)
                tg.start_soon(skipped)
