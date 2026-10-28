# rebox-aggregator

One authenticated streamable-HTTP MCP endpoint (`/mcp`) in front of several stdio MCP servers,
with **one set of child processes per downstream MCP session**. Also contains the container's
supervisor, which runs the ghidra-mcp headless server next to the aggregator.

Why custom, and the protocol-era decision: `docs/DECISIONS.md`, "5.2".

## Modules

| module | role |
| --- | --- |
| `config.py` | settings from env, children and session texts from the TOML config |
| `children.py` | one stdio child: own process group, newline-delimited JSON-RPC through mcp's `JSONRPCDispatcher`, stderr relay, group kill |
| `catalog.py` | prefixing, name validation, merged tools/prompts/resources catalog |
| `session.py` | per-session router between the downstream transport (its own `JSONRPCDispatcher`) and the children; idle and orphan timeouts |
| `sessioncatalog.py` | what a session advertises: merged listings, list_changed refresh, the cached catalog while Ghidra starts, `on_ready` |
| `autosave.py` | a child's autosave tool: on close, periodically, before a tool that drops the work |
| `app.py` | ASGI app: auth, `/healthz`, `/mcp` session manager (cap, eviction, era rejection) |
| `supervisor.py` | container entry point: config checks, the 8089 auth token, runs the app and the headless server, catalog warm-up, signal handling |
| `headless.py` | the ghidra-mcp JVM (on demand: start, health, save, idle stop, crash restart) and the Ghidra project |
| `evidence.py` | REA evidence bundles: drop an empty export |
| `housekeeping.py` | leftovers nothing else removes (a session's or instance's processes by environment, `/tmp` sweep, half-written exports, old evidence bundles, rolled Ghidra logs), the daily loop, project size |
| `catalogcache.py` | per-child catalog cache (key, atomic save/load) and the session-less listing used by the warm-up |
| `log.py` | JSON-lines logging to stdout (also the mcp library's warnings), process output relay |

Entry points: `rebox-supervisor` (container default), `rebox-aggregator` (app only, no
headless server; used by unit tests and for development).

## Configuration

Env: every variable, default and meaning is in docs/OPERATIONS.md, "Environment". Notes that
belong to the code:

- `REBOX_TOKEN_FILE` is preferred over `REBOX_TOKEN` because a process's environment is readable
  through `/proc/<pid>/environ` by other processes of the same uid; the file wins, and neither (or
  an empty token) refuses to start.
- `REBOX_INIT_TIMEOUT` is per child and covers spawn, `initialize` and listing; a child that
  exceeds it is killed and counts as failed.
- `REBOX_CHILD_GRACE` is the wait between SIGTERM and SIGKILL of a child's process group.
- `REBOX_STATE_DIR` is resolved and exported by the supervisor. Empty in the bare `rebox-aggregator`
  (unit tests, development) it disables autosave and the catalog cache; the container always
  defaults it.
- `REBOX_HEADLESS_START_TIMEOUT` is also how long a session waits for Ghidra (below).

Children and session texts (TOML), generic so more servers can be added without code changes:

```toml
[session]                    # optional; the shipped file says what Ghidra and REA need said
instructions = '''...'''     # the initialize result's instructions; {NAME}: env var (the session adds state dir, tag, evidence file)
backend_failed = "Ghidra is {state}: {reason}. Try again later."      # a call held for the backend: it failed
backend_starting = "Ghidra is still starting after {seconds} s. Try again later."  # ... or took too long

[[child]]
name = "rea"                 # [a-z0-9-]+, used in logs and errors
prefix = "rea_"              # prepended to tool and prompt names
command = ["/usr/local/bin/rea", "mcp"]   # absolute path required
deny = ["run_script"]        # optional: the child's own tool names to hide and refuse
[child.env]                  # forced: overrides the container env; {NAME}: the supervisor's env var
REA_LOG_LEVEL = "warn"
TMPDIR = "{REBOX_REA_TMP}"
[child.autosave]             # optional: a child tool that persists its work
tool = "export_evidence_bundle"   # the child's own (unprefixed) tool name
arguments = { path = "{state_dir}/rea/evidence/{session}.json", overwrite = true }  # {state_dir}, {session} substituted
prune_empty = true           # optional: delete the file again if it is an evidence bundle with no records/unknowns
before_tools = ["close_binary"]  # optional: the child's tools that drop its work; saved before they are forwarded
[child.on_ready]             # optional: a child tool called when the Ghidra server becomes ready after the child booted
tool = "connect_instance"
arguments = { project = "rebox" }
[child.catalog_cache]        # optional: remember the child's full catalog, advertise it while the backend starts
key = "{file:/opt/ghidra-mcp/VERSION}-lazy{GHIDRA_MCP_LAZY}-scripts{GHIDRA_MCP_ALLOW_SCRIPTS}"   # {NAME}: env var, {file:/path}: file content
```

**Deny list:** `deny` names tools by the child's own (unprefixed) names. They are dropped from
`tools/list` (again after every `list_changed` refresh), and a `tools/call` to one fails with
`-32602` "Tool ghidra_x is disabled by this re-box's configuration" without reaching the child. The
shipped config denies ghidra-mcp's `run_ghidra_script`, `run_script_inline` and `delete_project`.
`REBOX_DENY_TOOLS` (comma-separated, prefixed names such as `rea_start_gdb_session`) adds to the
lists at start without editing the file: the longest matching child prefix picks the child. It is a
config error to name an unknown prefix or a tool of that child's `autosave` (`tool`, `before_tools`)
or `on_ready`, since denying it would silently break the hook.

**on_ready:** the Ghidra server starts with the first session, so a bridge usually boots before it
and shows only static tools. When the server becomes ready (and the client has sent
`notifications/initialized`), the aggregator calls this tool on every session's child (its response
is never held); the bridge connects and sends `tools/list_changed`, which is relayed, and a child
advertised from the cache is re-listed either way. A session that opens while Ghidra is already up
gets no call, unless there is no cache yet (below). Logged as `on_ready` (`outcome`
ok/tool_error/failed); the bridge's own 15 s retry is the fallback.

**Catalog cache:** clients cannot be relied on to follow `tools/list_changed`, so a session must
advertise the child's full catalog from its first `tools/list` even while the backend is down.
Whenever a child's listing is complete (it booted against a ready backend, or `on_ready` succeeded
and the child was re-listed) it is written, atomically, to
`$REBOX_STATE_DIR/cache/rebox-catalog/<child>-<key>.json` (files for other keys are removed; a
smaller listing never replaces a bigger one). A session that opens with the backend not ready
advertises the cache instead of the child's partial listing; the child's tools that existed at
boot (the bridge's static ones) stay callable at once, a `tools/call` for any other cached tool is
held (progress keepalive applies) until the live listing has replaced the cache, at most
`REBOX_HEADLESS_START_TIMEOUT`, and then fails with a tool result `isError` (`[session]`
`backend_failed` / `backend_starting`). When the live listing differs, `list_changed` is sent for
each kind that changed. With no cache the session's `initialize` reply is held until the child is
connected (`on_ready`, then re-listed, also when Ghidra is already up; bounded;
on timeout the partial listing is used and `catalog_cold` is logged), and the supervisor, at
container start, runs the server once and lists the child itself (`catalog_warmup`) so that rarely
happens. Without `REBOX_STATE_DIR` the feature is off.

Autosave runs when `REBOX_STATE_DIR` is set, for children that served a `tools/call` since their
last save (dirty): on session close (all reasons, concurrently, before the children are closed,
each bounded by `REBOX_AUTOSAVE_TIMEOUT`, waiting for a periodic save in flight) and every
`REBOX_AUTOSAVE_INTERVAL` seconds while no request is in flight to that child. A save that fails,
times out or whose caller is cancelled leaves the child dirty. It never counts as session activity. Logged as `autosave`
(`reason`, `outcome` ok/tool_error/rpc_error/timeout, `duration_ms`); failures only warn.

A child's environment is the container env **minus every `REBOX_*` variable** (the token is not
handed to children or the targets REA executes; a same-uid process could still read a token
file, so the container, not the uid, is the boundary: docs/OPERATIONS.md, "Security"), plus `[child.env]`, plus `REBOX_INSTANCE` (the
supervisor's random instance id) and `REBOX_SESSION` (the session tag, below).

## Session lifecycle

- **Session tag** = first 16 hex chars of `sha256(Mcp-Session-Id)`. Logs carry the tag, never
  the id. Every child gets it as `REBOX_SESSION`, inherited by its descendants (REA's tools, attach helper
  and bridge), which is how cleanup and the e2e test find a session's processes without PID-namespace
  isolation.
- **Open:** a POST without `Mcp-Session-Id` whose single JSON-RPC message is `initialize`.
  At the cap, the session idle longest with nothing in flight and idle at least
  `REBOX_SESSION_EVICT_MIN_IDLE` is closed first (autosave included; logged `session_evicted`, close
  reason `evicted`; its client gets 404 and re-initializes). Refused with HTTP 503 and JSON-RPC error
  `-32000` "re-box session limit reached (N of N active) ..." when none qualifies, or while shutting
  down. Otherwise a session is created, the Ghidra server is wanted (below) and the session's router
  handles the `initialize` request: spawn all children concurrently, `initialize`
  each (forwarding the client's `capabilities` and `clientInfo`, with the negotiated
  `protocolVersion`), send each `notifications/initialized`, fetch `tools/list` (all pages),
  `prompts/list` / `resources/list` / `resources/templates/list` where advertised, build the
  catalog, reply (the result carries `instructions`: `[session] instructions`, then the state dir,
  this session's tag and evidence file). It does not wait for Ghidra (except as in "Catalog cache").
  A child that fails to start or initialize is logged and marked dead; the
  session still opens if at least one child is up. All down, or an invalid catalog (below): the
  `initialize` gets a JSON-RPC error and the session is discarded.
- **Era:** only the handshake protocol versions (2024-11-05 .. 2025-11-25). The reply uses the
  client's requested version if it is one of those, else 2025-11-25. A sessionless POST that is
  not `initialize`, or that carries an `MCP-Protocol-Version` header outside that set (2026-07-28
  `server/discover` probes), gets HTTP 400 with JSON-RPC error `-32022` and
  `data: {"supported": [...handshake versions...], "requested": <header or "">}`; clients fall back
  to `initialize`. GET/DELETE without a session id: 400. Unknown session id: 404, `-32001`.
- **Close:** on DELETE, idle or orphan timeout, eviction, supervisor shutdown, or as soon as any
  child dies (`child_exited`: a session missing a backend is half dead; the client's next request
  gets 404 and it re-initializes; a child's exit is seen when it exits, even while a grandchild
  holds its stdout, and its process group is killed then): (0) autosave
  the live children; (1) close
  each child's stdin and wait up to 2 s; (2) SIGTERM its process group, wait `REBOX_CHILD_GRACE`;
  (3) SIGKILL the group; (4) SIGKILL any remaining process whose `/proc/<pid>/environ` holds
  this `REBOX_SESSION` and `REBOX_INSTANCE`. Then terminate the transport (later requests: 404).
- **Idle:** no downstream message for `REBOX_SESSION_IDLE_TIMEOUT` seconds and nothing in flight, or
  no downstream message for 4 x that while requests are in flight (a long call may keep a live client
  quiet; a client silent for 4x is gone). A session whose client holds its standalone GET stream
  open (the MCP SDKs open it after `notifications/initialized`) keeps the full timeout; one with no
  open GET stream is probably gone (`claude -p` exited without DELETE) and uses
  `REBOX_SESSION_ORPHAN_TIMEOUT` instead (close reason `orphaned`), so its REA process, helper and bridge do not linger.
  The app counts open GET streams per session and logs `get_stream_open` / `get_stream_closed`
  (level `debug`, with the number open). `Session.idle_timeout()` is the one place that decides.
- **Keepalive progress:** for an in-flight request with a `progressToken` the child has not
  reported progress on for `REBOX_PROGRESS_KEEPALIVE_INTERVAL`, the session sends a synthetic
  `notifications/progress` (`progress` = highest value seen + 0.001 per keepalive, `message`
  "still working"), so a client's no-progress abort (Claude Code: 5 min) does not kill a long
  import or analysis that is merely quiet.
- **Limits:** at most 64 in-flight requests per session (`-32000` "too many concurrent requests"),
  a duplicate in-flight request id gets `-32600`, child output lines are capped at 64 MiB, and
  notifications queued for a stalled client are dropped beyond 1000 (`downstream_backpressure`).
  The supervisor and aggregator set `PR_SET_DUMPABLE=0` so their own environment is not readable
  through `/proc` by same-uid processes; children are unaffected.

## Routing (per session)

Messages stay plain dicts end to end; results and `structuredContent` are forwarded unchanged.
mcp's `JSONRPCDispatcher` (2.3.0) runs both sides: the downstream session and, as its client, each
child. It owns request ids, correlation, cancellation and progress tokens; every child line is
validated as a JSON-RPC message (an invalid one, such as an id that is a list, is logged
`child_bad_line` and dropped). An unexpected error in a request handler answers `-32603`; in a
notification handler it closes the session (`internal_error`).

- `initialize`: above. `notifications/initialized` from downstream: not forwarded (the children had
  theirs); it lets `on_ready` calls go out.
- `notifications/roots/list_changed` from downstream: forwarded to every child.
- `ping`: answered locally.
- `tools/list`: the merged catalog, one page. `prompts/list`, `resources/list`,
  `resources/templates/list`: likewise.
- `tools/call`, `prompts/get`: strip the prefix, forward to the owning child with the original
  name, everything else in `params` unchanged except `_meta.progressToken` (below). Unknown name:
  JSON-RPC `-32602` "Unknown tool: X". Dead child: `-32603` "backend 'rea' is not running in
  this session (exited with code N)".
- `resources/read`, `resources/subscribe`, `resources/unsubscribe`: routed by URI to the child
  that listed it (or whose template's static prefix matches). Resource URIs are not rewritten;
  resource and template `name`s get the prefix.
- `completion/complete`: `ref/prompt` by prefixed name, `ref/resource` by URI.
- `logging/setLevel`: sent to every live child that advertises logging; reply `{}`.
- Anything else: `-32601`.
- **Progress:** a request with a `progressToken` reaches the child with the child request's id as
  its token; the child's progress for it is sent downstream with the client's token (values as
  floats, kept increasing past keepalives), related to that request (so it rides that POST's SSE
  stream). Progress for any other token is dropped.
- **Cancellation:** downstream `notifications/cancelled` cancels the request's handler; the child
  gets `notifications/cancelled` for its own request id (reason "caller cancelled"), the downstream
  request is settled without a response (the transport's `on_request_unanswered` callback) and a
  late child reply is dropped. A child's `notifications/cancelled` for a request it sent to the
  client is relayed the same way.
- **Child-to-client requests** (`roots/list`, `sampling/createMessage`, `elicitation/create`):
  forwarded downstream under the dispatcher's own id, related to an in-flight downstream request to
  that child when there is one; the client's response goes back. A child `ping` is answered locally.
- **Other child notifications** (logging, resource updates): forwarded, related to an in-flight
  request to that child if any.
- **list_changed:** on a child's `notifications/tools/list_changed` (and prompts/resources),
  re-list that child, revalidate, swap its part of the catalog, then send one downstream
  `notifications/tools/list_changed`, related to an in-flight downstream request to that child if
  any, else on the standalone GET stream. While a refresh for a child is pending (from the
  list_changed on, in receive order), responses to calls forwarded to that child are held and new
  requests wait for it, so the client never sees a response before the notification or calls a
  tool the aggregator does not know yet. A refresh that fails is logged `refresh_failed`; the
  previous catalog stays.
- **Child crash:** its in-flight requests fail with `-32603`, then the session is closed
  (`child_exited`, above).
- **No timeouts** on forwarded requests: a call may legitimately run for minutes (an import with
  analysis).

## Names

Prefixed tool and prompt names must match `^[a-zA-Z0-9_-]{1,64}$` and be unique across
children. At session open a violation fails the `initialize` with an error naming every
offending name; on a list_changed refresh it rejects that refresh (previous catalog kept) and
logs `catalog_rejected`. Never truncate or rename silently.

## Logging

JSON lines on stdout, one object per line, always `ts`, `level`, `event`:
`session_open` (session tag, children with pid), `session_close` (reason, duration, leftovers
killed), `session_refused` (reason), `session_evicted`, `get_stream_open` / `get_stream_closed`,
`on_ready`, `autosave` (session or `component: ghidra`, reason, outcome, duration_ms), `headless_state` (state, reason), `headless_idle_stop`, `evidence_dropped`,
`evidence_pruned`, `evidence_partial_removed`, `tmp_swept`, `ghidra_logs_pruned`, `project_size` (bytes, files; at each Ghidra start), `child_exit`, `tool_call` (session, tool, duration_ms,
outcome `ok` / `tool_error` / `rpc_error` / `cancelled`, error code), `catalog_rejected`,
`child_stderr` (session, child, line truncated to 2000 chars), `child_bad_line`, `child_line_too_long`,
`child_init_failed`, `refresh_failed`, `catalog_warmup` / `catalog_warmup_failed` (container-start
fill of a missing cache), `catalog_cold` (no cache, and the session could not get a complete listing; with the reason), `catalog_cache_written` / `catalog_cache_kept` / `catalog_cache_failed`,
`downstream_backpressure`, `transport_error`, `headless_exit` / `headless_start_timeout` /
`project_failed` / `rea_attach_install`, `internal_error`, `library_log` (a warning of the mcp library: logger, message,
exception type). **Never tool arguments or results.**

## Supervisor

1. Refuse to start (exit 2, one clear line) without a token, if `RE_WORKDIR` is missing or
   not writable, if `REBOX_STATE_DIR` (default `$RE_WORKDIR/re-box-state`; absolute) cannot be
   created or written (checked with a temp file) or if `GHIDRA_PROJECTS_DIR` (default
   `$REBOX_STATE_DIR/ghidra-projects`, any absolute path) has a path element starting with `.`
   (Ghidra refuses those). It creates `ghidra-projects/`, `home/`, `cache/` and `rea/evidence/`
   under the state dir as the running user (plus a `.gitignore` of `*` there if none exists, since
   the default sits in the work dir) and never chowns.
2. Export `HOME=$STATE/home`, `XDG_{CONFIG,DATA,STATE}_HOME` below it and `XDG_CACHE_HOME=$STATE/cache`
   (inherited by the JVM and every child). Unless `GHIDRA_MCP_AUTH_TOKEN` is already set (the test stage presets it), set it to a
   random token: ghidra-mcp's HTTP port 8089 then wants `Authorization: Bearer <token>` on every call except `/check_connection`
   and `/mcp/health`. It is made once per container start, so the bridges survive a JVM restart; the JVM, the bridges and REA's
   attach helper inherit it, and the supervisor's own calls send it. It is never logged or put in a `REBOX_*` variable.
   Set `REBOX_INSTANCE` to a random id in its own environment, unless it is already set (the
   test-stage runner sets it to find every process of the instance).
3. Before anything else runs, remove what killed processes left in the temp dir (`sweep_tmp`:
   `rea-*`, also in `rebox-rea-*/`, and `rebox-attach-locks-*/*.lock` (an emptied lock dir of ours too) only; logged as `tmp_swept`). No session exists yet,
   so none of it is in use. Then serve at once (the port opens; no JVM runs yet). The headless server is **on demand**: the app
   calls `want(True)` when a session is created and `want(False)` when the last one is gone. State:
   `stopped`, `starting`, `ready`, `failed` (crashed, backing off), `stopping` (idle stop),
   `shutting_down`. While wanted, start the headless server in its own process group:
   `java $JAVA_OPTS -Djava.awt.headless=true -Dghidra.home=$GHIDRA_INSTALL_DIR
   -Dapplication.name=GhidraMCP -classpath <jar>:<Ghidra/{Framework,Features,Processors}/*/lib/*.jar>
   com.xebyte.headless.GhidraMCPHeadlessServer --port 8089 --bind 127.0.0.1
   --project <projects>/rebox.gpr` (plus `-Duser.home=$STATE/home`), env minus `REBOX_*` plus
   `REBOX_INSTANCE`,
   with `GHIDRA_MCP_FILE_ROOT=$RE_WORKDIR:<REA's TMPDIR>` (`REBOX_REA_TMP`) and
   `GHIDRA_MCP_IMPORT_SCRIPT_ROOTS=<REBOX_REA_ATTACH_DIR>/import_scripts` forced; REA's attach script is copied to
   `$STATE/home/ghidra_scripts` at every start; if `rebox.gpr` is missing it is first created with
   `analyzeHeadless <dir> rebox -preScript ReboxNoop.java` (the HTTP API cannot create projects
   outside the file root); its output relayed as `headless_log` lines. When the JVM crashes, the supervisor also kills the
   attach helpers of the instance (processes running `rea-attach/helper.mjs` that carry its `REBOX_INSTANCE`), so
   REA reports the lost provider at once.
4. Wait for `GET http://127.0.0.1:8089/check_connection` to answer `{"status":"ok",...}`
   (`REBOX_HEADLESS_START_TIMEOUT`, 300 s). Then check the project (and log its size on disk as `project_size`): `GET /get_project_info`;
   if `has_project` is false (works only inside the file root), `POST /open_project {"path": "<dir>/rebox.gpr"}` (JSON body).
   The `.gpr` exists by then, so there is no create fallback.
5. `/healthz` is 200 with `"ghidra": "<state>"` when stopped, stopping, ready, or starting within
   `REBOX_HEADLESS_START_TIMEOUT`; 503 (with `reason`) when failed, shutting down, or starting
   past the timeout. `ready` means the last `check_connection` (polled every 10 s) succeeded and
   the project is open; every transition into ready (the first, and each recovery after a failed poll) calls the `on_ready` hook.
   Every `REBOX_AUTOSAVE_INTERVAL` seconds a separate task `POST`s `/save_all_programs`
   (sequential, never overlapping; logged as `autosave` with `component: ghidra` when something
   was saved or failed).
6. No session for `REBOX_GHIDRA_IDLE_TIMEOUT` seconds: `save_all`, then `exit_ghidra` -> SIGTERM ->
   SIGKILL like a shutdown, but without the instance-wide leftover sweep (it would kill a session
   opening concurrently). A session opening during the stop gets a second start once the old
   process has exited. A crash with sessions restarts with backoff (5 s doubling to 60 s; the last
   session leaving cancels the wait); a crash without sessions leaves it stopped.
7. At start, if a child with a `catalog_cache` has none for its current key, the server is started
   without a session, the child listed against it, the cache written and the server released to the
   normal idle stop (`warm_up`).
8. At start and daily: REA evidence bundles older than `REBOX_EVIDENCE_RETENTION_DAYS` are deleted,
   and rolled Ghidra logs (`application.log-*.log`, `script.log-*.log`) older than 30 days. At start
   only: `.rea-write-*` directories left in `rea/evidence/` by an export that was killed.
9. SIGTERM/SIGINT: at once `POST /save_all_programs` (concurrently with closing the sessions, so a
   short `docker stop` grace cannot lose it; waited for at most 30 s), refuse new sessions, close all sessions, `POST /exit_ghidra` (saves all
   programs), wait up to 60 s for the JVM, then SIGTERM/SIGKILL its group, then SIGKILL anything
   left with this `REBOX_INSTANCE`; exit 0. A second signal skips the waits. The sequence needs
   `docker stop` to allow 120 s (docs/OPERATIONS.md).
