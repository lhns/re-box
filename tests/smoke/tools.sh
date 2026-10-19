#!/bin/bash
# Smoke test for the bundled analysis tools (spec 8.1, first two bullets). Run as the runtime user with
# the image env (the REA_* tool paths in the Dockerfile). Exits non-zero if
# any tool or REA-reported readiness check fails. Hopper, IDA, JEB, Kuna, ILSpy, macOS/Apple
# features and the REA skill install are expected to be unavailable and are not asserted on.
set -u

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# Apktool and adb keep state under $HOME.
[ -n "${HOME:-}" ] && [ -w "$HOME" ] || { export HOME="$work/home"; mkdir -p "$HOME"; }
export TERM=${TERM:-dumb}
cd "$work"

fail=0
rows=()
row() { rows+=("$(printf '%-24s %-5s %s' "$1" "$2" "$3")"); [ "$2" = ok ] || fail=1; }

# run <name> <expected-line-regex> <cmd...>: the command must exit 0 and print a matching line.
run() {
  local name=$1 re=$2; shift 2
  local out
  if out=$("$@" 2>&1) && printf '%s\n' "$out" | grep -qE "$re"; then
    row "$name" ok "$(printf '%s\n' "$out" | grep -E "$re" | head -n 1 | cut -c1-70)"
  else
    row "$name" FAIL "$(printf '%s\n' "$out" | head -n 1 | cut -c1-70)"
    printf -- '--- %s output:\n%s\n' "$name" "$out" | tail -n 40 >&2
  fi
}
# rea_ok <name> <provider-id> <rea args...>: REA runs the real tool; Evidence names the provider.
rea_ok() {
  local name=$1 provider=$2; shift 2
  local out
  if out=$(rea "$@" --json 2>"$work/err") && [ "$(printf '%s' "$out" | jq -r '.provider.id // empty')" = "$provider" ]; then
    row "rea $name" ok "provider=$provider"
  else
    row "rea $name" FAIL "$(printf '%s' "${out:-$(cat "$work/err")}" | head -c 70)"
  fi
}

# --- 1. the tools' own version commands --------------------------------------------------------
run objdump     '^GNU objdump'                "${REA_OBJDUMP_COMMAND:-objdump}" --version
run strings     '^GNU strings'                strings --version
run file        '^file-'                      file --version
run xxd         '^xxd'                        xxd -v
run 7z          '7-Zip|p7zip'                 7z i
run unzip       '^UnZip'                      unzip -v
run jq          '^jq-'                        jq --version
run git         '^git version'                git --version
run curl        '^curl '                      curl --version
run prlimit     '^prlimit'                    prlimit --version
run gdb         '^GNU gdb'                    "${REA_GDB_COMMAND:-gdb}" --version
run rizin       '^rizin 0\.'                  "${REA_RIZIN_COMMAND:-rizin}" -v
run adb         '^Android Debug Bridge'       "${REA_ADB_PATH:-adb}" version
run apktool     '^[0-9]+\.[0-9]+'             "${REA_APKTOOL_COMMAND:-apktool}" --version
run binwalk     '^binwalk 3\.1\.'             "$REA_BINWALK_COMMAND" --version
run unblob      '^26\.6\.'                    "$REA_UNBLOB_COMMAND" --version
run tshark      '^TShark .* 4\.'              "$REA_TSHARK_COMMAND" --version
run capinfos    '^Capinfos .* 4\.'            "$REA_CAPINFOS_COMMAND" --version
run sigrok-cli  '^sigrok-cli 0\.7\.'          "$REA_SIGROK_COMMAND" --version
run wasm-validate '^1\.0\.42'                 "$REA_WABT_BIN_DIRECTORY/wasm-validate" --version
run wasm-objdump  '^1\.0\.42'                 "$REA_WABT_BIN_DIRECTORY/wasm-objdump" --version
run wasm2wat      '^1\.0\.42'                 "$REA_WABT_BIN_DIRECTORY/wasm2wat" --version
run mitmdump    '^Mitmproxy: 12\.2\.3'        "$REA_MITMDUMP_COMMAND" --version
run chrome      'Chrome for Testing'          "$REA_BROWSER_EXECUTABLE" --version
run jadx-jar    'MANIFEST'                    jar tf "$REA_JADX_MCP_JAR"
run pwntools    "'pwntools': '4.15.0'"        "$REA_PWNTOOLS_PYTHON" -c '
import importlib.metadata as m
print({n: m.version(n) for n in ("pwntools", "pyelftools", "unicorn")})'
run pwndbg      'loaded [0-9]+ pwndbg commands' env PWNDBG_VENV_PATH="$REA_PWNDBG_VENV_PATH" PWNDBG_NO_AUTOUPDATE=1 \
  "$REA_PWNDBG_GDB" -nx -q -batch -ex "source $REA_PWNDBG_GDBINIT"
# Chrome must start headless with the sandbox disabled, as REA_BROWSER_NO_SANDBOX makes REA launch it.
run chrome-headless '<html>'                  "$REA_BROWSER_EXECUTABLE" --headless=new --no-sandbox --disable-gpu --dump-dom about:blank

# --- 2. REA drives the tools on real inputs ------------------------------------------------------
cat > a.wat <<'EOF'
(module (func (export "f") (result i32) i32.const 42))
EOF
"$REA_WABT_BIN_DIRECTORY/wat2wasm" a.wat -o a.wasm
printf 'hello firmware hello firmware hello firmware\n' | gzip > fw.gz
cp /bin/true t.elf

rea_ok inspect-adb-client           adb            inspect-adb-client
rea_ok inspect-apktool-client       apktool        inspect-apktool-client
rea_ok inspect-pcap-client          pcap           inspect-pcap-client
rea_ok inspect-signal-decoders      sigrok-recording inspect-signal-decoders
rea_ok list-objdump-architectures   gnu.objdump    list-objdump-architectures
rea_ok inspect-with-objdump         gnu.objdump    inspect-with-objdump t.elf file_headers
rea_ok execute-rizin-command        rizin          execute-rizin-command t.elf iI
rea_ok inspect-wasm-artifact        wabt-artifact  inspect-wasm-artifact a.wasm
rea_ok inspect-firmware-regions     binwalk        inspect-firmware-regions fw.gz
rea_ok extract-firmware             unblob         extract-firmware fw.gz "$work/extracted"
rea_ok inspect-binary-layout        pwntools-elf   inspect-binary-layout t.elf

# --- 3. rea doctor / rea providers ---------------------------------------------------------------
# doctor exits 1 whenever an optional provider (Hopper, IDA, Kuna) is missing, so only its JSON counts.
rea doctor --json > doctor.json 2>/dev/null
rea providers --json > providers.json 2>/dev/null
if ! jq -e . doctor.json >/dev/null 2>&1 || ! jq -e . providers.json >/dev/null 2>&1; then
  row "rea doctor/providers" FAIL "no JSON output"
else
  for c in node host ghidra ghidra-platform ghidra-architecture ghidra-installation ghidra-version \
           ghidra-headless ghidra-native_decompiler ghidra-java jadx; do
    if jq -e --arg c "$c" '.checks[] | select(.name == $c and .ok == true)' doctor.json >/dev/null; then
      row "doctor: $c" ok ""
    else
      row "doctor: $c" FAIL "$(jq -c --arg c "$c" '.checks[] | select(.name == $c)' doctor.json | head -c 70)"
    fi
  done
  # Any other failing check means a tool we ship regressed; the optional providers below are expected.
  unexpected=$(jq -r '[.checks[] | select(.ok == false) | .name]
    - ["hopper", "ida-registration", "kuna-installation", "skill:identity"] | join(",")' doctor.json)
  if [ -z "$unexpected" ]; then row "doctor: no other failures" ok ""; else row "doctor: other failures" FAIL "$unexpected"; fi
  if jq -e '.analysis_provider_candidates[] | select(.provider.id == "ghidra" and .availability.status == "available")' \
       providers.json >/dev/null; then
    row "providers: ghidra" ok "available"
  else
    row "providers: ghidra" FAIL "$(jq -c '.analysis_provider_candidates[] | select(.provider.id == "ghidra") | .availability' providers.json | head -c 70)"
  fi
fi

printf '%-24s %-5s %s\n' CHECK STATE DETAIL
printf '%s\n' "${rows[@]}"
if [ "$fail" -ne 0 ]; then echo "SMOKE FAILED" >&2; exit 1; fi
echo "SMOKE PASSED"
