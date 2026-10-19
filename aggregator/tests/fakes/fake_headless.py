"""Stand-in for the ghidra-mcp headless server (run via a fake `java` on PATH)."""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[sys.argv.index("--port") + 1])
marker = os.environ["FAKE_MARKER"]
args = sys.argv
gpr = args[args.index("--project") + 1] if "--project" in args else ""
project = {"open": bool(gpr) and os.path.exists(gpr)}
with open(marker, "a") as f:
    f.write(f"start project={gpr} home={os.environ.get('HOME')} user.home={[a for a in args if a.startswith('-Duser.home=')]} "
            f"cache={os.environ.get('XDG_CACHE_HOME')}\n")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/check_connection":
            self.reply({"status": "ok"})
        else:
            self.reply({"has_project": project["open"]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        with open(marker, "a") as f:
            f.write(f"{self.path} {json.dumps(body)} env_token={'REBOX_TOKEN' in os.environ}\n")
        project["open"] = True
        self.reply({"success": True, "saved_count": 0, "errors": []})
        if self.path == "/exit_ghidra":
            threading.Thread(target=lambda: os._exit(0)).start()


print("fake headless starting", flush=True)
HTTPServer(("127.0.0.1", port), H).serve_forever()
