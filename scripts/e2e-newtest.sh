#!/bin/bash
# New-test page end-to-end in a real browser (phone + desktop): 1->1 round trip, 1->1 one way, 1->2, Mark, Save, Swap. Output -> docs/e2e-newtest-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
python3 scripts/e2e_newtest.py 2>&1 | tee docs/e2e-newtest-last.log
exit ${PIPESTATUS[0]}
