#!/usr/bin/env python3
"""Patch an installed rea-agents package so its Ghidra sessions attach to the ghidra-mcp JVM.

    patch-rea.py <rea-agents package root> <bridge output dir> <import scripts output dir>

1. JS: the `rea mcp` bundle chunk (dist/mcp/main-*.js) that constructs GhidraHeadlessLauncher
   gets a `javaLaunch` override: REA spawns REBOX_REA_ATTACH_HELPER instead of its own JVM, with
   the AnalyzeHeadless arguments it would have used (the helper maps them to ghidra-mcp's import,
   or refuses one it does not know). REA's descriptor, process ownership, handshake and cleanup
   are unchanged. Without the helper REA fails rather than quietly starting its own JVM;
   REBOX_REA_ATTACH=0 is the explicit way back to that, for debugging.
2. Java, from bridge/ghidra/: the bridge (patched to run without a HeadlessAnalyzer, keep its
   default address space per session thread, and edit under ghidra-mcp's write lock) and its
   extensions go to the bridge dir, which only the attach host script loads; REA's pre- and
   post-scripts go to the import scripts dir, the only one ghidra-mcp's import may run scripts
   from. Keeping the bridge out of it matters: run as an import post-script with a descriptor a
   client wrote, it would load the client's jars. REA's own copy stays as shipped.

Every anchor must match exactly as expected, or this fails: REA ships often, and a silent
mismatch would ship a REA that quietly runs its own JVM again or a bridge that doesn't compile.
"""
from __future__ import annotations

import glob
import re
import shutil
import sys
from pathlib import Path

MARK = "/* re-box: attach to ghidra-mcp */"

JS_SITE = "launcher: new GhidraHeadlessLauncher({"
JS_SITE_NEW = JS_SITE + " ...__reboxAttach(target),"
JS_CACHE = ': { cachePolicy: "snapshot" },'
JS_CACHE_NEW = ': { cachePolicy: process.env.REBOX_REA_ATTACH === "0" ? "snapshot" : "live" },'
# What the appended function relies on; checked so a rename fails the build.
JS_NEEDS = (
    "this.options.javaLaunch ?? ghidraJavaLaunch",
    "targetPath: installation.platform === \"win32\" ? target.sourcePath ?? target.path : target.path,",
)
JS_TAIL = MARK + r"""
function __reboxAttach(target) {
	if (process.env.REBOX_REA_ATTACH === "0") return {};  // debugging: REA's own JVM
	const helper = process.env.REBOX_REA_ATTACH_HELPER;
	if (!helper || process.platform === "win32" || typeof target?.path !== "string")
		throw new Error("re-box: REA's Ghidra provider must attach to the shared ghidra-mcp JVM, but " +
			"REBOX_REA_ATTACH_HELPER is not set or the target has no path (REBOX_REA_ATTACH=0 for REA's own JVM)");
	return { javaLaunch: async (options) => {
		const environment = { ...options.environment };
		// REA gives its child a sanitized environment: pass on what the helper needs.
		for (const [key, value] of Object.entries(process.env))
			if ((key.startsWith("REBOX_") || key === "GHIDRA_MCP_AUTH_TOKEN") && value !== undefined) environment[key] = value;
		return { command: process.execPath, arguments: [helper, "--target", target.path, ...options.arguments],
			environment, configuredMaxHeap: undefined };
	} };
}
"""

BRIDGE = "ReaGhidraBridge.java"
CLASS_DECL = "public final class ReaGhidraBridge extends HeadlessScript {"
CLASS_DECL_NEW = CLASS_DECL + """
    // --- re-box attach mode (patch-rea.py): this bridge runs on a thread of the ghidra-mcp JVM.
    // No HeadlessAnalyzer here; the attach helper starts the bridge only after auto-analysis.
    @Override
    public boolean analysisTimeoutOccurred() {
        return false;
    }
    // Sessions share this class, each serving on its own thread.
    private static final ThreadLocal<AddressSpace> SESSION_SPACE = new ThreadLocal<>();
    // Set by ReboxReaAttach: ghidra-mcp's write lock. Ghidra merges concurrent transactions into
    // one, so an aborted REA edit would discard a ghidra_ edit made meanwhile, and the other way round.
    private java.util.concurrent.locks.Lock reboxWriteLock;
    private static final Set<String> REBOX_EDITS =
        Set.of("annotate_native_function", "set_address_name", "set_addresses_names");

    // The program is shared: drop this bridge's inventories whenever anyone changed it since the
    // last request (REA only invalidates them after its own edits).
    private long reboxSeenModification = -1;

    private JsonElement reboxHandle(Request request, SessionDescriptor descriptor) throws Exception {
        long modification = currentProgram.getModificationNumber();
        if (modification != reboxSeenModification) {
            invalidateAnalysisCaches();
            reboxSeenModification = modification;
        }
        if (!REBOX_EDITS.contains(request.method)) return handleRequest(request, descriptor);
        // Writers that don't take the lock (analysis, scripts): wait for their transaction to end
        // rather than join it, but never while holding the lock, which every ghidra_ call needs.
        // Never join one either: an abort on either side would discard the other's work.
        long deadline = System.nanoTime() + 60_000_000_000L;
        while (true) {
            if (reboxWriteLock != null) reboxWriteLock.lockInterruptibly();
            try {
                if (currentProgram.getCurrentTransactionInfo() == null)
                    return handleRequest(request, descriptor);
            }
            finally {
                if (reboxWriteLock != null) reboxWriteLock.unlock();
            }
            if (System.nanoTime() > deadline)
                throw new RequestFailure("program_busy",
                    "The shared Ghidra program stayed busy (analysis or a script) for 60 s; retry later");
            Thread.sleep(20);
        }
    }
    // --- end re-box"""
DISPATCH = "writeSuccess(writer, request.id, handleRequest(request, descriptor));"
DISPATCH_NEW = "writeSuccess(writer, request.id, reboxHandle(request, descriptor));"
SPACE_DECL = "    private static AddressSpace sessionDefaultAddressSpace;\n"
SPACE_SET = re.compile(r"\bsessionDefaultAddressSpace\s*=\s*([^;]+);")
SPACE_READ = re.compile(r"\bsessionDefaultAddressSpace\b")
HEADLESS_ONLY = re.compile(
    r"\b(getStoredHeadlessValue|storeHeadlessValue|headlessStorageContainsKey|"
    r"setHeadlessContinuationOption|enableHeadlessAnalysis|isHeadlessAnalysisEnabled|isImporting|"
    r"setHeadlessImportDirectory)\(")
# Members reboxHandle uses; a rename upstream must fail here, not at bridge compile time.
BRIDGE_NEEDS = ("private JsonElement handleRequest(Request request, SessionDescriptor descriptor)",
                "private record Request(int id, String method,", "import java.util.Set;", "import com.google.gson.JsonElement;",
                "private void invalidateAnalysisCaches()", "RequestFailure(String code, String message)")
# Where each of REA's bridge/ghidra/*.java goes. A file REA adds fails the patch until someone
# decides: anything in IMPORT_SCRIPTS can be run by any client through ghidra_import_file.
BRIDGE_FILES = {"ReaGhidraBridge.java", "ReaGhidraExtensions.java", "ReaGhidraBundleActivator.java"}
IMPORT_SCRIPTS = {"ReaGhidraPrepareRaw.java", "ReaGhidraPrepareCom.java", "ReaGhidraApplySeeds.java",
                  "ReaGhidraAnalysisOptions.java", "ReaGhidraNoReturnFix.java", "ReaGhidraMipsSoftFloat.java",
                  "ReaGhidraBundleActivator.java"}


class PatchError(Exception):
    pass


def replace_once(text: str, old: str, new: str, where: str) -> str:
    n = text.count(old)
    if n != 1:
        raise PatchError(f"{where}: expected 1 match of {old!r}, found {n}")
    return text.replace(old, new)


def patch_js_text(text: str, where: str = "chunk") -> str:
    if MARK in text:
        raise PatchError(f"{where} is already patched")
    for needed in JS_NEEDS:
        if needed not in text:
            raise PatchError(f"{where}: missing {needed!r}")
    text = replace_once(text, JS_SITE, JS_SITE_NEW, where)
    # REA replays analysis results per target, assuming a program nobody else edits; attached, it
    # does not own the program, so ask live (REA's policy for IDA and Kuna, which have the same issue).
    text = replace_once(text, JS_CACHE, JS_CACHE_NEW, where)
    return text + "\n" + JS_TAIL


def patch_bridge_text(text: str) -> str:
    if "re-box attach mode" in text:
        raise PatchError(f"{BRIDGE} is already patched")
    for needed in BRIDGE_NEEDS:
        if needed not in text:
            raise PatchError(f"{BRIDGE}: missing {needed!r}")
    if HEADLESS_ONLY.search(text):
        raise PatchError(f"{BRIDGE}: uses HeadlessAnalyzer-only API that attach mode does not provide")
    text = replace_once(text, CLASS_DECL, CLASS_DECL_NEW, BRIDGE)
    text = replace_once(text, DISPATCH, DISPATCH_NEW, BRIDGE)
    text = replace_once(text, SPACE_DECL, "", BRIDGE)
    text, n = SPACE_SET.subn(r"SESSION_SPACE.set(\1);", text)
    if n != 1:
        raise PatchError(f"{BRIDGE}: expected 1 assignment of sessionDefaultAddressSpace, found {n}")
    text, n = SPACE_READ.subn("SESSION_SPACE.get()", text)
    if n < 1:
        raise PatchError(f"{BRIDGE}: no reads of sessionDefaultAddressSpace")
    return text


def patch(pkg: Path, bridge_out: Path, import_out: Path) -> list[Path]:
    """Patch the package in place; write the bridge to `bridge_out`, the import scripts to `import_out`."""
    # `rea mcp` must construct its launcher in exactly one place, the chunk patched here.
    chunks = [Path(p) for p in glob.glob(str(pkg / "dist/mcp/*.js"))]
    sites = [c for c in chunks if JS_SITE in c.read_text(encoding="utf-8")]
    if len(sites) != 1 or not sites[0].name.startswith("main-"):
        raise PatchError(f"expected GhidraHeadlessLauncher constructed only in dist/mcp/main-*.js, found {sites}")
    src = pkg / "bridge/ghidra"
    shipped = {f.name for f in src.glob("*.java")}
    if shipped - BRIDGE_FILES - IMPORT_SCRIPTS or (BRIDGE_FILES | IMPORT_SCRIPTS) - shipped:
        raise PatchError(f"bridge/ghidra changed: new {sorted(shipped - BRIDGE_FILES - IMPORT_SCRIPTS)}, "
                         f"gone {sorted((BRIDGE_FILES | IMPORT_SCRIPTS) - shipped)}; classify them in patch-rea.py")
    # Every one of them runs in the shared JVM, where there is no HeadlessAnalyzer.
    for name in sorted(shipped):
        if HEADLESS_ONLY.search((src / name).read_text(encoding="utf-8")):
            raise PatchError(f"{name}: uses HeadlessAnalyzer-only API that attach mode does not provide")
    bridge = patch_bridge_text((src / BRIDGE).read_text(encoding="utf-8"))
    chunk = patch_js_text(sites[0].read_text(encoding="utf-8"), str(sites[0]))
    for out, names in ((bridge_out, BRIDGE_FILES), (import_out, IMPORT_SCRIPTS)):
        out.mkdir(parents=True, exist_ok=True)
        for name in names:
            shutil.copy2(src / name, out / name)
    (bridge_out / BRIDGE).write_text(bridge, encoding="utf-8")
    sites[0].write_text(chunk, encoding="utf-8")
    return [sites[0], bridge_out / BRIDGE, import_out]


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit("usage: patch-rea.py <rea-agents root> <bridge output dir> <import scripts output dir>")
    try:
        for path in patch(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])):
            print(f"patch-rea: patched {path}")
    except PatchError as e:
        sys.exit(f"patch-rea: {e} -- REA changed; re-check the attach patch (deps/rea-attach)")


if __name__ == "__main__":
    main()
