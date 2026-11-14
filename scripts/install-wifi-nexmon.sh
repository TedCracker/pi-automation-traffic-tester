#!/usr/bin/env bash
# Pi Automation Traffic Tester - built-in Wi-Fi sniffing (Nexmon) installer. Called by install-pi.sh; can also be run by hand:
#   sudo scripts/install-wifi-nexmon.sh <nexmon-dir>  [--uninstall]
# <nexmon-dir> (inside the offline package: nexmon/) holds   modules/<kernel>/brcmfmac.ko   and   firmware/cyfmac43455-sdio-nexmon.bin
# It installs the root helper + systemd units + permission rule. It does NOT touch the stock Wi-Fi driver or firmware:
# the Nexmon driver is loaded only while sniffing, and "Restore normal Wi-Fi" / stop / reboot puts the stock one back.
set -euo pipefail
ROOT="${TT_INSTALL_ROOT:-}"                                        # test sandbox prefix; empty on a real Pi
OPT=$ROOT/opt/traffic-tester; NEXDIR=$OPT/nexmon; SBIN=$OPT/sbin
UNITDIR=$ROOT/etc/systemd/system; POLKIT=$ROOT/etc/polkit-1/rules.d/49-traffic-tester-wifi.rules; FWDIR=$ROOT/lib/firmware/cypress
SVC_USER="${SVC_USER:-traffic-tester}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  command -v systemctl >/dev/null 2>&1 && { systemctl disable --now tt-wifi-watchdog.timer tt-wifi-boot-restore.service 2>/dev/null || true
                                            systemctl start tt-wifi-helper@restore.service 2>/dev/null || true; }
  command -v update-alternatives >/dev/null 2>&1 && update-alternatives --remove cyfmac43455-sdio.bin "$FWDIR/cyfmac43455-sdio-nexmon.bin" 2>/dev/null || true
  rm -f "$UNITDIR"/tt-wifi-helper@.service "$UNITDIR"/tt-wifi-watchdog.service "$UNITDIR"/tt-wifi-watchdog.timer "$UNITDIR"/tt-wifi-boot-restore.service "$POLKIT" "$FWDIR/cyfmac43455-sdio-nexmon.bin"
  rm -rf "$NEXDIR" "$SBIN"
  command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload 2>/dev/null || true
  say "Built-in Wi-Fi sniffing removed; stock Wi-Fi untouched."; exit 0
fi

SRC="${1:-}"
[ -n "$SRC" ] && [ -d "$SRC" ] || { echo "usage: $0 <nexmon-dir> [--uninstall]" >&2; exit 2; }
KVER="${TT_KVER:-$(uname -r)}"

# ---- root-owned helper (the app's own copy is writable by the service user, so root never runs that one)
install -d -m 755 -o root -g root "$OPT" "$SBIN" "$NEXDIR"
install -m 755 -o root -g root "$HERE/traffic_tester/sniff/wifi_helper.py" "$SBIN/tt-wifi-helper"

# ---- driver for this kernel (prebuilt; see README for other kernels)
HAVE_MOD=0
if [ -d "$SRC/modules" ]; then
  for d in "$SRC"/modules/*/; do
    k="$(basename "$d")"; [ -f "$d/brcmfmac.ko" ] || continue
    install -d -m 755 -o root -g root "$NEXDIR/$k"; install -m 644 -o root -g root "$d/brcmfmac.ko" "$NEXDIR/$k/brcmfmac.ko"
  done
fi
[ -f "$NEXDIR/$KVER/brcmfmac.ko" ] && HAVE_MOD=1
if [ -d "$SRC/build" ]; then install -d -m 755 -o root -g root "$NEXDIR/build"; cp -r "$SRC/build/." "$NEXDIR/build/"; chmod 755 "$NEXDIR/build/"*.sh 2>/dev/null || true; fi

# ---- Nexmon firmware: its own file, selectable through the system's firmware "alternatives" - the stock file stays the default
install -d -m 755 "$FWDIR"
if [ -f "$SRC/firmware/cyfmac43455-sdio-nexmon.bin" ]; then
  install -m 644 -o root -g root "$SRC/firmware/cyfmac43455-sdio-nexmon.bin" "$FWDIR/cyfmac43455-sdio-nexmon.bin"
  if command -v update-alternatives >/dev/null 2>&1 && update-alternatives --query cyfmac43455-sdio.bin >/dev/null 2>&1; then
    update-alternatives --install "$FWDIR/cyfmac43455-sdio.bin" cyfmac43455-sdio.bin "$FWDIR/cyfmac43455-sdio-nexmon.bin" 5 >/dev/null   # priority 5: never chosen automatically
    update-alternatives --auto cyfmac43455-sdio.bin >/dev/null
  else
    warn "No firmware alternatives group on this system; the helper will switch the firmware link itself (and put it back)."
  fi
fi

# ---- systemd units + permission rule
install -d -m 755 "$UNITDIR" "$(dirname "$POLKIT")"
cat > "$UNITDIR/tt-wifi-helper@.service" <<UNIT
[Unit]
Description=Pi Automation Traffic Tester built-in Wi-Fi helper (%i)
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $SBIN/tt-wifi-helper %i
TimeoutStartSec=170
UNIT
cat > "$UNITDIR/tt-wifi-watchdog.service" <<UNIT
[Unit]
Description=Pi Automation Traffic Tester: restore normal Wi-Fi if the app stopped while the built-in Wi-Fi was sniffing
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $SBIN/tt-wifi-helper watchdog
TimeoutStartSec=170
UNIT
cat > "$UNITDIR/tt-wifi-watchdog.timer" <<UNIT
[Unit]
Description=Pi Automation Traffic Tester Wi-Fi watchdog
[Timer]
OnBootSec=30
OnUnitActiveSec=10
AccuracySec=1
[Install]
WantedBy=timers.target
UNIT
cat > "$UNITDIR/tt-wifi-boot-restore.service" <<UNIT
[Unit]
Description=Pi Automation Traffic Tester: make sure normal Wi-Fi is selected after a reboot
Before=NetworkManager.service wpa_supplicant.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $SBIN/tt-wifi-helper restore --if-needed
TimeoutStartSec=170
[Install]
WantedBy=multi-user.target
UNIT
cat > "$POLKIT" <<RULE
// Pi Automation Traffic Tester: the web app's service user may start ONLY the Wi-Fi helper units (fixed command names), nothing else.
polakit_unused = null;
polkit.addRule(function(action, subject) {
  if (action.id == "org.freedesktop.systemd1.manage-units" && subject.user == "$SVC_USER") {
    var unit = action.lookup("unit"), verb = action.lookup("verb");
    if (verb == "start" && /^tt-wifi-helper@(status|monitor|channel|restore|inject-test)\\.service\$/.test(unit)) { return polkit.Result.YES; }
  }
});
RULE
sed -i '/^polakit_unused/d' "$POLKIT"
chmod 644 "$POLKIT"
if command -v systemctl >/dev/null 2>&1 && [ -d "$ROOT/run/systemd/system" ] && [ -z "$ROOT" ]; then
  systemctl daemon-reload
  systemctl enable --now tt-wifi-watchdog.timer >/dev/null 2>&1 || true
  systemctl start polkit.service >/dev/null 2>&1 || true               # make sure the permission service is up (it normally starts itself when first asked)
  systemctl enable tt-wifi-boot-restore.service >/dev/null 2>&1 || true
fi

if [ "$HAVE_MOD" = 1 ]; then say "Built-in Wi-Fi sniffing (Nexmon): driver for kernel $KVER installed."
else
  warn "No prebuilt Nexmon driver for this kernel ($KVER). The USB Wi-Fi adapter still works. To enable the built-in Wi-Fi: see README 'Other kernels' (pin the kernel, or build once with $NEXDIR/build/build-on-pi.sh)."
  ls "$NEXDIR" 2>/dev/null | grep -v build | sed 's/^/   driver available for: /' >&2 || true
fi
