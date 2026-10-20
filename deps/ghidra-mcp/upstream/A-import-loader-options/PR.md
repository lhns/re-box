# PR: import_file takes the loader options AnalyzeHeadless takes

Target `dev`. Two commits: a pure move (`import_file` into `import_tool.py`, because
`static_tools.py` is at 794 of the 800-line cap and `test_bridge_modules_stay_focused` would
fail), then the feature. Independent of the other changes.

## What this changes

`import_file` gains optional parameters; with none of them it behaves as before, apart from the
reuse rule below.

| Parameter | Meaning |
| --- | --- |
| `loader` | `""` (as before: auto-detect, or `BinaryLoader` when a `language` is given), `"auto"` (auto-detect, `language`/`compiler_spec` as hints), or a loader class name (`BinaryLoader`, `MzLoader`, `ElfLoader`, ...) |
| `loader_args` | loader options by name, with or without `-loader-` (`{"baseAddr": "1000:0100"}`) |
| `program_name` | name of the new program in the project (default: file name) |
| `analysis_options` | analyzer options set before auto-analysis; an unknown name or unparsable value refuses the call and removes the import |
| `if_exists` | `reuse` (default), `error`, `unique` (`name.1`, `name.2`, ...) |
| `wait` | `false`: return once imported and saved; analysis continues in the background, `/analysis_status` reports `analyzing` and `import_error` |

`ProgramLoader.builder()` replaces `AutoImporter`. The destination still goes through
`ProjectPaths` (scope, `..`), and refusals are `Refusal`s, not logged errors. Imports are
serialized per destination folder and name, through analysis: a `wait=false` import hands its
destination to the worker thread, which leaves it when analysis is done. A failed import
(analysis options, analysis) removes its program. A program saved by an import that has not
finished carries a `ghidra-mcp import pending` flag in its program info, cleared once analysis is
done; the next import of that name deletes such a program and imports again, whatever
`if_exists` says, instead of reusing a program that never got its analysis. The bridge tool
exposes the parameters (sending only non-default values) and its timeout goes 300 to 1800 s,
because `wait=true` covers analysis.

Fixes #

## Why

- A raw firmware image needs a base address. Today it loads at 0 and the client cannot say
  otherwise.
- A DOS `.COM` needs `BinaryLoader` at `1000:0100` with `x86:LE:16:Real Mode`; an MZ executable
  needs `MzLoader`, where the language is only a hint. `import_file` offers auto-detect or a raw
  binary, nothing between.
- Two clients importing the same new file at once: the existence check and the import are not
  atomic, and Ghidra saves the second as `<name>.0`, a second program of the same bytes that
  tools then report as ambiguous. Now the second waits and reuses the first's program.
- A server that dies during analysis left a saved, unanalyzed program that the next import of
  the same file reused as if it were done (`reused_existing: true`, no analysis, ever). The
  pending flag lets the next import tell, and redo it. The flag and the stored SHA-256 are read
  from the project file's metadata, so the program is not opened to be judged.
- Reuse: a same-named program was opened and reported as the import whatever it held, so
  importing a different build under the same name silently answered about the old one. It is now
  reused only if its recorded SHA-256 matches the file's (a program that records none is reused
  as before); otherwise the call is refused and names `if_exists=unique`, another `program_name`
  or folder. This is a behaviour change; CHANGELOG lists it under Changed.
- `wait=false` exists because a large binary's analysis outlasts a client's request timeout.

Behaviour notes: a program imported under another name is found by that name (the loader names
the program after the source file; `ProjectProgramProvider.match` now also compares the project
file name, for exact and substring lookups). Enum-valued analyzer options cannot be set and are refused with their type.

## Tests you ran

- [x] `./gradlew test --tests 'com.xebyte.offline.*' --tests 'com.xebyte.core.*' "-PGHIDRA_INSTALL_DIR=/opt/ghidra"`:
      884 tests, 0 failures. New: `ImportRequestTest`; three reuse-rule cases and two name-match
      cases (exact and substring on the project file name) in `ProjectProgramProviderTest`;
      `ProgramImportGhidraTest` (real loaders and a real project: BinaryLoader at `1000:0100`,
      named loader and `program_name`, unknown loader and language refused, same bytes reused and
      other bytes refused, `unique`/`error`, unknown and known analysis options, `wait=false`,
      an import that never finished is deleted and redone under every `if_exists`, a finished
      import carries no pending flag, a second import waits for a `wait=false` one instead of
      deleting its program, four concurrent imports make one program).
- [x] `uv run pytest tests/offline tests/unit`: green except
      `test_default_branch_constant_matches_git` (fails on any clone whose HEAD is `dev`; pre-existing).
      New bridge tests: payload forwarding, timeout, poll by path, poll reports `import_error`.
- [x] Could not run the live tier.

## If you touched an @McpTool or @Param annotation

- [x] No Maven here: `tests/endpoints.json` edited by hand in `@Param` order (the parity test
      passes); `python -m tools.audit_server_scope --write` and
      `python -m tools.gen_readme_api_reference --write` report nothing to change. Please
      regenerate with Maven if you want to be sure.

## Checklist

- [x] Branched from `dev` and targeting `dev`
- [x] `CHANGELOG.md` updated
- [x] No unrelated reformatting mixed into the diff
