# re-box

One container for agent-driven reverse engineering: [Ghidra](https://github.com/NationalSecurityAgency/ghidra)
12.1.4, [REA](https://www.npmjs.com/package/rea-agents) (rea-agents) and
[ghidra-mcp](https://github.com/bethington/ghidra-mcp), plus the Linux tools REA drives (rizin,
gdb/pwndbg, pwntools, JADX, apktool, binwalk, unblob, tshark, sigrok, WABT, mitmproxy, Chrome for
Testing), behind **one authenticated MCP endpoint** on port 8443. REA's tools and prompts are
prefixed `rea_`, ghidra-mcp's `gh_`. Every MCP session gets its own REA and bridge processes; the
ghidra-mcp Ghidra project is shared and persistent.

Image: `ghcr.io/lhns/re-box` (`latest` = `main`, `sha-<commit>`, `X.Y.Z` for release tags), linux/amd64, ~3.7 GB.

## Quick start

```sh
git clone https://github.com/lhns/re-box && cd re-box     # for compose.yaml and .env.example
cp .env.example .env                                      # set RE_WORKDIR, PUID, PGID
openssl rand -hex 32 > rebox_token && chmod 600 rebox_token
docker compose up -d
docker compose ps                                         # healthy after 1-2 min
```

Or without compose (runs as uid 1000; the work dir must be writable by it):

```sh
docker run -d --name re-box --stop-timeout 120 --memory 16g \
  -e REBOX_TOKEN_FILE=/run/secrets/rebox_token -v "$PWD/rebox_token:/run/secrets/rebox_token:ro" \
  -e RE_WORKDIR="$HOME/re" -v "$HOME/re:$HOME/re" \
  -p 127.0.0.1:8443:8443 ghcr.io/lhns/re-box:latest
```

Mount the work dir at the **same absolute path** inside and out: tool arguments are container
paths, so the paths your agent already uses stay valid, and so do the paths re-box writes back.
Pulling needs `docker login ghcr.io` while the package is private.

## Connect a client

Claude Code, `.mcp.json` in the project (token from the environment, so it never lands in a file):

```json
{
  "mcpServers": {
    "re-box": {
      "type": "http",
      "url": "http://127.0.0.1:8443/mcp",
      "headers": { "Authorization": "Bearer ${REBOX_TOKEN}" },
      "timeout": 900000
    }
  }
}
```

```sh
export REBOX_TOKEN=$(cat rebox_token)
claude
```

Or in one line (writes the token into `~/.claude.json`):

```sh
claude mcp add --transport http re-box http://127.0.0.1:8443/mcp --header "Authorization: Bearer $(cat rebox_token)"
```

REA's workflow skill, rewritten for the prefixed names:
`docker run --rm ghcr.io/lhns/re-box tar -C /opt/rebox/skills -c . | tar -x -C ~/.claude/skills`.
Timeouts, large catalogs, Hermes and other clients: [docs/CLIENTS.md](docs/CLIENTS.md).

## Persistence

Everything worth keeping that re-box writes at runtime lives under one directory,
`REBOX_STATE_DIR` (default `$RE_WORKDIR/re-box-state`, so it persists with the work dir): the
ghidra-mcp Ghidra project, Ghidra and tool settings, caches and REA evidence exports. Point it
anywhere, e.g. an agent's scratchpad, and mount it at the same path. ghidra-mcp programs are saved
every 5 minutes and on shutdown; REA sessions export their evidence when they end. What survives a
`docker stop`, a `docker kill` and an OOM kill: [docs/PERSISTENCE.md](docs/PERSISTENCE.md).

## Docs

| | |
| --- | --- |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | environment, users and ownership, memory sizing, health, logs, security, upgrades |
| [docs/PERSISTENCE.md](docs/PERSISTENCE.md) | the state dir, what is saved when, what survives what |
| [docs/CLIENTS.md](docs/CLIENTS.md) | Claude Code, Hermes, generic MCP clients |
| [docs/BUILDING.md](docs/BUILDING.md) | `docker build`, CI, runners, measured build times |
| [docs/DECISIONS.md](docs/DECISIONS.md) | why things are the way they are |
| [aggregator/README.md](aggregator/README.md) | the MCP aggregator and supervisor |

**Security:** several REA features execute the target. The container is the sandbox: run it on an
egress-restricted network and treat what it analyses as hostile (docs/OPERATIONS.md, "Security").
