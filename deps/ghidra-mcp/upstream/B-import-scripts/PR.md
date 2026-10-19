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
  path, so symlinks do not escape). Unset: no restriction beyond the existing gate.
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
  elsewhere (a gateway deny list) keeps its deny.
- Only absolute paths to existing `.java` files; checked on the canonical path.
- Why not reuse `run_ghidra_script`'s copy into `~/ghidra_scripts`: a directory may ship its own
  bundle activator, and copying a client-named file there can replace a script of the same name.
  Import scripts enable the script's own directory as a bundle instead. Under the GUI this
  enables that directory in the running bundle host for the session (not persisted).
- Scripts run on the request thread under the program's own script transaction, not under the
  server's write lock; the program is not yet visible to other clients for a `wait=true` import
  (imports of one destination are serialized).

## Tests you ran

- [x] `./gradlew test --tests 'com.xebyte.offline.*' --tests 'com.xebyte.core.*' "-PGHIDRA_INSTALL_DIR=/opt/ghidra"`:
      898 tests, 0 failures. New: `ImportScriptsTest` (request shape, the gate, roots, symlink),
      `ImportScriptsGhidraTest` (real scripts: arguments, pre and post, failures leave nothing
      behind, refusals before any import, concurrent loads, concurrent `/run_ghidra_script`).
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
