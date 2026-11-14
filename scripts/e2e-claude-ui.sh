#!/bin/bash
# Browser end-to-end of the Ask Claude sidebar + Claude Code log-in/log-out (stub claude CLI), both directions:
#   scripts/e2e-claude-ui.sh            sidebar on the first instance, the second answers        -> docs/e2e-claude-ui-last.log
#   E2E_REVERSE=1 scripts/e2e-claude-ui.sh   sidebar on the second instance, the first answers  -> docs/e2e-claude-ui-reverse-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
export TT_NO_DEFAULT_REFLECTOR=1 TT_NO_DISCOVERY=1          # two instances on one host: no shared port 20500, no mDNS
LOG=docs/e2e-claude-ui-last.log; [ "${E2E_REVERSE:-}" = 1 ] && LOG=docs/e2e-claude-ui-reverse-last.log
python3 scripts/e2e_claude_ui.py 2>&1 | tee "$LOG"
exit ${PIPESTATUS[0]}
