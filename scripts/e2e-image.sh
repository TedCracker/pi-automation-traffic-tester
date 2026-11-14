#!/bin/bash
# The card image booted in QEMU, driven with a phone-size browser. Usage: scripts/e2e-image.sh [main|update]   (needs build-image/pi-traffic-tester-<ver>.img: build-image.sh --keep)
# Output -> docs/e2e-image-<phase>-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
docker image inspect tt-qemu-image >/dev/null 2>&1 || docker build -q --platform linux/amd64 -t tt-qemu-image -f scripts/docker/qemu-image.Dockerfile scripts/docker || exit 1
P="${1:-main}"
python3 scripts/e2e_image.py "$P" 2>&1 | tee "docs/e2e-image-$P-last.log"
exit ${PIPESTATUS[0]}
