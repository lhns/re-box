#!/bin/sh
# Compile the patched bridge scripts and the host script against the Ghidra install, so a REA
# release that no longer compiles with the attach patch fails the image build, not a session.
# usage: compile-check.sh <ghidra dir> <dir with ReboxReaAttach.java, ghidra_scripts/ and import_scripts/>
set -eu
ghidra=$1 dir=$2
cp=$(find "$ghidra/Ghidra/Framework" "$ghidra/Ghidra/Features" -name '*.jar' -path '*/lib/*' | sort | tr '\n' ':')
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
mkdir -p "$out/bridge" "$out/import"
javac -proc:none -nowarn -encoding UTF-8 -d "$out/bridge" -cp "$cp" "$dir"/ghidra_scripts/*.java "$dir"/ReboxReaAttach.java
# A bundle of their own (the import enables their directory alone).
javac -proc:none -nowarn -encoding UTF-8 -d "$out/import" -cp "$cp" "$dir"/import_scripts/*.java
echo "compile-check: ok"
