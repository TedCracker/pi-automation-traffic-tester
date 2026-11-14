#!/bin/bash
# Verify the Pi install + app on arm64 (QEMU) in a Debian Bookworm container. Output → docs/arm64/arm64-check.log
# Needs docker + arm64 binfmt once:  docker run --privileged --rm tonistiigi/binfmt --install arm64
exec 9>/tmp/tt-test.lock; flock 9          # one test run at a time (fixed ports / namespaces / docker names are shared with other phases)
cd "$(dirname "$0")/.." || exit 1
mkdir -p docs/arm64
NAME=tt-arm64-check
docker rm -f $NAME >/dev/null 2>&1
docker run -d --name $NAME --platform linux/arm64 --privileged -v "$PWD":/src:ro debian:bookworm bash /src/scripts/arm64-inner.sh >/dev/null || exit 1
docker logs -f $NAME > docs/arm64/arm64-check.log 2>&1
docker rm -f $NAME >/dev/null 2>&1
tail -1 docs/arm64/arm64-check.log
