#!/bin/bash
# Runs INSIDE the arm64 builder container: build the patched Nexmon brcmfmac for ONE Raspberry Pi OS kernel version.
#   nexmon-inner-build.sh <kver e.g. 6.12.109+rpt-rpi-2712> <src dir> <out dir>
# Installs that kernel's headers from archive.raspberrypi.com (unless already present), builds, checks vermagic.
set -uo pipefail
KVER="$1"; SRC="$2"; OUT="$3"
BASE="${KVER%%+rpt*}"                                   # 6.12.109
FLAVOUR="${KVER##*+rpt-}"                               # rpi-2712
mkdir -p "$OUT"
trap 'chmod -R a+rwX "$OUT" 2>/dev/null' EXIT        # the container runs as root: leave the results deletable by the host user
if [ ! -d "/lib/modules/$KVER/build" ]; then
  POOL=http://archive.raspberrypi.com/debian/pool/main/l/linux
  LIST=$(curl -fsS "$POOL/") || { echo "BUILD-FAIL no pool listing"; exit 3; }
  H=$(grep -oE "linux-headers-${BASE//./\\.}\+rpt-${FLAVOUR}_[^\"]*arm64\.deb" <<<"$LIST" | sort -u | grep -E 'bookworm' | head -1)
  [ -n "$H" ] || H=$(grep -oE "linux-headers-${BASE//./\\.}\+rpt-${FLAVOUR}_[^\"]*arm64\.deb" <<<"$LIST" | sort -u | head -1)
  [ -n "$H" ] || { echo "BUILD-FAIL no headers deb for $KVER"; exit 4; }
  V=${H#linux-headers-${BASE}+rpt-${FLAVOUR}_}; V=${V%_arm64.deb}
  C="linux-headers-${BASE}+rpt-common-rpi_${V}_all.deb"; K="linux-kbuild-${BASE}+rpt_${V}_arm64.deb"
  mkdir -p /tmp/kdebs && cd /tmp/kdebs
  for f in "$K" "$C" "$H"; do curl -fsS -O "$POOL/$f" || { echo "BUILD-FAIL download $f"; exit 5; }; done
  dpkg -i --force-depends "$K" "$C" "$H" >/dev/null 2>&1 || true        # only unpacking matters; configuration scripts (and their dependencies) are not needed to compile a module
  [ -d "/lib/modules/$KVER/build" ] || { echo "BUILD-FAIL dpkg $KVER (headers did not unpack)"; dpkg -i "$K" "$C" "$H" 2>&1 | tail -5; exit 6; }
fi
W=$(mktemp -d); cp -r "$SRC"/. "$W"/
cd "$W"
if make -j"$(nproc)" -C "/lib/modules/$KVER/build" M="$W" modules > "$OUT/build.log" 2>&1; then
  VM=$(modinfo "$W/brcmfmac.ko" | awk -F': *' '/^vermagic/{print $2}')
  case "$VM" in "$KVER "*) mkdir -p "$OUT/$KVER"; cp "$W/brcmfmac.ko" "$OUT/$KVER/brcmfmac.ko"; echo "BUILD-OK $KVER vermagic=[$VM] sha256=$(sha256sum "$W/brcmfmac.ko" | cut -d' ' -f1)";;
                *) echo "BUILD-FAIL $KVER vermagic mismatch [$VM]";; esac
else
  echo "BUILD-FAIL $KVER compile error:"; grep -E "error" "$OUT/build.log" | head -8
fi
