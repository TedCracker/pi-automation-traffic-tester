#!/bin/bash
# Compare runs, baseline and job info in a real browser (phone 390x844 + desktop) against stored results. Output -> docs/e2e-compare-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
python3 scripts/e2e_compare.py 2>&1 | tee docs/e2e-compare-last.log
exit ${PIPESTATUS[0]}
