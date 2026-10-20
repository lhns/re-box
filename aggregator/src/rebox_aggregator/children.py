"""One stdio MCP child: own process group, newline-delimited JSON-RPC (mcp's JSONRPCDispatcher does the
correlation, cancellation and progress), stderr relay, group kill."""

import math
import os
import signal
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

import anyio
from anyio.abc import Process, TaskGroup, TaskStatus
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp.shared.dispatcher import DispatchContext, ProgressFnT
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.message import SessionMessage
from mcp_types import CONNECTION_CLOSED, jsonrpc_message_adapter
from pydantic import ValidationError

from .catalog import ChildCatalog
from .config import ChildSpec
from .log import log, relay_lines

# REA's tools/list is one ~3 MB line.
MAX_LINE = 64 * 1024 * 1024
STDIN_WAIT = 2.0
KINDS = ("tools", "prompts", "resources")


class Handler(Protocol):
    async def on_request(self, child: "Child", ctx: DispatchContext, method: str, params: Mapping[str, Any] | None) -> dict: ...
    def on_notification(self, child: "Child", method: str, params: Mapping[str, Any] | None) -> None: ...
    async def on_exit(self, child: "Child") -> None: ...


async def _unused(*_: Any) -> None:
    """The intercept consumes every notification."""


class Child:
    def __init__(self, spec: ChildSpec, session: str, env: dict[str, str], grace: float, handler: Handler):
        self.spec, self.session, self.env, self.grace, self.handler = spec, session, env, grace, handler
        self.name = spec.name
        self.proc: Process | None = None
        self.pid: int | None = None
        self.returncode: int | None = None
        self.exited = anyio.Event()  # returncode is known
        self.dead = False  # its stdout ended: nothing more comes from it
        self.closing = False
        self.capabilities: dict = {}
        self.settled = anyio.Event()  # cleared while a catalog refresh holds back this child's responses
        self.settled.set()
        self._feed, feed = anyio.create_memory_object_stream[SessionMessage | Exception](math.inf)
        # Unbounded, so nothing ever waits on a child that does not read its stdin.
        self._out, self._out_recv = anyio.create_memory_object_stream[SessionMessage](math.inf)
        self._rpc = JSONRPCDispatcher(feed, self._out)
        self._reader = anyio.CancelScope()

    def gone_error(self) -> MCPError:
        code = "unknown" if self.returncode is None else self.returncode
        return MCPError(-32603, f"backend '{self.name}' is not running in this session (exited with code {code})")

    async def start(self, tg: TaskGroup) -> None:
        if self.closing:
            raise MCPError(-32603, "session is closing")
        self.proc = await anyio.open_process(
            list(self.spec.command), stdin=-1, stdout=-1, stderr=-1, env=self.env, start_new_session=True
        )
        self.pid = self.proc.pid
        if self.closing:  # close() ran while the process was being spawned
            self._signal_group(signal.SIGKILL)
            raise MCPError(-32603, "session is closing")
        tg.start_soon(self._read_stdout)
        tg.start_soon(relay_lines, self.proc.stderr, self._log_stderr)
        tg.start_soon(self._write_stdin)
        tg.start_soon(self._watch)
        await tg.start(self._run)

    async def open(self, tg: TaskGroup, params: dict) -> None:
        """Spawn it, then the handshake: initialize (with the client's params), notifications/initialized."""
        await self.start(tg)
        self.capabilities = (await self.request("initialize", params)).get("capabilities") or {}
        await self.notify("notifications/initialized")

    async def list_catalog(self, kinds: Iterable[str] = KINDS, base: ChildCatalog | None = None) -> ChildCatalog:
        """List the given kinds (all pages); the rest come from `base`, and kinds it does not advertise stay empty."""
        part = ChildCatalog(**vars(base)) if base else ChildCatalog()
        if "tools" in kinds and "tools" in self.capabilities:
            part.tools = await self._list("tools/list", "tools")
        if "prompts" in kinds and "prompts" in self.capabilities:
            part.prompts = await self._list("prompts/list", "prompts")
        if "resources" in kinds and "resources" in self.capabilities:
            part.resources = await self._list("resources/list", "resources")
            try:
                part.templates = await self._list("resources/templates/list", "resourceTemplates")
            except MCPError as e:
                if e.code != -32601:
                    raise
        return part

    async def _list(self, method: str, key: str) -> list[dict]:
        items: list[dict] = []
        cursor = None
        while True:
            r = await self.request(method, {"cursor": cursor} if cursor else None)
            items += r.get(key, [])
            cursor = r.get("nextCursor")
            if not cursor:
                return items

    # -- JSON-RPC ---------------------------------------------------------

    async def request(self, method: str, params: dict | None = None, *, on_progress: ProgressFnT | None = None) -> dict:
        """Send a request and wait for its result (no timeout; cold Ghidra starts take minutes). A caller
        that is cancelled makes the dispatcher send the child notifications/cancelled."""
        if self.dead:
            raise self.gone_error()
        try:
            return await self._rpc.send_raw_request(method, params, {"on_progress": on_progress} if on_progress else None)
        except MCPError as e:
            if e.code != CONNECTION_CLOSED or not (self.dead or self.closing):
                raise
        with anyio.move_on_after(STDIN_WAIT):
            await self.exited.wait()
        raise self.gone_error()

    async def call_tool(self, tool: str, arguments: Mapping[str, Any]) -> tuple[str, str | None]:
        """One of the child's own tools, called by the aggregator itself (autosave, on_ready):
        (ok | tool_error | rpc_error, the error text)."""
        try:
            result = await self.request("tools/call", {"name": tool, "arguments": dict(arguments)})
        except MCPError as e:
            return "rpc_error", e.message
        if result.get("isError"):
            return "tool_error", " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
        return "ok", None

    async def notify(self, method: str, params: dict | None = None) -> None:
        """Fire and forget: dropped once the child is gone."""
        await self._rpc.notify(method, params)

    async def _run(self, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        await self._rpc.run(self._on_request, _unused, self._intercept, task_status=task_status)
        await self.exited.wait()  # stdout ended; the group is being killed (_read_stdout, close)
        self.end_hold()
        log("child_exit", session=self.session, child=self.name, pid=self.pid, exit_code=self.returncode)
        await self.handler.on_exit(self)

    async def _on_request(self, ctx: DispatchContext, method: str, params: Mapping[str, Any] | None) -> dict:
        return await self.handler.on_request(self, ctx, method, params)

    def _intercept(self, method: str, params: Mapping[str, Any] | None) -> bool:
        """In receive order, so a list_changed holds back the responses that follow it."""
        self.handler.on_notification(self, method, params)
        return True

    # -- response hold (list_changed refresh) -----------------------------

    def begin_hold(self) -> None:
        if self.settled.is_set():
            self.settled = anyio.Event()

    def end_hold(self) -> None:
        self.settled.set()

    # -- pipes --------------------------------------------------------------

    async def _write_stdin(self) -> None:
        assert self.proc and self.proc.stdin
        try:
            async for item in self._out_recv:
                await self.proc.stdin.send(item.message.model_dump_json(by_alias=True, exclude_unset=True).encode() + b"\n")
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass
        finally:
            with anyio.CancelScope(shield=True):
                await self.proc.stdin.aclose()

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        buf = BufferedByteReceiveStream(self.proc.stdout)
        try:
            with self._reader:
                while True:
                    line = await buf.receive_until(b"\n", MAX_LINE)
                    if not line.strip():
                        continue
                    try:  # validation also drops hostile ids: JSON-RPC ids are str or int
                        msg = jsonrpc_message_adapter.validate_json(line, by_name=False)
                    except ValidationError:
                        log("child_bad_line", "warn", child=self.name, session=self.session)
                        continue
                    self._feed.send_nowait(SessionMessage(msg))
        except (anyio.IncompleteRead, anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass
        except anyio.DelimiterNotFound:
            log("child_line_too_long", "error", child=self.name, session=self.session)
        finally:
            self.dead = True  # before the feed closes, so the requests it fails see a dead child
            self._feed.close()
            if not self.closing:  # a child without stdout is gone; so is its group
                self._signal_group(signal.SIGKILL)

    def _log_stderr(self, line: str) -> None:
        log("child_stderr", session=self.session, child=self.name, line=line)

    async def _watch(self) -> None:
        """The child's exit, even while a grandchild still holds its stdout open."""
        assert self.proc
        self.returncode = await self.proc.wait()
        self.exited.set()
        if not self.closing:
            self._signal_group(signal.SIGKILL)
        await anyio.sleep(STDIN_WAIT)  # time to read what it wrote before exiting
        self._reader.cancel()

    # -- shutdown ---------------------------------------------------------

    def _signal_group(self, sig: int) -> None:
        if self.pid is None:
            return
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            pass

    def _group_alive(self) -> bool:
        try:
            os.killpg(self.pid, 0)  # type: ignore[arg-type]
        except ProcessLookupError:
            return False
        return True

    async def _wait_group_gone(self, timeout: float) -> bool:
        with anyio.move_on_after(timeout):
            while self._group_alive():
                await anyio.sleep(0.05)
        return not self._group_alive()

    async def close(self) -> None:
        """Close stdin, then SIGTERM, then SIGKILL the whole process group."""
        self.closing = True
        if self.proc is None:
            return
        with anyio.CancelScope(shield=True):
            self._out.close()  # the writer task drains the queue, then closes stdin
            if await self._wait_group_gone(STDIN_WAIT):
                return
            self._signal_group(signal.SIGTERM)
            if await self._wait_group_gone(self.grace):
                return
            self._signal_group(signal.SIGKILL)
            await self._wait_group_gone(2.0)


def kill_leftovers(session: str | None, instance: str, command: str | None = None) -> int:
    """SIGKILL every process whose environment carries this instance (and session, when given) and, when
    `command` is given, whose command line contains it."""
    wanted = {f"REBOX_INSTANCE={instance}".encode()}
    if session:
        wanted.add(f"REBOX_SESSION={session}".encode())
    killed = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            if command:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    if command.encode() not in f.read():
                        continue
            with open(f"/proc/{entry}/environ", "rb") as f:
                if wanted <= set(f.read().split(b"\0")):
                    os.kill(int(entry), signal.SIGKILL)
                    killed += 1
        except OSError:
            continue
    return killed
