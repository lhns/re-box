# PR: import_file runs pre_scripts and post_scripts

Target `dev`. **Depends on** the import loader options change (A: `ImportRequest`, the import
flow, the bridge module) **and** on the several-roots change (C: `canonicalRoots`/`underAny`).
Open after both are merged.

## What this changes

- `pre_scripts` run on the loaded program before its first save and analysis; `post_scripts`
  after auto-analysis. Each is `{"path": "/abs/Script.java", "args": [...]}`, as AnalyzeHeadless
  `-preScript` / `-postScript`. Arguments are passed whole, never split.
- A failing pre-script imports nothing. A failing post-script removes the import, so a retry
  starts from the file. The response carries `script_output`; a failed `wait=false` import is
  reported by `/analysis_status` (`Import failed: ...`).
- New `GHIDRA_MCP_IMPORT_SCRIPT_ROOTS` (path list): scripts must lie under one of them (canonical
  path, so symlinks do not escape). Unset: no restriction beyond the existing gate. Set but
  naming no directory (empty after trimming, or only separators): no import script may run.
- Post-scripts run under the server's write lock, like every other write to a program other
  clients can see, and the import's pending flag (A) is cleared in the same transaction.
- All script loads share one JVM-wide lock (`ImportScripts.LOAD_LOCK`), including
  `/run_ghidra_script`, which now also executes the instance it loaded instead of calling
  `runScript(name, args)` (that looked the script up and loaded it a second time).
- Bridge: `pre_scripts`, `post_scripts`; the poll stops when the import was removed.

Fixes #

## Why

A target that needs preparing before analysis (a COM image given its segment and base, analyzer
state no loader argument covers) or fixing after it (a no-return fix) can only be imported,
then scripted, in two calls, with the unprepared program visible to every other client in
between and no way to make the pair atomic. This is what AnalyzeHeadless users do.

The lock: Ghidra guards a bundle build with a file lock meant for separate JVMs. Two threads of
one JVM building scripts at once throw `OverlappingFileLockException`. Reproduced: four concurrent
loads, and four concurrent `/run_ghidra_script` calls, both fail without the lock and pass with it
(the tests). `run_ghidra_script` loaded its script twice, the second time outside any lock, so
wrapping `getScriptInstance` alone was not enough.

## Security notes

- Scripts are arbitrary Java in the server's JVM. They stay behind `GHIDRA_MCP_ALLOW_SCRIPTS`
  like `run_ghidra_script` and `run_script_inline`, which already run any script file the caller
  can name; this adds no capability to a server that allows scripts, and nothing to one that does
  not (checked before anything is imported).
- `GHIDRA_MCP_IMPORT_SCRIPT_ROOTS` is for the server that wants import scripts but not arbitrary
  ones: with the root set, a client can run the vetted scripts through an import and nothing else
  through it. It does not restrict `run_ghidra_script`; a deployment that denies that tool
  elsewhere (a gateway deny list) keeps its deny. A variable that is set but names no directory
  (`GHIDRA_MCP_IMPORT_SCRIPT_ROOTS=:`, or just blank) allows nothing: a stray separator must not
  switch the restriction off.
- Only absolute paths to existing `.java` files; checked on the canonical path.
- Why not reuse `run_ghidra_script`'s copy into `~/ghidra_scripts`: a directory may ship its own
  bundle activator, and copying a client-named file there can replace a script of the same name.
  Import scripts enable the script's own directory as a bundle instead. Under the GUI this
  enables that directory in the running bundle host for the session (not persisted).
- Pre-scripts run on the request thread on the loaded program, before its first save, so no other
  client can see it. Post-scripts run after analysis, when the program is in the project, under
  the server's write lock (`executeWrite`), so a script's edits cannot merge into another
  request's transaction. Imports of one destination are serialized through analysis and the
  post-scripts.

## Tests you ran

- [x] `./gradlew test --tests 'com.xebyte.offline.*' --tests 'com.xebyte.core.*' "-PGHIDRA_INSTALL_DIR=/opt/ghidra"`:
      905 tests, 0 failures (on top of A and C). New: `ImportScriptsTest` (request shape, the
      gate, roots, roots set but naming no directory, symlink), `ImportScriptsGhidraTest` (real
      scripts: arguments, pre and post, post-scripts inside the write transaction, failures leave
      nothing behind, refusals before any import, roots set but naming no directory, concurrent
      loads, concurrent `/run_ghidra_script`).
- [x] `uv run pytest tests/offline tests/unit`: green except
      `test_default_branch_constant_matches_git` (pre-existing). New bridge tests: payload, poll
      stops on a removed import.
- [x] Could not run the live tier.

## If you touched an @McpTool or @Param annotation

- [x] `tests/endpoints.json` edited by hand (no Maven here); the generators report nothing to
      change. Please regenerate with Maven.

## Checklist

- [x] Branched from `dev` and targeting `dev`
- [x] `CHANGELOG.md` updated
- [x] No unrelated reformatting mixed into the diff
