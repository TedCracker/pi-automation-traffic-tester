#!/bin/bash
# Mobile sweep: every tab + dialogs at phone / tablet / desktop sizes, light and dark. Output -> docs/e2e-mobile-sweep-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
python3 scripts/e2e_mobile_sweep.py 2>&1 | tee docs/e2e-mobile-sweep-last.log
exit ${PIPESTATUS[0]}
