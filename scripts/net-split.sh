#!/usr/bin/env bash
# Network split for the Pi Automation Traffic Tester: Wi-Fi = management/internet, Ethernet test ports never get a default route.
#   sudo scripts/net-split.sh --apply     make the Ethernet ports "never default" (NetworkManager or dhcpcd, whichever the OS uses)
#   scripts/net-split.sh --check          show the routing self-check (exit 0 ok, 1 warning, 2 problem)
#   sudo scripts/net-split.sh --undo      restore the default behaviour
# Env for tests: TT_NMCLI, TT_DHCPCD_CONF, TT_NM_ACTIVE (1/0).
set -euo pipefail
MODE="${1:---check}"
NMCLI="${TT_NMCLI:-nmcli}"
DHCPCD_CONF="${TT_DHCPCD_CONF:-/etc/dhcpcd.conf}"
BEGIN="# >>> traffic-tester net split"; END="# <<< traffic-tester net split"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${TT_PYTHON:-python3}"

nm_active() { [ "${TT_NM_ACTIVE:-}" = "1" ] && return 0; [ "${TT_NM_ACTIVE:-}" = "0" ] && return 1; command -v "$NMCLI" >/dev/null 2>&1 && "$NMCLI" -t -f RUNNING general 2>/dev/null | grep -q running; }

wired_connections() {            # names of NetworkManager ethernet connection profiles (not Wi-Fi, not bridge/loopback)
  "$NMCLI" -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$2=="802-3-ethernet" || $2=="ethernet" {print $1}'
}

wired_ifaces() { ls /sys/class/net 2>/dev/null | grep -E '^(eth|en)' || true; }

case "$MODE" in
  --apply)
    if nm_active; then
      while IFS= read -r c; do
        [ -n "$c" ] || continue
        "$NMCLI" connection modify "$c" ipv4.never-default yes ipv6.never-default yes
        echo "NetworkManager: '$c' will never be the default route"
        "$NMCLI" -t connection up "$c" >/dev/null 2>&1 || true
      done < <(wired_connections)
    elif [ -f "$DHCPCD_CONF" ] || ! command -v "$NMCLI" >/dev/null 2>&1; then
      touch "$DHCPCD_CONF"
      sed -i "/$BEGIN/,/$END/d" "$DHCPCD_CONF"
      { echo "$BEGIN"; for i in $(wired_ifaces); do printf 'interface %s\nnogateway\n' "$i"; done; echo "$END"; } >> "$DHCPCD_CONF"
      echo "dhcpcd: 'nogateway' set for: $(wired_ifaces | tr '\n' ' ')"
    fi
    ;;
  --undo)
    if nm_active; then
      while IFS= read -r c; do [ -n "$c" ] && "$NMCLI" connection modify "$c" ipv4.never-default no ipv6.never-default no; done < <(wired_connections)
    fi
    [ -f "$DHCPCD_CONF" ] && sed -i "/$BEGIN/,/$END/d" "$DHCPCD_CONF" || true
    echo "restored default behaviour"
    ;;
  --check) exec "$PY" -m traffic_tester.netcheck ;;
  *) echo "usage: $0 --apply | --check | --undo" >&2; exit 64 ;;
esac
