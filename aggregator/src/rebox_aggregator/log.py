"""JSON-lines logging to stdout. Never pass tool arguments or results here."""

import json
import sys
import time
from collections.abc import Callable
from typing import Any

# Tests replace this to capture events.
sink: Callable[[dict[str, Any]], None] | None = None


def log(event: str, level: str = "info", **fields: Any) -> None:
    record = {"ts": round(time.time(), 3), "level": level, "event": event, **fields}
    if sink is not None:
        sink(record)
        return
    sys.stdout.write(json.dumps(record, default=str) + "\n")
    sys.stdout.flush()
