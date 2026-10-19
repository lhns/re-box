# Operating re-box

## What runs

`tini` (PID 1) → `re-box-entrypoint` (optional uid remap, then drops root) → `rebox-supervisor`:

- the ghidra-mcp headless server: one JVM, `127.0.0.1:8089` only, shared by all sessions, owns
  the Ghidra project in `GHIDRA_PROJECTS_DIR`;
- the aggregator on `REBOX_PORT` (`/mcp`, `/healthz`), the only listening port;
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
| `REBOX_SESSION_IDLE_TIMEOUT` | `1800` | seconds without a request before a session and its processes are closed; a session with a request still running is closed after 4� this without any client message |
| `REBOX_INIT_TIMEOUT` | `120` | seconds a child may take to start and initialize at session open |
| `REBOX_MAX_BODY_BYTES` | `16777216` | largest accepted POST body (413 above); a session also accepts at most 64 concurrent requests |
| `REBOX_CHILD_GRACE` | `5` | seconds between SIGTERM and SIGKILL when closing a session |
| `REBOX_HEADLESS_START_TIMEOUT` | `300` | seconds the headless server may take to answer |
| `REBOX_CONFIG` | `/etc/rebox/aggregator.toml` | the children of each session |
| `RE_WORKDIR` | `/work` | bind-mount point; must equal the path the harness uses. `GHIDRA_MCP_FILE_ROOT` is forced to it. |
| `GHIDRA_PROJECTS_DIR` | `$RE_WORKDIR/ghidra-projects` | ghidra-mcp's persistent project (`rebox.gpr`); must be inside `RE_WORKDIR`, no path element may start with `.` |
| `PUID` / `PGID` | `1000` | owner of files written to `RE_WORKDIR`; only applied when the container starts as root (below) |
| `JAVA_OPTS` | `-Xmx4g -XX:+UseG1GC` | headless server JVM |
| `GHIDRA_HEADLESS_MAXMEM` | `1536m` | heap of each REA Ghidra JVM (REA's own variable; upstream default 2G) |
| `REA_JADX_HEAP_MIB` | `2048` | heap of REA's JADX JVM for Android work |
| `GHIDRA_MCP_REQUIRE_PROGRAM_SELECTORS` | `1`, forced | every program-scoped ghidra-mcp call must name its program; the project is shared |
| `GHIDRA_MCP_ALLOW_SCRIPTS` | `0` | `1` enables ghidra-mcp's script and inline-script tools (arbitrary code in the headless JVM) |
| `GHIDRA_MCP_LAZY` | `1` | lazy tool groups; `0` for clients that ignore `tools/list_changed` |
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
  work unchanged. Nothing derives from `/work` except the default.
- `GHIDRA_PROJECTS_DIR` holds ghidra-mcp's annotations next to the binaries; back it up with them.
  Only one container may use a project at a time (Ghidra locks it); a stale lock after a crash is
  taken over on the next start.
- REA's temporary Ghidra projects and caches go to `/tmp`; give it a volume or tmpfs
  (compose.yaml) sized for a few GB per concurrent REA session on large targets.

## Users and ownership

- Started as uid 1000 (the image default, or Kubernetes `runAsUser: 1000`, `runAsNonRoot`):
  no remap; files in `RE_WORKDIR` belong to 1000. `PUID`/`PGID` are ignored with a warning. Any
  other uid works too; if it cannot write `/home/rebox`, `HOME` moves to `/tmp/rebox-home`.
- Started as root (`user: "0:0"`): the entrypoint changes `rebox` to `PUID:PGID`, then drops to it
  with `setpriv` (no new privileges, no inheritable capabilities) before anything else runs.
  Needs `CAP_CHOWN`, `CAP_SETUID`, `CAP_SETGID` and a writable `/etc`; `PUID`/`PGID` 0 are refused.
  The test stage runs in this mode (tests/run-tests.sh).

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
no Ghidra call yet costs about 0.2 GiB. To fit less memory lower `REBOX_MAX_SESSIONS` first; to
analyse very large binaries raise `GHIDRA_HEADLESS_MAXMEM` and `JAVA_OPTS` and the limit with
them. Measured on the test fixtures: see docs/IMAGEBUILD.md.

## Health, logs, shutdown

- `HEALTHCHECK` runs `re-box-healthcheck` → `/healthz`, 200 only while the headless server answers
  and its project is open (start period 5 min).
- Logs are JSON lines on stdout: `session_open`, `session_close` (with reason), `tool_call` (tool,
  duration, outcome, error code), `child_exit`, `headless_log`, `child_stderr`, ... Tool arguments
  and results are never logged.
- SIGTERM: new sessions are refused, sessions closed, the headless server saves all programs and
  exits; give it time (`stop_grace_period: 90s`). A crashed headless server is restarted with
  backoff; sessions survive, `gh_` calls fail until it is back.
- Clients that exit without ending their session (Claude Code `-p` does not send DELETE) keep it,
  and its REA Ghidra JVM, until `REBOX_SESSION_IDLE_TIMEOUT`; they also count against
  `REBOX_MAX_SESSIONS`. For many short-lived clients lower the idle timeout (e.g. 300).

## Security

- The token is compared in constant time; `/healthz` is the only unauthenticated path. Put TLS in
  front (reverse proxy, or `REBOX_TLS_*`); give the proxy a read timeout of at least 15 min, since
  a tool call's response stream can stay silent for minutes.
- **The container is the sandbox.** Several REA features execute the target (process capture,
  gdb, browser scenarios). Run re-box on an egress-restricted network and treat what it analyses
  as hostile. Defaults: non-root, all capabilities dropped (compose.yaml adds back only what the
  uid remap needs), `no-new-privileges`, headless server on loopback, scripts off, the file root
  guard on `RE_WORKDIR`, the token stripped from every child's environment.
- Executed targets run as the same uid as the services, so they could reach the loopback headless
  server or read a mounted token file; the network policy above is what contains them. The
  supervisor marks itself non-dumpable so its own environment is not readable through `/proc`.

## Upgrades

All pins are in `versions.env` (plus the lockfiles it names). After changing a version run
`python3 scripts/check-pins.py --update`, then `docker build --target test .`. Renovate does the
version bumps (renovate.json) and CI fails on a stale digest. Ghidra, ghidra-mcp and REA
interlock: REA accepts one Ghidra minor line (`GhidraReleasePolicy.ts`) and ghidra-mcp targets one
Ghidra release (`pom.xml`), so bump them together and read the e2e results. The REA skill is
regenerated from the installed REA at build time; clients re-extract it (docs/CLIENTS.md).
