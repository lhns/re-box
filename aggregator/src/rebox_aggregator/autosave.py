"""Autosave: a child tool that persists the child's work (REA's evidence export), called on session close,
periodically, and before a tool that drops the work."""

import os
import time
from collections.abc import Callable, Iterable
from typing import Any

import anyio

from .children import Child
from .config import Autosave, Settings
from .evidence import drop_if_empty
from .log import log


class Autosaver:
    def __init__(self, settings: Settings, tag: str):
        self.settings, self.tag = settings, tag
        self.dirty: set[str] = set()  # children that served a tools/call since their last save
        self._locks: dict[str, anyio.Lock] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.state_dir) and any(s.autosave for s in self.settings.children)

    def arguments(self, spec: Autosave) -> dict:
        """The tool's arguments with {state_dir} and {session} filled in."""
        subs = {"{state_dir}": self.settings.state_dir, "{session}": self.tag}

        def fill(v: Any) -> Any:
            if isinstance(v, str):
                for k, new in subs.items():
                    v = v.replace(k, new)
                return v
            if isinstance(v, dict):
                return {k: fill(x) for k, x in v.items()}
            if isinstance(v, list):
                return [fill(x) for x in v]
            return v

        return fill(dict(spec.arguments))

    def saving(self, child: Child) -> bool:
        lock = self._locks.get(child.name)
        return lock is not None and lock.locked()

    async def before(self, child: Child, tool: Any) -> None:
        """A call to `tool` is about to drop what the autosave keeps (REA's close_binary clears its evidence)."""
        spec = child.spec.autosave
        if spec and tool in spec.before_tools:
            await self.save(child, f"before {tool}")

    async def save(self, child: Child, reason: str) -> None:
        """Persist a child's work when it is dirty. Never counts as session activity; failures only warn."""
        spec = child.spec.autosave
        if not (spec and self.settings.state_dir) or child.dead:
            return
        args = self.arguments(spec)
        lock = self._locks.setdefault(child.name, anyio.Lock())
        start = time.monotonic()
        outcome, error = "timeout", None
        with anyio.move_on_after(self.settings.autosave_timeout):
            async with lock:  # also when clean: a save in flight must finish before close goes on
                if child.name not in self.dirty or child.dead:  # clean, or a concurrent save covered it
                    return
                self.dirty.discard(child.name)
                try:
                    os.makedirs(os.path.dirname(args.get("path") or "") or ".", exist_ok=True)
                    outcome, error = await child.call_tool(spec.tool, args)
                    if outcome == "ok" and spec.prune_empty:
                        await anyio.to_thread.run_sync(drop_if_empty, args.get("path"))
                except OSError as e:
                    outcome, error = "rpc_error", f"cannot create directory: {e.strerror}"
                finally:
                    if outcome != "ok":  # also on timeout or a cancelled caller: retried by the next save
                        self.dirty.add(child.name)
        log(
            "autosave", "info" if outcome == "ok" else "warn", session=self.tag, child=child.name, reason=reason,
            outcome=outcome, duration_ms=round((time.monotonic() - start) * 1000), **({"error": error[:300]} if error else {}),
        )

    async def loop(self, children: Callable[[], Iterable[Child]], busy: Callable[[Child], bool]) -> None:
        """Every REBOX_AUTOSAVE_INTERVAL, save the children with nothing in flight."""
        while True:
            await anyio.sleep(self.settings.autosave_interval)
            for child in list(children()):
                if not busy(child):
                    await self.save(child, "interval")
