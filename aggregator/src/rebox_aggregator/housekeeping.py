"""Cleanup of leftovers that nothing else removes: temp dirs of killed processes, half-written exports,
old evidence bundles and rolled Ghidra logs, plus the project size (logged at each Ghidra start)."""

import fnmatch
import os
import shutil
import time
from collections.abc import Iterable
from pathlib import Path

import anyio

from .log import log

PRUNE_INTERVAL = 86400.0
LOG_RETENTION_DAYS = 30  # rolled Ghidra logs: Ghidra rolls at 50 MB and never deletes earlier days' files
ROLLED_LOGS = ("application.log-*.log", "script.log-*.log")
LOG_HOMES = ("ghidramcp*", "rebox-ghidra*")  # under $STATE/home/.config


def dir_size(path: str | Path) -> tuple[int, int]:
    """(bytes, files) under path; symlinks are not followed and unreadable entries are skipped."""
    total = files = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files


def _remove(entry: os.DirEntry) -> int:
    """Remove a dir tree, file or symlink itself (never a link's target); returns the bytes freed."""
    if entry.is_dir(follow_symlinks=False):
        size = dir_size(entry.path)[0]
        shutil.rmtree(entry.path, ignore_errors=True)
        return 0 if os.path.lexists(entry.path) else size
    size = entry.stat(follow_symlinks=False).st_size
    os.unlink(entry.path)
    return size


def _entries(path: str) -> list[os.DirEntry]:
    try:
        return list(os.scandir(path))
    except OSError:
        return []


def sweep_tmp(root: str) -> tuple[int, int]:
    """Remove what killed processes left in the temp dir: REA's rea-* runtime dirs, also inside REA's
    TMPDIRs (rebox-rea-<uid>, any uid: the name follows PUID), and the attach helper's lock files.
    Only these patterns; run before any session exists. Returns (removed, bytes)."""
    removed = freed = 0
    entries = _entries(root)
    todo = [e for e in entries if fnmatch.fnmatchcase(e.name, "rea-*")]
    for e in entries:
        if not e.is_dir(follow_symlinks=False):
            continue
        if fnmatch.fnmatchcase(e.name, "rebox-rea-*"):
            todo += [x for x in _entries(e.path) if fnmatch.fnmatchcase(x.name, "rea-*")]
        elif e.name == "rebox-attach-locks":
            todo += [x for x in _entries(e.path) if x.name.endswith(".lock")]
    for e in todo:
        try:
            freed += _remove(e)
            removed += 1
        except OSError:
            continue
    if removed:
        log("tmp_swept", removed=removed, bytes=freed)
    return removed, freed


def remove_partial_exports(evidence_dir: str) -> int:
    """REA writes an export through a `.rea-write-*` temp dir; a kill mid-export leaves it behind."""
    removed = 0
    try:
        entries = list(os.scandir(evidence_dir))
    except OSError:
        return 0
    for e in entries:
        if e.name.startswith(".rea-write-"):
            try:
                _remove(e)
                removed += 1
            except OSError:
                continue
    if removed:
        log("evidence_partial_removed", removed=removed)
    return removed


def _remove_older(files: Iterable[Path], days: float, now: float | None) -> int:
    """Unlink the regular files not modified for `days` days; returns how many."""
    cutoff = (time.time() if now is None else now) - days * 86400
    removed = 0
    for f in files:
        try:
            if f.is_file() and not f.is_symlink() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def prune_evidence(directory: str, days: float, now: float | None = None) -> int:
    """Delete *.json evidence bundles not modified for `days` days (0: keep everything)."""
    if days <= 0:
        return 0
    removed = _remove_older(Path(directory).glob("*.json"), days, now)
    if removed:
        log("evidence_pruned", removed=removed, retention_days=days)
    return removed


def prune_rolled_logs(state_dir: str, days: float = LOG_RETENTION_DAYS, now: float | None = None) -> int:
    """Delete rolled Ghidra logs (application.log-DATE-N.log, script.log-...) not modified for `days` days."""
    base = Path(state_dir) / "home" / ".config"
    files = (f for home in LOG_HOMES for pattern in ROLLED_LOGS for f in base.glob(f"{home}/**/{pattern}"))
    removed = _remove_older(files, days, now)
    if removed:
        log("ghidra_logs_pruned", removed=removed, retention_days=days)
    return removed


def _housekeep(state_dir: Path, retention_days: float, first: bool) -> None:
    evidence = str(state_dir / "rea" / "evidence")
    if first:
        remove_partial_exports(evidence)
    prune_evidence(evidence, retention_days)
    prune_rolled_logs(str(state_dir))


async def housekeeping_loop(state_dir: Path, evidence_retention_days: float) -> None:
    """At start and then daily: drop old REA evidence bundles (when a retention is set), half-written
    exports (at start only) and old rolled Ghidra logs."""
    first = True
    while True:
        await anyio.to_thread.run_sync(_housekeep, state_dir, evidence_retention_days, first)
        first = False
        await anyio.sleep(PRUNE_INTERVAL)
