"""Offline tests for check-pins.py: the lookups are faked, nothing touches the network.

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_pins", Path(__file__).with_name("check-pins.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

ENV = """\
# renovate: datasource=github-releases depName=NationalSecurityAgency/ghidra
GHIDRA_VERSION=1.0
GHIDRA_RELEASE_DATE=20260101
GHIDRA_URL=https://example.invalid/g_${GHIDRA_VERSION}_${GHIDRA_RELEASE_DATE}.zip
GHIDRA_SHA256=aaa

# renovate: datasource=github-tags depName=owner/tool
TOOL_REF=v2
TOOL_COMMIT=111
REA_VERSION=6.0.0
"""


def fakes(date="20260101", commit="111", digests=None, urls=None):
    seen = urls if urls is not None else []

    def digest_of(url):
        seen.append(url)
        return (digests or {}).get(url, "aaa")

    return dict(release_date=lambda v: date, commit_of=lambda repo, ref: commit, digest_of=digest_of)


class CheckPins(unittest.TestCase):
    def wanted(self, text=ENV, rea="6.0.0", **kw):
        return cp.compute_wanted(text, rea, **fakes(**kw))

    def test_up_to_date(self):
        wanted, problems = self.wanted()
        self.assertEqual(problems, [])
        self.assertEqual(cp.stale_values(ENV, wanted), {})

    def test_stale_commit_is_detected(self):
        wanted, problems = self.wanted(commit="222")
        self.assertEqual(problems, [])
        self.assertEqual(cp.stale_values(ENV, wanted), {"TOOL_COMMIT": "222"})

    def test_stale_release_date_and_digest_of_the_new_url(self):
        urls = []
        new_url = "https://example.invalid/g_1.0_20260202.zip"
        wanted, _ = self.wanted(date="20260202", digests={new_url: "bbb"}, urls=urls)
        self.assertEqual(urls, [new_url])  # the digest is of the URL as it will read
        self.assertEqual(cp.stale_values(ENV, wanted), {"GHIDRA_RELEASE_DATE": "20260202", "GHIDRA_SHA256": "bbb"})

    def test_update_writes_only_the_stale_values(self):
        wanted, _ = self.wanted(commit="222")
        updated = cp.set_values(ENV, cp.stale_values(ENV, wanted))
        self.assertEqual(updated, ENV.replace("TOOL_COMMIT=111", "TOOL_COMMIT=222"))
        self.assertEqual(cp.stale_values(updated, wanted), {})

    def test_missing_tag_and_failed_lookup_are_messages(self):
        wanted, problems = self.wanted(commit=None)
        self.assertEqual(problems, ["owner/tool has no tag v2"])
        self.assertNotIn("TOOL_COMMIT", wanted)

        def boom(*_):
            raise cp.PinError("GitHub API repos/x: HTTP 403 (rate limited? set GITHUB_TOKEN)")

        wanted, problems = cp.compute_wanted(ENV, "6.0.0", **{**fakes(), "release_date": boom})
        self.assertEqual(problems, ["GitHub API repos/x: HTTP 403 (rate limited? set GITHUB_TOKEN)"])
        self.assertNotIn("GHIDRA_RELEASE_DATE", wanted)

    def test_download_failure_names_the_url(self):
        def boom(url):
            raise OSError("connection reset")

        _, problems = cp.compute_wanted(ENV, "6.0.0", **{**fakes(), "digest_of": boom})
        self.assertEqual(len(problems), 1)
        self.assertIn("GHIDRA_URL", problems[0])
        self.assertIn("connection reset", problems[0])

    def test_rea_version_must_match_the_lock(self):
        _, problems = self.wanted(rea="6.1.0")
        self.assertEqual(len(problems), 1)
        self.assertIn("REA_VERSION=6.0.0", problems[0])


if __name__ == "__main__":
    unittest.main()
