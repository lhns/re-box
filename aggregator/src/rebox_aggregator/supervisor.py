"""Container supervisor: ghidra-mcp headless server lifecycle, Ghidra project, and the aggregator app."""

import glob
import os
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import anyio
import anyio.abc
import httpx2
from anyio.streams.text import TextReceiveStream

from . import catalogcache
from .app import App, make_server
from .children import kill_by_command, kill_leftovers
from .config import ConfigError, harden, load_settings, read_token
from .evidence import prune_old
from .housekeeping import dir_size, prune_rolled_logs, remove_partial_exports, sweep_tmp
from .log import log

HEADLESS_PORT = 8089
PROJECT_NAME = "rebox"
POLL_INTERVAL = 10.0
SUBDIRS = ("ghidra-projects", "home", "cache", "rea/evidence")
# `support/analyzeHeadless` (runs a no-op script) creates the project: ghidra-mcp's HTTP API only
# creates projects inside GHIDRA_MCP_FILE_ROOT (RE_WORKDIR), and REBOX_STATE_DIR may be elsewhere.
SCRIPT_PATH = "/usr/local/lib/rebox/ghidra_scripts"
AUTOSAVE_CLIENT_TIMEOUT = 3600.0  # save_all_programs waits for running auto-analysis
SHUTDOWN_SAVE_WAIT = 30.0
PRUNE_INTERVAL = 86400.0
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


def _dot_element(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts[1:])


def _ensure_writable(path: Path, what: str) -> None:
    """Create path and prove it is writable by creating a file in it (os.access misses read-only mounts and ACLs)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".rebox-probe-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        raise ConfigError(
            f"{what} {path} is not writable by uid {os.getuid()} gid {os.getgid()} ({e.strerror}); "
            "chown it to that user, or start the container as root with PUID/PGID"
        ) from None


def _keep_out_of_git(state: Path) -> None:
    """The state dir can sit inside a repo (it defaults to RE_WORKDIR/re-box-state): ignore all of it."""
    ignore = state / ".gitignore"
    if not ignore.exists():
        try:
            ignore.write_text("*\n")
        except OSError:
            pass


def validate(env: Mapping[str, str]) -> SupervisorConfig:
    """Raise ConfigError (one clear line) when the container is misconfigured."""
    read_token(env)
    workdir_s = env.get("RE_WORKDIR", "")
    if not workdir_s or not os.path.isdir(workdir_s) or not os.access(workdir_s, os.W_OK):
        raise ConfigError(f"RE_WORKDIR {workdir_s!r} must be an existing writable directory")
    workdir = Path(os.path.abspath(workdir_s))
    state_s = env.get("REBOX_STATE_DIR") or ""
    if state_s and not os.path.isabs(state_s):
        raise ConfigError(f"REBOX_STATE_DIR {state_s!r} must be an absolute path")
    state = Path(os.path.abspath(state_s or workdir / "re-box-state"))
    projects_s = env.get("GHIDRA_PROJECTS_DIR") or ""
    projects = Path(os.path.abspath(projects_s or state / "ghidra-projects"))
    if _dot_element(projects):
        raise ConfigError(f"GHIDRA_PROJECTS_DIR {projects} has a path element starting with '.', which Ghidra refuses")
    try:
        timeout = float(env.get("REBOX_HEADLESS_START_TIMEOUT", 300))
        interval = float(env.get("REBOX_AUTOSAVE_INTERVAL", 300))
        idle = float(env.get("REBOX_GHIDRA_IDLE_TIMEOUT", 300))
        retention = float(env.get("REBOX_EVIDENCE_RETENTION_DAYS", 30))
    except ValueError:
        raise ConfigError(
            "REBOX_HEADLESS_START_TIMEOUT, REBOX_AUTOSAVE_INTERVAL, REBOX_GHIDRA_IDLE_TIMEOUT and "
            "REBOX_EVIDENCE_RETENTION_DAYS must be numbers"
        ) from None
    if min(interval, idle, retention) < 0:
        raise ConfigError(
            "REBOX_AUTOSAVE_INTERVAL, REBOX_GHIDRA_IDLE_TIMEOUT and REBOX_EVIDENCE_RETENTION_DAYS must be >= 0"
        )
    _ensure_writable(state, "REBOX_STATE_DIR")
    _keep_out_of_git(state)
    for sub in SUBDIRS:
        _ensure_writable(state / sub, "REBOX_STATE_DIR")
    if projects != state / "ghidra-projects":
        _ensure_writable(projects, "GHIDRA_PROJECTS_DIR")
    return SupervisorConfig(
        workdir=workdir,
        state_dir=state,
        projects_dir=projects,
        ghidra_dir=env.get("GHIDRA_INSTALL_DIR", "/opt/ghidra"),
        jar=env.get("GHIDRA_MCP_JAR", "/opt/ghidra-mcp/GhidraMCP.jar"),
        java_opts=env.get("JAVA_OPTS", "").split(),
        start_timeout=timeout,
        autosave_interval=interval,
        idle_timeout=idle,
        evidence_retention_days=retention,
        rea_attach_dir=Path(env.get("REBOX_REA_ATTACH_DIR", "/opt/rebox/rea-attach")),
    )


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


def apply_state_env(cfg: SupervisorConfig) -> None:
    """Point HOME and the XDG dirs of the services and every child into the state dir."""
    home = cfg.state_dir / "home"
    os.environ.update({
        "REBOX_STATE_DIR": str(cfg.state_dir),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(cfg.state_dir / "cache"),
    })


def headless_command(cfg: SupervisorConfig) -> list[str]:
    jars = [cfg.jar]
    for group in ("Framework", "Features", "Processors"):
        jars += sorted(glob.glob(f"{cfg.ghidra_dir}/Ghidra/{group}/*/lib/*.jar"))
    return [
        "java", *cfg.java_opts, "-Djava.awt.headless=true", f"-Dghidra.home={cfg.ghidra_dir}",
        f"-Duser.home={cfg.state_dir / 'home'}",  # the JVM takes it from passwd, not $HOME
        "-Dapplication.name=GhidraMCP", "-classpath", ":".join(jars),
        "com.xebyte.headless.GhidraMCPHeadlessServer", "--port", str(HEADLESS_PORT), "--bind", "127.0.0.1",
        "--project", str(cfg.projects_dir / f"{PROJECT_NAME}.gpr"),
    ]


async def ensure_project(http: httpx2.AsyncClient, base: str, projects_dir: Path) -> None:
    """Fallback when --project did not open it: ghidra-mcp's HTTP API, which only works inside its file root."""
    info = await http.get(f"{base}/get_project_info")
    info.raise_for_status()
    if info.json().get("has_project"):
        return
    gpr = projects_dir / f"{PROJECT_NAME}.gpr"
    if gpr.exists():
        r = await http.post(f"{base}/open_project", json={"path": str(gpr)})
    else:
        r = await http.post(f"{base}/create_project", json={"parentDir": str(projects_dir), "name": PROJECT_NAME})
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
        self._fail = ""  # why the last start failed, once known (shown in /healthz)
        self._wake = anyio.Event()
        self._starting_since = 0.0
        self._saving = anyio.Lock()  # one save_all_programs at a time
        self._http = httpx2.AsyncClient(timeout=10)

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
        if state != self.state:
            log("headless_state", state=state, **({"reason": reason} if reason else {}))
        self.state, self.reason = state, reason

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

    async def _relay(self, proc: anyio.abc.Process) -> None:
        buf = ""
        async for chunk in TextReceiveStream(proc.stdout, errors="replace"):  # type: ignore[arg-type]
            buf += chunk
            *lines, buf = buf.split("\n")
            for line in lines:
                if line.strip():
                    log("headless_log", line=line.rstrip()[:2000])

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
        backoff = 5.0
        while not self._stopping:
            await self._until(lambda: self._want or self._stopping)
            if self._stopping:
                return
            env = {k: v for k, v in os.environ.items() if not k.startswith("REBOX_")}
            env["GHIDRA_MCP_FILE_ROOT"] = str(self.cfg.workdir)
            env["REBOX_INSTANCE"] = self.instance  # lets the shutdown sweep find a stuck JVM too
            started = time.monotonic()
            self._fail = ""
            self._set("starting")
            if not await self._create_project(env):
                self._set("failed", "project not created")
                await self._backoff(backoff)
                backoff = min(60.0, backoff * 2)
                continue
            if self._stopping:
                return
            install_attach_script(self.cfg)
            self._proc = proc = await anyio.open_process(
                headless_command(self.cfg), env=env, stdout=-1, stderr=-2, start_new_session=True
            )
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._relay, proc)
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
                backoff = 5.0
                continue
            # A crash: the REA bridges it served are gone. Their helpers would notice within seconds;
            # killing them now makes REA report it at once (reopen the binary to attach again).
            await anyio.to_thread.run_sync(kill_by_command, self.instance, ATTACH_HELPER)
            log("headless_exit", "error", exit_code=proc.returncode)
            if time.monotonic() - started > 60:
                backoff = 5.0
            if not self._want:
                self._set("stopped")  # nobody is waiting for it; the next session starts it again
                continue
            self._set("failed", self._fail or "headless server down")
            await self._backoff(backoff)
            backoff = min(60.0, backoff * 2)

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
            self._fail = str(e) if isinstance(e, ProjectError) else "project not open"
            self._set("failed", self._fail)
            log("project_failed", "error", error=str(e)[:500])
            self._kill_group(proc, signal.SIGKILL)
            return
        if self.state != "starting":  # being stopped meanwhile
            return
        self._healthy = True
        self._set("ready")
        size, files = await anyio.to_thread.run_sync(dir_size, self.cfg.projects_dir)
        log("project_size", bytes=size, files=files)
        if self.on_ready:
            self.on_ready()
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


def _housekeep(cfg: SupervisorConfig, first: bool) -> None:
    evidence = str(cfg.state_dir / "rea" / "evidence")
    if first:
        remove_partial_exports(evidence)
    prune_old(evidence, cfg.evidence_retention_days)
    prune_rolled_logs(str(cfg.state_dir))


async def prune_evidence_loop(cfg: SupervisorConfig) -> None:
    """At start and then daily: drop old REA evidence bundles (when a retention is set), half-written
    exports (at start only) and old rolled Ghidra logs."""
    first = True
    while True:
        await anyio.to_thread.run_sync(_housekeep, cfg, first)
        first = False
        await anyio.sleep(PRUNE_INTERVAL)


async def warm_up(cfg: SupervisorConfig, app: App, headless: Headless) -> None:
    """Fill missing catalog caches at container start: run the server once, list the children against it
    exactly as a session would, then let the normal idle stop take the server down again."""
    state = app.settings.state_dir
    todo = [s for s in app.settings.children if s.catalog_cache and state and catalogcache.load(state, s) is None]
    if not todo:
        return
    log("catalog_warmup", names=[s.name for s in todo])
    headless.warm(True)
    try:
        with anyio.fail_after(cfg.start_timeout + 60):
            while not headless.ready():
                await anyio.sleep(0.5)
            base = {k: v for k, v in os.environ.items() if not k.startswith("REBOX_")}
            for spec in todo:
                env = {**base, **spec.env, "REBOX_INSTANCE": app.instance, "REBOX_SESSION": "warmup"}
                part = await catalogcache.fetch_catalog(spec, env)
                await anyio.to_thread.run_sync(catalogcache.save, state, spec, part)
    except Exception as e:  # a background nicety: sessions fall back to holding their initialize
        log("catalog_warmup_failed", "warn", error=(str(e) or type(e).__name__)[:300])
    finally:
        headless.warm(False)


async def supervise(cfg: SupervisorConfig, app: App) -> int:
    headless = Headless(cfg, app.instance)
    app.health = headless.health
    app.backend = headless
    headless.on_ready = app.backend_became_ready
    skip = anyio.Event()
    server = make_server(app)
    async with anyio.create_task_group() as tg:
        async def on_signals() -> None:
            with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as sigs:
                first = True
                async for _ in sigs:
                    if not first:
                        skip.set()
                        continue
                    first = False
                    tg.start_soon(shutdown)

        async def shutdown() -> None:
            # Saved first and concurrently: a short `docker stop` grace may end before the sessions are closed.
            # Bounded, since a save waits for running auto-analysis; exit_ghidra saves again anyway.
            async def early_save() -> None:
                with anyio.move_on_after(SHUTDOWN_SAVE_WAIT):
                    await headless.save_all("shutdown")

            async with anyio.create_task_group() as stg:
                stg.start_soon(early_save)
                stg.start_soon(app.shutdown)
            await headless.stop(skip)
            server.should_exit = True

        tg.start_soon(on_signals)
        tg.start_soon(headless.run)
        tg.start_soon(prune_evidence_loop, cfg)
        tg.start_soon(warm_up, cfg, app, headless)
        await server.serve()  # the port opens at once; Ghidra starts with the first session
        tg.cancel_scope.cancel()
    return 0


def main() -> None:
    import argparse

    argparse.ArgumentParser(
        prog="rebox-supervisor",
        description="Run the ghidra-mcp headless server and the MCP aggregator; configured through env.",
    ).parse_args()
    harden()
    try:
        cfg = validate(os.environ)
        apply_state_env(cfg)
        settings = load_settings()
    except ConfigError as e:
        print(f"rebox-supervisor: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    sweep_tmp(tempfile.gettempdir())  # no session exists yet, so nothing in there is in use
    app = App(settings)
    os.environ["REBOX_INSTANCE"] = app.instance
    raise SystemExit(anyio.run(supervise, cfg, app))
