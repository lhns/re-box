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
| `supervisor.py` | headless JVM lifecycle, Ghidra project, runs the app, signal handling |
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
(`/etc/rebox/aggregator.toml`).

Children (TOML), generic so more servers can be added without code changes:

```toml
[[child]]
name = "rea"                 # [a-z0-9-]+, used in logs and errors
prefix = "rea_"              # prepended to tool and prompt names
command = ["/usr/local/bin/rea", "mcp"]   # absolute path required
[child.env]                  # forced: overrides the container env
REA_LOG_LEVEL = "warn"
```

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
  Refused with HTTP 503 and JSON-RPC error `-32000` "re-box session limit reached (N of N
  active) ..." when the cap is reached, or while shutting down. Otherwise a session is created
  and its router handles the `initialize` request: spawn all children concurrently, `initialize`
  each (forwarding the client's `capabilities` and `clientInfo`, with the negotiated
  `protocolVersion`), send each `notifications/initialized`, fetch `tools/list` (all pages),
  `prompts/list` / `resources/list` / `resources/templates/list` where advertised, build the
  catalog, reply. A child that fails to start or initialize is logged and marked dead; the
  session still opens if at least one child is up. All down, or an invalid catalog (below): the
  `initialize` gets a JSON-RPC error and the session is discarded.
- **Era:** only the handshake protocol versions (2024-11-05 .. 2025-11-25). The reply uses the
  client's requested version if it is one of those, else 2025-11-25. A sessionless POST that is
  not `initialize`, or that carries an `MCP-Protocol-Version` header outside that set (2026-07-28
  `server/discover` probes), gets HTTP 400 with JSON-RPC error `-32022` and
  `data: {"supported": [...handshake versions...], "requested": <header or "">}`; clients fall back
  to `initialize`. GET/DELETE without a session id: 400. Unknown session id: 404, `-32001`.
- **Close:** on DELETE, idle timeout, supervisor shutdown, or once every child is dead: (1) close
  each child's stdin and wait up to 2 s; (2) SIGTERM its process group, wait `REBOX_CHILD_GRACE`;
  (3) SIGKILL the group; (4) SIGKILL any remaining process whose `/proc/<pid>/environ` holds
  this `REBOX_SESSION` and `REBOX_INSTANCE`. Then terminate the transport (later requests: 404).
- **Idle:** no downstream message for `REBOX_SESSION_IDLE_TIMEOUT` seconds and nothing in flight, or
  no downstream message for 4 x that while requests are in flight (a long call may keep a live client
  quiet; a client silent for 4x is gone).
- **Limits:** at most 64 in-flight requests per session (`-32000` "too many concurrent requests"),
  a duplicate in-flight request id gets `-32600`, child output lines are capped at 64 MiB, and
  notifications queued for a stalled client are dropped beyond 1000 (`downstream_backpressure`).
  The supervisor and aggregator set `PR_SET_DUMPABLE=0` so their own environment is not readable
  through `/proc` by same-uid processes; children are unaffected.
  An open GET stream does not keep a session alive: every REA session can hold a 2 GB JVM.

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
- **Child crash:** its in-flight requests fail with `-32603`; later calls to its tools likewise;
  other children keep working.
- **No timeouts** on forwarded requests; cold Ghidra starts take minutes.

## Names

Prefixed tool and prompt names must match `^[a-zA-Z0-9_-]{1,64}$` and be unique across
children. At session open a violation fails the `initialize` with an error naming every
offending name; on a list_changed refresh it rejects that refresh (previous catalog kept) and
logs `catalog_rejected`. Never truncate or rename silently.

## Logging

JSON lines on stdout, one object per line, always `ts`, `level`, `event`:
`session_open` (session tag, children with pid), `session_close` (reason, duration, leftovers
killed), `session_refused` (reason), `child_exit`, `tool_call` (session, tool, duration_ms,
outcome `ok` / `tool_error` / `rpc_error` / `cancelled`, error code), `catalog_rejected`,
`child_stderr` (session, child, line truncated to 2000 chars). **Never tool arguments or
results.**

## Supervisor

1. Refuse to start (exit 2, one clear line) without a token, if `RE_WORKDIR` is missing or
   not writable, if `GHIDRA_PROJECTS_DIR` (default `$RE_WORKDIR/ghidra-projects`) is outside
   `RE_WORKDIR` (`GHIDRA_MCP_FILE_ROOT` is `RE_WORKDIR`) or has a path element starting with `.`
   (Ghidra refuses those).
2. Set `REBOX_INSTANCE` to a random id in its own environment, unless it is already set (the
   test-stage runner sets it to find every process of the instance).
3. Start the headless server in its own process group:
   `java $JAVA_OPTS -Djava.awt.headless=true -Dghidra.home=$GHIDRA_INSTALL_DIR
   -Dapplication.name=GhidraMCP -classpath <jar>:<Ghidra/{Framework,Features,Processors}/*/lib/*.jar>
   com.xebyte.headless.GhidraMCPHeadlessServer --port 8089 --bind 127.0.0.1`, env minus `REBOX_*` plus
   `REBOX_INSTANCE`,
   with `GHIDRA_MCP_FILE_ROOT=$RE_WORKDIR` forced; its output relayed as `headless_log` lines.
4. Wait for `GET http://127.0.0.1:8089/check_connection` to answer `{"status":"ok",...}`
   (`REBOX_HEADLESS_START_TIMEOUT`, 300 s). Then ensure the project: `GET /get_project_info`;
   if `has_project` is false, `POST /open_project {"path": "<dir>/rebox.gpr"}` when it exists,
   else `POST /create_project {"parentDir": "<dir>", "name": "rebox"}` (JSON bodies).
5. Serve the app. `/healthz` is 200 only while the headless process runs, its last
   `check_connection` (polled every 10 s) succeeded and the project is open; else 503 with the
   reason.
6. If the headless server exits unexpectedly, restart it with backoff (5 s doubling to 60 s) and
   redo step 4; sessions stay up.
7. SIGTERM/SIGINT: refuse new sessions, close all sessions, `POST /exit_ghidra` (saves all
   programs), wait up to 60 s for the JVM, then SIGTERM/SIGKILL its group, then SIGKILL anything
   left with this `REBOX_INSTANCE`; exit 0. A second signal skips the waits.
