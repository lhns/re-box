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

"Same path" works on Linux and macOS: `/Users/you/re` is a valid Linux path too (Docker Desktop
shares `/Users` with its VM), so `-v /Users/you/re:/Users/you/re` is fine. Only native Windows
paths (`C:\...`) cannot exist in a Linux container: mount the Windows folder at the Git Bash form,
`-v 'C:\Users\you\re:/c/Users/you/re' -e RE_WORKDIR=/c/Users/you/re` (host side a Windows path,
container side `/c/...`; `MSYS_NO_PATHCONV=1` in Git Bash; README, "Windows"), and have the agent
pass `/c/Users/you/re/...` paths.

**Project locks on a Windows folder** (tested 2026-10-19 under lhns/remote-docker, which mounts
Windows folders through NFSv3 `soft,nolock,local_lock=all`): Ghidra's lock refuses a second
container on the same state dir (the second container reports it: `/healthz` is 503 with "project
locked by another Ghidra instance"), stop/restart keeps the project, a `docker kill` after a save
leaves a stale `rebox.lock` that the next start recovers, and a `docker kill` during a 59 MB import
leaves the half-imported program absent and the project intact. A named volume was 25-35 % faster
(import 26 s vs 38 s, stop 3.2 s vs 6.2 s) and avoids NFS ownership quirks (files show uid 10000,
mode 777); it needs a one-time chown to uid 1000. Docker Desktop was not tested.

## What is in it

| path (under `REBOX_STATE_DIR`) | holds | why keep it | size |
| --- | --- | --- | --- |
| `ghidra-projects/rebox.gpr`, `rebox.rep/` | the shared Ghidra project: every program imported with `gh_import_file` or opened with `rea_open_binary` (under `/rea/<sha256 prefix>-<REA profile>/<file name>`, with REA's loader options and scripts), its auto-analysis, and all `gh_` and REA edits (renames, types, comments, bookmarks) | the long-running work. Re-importing a large binary costs its auto-analysis again (hours for big targets) and loses every annotation | several times each imported binary: MBs for small ELFs, GBs for large firmware |
| `home/` | `HOME` of every service and tool | | small |
| `home/.config/ghidramcp/ghidramcp_12.1.4_PUBLIC/` | the headless server's Ghidra settings: preferences, tool state, `application.log` and `script.log` (Ghidra's own logs, more detail than the container log); `home/.config/ghidra*`, `rebox-ghidra` are the first-start project creation's | diagnosing after a restart; preferences | small; logs roll at 50 MB (below) |
| `home/ghidra_scripts/` | `ReboxReaAttach.java`, which starts REA's bridge in the Ghidra JVM (copied from the image at every Ghidra start), and Ghidra's script bundle state | nothing to keep; rewritten at start. Clients cannot run scripts (the aggregator denies ghidra-mcp's script tools) | small |
| `home/.config`, `home/.local`, `home/.cache` | other tools' user state when run directly: rizin, gdb/pwndbg history and config, adb keys (`.android/`), Chrome profile, NSS db | device pairings, histories | small |
| `cache/` (`XDG_CACHE_HOME`) | Ghidra's cache (`rebox-ghidramcp/`: `fscache2`, `packed-db-cache`, `debuginfo-cache` with downloaded debug symbols), pwndbg, fontconfig, Chrome | faster opens, no repeated symbol downloads. **Safe to delete** while the container is stopped. Ghidra purges its own caches: `fscache2` files unused for 1 day, `packed-db-cache` and downloaded symbols after 7 days | 100s of MB |
| `cache/rebox-catalog/ghidra-<key>.json` | the aggregator's catalog cache: the full tool list of the ghidra-mcp bridge, advertised to sessions while Ghidra starts | a cold session lists the `ghidra_` tools at once. **Safe to delete** (refilled at the next start, which then runs Ghidra once) | ~250 KB |
| `rea/evidence/<session>.json` | REA evidence bundles, one per MCP session: every Evidence record and unknown the session produced, written by the aggregator ("When things are saved") | REA's in-session state (its evidence, findings, caches) ends with the session; this is what is left of it. Renames and comments REA made are in the Ghidra project instead. Restore with `rea_import_evidence_bundle {"path": ...}` | KBs to tens of MB per session; pruned automatically (below) |
| `.gitignore` | `*`, written once at start | the default state dir sits inside the work dir, which is often a git repo | bytes |

**Pruning.** An export with no records and no unknowns is deleted right after it is written
(nothing to restore). Bundles not modified for `REBOX_EVIDENCE_RETENTION_DAYS` (default 30; `0`
keeps everything) are deleted at start and daily. Bundles are only data for REA's evidence views
(no renames are re-applied), so age-based pruning loses little; annotations that must last belong
in the `ghidra_` project.

**Other housekeeping**, logged as the events named:

- `rea/evidence/.rea-write-*`: REA writes an export through a temp directory next to its
  destination; a kill mid-export leaves it. Removed at start (`evidence_partial_removed`).
- Ghidra's `application.log` and `script.log` roll at 50 MB (20 files per day), but Ghidra never
  deletes rolled files of earlier days. Rolled files (`*.log-DATE-N.log`) not modified for 30 days
  (fixed, not configurable) are deleted at start and daily (`ghidra_logs_pruned`).
- `/tmp`: REA's per-session `rea-ghidra-*` directory (it holds a full copy of the target) is
  deleted on a normal `rea_close_binary` but survives a `docker kill` or a crash, and the attach
  helper's `rebox-attach-locks/*.lock` stay. The supervisor removes `rea-*` entries and those lock
  files from the temp dir at each start (`tmp_swept`, count and bytes), before any session exists.
  Limit: a session that is SIGKILLed while the container keeps running leaves its `rea-ghidra-*`
  until the next container start. Ghidra's own temp files clean up after themselves.
- The size of `ghidra-projects/` is logged as `project_size` at every Ghidra start.

Suggested places for an agent's own REA exports (not created automatically): `rea/exports/` for
`rea_export_evidence_bundle`, `rea/snapshots/` for `rea_close_binary {"snapshot_path": ...}`.

### Not in it, on purpose

| what | where | why not |
| --- | --- | --- |
| REA's per-session runtime (`rea-ghidra-*`: descriptor, socket, target snapshot, logs) | `/tmp/rebox-rea` (REA's `TMPDIR`) | REA creates it privately per session and deletes it when the session closes the binary (leftovers of killed sessions are swept at start, above); the program itself is imported into the project from the snapshot |
| mitmproxy config and CA, apktool frameworks, extraction scratch | `/tmp`, per REA operation | REA gives each run its own directory (`confdir=<run dir>`, `--frame-path <run dir>`), so there is no long-lived CA to keep or trust. Extraction output a caller wants kept goes to a path the caller names |
| Ghidra temp files, JVM perf data, ghidra-mcp's socket | `/tmp` | scratch |
| aggregator and supervisor logs | stdout | the container runtime's logging driver keeps them; Ghidra's own log is under `home/` |
| the token | `REBOX_TOKEN_FILE` | a secret; never written to the state dir |

The work dir holds only the targets and what tools or agents write there explicitly.

## When things are saved

**ghidra-mcp** keeps edits in memory until a program is saved. Saves happen on:

- `ghidra_save_program`, `ghidra_save_all_programs`, `ghidra_close_program` with `save`, or a program being
  evicted from ghidra-mcp's open-program cache;
- every `REBOX_AUTOSAVE_INTERVAL` seconds (default 300; `0` disables): the supervisor saves every
  changed program (after waiting for running auto-analysis, as ghidra-mcp's save does);
- idle stop: when no session has existed for `REBOX_GHIDRA_IDLE_TIMEOUT` seconds the supervisor
  saves, then sends `exit_ghidra` (the next session starts a fresh JVM that reopens the project);
- shutdown: on SIGTERM the supervisor starts a save at once (waiting up to 30 s for it), then sends
  `exit_ghidra`, which saves again and stops the JVM; the JVM's shutdown hook saves too if it gets a bare SIGTERM.

**REA** opens its targets in the shared project (docs/DECISIONS.md, "REA attaches to the
ghidra-mcp JVM"), so its renames and comments are saved with it, as above. REA's own wording
("ephemeral" project, `project_ephemeral`, `persists_after_close: false`) predates this and is
stale under re-box. REA keeps the rest of each session's state (evidence, findings) in that
session's REA process. The aggregator calls REA's
`export_evidence_bundle` to `rea/evidence/<session>.json` (overwritten each time, written
atomically) when:

- the session ends: DELETE from the client, idle or orphan timeout
  (`REBOX_SESSION_IDLE_TIMEOUT`, `REBOX_SESSION_ORPHAN_TIMEOUT`), eviction at the session cap, a
  dead backend, or container shutdown; bounded by `REBOX_AUTOSAVE_TIMEOUT` (20 s);
- every `REBOX_AUTOSAVE_INTERVAL` seconds while the session has made REA calls since the last
  export and has none running.

Sessions without REA calls write nothing. `<session>` is the session tag the container log uses
(`session_open`, `session_close`, `autosave` lines).

## What survives what

| event | ghidra-mcp project | REA in-session analysis | REA evidence | settings, caches |
| --- | --- | --- | --- | --- |
| client ends the session, or it idles out | kept, with REA's edits (saved, and the headless server stops `REBOX_GHIDRA_IDLE_TIMEOUT` after the last session: it saves again and exits) | gone, by design (REA's edits are in the project) | exported at session end | kept |
| `docker stop` with enough grace (`stop_grace_period: 120s`, `--stop-timeout 120`) | **everything saved** | gone | **exported** for every session | kept |
| `docker stop` with Docker's default 10 s | saved: the save starts at SIGTERM (measured: done 1.6 s after it, 145 ms for the save). Only a save that must first wait for long-running auto-analysis can be cut off (the shutdown waits at most 30 s for it before `exit_ghidra`); then edits since the last save are lost | gone | exported unless a session's export is still running at the SIGKILL | kept |
| `docker kill`, host crash, power loss | edits since the last save (at most one `REBOX_AUTOSAVE_INTERVAL`) lost; imported programs survive (an import saves). The project opens at its last save; the stale lock is taken over on the next start (verified) | gone | changes since the last periodic export lost | kept |
| OOM kill of the headless JVM (largest process at the defaults) | edits since the last save lost; the supervisor restarts the JVM with backoff and reopens the project; sessions stay up, `gh_` calls fail until it is back | an open REA binary's calls fail with "provider exited"; `rea_open_binary` again attaches to the restarted JVM | unaffected | kept |
| OOM kill of a session's `rea mcp` process | unaffected | gone | changes since the last periodic export lost; the session's REA tools fail until the client opens a new session | kept |
| `docker rm`, `compose down`, image upgrade | kept (it is in the mount) | gone | kept | kept |

To keep a REA investigation beyond its session: its renames and comments already are (in the
project); export REA evidence or snapshots to the state dir before ending the session, and after a
restart `rea_open_binary` the target (it reopens the same program, annotations included) and
`rea_import_evidence_bundle` the export. Imported evidence is data for REA's evidence views.

## Removing programs

Nothing deletes programs from the shared project automatically: they are the long-running work,
and re-importing costs the analysis again. The project grows by every binary imported (REA's
`rea_open_binary` imports under `/rea/<sha256 prefix>/`). To remove ones you no longer need,
ask the agent to:

1. list them with `ghidra_list_project_files {"folder": "/"}` (REA's under `/rea/…`);
2. `rea_close_binary` first if REA has the program open (deleting under an attached REA session
   is untested);
3. call `ghidra_delete_file {"filePath": "/path/in/project"}`. It removes the program from the
   project (closing it first if it is open), never the source file on disk. The response is
   `{"success": true, "deleted": true}`; an unknown path gives `"deleted": false` and no error, so
   check `deleted`.

Emptied folders (such as `/rea/<sha12>/`) stay: there is no folder-delete tool, and they are
small. `delete_project` is hidden by the aggregator, since it would drop the whole shared project.
A state dir in an agent's scratchpad is removed with the scratchpad, which is the other way to
start clean. The `project_size` log line shows how large the project has grown.

## Backups

Ghidra's undo history lives in memory and is gone after any restart (idle stop included). A
deleted program or a bad edit can therefore only be recovered from a copy of the state dir made
while the container was stopped:

```sh
docker compose stop            # or: docker stop --time 120 re-box   (saves everything)
cp -a "$RE_WORKDIR/re-box-state" "$RE_WORKDIR/re-box-state.bak"   # or your REBOX_STATE_DIR
docker compose start
```

A copy of a running container's state dir can catch a Ghidra save half way; take it between
autosaves at least, and expect to fall back to an older copy if Ghidra refuses to open that
program.
