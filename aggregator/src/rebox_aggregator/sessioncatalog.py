"""What one session advertises: its children's listings merged (catalog.Catalog) and kept current on their
list_changed, with the catalog cache standing in for a child whose backend (Ghidra) is still starting, and
the child's on_ready call once that backend is up."""

from collections.abc import Callable, Iterable
from typing import Any

import anyio
from mcp.shared.exceptions import MCPError

from . import catalogcache
from .catalog import Catalog, CatalogError, ChildCatalog
from .children import Child
from .config import ChildSpec, Settings, fill_in
from .log import log

Announce = Callable[[Iterable[str], Child | None], None]  # list_changed kinds, related to a request to that child


def changed_kinds(old: ChildCatalog, new: ChildCatalog) -> list[str]:
    """The list_changed kinds that tell a client about the difference between two listings of a child."""
    kinds = [k for k in ("tools", "prompts") if getattr(old, k) != getattr(new, k)]
    if (old.resources, old.templates) != (new.resources, new.templates):
        kinds.append("resources")
    return kinds


class SessionCatalog:
    def __init__(
        self, settings: Settings, backend: Any, tag: str, children: dict[str, Child],
        spawn: Callable[..., None], announce: Announce, closing: Callable[[], bool],
    ):
        self.settings, self.backend, self.tag, self.children = settings, backend, tag, children
        self._spawn, self._announce, self._closing = spawn, announce, closing
        self.merged: Catalog | None = None  # None until the session is initialized
        self.parts: dict[str, ChildCatalog] = {}
        self._cached: set[str] = set()  # children advertised from the cache, not yet from their live listing
        self._boot_tools: dict[str, set] = {}  # their tools as listed at boot (callable while cold)
        self._refreshing: dict[str, set[str]] = {}
        self._pending_ready: set[str] = set()  # children whose on_ready call is still owed
        self._client_ready = False  # the client sent notifications/initialized

    def build(self, parts: dict[str, ChildCatalog] | None = None) -> Catalog:
        parts = self.parts if parts is None else parts
        return Catalog.build(tuple(s for s in self.settings.children if s.name in parts), parts)

    # -- session open -----------------------------------------------------

    async def boot(self, child: Child) -> None:
        """A child booted and listed (self.parts): decide what the session advertises for it."""
        spec = child.spec
        if spec.on_ready and not self.backend.ready():
            self._pending_ready.add(spec.name)  # booted before its backend: told once that it is up
        if spec.catalog_cache and self.settings.state_dir:
            await self._cold_catalog(child, spec)

    async def _cold_catalog(self, child: Child, spec: ChildSpec) -> None:
        part = self.parts[spec.name]
        self._boot_tools[spec.name] = {t.get("name") for t in part.tools}
        cached = catalogcache.load(self.settings.state_dir, spec)
        # A live backend does not mean the child has connected to it yet: a listing smaller than the cache
        # is still the bridge's static one. Without a cache there is no telling, so that case connects it below.
        if self.backend.ready() and cached is not None and not catalogcache.is_partial(part, cached):
            self._spawn(self._save_cache, spec, part)
            return
        if cached is not None:  # advertised now; calls wait for the live catalog (wait_live)
            self.parts[spec.name] = cached
            self._cached.add(spec.name)
            if spec.on_ready:
                self._pending_ready.add(spec.name)  # told to connect once the client is initialized
            return
        # Nothing cached: hold the initialize reply until the child is connected, so the first list is complete.
        with anyio.move_on_after(self.settings.start_timeout) as waited:
            while not self.backend.ready():
                if self.backend.status()[0] in ("failed", "shutting_down"):
                    log("catalog_cold", "warn", session=self.tag, child=spec.name, reason=self.backend.status()[1])
                    return
                await anyio.sleep(0.25)
            if await self._run_on_ready(child) == "ok":
                self._pending_ready.discard(spec.name)
                try:
                    live = await child.list_catalog(base=part)
                except MCPError as e:
                    log("catalog_cold", "warn", session=self.tag, child=spec.name, reason=e.message[:300])
                    return
                self.parts[spec.name] = live
                self._spawn(self._save_cache, spec, live)
        if waited.cancelled_caught:
            log("catalog_cold", "warn", session=self.tag, child=spec.name, reason="backend did not start in time")

    # -- calls --------------------------------------------------------------

    def cold(self, child: str, tool: str) -> bool:
        """A call to this tool must wait for the child's live catalog (its boot listing did not have it)."""
        return child in self._cached and tool not in self._boot_tools.get(child, ())

    async def wait_live(self, child: Child) -> dict | None:
        """Hold a call to a cached tool until the child's live catalog has replaced the cache. Returns a tool
        error result when that cannot happen, else None."""
        with anyio.move_on_after(self.settings.start_timeout) as waited:
            while child.name in self._cached:
                state, reason = self.backend.status()
                if state in ("failed", "shutting_down"):
                    return _tool_error(fill_in(self.settings.backend_failed, {"state": state, "reason": reason}))
                if child.dead:
                    raise child.gone_error()
                await anyio.sleep(0.2)
        if waited.cancelled_caught:
            seconds = f"{self.settings.start_timeout:.0f}"
            return _tool_error(fill_in(self.settings.backend_starting, {"seconds": seconds}))
        return None

    # -- backend readiness --------------------------------------------------

    def client_initialized(self) -> None:
        self._client_ready = True
        self.backend_changed()

    def backend_ready(self) -> None:
        """The shared backend came up: tell every child that wants to know."""
        self._pending_ready |= {n for n, c in self.children.items() if c.spec.on_ready}
        self.backend_changed()

    def backend_changed(self) -> None:
        if not (self._client_ready and self.merged is not None and not self._closing() and self.backend.ready()):
            return
        for name in list(self._pending_ready):
            self._pending_ready.discard(name)
            child = self.children.get(name)
            if child is not None and not child.dead:
                self._spawn(self._call_on_ready, child)

    async def _call_on_ready(self, child: Child) -> None:
        if await self._run_on_ready(child) == "ok" and child.name in self._cached:
            await self._resync(child)  # the bridge will say so itself; do not depend on that

    async def _run_on_ready(self, child: Child) -> str:
        spec = child.spec.on_ready
        assert spec
        outcome, error = "failed", "timed out"
        with anyio.move_on_after(60):
            outcome, error = await child.call_tool(spec.tool, spec.arguments)
            if outcome == "rpc_error":
                outcome = "failed"
        log(
            "on_ready", "info" if outcome == "ok" else "warn", session=self.tag, child=child.name, outcome=outcome,
            **({"error": error[:300]} if error else {}),
        )
        return outcome

    async def _resync(self, child: Child) -> None:
        """Replace a cached catalog with the child's live one."""
        try:
            part = await child.list_catalog(base=self.parts.get(child.name))
            if self._partial(child, part):
                return  # not connected after all; its own list_changed brings the full listing
            old = self.parts[child.name]
            self._adopt(child, part)
            self._announce(changed_kinds(old, part), None)
        except (MCPError, CatalogError) as e:
            log("refresh_failed", "warn", session=self.tag, child=child.name, error=str(e)[:500])

    # -- list_changed ---------------------------------------------------------

    def list_changed(self, child: Child, kind: str) -> None:
        """In receive order: the child's responses are held back until the client has been told."""
        if self.merged is None or self._closing():
            return
        child.begin_hold()
        running = child.name in self._refreshing
        self._refreshing.setdefault(child.name, set()).add(kind)
        if not running:
            self._spawn(self._refresh, child)

    async def _refresh(self, child: Child) -> None:
        try:
            while kinds := self._refreshing.get(child.name):
                self._refreshing[child.name] = set()
                try:
                    part = await child.list_catalog(kinds, self.parts.get(child.name))
                    if child.name in self._cached and (not self.backend.ready() or self._partial(child, part)):
                        continue  # still the bridge's partial listing; the cached one stays
                    changed = self._adopt(child, part)
                except MCPError as e:
                    log("refresh_failed", "warn", session=self.tag, child=child.name, error=e.message[:500])
                    continue
                except CatalogError as e:
                    log("catalog_rejected", "error", session=self.tag, child=child.name, problems=e.problems)
                    continue
                if changed:
                    self._announce(sorted(kinds), child)
        finally:
            self._refreshing.pop(child.name, None)
            child.end_hold()

    # -- helpers --------------------------------------------------------------

    def _partial(self, child: Child, part: ChildCatalog) -> bool:
        """Smaller than the cached catalog still advertised for this child: not connected yet."""
        return child.name in self._cached and catalogcache.is_partial(part, self.parts[child.name])

    def _adopt(self, child: Child, part: ChildCatalog) -> bool:
        """Install a freshly listed catalog part, remember it in the cache. True when the client must be told."""
        merged = self.build({**self.parts, child.name: part})
        changed = part != self.parts.get(child.name) or child.name not in self._cached
        self.parts[child.name] = part
        self.merged = merged
        self._cached.discard(child.name)
        if child.spec.catalog_cache and self.settings.state_dir:
            self._spawn(self._save_cache, child.spec, part)
        return changed

    async def _save_cache(self, spec: ChildSpec, part: ChildCatalog) -> None:
        await anyio.to_thread.run_sync(catalogcache.save, self.settings.state_dir, spec, part)


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}
