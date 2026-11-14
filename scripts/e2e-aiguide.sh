#!/bin/bash
# AI guide end-to-end in a real browser: TWO identical apps (pi1, pi2; no roles) on this PC. Extraction by Tag Along's own code, control sweep of every screen, recipes followed from the
# published guide (add a Pi by IP, run to another Pi, one way / both ways, generator, custom test, name + time source, network). Output -> docs/e2e-aiguide-last.log
# Same-host: the always-on reflector, mDNS discovery and the no-network watchdog are switched off (two apps in one network namespace would fight over UDP 20500).
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
export TT_NO_DEFAULT_REFLECTOR=1 TT_NO_DISCOVERY=1 TT_NO_NET_WATCHDOG=1
python3 scripts/e2e_aiguide.py 2>&1 | tee docs/e2e-aiguide-last.log
exit ${PIPESTATUS[0]}
