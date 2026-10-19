# Sourced by Dockerfile RUNs. fetch <url> <sha256> <dest>: download, then refuse on digest mismatch.
fetch() {
  curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 -o "$3" "$1"
  echo "${2#sha256:}  $3" | sha256sum -c --quiet - || { echo "digest mismatch for $1" >&2; return 1; }
}
