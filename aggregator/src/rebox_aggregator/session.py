"""Per-session router between the downstream transport and the children.

Messages stay plain dicts end to end; results are forwarded unchanged. mcp's JSONRPCDispatcher runs both
sides: downstream over the session's transport, and one per child (children.py).
"""

import hashlib
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import anyio
from anyio.abc import TaskStatus
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.shared.dispatcher import DispatchContext
from mcp.shared.exceptions import MCPError, NoBackChannelError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher, progress_token_from_params
from mcp.shared.message import MessageMetadata, ServerMessageMetadata, SessionMessage
from mcp_types import JSONRPCMessage, JSONRPCNotification
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_HANDSHAKE_VERSION

from . import __version__, catalogcache
from .catalog import Catalog, CatalogError, ChildCatalog
from .children import Child, kill_leftovers
from .config import Settings, child_environ
from .evidence import drop_if_empty
from .log import log

LIST_CHANGED = {
    "notifications/tools/list_changed": "tools",
    "notifications/prompts/list_changed": "prompts",
    "notifications/resources/list_changed": "resources",
}
MAX_INFLIGHT = 64
MAX_QUEUED_NOTIFICATIONS = 1000
LISTS = {
    "tools/list": "tools",
    "prompts/list": "prompts",
    "resources/list": "resources",
    "resources/templates/list": "resourceTemplates",
}


class NoBackend:
    """Stand-in for the supervisor's Ghidra lifecycle (supervisor.Headless) when the app runs on its own."""

    def want(self, wanted: bool) -> None: ...

    def ready(self) -> bool:
        return True

    def status(self) -> tuple[str, str]:
        return "ready", ""

    def health(self) -> tuple[bool, str, str]:
        """(ok, reason, backend state) for /healthz."""
        return True, "", ""


@dataclass
class Inflight:
    id: Any
    ctx: DispatchContext
    child: Child | None = None
    token: Any = None  # the request's progressToken, once forwarded to a child
    progress: float = 0.0  # highest progress value sent downstream for it
    progress_at: float = 0.0  # monotonic time of the last progress downstream (real or synthetic)


class _Queue:
    """The downstream dispatcher's write stream: the session's own queue, so a stalled client never blocks."""

    def __init__(self, write: Callable[[JSONRPCMessage, MessageMetadata], None]):
        self._write = write

    async def send(self, item: SessionMessage) -> None:
        self._write(item.message, item.metadata)

    async def aclose(self) -> None: ...

    async def __aenter__(self) -> "_Queue":
        return self

    async def __aexit__(self, *exc: object) -> None: ...


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _changed_kinds(old: ChildCatalog, new: ChildCatalog) -> list[str]:
    """The list_changed kinds that tell a client about the difference between two listings of a child."""
    kinds = [k for k in ("tools", "prompts") if getattr(old, k) != getattr(new, k)]
    if (old.resources, old.templates) != (new.resources, new.templates):
        kinds.append("resources")
    return kinds


class Session:
    def __init__(
        self,
        sid: str,
        transport: StreamableHTTPServerTransport,
        settings: Settings,
        instance: str,
        on_close: Callable[["Session"], None],
        backend: Any = None,
    ):
        self.sid, self.transport, self.settings, self.instance, self.on_close = sid, transport, settings, instance, on_close
        self.backend = backend or NoBackend()  # want(), ready(), status()
        self.get_streams = 0  # open standalone GET (notification) streams; maintained by the app
        self._client_ready = False  # the client sent notifications/initialized
        self._pending_ready: set[str] = set()  # children whose on_ready call is still owed
        self._cached: set[str] = set()  # children whose advertised catalog is the cache, not yet their live listing
        self._boot_tools: dict[str, set] = {}  # children's tool names as listed at boot (callable while cold)
        self.tag = hashlib.sha256(sid.encode()).hexdigest()[:16]
        self.opened_at = time.monotonic()
        self.last_activity = self.opened_at
        self.children: dict[str, Child] = {}
        self.parts: dict[str, ChildCatalog] = {}
        self.catalog: Catalog | None = None
        self.initialized = False
        self._initializing = False
        self._inflight: dict[Any, Inflight] = {}
        self._refreshing: dict[str, set[str]] = {}
        self._dirty: set[str] = set()  # children that served a tools/call since their last autosave
        self._save_locks: dict[str, anyio.Lock] = {}
        self._closing: str | None = None
        self._closed = anyio.Event()
        self._down: JSONRPCDispatcher | None = None
        self._tg: Any = None
        self._out_send, self._out_recv = anyio.create_memory_object_stream[SessionMessage](math.inf)
        self._queued = 0
        self._warned_backpressure = False

    # -- lifecycle --------------------------------------------------------

    async def serve(self, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        reason = "disconnect"
        try:
            async with self.transport.connect() as (read, write):
                self._down = JSONRPCDispatcher(read, _Queue(self._write), on_stream_exception=self._transport_error)
                async with anyio.create_task_group() as tg:
                    self._tg = tg
                    tg.start_soon(self._write_downstream, write)
                    tg.start_soon(self._idle_watch)
                    if self.settings.progress_keepalive > 0:
                        tg.start_soon(self._keepalive_loop)
                    if self._autosave_on() and self.settings.autosave_interval > 0:
                        tg.start_soon(self._autosave_loop)
                    await self._down.run(self._on_request, self._on_notify, task_status=task_status)
                    await self.close("disconnect")  # while the child tasks still run, so the autosave can answer
                    tg.cancel_scope.cancel()
        except Exception as e:  # session-local: never take the app's task group down
            log("internal_error", "error", session=self.tag, error=repr(e)[:500])
            reason = "internal_error"
        finally:
            await self.close(reason)

    async def close(self, reason: str) -> None:
        if self._closing is not None:
            await self._closed.wait()
            return
        self._closing = reason
        with anyio.CancelScope(shield=True):
            async with anyio.create_task_group() as tg:
                for child in self.children.values():
                    tg.start_soon(self._autosave, child, "close")
            async with anyio.create_task_group() as tg:
                for child in self.children.values():
                    tg.start_soon(child.close)
            leftovers = await anyio.to_thread.run_sync(kill_leftovers, self.tag, self.instance)
            log(
                "session_close",
                session=self.tag,
                reason=reason,
                duration_s=round(time.monotonic() - self.opened_at, 3),
                leftovers_killed=leftovers,
            )
            if not self.transport.is_terminated:
                await self.transport.terminate()
            self.on_close(self)
            self._closed.set()
        if self._tg is not None:
            self._tg.cancel_scope.cancel()

    def idle_timeout(self) -> float:
        """A client that keeps its GET stream open is present (an interactive agent); one without is
        probably gone (`claude -p` exited), so it gives up its slot, and its REA JVM, sooner."""
        s = self.settings
        if self.get_streams > 0 or s.orphan_timeout <= 0:
            return s.idle_timeout
        return min(s.idle_timeout, s.orphan_timeout)

    @property
    def busy(self) -> bool:
        return bool(self._inflight)

    def idle_for(self) -> float:
        return time.monotonic() - self.last_activity

    async def _idle_watch(self) -> None:
        while True:
            timeout = self.idle_timeout()
            await anyio.sleep(max(0.05, min(5.0, timeout / 5)))
            timeout = self.idle_timeout()
            idle = self.idle_for()
            # A long call may legitimately keep a session quiet; a client silent for 4x is gone.
            if idle > timeout and (not self._inflight or idle > 4 * timeout):
                await self.close("orphaned" if timeout < self.settings.idle_timeout else "idle")

    async def _keepalive_loop(self) -> None:
        """Clients abort a request after minutes without progress; imports and analysis can be quiet longer."""
        interval = self.settings.progress_keepalive
        while True:
            await anyio.sleep(max(0.02, interval / 4))
            now = time.monotonic()
            for fl in list(self._inflight.values()):
                if fl.token is not None and now - fl.progress_at >= interval:
                    fl.progress += 0.001  # must increase; real values from the child stay larger
                    fl.progress_at = now
                    self._notify_down(
                        "notifications/progress",
                        {"progressToken": fl.token, "progress": fl.progress, "message": "still working"},
                        fl.id,
                    )

    # -- autosave ---------------------------------------------------------

    def _autosave_on(self) -> bool:
        return bool(self.settings.state_dir) and any(s.autosave for s in self.settings.children)

    def _autosave_call(self, child: Child) -> tuple[str, dict]:
        spec = child.spec.autosave
        assert spec
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

        return spec.tool, fill(dict(spec.arguments))

    async def _autosave(self, child: Child, reason: str) -> None:
        """Persist a child's work through its configured tool. Never touches last_activity."""
        if not (child.spec.autosave and self.settings.state_dir) or child.dead:
            return
        tool, args = self._autosave_call(child)
        lock = self._save_locks.setdefault(child.name, anyio.Lock())
        start = time.monotonic()
        outcome, error = "timeout", None
        with anyio.move_on_after(self.settings.autosave_timeout):
            async with lock:  # also when clean: a save in flight must finish before close goes on
                if child.name not in self._dirty or child.dead:  # clean, or a concurrent save covered it
                    return
                self._dirty.discard(child.name)
                try:
                    os.makedirs(os.path.dirname(args.get("path") or "") or ".", exist_ok=True)
                    result = await child.request("tools/call", {"name": tool, "arguments": args})
                    outcome = "ok"
                    if result.get("isError"):
                        outcome = "tool_error"
                        error = " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
                    elif child.spec.autosave.prune_empty:
                        await anyio.to_thread.run_sync(drop_if_empty, args.get("path"))
                except MCPError as e:
                    outcome, error = "rpc_error", e.message
                except OSError as e:
                    outcome, error = "rpc_error", f"cannot create directory: {e.strerror}"
                finally:
                    if outcome != "ok":  # also on timeout or a cancelled caller: retried by the next save
                        self._dirty.add(child.name)
        log(
            "autosave",
            "info" if outcome == "ok" else "warn",
            session=self.tag,
            child=child.name,
            reason=reason,
            outcome=outcome,
            duration_ms=round((time.monotonic() - start) * 1000),
            **({"error": error[:300]} if error else {}),
        )

    async def _autosave_loop(self) -> None:
        while True:
            await anyio.sleep(self.settings.autosave_interval)
            for child in list(self.children.values()):
                busy = any(fl.child is child for fl in self._inflight.values())
                if not (self._closing or busy):
                    await self._autosave(child, "interval")

    async def on_exit(self, child: Child) -> None:
        # A session missing a backend is half dead (no rea_ tools, or no ghidra_ edits); the client's
        # next request gets 404 and re-initializes into a whole one.
        if self.initialized and not self._closing:
            self._tg.start_soon(self.close, "child_exited")

    # -- downstream writes --------------------------------------------------

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def _write(self, msg: JSONRPCMessage, metadata: MessageMetadata = None, *, droppable: bool = False) -> None:
        """Queue a downstream message. Droppable notifications are dropped once too many are waiting."""
        if droppable and self._queued > MAX_QUEUED_NOTIFICATIONS:
            if not self._warned_backpressure:
                self._warned_backpressure = True
                log("downstream_backpressure", "warn", session=self.tag, queued=self._queued)
            return
        self._queued += 1
        try:
            self._out_send.send_nowait(SessionMessage(msg, metadata))
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            self._queued -= 1

    async def _write_downstream(self, write: Any) -> None:
        async for item in self._out_recv:
            try:
                await write.send(item)
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass
            self._queued -= 1

    def _notify_down(self, method: str, params: Mapping[str, Any] | None, related: Any = None, *, droppable: bool = True) -> None:
        msg = JSONRPCNotification(jsonrpc="2.0", method=method, params=dict(params) if params is not None else None)
        self._write(msg, ServerMessageMetadata(related_request_id=related) if related is not None else None, droppable=droppable)

    def _related_for(self, child: Child) -> Inflight | None:
        """An in-flight downstream request to this child: what it says rides that request's stream."""
        return next((fl for fl in self._inflight.values() if fl.child is child), None)

    async def _all_settled(self) -> None:
        for child in list(self.children.values()):
            await child.settled.wait()

    async def _transport_error(self, e: Exception) -> None:
        log("transport_error", "warn", session=self.tag, error=type(e).__name__)

    # -- downstream messages ----------------------------------------------

    async def _on_notify(self, ctx: DispatchContext, method: str, params: Mapping[str, Any] | None) -> None:
        try:
            await self._handle_notification(method, params)
        except Exception as e:  # the session is suspect now; other sessions are not
            log("internal_error", "error", session=self.tag, error=repr(e)[:500])
            self._tg.start_soon(self.close, "internal_error")

    async def _handle_notification(self, method: str, params: Mapping[str, Any] | None) -> None:
        # notifications/cancelled: the dispatcher has cancelled that request's handler.
        if method == "notifications/initialized":
            self._client_ready = True
            self.backend_changed()
        elif method == "notifications/roots/list_changed":
            for child in self.children.values():
                await child.notify(method, dict(params) if params is not None else None)

    async def _on_request(self, ctx: DispatchContext, method: str, params: Mapping[str, Any] | None) -> dict:
        rid = ctx.request_id
        # The dispatcher neither refuses a duplicate id nor caps concurrency.
        if rid in self._inflight:
            raise MCPError(-32600, "duplicate request id")
        if len(self._inflight) >= MAX_INFLIGHT:
            raise MCPError(-32000, "too many concurrent requests")
        fl = self._inflight[rid] = Inflight(rid, ctx)
        p = dict(params or {})
        start = time.monotonic()
        outcome, code = "ok", None
        try:
            result = await self._dispatch(method, p, fl)
            if method == "tools/call" and result.get("isError"):
                outcome = "tool_error"
            return result
        except MCPError as e:
            outcome, code = "rpc_error", e.code
            raise
        except anyio.get_cancelled_exc_class():
            outcome = "cancelled"
            raise
        except Exception as e:  # the dispatcher would answer code 0
            log("internal_error", "error", session=self.tag, method=method, error=repr(e)[:500])
            outcome, code = "rpc_error", -32603
            raise MCPError(-32603, "Internal error") from None
        finally:
            del self._inflight[rid]
            self.last_activity = time.monotonic()
            if method == "tools/call":
                tool = p.get("name")
                log(
                    "tool_call",
                    session=self.tag,
                    tool=tool if isinstance(tool, str) else None,
                    duration_ms=round((time.monotonic() - start) * 1000),
                    outcome=outcome,
                    error_code=code,
                )

    async def _dispatch(self, method: str, p: dict, fl: Inflight) -> dict:
        if method == "initialize":
            return await self._initialize(p)
        if method == "ping":
            return {}
        if self.catalog is None:
            raise MCPError(-32600, "session is not initialized")
        await self._all_settled()
        cat = self.catalog
        if method in LISTS:
            return {LISTS[method]: cat.lists[LISTS[method]]}
        if method in ("tools/call", "prompts/get"):
            kind = "tool" if method == "tools/call" else "prompt"
            owner = (cat.tools if kind == "tool" else cat.prompts).get(p.get("name"))  # type: ignore[arg-type]
            if owner is None:
                if kind == "tool" and p.get("name") in cat.denied:
                    raise MCPError(-32602, f"Tool {p.get('name')} is disabled by this re-box's configuration")
                raise MCPError(-32602, f"Unknown {kind}: {p.get('name')}")
            if kind == "tool" and owner[0] in self._cached and owner[1] not in self._boot_tools.get(owner[0], ()):
                early = await self._wait_live(self.children[owner[0]], fl, p)
                if early is not None:
                    return early
                owner = self.catalog.tools.get(p.get("name"))  # type: ignore[union-attr,arg-type]
                if owner is None:
                    raise MCPError(-32602, f"Unknown tool: {p.get('name')}")
            return await self._forward(self.children[owner[0]], method, {**p, "name": owner[1]}, fl)
        if method in ("resources/read", "resources/subscribe", "resources/unsubscribe"):
            return await self._forward(self._owner_of_uri(p.get("uri")), method, p, fl)
        if method == "completion/complete":
            ref = p.get("ref") or {}
            if ref.get("type") == "ref/prompt":
                owner = cat.prompts.get(ref.get("name"))
                if owner is None:
                    raise MCPError(-32602, f"Unknown prompt: {ref.get('name')}")
                return await self._forward(self.children[owner[0]], method, {**p, "ref": {**ref, "name": owner[1]}}, fl)
            if ref.get("type") == "ref/resource":
                return await self._forward(self._owner_of_uri(ref.get("uri")), method, p, fl)
            raise MCPError(-32602, "Unsupported completion ref")
        if method == "logging/setLevel":
            targets = [c for c in self.children.values() if not c.dead and "logging" in c.capabilities]
            async with anyio.create_task_group() as tg:
                for c in targets:
                    tg.start_soon(self._quiet_request, c, method, p)
            return {}
        raise MCPError(-32601, f"Method not found: {method}")

    @staticmethod
    async def _quiet_request(child: Child, method: str, params: dict) -> None:
        try:
            await child.request(method, params)
        except MCPError:
            pass

    def _owner_of_uri(self, uri: Any) -> Child:
        assert self.catalog
        name = self.catalog.owner_of_uri(uri) if isinstance(uri, str) else None
        if name is None:
            raise MCPError(-32602, f"Unknown resource: {uri}")
        return self.children[name]

    async def _forward(self, child: Child, method: str, params: dict, fl: Inflight) -> dict:
        if child.dead:
            raise child.gone_error()
        fl.child = child
        on_progress = None
        token = progress_token_from_params(params)
        if token is not None:  # the dispatcher gives the child its own token; progress comes back here
            fl.token, fl.progress_at = token, time.monotonic()

            async def on_progress(progress: float, total: float | None, message: str | None) -> None:
                self._relay_progress(fl, progress, total, message)

        save = child.spec.autosave
        if method == "tools/call" and save and params.get("name") in save.before_tools:
            # The call drops what the autosave keeps (REA's close_binary clears its evidence).
            await self._autosave(child, f"before {params.get('name')}")
        try:
            return await child.request(method, params, on_progress=on_progress)
        finally:
            if method == "tools/call":
                self._dirty.add(child.name)
            await child.settled.wait()  # a catalog refresh announces itself before this response

    def _relay_progress(self, fl: Inflight, progress: float, total: float | None, message: str | None) -> None:
        fl.progress_at = time.monotonic()
        if progress <= fl.progress:  # keepalives went past it; progress must keep increasing
            progress = fl.progress + 0.001
        fl.progress = progress
        params: dict[str, Any] = {"progressToken": fl.token, "progress": progress}
        if total is not None:
            params["total"] = total
        if message is not None:
            params["message"] = message
        self._notify_down("notifications/progress", params, fl.id)

    # -- initialize -------------------------------------------------------

    async def _initialize(self, p: dict) -> dict:
        if self._initializing or self.initialized:
            raise MCPError(-32600, "session already initialized")
        self._initializing = True
        result = await self._open(p)
        self.initialized = True
        log("session_open", session=self.tag, children=[{"name": c.name, "pid": c.pid} for c in self.children.values()])
        return result

    async def _open(self, p: dict) -> dict:
        requested = p.get("protocolVersion")
        version = requested if requested in HANDSHAKE_PROTOCOL_VERSIONS else LATEST_HANDSHAKE_VERSION
        child_params = {
            "protocolVersion": version,
            "capabilities": p.get("capabilities") or {},
            "clientInfo": p.get("clientInfo") or {"name": "rebox-aggregator", "version": __version__},
        }
        async def boot(spec: Any) -> None:
            if self._closing:
                return
            if spec.on_ready and not self.backend.ready():
                self._pending_ready.add(spec.name)  # booted before its backend: told once that it is up
            env = child_environ(spec, self.instance, self.tag)
            child = self.children[spec.name] = Child(spec, self.tag, env, self.settings.child_grace, self)
            try:
                with anyio.fail_after(self.settings.init_timeout):
                    await child.open(self._tg, child_params)
                    self.parts[spec.name] = await child.list_catalog()
                if spec.catalog_cache and self.settings.state_dir:
                    await self._cold_catalog(child, spec)
            except (MCPError, OSError, TimeoutError) as e:
                log("child_init_failed", "error", session=self.tag, child=spec.name, error=(str(e) or type(e).__name__)[:500])
                self.parts.pop(spec.name, None)
                if child.proc is not None:
                    await child.close()
                    with anyio.move_on_after(2):
                        await child.proc.wait()
                    child.returncode = child.proc.returncode
                child.dead = True

        async with anyio.create_task_group() as tg:
            for spec in self.settings.children:
                tg.start_soon(boot, spec)
        up = [c for c in self.children.values() if not c.dead]
        if not up:
            raise MCPError(-32603, "no backend started; see the container log")
        try:
            self.catalog = self._build_catalog()
        except CatalogError as e:
            log("catalog_rejected", "error", session=self.tag, problems=e.problems)
            raise MCPError(-32603, "invalid catalog: " + "; ".join(e.problems)) from None
        return {
            "protocolVersion": version,
            "capabilities": self._merged_capabilities(up),
            "serverInfo": {"name": "rebox-aggregator", "version": __version__},
            "instructions": self._instructions(),
        }

    def _instructions(self) -> str:
        """What an agent cannot see from the tool list: where state lives and how the two backends differ."""
        s = self.settings
        lines = [
            "re-box: Ghidra (ghidra_ tools) and REA (rea_ tools) behind one endpoint.",
            "ghidra_: one persistent Ghidra project shared by all sessions. Pass the program selector on every call; "
            "ghidra_import_file only reads files under RE_WORKDIR" + (f" ({os.environ['RE_WORKDIR']})." if os.environ.get("RE_WORKDIR") else "."),
            "rea_: REA works in the same Ghidra project: rea_open_binary imports the file into "
            "/rea/<sha256 prefix>-<REA profile>/<file name> (open it with ghidra_ tools there), and REA's renames and "
            "comments persist, whatever REA's own texts say about an ephemeral project. rea_ tools see edits made "
            "meanwhile by ghidra_ tools or other sessions. REA's evidence and in-session state still end with the "
            "session.",
            "Paths: the container sees host paths unchanged (same-path mount). On Windows send the Git Bash form "
            "(/c/Users/you/re/x.bin), not C:\\... .",
            "Ghidra starts with the first session (about 40 s). A ghidra_ call made while it starts waits for it; "
            "if it fails you get a tool error saying why.",
        ]
        if s.state_dir:
            ev = next((self._autosave_call(c)[1].get("path") for c in self.children.values() if c.spec.autosave), None)
            lines.append(
                f"State directory: {s.state_dir}. Session tag: {self.tag}."
                + (f" Evidence is saved to {ev} on close and periodically." if ev else "")
            )
        return "\n".join(lines)

    # -- backend readiness ------------------------------------------------

    def on_backend_ready(self) -> None:
        """The shared backend (Ghidra) came up: tell every child that wants to know."""
        self._pending_ready |= {n for n, c in self.children.items() if c.spec.on_ready}
        self.backend_changed()

    def backend_changed(self) -> None:
        if not (self._client_ready and self.initialized and not self._closing and self.backend.ready()):
            return
        for name in list(self._pending_ready):
            self._pending_ready.discard(name)
            child = self.children.get(name)
            if child is not None and not child.dead:
                self._tg.start_soon(self._call_on_ready, child)

    async def _call_on_ready(self, child: Child) -> None:
        if await self._run_on_ready(child) == "ok" and child.name in self._cached:
            await self._resync(child)  # the bridge will say so itself; do not depend on that

    async def _resync(self, child: Child) -> None:
        """Replace a cached catalog with the child's live one."""
        try:
            part = await child.list_catalog(base=self.parts.get(child.name))
            if self._partial(child, part):
                return  # not connected after all; its own list_changed brings the full listing
            old = self.parts[child.name]
            self._adopt(child, part)
            for kind in _changed_kinds(old, part):
                self._notify_down(f"notifications/{kind}/list_changed", None, None, droppable=False)
        except (MCPError, CatalogError) as e:
            log("refresh_failed", "warn", session=self.tag, child=child.name, error=str(e)[:500])

    def _partial(self, child: Child, part: ChildCatalog) -> bool:
        """A listing smaller than the cached catalog still advertised for this child: not connected yet."""
        return child.name in self._cached and len(part.tools) < len(self.parts[child.name].tools)

    def _adopt(self, child: Child, part: ChildCatalog) -> bool:
        """Install a freshly listed catalog part, remember it in the cache. True when the client must be told."""
        candidate = self._build_catalog({**self.parts, child.name: part})
        changed = part != self.parts.get(child.name) or child.name not in self._cached
        self.parts[child.name] = part
        self.catalog = candidate
        self._cached.discard(child.name)
        if child.spec.catalog_cache and self.settings.state_dir:
            self._tg.start_soon(self._save_cache, child.spec, part)
        return changed

    async def _save_cache(self, spec: Any, part: ChildCatalog) -> None:
        await anyio.to_thread.run_sync(catalogcache.save, self.settings.state_dir, spec, part)

    async def _cold_catalog(self, child: Child, spec: Any) -> None:
        """A child with a catalog cache booted: decide what the session advertises for it."""
        part = self.parts[spec.name]
        self._boot_tools[spec.name] = {t.get("name") for t in part.tools}
        cached = catalogcache.load(self.settings.state_dir, spec)
        # A live backend does not mean the child has connected to it yet: a listing smaller than the
        # cache is still the bridge's static one (the rule catalogcache.save uses too). Without a cache
        # there is no telling, so that case connects it below.
        if self.backend.ready() and cached is not None and len(part.tools) >= len(cached.tools):
            self._tg.start_soon(self._save_cache, spec, part)
            return
        if cached is not None:  # advertised now; calls wait for the live catalog (_wait_live)
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
                self._tg.start_soon(self._save_cache, spec, live)
        if waited.cancelled_caught:
            log("catalog_cold", "warn", session=self.tag, child=spec.name, reason="backend did not start in time")

    async def _wait_live(self, child: Child, fl: Inflight, p: dict) -> dict | None:
        """Hold a call to a cached tool until the child's live catalog has replaced the cache.
        Returns a tool error result when that cannot happen, else None."""
        token = progress_token_from_params(p)
        if token is not None:
            fl.token, fl.progress_at = token, time.monotonic()  # keepalive progress covers the wait
        with anyio.move_on_after(self.settings.start_timeout) as waited:
            while child.name in self._cached:
                state, reason = self.backend.status()
                if state in ("failed", "shutting_down"):
                    return _tool_error(f"Ghidra is {state}: {reason}. Try again later.")
                if child.dead:
                    raise child.gone_error()
                await anyio.sleep(0.2)
        if waited.cancelled_caught:
            return _tool_error(f"Ghidra is still starting after {self.settings.start_timeout:.0f} s. Try again later.")
        return None

    async def _run_on_ready(self, child: Child) -> str:
        spec = child.spec.on_ready
        assert spec
        outcome, error = "ok", None
        try:
            with anyio.fail_after(60):
                result = await child.request("tools/call", {"name": spec.tool, "arguments": dict(spec.arguments)})
            if result.get("isError"):
                outcome = "tool_error"
                error = " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
        except (MCPError, TimeoutError) as e:
            outcome, error = "failed", str(e) or type(e).__name__
        log(
            "on_ready", "info" if outcome == "ok" else "warn", session=self.tag, child=child.name, outcome=outcome,
            **({"error": error[:300]} if error else {}),
        )
        return outcome

    def _build_catalog(self, parts: dict[str, ChildCatalog] | None = None) -> Catalog:
        parts = self.parts if parts is None else parts
        return Catalog.build(tuple(s for s in self.settings.children if s.name in parts), parts)

    @staticmethod
    def _merged_capabilities(children: list[Child]) -> dict:
        caps: dict[str, dict] = {}
        for c in children:
            for key in ("tools", "prompts", "resources"):
                if key in c.capabilities:
                    merged = caps.setdefault(key, {})
                    for flag in ("listChanged", "subscribe"):
                        if (c.capabilities[key] or {}).get(flag):
                            merged[flag] = True
            for key in ("completions", "logging"):
                if key in c.capabilities:
                    caps[key] = {}
        return caps

    # -- child messages ---------------------------------------------------

    async def on_request(self, child: Child, ctx: DispatchContext, method: str, params: Mapping[str, Any] | None) -> dict:
        """A child's request to the client (roots/list, sampling, elicitation) goes out under the dispatcher's own
        id, on the stream of a request to that child when there is one; a cancel either way is relayed."""
        if method == "ping":
            return {}
        assert self._down
        fl = self._related_for(child)
        if fl is not None:
            try:
                return await fl.ctx.send_raw_request(method, params)
            except NoBackChannelError:
                pass  # that request just finished
        return await self._down.send_raw_request(method, params)

    def on_notification(self, child: Child, method: str, params: Mapping[str, Any] | None) -> None:
        """In receive order. Progress and cancellation are the dispatcher's (see _forward, on_request)."""
        if method in LIST_CHANGED:
            self._list_changed(child, LIST_CHANGED[method])
        elif method not in ("notifications/progress", "notifications/cancelled") and self.initialized:
            fl = self._related_for(child)
            self._notify_down(method, params, fl.id if fl else None)

    def _list_changed(self, child: Child, kind: str) -> None:
        if self.catalog is None or self._closing:
            return
        child.begin_hold()
        running = child.name in self._refreshing
        self._refreshing.setdefault(child.name, set()).add(kind)
        if not running:
            self._tg.start_soon(self._refresh, child)

    async def _refresh(self, child: Child) -> None:
        """Re-list a child after list_changed; its responses stay held until the client is told."""
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
                if not changed:
                    continue
                fl = self._related_for(child)
                for kind in sorted(kinds):
                    self._notify_down(f"notifications/{kind}/list_changed", None, fl.id if fl else None, droppable=False)
        finally:
            self._refreshing.pop(child.name, None)
            child.end_hold()
