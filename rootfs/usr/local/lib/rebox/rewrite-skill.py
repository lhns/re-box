#!/usr/bin/env python3
"""Copy REA's bundled skill with tool and prompt names prefixed for the re-box aggregator.

Usage: rewrite-skill.py <upstream skills dir> <output dir> <prefix>

Names come from the installed REA itself (`rea mcp`, tools/list and prompts/list), so a REA
upgrade cannot silently leave a name unrewritten: the run fails if any rewritten file still
mentions an unprefixed name.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

NOTE = """
> **re-box:** REA runs inside the re-box container, behind its MCP aggregator. Every REA tool
> and prompt is prefixed `{prefix}` (for example `{prefix}open_binary`). Paths must be valid
> inside the container; re-box mounts the work directory at the same absolute path as on the
> client side. Do not run `npx rea-agents setup` or `doctor` on the client: the server, its
> engines and their configuration live in the container (see re-box docs/CLIENTS.md).
"""


def rea_names() -> list[str]:
    proc = subprocess.Popen(["rea", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)

    def call(rid: int, method: str, params: dict) -> dict:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        while True:
            msg = json.loads(proc.stdout.readline())
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]

    try:
        call(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                               "clientInfo": {"name": "rebox-skill-rewrite", "version": "1"}})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        names, rid, cursor = [], 2, None
        for method, key in (("tools/list", "tools"), ("prompts/list", "prompts")):
            cursor = None
            while True:
                result = call(rid, method, {"cursor": cursor} if cursor else {})
                rid += 1
                names += [item["name"] for item in result[key]]
                cursor = result.get("nextCursor")
                if not cursor:
                    break
        return names
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def main() -> int:
    src, dst, prefix = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    names = rea_names()
    # Whole identifiers only, longest first so `binary_overview` never matches inside a longer name.
    pattern = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True))
                         + r")(?![A-Za-z0-9_])")
    shutil.copytree(src, dst)
    rewritten = 0
    for path in sorted(dst.rglob("*.md")):
        text = path.read_text()
        new, count = pattern.subn(lambda m: prefix + m.group(1), text)
        if path.name == "SKILL.md":
            # After the front matter, so the skill's name/description stay parseable.
            head, sep, body = new.partition("\n---\n")
            new = head + sep + NOTE.format(prefix=prefix) + body
        path.write_text(new)
        rewritten += count
    # Coverage check: no name may survive unprefixed.
    leftover = re.compile(r"(?<![A-Za-z0-9_])(?<!" + re.escape(prefix) + r")("
                          + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9_])")
    for path in dst.rglob("*.md"):
        for m in leftover.finditer(path.read_text()):
            print(f"unprefixed REA name {m.group(1)!r} in {path}", file=sys.stderr)
            return 1
    print(f"rewrote {rewritten} references to {len(names)} REA tool/prompt names into {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
