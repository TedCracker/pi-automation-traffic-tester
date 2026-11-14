#!/bin/bash
# Network card in a real browser at phone / dark / desktop sizes against a fake NetworkManager (no real network is touched). Output -> docs/e2e-netcfg-ui-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
python3 scripts/e2e_netcfg_ui.py 2>&1 | tee docs/e2e-netcfg-ui-last.log
exit ${PIPESTATUS[0]}
