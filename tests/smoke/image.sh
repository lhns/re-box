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

# State dir checks run before any service starts: one line, exit 2.
sup() { as_rebox env -u REBOX_TOKEN_FILE REBOX_TOKEN=t HOME=/home/rebox RE_WORKDIR=/tmp "$@" /opt/rebox/venv/bin/rebox-supervisor 2>&1; }
expect_refused() { # <what> <output> <rc>
  [ "$3" = 2 ] || fail "$1 exited $3: $2"
  grep -q REBOX_STATE_DIR <<<"$2" || fail "$1: no REBOX_STATE_DIR in message: $2"
  [ "$(wc -l <<<"$2")" = 1 ] || fail "$1: not exactly one line: $2"
}
ro=$(mktemp -d); chmod 0755 "$ro"
set +e
out=$(sup REBOX_STATE_DIR="$ro/state"); rc=$?
expect_refused "unwritable state dir" "$out" $rc
out=$(sup REBOX_STATE_DIR=relative/state); rc=$?
expect_refused "relative state dir" "$out" $rc
out=$(sup REBOX_STATE_DIR=/tmp/.dot/state); rc=$?
set -e
[ "$rc" = 2 ] && [ "$(wc -l <<<"$out")" = 1 ] && grep -q "'.'" <<<"$out" || fail "dot-element state dir: rc=$rc $out"
[ ! -e /tmp/.dot ] || fail "dot-element state dir was created"
rm -rf "$ro"

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
# Scripts are on for REA's attach (DECISIONS.md); clients must not reach the script tools.
for t in run_ghidra_script run_script_inline delete_project; do
  grep -q "^deny = \[.*\"$t\"" /etc/rebox/aggregator.toml || fail "ghidra-mcp's $t is not denied"
done
grep -q '^REBOX_REA_ATTACH_HELPER = "/opt/rebox/rea-attach/helper.mjs"' /etc/rebox/aggregator.toml \
  || fail "REA attach helper not configured"
[ -f /opt/rebox/rea-attach/helper.mjs ] && [ -f /opt/rebox/rea-attach/ReboxReaAttach.java ] \
  && [ -f /opt/rebox/rea-attach/ghidra_scripts/ReaGhidraBridge.java ] || fail "REA attach files missing"
grep -q 're-box: attach to ghidra-mcp' /opt/rea/node_modules/rea-agents/dist/mcp/main-*.js || fail "REA not patched"
grep -q 'GHIDRA_MCP_REQUIRE_PROGRAM_SELECTORS = "1"' /etc/rebox/aggregator.toml || fail "program selectors not forced"

# The shipped REA skill is rewritten to the prefixed names.
skill=/opt/rebox/skills/reverse-engineer-anything/SKILL.md
grep -q 'rea_open_binary' "$skill" || fail "skill not rewritten"

echo "image smoke: ok"
