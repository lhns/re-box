# Building with ImageBuild (kube-oci-composer)

The release image is built in-cluster by kube-oci-composer's `ImageBuild`, which takes exactly one
`target`. Building `final` always runs the test suite: `final` copies
`/opt/rebox/tests-passed`, which only a passing `test` stage writes (docs/DECISIONS.md). CI
(`.github/workflows/build.yml`) builds `test` only and pushes nothing.

The test stage starts the services inside a `RUN`, which works under rootless BuildKit with
`--oci-worker-no-process-sandbox` (no init, no PID namespace): `tests/run-tests.sh` scopes every
process check to its own instance id and kills everything it started before the `RUN` ends.

## Measured locally

Docker Desktop, 20 CPUs, 64 GB, BuildKit, empty build cache (2026-10-19):

| | |
| --- | --- |
| cold build of `test` (all stages, incl. the suite) | 37 min (`install-tools.sh` 15 min, the suite 9-11 min) |
| cold build of `final` | 40 min |
| test stage, peak RSS of the services | 2.5-2.7 GiB (headless JVM + one REA Ghidra JVM at a time) |
| `final` image | 3.66 GB uncompressed |

Rootless BuildKit in a pod is slower than Docker Desktop; allow about twice the local time.

## Draft manifest

Hosting is not decided; replace the URL and the push repository.

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: re-box
  namespace: re-box
spec:
  interval: 1h
  url: https://<forge>/<owner>/re-box
  ref:
    branch: main
---
apiVersion: oci.lhns.de/v1alpha1
kind: ImageBuild
metadata:
  name: re-box
  namespace: re-box
spec:
  interval: 1h
  platforms: [linux/amd64]
  context:
    sourceRef:
      kind: GitRepository
      name: re-box
  target: final
  # Downloads (pinned and digest-checked) and the test suite need the network; the suite only
  # uses loopback.
  network: Sandbox
  # 37 min cold on 20 local CPUs; rootless and fewer CPUs roughly double it.
  timeout: 2h
  resources:
    requests:
      cpu: "4"
      memory: 6Gi
      ephemeral-storage: 30Gi
    limits:
      # The suite peaked at 2.7 GiB; the headless JVM may grow to its 4 GiB heap and REA's JVM to
      # 1.5 GiB, and the Gradle and cargo (binwalk) build steps run JVM/rustc next to BuildKit.
      memory: 12Gi
      ephemeral-storage: 40Gi
  push:
    repository: <registry>/re-box
    history: 10
```

Notes:

- `ephemeral-storage`: the layers (runtime image about 3.7 GB uncompressed, plus Ghidra, JDK,
  Gradle and the test stage) live in the build pod while it runs; 30 GiB leaves headroom.
- The composer's default `timeout` is 30m, too short for a cold build; a warm build with only
  the aggregator or a test changed still reruns the suite (about 10 min).
- Tags: the composer tags after the push (`push.tags`); a composite tag such as
  `ghidra-12.1.4_gmcp-v7.0.0-rc.3_rea-6.7.0` can be added there from `versions.env`.
