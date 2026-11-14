#!/bin/bash
# Phase 8 network split proven in network namespaces, both directions (alpha->bravo and bravo->alpha). Needs sudo -n. Output -> docs/e2e-netsplit-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
python3 scripts/e2e_netsplit.py 2>&1 | tee docs/e2e-netsplit-last.log
exit ${PIPESTATUS[0]}
