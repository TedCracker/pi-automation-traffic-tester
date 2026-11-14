#!/usr/bin/env bash
# Build the Nexmon driver ON the Pi for a kernel that has no prebuilt driver in this package.
# Needs (internet once): sudo apt install linux-headers-$(uname -r) build-essential bc
#   sudo /opt/traffic-tester/nexmon/build/build-on-pi.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KVER="$(uname -r)"; DEST="$(dirname "$HERE")/$KVER"
[ -d "/lib/modules/$KVER/build" ] || { echo "Kernel headers for $KVER are missing. Install them (needs internet once):  sudo apt install linux-headers-$KVER build-essential bc" >&2; exit 2; }
command -v gcc >/dev/null && command -v make >/dev/null || { echo "Install the compiler:  sudo apt install build-essential bc" >&2; exit 2; }
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
tar -xzf "$HERE/brcmfmac-nexmon-src.tar.gz" -C "$W"
make -C "/lib/modules/$KVER/build" M="$W" modules -j"$(nproc)" >"$W/build.log" 2>&1 || { tail -25 "$W/build.log"; echo "BUILD FAILED for kernel $KVER (log above). Use a USB Wi-Fi adapter instead, or boot a kernel from the supported list." >&2; exit 3; }
modinfo "$W/brcmfmac.ko" | grep -q "^vermagic: *$KVER " || { echo "Built module does not match the running kernel." >&2; exit 4; }
install -d -m 755 "$DEST"; install -m 644 "$W/brcmfmac.ko" "$DEST/brcmfmac.ko"
echo "Nexmon driver for $KVER installed: $DEST/brcmfmac.ko"
