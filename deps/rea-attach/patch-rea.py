#!/usr/bin/env python3
"""Patch an installed rea-agents package so its Ghidra sessions attach to the ghidra-mcp JVM.

    patch-rea.py <rea-agents package root> <output dir for the attach bridge scripts>

1. JS: in each bundle chunk that constructs GhidraHeadlessLauncher (dist/mcp/main-*.js for `rea mcp`,
   dist/cli-runtime/binary-*.js for the CLI), inject a `javaLaunch` override. When
   REBOX_REA_ATTACH_HELPER is set and the target needs nothing ghidra-mcp's import can't do, REA
   spawns that helper instead of its own JVM, with the AnalyzeHeadless arguments it would have
   used. REA's descriptor, process ownership, handshake and cleanup are unchanged.
2. Java: copy bridge/ghidra/*.java to the output dir and patch ReaGhidraBridge.java to run without
   a HeadlessAnalyzer and to keep its default address space per session thread. REA's own copy
   stays untouched for the fallback path.

Every anchor must match exactly as expected, or the build fails: REA ships often.
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
JS_TAIL = MARK + r"""
function __reboxAttach(target, extensions) {
	const helper = process.env.REBOX_REA_ATTACH_HELPER;
	const root = (process.env.RE_WORKDIR ?? "").replace(/\/+$/u, "");
	// ghidra-mcp imports from its file root (RE_WORKDIR) only, with auto-detected loaders only.
	if (!helper || process.platform === "win32" || root === "" || extensions.length > 0 ||
		typeof target?.path !== "string" || !target.path.startsWith(root + "/")) return {};
	return { javaLaunch: async (options) => {
		const args = options.arguments;
		if (args.some((a) => a === "-loader" || a === "-processor" || a === "-cspec" || a === "-preScript" ||
			a.endsWith("/ReaGhidraMipsSoftFloat.java"))) return ghidraJavaLaunch(options);
		return { command: process.execPath, arguments: [helper, "--target", target.path, ...args],
			environment: options.environment, configuredMaxHeap: undefined };
	} };
}
"""

BRIDGE = "ReaGhidraBridge.java"
CLASS_DECL = "public final class ReaGhidraBridge extends HeadlessScript {"
CLASS_DECL_NEW = CLASS_DECL + """
    // re-box attach mode: no HeadlessAnalyzer in this JVM; the helper starts the bridge only after
    // auto-analysis finished.
    @Override
    public boolean analysisTimeoutOccurred() {
        return false;
    }
    // re-box attach mode: several sessions share this class; each serves on its own thread.
    private static final ThreadLocal<AddressSpace> SESSION_SPACE = new ThreadLocal<>();"""
SPACE_DECL = "    private static AddressSpace sessionDefaultAddressSpace;\n"
SPACE_SET = re.compile(r"sessionDefaultAddressSpace\s*=\s*([^;]+);")


def fail(msg: str) -> None:
    sys.exit(f"patch-rea: {msg}")


def replace_once(text: str, old: str, new: str, where: str) -> str:
    n = text.count(old)
    if n != 1:
        fail(f"{where}: expected 1 match of {old!r}, found {n}")
    return text.replace(old, new)


def patch_js(pkg: Path) -> None:
    chunks = [Path(p) for p in glob.glob(str(pkg / "dist/mcp/main-*.js")) + glob.glob(str(pkg / "dist/cli-runtime/binary-*.js"))]
    sites = [c for c in chunks if JS_SITE in c.read_text()]
    if not any("/dist/mcp/" in c.as_posix() for c in sites):
        fail("no dist/mcp/main-*.js chunk constructs GhidraHeadlessLauncher")
    for chunk in sites:
        text = chunk.read_text()
        if MARK in text:
            fail(f"{chunk} is already patched")
        for needed in ("var GHIDRA_LAUNCH_SUPPORT_CAPTURE_BYTES", "ghidraJavaLaunch = async (options) =>",
                       "this.options.javaLaunch ?? ghidraJavaLaunch",
                       "extensions.length === 0 ? {} : { analysisExtensions: extensions }"):
            if needed not in text:
                fail(f"{chunk}: missing {needed!r}")
        text = replace_once(text, JS_SITE, JS_SITE_NEW, str(chunk))
        chunk.write_text(text + "\n" + JS_TAIL)
        print(f"patched {chunk}")


def patch_bridge(pkg: Path, out: Path) -> None:
    src = pkg / "bridge/ghidra"
    out.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.java"):
        shutil.copy2(f, out / f.name)
    path = out / BRIDGE
    text = path.read_text()
    text = replace_once(text, CLASS_DECL, CLASS_DECL_NEW, BRIDGE)
    text = replace_once(text, SPACE_DECL, "", BRIDGE)
    text, n = SPACE_SET.subn(r"SESSION_SPACE.set(\1);", text)
    if n != 1:
        fail(f"{BRIDGE}: expected 1 assignment of sessionDefaultAddressSpace, found {n}")
    text, n = re.subn(r"\bsessionDefaultAddressSpace\b", "SESSION_SPACE.get()", text)
    if n < 1:
        fail(f"{BRIDGE}: no reads of sessionDefaultAddressSpace")
    if "extends HeadlessScript" not in text or re.search(r"\b(getStoredHeadlessValue|storeHeadlessValue|"
            r"headlessStorageContainsKey|setHeadlessContinuationOption|enableHeadlessAnalysis|"
            r"isHeadlessAnalysisEnabled|isImporting|setHeadlessImportDirectory)\(", text):
        fail(f"{BRIDGE}: uses HeadlessAnalyzer-only API the attach mode does not provide")
    path.write_text(text)
    print(f"patched {path} ({n} reads)")


def main() -> None:
    if len(sys.argv) != 3:
        fail("usage: patch-rea.py <rea-agents root> <bridge output dir>")
    pkg, out = Path(sys.argv[1]), Path(sys.argv[2])
    patch_bridge(pkg, out)
    patch_js(pkg)


if __name__ == "__main__":
    main()
