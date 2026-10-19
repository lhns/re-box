#!/usr/bin/env python3
"""Check (or with --update, refresh) the digests and derived values in versions.env.

Renovate bumps the *_VERSION / *_REF lines; this keeps the rest in step:
- <X>_URL + <X>_SHA256: the digest of the file the URL names (GitHub's asset digest when the
  API has one, else a download).
- GHIDRA_RELEASE_DATE: the date part of the release asset's name.
- <X>_COMMIT: the commit the <X>_REF tag points at (repo from the renovate comment's depName).
- REA_VERSION must equal the rea-agents version locked in deps/rea/package-lock.json.

Exit 1 when anything is stale (without --update) or cannot be resolved.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / "versions.env"
GH_ASSET = re.compile(r"^https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/([^/]+)$")


class PinError(Exception):
    """A lookup that failed in a way worth one clear line."""


def _request(url: str) -> urllib.request.Request:
    headers = {"User-Agent": "re-box-check-pins"}
    if "api.github.com" in url and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return urllib.request.Request(url, headers=headers)


def github_json(path: str) -> dict:
    try:
        with urllib.request.urlopen(_request(f"https://api.github.com/{path}"), timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        hint = " (rate limited? set GITHUB_TOKEN)" if e.code in (403, 429) else ""
        raise PinError(f"GitHub API {path}: HTTP {e.code}{hint}") from e
    except OSError as e:
        raise PinError(f"GitHub API {path}: {e}") from e


def expand(text: str) -> dict[str, str]:
    """Values as the Dockerfile sees them: the file sourced by sh."""
    keys = re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M)
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write(text)
    try:
        script = f". '{Path(f.name).as_posix()}'\n" + "".join(f'printf "%s=%s\\n" {k} "${{{k}}}"\n' for k in keys)
        out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True).stdout
    finally:
        os.unlink(f.name)
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def sha256_of(url: str) -> str:
    m = GH_ASSET.match(url)
    if m:
        repo, tag, name = m.groups()
        try:
            release = github_json(f"repos/{repo}/releases/tags/{urllib.parse.unquote(tag)}")
            for asset in release.get("assets", []):
                if asset["name"] == name and (asset.get("digest") or "").startswith("sha256:"):
                    return asset["digest"].split(":", 1)[1]
        except PinError:  # fall back to downloading
            pass
    h = hashlib.sha256()
    with urllib.request.urlopen(_request(url), timeout=600) as r:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ghidra_release_date(version: str) -> str:
    release = github_json(f"repos/NationalSecurityAgency/ghidra/releases/tags/Ghidra_{version}_build")
    names = [a["name"] for a in release["assets"]]
    for n in names:
        m = re.search(r"_PUBLIC_(\d{8})\.zip$", n)
        if m:
            return m.group(1)
    raise PinError(f"no Ghidra zip asset in {names}")


def tag_commit(repo: str, ref: str) -> str | None:
    """The commit a tag points at (peeled when annotated), None when the repo has no such tag."""
    try:
        out = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}.git", f"refs/tags/{ref}",
                              f"refs/tags/{ref}^{{}}"], capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError as e:
        raise PinError(f"git ls-remote {repo}: {e.stderr.strip() or e}") from e
    commits = dict(reversed(line.split("\t")) for line in out.splitlines())
    return commits.get(f"refs/tags/{ref}^{{}}") or commits.get(f"refs/tags/{ref}")


def set_values(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = re.sub(rf"^{key}=.*$", lambda _: f"{key}={value}", text, flags=re.M)
    return text


def compute_wanted(text: str, locked_rea: str, *, release_date=ghidra_release_date, commit_of=tag_commit,
                   digest_of=sha256_of) -> tuple[dict[str, str], list[str]]:
    """What versions.env should say for each derived value, and the problems met on the way.

    The lookups are parameters so a test can run this offline.
    """
    values = expand(text)
    wanted: dict[str, str] = {}
    problems: list[str] = []

    def attempt(what: str, fn, *args):
        try:
            return fn(*args)
        except PinError as e:
            problems.append(str(e))
        except Exception as e:  # network, DNS, timeouts: one line, not a traceback
            problems.append(f"{what}: {e}")

    if "GHIDRA_VERSION" in values:
        date = attempt("Ghidra release date", release_date, values["GHIDRA_VERSION"])
        if date:
            wanted["GHIDRA_RELEASE_DATE"] = date
    # <X>_REF (a git tag) + <X>_COMMIT: the repo is the depName of the renovate comment above.
    for m in re.finditer(r"^#\s*renovate:.*?depName=(\S+).*\n([A-Z][A-Z0-9_]*)_REF=", text, re.M):
        repo, prefix = m.group(1), m.group(2)
        if f"{prefix}_COMMIT" not in values:
            continue
        ref = values[f"{prefix}_REF"]
        failed = len(problems)
        commit = attempt(f"{repo} tag {ref}", commit_of, repo, ref)
        if commit:
            wanted[f"{prefix}_COMMIT"] = commit
        elif len(problems) == failed:
            problems.append(f"{repo} has no tag {ref}")

    # Digests are of the URLs as they read once the values above are in.
    for key, url in expand(set_values(text, wanted)).items():
        if key.endswith("_URL") and f"{key[:-4]}_SHA256" in values:
            digest = attempt(f"{key}: cannot fetch {url}", digest_of, url)
            if digest:
                wanted[f"{key[:-4]}_SHA256"] = digest

    if values.get("REA_VERSION") != locked_rea:
        problems.append(f"REA_VERSION={values.get('REA_VERSION')} but deps/rea locks rea-agents {locked_rea}; "
                        "run `npm install --package-lock-only --ignore-scripts rea-agents@<version>` in deps/rea")
    return wanted, problems


def stale_values(text: str, wanted: dict[str, str]) -> dict[str, str]:
    """The wanted values that differ from what the file says now."""
    current = expand(text)
    return {k: v for k, v in wanted.items() if current.get(k) != v}


def main() -> int:
    update = "--update" in sys.argv[1:]
    text = ENV.read_text()
    lock = json.loads((ROOT / "deps/rea/package-lock.json").read_text())
    wanted, problems = compute_wanted(text, lock["packages"]["node_modules/rea-agents"]["version"])
    stale = stale_values(text, wanted)
    current = expand(text)
    for key, value in stale.items():
        print(f"{'updated' if update else 'stale'}: {key}={value} (was {current.get(key)})")
    if update and stale:
        ENV.write_text(set_values(text, stale))
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    if problems or (stale and not update):
        return 1
    print(f"versions.env: {len(wanted)} derived values checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
