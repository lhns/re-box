# Persistence

Everything worth keeping that re-box writes at runtime goes under **one directory**,
`REBOX_STATE_DIR`. Mount it (or the work dir that contains it) and a stopped, removed or
recreated container loses nothing but running sessions. Why it is built this way:
docs/DECISIONS.md, "State dir" and "Saving".

## Where it is

| | |
| --- | --- |
| default | `$RE_WORKDIR/re-box-state`: persists with the work-dir mount, no extra configuration |
| elsewhere | `-e REBOX_STATE_DIR=/abs/path -v /abs/path:/abs/path`: any absolute path, mounted at the **same path** inside and out, so paths re-box writes (and paths an agent passes, e.g. to `rea_export_evidence_bundle`) are valid on both sides |
| rules | no path element may start with `.` (Ghidra refuses such project paths); writable by the service user (uid 1000, or `PUID` when started as root); one container per state dir (Ghidra locks the project) |
| ownership | nothing is chowned. Missing directories are created by the service user; an existing directory keeps its owner and mode. Not writable: the container exits at start with one line naming `REBOX_STATE_DIR`. A fresh named volume is root-owned (except at `/work`, which the image pre-creates for uid 1000): chown it once, or start as root with `PUID`/`PGID` and a volume owned by that id |

### An agent's scratchpad

To let an agent read results directly, put the state dir inside a directory the agent can read,
e.g. its scratchpad, at the same path:

```sh
S=/tmp/claude-1000/my-project/scratchpad/re-box     # any absolute path the agent can read
mkdir -p "$S"
docker run -d --name re-box --stop-timeout 120 --memory 16g \
  -e REBOX_TOKEN_FILE=/run/secrets/rebox_token -v "$PWD/rebox_token:/run/secrets/rebox_token:ro" \
  -e RE_WORKDIR="$HOME/re"   -v "$HOME/re:$HOME/re" \
  -e REBOX_STATE_DIR="$S"    -v "$S:$S" \
  -p 127.0.0.1:8443:8443 ghcr.io/lhns/re-box:latest
```

(compose: set `REBOX_STATE_DIR` in `.env` and uncomment its volume line.) A state dir under
`/tmp` is fine: the bind mount sits on top of the container's `/tmp` volume. A scratchpad that is
deleted with the agent session takes the Ghidra project with it; for long projects use a stable
directory and give the agent read access to it.

"Same path" needs a Linux host (or a path inside the Docker Desktop / WSL2 VM): a Windows or
macOS host path cannot exist under the same name in a Linux container. Ghidra's project locks on
Docker Desktop's host-directory mounts are untested; prefer a named volume or a WSL2 path there.

## What is in it

| path (under `REBOX_STATE_DIR`) | holds | why keep it | size |
| --- | --- | --- | --- |
| `ghidra-projects/rebox.gpr`, `rebox.rep/` | ghidra-mcp's Ghidra project: every program imported with `gh_import_file`, its auto-analysis, and all `gh_` edits (renames, types, comments, bookmarks) | the long-running work. Re-importing a large binary costs its auto-analysis again (hours for big targets) and loses every annotation | several times each imported binary: MBs for small ELFs, GBs for large firmware |
| `home/` | `HOME` of every service and tool | | small |
| `home/.config/ghidramcp/ghidramcp_12.1.4_PUBLIC/` | the headless server's Ghidra settings: preferences, tool state, `application.log` (Ghidra's own log, more detail than the container log); `home/.config/ghidra*`, `rebox-ghidra` are the first-start project creation's | diagnosing after a restart; preferences | < 10 MB |
| `home/ghidra_scripts/` | user Ghidra scripts (only with `GHIDRA_MCP_ALLOW_SCRIPTS=1`) | scripts an agent wrote | small |
| `home/.config`, `home/.local`, `home/.cache` | other tools' user state when run directly: rizin, gdb/pwndbg history and config, adb keys (`.android/`), Chrome profile, NSS db | device pairings, histories | small |
| `cache/` (`XDG_CACHE_HOME`) | Ghidra's cache (`rebox-ghidramcp/`: `fscache2`, `packed-db-cache`, `debuginfo-cache` with downloaded debug symbols), pwndbg, fontconfig, Chrome | faster opens, no repeated symbol downloads. **Safe to delete** while the container is stopped | 100s of MB, grows with symbol downloads |
| `rea/evidence/<session>.json` | REA evidence bundles, one per MCP session: every Evidence record and unknown the session produced, written by the aggregator ("When things are saved") | REA's in-session analysis is discarded when its session ends; this is what is left. Restore with `rea_import_evidence_bundle {"path": ...}` | KBs to tens of MB per session; delete old ones by hand |

Suggested places for an agent's own REA exports (not created automatically): `rea/exports/` for
`rea_export_evidence_bundle`, `rea/snapshots/` for `rea_close_binary {"snapshot_path": ...}`.

### Not in it, on purpose

| what | where | why not |
| --- | --- | --- |
| REA's per-session Ghidra project and runtime (`rea-ghidra-*`: project, Ghidra settings, cache, logs) | `/tmp` | REA creates it privately per session and deletes it when the session's REA process ends; nothing can reopen it. Its content survives only as the evidence export |
| mitmproxy config and CA, apktool frameworks, extraction scratch | `/tmp`, per REA operation | REA gives each run its own directory (`confdir=<run dir>`, `--frame-path <run dir>`), so there is no long-lived CA to keep or trust. Extraction output a caller wants kept goes to a path the caller names |
| Ghidra temp files, JVM perf data, ghidra-mcp's socket | `/tmp` | scratch |
| aggregator and supervisor logs | stdout | the container runtime's logging driver keeps them; Ghidra's own log is under `home/` |
| the token | `REBOX_TOKEN_FILE` | a secret; never written to the state dir |

The work dir holds only the targets and what tools or agents write there explicitly.

## When things are saved

**ghidra-mcp** keeps edits in memory until a program is saved. Saves happen on:

- `gh_save_program`, `gh_save_all_programs`, `gh_close_program` with `save`, or a program being
  evicted from ghidra-mcp's open-program cache;
- every `REBOX_AUTOSAVE_INTERVAL` seconds (default 300; `0` disables): the supervisor saves every
  changed program (after waiting for running auto-analysis, as ghidra-mcp's save does);
- shutdown: on SIGTERM the supervisor starts a save at once (waiting up to 30 s for it), then sends
  `exit_ghidra`, which saves again and stops the JVM; the JVM's shutdown hook saves too if it gets a bare SIGTERM.

**REA** keeps each session's analysis in that session's REA process. The aggregator calls REA's
`export_evidence_bundle` to `rea/evidence/<session>.json` (overwritten each time, written
atomically) when:

- the session ends: DELETE from the client, idle timeout (`REBOX_SESSION_IDLE_TIMEOUT`), or
  container shutdown; bounded by `REBOX_AUTOSAVE_TIMEOUT` (20 s);
- every `REBOX_AUTOSAVE_INTERVAL` seconds while the session has made REA calls since the last
  export and has none running.

Sessions without REA calls write nothing. `<session>` is the session tag the container log uses
(`session_open`, `session_close`, `autosave` lines).

## What survives what

| event | ghidra-mcp project | REA in-session analysis | REA evidence | settings, caches |
| --- | --- | --- | --- | --- |
| client ends the session, or it idles out | kept (the headless server and its project outlive sessions) | gone, by design | exported at session end | kept |
| `docker stop` with enough grace (`stop_grace_period: 120s`, `--stop-timeout 120`) | **everything saved** | gone | **exported** for every session | kept |
| `docker stop` with Docker's default 10 s | saved: the save starts at SIGTERM (measured: done 1.6 s after it, 145 ms for the save). Only a save that must first wait for long-running auto-analysis can be cut off (the shutdown waits at most 30 s for it before `exit_ghidra`); then edits since the last save are lost | gone | exported unless a session's export is still running at the SIGKILL | kept |
| `docker kill`, host crash, power loss | edits since the last save (at most one `REBOX_AUTOSAVE_INTERVAL`) lost; imported programs survive (an import saves). The project opens at its last save; the stale lock is taken over on the next start (verified) | gone | changes since the last periodic export lost | kept |
| OOM kill of the headless JVM (largest process at the defaults) | edits since the last save lost; the supervisor restarts the JVM with backoff and reopens the project; sessions stay up, `gh_` calls fail until it is back | unaffected | unaffected | kept |
| OOM kill of a REA Ghidra JVM | unaffected | that call fails; REA reports it and can start a new JVM | unaffected | kept |
| OOM kill of a session's `rea mcp` process | unaffected | gone | changes since the last periodic export lost; the session's REA tools fail until the client opens a new session | kept |
| `docker rm`, `compose down`, image upgrade | kept (it is in the mount) | gone | kept | kept |

To keep a REA investigation beyond its session: work in ghidra-mcp's project (`gh_` tools, saved
as above) for annotations that must last, and export REA evidence or snapshots to the state dir
before ending the session; after a restart, `rea_open_binary` the target and
`rea_import_evidence_bundle` the export. Imported evidence is data for REA's evidence views; it
does not re-apply renames to REA's new in-session database.

## Backups

Copy the state dir while the container is stopped. A copy of a running container's state dir
can catch a Ghidra save half way; take it between autosaves at least, and expect to fall back to
an older copy if Ghidra refuses to open that program.
