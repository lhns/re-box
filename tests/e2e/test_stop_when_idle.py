"""Ghidra stops when the last session is gone (REBOX_GHIDRA_IDLE_TIMEOUT=5) and comes back.

Its own file because pytest runs files alphabetically and this one must come last: it releases
the keeper session (conftest.py) that holds Ghidra up for every other test.
"""
from __future__ import annotations

import time

import httpx2 as httpx

from conftest import TOKEN, URL, record, release_keeper
from helpers import ghidra_mcp_jvm, jtext
from mcpclient import Session


def test_11_idle_stop_and_restart():
    assert ghidra_mcp_jvm(), "the keeper session should have kept Ghidra running"
    release_keeper()  # every other session of the run is closed already
    t0 = time.monotonic()
    deadline = t0 + 90
    while ghidra_mcp_jvm() and time.monotonic() < deadline:
        time.sleep(0.5)
    record("11_idle_stop", "Ghidra gone after the last session closed (idle timeout 5 s)", time.monotonic() - t0)
    assert not ghidra_mcp_jvm()
    # The JVM leaves /proc a moment before the supervisor has reaped it and says "stopped"
    # (measured 0.3 s on an idle machine).
    with httpx.Client(timeout=30) as http:
        for _ in range(40):
            health = http.get(URL.rsplit("/mcp", 1)[0] + "/healthz")
            if health.status_code != 200 or health.json()["ghidra"] != "stopping":
                break
            time.sleep(0.25)
    assert health.status_code == 200 and health.json()["ghidra"] == "stopped", health.text

    # A cold session: the catalog comes from the cache at once, and a ghidra_ call waits for Ghidra.
    s = Session(URL, TOKEN)
    try:
        s.initialize()
        assert "ghidra_get_metadata" in s.tool_names()
        opened = s.tool("ghidra_open_program", {"path": "/hello_stripped"})  # imported and saved by test_03
        record("11_idle_stop", "first ghidra_ call of a cold session (restart included)", opened.seconds)
        assert jtext(opened).get("success"), opened.text()[:500]
        assert ghidra_mcp_jvm()
    finally:
        s.close()
