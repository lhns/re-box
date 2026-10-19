import json
import os
import sys
import time

import pytest

from conftest import FAKE, raw_init, run_app
from rebox_aggregator.config import Autosave, ChildSpec
from rebox_aggregator.evidence import drop_if_empty, prune_old

pytestmark = pytest.mark.anyio


def write(path, body):
    path.write_text(json.dumps(body) if not isinstance(body, str) else body)
    return str(path)


def test_empty_bundle_is_dropped(tmp_path):
    p = write(tmp_path / "e.json", {"records": [], "unknowns": [], "artifacts": []})
    assert drop_if_empty(p) and not os.path.exists(p)


@pytest.mark.parametrize("body", [
    {"records": [{"evidence_id": "x"}], "unknowns": []},
    {"records": [], "unknowns": [{"unknown_id": "u"}]},
    {"records": []},
    [],
    "not json",
])
def test_bundle_with_content_or_unexpected_shape_is_kept(tmp_path, body):
    p = write(tmp_path / "e.json", body)
    assert not drop_if_empty(p) and os.path.exists(p)


def test_drop_ignores_missing_file_and_bad_path(tmp_path):
    assert not drop_if_empty(str(tmp_path / "none.json")) and not drop_if_empty(None)


def test_prune_old_deletes_by_age(tmp_path):
    old, new, other = tmp_path / "old.json", tmp_path / "new.json", tmp_path / "keep.txt"
    for p in (old, new, other):
        p.write_text("{}")
    now = time.time()
    os.utime(old, (now - 40 * 86400,) * 2)
    os.utime(other, (now - 40 * 86400,) * 2)
    assert prune_old(str(tmp_path), 30) == 1
    assert not old.exists() and new.exists() and other.exists()
    os.utime(new, (now - 40 * 86400,) * 2)
    assert prune_old(str(tmp_path), 0) == 0 and new.exists()  # 0 keeps everything
    assert prune_old(str(tmp_path / "missing"), 30) == 0


async def test_session_close_drops_an_empty_export(tmp_path):
    # The fake's `save` writes {"saves": n}, i.e. not an empty bundle: kept. FAKE_SAVE_EMPTY makes it one.
    spec = ChildSpec("a", "a_", (sys.executable, FAKE, "a"), {"FAKE_SAVE_EMPTY": "1"},
                     Autosave("save", {"path": "{state_dir}/ev/{session}.json"}, prune_empty=True))
    kept = ChildSpec("a", "a_", (sys.executable, FAKE, "a"), {}, Autosave("save", {"path": "{state_dir}/ev/{session}.json"}, prune_empty=True))
    for s, expect_file in ((spec, False), (kept, True)):
        with run_app([s], state_dir=str(tmp_path), autosave_interval=0) as r:
            async with r.raw() as http:
                sid, _ = await raw_init(http, r.url)
                await http.post(r.url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                             "params": {"name": "a_echo", "arguments": {}}},
                                headers={"Accept": "application/json, text/event-stream", "mcp-session-id": sid,
                                         "mcp-protocol-version": "2025-11-25"})
                await http.delete(r.url, headers={"mcp-session-id": sid})
            files = list((tmp_path / "ev").glob("*.json"))
            assert bool(files) is expect_file
            for f in files:
                f.unlink()
