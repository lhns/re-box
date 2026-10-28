"""Container supervisor: ghidra-mcp headless server lifecycle, Ghidra project, and the aggregator app."""

import os
import secrets
import signal
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

import anyio

from . import catalogcache
from .app import App, make_server
from .config import ConfigError, child_environ, harden, load_settings, number, read_token
from .headless import REA_TMP, SHUTDOWN_SAVE_WAIT, Headless, SupervisorConfig
from .housekeeping import housekeeping_loop, sweep_tmp
from .log import log

SUBDIRS = ("ghidra-projects", "home", "cache", "rea/evidence")


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
    state_s = env.get("REBOX_STATE_DIR") or ""
    # Read-only is fine for a shared sample store, but only when the state dir is elsewhere (its default is inside).
    need = os.R_OK | os.X_OK | (0 if state_s else os.W_OK)
    if not workdir_s or not os.path.isdir(workdir_s) or not os.access(workdir_s, need):
        raise ConfigError(
            f"RE_WORKDIR {workdir_s!r} must be an existing directory, writable unless REBOX_STATE_DIR is set elsewhere"
        )
    workdir = Path(os.path.abspath(workdir_s))
    if state_s and not os.path.isabs(state_s):
        raise ConfigError(f"REBOX_STATE_DIR {state_s!r} must be an absolute path")
    state = Path(os.path.abspath(state_s or workdir / "re-box-state"))
    projects_s = env.get("GHIDRA_PROJECTS_DIR") or ""
    projects = Path(os.path.abspath(projects_s or state / "ghidra-projects"))
    if _dot_element(projects):
        raise ConfigError(f"GHIDRA_PROJECTS_DIR {projects} has a path element starting with '.', which Ghidra refuses")
    timeout, interval = number(env, "REBOX_HEADLESS_START_TIMEOUT", 300), number(env, "REBOX_AUTOSAVE_INTERVAL", 300)
    idle, retention = number(env, "REBOX_GHIDRA_IDLE_TIMEOUT", 300), number(env, "REBOX_EVIDENCE_RETENTION_DAYS", 30)
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




def ensure_mcp_auth_token() -> None:
    """ghidra-mcp's HTTP port (8089) requires `Authorization: Bearer $GHIDRA_MCP_AUTH_TOKEN` when that is set.
    Make one per container start unless preset: the JVM, the bridges and REA's helper inherit it, and a JVM
    restart keeps it, so the bridges survive that. Nothing logs or forwards it."""
    if not os.environ.get("GHIDRA_MCP_AUTH_TOKEN"):
        os.environ["GHIDRA_MCP_AUTH_TOKEN"] = secrets.token_hex(32)


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
            for spec in todo:
                env = child_environ(spec, app.instance, "warmup")
                part = await catalogcache.fetch_catalog(spec, env, app.settings.child_grace)
                await anyio.to_thread.run_sync(catalogcache.save, state, spec, part)
    except Exception as e:  # a background nicety: sessions fall back to holding their initialize
        log("catalog_warmup_failed", "warn", error=(str(e) or type(e).__name__)[:300])
    finally:
        headless.warm(False)


async def supervise(cfg: SupervisorConfig, app: App) -> int:
    headless = Headless(cfg, app.instance)
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
        tg.start_soon(housekeeping_loop, cfg.state_dir, cfg.evidence_retention_days)
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
    ensure_mcp_auth_token()
    try:
        cfg = validate(os.environ)
        _ensure_writable(REA_TMP, "REA's TMPDIR")
        os.environ["REBOX_REA_TMP"] = str(REA_TMP)
        apply_state_env(cfg)
        settings = load_settings()
    except ConfigError as e:
        print(f"rebox-supervisor: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    sweep_tmp(tempfile.gettempdir())  # no session exists yet, so nothing in there is in use
    app = App(settings)
    os.environ["REBOX_INSTANCE"] = app.instance
    raise SystemExit(anyio.run(supervise, cfg, app))
