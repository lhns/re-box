# Draft: upstream PR to REA (not opened)

If REA accepted this, `patch-rea.py` would shrink to nothing: re-box would set one environment
variable and ship its helper and host script, with no per-release anchor maintenance.

---

**Title:** Opt-in external launcher command for Ghidra sessions

**Summary**

Adds `REA_GHIDRA_LAUNCH_COMMAND`, an absolute path to an executable that REA starts *instead of*
the inspected Ghidra JVM for a Ghidra session. Unset (the default), nothing changes.

The command receives `--target <admitted target path>` followed by exactly the AnalyzeHeadless
argument list REA builds today (project root, `-import <snapshot>`, loader options, scripts,
`-postScript ReaGhidraBridge.java <descriptor>`). Everything around the process stays REA's:
the descriptor and token, the process group and ownership record, the bridge handshake
(run id, provider version, profile digest, target SHA-256), and cleanup. The command must serve
the bridge protocol on the descriptor's endpoint, as the bridge script does, and REA treats its
exit like a JVM exit.

**Motivation**

Hosts that already run a long-lived Ghidra (a headless server, an MCP server, a GUI with a
script runner) want REA's Ghidra workflows against *that* instance and its project: one JVM
instead of one per session, and REA's annotations landing in a project other tools see. Today
the only way is to patch the bundled launcher. The launcher already has `javaLaunch` and
`spawnProcess` seams; this exposes one of them through configuration.

**Changes**

1. `GhidraProviderClient`: when `REA_GHIDRA_LAUNCH_COMMAND` is set (POSIX only), pass a
   `javaLaunch` that returns `{ command, arguments: ["--target", target.path, ...arguments],
   environment }` instead of probing the JVM. Process identity checks use that command.
2. `ReaGhidraBridge.java`, so the bridge can be hosted outside AnalyzeHeadless:
   - `analysisTimeoutOccurred()` → catch `ImproperUseException` (no HeadlessAnalyzer in the
     JVM) and report "not timed out"; the host is responsible for finishing analysis first.
   - `sessionDefaultAddressSpace`: per instance (or per serving thread) instead of `static`, so
     several bridges can serve in one JVM.
3. Docs: the variable, the argument contract, and that `project_ephemeral` / "ephemeral" no
   longer hold for such hosts (annotations persist wherever the host keeps the program).

**Security**

Opt-in, absolute path, same uid; it widens nothing an operator could not already do with
`GHIDRA_INSTALL_DIR` (pointing REA at a Ghidra of their choosing). The bridge still authenticates REA with the descriptor token and
refuses a program whose executable SHA-256 differs from the admitted target.

**Not in scope**

Choosing which target profiles a host can import (raw, DOS, PSP, seeds): the command sees the
arguments and can delegate to `analyzeHeadless` itself.

**Testing**

Unit test for the argument contract; an integration test with a stub command that execs
`analyzeHeadless` with the arguments it received (equivalent to today's behaviour).
