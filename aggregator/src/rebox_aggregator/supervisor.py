"""Container supervisor: ghidra-mcp headless server lifecycle, Ghidra project, and the aggregator app."""

import glob
import os
import signal
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import anyio
import anyio.abc
import httpx2
from anyio.streams.text import TextReceiveStream

from .app import App, make_server
from .children import kill_leftovers
from .config import ConfigError, harden, load_settings, read_token
from .log import log

HEADLESS_PORT = 8089
PROJECT_NAME = "rebox"
POLL_INTERVAL = 10.0


@dataclass(frozen=True)
class SupervisorConfig:
    workdir: Path
    projects_dir: Path
    ghidra_dir: str
    jar: str
    java_opts: list[str]
    start_timeout: float


def validate(env: Mapping[str, str]) -> SupervisorConfig:
    """Raise ConfigError (one clear line) when the container is misconfigured."""
    read_token(env)
    workdir_s = env.get("RE_WORKDIR", "")
    if not workdir_s or not os.path.isdir(workdir_s) or not os.access(workdir_s, os.W_OK):
        raise ConfigError(f"RE_WORKDIR {workdir_s!r} must be an existing writable directory")
    workdir = Path(os.path.abspath(workdir_s))
    projects = Path(os.path.abspath(env.get("GHIDRA_PROJECTS_DIR") or workdir / "ghidra-projects"))
    if projects != workdir and workdir not in projects.parents:
        raise ConfigError(f"GHIDRA_PROJECTS_DIR {projects} must be inside RE_WORKDIR {workdir}")
    if any(part.startswith(".") for part in projects.parts[1:]):
        raise ConfigError(f"GHIDRA_PROJECTS_DIR {projects} has a path element starting with '.', which Ghidra refuses")
    try:
        timeout = float(env.get("REBOX_HEADLESS_START_TIMEOUT", 300))
    except ValueError:
        raise ConfigError("REBOX_HEADLESS_START_TIMEOUT must be a number") from None
    return SupervisorConfig(
        workdir=workdir,
        projects_dir=projects,
        ghidra_dir=env.get("GHIDRA_INSTALL_DIR", "/opt/ghidra"),
        jar=env.get("GHIDRA_MCP_JAR", "/opt/ghidra-mcp/GhidraMCP.jar"),
        java_opts=env.get("JAVA_OPTS", "").split(),
        start_timeout=timeout,
    )


def headless_command(cfg: SupervisorConfig) -> list[str]:
    jars = [cfg.jar]
    for group in ("Framework", "Features", "Processors"):
        jars += sorted(glob.glob(f"{cfg.ghidra_dir}/Ghidra/{group}/*/lib/*.jar"))
    return [
        "java", *cfg.java_opts, "-Djava.awt.headless=true", f"-Dghidra.home={cfg.ghidra_dir}",
        "-Dapplication.name=GhidraMCP", "-classpath", ":".join(jars),
        "com.xebyte.headless.GhidraMCPHeadlessServer", "--port", str(HEADLESS_PORT), "--bind", "127.0.0.1",
    ]


async def ensure_project(http: httpx2.AsyncClient, base: str, projects_dir: Path) -> None:
    """Open the rebox project, creating it on first start."""
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


class Headless:
    def __init__(self, cfg: SupervisorConfig, instance: str, base: str | None = None):
        self.cfg, self.instance = cfg, instance
        self.base = base or f"http://127.0.0.1:{HEADLESS_PORT}"
        self.ready = anyio.Event()
        self.reason = "starting"
        self._proc: anyio.abc.Process | None = None
        self._healthy = False
        self._stopping = False
        self._http = httpx2.AsyncClient(timeout=10)

    def health(self) -> tuple[bool, str]:
        return (True, "") if self._healthy else (False, self.reason)

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

    async def run(self) -> None:
        """Start the headless server, keep it healthy, restart it with backoff when it exits."""
        backoff = 5.0
        while not self._stopping:
            env = {k: v for k, v in os.environ.items() if not k.startswith("REBOX_")}
            env["GHIDRA_MCP_FILE_ROOT"] = str(self.cfg.workdir)
            env["REBOX_INSTANCE"] = self.instance  # lets the shutdown sweep find a stuck JVM too
            self.cfg.projects_dir.mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            self.reason = "starting"
            self._proc = proc = await anyio.open_process(
                headless_command(self.cfg), env=env, stdout=-1, stderr=-2, start_new_session=True
            )
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._relay, proc)
                tg.start_soon(self._monitor, proc, tg.cancel_scope)
                await proc.wait()
                tg.cancel_scope.cancel()
            self._healthy = False
            self._kill_group(proc, signal.SIGKILL)  # stragglers of the old JVM's group
            self.reason = "headless server down"
            if self._stopping:
                return
            log("headless_exit", "error", exit_code=proc.returncode)
            if time.monotonic() - started > 60:
                backoff = 5.0
            await anyio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    async def _monitor(self, proc: anyio.abc.Process, scope: anyio.CancelScope) -> None:
        with anyio.move_on_after(self.cfg.start_timeout) as waiting:
            while not await self._check():
                await anyio.sleep(1)
        if waiting.cancelled_caught:
            log("headless_start_timeout", "error")
            self._kill_group(proc, signal.SIGKILL)
            return
        try:
            await ensure_project(self._http, self.base, self.cfg.projects_dir)
        except (httpx2.HTTPError, ValueError) as e:
            self.reason = "project not open"
            log("project_failed", "error", error=str(e)[:500])
            self._kill_group(proc, signal.SIGKILL)
            return
        self._healthy = True
        self.ready.set()
        while True:
            await anyio.sleep(POLL_INTERVAL)
            ok = await self._check()
            self._healthy = ok
            if not ok:
                self.reason = "headless server down"

    @staticmethod
    def _kill_group(proc: anyio.abc.Process, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    async def stop(self, skip: anyio.Event) -> None:
        """Save and exit Ghidra, then escalate: exit_ghidra, wait 60 s, SIGTERM, SIGKILL, instance sweep."""
        self._stopping = True
        self._healthy = False
        self.reason = "shutting down"
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                await self._http.post(f"{self.base}/exit_ghidra", timeout=30)
            except httpx2.HTTPError:
                pass
            for sig, wait in ((None, 60), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
                if sig is not None:
                    self._kill_group(proc, sig)
                await self._wait_exit(proc, skip, wait)
                if proc.returncode is not None:
                    break
        await anyio.to_thread.run_sync(kill_leftovers, None, self.instance)

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


async def supervise(cfg: SupervisorConfig, app: App) -> int:
    headless = Headless(cfg, app.instance)
    app.health = headless.health
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
            await app.shutdown()
            await headless.stop(skip)
            server.should_exit = True

        tg.start_soon(on_signals)
        tg.start_soon(headless.run)
        with anyio.move_on_after(cfg.start_timeout + 30) as waiting:
            await headless.ready.wait()
        if waiting.cancelled_caught:
            print("rebox-supervisor: headless server did not become ready", file=sys.stderr)
            await headless.stop(skip)
            tg.cancel_scope.cancel()
            return 1
        await server.serve()
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
        settings = load_settings()
    except ConfigError as e:
        print(f"rebox-supervisor: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    app = App(settings)
    os.environ["REBOX_INSTANCE"] = app.instance
    raise SystemExit(anyio.run(supervise, cfg, app))
