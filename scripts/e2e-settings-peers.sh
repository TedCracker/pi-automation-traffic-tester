#!/bin/bash
# Settings screen of the no-roles design (This Pi, Pis on this network, Time source, Update another Pi) at phone width, light + dark. Output -> docs/e2e-settings-peers-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
python3 scripts/e2e_settings_peers.py 2>&1 | tee docs/e2e-settings-peers-last.log
exit ${PIPESTATUS[0]}
