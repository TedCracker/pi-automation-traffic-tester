#!/bin/bash
# Phase 7: the WHOLE Pi Automation Traffic Tester, two identical Pis (no roles), tests started on either one. Runs every suite one after the other and prints one summary.
#   scripts/e2e-full.sh              native suites (unit tests, two-instance e2e started on either one, peers, both ways at once, one way each way, sniffer container e2e, network split, UI/API sweep)
#   scripts/e2e-full.sh --arm64      the same plus the arm64 (QEMU) install + e2e (slow: ~40 min)
# Logs: docs/e2e-last.log  e2e-reverse-last.log  e2e-sniff-last.log  e2e-netsplit-last.log  e2e-ui-sweep-last.log  arm64/*.log
cd "$(dirname "$0")/.." || exit 1
ARM=0; [ "${1:-}" = "--arm64" ] && ARM=1
SUM=docs/e2e-full-summary.txt; : > "$SUM"
row() { printf '%-58s %s\n' "$1" "$2" | tee -a "$SUM"; }
echo "# Pi Automation Traffic Tester full e2e  $(date '+%F %T')" | tee -a "$SUM"
t0=$(date +%s)
echo "== unit tests"; venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tee docs/unit-tests-last.log | tail -3
row "Unit tests (pytest)" "$(tail -1 docs/unit-tests-last.log)"
run() { name="$1"; log="$2"; shift 2; echo "== $name"; "$@" >/dev/null 2>&1; rc=$?; row "$name" "$(grep -E '^# [0-9]+/[0-9]+ checks passed|^[0-9]+/[0-9]+ checks passed' "$log" | tail -1) $(grep -c '| unresolved |' "$log") unresolved (rc=$rc)"; }
run "tests started on the first instance, naming the second (serial, UDP, TCP, netem, GOOSE L2+L3, generator, PDF; peers, both ways at once, one way each way)" docs/e2e-last.log scripts/e2e-local.sh
E2E_REVERSE=1 run "tests started on the second instance, naming the first (same suite)" docs/e2e-reverse-last.log scripts/e2e-local.sh
run "Sniffer, real dumpcap/tshark/bridge, two Pis both ways" docs/e2e-sniff-last.log scripts/e2e-sniff.sh
run "Network split (Wi-Fi internet vs Ethernet test), both ways" docs/e2e-netsplit-last.log scripts/e2e-netsplit.sh
run "Web UI + API sweep of two instances (real browser)" docs/e2e-ui-sweep-last.log scripts/e2e-ui-sweep.sh
if [ "$ARM" = 1 ]; then
  echo "== arm64"; scripts/arm64-check.sh >/dev/null 2>&1; row "arm64 (QEMU) install + unit + e2e (see log)" "$(grep -cE '\| pass \|' docs/arm64/arm64-check.log) pass / $(grep -cE '\| FAIL \|' docs/arm64/arm64-check.log) FAIL / $(grep -c '| unresolved |' docs/arm64/arm64-check.log) unresolved"
fi
row "Total time" "$(( ($(date +%s) - t0) / 60 )) min"
