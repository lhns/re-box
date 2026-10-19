#!/bin/sh
# Compile the patched bridge scripts and the host script against the Ghidra install, so a REA
# release that no longer compiles with the attach patch fails the image build, not a session.
# usage: compile-check.sh <ghidra dir> <dir with ReboxReaAttach.java and ghidra_scripts/>
set -eu
ghidra=$1 dir=$2
cp=$(find "$ghidra/Ghidra/Framework" "$ghidra/Ghidra/Features" -name '*.jar' -path '*/lib/*' | sort | tr '\n' ':')
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
javac -proc:none -nowarn -encoding UTF-8 -d "$out" -cp "$cp" "$dir"/ghidra_scripts/*.java "$dir"/ReboxReaAttach.java
echo "compile-check: ok"
