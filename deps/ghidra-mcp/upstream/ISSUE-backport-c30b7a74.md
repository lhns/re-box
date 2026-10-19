# Issue: backport c30b7a74 (every program write takes the headless write lock) to release/7.0

Title: `[Bug]: headless writes can join another thread's transaction and be lost (fixed on dev in c30b7a74)`

Use the bug template; the fields below map onto it.

## What happens

On the headless server of 7.0.0 and the 7.0.x line, 21 tools open their own transaction inside
`runOnUi` or `executeRead`, or on the request thread, instead of going through `executeWrite`.
Those writes skip the lock that serializes writes. Ghidra merges transactions that overlap on
one program into a single one, so a write that joins another thread's transaction is committed
or discarded with it: when the other thread aborts (a refused rename, a prototype that fails to
apply), the joined write is rolled back too, while its own call reported success.

Concrete case: two clients on one headless server. One calls `set_function_prototype` with a
calling convention that fails to apply; the other adds comments or labels at the same moment.
The first call aborts its transaction, and the comments the second client was told were saved
are gone. (Observed with an aborting edit and a plain edit running concurrently on one program:
the plain edit was lost.)

## Fix

Already on `dev` as c30b7a74 ("fix(java): every program write takes the headless write lock"):
the 21 tools go through `executeWrite`, a tool's own refusal rolls back without a stack trace,
`set_function_prototype` is one unit, `rename_variables` keeps what its fallback renamed.
`WritesTakeTheWriteLockTest` pins it.

Request: cherry-pick c30b7a74 onto `release/7.0` for a 7.0.x patch release. It applies to
v7.0.0-rc.3 (295e3ad) with care for `DirectThreadingStrategy.java` (mixed CRLF/LF lines).

## Environment

Ghidra 12.1.4, headless server (`GhidraMCPHeadlessServer`), 7.0.0-rc.3. The GUI server
serializes on the Swing thread and is not affected.
