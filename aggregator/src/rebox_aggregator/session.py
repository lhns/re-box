"""Per-session router between the downstream transport and the children.

Messages stay plain dicts end to end; results are forwarded unchanged. mcp's JSONRPCDispatcher runs both
sides: downstream over the session's transport, and one per child (children.py). What the session
advertises lives in sessioncatalog.py, the children's autosave in autosave.py.
"""

import hashlib
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
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

from . import __version__
from .autosave import Autosaver
from .catalog import CatalogError
from .children import Child
from .config import ChildSpec, Settings, child_environ
from .housekeeping import kill_leftovers
from .log import log
from .sessioncatalog import SessionCatalog

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
    """Stand-in for the supervisor's Ghidra lifecycle (headless.Headless) when the app runs on its own."""

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
    token: Any = None  # the request's progressToken, once forwarded to a child or held for one
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
        self.tag = hashlib.sha256(sid.encode()).hexdigest()[:16]
        self.opened_at = time.monotonic()
        self.last_activity = self.opened_at
        self.children: dict[str, Child] = {}
        self.autosaver = Autosaver(settings, self.tag)
        self.catalog = SessionCatalog(
            settings, self.backend, self.tag, self.children, self._spawn, self._announce, lambda: self._closing is not None
        )
        self.initialized = False
        self._initializing = False
        self._inflight: dict[Any, Inflight] = {}
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
                    if self.autosaver.enabled and self.settings.autosave_interval > 0:
                        tg.start_soon(self.autosaver.loop, self.children.values, self._busy_with)
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
                    tg.start_soon(self.autosaver.save, child, "close")
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

    def touch(self) -> None:
        self.last_activity = time.monotonic()

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

    def _busy_with(self, child: Child) -> bool:
        return self._closing is not None or self._related_for(child) is not None

    def _spawn(self, fn: Callable[..., Any], *args: Any) -> None:
        self._tg.start_soon(fn, *args)

    async def on_exit(self, child: Child) -> None:
        # A session missing a backend is half dead (no rea_ tools, or no ghidra_ edits); the client's
        # next request gets 404 and re-initializes into a whole one. (A child that failed to start is not listed.)
        if self.initialized and not self._closing and child.name in self.catalog.parts:
            self._spawn(self.close, "child_exited")

    def on_backend_ready(self) -> None:
        self.catalog.backend_ready()

    # -- downstream writes --------------------------------------------------

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

    def _announce(self, kinds: Iterable[str], child: Child | None) -> None:
        """list_changed, never dropped; on the stream of a request to `child` when there is one, else the GET stream."""
        fl = self._related_for(child) if child else None
        for kind in kinds:
            self._notify_down(f"notifications/{kind}/list_changed", None, fl.id if fl else None, droppable=False)

    def _related_for(self, child: Child) -> Inflight | None:
        """An in-flight downstream request to this child: what the child says rides that request's stream."""
        return next((fl for fl in self._inflight.values() if fl.child is child), None)

    async def _transport_error(self, e: Exception) -> None:
        log("transport_error", "warn", session=self.tag, error=type(e).__name__)

    # -- downstream messages ----------------------------------------------

    async def _on_notify(self, ctx: DispatchContext, method: str, params: Mapping[str, Any] | None) -> None:
        try:
            await self._handle_notification(method, params)
        except Exception as e:  # the session is suspect now; other sessions are not
            log("internal_error", "error", session=self.tag, error=repr(e)[:500])
            self._spawn(self.close, "internal_error")

    async def _handle_notification(self, method: str, params: Mapping[str, Any] | None) -> None:
        # notifications/cancelled: the dispatcher has already cancelled that request's handler.
        if method == "notifications/initialized":
            self.catalog.client_initialized()
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
        if self.catalog.merged is None:
            raise MCPError(-32600, "session is not initialized")
        for child in list(self.children.values()):  # a pending catalog refresh first
            await child.settled.wait()
        cat = self.catalog.merged
        if method in LISTS:
            return {LISTS[method]: cat.lists[LISTS[method]]}
        if method in ("tools/call", "prompts/get"):
            kind = "tool" if method == "tools/call" else "prompt"
            owner = (cat.tools if kind == "tool" else cat.prompts).get(p.get("name"))  # type: ignore[arg-type]
            if owner is None:
                if kind == "tool" and p.get("name") in cat.denied:
                    raise MCPError(-32602, f"Tool {p.get('name')} is disabled by this re-box's configuration")
                raise MCPError(-32602, f"Unknown {kind}: {p.get('name')}")
            if kind == "tool" and self.catalog.cold(*owner):
                token = progress_token_from_params(p)
                if token is not None:
                    fl.token, fl.progress_at = token, time.monotonic()  # keepalive progress covers the wait
                early = await self.catalog.wait_live(self.children[owner[0]])
                if early is not None:
                    return early
                owner = self.catalog.merged.tools.get(p.get("name"))  # type: ignore[arg-type]
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
            async with anyio.create_task_group() as tg:
                for c in self.children.values():
                    if not c.dead and "logging" in c.capabilities:
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
        assert self.catalog.merged
        name = self.catalog.merged.owner_of_uri(uri) if isinstance(uri, str) else None
        if name is None:
            raise MCPError(-32602, f"Unknown resource: {uri}")
        return self.children[name]

    async def _forward(self, child: Child, method: str, params: dict, fl: Inflight) -> dict:
        if child.dead:
            raise child.gone_error()
        fl.child = child
        on_progress = None
        token = progress_token_from_params(params)
        if token is not None:  # the dispatcher gives the child its own token; its progress comes back here
            fl.token, fl.progress_at = token, time.monotonic()
            on_progress = partial(self._relay_progress, fl)
        if method == "tools/call":
            await self.autosaver.before(child, params.get("name"))
        try:
            return await child.request(method, params, on_progress=on_progress)
        finally:
            if method == "tools/call":
                self.autosaver.dirty.add(child.name)
            await child.settled.wait()  # a catalog refresh announces itself before this response

    async def _relay_progress(self, fl: Inflight, progress: float, total: float | None, message: str | None) -> None:
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
        """Boot every child concurrently: handshake (with the client's params), list, then the catalog's say."""
        requested = p.get("protocolVersion")
        version = requested if requested in HANDSHAKE_PROTOCOL_VERSIONS else LATEST_HANDSHAKE_VERSION
        child_params = {
            "protocolVersion": version,
            "capabilities": p.get("capabilities") or {},
            "clientInfo": p.get("clientInfo") or {"name": "rebox-aggregator", "version": __version__},
        }

        async def boot(spec: ChildSpec) -> None:
            if self._closing:
                return
            env = child_environ(spec, self.instance, self.tag)
            child = self.children[spec.name] = Child(spec, self.tag, env, self.settings.child_grace, self)
            try:
                with anyio.fail_after(self.settings.init_timeout):
                    await child.open(self._tg, child_params)
                    self.catalog.parts[spec.name] = await child.list_catalog()
                await self.catalog.boot(child)
            except (MCPError, OSError, TimeoutError) as e:
                log("child_init_failed", "error", session=self.tag, child=spec.name, error=(str(e) or type(e).__name__)[:500])
                self.catalog.parts.pop(spec.name, None)
                child.dead = True
                await child.close()

        async with anyio.create_task_group() as tg:
            for spec in self.settings.children:
                tg.start_soon(boot, spec)
        up = [c for c in self.children.values() if not c.dead]
        if not up:
            raise MCPError(-32603, "no backend started; see the container log")
        try:
            self.catalog.merged = self.catalog.build()
        except CatalogError as e:
            log("catalog_rejected", "error", session=self.tag, problems=e.problems)
            raise MCPError(-32603, "invalid catalog: " + "; ".join(e.problems)) from None
        return {
            "protocolVersion": version,
            "capabilities": _merged_capabilities(up),
            "serverInfo": {"name": "rebox-aggregator", "version": __version__},
            "instructions": self._instructions(),
        }

    def _instructions(self) -> str:
        """The configured text ([session] instructions) plus where this session keeps its state."""
        s = self.settings
        text = s.instructions
        if s.state_dir:
            ev = next((self.autosaver.arguments(c.autosave).get("path") for c in s.children if c.autosave), None)
            text += (
                f"\nState directory: {s.state_dir}. Session tag: {self.tag}."
                + (f" Evidence is saved to {ev} on close and periodically." if ev else "")
            )
        return text.strip()

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
                pass  # that request has just finished
        return await self._down.send_raw_request(method, params)

    def on_notification(self, child: Child, method: str, params: Mapping[str, Any] | None) -> None:
        """In receive order. Progress and cancellation are the dispatcher's (see _forward, on_request)."""
        if method in LIST_CHANGED:
            self.catalog.list_changed(child, LIST_CHANGED[method])
        elif method not in ("notifications/progress", "notifications/cancelled") and self.initialized:
            fl = self._related_for(child)
            self._notify_down(method, params, fl.id if fl else None)


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
