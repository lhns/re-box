"""Tests for patch-rea.py. Stdlib only: they run in the image build, before the patch is applied.

    REA_PACKAGE=/opt/rea/node_modules/rea-agents python3 -m unittest test_patch_rea

REA_PACKAGE is an unpatched rea-agents (the version pinned in versions.env); without it, only
the anchor-logic tests run.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("patch_rea", Path(__file__).with_name("patch-rea.py"))
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

PACKAGE = os.environ.get("REA_PACKAGE")

JS = ("\n".join(pr.JS_NEEDS) + "\n" + pr.JS_CACHE
      + "\nconst client = clientFactory({ " + pr.JS_SITE + " a: 1 }) });\n")
JAVA = "\n".join(pr.BRIDGE_NEEDS) + "\n" + pr.CLASS_DECL + "\n" + pr.SPACE_DECL + """
    void run() { sessionDefaultAddressSpace =
        x(); }
    void serve() { """ + pr.DISPATCH + """ }
    String f() { return sessionDefaultAddressSpace.getName(); }
}
"""


class AnchorLogic(unittest.TestCase):
    def test_patches_minimal_inputs(self):
        js = pr.patch_js_text(JS)
        self.assertEqual(js.count(pr.JS_SITE_NEW), 1)
        self.assertEqual(js.count("function __reboxAttach("), 1)
        self.assertIn(pr.JS_CACHE_NEW, js)
        self.assertIn("invalidateAnalysisCaches();\n            reboxSeenModification", pr.patch_bridge_text(JAVA))
        java = pr.patch_bridge_text(JAVA)
        self.assertNotIn("sessionDefaultAddressSpace", java)
        self.assertIn("SESSION_SPACE.set(x());", java)
        self.assertIn("SESSION_SPACE.get().getName()", java)
        self.assertIn(pr.DISPATCH_NEW, java)

    def test_missing_or_duplicate_anchor_fails(self):
        for broken in (JS.replace(pr.JS_SITE, "launcher: other"), JS + pr.JS_SITE,
                       JS.replace("?? ghidraJavaLaunch", "?? other"), JS.replace(pr.JS_CACHE, "")):
            with self.assertRaises(pr.PatchError):
                pr.patch_js_text(broken)
        for broken in (JAVA.replace(pr.DISPATCH, "x();"), JAVA + pr.CLASS_DECL,
                       JAVA.replace(pr.SPACE_DECL, ""), JAVA.replace("private record Request", "record Req"),
                       JAVA + "storeHeadlessValue(k, v);"):
            with self.assertRaises(pr.PatchError):
                pr.patch_bridge_text(broken)

    def test_attach_fails_closed(self):
        """Without the helper REA must not quietly start its own JVM; REBOX_REA_ATTACH=0 is explicit."""
        probe = pr.JS_TAIL + """
const target = { path: "/work/x" };
try {
    const r = __reboxAttach(target);
    console.log(typeof r.javaLaunch === "function" ? "helper" : "own-jvm");
} catch (e) { console.log("error"); }
"""

        def run(env):
            return subprocess.run(["node", "-e", probe], env={"PATH": os.environ["PATH"], **env},
                                  capture_output=True, text=True, check=True).stdout.strip()

        self.assertEqual(run({"REBOX_REA_ATTACH_HELPER": "/h.mjs"}), "helper")
        self.assertEqual(run({}), "error")
        self.assertEqual(run({"REBOX_REA_ATTACH": "0"}), "own-jvm")

    def test_helper_gets_rebox_and_token_env_only(self):
        """REA sanitizes its child's environment; the helper needs REBOX_* and ghidra-mcp's token."""
        probe = pr.JS_TAIL + """
__reboxAttach({ path: "/work/x" }).javaLaunch({ environment: { PATH: "/bin" }, arguments: ["a"] })
    .then((l) => console.log(JSON.stringify([l.arguments, l.environment])));
"""
        env = {"PATH": os.environ["PATH"], "REBOX_REA_ATTACH_HELPER": "/h.mjs", "REBOX_INSTANCE": "i",
               "GHIDRA_MCP_AUTH_TOKEN": "tok", "GHIDRA_MCP_URL": "x", "SECRET": "s"}
        out = subprocess.run(["node", "-e", probe], env=env, capture_output=True, text=True, check=True).stdout
        arguments, environment = json.loads(out)
        self.assertEqual(arguments[1:], ["--target", "/work/x", "a"])
        self.assertEqual(environment, {"PATH": "/bin", "REBOX_REA_ATTACH_HELPER": "/h.mjs", "REBOX_INSTANCE": "i",
                                       "GHIDRA_MCP_AUTH_TOKEN": "tok"})

    def test_refuses_to_patch_twice(self):
        with self.assertRaises(pr.PatchError):
            pr.patch_js_text(pr.patch_js_text(JS))
        with self.assertRaises(pr.PatchError):
            pr.patch_bridge_text(pr.patch_bridge_text(JAVA))


@unittest.skipUnless(PACKAGE, "REA_PACKAGE not set")
class PinnedRea(unittest.TestCase):
    """The anchors still match the REA release in versions.env."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pkg = self.tmp / "rea-agents"
        for part in ("dist/mcp", "bridge/ghidra"):
            shutil.copytree(Path(PACKAGE) / part, self.pkg / part)
        self.out = self.tmp / "ghidra_scripts"
        self.imports = self.tmp / "import_scripts"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_patch_applies(self):
        chunk, bridge, imports = pr.patch(self.pkg, self.out, self.imports)
        js = chunk.read_text()
        self.assertEqual(js.count(pr.MARK), 1)
        self.assertEqual(js.count(pr.JS_SITE_NEW), 1)
        self.assertEqual(js.count(pr.JS_CACHE_NEW), 1)
        subprocess.run(["node", "--check", str(chunk)], check=True)
        java = bridge.read_text()
        self.assertNotIn("sessionDefaultAddressSpace", java)
        self.assertEqual(java.count(pr.DISPATCH_NEW), 1)
        self.assertEqual({p.name for p in self.out.glob("*.java")}, pr.BRIDGE_FILES)
        self.assertEqual({p.name for p in imports.glob("*.java")}, pr.IMPORT_SCRIPTS)
        # Import scripts are runnable by any client: never the bridge or its extension loader.
        self.assertFalse({pr.BRIDGE, "ReaGhidraExtensions.java"} & {p.name for p in imports.glob("*")})
        # REA's own bridge stays as shipped.
        self.assertNotIn("re-box", (self.pkg / "bridge/ghidra" / pr.BRIDGE).read_text())

    def test_patch_twice_fails(self):
        pr.patch(self.pkg, self.out, self.imports)
        with self.assertRaises(pr.PatchError):
            pr.patch(self.pkg, self.tmp / "again", self.tmp / "again-imports")

    def test_unclassified_or_headless_scripts_fail(self):
        (self.pkg / "bridge/ghidra/ReaGhidraNew.java").write_text("class ReaGhidraNew {}")
        with self.assertRaises(pr.PatchError):
            pr.patch(self.pkg, self.out, self.imports)
        (self.pkg / "bridge/ghidra/ReaGhidraNew.java").unlink()
        seeds = self.pkg / "bridge/ghidra/ReaGhidraApplySeeds.java"
        seeds.write_text(seeds.read_text() + "\n// isImporting() would need a HeadlessAnalyzer\n")
        with self.assertRaises(pr.PatchError):
            pr.patch(self.pkg, self.out, self.imports)

    def test_launcher_constructed_elsewhere_fails(self):
        (self.pkg / "dist/mcp/other-x.js").write_text("const c = { " + pr.JS_SITE + " } });")
        with self.assertRaises(pr.PatchError):
            pr.patch(self.pkg, self.out, self.imports)


if __name__ == "__main__":
    unittest.main()
