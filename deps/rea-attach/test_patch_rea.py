"""Tests for patch-rea.py. Stdlib only: they run in the image build, before the patch is applied.

    REA_PACKAGE=/opt/rea/node_modules/rea-agents python3 -m unittest test_patch_rea

REA_PACKAGE is an unpatched rea-agents (the version pinned in versions.env); without it, only
the anchor-logic tests run.
"""
from __future__ import annotations

import importlib.util
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

JS = "\n".join(pr.JS_NEEDS) + "\nconst client = clientFactory({ " + pr.JS_SITE + " a: 1 }) });\n"
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
        java = pr.patch_bridge_text(JAVA)
        self.assertNotIn("sessionDefaultAddressSpace", java)
        self.assertIn("SESSION_SPACE.set(x());", java)
        self.assertIn("SESSION_SPACE.get().getName()", java)
        self.assertIn(pr.DISPATCH_NEW, java)

    def test_missing_or_duplicate_anchor_fails(self):
        for broken in (JS.replace(pr.JS_SITE, "launcher: other"), JS + pr.JS_SITE,
                       JS.replace("ghidraJavaLaunch;", "javaLaunch;")):
            with self.assertRaises(pr.PatchError):
                pr.patch_js_text(broken)
        for broken in (JAVA.replace(pr.DISPATCH, "x();"), JAVA + pr.CLASS_DECL,
                       JAVA.replace(pr.SPACE_DECL, ""), JAVA.replace("private record Request", "record Req"),
                       JAVA + "storeHeadlessValue(k, v);"):
            with self.assertRaises(pr.PatchError):
                pr.patch_bridge_text(broken)

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

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_patch_applies(self):
        chunk, bridge = pr.patch(self.pkg, self.out)
        js = chunk.read_text()
        self.assertEqual(js.count(pr.MARK), 1)
        self.assertEqual(js.count(pr.JS_SITE_NEW), 1)
        subprocess.run(["node", "--check", str(chunk)], check=True)
        java = bridge.read_text()
        self.assertNotIn("sessionDefaultAddressSpace", java)
        self.assertEqual(java.count(pr.DISPATCH_NEW), 1)
        self.assertEqual(sorted(p.name for p in self.out.glob("*.java")),
                         sorted(p.name for p in (Path(PACKAGE) / "bridge/ghidra").glob("*.java")))
        # REA's own bridge stays as shipped, for the fallback JVM.
        self.assertNotIn("re-box", (self.pkg / "bridge/ghidra" / pr.BRIDGE).read_text())

    def test_patch_twice_fails(self):
        pr.patch(self.pkg, self.out)
        with self.assertRaises(pr.PatchError):
            pr.patch(self.pkg, self.tmp / "again")


if __name__ == "__main__":
    unittest.main()
