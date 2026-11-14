#!/bin/bash
# Rerunnable local end-to-end test (two identical app instances, socat serial, UDP, TCP, netem). Output → docs/e2e-last.log  (E2E_REVERSE=1: the tests are started on the second instance -> docs/e2e-reverse-last.log)
# Same-host runs: no always-on reflector on the shared port 20500 and no mDNS (the script claims what each test needs, and adds the other instance with POST /api/peers).
exec 9>/tmp/tt-test.lock; flock 9          # one test run at a time (fixed ports / namespaces / docker names are shared with other phases)
cd "$(dirname "$0")/.." || exit 1
export TT_NO_DEFAULT_REFLECTOR=1 TT_NO_DISCOVERY=1
[ -x venv/bin/python ] || { python3 -m venv venv && venv/bin/pip install -q -r requirements-dev.txt; }
LOG=docs/e2e-last.log; [ "${E2E_REVERSE:-}" = 1 ] && LOG=docs/e2e-reverse-last.log
venv/bin/python scripts/e2e_local.py "$@" 2>&1 | tee "$LOG"
exit ${PIPESTATUS[0]}
