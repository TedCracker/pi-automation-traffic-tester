#!/bin/bash
# Sniffer end-to-end with real dumpcap/tshark/bridge inside an isolated Debian Bookworm container (own network namespace; nothing on the host is touched).
# Needs docker; builds the test image once (scripts/docker/sniff-test.Dockerfile). Output -> docs/e2e-sniff-last.log
exec 9>/tmp/tt-test.lock; flock 9          # one test run at a time (fixed ports / namespaces / docker names are shared with other phases)
cd "$(dirname "$0")/.." || exit 1
docker image inspect tt-sniff-test >/dev/null 2>&1 || docker build -q -t tt-sniff-test -f scripts/docker/sniff-test.Dockerfile . || exit 1
mkdir -p docs/sniff-samples
docker run --rm --privileged -v "$PWD":/src:ro -v "$PWD/docs/sniff-samples":/out tt-sniff-test bash -c 'mkdir -p /work && cd /src && tar cf - --exclude=./venv --exclude=./.git --exclude=./docs/screenshots --exclude=./.codegraph . | tar xf - -C /work && cd /work && /opt/venv/bin/python scripts/e2e_sniff.py' 2>&1 | tee docs/e2e-sniff-last.log
exit ${PIPESTATUS[0]}
