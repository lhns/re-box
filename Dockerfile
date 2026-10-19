# syntax=docker/dockerfile:1
#
# re-box: ghidra-mcp + REA behind one authenticated MCP endpoint. Stages:
#   pins -> base -> ghidra, ghidra-mcp, rea -> runtime-core -> runtime -> test -> final
# `final` copies a marker that only a passing `test` stage writes, so building `final` (the
# ImageBuild target) always runs the test suite. Pins live in versions.env; docs/DECISIONS.md
# says why each departure from the spec exists.

# --- pins: versions.env split per component, so a bump rebuilds only the stages that use it.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS pins
COPY versions.env /pins/versions.env
RUN <<'EOS'
set -eu
cd /pins
split() { grep -E "^($2)=" versions.env > "$1.env" || true; }
split ghidra 'GHIDRA_VERSION|GHIDRA_RELEASE_DATE|GHIDRA_SHA256'
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
jdk_tag=$(printf 'jdk-%s' "$JDK_VERSION" | sed 's/+/%2B/')
jdk_file=$(printf 'OpenJDK21U-jdk_x64_linux_hotspot_%s.tar.gz' "$JDK_VERSION" | tr + _)
fetch "https://github.com/adoptium/temurin21-binaries/releases/download/$jdk_tag/$jdk_file" "$JDK_SHA256" "$t/jdk.tgz"
mkdir -p /opt/java && tar -xzf "$t/jdk.tgz" -C /opt/java --strip-components=1
rm -rf /opt/java/lib/src.zip /opt/java/man
fetch "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" "$NODE_SHA256" "$t/node.txz"
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
fetch "https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_${GHIDRA_VERSION}_build/ghidra_${GHIDRA_VERSION}_PUBLIC_${GHIDRA_RELEASE_DATE}.zip" \
  "$GHIDRA_SHA256" "$t/ghidra.zip"
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
EOS

FROM ghidra-mcp-src AS ghidra-mcp
RUN --mount=type=bind,from=pins,source=/pins/gradle.env,target=/run/pins.env <<'EOS'
set -eu
. /run/pins.env
. /usr/local/lib/rebox/fetch.sh
t=$(mktemp -d)
fetch "https://services.gradle.org/distributions/gradle-$GRADLE_VERSION-bin.zip" "$GRADLE_SHA256" "$t/gradle.zip"
unzip -q "$t/gradle.zip" -d "$t"
cd /src/ghidra-mcp
# Compile only: the Java unit tests would pull junit/mockito from Maven Central unverified.
"$t/gradle-$GRADLE_VERSION/bin/gradle" --no-daemon -q jar -x test -PGHIDRA_INSTALL_DIR=/opt/ghidra
mkdir -p /opt/ghidra-mcp
cp build/libs/GhidraMCP-*.jar /opt/ghidra-mcp/GhidraMCP.jar
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
npm ci --omit=dev --ignore-scripts --no-audit --no-fund
rm -rf /root/.npm
EOS

# --- fixtures: test binaries (spec 8.2).
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS fixtures
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev gcc-mingw-w64-x86-64 binutils \
 && rm -rf /var/lib/apt/lists/*
COPY tests/fixtures/src /src
RUN <<'EOS'
set -eu
mkdir -p /fixtures
gcc -g -O0 -o /fixtures/hello_debug /src/hello.c
gcc -O2 -s -o /fixtures/hello_stripped /src/hello.c
x86_64-w64-mingw32-gcc -O2 -o /fixtures/hello.exe /src/hello.c
cp /fixtures/hello_debug /fixtures/hello_debug_b
printf 'rebox' >> /fixtures/hello_debug_b
EOS

# --- runtime-core: Ghidra, ghidra-mcp and REA, before the extra tools and the aggregator.
FROM base AS runtime-core
COPY --from=ghidra /opt/ghidra /opt/ghidra
COPY --from=ghidra-mcp /opt/ghidra-mcp /opt/ghidra-mcp
COPY --from=rea /opt/rea /opt/rea
RUN ln -s /opt/rea/node_modules/.bin/rea /usr/local/bin/rea
ENV GHIDRA_INSTALL_DIR=/opt/ghidra
