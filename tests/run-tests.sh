#!/bin/bash
# The test stage's whole suite. Runs as root inside `docker build` (no init, and under rootless
# BuildKit no PID namespace of its own), so it starts the services itself, scopes every process
# check to its own REBOX_INSTANCE, and kills everything it started before returning.
# Writes /opt/rebox/tests-passed only when everything passed; `final` copies that file.
set -euo pipefail

TESTS=$(cd "$(dirname "$0")" && pwd)
PY=/opt/rebox/test-venv/bin/python
export PUID=4321 PGID=4321                       # exercise the root -> PUID/PGID remap
REBOX_INSTANCE="e2e-$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
export REBOX_TOKEN="e2e-$(head -c24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
export REBOX_PORT=18443
export REBOX_URL="http://127.0.0.1:$REBOX_PORT/mcp"
export RE_WORKDIR=/tmp/rebox-e2e/work
export GHIDRA_PROJECTS_DIR=$RE_WORKDIR/ghidra-projects
LOG=/tmp/rebox-e2e/services.log
MEM=/tmp/rebox-e2e/rss.log

step() { printf '\n=== %s\n' "$*"; }

# pids whose environment carries our instance id: the services and their descendants. The id is
# deliberately not exported, so this script's own helpers never match. Read as PUID: without
# CAP_SYS_PTRACE (Docker's default) root cannot read another uid's /proc/<pid>/environ.
as_puid() { setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"; }
instance_pids() {
  as_puid sh -c 'grep -lsa --null-data "^REBOX_INSTANCE=$1\$" /proc/[0-9]*/environ' sh "$REBOX_INSTANCE" 2>/dev/null \
    | cut -d/ -f3 | grep -vx "$$" || true
}

sup_pid=
cleanup() {
  local rc=$?
  [ -n "$sampler" ] && kill "$sampler" 2>/dev/null || true
  if [ -n "$sup_pid" ] && kill -0 "$sup_pid" 2>/dev/null; then kill -KILL -- "-$sup_pid" 2>/dev/null || true; fi
  for p in $(instance_pids); do kill -KILL "$p" 2>/dev/null || true; done
  exit $rc
}
sampler=
trap cleanup EXIT

step "smoke: image"
"$TESTS/smoke/image.sh"

step "smoke: tools and rea doctor (as rebox)"
setpriv --reuid=rebox --regid=rebox --init-groups env HOME=/home/rebox "$TESTS/smoke/tools.sh"

step "aggregator unit tests"
(cd /src/aggregator && "$PY" -m pytest -q -p no:cacheprovider tests)

step "start services (root -> uid $PUID)"
rm -rf /tmp/rebox-e2e && mkdir -p "$RE_WORKDIR"
cp /opt/rebox/fixtures/* "$RE_WORKDIR/"
chown -R "$PUID:$PGID" "$RE_WORKDIR"
chmod 0644 "$RE_WORKDIR"/*
# Own session and process group, like the container's PID 1 would give it.
setsid env REBOX_INSTANCE="$REBOX_INSTANCE" /usr/local/bin/re-box-entrypoint /opt/rebox/venv/bin/rebox-supervisor >"$LOG" 2>&1 &
sup_pid=$!
# RSS of everything in the instance, for sizing the ImageBuild resources.
( peak=0; while sleep 2; do
    kb=0; for p in $(instance_pids) $sup_pid; do
      r=$(awk '/^VmRSS/{print $2}' "/proc/$p/status" 2>/dev/null || echo 0); kb=$((kb + ${r:-0})); done
    [ "$kb" -gt "$peak" ] && peak=$kb && echo "$peak" > "$MEM"
  done ) &
sampler=$!
start=$(date +%s)
until curl -fs -o /dev/null "http://127.0.0.1:$REBOX_PORT/healthz"; do
  kill -0 "$sup_pid" 2>/dev/null || { cat "$LOG"; echo "supervisor exited during startup" >&2; exit 1; }
  [ $(( $(date +%s) - start )) -lt 600 ] || { tail -100 "$LOG"; echo "not ready after 600 s" >&2; exit 1; }
  sleep 1
done
echo "ready after $(( $(date +%s) - start )) s"

step "e2e"
set +e
# As PUID, for the same /proc reason (test 10 finds a session's processes by their environment).
(cd "$TESTS/e2e" && REBOX_INSTANCE="$REBOX_INSTANCE" as_puid "$PY" -m pytest -v -p no:cacheprovider -rA .)
e2e=$?
set -e

step "graceful shutdown"
kill -TERM "$sup_pid"
for _ in $(seq 120); do kill -0 "$sup_pid" 2>/dev/null || break; sleep 0.5; done
if kill -0 "$sup_pid" 2>/dev/null; then echo "supervisor ignored SIGTERM for 60 s" >&2; e2e=1; fi
sup_rc=0; wait "$sup_pid" || sup_rc=$?
left=$(instance_pids)
if [ -n "$left" ]; then echo "processes left after shutdown: $left" >&2; ps -o pid,args -p $left >&2 || true; e2e=1; fi
[ "$sup_rc" = 0 ] || { echo "supervisor exit code $sup_rc" >&2; e2e=1; }
grep -q '"event": *"session_open"' "$LOG" || { echo "no session_open log line" >&2; e2e=1; }
echo "peak RSS of the instance: $(( $(cat "$MEM" 2>/dev/null || echo 0) / 1024 )) MiB"

if [ "$e2e" != 0 ]; then
  echo "--- services log (tail)"; tail -200 "$LOG"
  exit 1
fi
touch /opt/rebox/tests-passed
echo "all tests passed"
