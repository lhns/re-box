"""ASGI app: bearer auth, /healthz, and the /mcp session manager (cap, era rejection)."""

import contextlib
import hmac
import json
import os
import re
import signal
import sys
import uuid
from collections.abc import Callable

import anyio
import anyio.abc
import uvicorn
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from .config import ConfigError, Settings, harden, load_settings
from .log import log
from .session import Session

Health = Callable[[], tuple[bool, str, str]]  # (ok, reason, backend state)


def instance_id() -> str:
    """REBOX_INSTANCE when set and well-formed (the test runner sets it), else a random id."""
    given = os.environ.get("REBOX_INSTANCE", "")
    return given if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", given) else uuid.uuid4().hex


class NoBackend:
    """Stand-in for the supervisor's Ghidra lifecycle when the app runs on its own."""

    def want(self, wanted: bool) -> None: ...

    def ready(self) -> bool:
        return True


class App:
    def __init__(self, settings: Settings, *, instance: str | None = None, health: Health | None = None):
        self.settings = settings
        self.instance = instance or instance_id()
        self.health = health or (lambda: (True, "", ""))
        self.backend = NoBackend()  # the supervisor swaps in the Ghidra lifecycle: want(bool), ready()
        self.sessions: dict[str, Session] = {}
        self.closing = False
        self._tg: anyio.abc.TaskGroup | None = None
        self._token = settings.token.encode()

    async def shutdown(self) -> None:
        """Refuse new sessions and close all existing ones."""
        self.closing = True
        async with anyio.create_task_group() as tg:
            for s in list(self.sessions.values()):
                tg.start_soon(s.close, "shutdown")

    # -- ASGI -------------------------------------------------------------

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            return await self._lifespan(receive, send)
        if scope["type"] != "http":
            return
        path = scope["path"]
        if path == "/healthz":
            ok, reason, state = self.health()
            body = {"status": "ok"} if ok else {"status": "unavailable", "reason": reason}
            if state:
                body["ghidra"] = state
            return await _json(send, 200 if ok else 503, body)
        if path not in ("/mcp", "/mcp/"):
            return await _json(send, 404, {"error": "not found"})
        if not self._authorized(scope):
            return await _json(send, 401, {"error": "unauthorized"}, {"www-authenticate": "Bearer"})
        await self._mcp(scope, receive, send)

    async def _lifespan(self, receive, send) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                self._cm = anyio.create_task_group()
                self._tg = await self._cm.__aenter__()
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await self.shutdown()
                self._tg.cancel_scope.cancel()
                await self._cm.__aexit__(None, None, None)
                await send({"type": "lifespan.shutdown.complete"})
                return

    def _authorized(self, scope) -> bool:
        for name, value in scope["headers"]:
            if name == b"authorization":
                scheme, _, token = value.partition(b" ")
                return scheme.lower() == b"bearer" and hmac.compare_digest(token.strip(), self._token)
        return False

    async def _mcp(self, scope, receive, send) -> None:
        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope["headers"]}
        method = scope["method"]
        sid = headers.get("mcp-session-id")
        if sid:
            session = self.sessions.get(sid)
            if session is None:
                return await _rpc_error(send, 404, None, -32001, "Session not found")
            if method == "DELETE":
                await session.close("delete")
                return await _json(send, 200, {})
            if method == "POST":
                session.touch()
                body = await self._read_body(headers, receive, send)
                if body is None:
                    return
                receive = _replay(body, receive)
            if method == "GET":
                return await self._get_stream(session, scope, receive, send)
            return await session.transport.handle_request(scope, receive, send)
        if method != "POST":
            return await _rpc_error(send, 400, None, -32600, "Missing Mcp-Session-Id header")

        body = await self._read_body(headers, receive, send)
        if body is None:
            return
        try:
            msg = json.loads(body)
        except ValueError:
            return await _rpc_error(send, 400, None, -32700, "Parse error")
        requested = headers.get("mcp-protocol-version", "")
        is_init = isinstance(msg, dict) and msg.get("method") == "initialize" and "id" in msg
        if not is_init or (requested and requested not in HANDSHAKE_PROTOCOL_VERSIONS):
            rid = msg.get("id") if isinstance(msg, dict) else None
            return await _rpc_error(
                send, 400, rid, -32022,
                "Unsupported protocol version; this server speaks the initialize handshake",
                {"supported": list(HANDSHAKE_PROTOCOL_VERSIONS), "requested": requested},
            )
        while not self.closing and len(self.sessions) >= self.settings.max_sessions:
            victim = self._eviction_candidate()
            if victim is None:
                break
            log("session_evicted", "info", session=victim.tag, idle_s=round(victim.idle_for()), reason="session limit")
            await victim.close("evicted")  # saves its work first
        if self.closing or len(self.sessions) >= self.settings.max_sessions:
            n = len(self.sessions)
            reason = "shutting down" if self.closing else "session limit"
            log("session_refused", "warn", reason=reason)
            text = (
                "re-box is shutting down" if self.closing else
                f"re-box session limit reached ({n} of {self.settings.max_sessions} active); "
                "each session runs its own backends. Close an idle session or retry later."
            )
            return await _rpc_error(send, 503, msg["id"], -32000, text)
        await self._open(msg, body, scope, receive, send)

    async def _open(self, msg, body: bytes, scope, receive, send) -> None:
        assert self._tg is not None
        transport = StreamableHTTPServerTransport(mcp_session_id=uuid.uuid4().hex)
        session = Session(
            transport.mcp_session_id, transport, self.settings, self.instance, self._forget,  # type: ignore[arg-type]
            lambda: self.backend.ready(),
        )
        self.sessions[session.sid] = session
        self.backend.want(True)
        try:
            await self._tg.start(session.serve)
            await transport.handle_request(scope, _replay(body, receive), send)
        except BaseException:
            await session.close("init_aborted")
            raise
        if not session.initialized:
            await session.close("init_failed")

    async def _read_body(self, headers: dict[str, str], receive, send) -> bytes | None:
        """Read a POST body up to REBOX_MAX_BODY_BYTES; answer 413 and return None beyond that."""
        limit = self.settings.max_body
        declared = headers.get("content-length", "")
        chunks, size = [], 0
        too_big = declared.isdigit() and int(declared) > limit
        while not too_big:
            event = await receive()
            if event["type"] == "http.disconnect":
                return None
            chunks.append(event.get("body", b""))
            size += len(chunks[-1])
            too_big = size > limit
            if not event.get("more_body"):
                break
        if too_big:
            await _rpc_error(send, 413, None, -32600, f"Request body exceeds {limit} bytes")
            return None
        return b"".join(chunks)

    def _forget(self, session: Session) -> None:
        self.sessions.pop(session.sid, None)
        if not self.sessions:
            self.backend.want(False)

    def backend_became_ready(self) -> None:
        for s in list(self.sessions.values()):
            s.on_backend_ready()

    def _eviction_candidate(self) -> Session | None:
        """The longest-idle session with nothing in flight, if it has been idle long enough."""
        idle = [s for s in self.sessions.values() if s.initialized and not s.busy and s.idle_for() >= self.settings.evict_min_idle]
        return max(idle, key=lambda s: s.idle_for(), default=None)

    async def _get_stream(self, session: Session, scope, receive, send) -> None:
        """The client's standalone notification stream. Its presence tells an attended session from an orphan."""
        session.get_streams += 1
        log("get_stream_open", "debug", session=session.tag, open=session.get_streams)
        try:
            await session.transport.handle_request(scope, receive, send)
        finally:
            session.get_streams -= 1
            session.touch()  # the stream's end is activity: the orphan clock starts from here
            log("get_stream_closed", "debug", session=session.tag, open=session.get_streams)


def _replay(body: bytes, receive):
    """An ASGI receive that yields an already-read body once, then the real events."""
    sent = False

    async def replay():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


async def _json(send, status: int, body: dict, headers: dict[str, str] | None = None) -> None:
    raw = json.dumps(body).encode()
    hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
    hdrs += [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": raw})


async def _rpc_error(send, status: int, rid, code: int, message: str, data=None) -> None:
    err = {"code": code, "message": message, **({"data": data} if data is not None else {})}
    await _json(send, status, {"jsonrpc": "2.0", "id": rid, "error": err})


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        # The supervisor owns SIGTERM/SIGINT; uvicorn would re-raise them after shutdown.
        yield


def make_server(app: App) -> uvicorn.Server:
    s = app.settings
    config = uvicorn.Config(
        app, host=s.bind, port=s.port, log_level="warning", access_log=False, lifespan="on",
        ssl_certfile=s.tls_cert, ssl_keyfile=s.tls_key, timeout_graceful_shutdown=3,
    )
    return _Server(config)


async def serve(app: App, *, started: Callable[[uvicorn.Server], None] | None = None) -> None:
    """Run uvicorn until SIGTERM/SIGINT; sessions are closed by the app's lifespan shutdown."""
    server = make_server(app)
    async with anyio.create_task_group() as tg:
        async def watch_signals():
            with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as sigs:
                async for _ in sigs:
                    await app.shutdown()
                    server.should_exit = True
                    return

        tg.start_soon(watch_signals)
        if started:
            started(server)
        await server.serve()
        tg.cancel_scope.cancel()


def main() -> None:
    import argparse

    argparse.ArgumentParser(
        prog="rebox-aggregator", description="Per-session MCP aggregator; configured through REBOX_* env."
    ).parse_args()
    harden()
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"rebox-aggregator: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    anyio.run(serve, App(settings))
