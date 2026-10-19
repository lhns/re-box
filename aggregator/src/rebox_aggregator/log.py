"""JSON-lines logging to stdout. Never pass tool arguments or results here."""

import json
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

import anyio
from anyio.abc import ByteReceiveStream
from anyio.streams.text import TextReceiveStream

# Tests replace this to capture events.
sink: Callable[[dict[str, Any]], None] | None = None


def log(event: str, level: str = "info", **fields: Any) -> None:
    record = {"ts": round(time.time(), 3), "level": level, "event": event, **fields}
    if sink is not None:
        sink(record)
        return
    sys.stdout.write(json.dumps(record, default=str) + "\n")
    sys.stdout.flush()


async def relay_lines(stream: ByteReceiveStream, emit: Callable[[str], None], max_line: int = 65536) -> None:
    """Hand each non-blank line of a process's output to `emit`, cut to 2000 characters (a line longer than
    `max_line` is split, so memory stays bounded)."""
    buf = ""
    try:
        async for chunk in TextReceiveStream(stream, errors="replace"):
            buf += chunk
            *lines, buf = buf.split("\n")
            if len(buf) > max_line:
                lines.append(buf)
                buf = ""
            for line in lines:
                if line.strip():
                    emit(line.rstrip()[:2000])
    except (anyio.BrokenResourceError, anyio.ClosedResourceError):
        pass


class _Library(logging.Handler):
    """The mcp library's own warnings as JSON lines (its messages name methods and ids, not arguments)."""

    def emit(self, record: logging.LogRecord) -> None:
        fields: dict[str, Any] = {"logger": record.name, "message": record.getMessage()[:500]}
        if record.exc_info and record.exc_info[1] is not None:
            fields["error"] = type(record.exc_info[1]).__name__
        log("library_log", "error" if record.levelno >= logging.ERROR else "warn", **fields)


_mcp = logging.getLogger("mcp")
_mcp.addHandler(_Library(logging.WARNING))
_mcp.propagate = False
