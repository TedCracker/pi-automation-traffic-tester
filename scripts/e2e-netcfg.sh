#!/bin/bash
# Network settings end to end in an isolated Debian Bookworm container: the REAL install-pi.sh (fresh install -> access point default, re-run -> network kept, --role ignored),
# then the REAL privileged helper (root, polkit-style start through a systemctl shim) driven by the app-side client as an unprivileged user, with a fake nmcli that keeps profiles
# in a JSON file and a fake systemd-run that records the scheduled revert: apply / Keep / revert at the deadline / failed join / fallback AP after 3 minutes / clock op.
# Needs docker + internet (apt, pip). Nothing on the host is touched. Output -> docs/e2e-netcfg-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
docker image inspect tt-netcfg-test >/dev/null 2>&1 || docker build -q -t tt-netcfg-test -f scripts/docker/netcfg-test.Dockerfile . || exit 1
docker run --rm -v "$PWD":/src:ro tt-netcfg-test bash -c 'mkdir -p /work && cd /src && tar cf - --exclude=./venv --exclude=./.git --exclude=./docs --exclude=./.codegraph . | tar xf - -C /work && cd /work && python3 scripts/e2e_netcfg.py' 2>&1 | tee docs/e2e-netcfg-last.log
exit ${PIPESTATUS[0]}
