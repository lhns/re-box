# Building re-box

A plain multi-stage Dockerfile; nothing outside `docker build` is needed.

```
docker build --target test .                 # the whole suite runs inside the build
docker build --target final -t re-box .      # the release image
```

`final` copies `/opt/rebox/tests-passed`, which only a passing `test` stage writes, so building
`final` always runs the suite first (docs/DECISIONS.md, "Other departures", Supervision). Stages:
`pins`; `ubuntu -> base -> tools, build-backends, ghidra, rea, fixtures`; `ghidra ->
ghidra-mcp-src -> ghidra-mcp` (and `ghidra-mcp-test`), `rea -> rea-attach`; `tools -> runtime`
(copies ghidra, ghidra-mcp, rea and rea-attach in) `-> test -> final`. `tools` (the analysis
tools) is the runtime's base, so a change to ghidra-mcp, its patches or REA does not reinstall
them (docs/DECISIONS.md, "Build"). All downloads are pinned in `versions.env` and checked against their sha256; after a version
change run `python3 scripts/check-pins.py --update` (docs/OPERATIONS.md, "Upgrades"). apt reads a
dated Ubuntu snapshot (`APT_SNAPSHOT` in `versions.env`; move it by hand to a recent date and run
the test stage). Python build backends and the tools' lockfiles are hash-locked: regenerate with
`uv pip compile --generate-hashes --python-version 3.12 deps/<dir>/requirements-<name>.in -o
deps/<dir>/requirements-<name>.txt` (the header it writes is what Renovate re-runs).

## CI

`.github/workflows/build.yml`:

| job | runs on | what |
| --- | --- | --- |
| `changes` | every push and PR | whether `versions.env`, `scripts/check-pins.py`, `renovate.json`, `deps/rea/package-lock.json` or `.github/workflows/` changed |
| `pins` | when `changes` says so, on `v*` tags and manual runs | `scripts/check-pins.py`: every `versions.env` digest matches its URL (hundreds of MB of downloads) |
| `unit` | every push and PR | check-pins and aggregator unit tests with fake children (seconds, no image) |
| `image` | after `unit` and `pins` (skipped `pins` is fine) | builds `test` (the suite); on `main`, `v*` tags and manual runs also builds and pushes `final` |

- **Registry:** `ghcr.io/lhns/re-box`, pushed with the job's `GITHUB_TOKEN` (`packages: write`).
  Tags: `sha-<short>` always, `latest` on `main`, `X.Y.Z` and `X.Y` for a `vX.Y.Z` git tag.
  OCI labels from `docker/metadata-action`, BuildKit provenance (`mode=min`).
- **Cache:** BuildKit registry cache at `ghcr.io/lhns/re-box:buildcache` (`mode=max`), written by
  `main`/tag builds and read by every build. Not the gha cache: it is capped at 10 GB per repo,
  below this build's layers. A PR that touches only the aggregator or tests reruns just the last
  stages and the suite.
- **PRs** build and test, push nothing and write no cache.
- **Actions** are pinned by commit sha with the version in a comment; Renovate's github-actions
  manager updates them.

### Runner

The `image` job defaults to the GitHub-hosted `ubuntu-24.04` runner, which for a private repo has
2 CPUs, 8 GB RAM and a 6 h job limit; the job frees ~25 GB of preinstalled toolchains first.
That is enough: the first run (empty cache, 2026-10-19) took 11 min for the whole job — the test
stage 7 min including `install-tools.sh` (3 min) and the suite (2 min, peak RSS 2.1 GiB), then
push and cache export 2.5 min. Most of the local 40 min was download and compile time that the
hosted runner's network and CPUs do faster than Docker Desktop. Each run costs its minutes from
the account's included private-repo Actions minutes.

To move it, set repository variables (Settings -> Secrets and variables -> Actions -> Variables):

| variable | example | effect |
| --- | --- | --- |
| `REBOX_RUNNER` | `"re-box"` or `["self-hosted","linux","x64"]` | JSON for `runs-on` of the `image` job |
| `REBOX_BUILD_TIMEOUT` | `600` | job timeout in minutes (default 330; hosted jobs stop at 360 regardless) |

A self-hosted runner needs a Docker daemon with buildx (the job uses the `docker-container`
driver), ~60 GB of disk for the daemon, and memory for BuildKit plus the suite (8 GB is enough;
the test stage needs no privileges). The free-disk step only runs on hosted runners. The
self-hosted runners in k0s-flux (`kube-cluster/apps/github-runner`) are registered per
repository and the Docker-capable one (`k0s-flux`) serves only `lhns/k0s-flux`, with a 20 GiB
graph shared with Renovate; re-box would need a scale set of its own with a dedicated
remote-docker workspace.

## Measured locally

Docker Desktop, 20 CPUs, 64 GB, BuildKit (2026-10-19, empty build cache):

| | |
| --- | --- |
| cold build of `test` (all stages, incl. the suite) | 37 min (the tools stage 15 min, the suite 9-11 min) |
| cold build of `final` | 40 min |
| rebuild after a change to ghidra-mcp patch 0002 | 1669 s with the tools stage after Ghidra, 684 s since it comes first |
| test stage, peak RSS of the services | 2.5-2.7 GiB (the headless JVM plus one REA Ghidra JVM at a time, as REA still ran its own then; not re-measured since the attach route) |
| `final` image | 3.66 GB uncompressed |

The RSS figure sums per-process RSS of the services during the suite (shared pages counted twice)
and does not cover build steps (Gradle, cargo).

## In-cluster

The image can also be built by kube-oci-composer's `ImageBuild` (one `target`, rootless BuildKit):
docs/IMAGEBUILD.md. The test stage is built to run there too (no init, no PID namespace).
