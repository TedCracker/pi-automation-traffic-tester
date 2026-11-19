#!/bin/bash
# Tests page regressions (list taps after leaving a test page; the page's own ▶ Run test button) in a real browser, desktop + phone. Output -> docs/e2e-testspage-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
python3 scripts/e2e_testspage.py 2>&1 | tee docs/e2e-testspage-last.log
exit ${PIPESTATUS[0]}
