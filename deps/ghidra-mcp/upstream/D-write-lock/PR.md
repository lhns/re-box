# PR: DirectThreadingStrategy.writeLock(), one write lock per JVM

Target `dev`. Independent. Builds on c30b7a74.

## What this changes

The headless write lock becomes one lock per JVM instead of one per `DirectThreadingStrategy`
instance, and `DirectThreadingStrategy.writeLock()` returns it. The server's own reads and
writes behave as before (there is one strategy instance per server).

Fixes #

## Why

Since c30b7a74 every write holds the lock for its whole transaction, which stops an aborting
edit from taking another thread's work with it (Ghidra merges overlapping transactions). Code
that edits a served program from outside the request path has no lock to take: a script that
runs a long-lived service in the server's JVM, an embedded tool. A per-instance field is
unreachable from there; a JVM-wide one is. Concrete user: a bridge thread inside the server's
JVM that edits the programs the `gh_` tools serve (re-box runs REA's Ghidra bridge that way) and
takes `writeLock()` around its transactions.

If you would rather not offer this as API, the alternative is a per-instance field that
embedders reflect into. This is smaller and says what it is.

## Tests you ran

- [x] `./gradlew test --tests 'com.xebyte.offline.*' "-PGHIDRA_INSTALL_DIR=/opt/ghidra"`:
      766 tests, 0 failures. New:
      `DirectThreadingStrategyTest.everyStrategyInstanceWritesUnderTheOneJvmWideLock` (a write
      through a second instance waits for a transaction open through the first).
- [x] Could not run the live tier.

`DirectThreadingStrategy.java` has mixed CRLF and LF lines; the diff keeps each line's ending,
so the patch contains CRLF lines.

## Checklist

- [x] Branched from `dev` and targeting `dev`
- [x] `CHANGELOG.md` updated
- [x] No unrelated reformatting mixed into the diff
