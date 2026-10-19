#!/usr/bin/env python3
"""Patch an installed rea-agents package so its Ghidra sessions attach to the ghidra-mcp JVM.

    patch-rea.py <rea-agents package root> <output dir for the attach bridge scripts>

1. JS: the `rea mcp` bundle chunk (dist/mcp/main-*.js) that constructs GhidraHeadlessLauncher
   gets a `javaLaunch` override. When REBOX_REA_ATTACH_HELPER is set and the target needs nothing
   ghidra-mcp's import can't do, REA spawns that helper instead of its own JVM, with the
   AnalyzeHeadless arguments it would have used. REA's descriptor, process ownership, handshake
   and cleanup are unchanged. Anything else falls back to REA's own JVM.
2. Java: bridge/ghidra/*.java is copied to the output dir and ReaGhidraBridge.java patched to
   run without a HeadlessAnalyzer, keep its default address space per session thread, and hold
   ghidra-mcp's write lock around its edits. REA's own copy stays untouched for the fallback.

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
JS_SITE_NEW = JS_SITE + " ...__reboxAttach(target, extensions),"
# Bundle-level names the appended function relies on; checked so a rename fails the build.
JS_NEEDS = (
    "var GHIDRA_LAUNCH_SUPPORT_CAPTURE_BYTES, GhidraLaunchSupportError, jvmEnvironmentArguments, "
    "rejectJvmPlatformOverrides, ghidraJavaLaunch;",
    "this.options.javaLaunch ?? ghidraJavaLaunch",
    "extensions.length === 0 ? {} : { analysisExtensions: extensions }",
    "targetPath: installation.platform === \"win32\" ? target.sourcePath ?? target.path : target.path,",
)
# Loader and script options only REA's own AnalyzeHeadless run can apply: raw, DOS, PSP and
# language overrides (-loader/-processor/-cspec), seeds and disabled analyzers (-preScript),
# MIPS soft-float storage (its post-script).
JS_TAIL = MARK + r"""
function __reboxAttach(target, extensions) {
	const helper = process.env.REBOX_REA_ATTACH_HELPER;
	const root = (process.env.RE_WORKDIR ?? "").replace(/\/+$/u, "");
	// ghidra-mcp imports only from its file root (RE_WORKDIR) and only with auto-detected loaders.
	if (!helper || process.platform === "win32" || root === "" || extensions.length > 0 ||
		typeof target?.path !== "string" || !target.path.startsWith(root + "/")) return {};
	return { javaLaunch: async (options) => {
		const args = options.arguments;
		if (args.some((a) => a === "-loader" || a === "-processor" || a === "-cspec" || a === "-preScript" ||
			a.endsWith("/ReaGhidraMipsSoftFloat.java"))) return ghidraJavaLaunch(options);
		const environment = { ...options.environment };
		for (const [key, value] of Object.entries(process.env))
			if (key.startsWith("REBOX_") && value !== undefined) environment[key] = value;
		return { command: process.execPath, arguments: [helper, "--target", target.path, ...args],
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
    // one, so an aborted REA edit would discard a gh_ edit made meanwhile, and the other way round.
    private java.util.concurrent.locks.Lock reboxWriteLock;
    private static final Set<String> REBOX_EDITS =
        Set.of("annotate_native_function", "set_address_name", "set_addresses_names");

    private JsonElement reboxHandle(Request request, SessionDescriptor descriptor) throws Exception {
        if (!REBOX_EDITS.contains(request.method)) return handleRequest(request, descriptor);
        // Writers that don't take the lock (analysis, scripts): wait for their transaction to end
        // rather than join it, but never while holding the lock, which every gh_ call needs.
        long deadline = System.nanoTime() + 60_000_000_000L;
        while (true) {
            if (reboxWriteLock != null) reboxWriteLock.lockInterruptibly();
            try {
                if (currentProgram.getCurrentTransactionInfo() == null || System.nanoTime() > deadline)
                    return handleRequest(request, descriptor);
            }
            finally {
                if (reboxWriteLock != null) reboxWriteLock.unlock();
            }
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
                "private record Request(int id, String method,", "import java.util.Set;", "import com.google.gson.JsonElement;")


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
    return replace_once(text, JS_SITE, JS_SITE_NEW, where) + "\n" + JS_TAIL


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


def patch(pkg: Path, out: Path) -> list[Path]:
    """Patch the package in place and write the attach bridge scripts to `out`."""
    chunks = [Path(p) for p in glob.glob(str(pkg / "dist/mcp/main-*.js"))]
    sites = [c for c in chunks if JS_SITE in c.read_text(encoding="utf-8")]
    if len(sites) != 1:
        raise PatchError(f"expected 1 dist/mcp/main-*.js chunk constructing GhidraHeadlessLauncher, found {len(sites)}")
    src = pkg / "bridge/ghidra"
    bridge = patch_bridge_text((src / BRIDGE).read_text(encoding="utf-8"))
    chunk = patch_js_text(sites[0].read_text(encoding="utf-8"), str(sites[0]))
    out.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.java"):
        shutil.copy2(f, out / f.name)
    (out / BRIDGE).write_text(bridge, encoding="utf-8")
    sites[0].write_text(chunk, encoding="utf-8")
    return [sites[0], out / BRIDGE]


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("usage: patch-rea.py <rea-agents root> <bridge output dir>")
    try:
        for path in patch(Path(sys.argv[1]), Path(sys.argv[2])):
            print(f"patch-rea: patched {path}")
    except PatchError as e:
        sys.exit(f"patch-rea: {e} -- REA changed; re-check the attach patch (deps/rea-attach)")


if __name__ == "__main__":
    main()
