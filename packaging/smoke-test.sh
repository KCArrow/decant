#!/usr/bin/env bash
# Check a built portable binary on the current distro:
# it starts, lists drives, flashes and verifies a real bzip2 image to a file,
# and renders every screen offscreen.
#
#   packaging/smoke-test.sh dist/decant-x86_64 [screenshot-dir]
set -euo pipefail
bin=$(realpath "$1")
shots=${2:-shots}
work=$(mktemp -d)
chmod +x "$bin"

echo "== version"
"$bin" --version

echo "== drives"
"$bin" --list-drives

echo "== flash a bzip2 image to a file and verify it"
head -c 20000000 /dev/urandom > "$work/img"
head -c 20000000 /dev/zero >> "$work/img"
bzip2 -k "$work/img"
: > "$work/out"
"$bin" --helper --source "$work/img.bz2" --target "$work/out" --decompressor bzcat \
    --verify --allow-file --no-stdin | tail -n 1 | tee "$work/result.json"
grep -q '"verified": true' "$work/result.json"
cmp "$work/img" "$work/out"

echo "== render the window"
QT_QPA_PLATFORM=offscreen "$bin" --screenshots "$shots"
ls -l "$shots"
