"""Newline-delimited JSON-RPC MCP client over a subprocess's stdio, for `rea mcp` with a chosen env.

The Ghidra profile settings (REA_GHIDRA_LANGUAGE_ID, ..._SEED_FILE, ...) are read when the REA
process starts, so the profile tests spawn their own REA instead of going through the aggregator.
Same call surface as mcpclient.Session where the tests need it (`tool`, `close`).
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import threading
from typing import Any

from helpers import ghidra_token
from mcpclient import PROTOCOL_VERSION, Call, McpError


class StdioSession:
    def __init__(self, command: list[str], env: dict[str, str]):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, text=True, bufsize=1)
        self.ids = itertools.count(1)
        self.stderr_lines: list[str] = []
        self._replies: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if line.startswith("{"):
                try:
                    self._replies.put(json.loads(line))
                except json.JSONDecodeError:
                    pass
        self._replies.put(None)

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip())

    def _send(self, msg: dict[str, Any]) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def initialize(self) -> dict[str, Any]:
        call = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                           "clientInfo": {"name": "rebox-e2e-stdio", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return call.result

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 900.0) -> Call:
        rid = next(self.ids)
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        call = Call(method)
        while True:
            try:
                reply = self._replies.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"no reply to {method} in {timeout} s; stderr: {self.stderr_lines[-20:]}")
            if reply is None:
                raise McpError({"code": -1, "message": f"rea exited; stderr: {self.stderr_lines[-20:]}"})
            if reply.get("id") == rid and ("result" in reply or "error" in reply):
                call.result, call.error = reply.get("result"), reply.get("error")
                return call
            call.notifications.append(reply)

    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> Call:
        call = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if call.error:
            raise McpError(call.error)
        return call

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait()


def start_rea(state_dir, extra_env: dict[str, str]) -> StdioSession:
    """`rea mcp` as the aggregator would start it, plus `extra_env`.

    Keeps the container env (REA's tool paths, REBOX_INSTANCE so the tests can find the process
    tree) and moves HOME and the XDG dirs into the state dir like the aggregator's children.
    """
    home = state_dir / "home"
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("REBOX_") and k not in ("REBOX_INSTANCE", "REBOX_STATE_DIR")]:
        del env[key]
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(state_dir / "cache"),
        "REBOX_REA_ATTACH_HELPER": "/opt/rebox/rea-attach/helper.mjs",
        # aggregator.toml's TMPDIR = "{REBOX_REA_TMP}": a ghidra-mcp file root, so the helper can
        # import REA's snapshot from there.
        "TMPDIR": os.environ["REBOX_REA_TMP"],
        **extra_env,
    })
    # The supervisor's token for ghidra-mcp reaches REA's children by inheritance; REBOX_* was stripped above.
    token = ghidra_token()
    if token:
        env["GHIDRA_MCP_AUTH_TOKEN"] = token
    s = StdioSession(["/usr/local/bin/rea", "mcp"], env)
    s.initialize()
    return s
