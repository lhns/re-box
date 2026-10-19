"""REA evidence bundles (JSON with `records` and `unknowns` arrays) kept under REBOX_STATE_DIR/rea/evidence."""

import json
import os
import time

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


def prune_old(directory: str, days: float, now: float | None = None) -> int:
    """Delete *.json bundles not modified for `days` days (0: keep everything). Returns the number deleted."""
    if days <= 0:
        return 0
    cutoff = (time.time() if now is None else now) - days * 86400
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    if removed:
        log("evidence_pruned", removed=removed, retention_days=days)
    return removed
