"""The catalog cache: a child's full tool list, remembered so a session can advertise it before the
shared backend (Ghidra) is up, plus the session-less warm-up that fills it."""

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

import anyio
from mcp.shared.exceptions import MCPError
from mcp_types.version import LATEST_HANDSHAKE_VERSION

from . import __version__
from .catalog import ChildCatalog
from .children import Child
from .config import ChildSpec, child_env
from .log import log

_save_lock = threading.Lock()  # sessions save from worker threads; compare-and-write must not interleave


def resolve_key(spec: ChildSpec) -> str:
    """spec.catalog_cache.key with {NAME} (the child's environment) and {file:/path} filled in, reduced to
    filename-safe characters."""
    assert spec.catalog_cache
    env = {**os.environ, **child_env(spec, os.environ)}

    def fill(m: re.Match) -> str:
        name = m.group(1)
        if name.startswith("file:"):
            try:
                return Path(name[5:]).read_text().strip()
            except OSError:
                return "nofile"
        return env.get(name, "unset")

    return re.sub(r"[^A-Za-z0-9._-]+", "_", re.sub(r"\{([^{}]+)\}", fill, spec.catalog_cache.key))


def path_for(state_dir: str, spec: ChildSpec) -> Path:
    return Path(state_dir) / "cache" / "rebox-catalog" / f"{spec.name}-{resolve_key(spec)}.json"


def is_partial(part: ChildCatalog, full: ChildCatalog) -> bool:
    """Fewer tools than a listing of the same child (and cache key): the bridge was not connected yet."""
    return len(part.tools) < len(full.tools)


def load(state_dir: str, spec: ChildSpec) -> ChildCatalog | None:
    if not (state_dir and spec.catalog_cache):
        return None
    try:
        data = json.loads(path_for(state_dir, spec).read_text())
        part = ChildCatalog(**{k: list(data[k]) for k in ("tools", "prompts", "resources", "templates")})
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return part if part.tools else None


def save(state_dir: str, spec: ChildSpec, part: ChildCatalog) -> bool:
    """Write atomically; False when nothing changed or the directory is not writable."""
    if not (state_dir and spec.catalog_cache and part.tools):
        return False
    target = path_for(state_dir, spec)
    body = json.dumps(vars(part), separators=(",", ":"), ensure_ascii=False)
    with _save_lock:
        return _save(target, spec, part, body)


def _save(target: Path, spec: ChildSpec, part: ChildCatalog, body: str) -> bool:
    try:
        if target.exists():
            old = target.read_text()
            if old == body:
                return False
            if is_partial(part, ChildCatalog(tools=list(json.loads(old).get("tools", ())))):
                # The key names what changes a catalog, so a smaller one is a bridge that was not
                # connected yet; keeping the bigger one is the safe side.
                log("catalog_cache_kept", "warn", child=spec.name, tools=len(part.tools))
                return False
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(body)
            os.replace(tmp, target)
        except BaseException:
            os.unlink(tmp)
            raise
        for old in target.parent.glob(f"{spec.name}-*.json"):  # other keys: an older ghidra-mcp or setting
            if old != target:
                old.unlink(missing_ok=True)
    except (OSError, ValueError) as e:
        log("catalog_cache_failed", "warn", child=spec.name, error=getattr(e, "strerror", None) or str(e))
        return False
    log("catalog_cache_written", child=spec.name, tools=len(part.tools))
    return True


class _NoClient:
    """The warm-up's child has no client behind it."""

    async def on_request(self, child: Child, ctx: Any, method: str, params: Any) -> dict:
        if method == "ping":
            return {}
        raise MCPError(-32601, f"{method}: no client during the catalog warm-up")

    def on_notification(self, child: Child, method: str, params: Any) -> None: ...

    async def on_exit(self, child: Child) -> None: ...


async def fetch_catalog(spec: ChildSpec, env: dict[str, str], grace: float) -> ChildCatalog:
    """Run the child once like a session would (handshake, on_ready, list) and return its catalog.
    The caller makes sure the backend is up first."""
    child = Child(spec, "warmup", env, grace, _NoClient())
    async with anyio.create_task_group() as tg:
        try:
            await child.open(tg, {
                "protocolVersion": LATEST_HANDSHAKE_VERSION, "capabilities": {},
                "clientInfo": {"name": "rebox-warmup", "version": __version__},
            })
            if spec.on_ready:
                await child.request("tools/call", {"name": spec.on_ready.tool, "arguments": dict(spec.on_ready.arguments)})
            return await child.list_catalog()
        finally:
            await child.close()
            tg.cancel_scope.cancel()
