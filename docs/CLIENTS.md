# Connecting clients

re-box serves one MCP endpoint over streamable HTTP: `http(s)://<host>:8443/mcp`, with a static
`Authorization: Bearer <REBOX_TOKEN>` header. REA's tools and prompts are prefixed `rea_`,
ghidra-mcp's `ghidra_`. Any client that speaks streamable HTTP with a custom header works.

Paths in tool arguments are paths **inside the container**. Mount your work directory at the
same absolute path (`RE_WORKDIR`, docs/OPERATIONS.md) and the paths your harness sees are valid
as they are (on Windows send the Git Bash form `/c/Users/you/re/x.exe`, not `C:\...`: the
`initialize` instructions tell the agent so). Nothing is uploaded. The same goes for the state dir (`REBOX_STATE_DIR`,
docs/PERSISTENCE.md): mounted at its own path, e.g. inside the agent's scratchpad, the agent reads
REA exports and evidence autosaves there directly, and a path it passes to
`rea_export_evidence_bundle` or `rea_close_binary {snapshot_path}` is valid on both sides.

## Claude Code

Prefer a `.mcp.json` entry (project root, or `~/.claude.json` for user scope) that reads the
token from the environment, so it never lands in a config file or shell history:

```json
{
  "mcpServers": {
    "re-box": {
      "type": "http",
      "url": "https://re-box.example:8443/mcp",
      "headers": { "Authorization": "Bearer ${REBOX_TOKEN}" },
      "timeout": 900000
    }
  }
}
```

Then `export REBOX_TOKEN=...` before starting `claude` (PowerShell: `$env:REBOX_TOKEN = (Get-Content
rebox_token)`). If the variable is unset, Claude Code
sends the literal text `${REBOX_TOKEN}` and gets 401; `claude mcp list` warns about the missing
variable.

The one-line alternative writes the token into `~/.claude.json`:

```
claude mcp add --transport http re-box https://re-box.example:8443/mcp \
  --header "Authorization: Bearer $REBOX_TOKEN"
```

### Timeouts

A cold Ghidra start takes about 40 s, an import with analysis of a large binary minutes. Claude
Code's relevant limits (https://code.claude.com/docs/en/env-vars.md,
https://code.claude.com/docs/en/mcp.md):

| setting | default | set to |
| --- | --- | --- |
| per-server `"timeout"` in `.mcp.json`, or `MCP_TOOL_TIMEOUT` (ms) | HTTP requests time out after 60 s unless one of these is above 60000 | `900000` (at least `600000`) |
| `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` (ms) | 300000 for network servers: a call with no response **and no progress notification** for 5 min is aborted | `1800000` as a belt-and-braces setting; re-box already sends a synthetic progress notification every 30 s (`REBOX_PROGRESS_KEEPALIVE_INTERVAL`) for calls that carry a progress token, which covers ghidra-mcp imports that report none |
| `MCP_TIMEOUT` (ms, server connect) | 30000 | default is fine; a session start spawns two children and takes a few seconds |

`MCP_TOOL_TIMEOUT` and the idle timeout can go in the shell or in `settings.json` under `env`.

### Cold start

Ghidra starts with the first session (about 40 s). No client support for `tools/list_changed` is
needed: the full `ghidra_` catalog is in the first `tools/list` (from a cache in the state dir; the
very first start of a new state dir fills it before answering `initialize`), and a `ghidra_` call made
while Ghidra starts waits for it, then runs. The wait is bounded by
`REBOX_HEADLESS_START_TIMEOUT` (300 s); on timeout or a failed start (for example the project is
locked by another container) the call returns a tool error naming the reason. re-box sends progress
notifications meanwhile for calls that carry a progress token.

The bridge gives `ghidra_import_file` 1800 s (a fixed table in its `config.py`, raised from
upstream's 300 s by re-box's patch, not an environment variable). If it times out the tool
reports an error but the import continues in the Ghidra server: check `ghidra_list_open_programs` /
`ghidra_analysis_status` before importing again. `rea_open_binary` is bounded by
`REA_GHIDRA_STARTUP_TIMEOUT_MS` (docs/OPERATIONS.md).

`ghidra_import_file` also takes loader options and import scripts; the scripts are restricted to
REA's own, so an agent cannot run arbitrary code through it. `rea_open_binary` imports into
`/rea/...` of the same project (docs/PERSISTENCE.md).

### Session lifetime

A session idles out after 30 min (5 min if the client holds no GET notification stream, e.g. a
`claude -p` that exited without DELETE); at the session cap the longest-idle one is evicted. A
closed session answers 404 and a compliant client re-initializes. Knobs: docs/OPERATIONS.md.
REA's open binary and findings are gone then, its evidence is exported and its annotations are in
the project (docs/PERSISTENCE.md).

### Large catalogs and results

- REA's full catalog is about 3 MB of schemas (docs/DECISIONS.md, 5.3). Claude Code defers MCP
  tool definitions by default (tool search), so this costs little context. With
  `ENABLE_TOOL_SEARCH=false`, or a client without deferral, start the container with
  `REA_MCP_INPUT_SCHEMA_PROFILE=compact` and `REA_MCP_OUTPUT_SCHEMA_PROFILE=off` (220 KB).
- Results above 25,000 tokens (`MAX_MCP_OUTPUT_TOKENS`) are cut; text over 50,000 characters is
  saved to a file. Raise `MAX_MCP_OUTPUT_TOKENS` if REA dossiers are truncated.
- ghidra-mcp's whole catalog (197 tools, ~250 KB) is listed up front; lazy tool groups
  (`GHIDRA_MCP_LAZY=1`) need a client that follows `tools/list_changed`, which Claude Code 2.1.303
  did not in a `-p` test (docs/DECISIONS.md, 5.3).

### REA's workflow skill

REA's setup normally installs a skill into the agent. Here the agent runs outside the
container, so the image ships the skill, rewritten for the `rea_` names, at `/opt/rebox/skills`:

```
docker run --rm <image> tar -C /opt/rebox/skills -c . | tar -x -C ~/.claude/skills
```

It installs `~/.claude/skills/reverse-engineer-anything/`. The build fails if any REA tool or
prompt name in the skill is left unprefixed. Re-extract after upgrading the image.

## Hermes

`~/.hermes/config.yaml` (Hermes expands `${VAR}` in `headers`; it re-fetches tools on
`tools/list_changed`):

```yaml
mcp_servers:
  re-box:
    url: "https://re-box.example:8443/mcp"
    headers:
      Authorization: "Bearer ${REBOX_TOKEN}"
    timeout: 900          # tool call timeout, seconds (default 300)
    connect_timeout: 60
```

## Other clients

Anything that does streamable HTTP with a static header: point it at `/mcp` with the
`Authorization` header and a tool timeout of at least 10 minutes.

- re-box speaks the session-based protocol versions (2024-11-05 to 2025-11-25). A client that
  first probes the sessionless 2026-07-28 protocol gets error `-32022` listing those versions and
  falls back to `initialize`, as the official SDKs and Claude Code do.
- Clients that ignore `tools/list_changed` need nothing special ("Cold start").
- Clients with small message or schema limits: see the schema profiles above, and
  `REA_MCP_MAX_RESPONSE_BYTES` (docs/OPERATIONS.md).
- A session ends when idle (see "Session lifetime"); the next request gets 404 and a compliant
  client re-initializes. REA's open binary and in-session state are gone; the Ghidra work
  (renames, comments) is in the project, and the evidence is exported (docs/PERSISTENCE.md).
