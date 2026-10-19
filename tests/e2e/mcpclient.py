"""Minimal streamable-HTTP MCP client for the e2e suite.

Raw on purpose: the tests need the session id, the exact notifications on each stream and
hand-made cancellations, which a full SDK client hides. One SDK-client test checks interop.
"""
from __future__ import annotations

import itertools
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx2 as httpx

PROTOCOL_VERSION = "2025-11-25"


class McpError(Exception):
    def __init__(self, error: dict[str, Any]):
        super().__init__(f"{error.get('code')}: {error.get('message')}")
        self.code = error.get("code")
        self.error = error


@dataclass
class Call:
    """Outcome of one request: the result (or error) and everything its stream carried."""

    method: str
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    notifications: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0

    def text(self) -> str:
        content = (self.result or {}).get("content") or []
        return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")


class Session:
    def __init__(self, url: str, token: str | None, *, timeout: float = 600.0):
        self.url = url
        self.headers = {"Accept": "application/json, text/event-stream"}
        if token is not None:
            self.headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))
        self.session_id: str | None = None
        self.ids = itertools.count(1)
        self.background: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._listener: threading.Thread | None = None
        self.init_result: dict[str, Any] | None = None

    # --- lifecycle
    def initialize(self) -> dict[str, Any]:
        call = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "rebox-e2e", "version": "1"},
        })
        self.init_result = call.result
        self.notify("notifications/initialized")
        return call.result

    def listen(self) -> None:
        """Open the standalone GET stream; its messages land in `self.background`."""

        def run() -> None:
            try:
                with self.http.stream("GET", self.url, headers=self._headers({"Accept": "text/event-stream"})) as r:
                    for msg in _sse_messages(r):
                        self.background.put(msg)
            except httpx.HTTPError:
                pass

        self._listener = threading.Thread(target=run, daemon=True)
        self._listener.start()

    def delete(self) -> int:
        r = self.http.delete(self.url, headers=self._headers())
        return r.status_code

    def close(self) -> None:
        if self.session_id:
            try:
                self.delete()
            except httpx.HTTPError:
                pass
        self.http.close()

    # --- messages
    def notify(self, method: str, params: dict[str, Any] | None = None) -> int:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        r = self.http.post(self.url, json=msg, headers=self._headers())
        return r.status_code

    def request(self, method: str, params: dict[str, Any] | None = None, *,
                on_message: Callable[[dict[str, Any]], None] | None = None,
                request_id: int | None = None) -> Call:
        rid = request_id if request_id is not None else next(self.ids)
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        call = Call(method)
        start = time.monotonic()
        with self.http.stream("POST", self.url, json=msg, headers=self._headers()) as r:
            if self.session_id is None and "mcp-session-id" in r.headers:
                self.session_id = r.headers["mcp-session-id"]
            ctype = r.headers.get("content-type", "")
            if ctype.startswith("application/json"):
                body = json.loads(r.read())
                if r.status_code >= 400 and "error" not in body:
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                self._settle(call, body)
            elif ctype.startswith("text/event-stream"):
                for m in _sse_messages(r):
                    if m.get("id") == rid and ("result" in m or "error" in m):
                        self._settle(call, m)
                        break
                    call.notifications.append(m)
                    if on_message:
                        on_message(m)
            else:
                r.read()
                raise httpx.HTTPStatusError(f"HTTP {r.status_code} {ctype}", request=r.request, response=r)
        call.seconds = time.monotonic() - start
        return call

    def tool(self, name: str, arguments: dict[str, Any] | None = None, **kw: Any) -> Call:
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        if "progress_token" in kw:
            params["_meta"] = {"progressToken": kw.pop("progress_token")}
        call = self.request("tools/call", params, **kw)
        if call.error:
            raise McpError(call.error)
        return call

    def tool_names(self) -> list[str]:
        call = self.request("tools/list", {})
        if call.error:
            raise McpError(call.error)
        return [t["name"] for t in call.result["tools"]]

    # --- helpers
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = dict(self.headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
            h["MCP-Protocol-Version"] = (self.init_result or {}).get("protocolVersion", PROTOCOL_VERSION)
        if extra:
            h.update(extra)
        return h

    @staticmethod
    def _settle(call: Call, msg: dict[str, Any]) -> None:
        call.result = msg.get("result")
        call.error = msg.get("error")


def _sse_messages(response: httpx.Response):
    data: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data:
                payload = "\n".join(data)
                data = []
                if payload.strip():
                    yield json.loads(payload)
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
