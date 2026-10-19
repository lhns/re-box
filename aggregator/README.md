# rebox-aggregator

One authenticated streamable-HTTP MCP endpoint (`/mcp`) in front of several stdio MCP servers,
with **one set of child processes per downstream MCP session**. Also contains the container's
supervisor, which runs the ghidra-mcp headless server next to the aggregator.

Why custom, and the protocol-era decision: `docs/DECISIONS.md`, "5.2".

## Modules

| module | role |
| --- | --- |
| `config.py` | settings from env, children from the TOML config |
| `children.py` | one stdio child: own process group, newline-delimited JSON-RPC, stderr relay, group kill |
| `catalog.py` | prefixing, name validation, merged tools/prompts/resources catalog |
| `session.py` | per-session router between the downstream transport and the children |
| `app.py` | ASGI app: auth, `/healthz`, `/mcp` session manager (cap, idle reaping, era rejection) |
| `supervisor.py` | headless JVM lifecycle (on demand), Ghidra project, evidence pruning, runs the app, signal handling |
| `evidence.py` | REA evidence bundles: drop an empty export, prune old ones |
| `log.py` | JSON-lines logging to stdout |

Entry points: `rebox-supervisor` (container default), `rebox-aggregator` (app only, no
headless server; used by unit tests and for development).

## Configuration

Env (container): `REBOX_TOKEN_FILE` (path to a file holding the token, trailing whitespace stripped;
preferred, because a process's environment is readable through `/proc/<pid>/environ` by other
processes of the same uid) or `REBOX_TOKEN` (the file wins; refuse to start with neither or an empty
token), `REBOX_INIT_TIMEOUT` (120 s, per child, covers spawn + `initialize` + listing; a child that
exceeds it is killed and counts as failed), `REBOX_MAX_BODY_BYTES` (16 MiB; larger POSTs get HTTP 413), `REBOX_PORT` (8443),
`REBOX_BIND` (0.0.0.0), `REBOX_TLS_CERT`/`REBOX_TLS_KEY` (both or neither), `REBOX_MAX_SESSIONS`
(4), `REBOX_SESSION_IDLE_TIMEOUT` (1800 s), `REBOX_CHILD_GRACE` (5 s), `REBOX_CONFIG`
(`/etc/rebox/aggregator.toml`), `REBOX_STATE_DIR` (the supervisor exports the resolved path; empty
disables autosave), `REBOX_AUTOSAVE_INTERVAL` (300 s, 0 = only on close), `REBOX_AUTOSAVE_TIMEOUT` (20 s),
`REBOX_SESSION_ORPHAN_TIMEOUT` (300 s: idle limit of a session with no open GET stream; 0 = same as the idle
timeout), `REBOX_SESSION_EVICT_MIN_IDLE` (120 s), `REBOX_PROGRESS_KEEPALIVE_INTERVAL` (30 s, 0 = off).
Supervisor only: `REBOX_GHIDRA_IDLE_TIMEOUT` (300 s, 0 = never stop), `REBOX_EVIDENCE_RETENTION_DAYS` (30, 0 = keep),
`REBOX_HEADLESS_START_TIMEOUT` (300 s).

Children (TOML), generic so more servers can be added without code changes:

```toml
[[child]]
name = "rea"                 # [a-z0-9-]+, used in logs and errors
prefix = "rea_"              # prepended to tool and prompt names
command = ["/usr/local/bin/rea", "mcp"]   # absolute path required
deny = ["run_script"]        # optional: the child's own tool names to hide and refuse
[child.env]                  # forced: overrides the container env
REA_LOG_LEVEL = "warn"
[child.autosave]             # optional: a child tool that persists its work
tool = "export_evidence_bundle"   # the child's own (unprefixed) tool name
arguments = { path = "{state_dir}/rea/evidence/{session}.json", overwrite = true }  # {state_dir}, {session} substituted
prune_empty = true           # optional: delete the file again if it is an evidence bundle with no records/unknowns
[child.on_ready]             # optional: a child tool called when the Ghidra server becomes ready after the child booted
tool = "connect_instance"
arguments = { project = "rebox" }
```

**Deny list:** `deny` names tools by the child's own (unprefixed) names. They are dropped from
`tools/list` (again after every `list_changed` refresh), and a `tools/call` to one fails with
`-32602` "Tool gh_x is disabled by this re-box's configuration" without reaching the child. The
shipped config denies ghidra-mcp's `run_ghidra_script` and `run_script_inline`.

**on_ready:** the Ghidra server starts with the first session, so a bridge usually boots before it
and shows only static tools. When the server becomes ready (and the client has sent
`notifications/initialized`), the aggregator calls this tool on every session's child over the same
bypass path as autosave; the bridge connects and sends `tools/list_changed`, which is relayed. A
session that opens while Ghidra is already up gets no call. Logged as `on_ready` (`outcome`
ok/tool_error/failed); the bridge's own 15 s retry is the fallback.

Autosave runs when `REBOX_STATE_DIR` is set, for children that served a `tools/call` since their
last save (dirty): on session close (all reasons, concurrently, before the children are closed,
each bounded by `REBOX_AUTOSAVE_TIMEOUT`) and every `REBOX_AUTOSAVE_INTERVAL` seconds while no
request is in flight to that child. It never counts as session activity. Logged as `autosave`
(`reason`, `outcome` ok/tool_error/rpc_error/timeout, `duration_ms`); failures only warn.

A child's environment is the container env **minus every `REBOX_*` variable** (the token is not
handed to children or the targets REA executes; a same-uid process could still read a token
file, so the container, not the uid, is the boundary: docs/OPERATIONS.md, "Security"), plus `[child.env]`, plus `REBOX_INSTANCE` (the
supervisor's random instance id) and `REBOX_SESSION` (the session tag, below).

## Session lifecycle

- **Session tag** = first 16 hex chars of `sha256(Mcp-Session-Id)`. Logs carry the tag, never
  the id. Every child gets it as `REBOX_SESSION`, inherited by its descendants (REA's Ghidra
  JVMs), which is how cleanup and the e2e test find a session's processes without PID-namespace
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
  catalog, reply (the result carries `instructions`: state dir, this session's tag and evidence
  file, gh_ vs rea_ persistence, the path rule, the cold-start note). It does not wait for Ghidra.
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
  gets 404 and it re-initializes): (0) autosave
  the live children; (1) close
  each child's stdin and wait up to 2 s; (2) SIGTERM its process group, wait `REBOX_CHILD_GRACE`;
  (3) SIGKILL the group; (4) SIGKILL any remaining process whose `/proc/<pid>/environ` holds
  this `REBOX_SESSION` and `REBOX_INSTANCE`. Then terminate the transport (later requests: 404).
- **Idle:** no downstream message for `REBOX_SESSION_IDLE_TIMEOUT` seconds and nothing in flight, or
  no downstream message for 4 x that while requests are in flight (a long call may keep a live client
  quiet; a client silent for 4x is gone). A session whose client holds its standalone GET stream
  open (the MCP SDKs open it after `notifications/initialized`) keeps the full timeout; one with no
  open GET stream is probably gone (`claude -p` exited without DELETE) and uses
  `REBOX_SESSION_ORPHAN_TIMEOUT` instead (close reason `orphaned`), so its REA JVM does not linger.
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
The aggregator is the client of each child and keeps its own request-id counter per child.

- `initialize`: above. `notifications/initialized` from downstream: swallowed.
- `ping`: answered locally.
- `tools/list`: the merged catalog, one page. `prompts/list`, `resources/list`,
  `resources/templates/list`: likewise.
- `tools/call`, `prompts/get`: strip the prefix, forward to the owning child with the original
  name, everything else in `params` unchanged (including `_meta.progressToken`). Unknown name:
  JSON-RPC `-32602` "Unknown tool: X". Dead child: `-32603` "backend 'rea' is not running in
  this session (exited with code N)".
- `resources/read`, `resources/subscribe`, `resources/unsubscribe`: routed by URI to the child
  that listed it (or whose template's static prefix matches). Resource URIs are not rewritten;
  resource and template `name`s get the prefix.
- `completion/complete`: `ref/prompt` by prefixed name, `ref/resource` by URI.
- `logging/setLevel`: sent to every live child that advertises logging; reply `{}`.
- Anything else: `-32601`.
- **Progress:** child `notifications/progress` is sent downstream as related to the downstream
  request that carried that `progressToken` (so it rides that POST's SSE stream).
- **Cancellation:** downstream `notifications/cancelled` is translated to the child's request id
  and forwarded; the downstream request is then settled without a response (the transport's
  `on_request_unanswered` callback) and a late child reply is dropped. A child's
  `notifications/cancelled` for a request it sent to the client is mapped the same way.
- **Child-to-client requests** (`roots/list`, `sampling/createMessage`, `elicitation/create`):
  forwarded downstream under a new id, related to an in-flight downstream request to that child
  when there is one; the client's response is mapped back. A child `ping` is answered locally.
- **list_changed:** on a child's `notifications/tools/list_changed` (and prompts/resources),
  re-list that child, revalidate, swap its part of the catalog, then send one downstream
  `notifications/tools/list_changed`, related to an in-flight downstream request to that child if
  any, else on the standalone GET stream. While a refresh for a child is pending, that child's
  responses are held and new `tools/list` / `tools/call` wait for it, so the client never sees a
  response before the notification or calls a tool the aggregator does not know yet.
- **Child crash:** its in-flight requests fail with `-32603`, then the session is closed
  (`child_exited`, above).
- **No timeouts** on forwarded requests; cold Ghidra starts take minutes.

## Names

Prefixed tool and prompt names must match `^[a-zA-Z0-9_-]{1,64}$` and be unique across
children. At session open a violation fails the `initialize` with an error naming every
offending name; on a list_changed refresh it rejects that refresh (previous catalog kept) and
logs `catalog_rejected`. Never truncate or rename silently.

## Logging

JSON lines on stdout, one object per line, always `ts`, `level`, `event`:
`session_open` (session tag, children with pid), `session_close` (reason, duration, leftovers
killed), `session_refused` (reason), `session_evicted`, `get_stream_open` / `get_stream_closed`,
`on_ready`, `headless_state` (state, reason), `headless_idle_stop`, `evidence_dropped`,
`evidence_pruned`, `child_exit`, `tool_call` (session, tool, duration_ms,
outcome `ok` / `tool_error` / `rpc_error` / `cancelled`, error code), `catalog_rejected`,
`child_stderr` (session, child, line truncated to 2000 chars). **Never tool arguments or
results.**

## Supervisor

1. Refuse to start (exit 2, one clear line) without a token, if `RE_WORKDIR` is missing or
   not writable, if `REBOX_STATE_DIR` (default `$RE_WORKDIR/re-box-state`; absolute) cannot be
   created or written (checked with a temp file) or if `GHIDRA_PROJECTS_DIR` (default
   `$REBOX_STATE_DIR/ghidra-projects`, any absolute path) has a path element starting with `.`
   (Ghidra refuses those). It creates `ghidra-projects/`, `home/`, `cache/` and `rea/evidence/`
   under the state dir as the running user (plus a `.gitignore` of `*` there if none exists, since
   the default sits in the work dir) and never chowns. `GHIDRA_MCP_FILE_ROOT` stays
   `RE_WORKDIR`.
2. Export `HOME=$STATE/home`, `XDG_{CONFIG,DATA,STATE}_HOME` below it and `XDG_CACHE_HOME=$STATE/cache`
   (inherited by the JVM and every child). Set `REBOX_INSTANCE` to a random id in its own environment, unless it is already set (the
   test-stage runner sets it to find every process of the instance).
3. Serve at once (the port opens; no JVM runs yet). The headless server is **on demand**: the app
   calls `want(True)` when a session is created and `want(False)` when the last one is gone. State:
   `stopped`, `starting`, `ready`, `failed` (crashed, backing off), `stopping` (idle stop),
   `shutting_down`. While wanted, start the headless server in its own process group:
   `java $JAVA_OPTS -Djava.awt.headless=true -Dghidra.home=$GHIDRA_INSTALL_DIR
   -Dapplication.name=GhidraMCP -classpath <jar>:<Ghidra/{Framework,Features,Processors}/*/lib/*.jar>
   com.xebyte.headless.GhidraMCPHeadlessServer --port 8089 --bind 127.0.0.1
   --project <projects>/rebox.gpr` (plus `-Duser.home=$STATE/home`), env minus `REBOX_*` plus
   `REBOX_INSTANCE`,
   with `GHIDRA_MCP_FILE_ROOT=$RE_WORKDIR` forced; if `rebox.gpr` is missing it is first created with
   `analyzeHeadless <dir> rebox -preScript ReboxNoop.java` (the HTTP API cannot create projects
   outside the file root); its output relayed as `headless_log` lines.
4. Wait for `GET http://127.0.0.1:8089/check_connection` to answer `{"status":"ok",...}`
   (`REBOX_HEADLESS_START_TIMEOUT`, 300 s). Then check the project: `GET /get_project_info`;
   if `has_project` is false (fallback, works only inside the file root), `POST /open_project {"path": "<dir>/rebox.gpr"}` when it exists,
   else `POST /create_project {"parentDir": "<dir>", "name": "rebox"}` (JSON bodies).
5. `/healthz` is 200 with `"ghidra": "<state>"` when stopped, stopping, ready, or starting within
   `REBOX_HEADLESS_START_TIMEOUT`; 503 (with `reason`) when failed, shutting down, or starting
   past the timeout. `ready` means the last `check_connection` (polled every 10 s) succeeded and
   the project is open; each transition to ready calls every session's `on_ready` hook.
   Every `REBOX_AUTOSAVE_INTERVAL` seconds a separate task `POST`s `/save_all_programs`
   (sequential, never overlapping; logged as `autosave` with `component: ghidra` when something
   was saved or failed).
6. No session for `REBOX_GHIDRA_IDLE_TIMEOUT` seconds: `save_all`, then `exit_ghidra` -> SIGTERM ->
   SIGKILL like a shutdown, but without the instance-wide leftover sweep (it would kill a session
   opening concurrently). A session opening during the stop gets a second start once the old
   process has exited. A crash with sessions restarts with backoff (5 s doubling to 60 s; the last
   session leaving cancels the wait); a crash without sessions leaves it stopped.
7. REA evidence bundles older than `REBOX_EVIDENCE_RETENTION_DAYS` are deleted at start and daily.
8. SIGTERM/SIGINT: at once `POST /save_all_programs` (concurrently with closing the sessions, so a
   short `docker stop` grace cannot lose it; waited for at most 30 s), refuse new sessions, close all sessions, `POST /exit_ghidra` (saves all
   programs), wait up to 60 s for the JVM, then SIGTERM/SIGKILL its group, then SIGKILL anything
   left with this `REBOX_INSTANCE`; exit 0. A second signal skips the waits.
