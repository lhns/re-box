"""REA evidence bundles (JSON with `records` and `unknowns` arrays) kept under REBOX_STATE_DIR/rea/evidence."""

import json
import os

from .log import log


def drop_if_empty(path: object) -> bool:
    """Delete an exported bundle that holds nothing to restore. Anything unreadable or unexpected stays."""
    if not isinstance(path, str):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            bundle = json.load(f)
        if not (isinstance(bundle, dict) and bundle.get("records") == [] and bundle.get("unknowns") == []):
            return False
        os.unlink(path)
    except (OSError, ValueError):
        return False
    log("evidence_dropped", reason="empty")
    return True
