# Decisions

Why things are the way they are: the outcomes of the early spikes and every place the build
departs from the original design brief (not part of this repo), with the evidence. The `5.x`
numbers in headings are kept as stable references. Upstream refs: Ghidra `12.1.4`
(`ghidra_12.1.4_PUBLIC_20260921.zip`), ghidra-mcp `v7.0.0-rc.3` (`295e3ad`), rea-agents `6.7.0`
(git tag `rea-agents-6.7.0`, `27dc42d`). Measured 2026-10-19.

## 5.1 One Ghidra install for both: yes, 12.1.4

**Decision:** one install at `/opt/ghidra`, Ghidra 12.1.4, used by REA and ghidra-mcp.

- REA's check is not exact-version. `src/ghidra/GhidraReleasePolicy.ts` (6.7.0):
  `SUPPORTED_GHIDRA_VERSION = "12.1.4"`; `ghidraReleaseCompatibility()` returns `verified` for
  12.1.4, `compatible` for any other 12.1.x, `unsupported` otherwise. Only `verified` and
  `compatible` are accepted (`ghidraReleaseAccepted`). 12.1.4 is the verified build; `rea doctor`
  reports Ghidra, its JDK 21 and the native decompiler healthy against the install.
- ghidra-mcp `v7.0.0-rc.3` declares `<ghidra.version>12.1.4</ghidra.version>` in `pom.xml`.
  `gradle buildExtension -x test -PGHIDRA_INSTALL_DIR=/opt/ghidra` (Gradle 9.8.0, the version in
  upstream's wrapper) builds `GhidraMCP-7.0.0.jar` and the extension zip against the install;
  no Maven `.m2` step. `v6.0.0` also builds against 12.1.4 with Gradle 9.6.1.
- End to end on the shared install: REA `open_binary`, `binary_overview` and `analyze_function`;
  ghidra-mcp `import_file`, `find_functions`, `get_functions(fields=decompiled_code)`,
  `rename_function`, `save_program`; the rename survives a server restart.

## ghidra-mcp ref: v7.0.0-rc.3, not v6.0.0

v6.0.0 is the newest stable tag but cannot serve the flow headless:

```
--> import_file {'file_path': '/work/hello_stripped'}
{"error":"Import requires GUI mode (PluginTool not available)"}
```

v7 serves `import_file` headless (`ProjectProgramProvider.importFile`), puts the project
operations behind `GHIDRA_MCP_FILE_ROOT`, makes lazy tool groups the default and reads
`GHIDRA_MCP_LAZY`, and targets 12.1.4. Cost: it is a release candidate. `versions.env` pins the
tag (for Renovate) and the commit (checked at build, since tags move); Renovate proposes
`v7.0.0` once it exists.

**Decided: ship rc.3 now.** v6.0.0 cannot do the one thing re-box needs from ghidra-mcp (headless
import), so waiting means shipping nothing; the image is pinned by commit, so rc.3 cannot change
under us. The Renovate PR to 7.0.0 is reviewed against the e2e suite when it lands (tool-name
changes between rc and final are the likely breakage; the e2e names every `ghidra_` tool it uses).

Tool names that changed in v7 (upstream wins over the earlier names,
`docs/project-management/MIGRATION_7.0.0_TOOL_CONSOLIDATION.md`): `decompile_function` is now
`get_functions(fields="decompiled_code")`, `list_methods` is `list_program_items(kind="methods")`
or `find_functions`.

## 5.2 Aggregator: custom

**Status:** accepted; the dispatcher section below revises how it is built.

No existing project meets the ten requirements (per-session children with a process group each,
prefixing that fails loud at the 64-character name limit, `list_changed`, progress and cancel
relay, timeouts, prompts and resources, bearer token plus `/healthz`, a session cap, idle
reaping), evaluated 2026-10-19 from source, nothing run. The closest, supergateway (TypeScript),
lacks the session cap, fail-loud name validation and prompt/resource prefixing; FastMCP defaults
to a fresh backend session per request and has no cancel relay; the rest share one child between
sessions, need a database or OAuth, or are not stdio-to-HTTP at all (mcp-proxy variants,
1mcp, MetaMCP, IBM mcp-context-forge, docker/mcp-gateway, microsoft/mcp-gateway, mcp-hub, mcpo).
Closing supergateway's gaps in a fork costs about as much as a small aggregator we own, in Python
with the official SDK. Hence `aggregator/`.

### Protocol era

MCP 2026-07-28 is sessionless (no `initialize`, no `Mcp-Session-Id`), so it cannot carry
per-session children. The aggregator speaks only the handshake era (2024-11-05 to 2025-11-25)
and answers anything else with `-32022` listing those versions, which clients take as "fall
back to `initialize`" (python-sdk 2.3.0 `mcp/client/_probe.py`). Verified with Claude Code
2.1.303 against a prototype: a sessionless `server/discover` gets `-32022`, then the client
sends `initialize` and carries on with a session id (standalone GET stream, `tools/list`,
`tools/call` with a `progressToken`).

The SDK's legacy `StreamableHTTPServerTransport` serves each session; the aggregator routes raw
JSON-RPC messages (params and results stay plain dicts, so `structuredContent` is never
re-modelled). Children are spoken to over our own stdio transport so each starts in its own
process group.

### JSON-RPC plumbing: mcp's `JSONRPCDispatcher`

**Status:** accepted.

**Context.** The first aggregator correlated request ids, cancellation and progress tokens by hand
on both sides: more code, and the corner cases (late replies, cancels crossing replies) were ours.

**Decision.** mcp 2.3.0's `JSONRPCDispatcher` runs both sides of a session: the downstream
transport, and each child as its client. It owns ids, correlation, cancellation and progress;
every child line is validated as a JSON-RPC message. Stays hand-written, because the SDK has no
notion of it: spawning in a process group, the stderr relay, the 64 MiB line cap, an unbounded
stdin queue (nothing waits on a child that does not read), holding a response behind a pending
`list_changed` refresh, duplicate-id `-32600`, `MAX_INFLIGHT`, `-32603` for unknown exceptions in
a request handler, and the process-group kill plus the `REBOX_INSTANCE` sweep.

**Consequences.** Validation costs about 0.1 s per 3 MB `tools/list`. An invalid child line is
logged (`child_bad_line`) and dropped, not fatal. We follow the SDK's dispatcher semantics and
version (`aggregator/uv.lock`), so an mcp upgrade is checked against the aggregator tests.

## 5.3 Tool catalog size

`tools/list` serialized as compact JSON, ghidra-mcp v7.0.0-rc.3, rea-agents 6.7.0:

| catalog | tools | bytes |
| --- | --- | --- |
| REA, schema profiles full/full (upstream default) | 207 | 3,158,382 |
| REA, input compact, output full | 207 | 2,576,441 |
| REA, input full, output off | 207 | 801,177 |
| REA, input compact, output off | 207 | 219,236 |
| ghidra-mcp lazy (default groups) | 75 | 102,801 |
| ghidra-mcp `--no-lazy` | 197 | 246,517 |
| merged, REA full/full + gh lazy | 282 | 3,262,225 |
| merged, REA full/full + gh no-lazy | 404 | 3,406,307 |
| merged, REA compact/off + gh lazy | 282 | 323,079 |
| merged, REA compact/off + gh no-lazy | 404 | 467,161 |

REA also has 6 prompts and no resources; ghidra-mcp has neither. The two catalogs share 5 names
(`set_bookmark`, `list_bookmarks`, `search_strings`, `set_comment`, `list_strings`), which the
prefixes separate. The lazy default groups already contain every tool the e2e ghidra flow needs.

**Decision (revised):** ghidra-mcp runs **not lazy** (`GHIDRA_MCP_LAZY=0`, 197 tools, +144 KB). The
first decision kept it lazy and relied on `tools/list_changed`; but Claude Code 2.1.303 ignored
`list_changed` in a `claude -p` test (tools added by `ghidra_load_tool_group` never appeared: "No such
tool available") and there are open reports of the same over Streamable HTTP and with tool search.
With on-demand Ghidra a cold session would have no `ghidra_` tools at all, so the full catalog is in the
first `tools/list` (Claude Code defers tool definitions via tool search, so the size costs little);
lazy mode remains an opt-in (`GHIDRA_MCP_LAZY=1`; the e2e lazy test runs only then). The aggregator
still relays `list_changed`. Keep REA's profiles at upstream `full`, exposed as
`REA_MCP_INPUT_SCHEMA_PROFILE` / `REA_MCP_OUTPUT_SCHEMA_PROFILE`. REA's full catalog is 3 MB;
docs/CLIENTS.md says when to switch to `compact`/`off`.

**Prefix:** `ghidra_` instead of the original `gh_`: `gh` reads as GitHub. Longest tool name:
`ghidra_server_admin_terminate_all_checkouts` (43 chars); with Claude Code's `mcp__re-box__` it
is 55 of the API's 64, so a server name in clients longer than 9 characters would push it over.

## Other departures from the original design

- **No dot-prefixed project path** (the design had `.ghidra-projects`): Ghidra refuses
  `ERROR Error creating project: Path element starting with '.' is not permitted`. The projects
  dir is `$REBOX_STATE_DIR/ghidra-projects` ("State dir" below), so the rule covers every element
  of the state dir path, e.g. a host path under `~/.cache` fails at start with one line.
- **ghidra-mcp extension install:** not needed. The headless server is
  `com.xebyte.headless.GhidraMCPHeadlessServer` run from `GhidraMCP-<v>.jar` plus Ghidra's
  jars on the classpath, which is what upstream's `docker/entrypoint.sh` does; the extension zip
  is for the GUI plugin. Upstream's Dockerfile also unpacks the bundled Jython extension; we do
  the same so scripts work if `GHIDRA_MCP_ALLOW_SCRIPTS=1`.
- **Two Python environments:** the ghidra-mcp bridge pins `mcp>=1.28.1,<2`; the aggregator uses
  `mcp` 2.3.0. Each runs from its own venv by absolute path.
- **Supervision: tini + `rebox-supervisor`, not s6-overlay.** The in-cluster build runs the test
  stage under rootless BuildKit with no init and no PID namespace, so the test stage must start the
  services itself; s6-overlay needs to be PID 1. One supervisor (Python, part of `aggregator/`)
  serves both: dependency order (aggregator after the headless server and its project are ready),
  readiness, headless restart with backoff, and the SIGTERM order (sessions closed, then
  `/exit_ghidra`, which saves all programs). The test stage exercises exactly the production
  start path.
- **Image user.** The image's `USER` is `1000:1000`, so it runs non-root under plain `docker run`
  and Kubernetes `runAsNonRoot`. The PUID/PGID remap needs root, so it applies only when the
  container is started as root (compose example: `user: "0:0"` plus `CHOWN`, `SETUID`, `SETGID`);
  the entrypoint then drops to the remapped user with `setpriv --no-new-privs`. The e2e suite
  runs in the remap mode (uid 4321); the uid-1000 mode is checked in `tests/smoke/image.sh` (no
  remap, warning, writable HOME fallback).
- **The e2e finds a session's processes** by the `REBOX_SESSION` tag every child inherits and
  by their process groups, never by scanning for process names, because the in-cluster build has
  no PID isolation. Reading another uid's `/proc/<pid>/environ` needs `CAP_SYS_PTRACE`, which
  Docker does not grant root, so the e2e tests run as the PUID user.
- **Pins outside versions.env.** Base images are pinned tag@digest in the `FROM` lines (a `FROM`
  cannot read a file; Renovate's dockerfile manager tracks them). Package trees are lockfiles with
  hashes: `aggregator/uv.lock`, `deps/rea/package-lock.json` (sha512 integrity for all 174
  packages instead of `npm i -g`, which would float transitive versions), `deps/tools/requirements-*.txt`,
  `deps/build/requirements-*.txt` and ghidra-mcp's own `uv.lock` exported with hashes for the
  bridge venv. `REA_VERSION` must equal the locked rea-agents version; the build and
  `check-pins.py` refuse a mismatch.
- **npm install scripts are skipped** (`npm ci --ignore-scripts`). The only one is frida's, which
  downloads an unpinned prebuilt binary; REA's Frida features therefore report unavailable.
- **Tools** (deps/tools/install-tools.sh): apktool 2.12.1 from upstream instead of Ubuntu's
  2.7.0 (in REA's CVE-2024-21633 advisory window); binwalk 3.1.0 built from its pinned commit
  (3.x ships no binaries, Ubuntu has 2.3.4); Chrome for Testing instead of Ubuntu's chromium snap;
  mitmproxy's standalone build, the one REA verifies. `unar` (pulls in GNUstep) and pwndbg's
  `ziglang` (optional command only) are left out. `rea doctor` / `rea providers` report only the
  Ghidra, JADX and the commercial/macOS providers, not the CLI tools, so `tests/smoke/tools.sh`
  proves each tool through the REA command that drives it and checks the provider named in the
  Evidence.
- **ghidra-mcp upstream tests:** the Python offline tier (`tests/offline/` and the read-only
  integration suite against upstream's fake server) runs at the pinned ref in the
  `ghidra-mcp-test` stage. The Java unit tests are not run: they resolve junit/mockito from Maven
  Central without verification.
- **REA's upstream verification lanes** are skipped: its `verify:*` scripts need a source
  checkout and its dev dependencies; they do not run against the installed package.

## Findings for clients

- **Python SDK 2.x clients** cap one SSE event at 1 MiB by default (`max_sse_event_size`). REA's
  full-profile `tools/list` is ~3 MB in one event, so such a client fails `list_tools` unless it
  raises the cap or the container uses `REA_MCP_INPUT_SCHEMA_PROFILE=compact` and
  `REA_MCP_OUTPUT_SCHEMA_PROFILE=off`. The aggregator does not paginate `tools/list`: clients that
  do not follow `nextCursor` would silently lose tools. Claude Code 2.1.303 handles the full catalog.
- **Claude Code `-p` exits without DELETE**, so its session (and REA's open binary) lives until the
  orphan timeout and counts against `REBOX_MAX_SESSIONS` until then or until evicted
  ("Session timeouts" below).

## Ghidra on demand

**Status:** partly superseded: the "separate JVMs" half by "REA attaches to the ghidra-mcp JVM"
below. The rest stands.

**Decision:** the ghidra-mcp headless server (shared, one at most) starts when the first session
opens and stops `REBOX_GHIDRA_IDLE_TIMEOUT` (300 s) after the last one closed; an idle container
runs no Java (measured: the idle server is 128 MB RSS, ready 39 s after start).

- **Start on the first session, not the first `ghidra_` call:** a bridge that boots without Ghidra
  shows only static tools, so the client would never see a `ghidra_` tool to call. `initialize` is not
  held when a catalog cache exists (39 s against client connect timeouts); the bridge starts at once,
  and when Ghidra is ready the aggregator calls the bridge's `connect_instance(project="rebox")`
  (`[child.on_ready]`), which discovers the server (matches project `rebox`, over TCP 8089). The
  bridge's own 15 s retry stays as fallback.
- **Clients cannot be relied on to follow `tools/list_changed`** (Claude Code ignored it in a test),
  so the full `ghidra_` catalog is in the first `tools/list` regardless: a per-child catalog cache
  (`[child.catalog_cache]`, `$REBOX_STATE_DIR/cache/rebox-catalog/`, keyed by the ghidra-mcp
  version, `GHIDRA_MCP_LAZY` and `GHIDRA_MCP_ALLOW_SCRIPTS`) holds what a connected bridge lists.
  A session that opens while Ghidra is down advertises the cache; a `tools/call` for a cached tool
  the bridge does not have yet is held until the live catalog replaces the cache, bounded by
  `REBOX_HEADLESS_START_TIMEOUT`, and returns a tool error naming the Ghidra state and reason on
  timeout or failure (texts: `[session]` in `aggregator.toml`). The bridge's static tools stay
  callable immediately. With no cache yet (new state dir, or an upgrade changed the key) the
  supervisor starts Ghidra at container boot, lists the bridge once exactly as a session would,
  writes the cache, and lets the idle stop take Ghidra down; a session opening before that holds
  its `initialize` reply until its bridge is connected (bounded).
- **Stop = save, then the shutdown escalation, but no instance-wide sweep** (it would kill a session
  opening concurrently). A session opening during the stop waits for the old process to exit, then
  starts a new one. A crash with sessions restarts with backoff; without sessions it stays stopped.
- **`/healthz` is the aggregator, not the JVM:** 200 when Ghidra is stopped, starting or ready,
  503 when failed or starting past `REBOX_HEADLESS_START_TIMEOUT`; the body names the state.
- **G1 flags** (`G1PeriodicGCInterval=60000`, `MinHeapFreeRatio=10`, `MaxHeapFreeRatio=30`) hand heap
  back after a burst.

## REA attaches to the ghidra-mcp JVM ("route 1")

**Status:** accepted. Supersedes the separate-JVMs decision of the first on-demand design: REA
never starts a Ghidra JVM of its own.

**Context.** REA started one Ghidra JVM per session (AnalyzeHeadless on a throwaway project,
`-deleteProject`), next to ghidra-mcp's JVM on the persistent `rebox.gpr`: N+1 JVMs, 1.5 GB each,
and REA's annotations lost at session end (only the evidence export survived). One JVM for both
was not possible without changing REA: it launches `AnalyzeHeadless` itself, its bridge needs
the `HeadlessAnalyzer` singleton of its own JVM, and it kills its JVM's process group on cancel.

**Decision.** REA's bridge (`ReaGhidraBridge.java`, the GhidraScript that serves REA's protocol
over a unix socket) runs on a thread inside the ghidra-mcp JVM, against a program in `rebox.gpr`.
- `deps/rea-attach/patch-rea.py` patches REA at image build: its launcher spawns `helper.mjs`
  instead of a JVM, with the AnalyzeHeadless arguments it would have used. REA's descriptor,
  token, sha check, process ownership and cleanup are unchanged.
- The helper maps that whole command line onto ghidra-mcp's `/import_file`: REA's verified
  target snapshot (REA's `TMPDIR`, `/tmp/rebox-rea-<uid>`, is a second ghidra-mcp file root), loader,
  loader options, processor and compiler spec, REA's pre- and post-scripts (raw and COM
  preparation, seeds, analyzer options, no-return fix, MIPS soft-float storage), into
  `/rea/<sha256[:12]>-<profile digest[:8]>/<file name>`. A program already there with the same
  bytes is reused; the agent's own `ghidra_` import of the file never is (other options, no REA
  scripts). It refuses an argument it does not know, so a new REA option fails loudly.
- Then it starts the bridge through `/run_ghidra_script` (`ReboxReaAttach.java`) and lives as
  long as the bridge.
- Bridge patches (on a copy, also the scripts' directory): `analysisTimeoutOccurred()` returns
  false (no HeadlessAnalyzer here; the import finished analysis first), the session's default
  address space is per thread instead of static, and edits run under ghidra-mcp's write lock.
- ghidra-mcp is patched at build too (`deps/ghidra-mcp/patches`): 0001 is upstream c30b7a74
  (every write takes the headless write lock); 0002 adds the import options and scripts, several
  file roots, `GHIDRA_MCP_IMPORT_SCRIPT_ROOTS`, a JVM-wide script-load lock and
  `DirectThreadingStrategy.writeLock()`. Upstream versions against `dev`: `deps/ghidra-mcp/upstream/`.

Rejected:
- REA opening `rebox.gpr` itself: blocked by the project lock while ghidra-mcp runs; a Ghidra
  Server adds a third JVM and check-out/check-in.
- Dropping REA's Ghidra provider: loses REA's Ghidra workflows.
- ghidra-mcp per session like REA: loses the shared project, N x 4 GB, a cold start per session,
  and still needs the attach work.
- Unmodified bridge in the shared JVM: it calls `analysisTimeoutOccurred()`, which needs a
  HeadlessAnalyzer, and keeps the address space in a static field.
- Keeping REA's own JVM for the profiles ghidra-mcp's import could not express (the first
  version did): two code paths, a second JVM with its own memory budget, and edits on those
  targets lost at session end. Extending the import was a contained patch.
- Running import scripts as any path a client names: with scripts enabled for the bridge, that
  would hand agents arbitrary code through `ghidra_import_file` past the script-tool deny list.

**Consequences.**
- One Ghidra JVM; REA's edits persist in the shared project (autosave, shutdown save) and are
  visible to ghidra_ tools and other sessions. REA's own texts ("ephemeral", `project_ephemeral`,
  `persists_after_close: false`) are stale under re-box. A target the agent also imports with
  `ghidra_import_file` is analyzed twice (once per folder). Inside Ghidra such a program keeps the
  snapshot's name (`target-<sha256[:12]>.<ext>`, e.g. in `ghidra_list_open_programs`); the project
  file, which `program` selectors resolve, is named after the original file.
- ghidra-mcp runs with scripts enabled; `run_ghidra_script`/`run_script_inline` (and
  `delete_project`, which would drop the shared project) are denied in the aggregator, and import
  scripts must lie in `/opt/rebox/rea-attach/import_scripts`: REA's six pre- and post-scripts,
  nothing else. Any client can name those through `ghidra_import_file`, so the bridge lives
  elsewhere: run as an import post-script with a descriptor a client wrote, it would load the jars
  the descriptor names (`analysis_extensions`). patch-rea.py classifies every file REA ships and
  fails on a new one. `GHIDRA_MCP_IMPORT_SCRIPT_ROOTS` set but naming no directory refuses all
  import scripts. The supervisor copies the host script into `$REBOX_STATE_DIR/home/ghidra_scripts`
  at every JVM start: `/run_ghidra_script` runs scripts only from there. That script must stay
  free of compile-time references to `ghidra.app.plugin.core.osgi` and ghidra-mcp's classes (bnd
  then imports the osgi package twice, Felix refuses the bundle, and every script in the directory
  stops loading, silently in headless mode); it reaches ghidra-mcp through the system class loader.
  Port 8089 is token-protected ("The 8089 token" below).
- REA's cancel and cleanup kill the helper's process group, never the JVM; the bridge thread
  ends with the helper (a watchdog in the host script), the helper ends with the bridge (its
  socket) or the JVM (and the supervisor kills helpers when the JVM exits). REA then reports
  "provider exited, reopen the binary", as it did for a crashed JVM of its own. An idle stop
  cannot happen while a session, hence a REA binary, is open.
- Ghidra merges concurrent transactions into one, so an aborted edit discards the other side's
  work (measured; ghidra-mcp's `WriteTx` even joins an open transaction on purpose). With patch
  0001 every ghidra_ write holds the server's write lock; the bridge holds the same lock around its
  edits and starts one only when no other transaction is open (it releases the lock while it
  waits, at most 60 s, then fails the request as `program_busy` rather than joining). Import
  post-scripts and analysis options run under the lock too. Transactions that take no lock remain
  (auto-analysis, script runs): a REA edit waits for them rather than joining.
- REA assumes nobody else edits its program: the bridge keeps name, procedure and string
  inventories until its own edit invalidates them, and REA's node side replays analysis results
  per target ("snapshot" cache policy). Attached, both would hide edits made by ghidra_ tools or other
  sessions. The patched bridge drops its inventories whenever the program's modification number
  moved since its last request, and the patched chunk asks REA for its "live" policy (the one it
  uses for IDA and Kuna) while the attach helper is configured.
- REA's `close_binary` clears its in-memory evidence ledger; the next autosave would then
  overwrite (or, empty, delete) the session's bundle. The rea child's autosave therefore runs
  before a `close_binary` is forwarded (`[child.autosave] before_tools`).
- Races that were found and fixed: concurrent attaches in one JVM collide on Ghidra's
  script-bundle file lock (ghidra-mcp serializes script loads JVM-wide, patch 0002, and the helper
  retries); two sessions importing the same new file at once saved the second as `<name>.0`, a
  separate program (patch 0002 serializes imports per destination, the helpers also lock per
  program and fail if an import comes back under another name, "The attach lock dir" below).
- An import that fails after its program was saved (analysis options, auto-analysis, a
  post-script) removes the program again, and one that never finished (the JVM died during
  analysis) carries a pending flag in its program info: the next import of that destination
  deletes it and starts over.
- REA's analysis extensions (`REA_GHIDRA_NATIVEAOT_JAR`) are refused: the bridge would load their
  jars into the shared JVM. PSP targets need Ghidra's Allegrex extension, which the image does
  not ship (unchanged).
- Memory: REA's programs live in the shared heap (`JAVA_OPTS`, `-Xmx8g`); a session costs about
  0.5 GiB outside it (rea node, helper, bridge).
- Maintenance per REA release (it ships about daily): the build tests the patch anchors and the
  helper's mapping of every profile against the pinned REA and recompiles the patched bridge; a
  mismatch fails the build and is re-fitted by hand. Per ghidra-mcp release the patches must apply
  (or are dropped once upstream; the build says which).
- Debugging switch: `REBOX_REA_ATTACH = "0"` in the rea child's `[child.env]` and REA runs its own
  JVMs again (heap `GHIDRA_HEADLESS_MAXMEM`, `GHIDRA_MAXMEM` as fallback, REA's default 2G; it
  applies only then). Without it, a missing `REBOX_REA_ATTACH_HELPER` makes REA's Ghidra provider
  fail rather than quietly start a JVM.
- Upstream: an opt-in external launcher command in REA would remove the REA patch
  (`deps/rea-attach/UPSTREAM.md`); the ghidra-mcp patches are prepared as issues and PRs.

## The 8089 token

**Status:** accepted.

**Context.** ghidra-mcp's HTTP port listens on loopback only, but headless Chrome (REA's browser
scenarios) loads pages that can send requests to `127.0.0.1:8089`, and the port can import files,
save and exit.

**Decision.** The supervisor sets `GHIDRA_MCP_AUTH_TOKEN` to a random value once per container
start, unless one is preset. ghidra-mcp then requires `Authorization: Bearer <token>` on every
call except `/check_connection` and `/mcp/health`. The JVM, the bridges and REA's attach helper
inherit it; the supervisor's own calls send it. One value per container start, so the bridges
survive a JVM restart.

**Consequences.** Not `REBOX_`-prefixed (children need it, `REBOX_*` is stripped), so REA and the
targets it runs can read it. Accepted: they already run as the same uid as the JVM; the token
closes the route from web pages, not from local processes. Never logged. The e2e runner presets it.

## The attach lock dir

**Status:** accepted.

**Context.** The helpers of concurrent REA sessions lock per program, so two opens of the same file
cannot both import it. REA's `TMPDIR` is private to a session, so the locks need a shared path.

**Decision.** `/tmp/rebox-attach-locks-<uid>`, mode 0700. A lock whose holder died is taken over.

**Consequences.** Per uid, because a `PUID` change on a persistent `/tmp` must not meet the old
uid's directory. The start-up sweep removes stale lock files (and the directory when empty and
ours), together with `rea-*` leftovers.

## Session timeouts

A gone client's session (REA process, helper, bridge) must not linger, without punishing
interactive users.

- A session whose client keeps its standalone GET stream open (the MCP SDKs open one after
  `notifications/initialized`) keeps `REBOX_SESSION_IDLE_TIMEOUT` (1800 s); a session with none (a
  `claude -p` that exited, a dead client) is closed after `REBOX_SESSION_ORPHAN_TIMEOUT` (300 s).
  The app counts GET streams per session (`get_stream_open`/`get_stream_closed` in the log). If a
  client turns out to hold no GET stream while interactive, set the two timeouts equal
  (`REBOX_SESSION_ORPHAN_TIMEOUT=0` uses the idle timeout for everyone).
- At the session cap a new `initialize` evicts the longest-idle session with nothing in flight if
  idle for at least `REBOX_SESSION_EVICT_MIN_IDLE` (120 s), after its autosave; 503 only when none
  qualifies. A refused agent is worse off than an evicted idle one, which re-initializes on 404.
- Requests with a `progressToken` that the child leaves silent for 30 s get a synthetic
  `notifications/progress`, so Claude Code's 5-minute no-progress abort does not kill a long import.
- A session is closed when any child dies: a session without `rea_` or without the bridge looks
  alive and fails confusingly; the 404 makes the client start a whole one.

## State dir

**Decision:** one state root, `REBOX_STATE_DIR`, default `$RE_WORKDIR/re-box-state`, holding the
ghidra-mcp project (`ghidra-projects/`), the services' `HOME` (`home/`: Ghidra settings and logs,
tool configs), `XDG_CACHE_HOME` (`cache/`) and REA evidence exports (`rea/evidence/`). Layout and
survival table: docs/PERSISTENCE.md.

- **Default inside the work dir,** not a container path: the work dir is already a required,
  writable mount, so state persists with no extra configuration; a container path would vanish
  with `docker rm`, `--rm` or `compose down`. A separate state dir is one variable and one mount.
- **HOME and XDG, not per-tool symlinks.** An empty `/home/rebox` and a runtime audit of the
  image (files written outside `/work` and `/tmp` while REA, ghidra-mcp and every bundled tool
  ran) showed everything user-level goes to `$HOME` or `/var/tmp/<user>-ghidramcp`; moving `HOME`
  plus the XDG variables, and `-Duser.home` for the headless JVM (Java takes `user.home` from
  passwd, not `$HOME`), catches all of it, including tools nobody audited.
- **The project is opened by the headless server's `--project` flag** and created on first start
  by `analyzeHeadless ... -preScript ReboxNoop.java`. ghidra-mcp's HTTP `/open_project` and
  `/create_project` honour `GHIDRA_MCP_FILE_ROOT` (canonical paths, symlinks resolved), which is
  `RE_WORKDIR` plus REA's `TMPDIR` as a guard for agent-driven imports; a state dir outside the
  work dir would otherwise be unreachable. Widening the root to a common ancestor would make it
  `/` in the scratchpad case.
- **No chown, ever.** The service user creates what is missing; an unwritable state dir stops the
  supervisor with one line (exit 2). `/work` exists in the image owned by 1000 so a named volume
  there is writable.
- **`/tmp` stays out of the state dir.** What REA keeps there is runtime that cannot be reopened:
  its private run dirs (descriptor, socket, target snapshot), mitmproxy's per-run `confdir` (a
  per-run CA) and apktool's per-run `--frame-path`. REA's Ghidra programs are in the project
  (attach, above); its own persistence for the rest is `export_evidence_bundle` /
  `import_evidence_bundle` and `close_binary {snapshot_path}`, and the aggregator calls the first.

## Saving

Mechanics and survival table: docs/PERSISTENCE.md. Why:

- **ghidra-mcp:** edits live in the headless JVM until saved. `exit_ghidra` and the JVM's
  shutdown hook save, but a kill or OOM would lose everything since the start, so the supervisor
  also saves every `REBOX_AUTOSAVE_INTERVAL` (300 s) and at once on SIGTERM (a short `docker stop`
  grace still saves first). Upstream refuses to autosave on behalf of a user in the GUI; here no
  user reviews edits before a save, and losing them is the failure the owner named.
- **REA:** a generic per-child `[child.autosave]` hook in the aggregator config calls
  `export_evidence_bundle` at session end and periodically, bounded by `REBOX_AUTOSAVE_TIMEOUT`
  (20 s, so a DELETE still clears the session's processes within the e2e test's 30 s). Snapshots
  are left to the agent: REA refuses them after metadata edits, and they only help reopen the same
  target.
- **Evidence pruning:** an export with no records and no unknowns is deleted at once; older
  bundles after `REBOX_EVIDENCE_RETENTION_DAYS` (30). Bundles feed only REA's evidence views (no
  renames are re-applied), so age loses little; durable work lives in the `ghidra_` project.

## Build

- **apt reads a dated snapshot.** *Status:* accepted. *Context:* `apt-get install` against the live
  archive made a rebuild a different image. *Decision:* every apt step reads
  `snapshot.ubuntu.com/ubuntu/$APT_SNAPSHOT/` (`APT_SNAPSHOT` in `versions.env`); only
  `ca-certificates`, needed for https to the snapshot, comes from the live archive. *Consequences:*
  no Renovate datasource, so the date is moved by hand, to a recent one, followed by the test
  stage; security updates arrive only then.
- **Build backends are hash-locked.** *Status:* accepted. *Context:* building the aggregator and
  bridge wheels fetched `hatchling` into an isolated environment, unverified. *Decision:*
  `deps/build/requirements-hatchling.txt` is installed with `--require-hashes` into a build venv,
  and wheels build with `--no-build-isolation` / `--no-build`, so no backend or sdist build is
  pulled in unchecked. *Consequences:* the lockfile is regenerated with
  `uv pip compile --generate-hashes` (docs/BUILDING.md).
- **Stage order: tools before Ghidra, ghidra-mcp and REA.** *Status:* accepted. *Context:* the
  `tools` stage (apt, compiles, Chrome, about 15 min cold) sat after the stages that change most.
  *Decision:* `runtime` starts `FROM tools` and copies the others in. *Consequences:* a change to
  a ghidra-mcp patch or REA no longer reinstalls the tools; rebuilding after a patch 0002 change
  went from 1669 s to 684 s.

## Release pipeline

- **Plain `docker build` / GitHub Actions to ghcr.io,** with the in-cluster ImageBuild kept as an
  optional route (docs/IMAGEBUILD.md). Nothing in the Dockerfile is ImageBuild-specific; the
  tini supervisor and the self-contained test stage also suit any BuildKit.
- **Runner: GitHub-hosted by default;** the self-hosted scale sets in k0s-flux cannot serve this
  repo as is (docs/BUILDING.md, "Runner"). `vars.REBOX_RUNNER` moves the job.
- **Registry build cache** on ghcr (`:buildcache`, `mode=max`): the gha cache's 10 GB cap is
  below this build's layers. Container registry storage is free (GitHub billing docs,
  2026-10-19).
