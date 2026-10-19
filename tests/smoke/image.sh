#!/bin/bash
# Image-level checks that need no running services. Runs as root in the test stage.
set -euo pipefail
fail() { echo "FAIL: $*" >&2; exit 1; }
as_rebox() { setpriv --reuid=1000 --regid=1000 --clear-groups "$@"; }

# Refuses to start without REBOX_TOKEN (exit 2, one clear line).
set +e
out=$(as_rebox env -u REBOX_TOKEN HOME=/home/rebox RE_WORKDIR=/tmp /opt/rebox/venv/bin/rebox-supervisor 2>&1)
rc=$?
set -e
[ "$rc" = 2 ] || fail "supervisor without REBOX_TOKEN exited $rc: $out"
grep -q REBOX_TOKEN <<<"$out" || fail "no REBOX_TOKEN message: $out"

# Started directly as uid 1000 (Kubernetes runAsUser): no remap, PUID/PGID ignored with a warning.
out=$(as_rebox env PUID=4321 PGID=4321 /usr/local/bin/re-box-entrypoint id -u 2>&1)
grep -q 'ignoring them' <<<"$out" || fail "no PUID warning in uid-1000 mode: $out"
[ "$(tail -1 <<<"$out")" = 1000 ] || fail "uid-1000 mode ran as $(tail -1 <<<"$out")"
# An arbitrary uid without a writable home still gets one.
[ "$(setpriv --reuid=12345 --regid=12345 --clear-groups env HOME=/home/rebox /usr/local/bin/re-box-entrypoint sh -c 'echo $HOME')" = /tmp/rebox-home ] \
  || fail "arbitrary uid did not get a writable HOME"

# As root, PUID/PGID 0 is refused.
for bad in 0 00 "" x1; do
  if [ -n "$bad" ] && env PUID="$bad" /usr/local/bin/re-box-entrypoint true 2>/dev/null; then fail "PUID=$bad accepted"; fi
done

# Security defaults.
[ "${GHIDRA_MCP_ALLOW_SCRIPTS:-}" = 0 ] || fail "GHIDRA_MCP_ALLOW_SCRIPTS is '${GHIDRA_MCP_ALLOW_SCRIPTS:-}'"
grep -q 'GHIDRA_MCP_REQUIRE_PROGRAM_SELECTORS = "1"' /etc/rebox/aggregator.toml || fail "program selectors not forced"

# The shipped REA skill is rewritten to the prefixed names.
skill=/opt/rebox/skills/reverse-engineer-anything/SKILL.md
grep -q 'rea_open_binary' "$skill" || fail "skill not rewritten"

echo "image smoke: ok"
