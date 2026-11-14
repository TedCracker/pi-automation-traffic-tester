#!/bin/bash
# Build the ready-made Raspberry Pi SD-card image from scratch, in one command:
#   scripts/pkg/build-image.sh [--out DIR] [--deliver DIR | --no-deliver] [--keep]
#
#   base      official Raspberry Pi OS Lite 64-bit (current release, from downloads.raspberrypi.com; its sha256 is checked)
#   adds      the app + everything install-pi.sh sets up (it runs INSIDE the image, in an arm64 chroot under qemu-user in a privileged Docker container):
#             venv, apt packages, capabilities, FTDI udev rule, chrony, polkit + update helper, release public key, read-only deploy key for "Check online",
#             Nexmon Wi-Fi sniffing, the Pi's own Wi-Fi `pi-setup` (password "password"), the first-boot "Name this Pi" marker
#   shrinks   resize2fs to the minimum (it expands again on first boot, as the stock image does) and compresses with xz
#   result    <out>/pi-traffic-tester-<version>.img.xz + .sha256   (and, with --deliver, a copy in that folder; default /mnt/d/pi-usb if it exists)
# Work files (download, the unpacked image) live under build-image/ (gitignored). --keep leaves the unpacked .img for the QEMU boot test; otherwise only the .img.xz stays.
# Needs: docker, arm64 binfmt registered once (docker run --privileged --rm tonistiigi/binfmt --install arm64), ~12 GB free, internet, the Nexmon modules
# for the image's kernel (build-image/nexmon from scripts/pkg/fetch-nexmon.sh + build-nexmon-modules.sh; the build says what is missing).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$REPO"
WORK="$REPO/build-image"; OUT=""; DELIVER=""; KEEP=0
[ -d /mnt/d/pi-usb ] && DELIVER=/mnt/d/pi-usb
while [ $# -gt 0 ]; do case "$1" in
  --out) OUT="$2"; shift 2;; --deliver) DELIVER="$2"; shift 2;; --keep) KEEP=1; shift;; --no-deliver) DELIVER=""; shift;;
  -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0;; *) echo "unknown option $1" >&2; exit 2;; esac; done
OUT="${OUT:-$WORK/out}"; mkdir -p "$WORK/dl" "$OUT"
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
VER="$(sed -n 's/.*__version__ *= *"\(.*\)".*/\1/p' traffic_tester/__init__.py)"
NAME="pi-traffic-tester-$VER"

# ---------------------------------------------------------------- 1. base image
LATEST_URL="$(curl -sI https://downloads.raspberrypi.com/raspios_lite_arm64_latest | tr -d '\r' | sed -n 's/^[Ll]ocation: //p')"
[ -n "$LATEST_URL" ] || { echo "Could not find the current Raspberry Pi OS Lite download." >&2; exit 1; }
BASE_XZ="$WORK/dl/$(basename "$LATEST_URL")"
if [ ! -f "$BASE_XZ" ]; then say "Downloading $(basename "$LATEST_URL")"; curl -fL --retry 3 -o "$BASE_XZ.part" "$LATEST_URL"; mv "$BASE_XZ.part" "$BASE_XZ"; fi
[ -f "$BASE_XZ.sha256" ] || curl -fL -o "$BASE_XZ.sha256" "$LATEST_URL.sha256"
( cd "$WORK/dl" && sha256sum -c "$(basename "$BASE_XZ").sha256" ) || { echo "The download does not match its sha256: not using it." >&2; rm -f "${BASE_XZ:?}"; exit 1; }
say "Base image verified: $(basename "$BASE_XZ")"

# ---------------------------------------------------------------- 2. what goes in
STAGE="$WORK/stage"; rm -rf "${STAGE:?}"; mkdir -p "$STAGE/scripts" "$STAGE/docs"
cp -r traffic_tester requirements.txt README.md CHANGELOG.md "$STAGE/"
cp scripts/install-*.sh scripts/net-split.sh "$STAGE/scripts/"; cp docs/SPEC.md docs/INSTALL-PI.md "$STAGE/docs/"
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +
KEY="$HOME/.config/pi-traffic-tester/release-deploy.key"          # read-only key of the releases repo ("Check online"); outside git, never printed
if [ -f "$KEY" ]; then mkdir -p "$STAGE/update"; install -m 600 "$KEY" "$STAGE/update/deploy_key"; else echo "WARNING: no release deploy key: 'Check online' will be greyed out in this image." >&2; fi
NX="$WORK/nexmon"; NXD="$STAGE/nexmon-dir"
if ls "$NX"/out/log-*/*+rpt-*/brcmfmac.ko >/dev/null 2>&1; then
  mkdir -p "$NXD/modules" "$NXD/firmware" "$NXD/build"
  for d in "$NX"/out/log-*/*+rpt-*/brcmfmac.ko; do k="$(basename "$(dirname "$d")")"; mkdir -p "$NXD/modules/$k"; cp "$d" "$NXD/modules/$k/brcmfmac.ko"; done
  cp "$NX/fw/usr/lib/firmware/cypress/cyfmac43455-sdio-standard.bin" "$NXD/firmware/cyfmac43455-sdio-nexmon.bin"
  tar -C "$NX/patched" -czf "$NXD/build/brcmfmac-nexmon-src.tar.gz" .
  cp scripts/pkg/build-on-pi.sh "$NXD/build/"; cp scripts/pkg/nexmon-patches/*.patch "$NXD/build/"
  cp "$NX/dkms.deb" "$NXD/build/brcmfmac-nexmon-dkms_6.12.2_all.deb"; cp "$NX/fw.deb" "$NXD/build/firmware-nexmon_0.2_all.deb"
  say "Nexmon modules for: $(ls "$NXD/modules" | tr '\n' ' ')"
else echo "WARNING: no Nexmon modules in $NX: built-in Wi-Fi sniffing will NOT be in this image." >&2; fi

# ---------------------------------------------------------------- 3. build inside a privileged container (loop mount + arm64 chroot)
say "Building the image (arm64 chroot under qemu-user; ~10-20 minutes)"
rm -f "${WORK:?}/work.img"
docker run --rm --privileged --platform linux/amd64 -v "$REPO/scripts/pkg/build-image-inner.sh:/inner.sh:ro" -v "$WORK:/w" -e BASE_XZ="/w/dl/$(basename "$BASE_XZ")" -e NAME="$NAME" -e VER="$VER" \
  debian:trixie bash /inner.sh
[ -f "$WORK/work.img" ] || { echo "The build produced no image." >&2; exit 1; }

# ---------------------------------------------------------------- 4. compress
say "Compressing (xz)"
IMGFILE="$WORK/$NAME.img"
mv "$WORK/work.img" "$IMGFILE"
xz -T0 -6 -k -f -c "$IMGFILE" > "$OUT/$NAME.img.xz"
( cd "$OUT" && sha256sum "$NAME.img.xz" > "$NAME.img.xz.sha256" )
say "Image: $OUT/$NAME.img.xz ($(du -h "$OUT/$NAME.img.xz" | cut -f1)); unpacked $(stat -c %s "$IMGFILE") bytes"
if [ -n "$DELIVER" ] && [ -d "$DELIVER" ]; then
  find "$DELIVER" -maxdepth 1 -name 'pi-traffic-tester-*.img.xz*' -delete
  cp "$OUT/$NAME.img.xz" "$OUT/$NAME.img.xz.sha256" "$DELIVER/"; say "Copied to $DELIVER"
fi
[ "$KEEP" = 1 ] || rm -f "$IMGFILE"
echo "$OUT/$NAME.img.xz"
