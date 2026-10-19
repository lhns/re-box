import os
import time

from rebox_aggregator import log as rlog
from rebox_aggregator.housekeeping import dir_size, prune_evidence, prune_rolled_logs, remove_partial_exports, sweep_tmp


def captured(fn, *a):
    events = []
    rlog.sink = events.append
    try:
        return fn(*a), events
    finally:
        rlog.sink = None


def test_dir_size_counts_files_and_skips_symlinks(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x").write_bytes(b"12345")
    (tmp_path / "y").write_bytes(b"123")
    os.symlink(tmp_path / "a", tmp_path / "link")
    assert dir_size(tmp_path) == (8, 2)
    assert dir_size(tmp_path / "missing") == (0, 0)


def test_sweep_tmp_removes_only_known_patterns(tmp_path):
    (tmp_path / "rea-ghidra-abc123").mkdir()
    (tmp_path / "rea-ghidra-abc123" / "target.bin").write_bytes(b"x" * 100)
    (tmp_path / "rea-other").write_bytes(b"12")
    locks = tmp_path / "rebox-attach-locks"
    locks.mkdir()
    (locks / "a.lock").write_text("")
    (locks / "keep.txt").write_text("")
    for uid in ("1000", "4321"):  # REA's TMPDIR, named after the service uid
        (tmp_path / f"rebox-rea-{uid}" / f"rea-ghidra-{uid}").mkdir(parents=True)
        (tmp_path / f"rebox-rea-{uid}" / f"rea-ghidra-{uid}" / "target.bin").write_bytes(b"x" * 10)
    (tmp_path / "rebox-rea-1000" / "other").write_text("")
    keep = [tmp_path / "rebox-e2e", tmp_path / "ghidra-mcp-rebox", tmp_path / "hsperfdata_x", tmp_path / "area-1"]
    for k in keep:
        k.mkdir()
    (tmp_path / "rebox-e2e" / "f").write_text("")
    (tmp_path / "rebox-e2e" / "rea-nested").mkdir()
    (res, freed), events = captured(sweep_tmp, str(tmp_path))
    assert res == 5 and freed == 122
    assert not (tmp_path / "rea-ghidra-abc123").exists() and not (tmp_path / "rea-other").exists()
    assert not list(tmp_path.glob("rebox-rea-*/rea-*")) and (tmp_path / "rebox-rea-1000" / "other").exists()
    assert not (locks / "a.lock").exists() and (locks / "keep.txt").exists()
    assert all(k.exists() for k in keep) and (tmp_path / "rebox-e2e" / "f").exists()
    assert (tmp_path / "rebox-e2e" / "rea-nested").exists()
    assert [(e["event"], e["removed"], e["bytes"]) for e in events] == [("tmp_swept", 5, 122)]


def test_sweep_tmp_does_not_follow_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("data")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    os.symlink(outside, tmp / "rea-link")
    (outside / "rea-inside").mkdir()
    os.symlink(outside, tmp / "rebox-rea-1000")  # not REA's TMPDIR: never entered
    (res, _), _ = captured(sweep_tmp, str(tmp))
    assert res == 1 and not os.path.lexists(tmp / "rea-link")
    assert (outside / "rea-inside").exists()
    assert (outside / "precious").read_text() == "data"


def test_sweep_tmp_quiet_when_nothing_to_do(tmp_path):
    (res, freed), events = captured(sweep_tmp, str(tmp_path))
    assert (res, freed) == (0, 0) and events == []
    assert sweep_tmp(str(tmp_path / "missing")) == (0, 0)


def test_partial_exports_removed_at_any_age(tmp_path):
    (tmp_path / ".rea-write-x1").mkdir()
    (tmp_path / ".rea-write-x1" / "part").write_text("{")
    (tmp_path / "s.json").write_text("{}")
    assert remove_partial_exports(str(tmp_path)) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]


def test_prune_evidence_deletes_by_age(tmp_path):
    old, new, other = tmp_path / "old.json", tmp_path / "new.json", tmp_path / "keep.txt"
    for p in (old, new, other):
        p.write_text("{}")
    now = time.time()
    os.utime(old, (now - 40 * 86400,) * 2)
    os.utime(other, (now - 40 * 86400,) * 2)
    (res, events) = captured(prune_evidence, str(tmp_path), 30)
    assert res == 1 and [e["event"] for e in events] == ["evidence_pruned"]
    assert not old.exists() and new.exists() and other.exists()
    os.utime(new, (now - 40 * 86400,) * 2)
    assert prune_evidence(str(tmp_path), 0) == 0 and new.exists()  # 0 keeps everything
    assert prune_evidence(str(tmp_path / "missing"), 30) == 0


def test_rolled_logs_pruned_by_age(tmp_path):
    d = tmp_path / "home" / ".config" / "ghidramcp" / "ghidramcp_12.1.4_PUBLIC"
    d2 = tmp_path / "home" / ".config" / "rebox-ghidra"
    d.mkdir(parents=True)
    d2.mkdir(parents=True)
    old = [d / "application.log-2026-01-01-1.log", d / "script.log-2026-01-01-1.log", d2 / "application.log-2026-01-01-2.log"]
    keep = [d / "application.log", d / "script.log", d / "application.log-2026-10-18-1.log", d / "tool.xml"]
    for f in old + keep:
        f.write_text("x")
    for f in old + [d / "application.log", d / "tool.xml"]:
        os.utime(f, (1, 1))
    assert prune_rolled_logs(str(tmp_path), 30) == 3
    assert not any(f.exists() for f in old) and all(f.exists() for f in keep)
    assert prune_rolled_logs(str(tmp_path / "none")) == 0


def test_rolled_logs_respect_the_age_limit(tmp_path):
    d = tmp_path / "home" / ".config" / "ghidramcp" / "v"
    d.mkdir(parents=True)
    f = d / "application.log-2026-01-01-1.log"
    f.write_text("x")
    now = time.time()
    os.utime(f, (now - 29 * 86400, now - 29 * 86400))
    assert prune_rolled_logs(str(tmp_path), 30) == 0
    os.utime(f, (now - 31 * 86400, now - 31 * 86400))
    assert prune_rolled_logs(str(tmp_path), 30) == 1
