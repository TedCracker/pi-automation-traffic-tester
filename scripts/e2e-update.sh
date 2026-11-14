#!/bin/bash
# Software updates, end to end in REAL systemd containers (Debian Bookworm, the real install-pi.sh, real polkit rule, real root helper):
# publish to a local bare git repo (stands in for GitHub) -> Check online -> install -> version -> roll back; upload a real file through the browser;
# tampered / wrong-key / downgrade refused; a broken bundle rolls back by itself; password gate; one Pi sends the update to another Pi. Output -> docs/e2e-update-last.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
python3 scripts/e2e_update.py 2>&1 | tee docs/e2e-update-last.log
exit ${PIPESTATUS[0]}
