#!/bin/bash
# 2026-11 review: streamlined UI + UI bug regressions in a real browser. Output -> docs/e2e-ui-review-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
python3 scripts/e2e_ui_review.py 2>&1 | tee docs/e2e-ui-review-last.log
exit ${PIPESTATUS[0]}
