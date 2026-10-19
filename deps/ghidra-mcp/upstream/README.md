# ghidra-mcp: upstream versions of re-box's patches

Against ghidra-mcp `dev` at `cf8b12db27eb139b28b90eaa3488306da910d759` (2026-10-14,
"perf(get_functions): reuse one decompiler across a call (#575)"). Nothing is opened upstream.
`../patches/0001` (c30b7a74) is already on `dev`; `../patches/0002` is re-box's own change, split
here by concern. Patches are `git format-patch` output (`git am --keep-cr`); some contain CRLF
lines on purpose (`.gitattributes` marks them `-text`).

| Change | Patches | Size | Depends on |
| --- | --- | --- | --- |
| C. Several roots in `GHIDRA_MCP_FILE_ROOT` | `C-file-roots/` | 7 files, +125 -27 | nothing |
| D. `DirectThreadingStrategy.writeLock()` | `D-write-lock/` | 3 files, +66 -1 | nothing (builds on c30b7a74) |
| A. `import_file` loader options, `if_exists`, `wait`, serialization | `A-import-loader-options/` (2 commits: move, feature) | 16 files, +1205 -191 | nothing |
| B. `import_file` scripts, import script roots, script-load lock | `B-import-scripts/` | 19 files, +837 -37 | A and C |

Each folder has `PR.md` (description in dev's PR template). `ISSUE-backport-c30b7a74.md` asks for
c30b7a74 on `release/7.0`.

## Order to open

1. Issue: backport c30b7a74 (no code).
2. C and D, in either order (small, independent).
3. A.
4. B, after A and C are merged.

A, C and D each apply to `dev` alone (`git am --keep-cr`). B applies on top of A and C. Applying
all in sequence conflicts only in `CHANGELOG.md` (every entry is added at the top of
Unreleased); keep both sides. The other files merge cleanly, and the resulting tree equals the
tested branches.

## Verified

Per change, in a container with Ghidra 12.1.4: `./gradlew test --tests 'com.xebyte.offline.*'`
(plus `com.xebyte.core.*` for A and B, which includes the real-Ghidra tests) and `uv run pytest
tests/offline tests/unit`. Baseline on `dev`: 765 offline tests, 0 failures. C 769, D 766,
A 880, B 898 (offline + core), all green. The only Python failure on every branch and on plain
`dev` is `test_default_branch_constant_matches_git` (the clone's HEAD is `dev`). Maven (the CI
gate) and the live tiers were not run; `tests/endpoints.json` was edited by hand.
