#!/bin/bash
# Verify the Claude Code install + integration against the REAL claude CLI on arm64 (QEMU, Debian Bookworm). Output -> docs/arm64/arm64-claude.log
exec 9>/tmp/tt-test.lock; flock 9          # one test run at a time (fixed ports / namespaces / docker names are shared with other phases)
cd "$(dirname "$0")/.." || exit 1
mkdir -p docs/arm64
NAME=tt-arm64-claude
docker rm -f $NAME >/dev/null 2>&1
docker run -d --name $NAME --platform linux/arm64 --privileged -v "$PWD":/src:ro debian:bookworm bash /src/scripts/arm64-claude-inner.sh >/dev/null || exit 1
docker logs -f $NAME > docs/arm64/arm64-claude.log 2>&1
docker rm -f $NAME >/dev/null 2>&1
tail -1 docs/arm64/arm64-claude.log
