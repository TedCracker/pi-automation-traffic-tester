#!/bin/bash
# Host side: build the patched Nexmon driver for every Raspberry Pi OS Pi 5 kernel listed (arm64 containers, QEMU if the host is x86).
#   scripts/pkg/build-nexmon-modules.sh <workdir> <kver>...     e.g. /tmp/nexmon 6.12.109+rpt-rpi-2712 6.12.96+rpt-rpi-2712
# Needs <workdir>/patched (patched DKMS source: see fetch-nexmon.sh) and the image tt-nexmon-builder (docker build -f scripts/pkg/nexmon-builder.Dockerfile).
set -uo pipefail
WD="$1"; shift
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$WD/out"
JOBS="${JOBS:-6}"
run_one() {
  k="$1"
  for img in tt-nexmon-builder tt-nexmon-builder-trixie; do         # newer Pi kernels are built with gcc-14 (needs the Trixie image)
    docker image inspect "$img" >/dev/null 2>&1 || continue
    docker run --rm --platform linux/arm64 -v "$WD/patched:/src:ro" -v "$WD/out:/out" -v "$HERE/nexmon-inner-build.sh:/inner.sh:ro" "$img" bash /inner.sh "$k" /src "/out/log-$k" 2>&1 | tail -12 > "$WD/out/result-$k.txt"
    grep -q '^BUILD-OK' "$WD/out/result-$k.txt" && break
  done
}
export -f run_one; export WD HERE
printf '%s\n' "$@" | xargs -P "$JOBS" -I{} bash -c 'run_one {}'
for k in "$@"; do echo "== $k"; cat "$WD/out/result-$k.txt"; done
