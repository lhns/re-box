"""One stdio MCP child: own process group, newline-delimited JSON-RPC, stderr relay, group kill."""

import itertools
import json
import os
import signal
from collections.abc import Callable
from typing import Any, Protocol

import anyio
from anyio.abc import Process, TaskGroup
from anyio.streams.buffered import BufferedByteReceiveStream
from anyio.streams.text import TextReceiveStream

from .config import ChildSpec
from .log import log

# REA's tools/list is one ~3 MB line.
MAX_LINE = 256 * 1024 * 1024
STDIN_WAIT = 2.0


class ChildError(Exception):
    """A JSON-RPC error from a child, or the child being gone."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class Handler(Protocol):
    async def on_request(self, child: "Child", msg: dict) -> None: ...
    async def on_notification(self, child: "Child", msg: dict) -> None: ...
    async def on_exit(self, child: "Child") -> None: ...


class _Pending:
    def __init__(self) -> None:
        self.event = anyio.Event()
        self.msg: dict | None = None


class Child:
    def __init__(self, spec: ChildSpec, session: str, env: dict[str, str], grace: float, handler: Handler):
        self.spec, self.session, self.env, self.grace, self.handler = spec, session, env, grace, handler
        self.name = spec.name
        self.proc: Process | None = None
        self.pid: int | None = None
        self.returncode: int | None = None
        self.dead = False
        self.capabilities: dict = {}
        self.settled = anyio.Event()  # cleared while a catalog refresh holds this child's responses
        self.settled.set()
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._bypass: set[int] = set()
        self._holding = False
        self._held: list[dict] = []
        self._write_lock = anyio.Lock()

    def gone_error(self) -> ChildError:
        code = "unknown" if self.returncode is None else self.returncode
        return ChildError(-32603, f"backend '{self.name}' is not running in this session (exited with code {code})")

    async def start(self, tg: TaskGroup) -> None:
        self.proc = await anyio.open_process(
            list(self.spec.command), stdin=-1, stdout=-1, stderr=-1, env=self.env, start_new_session=True
        )
        self.pid = self.proc.pid
        tg.start_soon(self._read_stdout)
        tg.start_soon(self._read_stderr)

    # -- outgoing ---------------------------------------------------------

    async def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        async with self._write_lock:
            if self.dead:
                raise self.gone_error()
            try:
                await self.proc.stdin.send(data)
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                raise self.gone_error() from None

    async def request(
        self,
        method: str,
        params: dict | None = None,
        *,
        on_id: Callable[[int], None] | None = None,
        bypass: bool = False,
    ) -> dict:
        """Send a request and wait for its result (no timeout; cold Ghidra starts take minutes)."""
        if self.dead:
            raise self.gone_error()
        rid = next(self._ids)
        pending = self._pending[rid] = _Pending()
        if bypass:
            self._bypass.add(rid)
        if on_id:
            on_id(rid)
        try:
            msg: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                msg["params"] = params
            await self._send(msg)
            await pending.event.wait()
        finally:
            self._pending.pop(rid, None)
            self._bypass.discard(rid)
        reply = pending.msg
        if reply is None:
            raise self.gone_error()
        if "error" in reply:
            e = reply["error"]
            raise ChildError(e.get("code", -32603), e.get("message", ""), e.get("data"))
        return reply.get("result", {})

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def respond(self, rid: Any, result: dict | None = None, error: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "id": rid}
        msg.update({"error": error} if error is not None else {"result": result or {}})
        await self._send(msg)

    # -- response hold (list_changed refresh) -----------------------------

    def begin_hold(self) -> None:
        if not self._holding:
            self._holding = True
            self.settled = anyio.Event()

    def end_hold(self) -> None:
        self._holding = False
        held, self._held = self._held, []
        for msg in held:
            self._resolve(msg)
        self.settled.set()

    def _resolve(self, msg: dict) -> None:
        pending = self._pending.get(msg.get("id"))
        if pending:
            pending.msg = msg
            pending.event.set()

    # -- incoming ---------------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        buf = BufferedByteReceiveStream(self.proc.stdout)
        try:
            while True:
                line = await buf.receive_until(b"\n", MAX_LINE)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    log("child_bad_line", "warn", child=self.name, session=self.session)
                    continue
                if isinstance(msg, dict):
                    await self._dispatch(msg)
        except (anyio.IncompleteRead, anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass
        except anyio.DelimiterNotFound:
            log("child_line_too_long", "error", child=self.name, session=self.session)
            self._signal_group(signal.SIGKILL)
        finally:
            with anyio.CancelScope(shield=True):
                await self._exited()

    async def _dispatch(self, msg: dict) -> None:
        if "method" in msg:
            if "id" in msg:
                await self.handler.on_request(self, msg)
            else:
                await self.handler.on_notification(self, msg)
        elif self._holding and msg.get("id") not in self._bypass:
            self._held.append(msg)
        else:
            self._resolve(msg)

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        buf = ""
        try:
            async for chunk in TextReceiveStream(self.proc.stderr, errors="replace"):
                buf += chunk
                *lines, buf = buf.split("\n")
                if len(buf) > 65536:
                    lines.append(buf)
                    buf = ""
                for line in lines:
                    if line.strip():
                        log("child_stderr", session=self.session, child=self.name, line=line.rstrip()[:2000])
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass

    async def _exited(self) -> None:
        if self.dead:
            return
        self.dead = True
        assert self.proc
        self.returncode = await self.proc.wait()
        self.end_hold()
        for p in self._pending.values():
            p.event.set()
        log("child_exit", session=self.session, child=self.name, pid=self.pid, exit_code=self.returncode)
        await self.handler.on_exit(self)

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
        if self.proc is None:
            return
        with anyio.CancelScope(shield=True):
            try:
                await self.proc.stdin.aclose()  # type: ignore[union-attr]
            except Exception:
                pass
            if await self._wait_group_gone(STDIN_WAIT):
                return
            self._signal_group(signal.SIGTERM)
            if await self._wait_group_gone(self.grace):
                return
            self._signal_group(signal.SIGKILL)
            await self._wait_group_gone(2.0)


def kill_leftovers(session: str | None, instance: str) -> int:
    """SIGKILL any process whose environment carries this instance (and session, when given)."""
    wanted = {f"REBOX_INSTANCE={instance}".encode()}
    if session:
        wanted.add(f"REBOX_SESSION={session}".encode())
    killed = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open(f"/proc/{entry}/environ", "rb") as f:
                if wanted <= set(f.read().split(b"\0")):
                    os.kill(int(entry), signal.SIGKILL)
                    killed += 1
        except OSError:
            continue
    return killed
