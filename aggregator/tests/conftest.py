import asyncio
import contextlib
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from rebox_aggregator import log as rlog
from rebox_aggregator.app import App, make_server
from rebox_aggregator.config import ChildSpec, Settings

FAKE = str(Path(__file__).parent / "fakes" / "fake_server.py")
TOKEN = "s3cret-token"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_spec(name: str, prefix: str, **env: str) -> ChildSpec:
    return ChildSpec(name, prefix, (sys.executable, FAKE, name), env)


class Running:
    def __init__(self, settings: Settings, app: App, server, thread, events: list, loop):
        self.settings, self.app, self.server, self.thread, self.events = settings, app, server, thread, events
        self.loop = loop
        self.url = f"http://127.0.0.1:{settings.port}/mcp"
        self.base = f"http://127.0.0.1:{settings.port}"

    def headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}", **extra}

    def client(self, **kw) -> Client:
        http = httpx2.AsyncClient(headers=self.headers(), timeout=60)
        return Client(streamable_http_client(self.url, http_client=http), **kw)

    def raw(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(headers=self.headers(), timeout=60)

    def logged(self, event: str) -> list[dict]:
        return [e for e in self.events if e["event"] == event]

    def on_loop(self, fn) -> None:
        """Run fn on the server's event loop (app state is not thread-safe)."""
        self.loop.call_soon_threadsafe(fn)


class FakeBackend:
    """Stands in for the supervisor's Ghidra lifecycle: records want() calls, ready is a flag."""

    def __init__(self, ready: bool = True):
        self.wants: list[bool] = []
        self.is_ready = ready

    def want(self, wanted: bool) -> None:
        self.wants.append(wanted)

    def ready(self) -> bool:
        return self.is_ready


@contextlib.contextmanager
def run_app(children=None, **overrides):
    events: list = []
    rlog.sink = events.append
    os.environ["REBOX_TOKEN"] = TOKEN  # must never reach a child
    settings = Settings(
        token=TOKEN,
        children=tuple(children or [fake_spec("a", "a_"), fake_spec("b", "b_")]),
        port=free_port(),
        bind="127.0.0.1",
        **{"max_sessions": 4, "idle_timeout": 600.0, "child_grace": 2.0, **overrides},
    )
    app = App(settings, instance="testinstance")
    server = make_server(app)
    loops: list = []

    def serve() -> None:
        loop = asyncio.new_event_loop()
        loops.append(loop)
        loop.run_until_complete(server.serve())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{settings.port}/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.05)
    try:
        yield Running(settings, app, server, thread, events, loops[0])
    finally:
        server.should_exit = True
        thread.join(30)
        rlog.sink = None
        os.environ.pop("REBOX_TOKEN", None)


@pytest.fixture
def running():
    with run_app() as r:
        yield r


def text_of(result) -> str:
    return result.content[0].text


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[1].split()[0] != "Z"
    except OSError:
        return False


async def wait_until(pred, timeout=15.0):
    import anyio

    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await anyio.sleep(0.1)
    return pred()


def sse_messages(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        if line.startswith("data:") and line[5:].strip():
            out.append(json.loads(line[5:]))
    return out


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
RAW_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def raw_init(http: httpx2.AsyncClient, url: str) -> tuple[str, dict]:
    r = await http.post(url, json=INIT, headers=RAW_HEADERS)
    assert r.status_code == 200, r.text
    sid = r.headers["mcp-session-id"]
    await http.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={**RAW_HEADERS, "mcp-session-id": sid})
    return sid, sse_messages(r.text)[0]


async def raw_call(http, url, sid, method, params, rid=2) -> list[dict]:
    r = await http.post(url, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                        headers={**RAW_HEADERS, "mcp-session-id": sid, "mcp-protocol-version": "2025-11-25"})
    return sse_messages(r.text)
