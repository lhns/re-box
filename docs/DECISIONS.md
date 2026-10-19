# Decisions

Outcomes of the spikes in the spec (section 5) and every place the build departs from the
spec, with the evidence. Upstream refs: Ghidra `12.1.4` (`ghidra_12.1.4_PUBLIC_20260921.zip`),
ghidra-mcp `v7.0.0-rc.3` (`295e3ad`), rea-agents `6.7.0` (git tag `rea-agents-6.7.0`,
`27dc42d`). Measured 2026-10-19.

## 5.1 One Ghidra install for both: yes, 12.1.4

**Decision:** one install at `/opt/ghidra`, Ghidra 12.1.4, used by REA and ghidra-mcp.

- REA's check is not exact-version. `src/ghidra/GhidraReleasePolicy.ts` (6.7.0):
  `SUPPORTED_GHIDRA_VERSION = "12.1.4"`; `ghidraReleaseCompatibility()` returns `verified` for
  12.1.4, `compatible` for any other 12.1.x, `unsupported` otherwise. Only `verified` and
  `compatible` are accepted (`ghidraReleaseAccepted`). 12.1.4 is the verified build.
- ghidra-mcp `v7.0.0-rc.3` declares `<ghidra.version>12.1.4</ghidra.version>` in `pom.xml`.
  `gradle buildExtension -x test -PGHIDRA_INSTALL_DIR=/opt/ghidra` (Gradle 9.8.0, the version in
  upstream's wrapper) builds `GhidraMCP-7.0.0.jar` and the extension zip against the install;
  no Maven `.m2` step. `v6.0.0` also builds against 12.1.4 with Gradle 9.6.1.
- `rea doctor --json` against the same install (excerpt):

  ```
  {"name":"ghidra-version","ok":true,"classification":"healthy","detail":"12.1.4"}
  {"name":"ghidra-java","ok":true,"detail":"21.0.12.1; 64-bit; JDK; /opt/java; accepted a 64-bit full JDK 21 or newer"}
  {"name":"ghidra-native_decompiler","ok":true,"detail":".../decompile reports elf x86_64, which matches linux/x64"}
  providerInspections: {"id":"ghidra","configured":true,"available":true,"providerVersion":"12.1.4"}
  ```
- End to end on the shared install: REA `open_binary` + `binary_overview` (cold Ghidra start
  42 s, 6 progress notifications) + `analyze_function compute`; ghidra-mcp headless
  `import_file`, `find_functions`, `get_functions(fields=decompiled_code)`, `rename_function`,
  `save_program`, and the rename survives a server restart.

## ghidra-mcp ref: v7.0.0-rc.3, not v6.0.0

v6.0.0 is the newest stable tag but cannot serve the spec's ghidra-mcp flow headless:

```
--> import_file {'file_path': '/work/hello_stripped'}
{"error":"Import requires GUI mode (PluginTool not available)"}
```

v7 serves `import_file` headless (`ProjectProgramProvider.importFile`), puts the project
operations behind `GHIDRA_MCP_FILE_ROOT`, makes lazy tool groups the default and reads
`GHIDRA_MCP_LAZY`, and targets 12.1.4. The spec's own vocabulary (`gh_find_functions`,
`GHIDRA_MCP_LAZY`) is v7's. Cost: it is a release candidate. `versions.env` pins the tag (for
Renovate) and the commit (checked at build, since tags move); Renovate proposes `v7.0.0` once
it exists. Open question for the owner: ship the RC or wait for 7.0.0.

Tool names that changed in v7 (upstream wins over the spec's test list,
`docs/project-management/MIGRATION_7.0.0_TOOL_CONSOLIDATION.md`):

| spec | v7.0.0-rc.3 |
| --- | --- |
| `gh_decompile_function` | `gh_get_functions(fields="decompiled_code")` |
| `gh_list_methods` | `gh_list_program_items(kind="methods")` or `gh_find_functions` |

## 5.2 Aggregator: custom

No existing project meets all ten requirements (evaluated 2026-10-19 from source, nothing run):

| candidate (commit) | 1 transports | 2 per-session children | 3 prefix, fail loud | 4 list_changed | 5 progress/cancel | 6 timeouts | 7 prompts/resources | 8 bearer + /healthz | 9 cap | 10 idle reap |
|---|---|---|---|---|---|---|---|---|---|---|
| supergateway 4.2.1-rc.2 (7909e33, TS) | Y | Y | P: warns, 128 chars, first wins | Y | Y (rewrites token) | ? | P: not prefixed | P | N | P |
| FastMCP (adf90b6, Py) | Y | P: `StatefulProxyClient`; default is a fresh backend session per request | P | N: TTL cache, no relay | P: no cancel relay | ? | Y | P | N | P |
| 1mcp agent 0.39.2 (33decb6, TS) | Y | P: `perClient` templates | P: fixed `_1mcp_` separator | P | ? | P | Y | N: OAuth | ? | ? |
| MetaMCP (ff4ff2d, TS) | Y | P | P: fixed `server__tool` | ? | P | P | Y | N; needs Postgres + UI | ? | ? |
| sparfenyuk/mcp-proxy (701a045, Py) | Y | N: one shared child | N | ? | ? | ? | ? | N | N | N |
| TBXark/mcp-proxy (a0e6dfb, Go) | Y | N: shared | N | ? | ? | P | Y | P | N | N |
| IBM mcp-context-forge 1.0.11 (86af292, Py) | Y | N | P | ? | ? | ? | Y | P | ? | ? |
| docker/mcp-gateway (a34df45, Go) | Y | N: pooled per server | N | P | ? | ? | Y | N | N | N |
| microsoft/mcp-gateway (f57c4f0, .NET) | N: k8s pod router | N | N | ? | ? | ? | ? | N | ? | ? |
| ravitemer/mcp-hub 4.2.1 (9c7670a, JS) | Y | N: shared | P | P | ? | ? | Y | P | N | N |
| mcpo | N: OpenAPI downstream | N | N | N | N | N | N | N | N | N |

Y yes, P partial, N no, ? not verified. Key pointers: supergateway
`src/gateways/statefulSessions.ts` (children per session id, group kill in
`src/lib/ownedChildProcesses.ts`), README "Combining servers on one URL" (collisions: "the
first one listed wins"); FastMCP `docs/servers/providers/proxy.mdx` "Session Isolation" (fresh
backend session per request by default), `providers/proxy.py` (`StatefulProxyClient`).

supergateway is closest; it lacks a session cap (9), fail-loud name validation at the 64-char
limit (3) and prompt/resource prefixing (7), and is TypeScript, while the spec asks for Python
with the official SDK. Closing those gaps in a fork costs about as much as a small aggregator we
own. Hence `aggregator/` (spec section 7).

### Protocol era

MCP 2026-07-28 is sessionless (no `initialize`, no `Mcp-Session-Id`), so it cannot carry
per-session children. The aggregator speaks only the handshake era (2024-11-05 to 2025-11-25)
and answers anything else with `-32022` listing those versions, which clients take as "fall
back to `initialize`" (python-sdk 2.3.0 `mcp/client/_probe.py`). Verified with Claude Code
2.1.303 against a prototype:

```
POST sid=None pv=2026-07-28  {"method":"server/discover",...}   -> -32022
POST sid=None pv=None        {"method":"initialize","params":{"protocolVersion":"2025-11-25",...}}
POST sid=df70... GET sid=df70... (standalone stream), tools/list, tools/call (with progressToken)
```

The SDK's legacy `StreamableHTTPServerTransport` serves each session; the aggregator routes raw
JSON-RPC messages (params and results stay plain dicts, so `structuredContent` is never
re-modelled). Children are spoken to over our own stdio transport so each starts in its own
process group.

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

REA also has 6 prompts and no resources; ghidra-mcp has neither. Longest prefixed name is 41
characters; the two catalogs share 5 names (`set_bookmark`, `list_bookmarks`,
`search_strings`, `set_comment`, `list_strings`), which the prefixes separate. The lazy default
groups already contain every tool the e2e ghidra flow needs (`import_file`, `find_functions`,
`get_functions`, `rename_function`, `save_program`, `open_program`, `close_program`,
`get_metadata`, `search_tools`, `load_tool_group`).

**Decision:** keep ghidra-mcp lazy (`GHIDRA_MCP_LAZY=1`, upstream default in v7); the aggregator
relays `list_changed`. Keep REA's profiles at upstream `full`, exposed as
`REA_MCP_INPUT_SCHEMA_PROFILE` / `REA_MCP_OUTPUT_SCHEMA_PROFILE`. REA's full catalog is 3 MB;
docs/CLIENTS.md says when to switch to `compact`/`off`.

## Other departures from the spec

- **`GHIDRA_PROJECTS_DIR` defaults to `$RE_WORKDIR/ghidra-projects`, not `.ghidra-projects`.**
  Ghidra refuses project paths with a dot-prefixed element:
  `ERROR Error creating project: Path element starting with '.' is not permitted`. The same holds
  for `RE_WORKDIR` itself when it contains the projects dir.
- **ghidra-mcp extension install:** not needed. The headless server is
  `com.xebyte.headless.GhidraMCPHeadlessServer` run from `GhidraMCP-<v>.jar` plus Ghidra's
  jars on the classpath, which is what upstream's `docker/entrypoint.sh` does; the extension zip
  is for the GUI plugin. Upstream's Dockerfile also unpacks the bundled Jython extension; we do
  the same so scripts work if `GHIDRA_MCP_ALLOW_SCRIPTS=1`.
- **REA per-session heap** is `GHIDRA_HEADLESS_MAXMEM` (fallback `GHIDRA_MAXMEM`, default
  `2G`; `src/ghidra/GhidraJavaLaunch.ts`).
- **Two Python environments:** the ghidra-mcp bridge pins `mcp>=1.28.1,<2`; the aggregator uses
  `mcp` 2.3.0. Each runs from its own venv by absolute path.
- **Supervision: tini + `rebox-supervisor`, not s6-overlay.** The in-cluster build runs the test
  stage under rootless BuildKit with no init and no PID namespace, so the test stage must start the
  services itself; s6-overlay needs to be PID 1. One supervisor (Python, part of `aggregator/`)
  serves both: dependency order (aggregator after the headless server and its project are ready),
  readiness, headless restart with backoff, and the SIGTERM order the spec asks for (sessions
  closed, then `/exit_ghidra`, which saves all programs). The test stage exercises exactly the
  production start path.
- **Image user.** The image's `USER` is `1000:1000`, so it runs non-root under plain `docker run`
  and Kubernetes `runAsNonRoot`. The PUID/PGID remap (spec 6.1) needs root, so it applies only when
  the container is started as root (compose example: `user: "0:0"` plus `CHOWN`, `SETUID`, `SETGID`);
  the entrypoint then drops to the remapped user with `setpriv --no-new-privs`. Test 8.3.9 runs in
  the remap mode (uid 4321); the uid-1000 mode is checked in `tests/smoke/image.sh` (no remap,
  warning, writable HOME fallback).
- **Test 8.3.10** finds a session's processes by the `REBOX_SESSION` tag every child inherits and
  by their process groups, never by scanning for process names, because the in-cluster build has
  no PID isolation. Reading another uid's `/proc/<pid>/environ` needs `CAP_SYS_PTRACE`, which
  Docker does not grant root, so the e2e tests run as the PUID user.
- **Pins outside versions.env.** Base images are pinned tag@digest in the `FROM` lines (a `FROM`
  cannot read a file; Renovate's dockerfile manager tracks them). Package trees are lockfiles with
  hashes: `aggregator/uv.lock`, `deps/rea/package-lock.json` (sha512 integrity for all 174
  packages instead of `npm i -g`, which would float transitive versions), `deps/tools/requirements-*.txt`,
  and ghidra-mcp's own `uv.lock` exported with hashes for the bridge venv. `REA_VERSION` must
  equal the locked rea-agents version; the build and `check-pins.py` refuse a mismatch.
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
- **Spec 8.1, ghidra-mcp upstream tests:** the Python offline tier (`tests/offline/` and the
  read-only integration suite against upstream's fake server) runs at the pinned ref in the
  `ghidra-mcp-test` stage. The Java unit tests are not run: they resolve junit/mockito from Maven
  Central without verification.
- **Spec 8.4 (upstream verification lanes):** skipped. REA's `verify:*` scripts need a source
  checkout and its dev dependencies; they do not run against the installed package.

## Findings for clients

- **Python SDK 2.x clients** cap one SSE event at 1 MiB by default (`max_sse_event_size`). REA's
  full-profile `tools/list` is ~3 MB in one event, so such a client fails `list_tools` unless it
  raises the cap or the container uses `REA_MCP_INPUT_SCHEMA_PROFILE=compact` and
  `REA_MCP_OUTPUT_SCHEMA_PROFILE=off`. The aggregator does not paginate `tools/list`: clients that
  do not follow `nextCursor` would silently lose tools. Claude Code 2.1.303 handles the full catalog.
- **Claude Code `-p` exits without DELETE**, so its session (and REA's Ghidra JVM) lives until the
  idle timeout and counts against `REBOX_MAX_SESSIONS` (docs/OPERATIONS.md).
