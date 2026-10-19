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
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / "versions.env"
GH_ASSET = re.compile(r"^https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/([^/]+)$")


def _request(url: str) -> urllib.request.Request:
    headers = {"User-Agent": "re-box-check-pins"}
    if "api.github.com" in url and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return urllib.request.Request(url, headers=headers)


def github_json(path: str) -> dict:
    with urllib.request.urlopen(_request(f"https://api.github.com/{path}"), timeout=60) as r:
        return json.load(r)


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
        except Exception:  # fall back to downloading
            pass
    h = hashlib.sha256()
    with urllib.request.urlopen(_request(url), timeout=600) as r:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    update = "--update" in sys.argv[1:]
    text = ENV.read_text()
    wanted: dict[str, str] = {}
    problems: list[str] = []

    values = expand(text)
    if "GHIDRA_VERSION" in values:
        release = github_json(f"repos/NationalSecurityAgency/ghidra/releases/tags/Ghidra_{values['GHIDRA_VERSION']}_build")
        names = [a["name"] for a in release["assets"]]
        date = next((re.search(r"_PUBLIC_(\d{8})\.zip$", n).group(1) for n in names if re.search(r"_PUBLIC_\d{8}\.zip$", n)), None)
        if not date:
            problems.append(f"no Ghidra zip asset in {names}")
        else:
            wanted["GHIDRA_RELEASE_DATE"] = date
    # <X>_REF (a git tag) + <X>_COMMIT: the repo is the depName of the renovate comment above.
    for m in re.finditer(r"^#\s*renovate:.*?depName=(\S+).*\n([A-Z][A-Z0-9_]*)_REF=", text, re.M):
        repo, prefix = m.group(1), m.group(2)
        if f"{prefix}_COMMIT" not in values:
            continue
        ref = values[f"{prefix}_REF"]
        out = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}.git", f"refs/tags/{ref}",
                              f"refs/tags/{ref}^{{}}"], capture_output=True, text=True, check=True).stdout
        commits = dict(reversed(line.split("\t")) for line in out.splitlines())
        commit = commits.get(f"refs/tags/{ref}^{{}}") or commits.get(f"refs/tags/{ref}")
        if commit:
            wanted[f"{prefix}_COMMIT"] = commit
        else:
            problems.append(f"{repo} has no tag {ref}")

    # Digests are computed from the URLs as they read after the fixes above.
    for key, value in wanted.items():
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    values = expand(text)
    for key in values:
        if key.endswith("_URL") and f"{key[:-4]}_SHA256" in values:
            url = values[key]
            try:
                wanted[f"{key[:-4]}_SHA256"] = sha256_of(url)
            except Exception as e:
                problems.append(f"{key}: cannot fetch {url}: {e}")

    lock = json.loads((ROOT / "deps/rea/package-lock.json").read_text())
    locked = lock["packages"]["node_modules/rea-agents"]["version"]
    if values.get("REA_VERSION") != locked:
        problems.append(f"REA_VERSION={values.get('REA_VERSION')} but deps/rea locks rea-agents {locked}; "
                        "run `npm install --package-lock-only --ignore-scripts rea-agents@<version>` in deps/rea")

    stale = {k: v for k, v in wanted.items() if values.get(k) != v}
    for key, value in stale.items():
        print(f"{'updated' if update else 'stale'}: {key}={value} (was {values.get(key)})")
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    if update and stale:
        ENV.write_text(text)
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    if problems or (stale and not update):
        return 1
    print(f"versions.env: {len(wanted)} derived values checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
