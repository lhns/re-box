"""Raw JSON-RPC stdio MCP server (no SDK) for the aggregator tests.

Env: FAKE_EXTRA_TOOLS=a,b adds no-op tools; FAKE_BIG=N adds a tool whose description is N bytes;
FAKE_COLD=1 lists only connect_instance/get_connects until connect_instance is called (a bridge without its backend);
FAKE_SAVE_SLEEP=N delays the `save` tool (it writes its `path` argument); FAKE_FAIL_INIT=1 exits during initialize; FAKE_HANG_INIT=1 never answers it.
"""

import asyncio
import json
import os
import subprocess
import sys

SERVER_NAME = sys.argv[1] if len(sys.argv) > 1 else "fake"
state: dict[str, str] = {}
cancelled: list = []
slow_ids: list = []
replies: list = []
cancel_events: dict = {}
extra_tools: list[str] = []
saves: list[str] = []
connects: list = []


def tool(name, props=None):
    return {"name": name, "description": f"{name} of {SERVER_NAME}",
            "inputSchema": {"type": "object", "properties": props or {}}}


def tools():
    if os.environ.get("FAKE_COLD") and not connects:  # a bridge whose backend is not up: static tools only
        return [tool("connect_instance"), tool("get_connects")]
    names = ["echo", "pid", "env", "set_state", "get_state", "slow", "get_cancelled", "progress",
             "add_tool", "crash", "spawn", "structured", "fail", "ask_roots", "bad_msgs", "save", "sleep", "connect_instance", "get_connects"]
    out = [tool(n) for n in names + os.environ.get("FAKE_EXTRA_TOOLS", "").split(",") + extra_tools if n]
    if os.environ.get("FAKE_BIG"):
        t = tool("big")
        t["description"] = "x" * int(os.environ["FAKE_BIG"])
        out.append(t)
    return out


def text(s, **extra):
    return {"content": [{"type": "text", "text": s}], **extra}


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


async def call(rid, name, args, meta):
    if name in extra_tools:
        return text(f"dyn:{name}")
    if name == "echo":
        return text(args.get("text", ""))
    if name == "save":
        saves.append(args["path"])
        await asyncio.sleep(float(os.environ.get("FAKE_SAVE_SLEEP", "0")))
        if os.environ.get("FAKE_SAVE_FAIL"):
            return text("disk full", isError=True)
        with open(args["path"], "w") as f:
            f.write(json.dumps({"records": [], "unknowns": []} if os.environ.get("FAKE_SAVE_EMPTY") else {"saves": len(saves)}))
        return text("saved")
    if name == "sleep":
        await asyncio.sleep(float(args.get("seconds", 1)))
        if "then_progress" in args:  # a late, small real progress value after quiet time
            send({"jsonrpc": "2.0", "method": "notifications/progress",
                  "params": {"progressToken": meta["progressToken"], "progress": args["then_progress"]}})
        return text("slept")
    if name == "connect_instance":
        connects.append(args)
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        return text("connected")
    if name == "get_connects":
        return text(json.dumps(connects))
    if name == "pid":
        return text(str(os.getpid()))
    if name == "env":
        return text(json.dumps(dict(os.environ)))
    if name == "set_state":
        state["v"] = args["value"]
        return text("ok")
    if name == "get_state":
        return text(state.get("v", ""))
    if name == "slow":
        slow_ids.append(rid)
        ev = cancel_events[rid] = asyncio.Event()
        await ev.wait()
        return None  # cancelled: no response
    if name == "get_cancelled":
        return text(json.dumps({"cancelled": cancelled, "slow_ids": slow_ids, "replies": replies}))
    if name == "progress":
        for i in range(3):
            send({"jsonrpc": "2.0", "method": "notifications/progress",
                  "params": {"progressToken": meta["progressToken"], "progress": i + 1, "total": 3}})
        return text("done")
    if name == "add_tool":
        extra_tools.append(args["name"])
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        return text("added")
    if name == "crash":
        os._exit(3)
    if name == "spawn":
        p = subprocess.Popen(["sleep", "300"])
        return text(str(p.pid))
    if name == "structured":
        return text("s", structuredContent={"n": None, "f": 1.5, "i": 2**53, "nested": {"a": [None, 0, ""]}, "u": "é☃"})
    if name == "fail":
        return text("boom", isError=True)
    if name == "bad_msgs":  # hostile ids and tokens
        send({"jsonrpc": "2.0", "id": [1], "method": "roots/list"})
        send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": [1], "progress": 1}})
        send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": [1]}})
        send({"jsonrpc": "2.0", "id": [1], "result": {}})
        return text("sent")
    if name == "ask_roots":
        send({"jsonrpc": "2.0", "id": 900, "method": "roots/list"})
        return text("asked")
    raise KeyError(name)


async def handle(msg):
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None:
        replies.append(msg)
        return
    if method == "notifications/cancelled":
        cancelled.append(params["requestId"])
        ev = cancel_events.get(params["requestId"])
        if ev:
            ev.set()
        return
    if rid is None:
        return
    if method == "initialize":
        if os.environ.get("FAKE_HANG_INIT"):
            await asyncio.sleep(1e6)
        if os.environ.get("FAKE_FAIL_INIT"):
            os._exit(1)
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {"listChanged": True}, "prompts": {}, "logging": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools()}})
    elif method == "prompts/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"prompts": [{"name": "greet", "description": SERVER_NAME}]}})
    elif method == "prompts/get":
        send({"jsonrpc": "2.0", "id": rid, "result": {"messages": [
            {"role": "user", "content": {"type": "text", "text": f"hello from {SERVER_NAME}: {params['name']}"}}]}})
    elif method == "logging/setLevel":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "tools/call":
        try:
            result = await call(rid, params["name"], params.get("arguments") or {}, params.get("_meta") or {})
        except KeyError:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {params['name']}"}})
            return
        if result is not None:
            send({"jsonrpc": "2.0", "id": rid, "result": result})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}})


async def main():
    loop = asyncio.get_running_loop()
    tasks = set()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        t = asyncio.create_task(handle(json.loads(line)))
        tasks.add(t)
        t.add_done_callback(tasks.discard)


asyncio.run(main())
