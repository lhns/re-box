# PR: GHIDRA_MCP_FILE_ROOT takes several roots

Target `dev`. Independent of the other changes.

## What this changes

`GHIDRA_MCP_FILE_ROOT` accepts a list separated by the platform path separator (`:` on Linux and
macOS, `;` on Windows). A path must fall under one entry. One directory behaves as before.

Fixes #

## Why

A server that must read from two places (an upload directory, and a scratch directory another
tool writes copies of the target into) can only widen the root to a common parent, which is
often `/` or `/tmp`, or drop the guard. Two roots keep the guard useful. Entries are
canonicalized and compared as whole path components (`/workshop` is not under `/work`); blank
entries are ignored; `getFileRoot()` still returns the raw value for log messages.

## Tests you ran

- [x] `./gradlew test --tests 'com.xebyte.offline.*' "-PGHIDRA_INSTALL_DIR=/opt/ghidra"`:
      769 tests, 0 failures. New: `SecurityConfigFileRootsTest` (split and blanks, any-root
      containment, sibling prefix, `..` in a root). The singleton reads the environment once, so
      the static helpers are tested directly.
- [x] `uv run pytest tests/offline tests/unit`: green except
      `test_default_branch_constant_matches_git`, which fails on any clone whose HEAD is `dev`
      (pre-existing, unrelated).
- [x] Could not run the live tier.

No `@McpTool` or `@Param` change. CHANGELOG, README, docker/README, TOOL_USAGE_GUIDE and
`.env.template` are updated.

## Checklist

- [x] Branched from `dev` and targeting `dev`
- [x] `CHANGELOG.md` updated
- [x] No unrelated reformatting mixed into the diff
