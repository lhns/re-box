# Operating re-box

## What runs

`tini` (PID 1) → `re-box-entrypoint` (optional uid remap, then drops root) → `rebox-supervisor`:

- the ghidra-mcp headless server: one JVM, `127.0.0.1:8089` only, shared by all sessions, owns
  the Ghidra project in `GHIDRA_PROJECTS_DIR`. **On demand:** it starts with the first session
  (about 40 s until ready) and stops `REBOX_GHIDRA_IDLE_TIMEOUT` seconds after the last one
  closed, so an idle container runs no Java at all;
- the aggregator on `REBOX_PORT` (`/mcp`, `/healthz`), the only listening port, up at once;
- per MCP session: `rea mcp` and `bridge-mcp-ghidra`, each in its own process group; REA starts
  its own short-lived Ghidra JVM per session when a call needs one.

Design: `aggregator/README.md`; why: `docs/DECISIONS.md`.

## Environment

| variable | default | meaning |
| --- | --- | --- |
| `REBOX_TOKEN_FILE` / `REBOX_TOKEN` | one required | Bearer token for `/mcp`, from a file (preferred: Kubernetes/compose secrets, keeps it out of `docker inspect` and the process environment) or the variable; the file wins. The supervisor exits 2 without one. Never passed to children. |
| `REBOX_PORT` | `8443` | aggregator port |
| `REBOX_BIND` | `0.0.0.0` | aggregator bind address |
| `REBOX_TLS_CERT` / `REBOX_TLS_KEY` | unset | both set: serve HTTPS; else plain HTTP (expected behind a reverse proxy) |
| `REBOX_MAX_SESSIONS` | `4` | concurrent MCP sessions; one more `initialize` gets HTTP 503 with a JSON-RPC error saying so |
| `REBOX_SESSION_IDLE_TIMEOUT` | `1800` | seconds without a request before a session and its processes are closed; a session with a request still running is closed after 4x this without any client message. Applies to sessions whose client holds its GET notification stream open (interactive agents) |
| `REBOX_SESSION_ORPHAN_TIMEOUT` | `300` | the same, for a session with **no** open GET stream (`claude -p` that exited, a dead client), so its REA JVM does not linger; `0` = use the idle timeout for all |
| `REBOX_SESSION_EVICT_MIN_IDLE` | `120` | at `REBOX_MAX_SESSIONS`, a new `initialize` evicts the longest-idle session with nothing in flight if it has been idle at least this long (its work is saved first; its client re-initializes on 404); 503 only when none qualifies |
| `REBOX_PROGRESS_KEEPALIVE_INTERVAL` | `30` | seconds of silence after which a request carrying a `progressToken` gets a synthetic `notifications/progress`, so a client's no-progress abort does not kill a long import; `0` = off |
| `REBOX_GHIDRA_IDLE_TIMEOUT` | `300` | seconds with no session before the ghidra-mcp server is saved and stopped; `0` = never stop |
| `REBOX_EVIDENCE_RETENTION_DAYS` | `30` | REA evidence bundles older than this are deleted at start and daily; `0` = keep. Bundles with no records and no unknowns are deleted right after export |
| `REBOX_INIT_TIMEOUT` | `120` | seconds a child may take to start and initialize at session open |
| `REBOX_MAX_BODY_BYTES` | `16777216` | largest accepted POST body (413 above); a session also accepts at most 64 concurrent requests |
| `REBOX_CHILD_GRACE` | `5` | seconds between SIGTERM and SIGKILL when closing a session |
| `REBOX_HEADLESS_START_TIMEOUT` | `300` | seconds the headless server may take to answer; `/healthz` is 503 once a start takes longer |
| `REBOX_CONFIG` | `/etc/rebox/aggregator.toml` | the children of each session |
| `RE_WORKDIR` | `/work` | bind-mount point; must equal the path the harness uses. `GHIDRA_MCP_FILE_ROOT` is forced to it, so ghidra-mcp imports only from here. |
| `REBOX_STATE_DIR` | `$RE_WORKDIR/re-box-state` | everything worth keeping that the services write: Ghidra project, settings, caches, REA evidence (docs/PERSISTENCE.md). Empty means the default. Must be writable by the service user; no path element may start with `.` |
| `GHIDRA_PROJECTS_DIR` | `$REBOX_STATE_DIR/ghidra-projects` | ghidra-mcp's persistent project (`rebox.gpr`); no path element may start with `.` |
| `REBOX_AUTOSAVE_INTERVAL` | `300` | seconds between saves of changed ghidra-mcp programs, and between evidence exports of REA sessions with new calls; `0` disables both periodic saves (shutdown and session-end saves stay) |
| `REBOX_AUTOSAVE_TIMEOUT` | `20` | seconds a REA evidence export may take at session end before the session is closed anyway |
| `PUID` / `PGID` | `1000` | owner of files written to `RE_WORKDIR`; only applied when the container starts as root (below) |
| `JAVA_OPTS` | `-Xmx4g -XX:+UseG1GC -XX:G1PeriodicGCInterval=60000 -XX:G1PeriodicGCSystemLoadThreshold=0 -XX:MinHeapFreeRatio=10 -XX:MaxHeapFreeRatio=30` | headless server JVM; the G1 flags return heap to the OS after a burst (REA's own JVMs reject JVM option overrides and keep the defaults) |
| `GHIDRA_HEADLESS_MAXMEM` | `1536m` | heap of each REA Ghidra JVM (REA's own variable; upstream default 2G) |
| `REA_JADX_HEAP_MIB` | `2048` | heap of REA's JADX JVM for Android work |
| `GHIDRA_MCP_REQUIRE_PROGRAM_SELECTORS` | `1`, forced | every program-scoped ghidra-mcp call must name its program; the project is shared |
| `GHIDRA_MCP_ALLOW_SCRIPTS` | `0` | `1` enables ghidra-mcp's script and inline-script tools (arbitrary code in the headless JVM) |
| `GHIDRA_MCP_LAZY` | `0` | `1` loads ghidra-mcp tool groups lazily (needs a client that follows `tools/list_changed`); `0` lists all 197 tools up front. Part of the catalog-cache key |
| `GHIDRA_MCP_LOG_LEVEL` | `WARNING` | bridge log level |
| `REA_ANALYSIS_PROVIDER` | `ghidra` | REA's default native provider |
| `REA_MCP_INPUT_SCHEMA_PROFILE` | `full` | `compact` for clients with schema limits |
| `REA_MCP_OUTPUT_SCHEMA_PROFILE` | `full` | `off` for small-frame clients |
| `REA_MCP_MAX_RESPONSE_BYTES` | unset | raise only if the client has a bigger buffer |
| `REA_GHIDRA_STARTUP_TIMEOUT_MS` | REA default (330 s) | raise for very large binaries |
| `REA_LOG_LEVEL` | `warn` | REA's log level (stderr, relayed as `child_stderr`) |

Tool paths REA uses (set in the image; override only to swap a tool):

| tool | variable |
| --- | --- |
| objdump | `REA_OBJDUMP_COMMAND` |
| rizin | `REA_RIZIN_COMMAND` |
| gdb / pwndbg | `REA_GDB_COMMAND`, `REA_PWNDBG_GDB`, `REA_PWNDBG_GDBINIT`, `REA_PWNDBG_VENV_PATH` |
| pwntools (Python with pwntools, pyelftools, unicorn) | `REA_PWNTOOLS_PYTHON` |
| JADX (jadx-headless-mcp jar) | `REA_JADX_MCP_JAR` |
| apktool, adb | `REA_APKTOOL_COMMAND`, `REA_ADB_PATH` |
| binwalk, unblob | `REA_BINWALK_COMMAND`, `REA_UNBLOB_COMMAND` |
| tshark, capinfos | `REA_TSHARK_COMMAND`, `REA_CAPINFOS_COMMAND` |
| sigrok-cli | `REA_SIGROK_COMMAND` |
| prlimit (resource caps for the above) | `REA_FIRMWARE_PRLIMIT_COMMAND`, `REA_TSHARK_PRLIMIT_COMMAND`, `REA_SIGROK_PRLIMIT_COMMAND` |
| WABT (`wasm-validate`, `wasm-objdump`, `wasm2wat`) | `REA_WABT_BIN_DIRECTORY` |
| mitmdump | `REA_MITMDUMP_COMMAND` |
| Chrome for Testing | `REA_BROWSER_EXECUTABLE`, `REA_BROWSER_NO_SANDBOX=true` |

Versions are in `versions.env` (`TOOL_*`) and `deps/tools/requirements-*.txt`. Not available:
Hopper (optional build, spec section 9, not built), IDA, JEB, Kuna, ILSpy, Frida (its npm install
script downloads an unpinned binary, so REA's frida features report unavailable), and REA's
macOS-only features.

## Volumes and paths

- Mount the work directory at the **same absolute path** inside and out and set `RE_WORKDIR` to
  it: `-v /home/you/re:/home/you/re -e RE_WORKDIR=/home/you/re`. Paths from the harness then
  work unchanged. Nothing derives from `/work` except the default. On Windows use the Git Bash
  form, `/c/Users/you/re` inside (README, "Windows").
- State (Ghidra project, settings, caches, REA evidence) goes to `REBOX_STATE_DIR`, by default
  inside the work dir; mount a separate one the same way. Layout, save points and what survives a
  stop, kill or OOM kill: docs/PERSISTENCE.md. Only one container may use a state dir at a time
  (Ghidra locks the project).
- REA's per-session Ghidra projects and tool scratch space go to `/tmp`; give it a volume or tmpfs
  (compose.yaml) sized for a few GB per concurrent REA session on large targets.

## Users and ownership

- Started as uid 1000 (the image default, or Kubernetes `runAsUser: 1000`, `runAsNonRoot`):
  no remap; files in `RE_WORKDIR` and `REBOX_STATE_DIR` belong to 1000. `PUID`/`PGID` are ignored
  with a warning. Any other uid works too, as long as it can write both directories.
- Nothing is ever chowned: the state dir and its subdirectories are created by the service user,
  and an existing one keeps its owner. A state dir the service user cannot write stops the
  container with one line saying so (exit 2).
- Started as root (`user: "0:0"`): the entrypoint changes `rebox` to `PUID:PGID`, then drops to it
  with `setpriv` (no new privileges, no inheritable capabilities) before anything else runs.
  Needs `CAP_CHOWN`, `CAP_SETUID`, `CAP_SETGID` and a writable `/etc`; `PUID`/`PGID` 0 are refused.
  `RE_WORKDIR` and `REBOX_STATE_DIR` must then be writable by `PUID` (your own directories are,
  with `PUID=$(id -u)`). The test stage runs in this mode (tests/run-tests.sh).

## Memory

Per container:

```
limit ≥ (JAVA_OPTS -Xmx + 1 GiB)                                headless server
      + REBOX_MAX_SESSIONS × (GHIDRA_HEADLESS_MAXMEM + 1 GiB)  one REA Ghidra JVM, decompiler,
                                                                node and bridge per session
      + 1 GiB                                                   supervisor, aggregator, page cache
      [+ (REA_JADX_HEAP_MIB + 0.5 GiB) per concurrent Android analysis]
```

Defaults: (4 + 1) + 4 × (1.5 + 1) + 1 = 16 GiB, the compose example's limit. A REA session with
no Ghidra call yet costs about 0.2 GiB; the headless server idles at about 130 MB RSS (measured)
and is gone entirely `REBOX_GHIDRA_IDLE_TIMEOUT` after the last session.

**Laptop profile** (Docker Desktop / WSL2, 8 GB for the VM): `REBOX_MAX_SESSIONS=2`,
`JAVA_OPTS="-Xmx2g -XX:+UseG1GC"` (+ the G1 flags above), `GHIDRA_HEADLESS_MAXMEM=1g`, container
limit 7g: the formula gives (2 + 1) + 2 × (1 + 1) + 1 = 8 GiB, so this runs 1 GiB below the worst
case and relies on the headless server and the REA JVMs not peaking together (they rarely do; an
OOM kill is what happens if they do). With a 10 GB VM use `--memory 8g` and `-Xmx3g`. Docker
Desktop's VM limit (Settings, Resources, or `.wslconfig` `memory=`) must be at least the container
limit. To fit less memory lower `REBOX_MAX_SESSIONS` first; to
analyse very large binaries raise `GHIDRA_HEADLESS_MAXMEM` and `JAVA_OPTS` and the limit with
them. Measured on the test fixtures: see docs/BUILDING.md.

## Health, logs, shutdown

- `HEALTHCHECK` runs `re-box-healthcheck` → `/healthz` (start period 30 s, the aggregator is up at
  once). 200 with `"ghidra": "stopped" | "starting" | "ready"`; 503 when the Ghidra server
  `failed` (crashed, backing off), is shutting down, or has been starting longer than
  `REBOX_HEADLESS_START_TIMEOUT`. An idle container is healthy with no JVM running.
- Logs are JSON lines on stdout: `session_open`, `session_close` (with reason), `tool_call` (tool,
  duration, outcome, error code), `autosave` (REA evidence export or Ghidra save), `child_exit`,
  `headless_state`, `headless_log`, `child_stderr`, ... Tool arguments and results are never
  logged. Readable with `docker logs re-box | jq -r 'select(.level != "info") | [.event, .error // .reason // ""] | @tsv'`
  (drop the `select` for everything; `docker compose logs --no-log-prefix re-box` for compose).
- SIGTERM: new sessions are refused, the headless server starts saving all programs, each session
  exports its REA evidence and is closed, then the headless server saves again and exits. Give it
  time: `stop_grace_period: 120s` / `docker run --stop-timeout 120`; Docker's default is 10 s
  (docs/PERSISTENCE.md). A crashed headless server is restarted with
  backoff while sessions exist; they survive, `gh_` calls fail until it is back.
- Clients that exit without ending their session (Claude Code `-p` does not send DELETE) keep it,
  and its REA Ghidra JVM, until `REBOX_SESSION_ORPHAN_TIMEOUT` (300 s; sessions with an open GET
  stream get the full `REBOX_SESSION_IDLE_TIMEOUT`); they count against `REBOX_MAX_SESSIONS` until
  then, unless idle long enough to be evicted for a new session (`REBOX_SESSION_EVICT_MIN_IDLE`).
- A session is closed as soon as one of its backends (`rea mcp`, the bridge) dies; the client's next
  request gets 404 and re-initializes.

## Security

- The token is compared in constant time; `/healthz` is the only unauthenticated path. Put TLS in
  front (reverse proxy, or `REBOX_TLS_*`); give the proxy a read timeout of at least 15 min, since
  a tool call's response stream can stay silent for minutes.
- **The container is the sandbox.** Several REA features execute the target (process capture,
  gdb, browser scenarios). Run re-box on an egress-restricted network and treat what it analyses
  as hostile (compose.yaml has a commented `internal` network for this). Offline, what stops:
  debug-symbol downloads (Ghidra's symbol servers; local symbol files still work) and anything a
  target or tool fetches from the internet. Analysis of local files is unaffected. Defaults: non-root, all capabilities dropped (compose.yaml adds back only what the
  uid remap needs), `no-new-privileges`, headless server on loopback, scripts off, the file root
  guard on `RE_WORKDIR`, the token stripped from every child's environment.
- Executed targets run as the same uid as the services, so they could reach the loopback headless
  server or read a mounted token file; the network policy above is what contains them. The
  supervisor marks itself non-dumpable so its own environment is not readable through `/proc`.

## Upgrading a running deployment

```sh
docker compose stop                                   # stop_grace_period 120s: saves Ghidra, exports REA evidence
cp -a "$RE_WORKDIR/re-box-state" "$RE_WORKDIR/re-box-state.bak"   # or your REBOX_STATE_DIR; stopped, so consistent
docker compose pull && docker compose up -d
docker create --name rebox-skill ghcr.io/lhns/re-box:latest \
  && docker cp rebox-skill:/opt/rebox/skills/. ~/.claude/skills/ && docker rm rebox-skill   # REA's skill, re-extracted
```

Open sessions end with the stop; clients re-initialize on their next request. `mkdir` the work dir
before the first `compose up`, or Docker creates it as root.

## Upgrades (building)

All pins are in `versions.env` (plus the lockfiles it names). After changing a version run
`python3 scripts/check-pins.py --update`, then `docker build --target test .`. Renovate does the
version bumps (renovate.json) and CI fails on a stale digest. Ghidra, ghidra-mcp and REA
interlock: REA accepts one Ghidra minor line (`GhidraReleasePolicy.ts`) and ghidra-mcp targets one
Ghidra release (`pom.xml`), so bump them together and read the e2e results. The REA skill is
regenerated from the installed REA at build time; clients re-extract it (docs/CLIENTS.md).
