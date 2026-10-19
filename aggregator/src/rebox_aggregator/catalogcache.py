"""The catalog cache: a child's full tool list, remembered so a session can advertise it before the
shared backend (Ghidra) is up, plus the session-less warm-up that fills it."""

import itertools
import json
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp_types.version import LATEST_HANDSHAKE_VERSION

from . import __version__
from .catalog import ChildCatalog
from .config import ChildSpec
from .log import log

MAX_LINE = 64 * 1024 * 1024


def resolve_key(spec: ChildSpec, environ: dict[str, str] | None = None) -> str:
    """spec.catalog_cache.key with {NAME} and {file:/path} filled in, reduced to filename-safe characters."""
    assert spec.catalog_cache
    env = {**(os.environ if environ is None else environ), **spec.env}

    def fill(m: re.Match) -> str:
        name = m.group(1)
        if name.startswith("file:"):
            try:
                return Path(name[5:]).read_text().strip()
            except OSError:
                return "nofile"
        return env.get(name, "unset")

    return re.sub(r"[^A-Za-z0-9._-]+", "_", re.sub(r"\{([^{}]+)\}", fill, spec.catalog_cache.key))


def path_for(state_dir: str, spec: ChildSpec, environ: dict[str, str] | None = None) -> Path:
    return Path(state_dir) / "cache" / "rebox-catalog" / f"{spec.name}-{resolve_key(spec, environ)}.json"


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
    try:
        if target.exists():
            old = target.read_text()
            if old == body:
                return False
            if len(json.loads(old).get("tools", ())) > len(part.tools):
                # The key names what changes a catalog, so a smaller one is a bridge that was not
                # connected yet; keeping the bigger one is the safe side.
                log("catalog_cache_kept", "warn", child=spec.name, tools=len(part.tools))
                return False
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_text(body)
        os.replace(tmp, target)
        for old in target.parent.glob(f"{spec.name}-*.json"):  # other keys: an older ghidra-mcp or setting
            if old != target:
                old.unlink(missing_ok=True)
    except (OSError, ValueError) as e:
        log("catalog_cache_failed", "warn", child=spec.name, error=getattr(e, "strerror", None) or str(e))
        return False
    log("catalog_cache_written", child=spec.name, tools=len(part.tools))
    return True


async def fetch_catalog(spec: ChildSpec, env: dict[str, str]) -> ChildCatalog:
    """Run the child once like a session would (initialize, on_ready, list) and return its catalog.
    The caller makes sure the backend is up first. The child's process group is killed afterwards."""
    proc = await anyio.open_process(
        list(spec.command), stdin=-1, stdout=-1, stderr=subprocess.DEVNULL, env=env, start_new_session=True
    )
    ids = itertools.count(1)
    try:
        assert proc.stdin and proc.stdout
        out = BufferedByteReceiveStream(proc.stdout)

        async def send(msg: dict) -> None:
            await proc.stdin.send(json.dumps(msg).encode() + b"\n")  # type: ignore[union-attr]

        async def rpc(method: str, params: dict | None = None) -> dict:
            rid = next(ids)
            await send({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params else {})})
            while True:
                msg = json.loads(await out.receive_until(b"\n", MAX_LINE))
                if isinstance(msg, dict) and msg.get("id") == rid and "method" not in msg:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                    return msg.get("result") or {}

        async def listing(method: str, key: str) -> list[dict]:
            items: list[dict] = []
            cursor = None
            while True:
                r = await rpc(method, {"cursor": cursor} if cursor else None)
                items += r.get(key, [])
                cursor = r.get("nextCursor")
                if not cursor:
                    return items

        init = await rpc("initialize", {
            "protocolVersion": LATEST_HANDSHAKE_VERSION, "capabilities": {},
            "clientInfo": {"name": "rebox-warmup", "version": __version__},
        })
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if spec.on_ready:
            await rpc("tools/call", {"name": spec.on_ready.tool, "arguments": dict(spec.on_ready.arguments)})
        caps: dict[str, Any] = init.get("capabilities") or {}
        part = ChildCatalog()
        if "tools" in caps:
            part.tools = await listing("tools/list", "tools")
        if "prompts" in caps:
            part.prompts = await listing("prompts/list", "prompts")
        if "resources" in caps:
            part.resources = await listing("resources/list", "resources")
            try:
                part.templates = await listing("resources/templates/list", "resourceTemplates")
            except RuntimeError:
                pass
        return part
    finally:
        with anyio.CancelScope(shield=True):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
