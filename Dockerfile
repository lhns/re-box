# syntax=docker/dockerfile:1
#
# re-box: ghidra-mcp + REA behind one authenticated MCP endpoint. Stages:
#   pins -> base -> ghidra, ghidra-mcp, rea (-> rea-attach) -> runtime-core -> runtime -> test -> final
# `final` copies a marker that only a passing `test` stage writes, so building `final` (the
# release image CI builds; an in-cluster ImageBuild also works) always runs the test suite.
# Pins live in versions.env; docs/DECISIONS.md says why each departure from the spec exists.

# --- pins: versions.env split per component, so a bump rebuilds only the stages that use it.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS pins
COPY versions.env /pins/versions.env
RUN <<'EOS'
set -eu
cd /pins
split() { grep -E "^($2)=" versions.env > "$1.env" || true; }
split ghidra 'GHIDRA_VERSION|GHIDRA_RELEASE_DATE|GHIDRA_URL|GHIDRA_SHA256'
split ghidra-mcp 'GHIDRA_MCP_[A-Z0-9_]+'
split rea 'REA_VERSION'
split runtimes 'NODE_[A-Z0-9_]+|JDK_[A-Z0-9_]+'
split gradle 'GRADLE_[A-Z0-9_]+'
split tools 'TOOL_[A-Z0-9_]+'
EOS

FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

# --- base: OS packages, JDK, Node, Python (Ubuntu's 3.12) and uv. Shared by build and runtime.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS base
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq procps python3 python3-venv tini unzip util-linux xz-utils \
 && rm -rf /var/lib/apt/lists/*
# fetch <url> <sha256> <dest>: every download is checked against its pin.
COPY rootfs/usr/local/lib/rebox/fetch.sh /usr/local/lib/rebox/fetch.sh
RUN --mount=type=bind,from=pins,source=/pins/runtimes.env,target=/run/pins.env <<'EOS'
set -eu
. /run/pins.env
. /usr/local/lib/rebox/fetch.sh
t=$(mktemp -d)
fetch "$JDK_URL" "$JDK_SHA256" "$t/jdk.tgz"
mkdir -p /opt/java && tar -xzf "$t/jdk.tgz" -C /opt/java --strip-components=1
rm -rf /opt/java/lib/src.zip /opt/java/man
fetch "$NODE_URL" "$NODE_SHA256" "$t/node.txz"
tar -xJf "$t/node.txz" -C /usr/local --strip-components=1 \
  --exclude='*/share/doc' --exclude='*/share/man' --exclude='*/*.md' --exclude='*/LICENSE'
rm -rf "$t"
EOS
COPY --from=uv /uv /usr/local/bin/uv
ENV JAVA_HOME=/opt/java \
    PATH=/opt/java/bin:$PATH \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# --- ghidra: the one install both REA and ghidra-mcp use (DECISIONS.md, 5.1).
FROM base AS ghidra
RUN --mount=type=bind,from=pins,source=/pins/ghidra.env,target=/run/pins.env <<'EOS'
set -eu
. /run/pins.env
. /usr/local/lib/rebox/fetch.sh
t=$(mktemp -d)
fetch "$GHIDRA_URL" "$GHIDRA_SHA256" "$t/ghidra.zip"
unzip -q "$t/ghidra.zip" -d "$t"
mv "$t/ghidra_${GHIDRA_VERSION}_PUBLIC" /opt/ghidra
# Jython is no longer enabled by default since 12.1; unpacked as upstream ghidra-mcp's image does.
unzip -q /opt/ghidra/Extensions/Ghidra/*_Jython.zip -d /opt/ghidra/Ghidra/Extensions/
rm -rf "$t" /opt/ghidra/docs
EOS

# --- ghidra-mcp: headless server jar and the Python bridge in its own venv.
FROM ghidra AS ghidra-mcp-src
RUN --mount=type=bind,from=pins,source=/pins/ghidra-mcp.env,target=/run/pins.env <<'EOS'
set -eu
. /run/pins.env
git init -q /src/ghidra-mcp && cd /src/ghidra-mcp
git remote add origin https://github.com/bethington/ghidra-mcp.git
tag_commit=$(git ls-remote origin "refs/tags/$GHIDRA_MCP_REF^{}" | cut -f1)
[ "$tag_commit" = "$GHIDRA_MCP_COMMIT" ] || { echo "tag $GHIDRA_MCP_REF is $tag_commit, pinned $GHIDRA_MCP_COMMIT" >&2; exit 1; }
git fetch -q --depth 1 origin "$GHIDRA_MCP_COMMIT"
git checkout -q FETCH_HEAD
echo "$GHIDRA_MCP_REF-$GHIDRA_MCP_COMMIT" > /src/ghidra-mcp.version  # keys the aggregator's catalog cache
EOS
# re-box's patches (deps/ghidra-mcp/patches; each header says why and where upstream stands). One
# that no longer applies fails the build; one already upstream (applies in reverse) fails it too,
# so it gets dropped. The patch hash joins the version, which keys the catalog cache.
RUN --mount=type=bind,source=deps/ghidra-mcp/patches,target=/run/patches <<'EOS'
set -eu
cd /src/ghidra-mcp
for p in /run/patches/*.patch; do
  if git apply --check "$p" 2>/dev/null; then
    git apply "$p"
  elif git apply --reverse --check "$p" 2>/dev/null; then
    echo "$(basename "$p") is already in ghidra-mcp $(cat /src/ghidra-mcp.version): drop it" >&2; exit 1
  else
    git apply --check "$p" || true
    echo "$(basename "$p") does not apply to ghidra-mcp $(cat /src/ghidra-mcp.version): rebase it" >&2; exit 1
  fi
done
echo "+rebox-$(cat /run/patches/*.patch | sha256sum | cut -c1-12)" >> /src/ghidra-mcp.version
tr -d '\n' < /src/ghidra-mcp.version > /tmp/v && mv /tmp/v /src/ghidra-mcp.version
EOS

FROM ghidra-mcp-src AS ghidra-mcp
RUN --mount=type=bind,from=pins,source=/pins/gradle.env,target=/run/pins.env <<'EOS'
set -eu
. /run/pins.env
. /usr/local/lib/rebox/fetch.sh
t=$(mktemp -d)
fetch "$GRADLE_URL" "$GRADLE_SHA256" "$t/gradle.zip"
unzip -q "$t/gradle.zip" -d "$t"
cd /src/ghidra-mcp
# Compile only: the Java unit tests would pull junit/mockito from Maven Central unverified.
"$t/gradle-$GRADLE_VERSION/bin/gradle" --no-daemon -q jar -x test -PGHIDRA_INSTALL_DIR=/opt/ghidra
mkdir -p /opt/ghidra-mcp
cp build/libs/GhidraMCP-*.jar /opt/ghidra-mcp/GhidraMCP.jar
cp /src/ghidra-mcp.version /opt/ghidra-mcp/VERSION
rm -rf "$t" /root/.gradle
EOS
# The bridge's runtime deps come from upstream's uv.lock with hashes; the bridge itself from source.
RUN <<'EOS'
set -eu
cd /src/ghidra-mcp
uv export --frozen --no-default-groups --no-emit-project --format requirements-txt -o /tmp/bridge-req.txt
uv venv -q /opt/ghidra-mcp/venv
uv pip install -q -p /opt/ghidra-mcp/venv/bin/python --require-hashes -r /tmp/bridge-req.txt
uv pip install -q -p /opt/ghidra-mcp/venv/bin/python --no-deps .
/opt/ghidra-mcp/venv/bin/bridge-mcp-ghidra --help >/dev/null
EOS

# --- rea: rea-agents from the committed lockfile. --ignore-scripts: the only install script is
# frida's, which downloads an unpinned prebuilt (DECISIONS.md).
FROM base AS rea
COPY deps/rea/package.json deps/rea/package-lock.json /opt/rea/
RUN --mount=type=bind,from=pins,source=/pins/rea.env,target=/run/pins.env <<'EOS'
set -eu
. /run/pins.env
locked=$(jq -r '.packages["node_modules/rea-agents"].version' /opt/rea/package-lock.json)
[ "$locked" = "$REA_VERSION" ] || { echo "deps/rea locks rea-agents $locked, versions.env says $REA_VERSION" >&2; exit 1; }
cd /opt/rea
npm ci --omit=dev --ignore-scripts --no-audit --no-fund --fetch-retries=5
rm -rf /root/.npm
EOS

# --- rea-attach: REA's Ghidra sessions attach to the ghidra-mcp JVM (deps/rea-attach,
# DECISIONS.md). The tests check the patch anchors against this REA release first; any mismatch
# fails the build. Copied into `runtime` late, so a patch change does not rebuild the tools.
FROM rea AS rea-attach
RUN --mount=type=bind,source=deps/rea-attach,target=/src/rea-attach <<'EOS'
set -eu
cd /src/rea-attach
REA_PACKAGE=/opt/rea/node_modules/rea-agents python3 -B -m unittest -q test_patch_rea
REA_PACKAGE=/opt/rea/node_modules/rea-agents node --test helper.test.mjs
python3 -B patch-rea.py /opt/rea/node_modules/rea-agents /opt/rebox/rea-attach/ghidra_scripts
install -m 0644 ReboxReaAttach.java helper.mjs /opt/rebox/rea-attach/
EOS

# --- fixtures: test binaries (spec 8.2).
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS fixtures
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev gcc-mingw-w64-x86-64 gcc-mipsel-linux-gnu binutils \
 && rm -rf /var/lib/apt/lists/*
COPY tests/fixtures/src /src
RUN <<'EOS'
set -eu
mkdir -p /fixtures
gcc -g -O0 -o /fixtures/hello_debug /src/hello.c
gcc -O2 -s -o /fixtures/hello_stripped /src/hello.c
x86_64-w64-mingw32-gcc -O2 -o /fixtures/hello.exe /src/hello.c
# Same code, different bytes: a second target for the session-isolation test.
cp /fixtures/hello_debug /fixtures/other_debug
printf rebox >> /fixtures/other_debug
# Targets REA runs with special Ghidra profiles (e2e/test_rea_profiles.py).
# Freestanding x86_64 ELF at 0x401000 (the seeds target) and the same code as a raw blob: _start
# is first in .text, so the blob's entry is offset 0 (loaded at address 0).
free="-O1 -fno-pic -fno-stack-protector -ffreestanding -nostdlib -static -Wl,--build-id=none -Wl,-e,_start"
gcc $free -no-pie -Wl,-Ttext=0x401000 -o /tmp/freestanding_x86_64 /src/freestanding.c
gcc $free -no-pie -Wl,-Ttext=0 -o /tmp/blob.elf /src/freestanding.c
objcopy -O binary -j .text /tmp/blob.elf /fixtures/blob_x86_64.bin
# Seeds (REA_GHIDRA_SEED_FILE, rea-ghidra-seeds-v1): address<TAB>kind[<TAB>name]. The ELF is
# stripped, so nothing but the seed makes the unreferenced function show up.
strip -o /fixtures/freestanding_x86_64 /tmp/freestanding_x86_64
addr=$(nm /tmp/freestanding_x86_64 | awk '$3 == "hidden_seed_target" { print $1 }')
printf '# re-box fixture\n0x%s\tfunction\trebox_seeded\n' "$addr" > /fixtures/seeds.tsv
# MIPS32r2 little-endian o32 soft-float executable (freestanding.c needs no libgcc helpers).
mipsel-linux-gnu-gcc $free -march=mips32r2 -mabi=32 -msoft-float -mno-abicalls -mnan=legacy \
    -Wl,-Ttext=0x401000 -o /fixtures/mips_softfloat /src/freestanding.c
sh /src/make-dos.sh /fixtures
EOS

# --- runtime-core: Ghidra, ghidra-mcp and REA, before the extra tools and the aggregator.
FROM base AS runtime-core
COPY --from=ghidra /opt/ghidra /opt/ghidra
COPY --from=ghidra-mcp /opt/ghidra-mcp /opt/ghidra-mcp
COPY --from=rea /opt/rea /opt/rea
RUN ln -s /opt/rea/node_modules/.bin/rea /usr/local/bin/rea
ENV GHIDRA_INSTALL_DIR=/opt/ghidra

# --- runtime: everything the release image needs.
FROM runtime-core AS runtime
# Ubuntu's image ships a uid-1000 user `ubuntu`; rebox takes that id.
RUN userdel -r ubuntu 2>/dev/null; groupadd -g 1000 rebox && useradd -u 1000 -g 1000 -m -s /bin/bash rebox
# The analysis tools REA drives on Linux (spec 6.3); deps/tools/install-tools.sh.
RUN --mount=type=bind,from=pins,source=/pins/tools.env,target=/run/pins.env     --mount=type=bind,source=deps/tools,target=/build/tools     sh /build/tools/install-tools.sh
# Where REA finds each tool. REA enables the firmware, packet, signal and WASM features only for
# absolute paths, and compares REA_BROWSER_NO_SANDBOX to the string "true": Chromium's sandbox
# needs user namespaces the container does not grant, the container is the sandbox.
ENV REA_RIZIN_COMMAND=/usr/local/bin/rizin     REA_OBJDUMP_COMMAND=/usr/bin/objdump     REA_GDB_COMMAND=/usr/bin/gdb     REA_JADX_MCP_JAR=/opt/rebox/tools/jadx/jadx-headless-mcp.jar     REA_JADX_HEAP_MIB=2048     REA_APKTOOL_COMMAND=/usr/local/bin/apktool     REA_ADB_PATH=/usr/bin/adb     REA_BINWALK_COMMAND=/usr/local/bin/binwalk     REA_UNBLOB_COMMAND=/usr/local/bin/unblob     REA_FIRMWARE_PRLIMIT_COMMAND=/usr/bin/prlimit     REA_TSHARK_COMMAND=/usr/bin/tshark     REA_CAPINFOS_COMMAND=/usr/bin/capinfos     REA_TSHARK_PRLIMIT_COMMAND=/usr/bin/prlimit     REA_SIGROK_COMMAND=/usr/bin/sigrok-cli     REA_SIGROK_PRLIMIT_COMMAND=/usr/bin/prlimit     REA_WABT_BIN_DIRECTORY=/opt/rebox/tools/wabt/bin     REA_PWNTOOLS_PYTHON=/opt/rebox/tools/pwntools/bin/python     REA_PWNDBG_GDB=/usr/bin/gdb     REA_PWNDBG_GDBINIT=/opt/rebox/tools/pwndbg/src/gdbinit.py     REA_PWNDBG_VENV_PATH=/opt/rebox/tools/pwndbg/venv     REA_MITMDUMP_COMMAND=/opt/rebox/tools/mitmproxy/mitmdump     REA_BROWSER_EXECUTABLE=/opt/rebox/tools/chrome/chrome     REA_BROWSER_NO_SANDBOX=true
# REA's patched `rea mcp` chunk and the attach files (stage rea-attach). The patched bridge must
# still compile against this Ghidra, or sessions would fail at open.
COPY --from=rea-attach /opt/rea/node_modules/rea-agents/dist/mcp /opt/rea/node_modules/rea-agents/dist/mcp
COPY --from=rea-attach /opt/rebox/rea-attach /opt/rebox/rea-attach
RUN --mount=type=bind,source=deps/rea-attach/compile-check.sh,target=/run/compile-check.sh \
    sh /run/compile-check.sh /opt/ghidra /opt/rebox/rea-attach
COPY aggregator /src/aggregator
RUN UV_PROJECT_ENVIRONMENT=/opt/rebox/venv uv sync -q --frozen --no-dev --no-editable --project /src/aggregator \
 && rm -rf /src /root/.cache /tmp/hsperfdata_root /tmp/uv-* /var/tmp/*
# A fresh named volume at /work is root-owned otherwise, and the uid-1000 supervisor refuses it.
# 0755 home: the PUID remap's `usermod` must read it without CAP_DAC_OVERRIDE (it is empty).
RUN mkdir -p /work && chown 1000:1000 /work && chmod 0755 /home/rebox
COPY --chmod=0755 rootfs/usr/local/bin/ /usr/local/bin/
COPY rootfs/usr/local/lib/rebox/ /usr/local/lib/rebox/
COPY rootfs/etc/rebox/ /etc/rebox/
# REA's workflow skill for the client side, with tool names prefixed as the aggregator serves
# them (spec 11). Extract: docker run --rm <image> tar -C /opt/rebox/skills -c . | tar -x -C ~/.claude/skills
RUN python3 /usr/local/lib/rebox/rewrite-skill.py /opt/rea/node_modules/rea-agents/skills /opt/rebox/skills rea_
# Defaults; docs/OPERATIONS.md explains each. Memory: the JAVA_OPTS heap holds ghidra-mcp's and
# REA's programs (REA attaches to that JVM); these fit 16 GiB. The G1 flags give heap back after
# a burst.
# GHIDRA_MCP_ALLOW_SCRIPTS: REA's bridge is started through ghidra-mcp's script endpoint; the
# aggregator denies the script tools to clients (aggregator.toml), and 8089 is loopback-only.
ENV RE_WORKDIR=/work \
    REBOX_PORT=8443 \
    REBOX_MAX_SESSIONS=4 \
    REBOX_SESSION_IDLE_TIMEOUT=1800 \
    REBOX_GHIDRA_IDLE_TIMEOUT=300 \
    REBOX_AUTOSAVE_INTERVAL=300 \
    JAVA_OPTS="-Xmx8g -XX:+UseG1GC -XX:G1PeriodicGCInterval=60000 -XX:G1PeriodicGCSystemLoadThreshold=0 -XX:MinHeapFreeRatio=10 -XX:MaxHeapFreeRatio=30" \
    GHIDRA_MCP_ALLOW_SCRIPTS=1 \
    GHIDRA_MCP_LAZY=0 \
    GHIDRA_MCP_LOG_LEVEL=WARNING \
    REA_ANALYSIS_PROVIDER=ghidra \
    REA_MCP_INPUT_SCHEMA_PROFILE=full \
    REA_MCP_OUTPUT_SCHEMA_PROFILE=full \
    REA_LOG_LEVEL=warn \
    HOME=/home/rebox
LABEL org.opencontainers.image.source="https://github.com/lhns/re-box" \
      org.opencontainers.image.title="re-box" \
      org.opencontainers.image.description="Ghidra, ghidra-mcp and REA behind one authenticated MCP endpoint"
EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 CMD ["/usr/local/bin/re-box-healthcheck"]
USER 1000:1000
WORKDIR /home/rebox
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/re-box-entrypoint"]
CMD ["/opt/rebox/venv/bin/rebox-supervisor"]

# --- ghidra-mcp-test: upstream's offline tier at the pinned ref (spec 8.1), no Ghidra needed.
FROM ghidra-mcp-src AS ghidra-mcp-test
RUN <<'EOS'
set -eu
cd /src/ghidra-mcp
uv sync -q --frozen --no-default-groups --group test
uv run --no-sync pytest -q -p no:cacheprovider --no-cov tests/offline/
uv run --no-sync pytest -q -p no:cacheprovider --no-cov -p tests.offline.replay_plugin tests/integration/test_readonly_endpoints.py
touch /ghidra-mcp-offline-passed
EOS

# --- test: the whole suite (spec 8). Root, to exercise the PUID/PGID remap; tests/run-tests.sh.
FROM runtime AS test
USER root
COPY --from=ghidra-mcp-test /ghidra-mcp-offline-passed /opt/rebox/
COPY --from=fixtures /fixtures /opt/rebox/fixtures
COPY aggregator /src/aggregator
# UV_NO_CACHE: HOME is /home/rebox here, and a root-owned cache there breaks the tools as rebox.
RUN UV_NO_CACHE=1 UV_PROJECT_ENVIRONMENT=/opt/rebox/test-venv uv sync -q --frozen --project /src/aggregator
COPY --chmod=0755 tests /src/tests
RUN /src/tests/run-tests.sh

# --- final: the release image. The marker exists only if `test` passed.
FROM runtime AS final
COPY --from=test /opt/rebox/tests-passed /opt/rebox/tests-passed
