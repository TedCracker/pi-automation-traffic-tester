#!/bin/bash
# "No fixed roles" end to end: THREE identical apps in three containers on one Docker network (own DNS, real mDNS), no Responder set up anywhere.
# Round trip / one way / both ways at once / 2 -> 1 and 3-way generator / serial "cable" (socat over TCP) / password on / rename / a Pi going away.
# Needs docker; builds the test image once (scripts/docker/roles-test.Dockerfile). Output -> docs/e2e-roles-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
docker image inspect tt-roles-test >/dev/null 2>&1 || docker build -q -t tt-roles-test -f scripts/docker/roles-test.Dockerfile . || exit 1
python3 scripts/e2e_roles.py 2>&1 | tee docs/e2e-roles-last.log
exit ${PIPESTATUS[0]}
