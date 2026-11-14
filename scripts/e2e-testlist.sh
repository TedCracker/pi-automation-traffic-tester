#!/bin/bash
# Setup test list (edit/rename/hide/reorder/create/delete+undo, info sheet), About + update banner, auto-refresh, Mirrored Bits mode in a real browser
# (portrait, landscape, desktop). Output -> docs/e2e-testlist-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
python3 scripts/e2e_testlist.py 2>&1 | tee docs/e2e-testlist-last.log
exit ${PIPESTATUS[0]}
