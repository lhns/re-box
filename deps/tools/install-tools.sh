#!/bin/sh
# Installs the tools REA 6.7.0 drives. Run as root during the image build:
#   RUN --mount=type=bind,from=pins,source=/pins/tools.env,target=/run/pins.env \
#       --mount=type=bind,source=deps/tools,target=/build/tools sh /build/tools/install-tools.sh
# Every download is sha256-pinned (versions.env, TOOL_*); Python is installed from hash-locked
# requirements-*.txt; apt packages come from the signed Ubuntu 24.04 archive (a dated snapshot, see
# the Dockerfile's ubuntu stage). Paths the tools land on are set as ENV in the Dockerfile (tools
# stage); tests/smoke/tools.sh checks that every tool runs.
set -eu

. /run/pins.env
. /usr/local/lib/rebox/fetch.sh

TOOLS=/opt/rebox/tools
SRC=/build/tools
export DEBIAN_FRONTEND=noninteractive
export UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/bin/python3.12 UV_NO_CACHE=1

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$TOOLS"
# The versions the tools were installed from, for tests/smoke/tools.sh.
install -m 0644 /run/pins.env "$TOOLS/pins.env"
install -m 0644 "$SRC"/requirements-pwntools.in "$SRC"/requirements-unblob.in "$TOOLS/"

# --- apt ----------------------------------------------------------------------------------------
# tools:    binutils (objdump, strings), rizin's neighbours, gdb, sigrok-cli (0.7.2 = REA's), adb,
#           tshark + capinfos (wireshark-common), general file utilities.
# unblob:   the external extractors it shells out to (7z comes with p7zip-full).
# chrome:   shared libraries Chrome for Testing links against.
# build:    removed again below; only for compiling Binwalk.
tools_pkgs="libfontconfig1 libfreetype6 binutils file xxd unzip p7zip-full gdb sigrok-cli adb tshark wireshark-common"
unblob_pkgs="e2fsprogs squashfs-tools lz4 lzop zstd cpio libmagic1"
chrome_pkgs="fonts-liberation libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libcairo2 libcups2t64
  libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1
  libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2"
build_pkgs="cargo-1.85 rustc-1.85 gcc libc6-dev pkg-config libfontconfig-dev libfreetype-dev"

apt-get update
# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $tools_pkgs $unblob_pkgs $chrome_pkgs $build_pkgs

# --- Binwalk 3.x: no release binaries exist, so build the pinned commit ---------------------------
git init -q "$work/binwalk"
git -C "$work/binwalk" remote add origin https://github.com/ReFirmLabs/binwalk.git
git -C "$work/binwalk" fetch -q --depth 1 origin "$TOOL_BINWALK_COMMIT"
git -C "$work/binwalk" checkout -q FETCH_HEAD
[ "$(git -C "$work/binwalk" rev-parse HEAD)" = "$TOOL_BINWALK_COMMIT" ]
# --locked: crates resolve to the checksums in the repo's Cargo.lock.
(cd "$work/binwalk" && PATH=/usr/lib/rust-1.85/bin:$PATH CARGO_HOME="$work/cargo" cargo build --release --locked --bin binwalk)
install -m 0755 "$work/binwalk/target/release/binwalk" /usr/local/bin/binwalk
rm -rf "$work/binwalk" "$work/cargo"

apt-get purge -y --auto-remove $build_pkgs
apt-get clean
rm -rf /var/lib/apt/lists/*

# --- Python tools, each in its own venv so their dependency sets never meet -----------------------
pyvenv() { # name
  uv venv -q --python "$UV_PYTHON" "$TOOLS/$1"
  uv pip install -q --python "$TOOLS/$1/bin/python" --require-hashes --no-deps --no-build \
    -r "$SRC/requirements-$1.txt"
}
pyvenv pwntools   # REA_PWNTOOLS_PYTHON: pwntools 4.15.0, pyelftools 0.33, unicorn 2.1.2
pyvenv unblob
ln -s "$TOOLS/unblob/bin/unblob" /usr/local/bin/unblob

# --- pwndbg: pinned commit, its own frozen uv.lock, loaded by gdb through REA_PWNDBG_GDBINIT ------
pw=$TOOLS/pwndbg
mkdir -p "$pw"
git init -q "$pw/src"
git -C "$pw/src" remote add origin https://github.com/pwndbg/pwndbg.git
git -C "$pw/src" fetch -q --depth 1 origin "$TOOL_PWNDBG_COMMIT"
git -C "$pw/src" checkout -q FETCH_HEAD
[ "$(git -C "$pw/src" rev-parse HEAD)" = "$TOOL_PWNDBG_COMMIT" ]
# ziglang (~100 MB) only backs the optional cymbol command. Its lock has two sdists (pt,
# intervaltree): built against hash-locked backends (--no-build-isolation), which fails if the
# lock gains another.
uv venv -q --python "$UV_PYTHON" "$pw/venv"
uv pip install -q --python "$pw/venv/bin/python" --require-hashes --no-deps --no-build \
  -r "$SRC/requirements-pwndbg-build.txt"
(cd "$pw/src" && UV_PROJECT_ENVIRONMENT="$pw/venv" uv sync -q --frozen --no-dev --no-install-project \
  --no-install-package ziglang --no-build-isolation --inexact)
uv pip uninstall -q --python "$pw/venv/bin/python" poetry-core
rm -rf "$pw/src/.git"

# --- pinned upstream archives --------------------------------------------------------------------
fetch "$TOOL_WABT_URL" "$TOOL_WABT_SHA256" "$work/wabt.tgz"
mkdir -p "$TOOLS/wabt"
tar -xzf "$work/wabt.tgz" -C "$TOOLS/wabt" --strip-components=1 --wildcards '*/bin'
rm -f "$work/wabt.tgz"

fetch "$TOOL_RIZIN_URL" "$TOOL_RIZIN_SHA256" "$work/rizin.txz"
mkdir -p "$TOOLS/rizin"
# Only the rizin binary and its data: each of the other rz-* tools is another 57 MB static binary.
tar -xJf "$work/rizin.txz" -C "$TOOLS/rizin" bin/rizin share
rm -f "$work/rizin.txz"
ln -s "$TOOLS/rizin/bin/rizin" /usr/local/bin/rizin

fetch "$TOOL_MITMPROXY_URL" "$TOOL_MITMPROXY_SHA256" "$work/mitmproxy.tgz"
mkdir -p "$TOOLS/mitmproxy"
tar -xzf "$work/mitmproxy.tgz" -C "$TOOLS/mitmproxy"
rm -f "$work/mitmproxy.tgz"

mkdir -p "$TOOLS/jadx" "$TOOLS/apktool"
fetch "$TOOL_JADX_MCP_URL" "$TOOL_JADX_MCP_SHA256" "$TOOLS/jadx/jadx-headless-mcp.jar"

fetch "$TOOL_APKTOOL_URL" "$TOOL_APKTOOL_SHA256" "$TOOLS/apktool/apktool.jar"
# Apktool keeps frameworks and the extracted aapt2 under $HOME/.local/share/apktool.
cat > /usr/local/bin/apktool <<EOF
#!/bin/sh
exec java -jar $TOOLS/apktool/apktool.jar "\$@"
EOF
chmod 0755 /usr/local/bin/apktool

fetch "$TOOL_CHROME_URL" "$TOOL_CHROME_SHA256" "$work/chrome.zip"
unzip -q "$work/chrome.zip" -d "$work/chrome"
mv "$work/chrome/chrome-linux64" "$TOOLS/chrome"
rm -rf "$work/chrome" "$work/chrome.zip"
# Keep en-US only; Widevine is DRM and unused.
find "$TOOLS/chrome/locales" -type f ! -name 'en-US.pak' -delete
rm -rf "$TOOLS/chrome/WidevineCdm"

# Everything under /opt/rebox/tools must be readable and executable by the unprivileged runtime user.
chmod -R a+rX "$TOOLS"
