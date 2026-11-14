#!/bin/bash
# Assemble the offline install package from: the app (this repo), apt repos (Bookworm + Trixie), wheels (Python 3.11 + 3.13) and the Nexmon modules.
#   scripts/pkg/build-offline-package.sh <workdir> <nexmon-workdir>
# <workdir>/apt-bookworm, apt-trixie (from build-offline-apt.sh)   <nexmon-workdir>/{fw,out,patched,dkms.deb,fw.deb} (from fetch-nexmon.sh + build-nexmon-modules.sh)
# Result: <workdir>/pi-traffic-tester-offline-<version>-<date>.zip  = the ONE file to download (zip opens natively on Windows) containing
#   the tar.gz with everything, INSTALL-OFFLINE.txt/.md and the checksum.
set -euo pipefail
WD="$(cd "$1" && pwd)"; NX="$(cd "$2" && pwd)"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
VER="$(python3 -c "import re;print(re.search(r'__version__\s*=\s*\"([^\"]+)\"',open('$REPO/traffic_tester/__init__.py').read()).group(1))")"
DATE="$(date +%F)"
NAME="pi-traffic-tester-offline-$VER"; FULL="$NAME-$DATE"; P="$WD/$NAME"
rm -rf "$P" "$WD/$FULL".*; mkdir -p "$P/app" "$P/wheels" "$P/nexmon/modules" "$P/nexmon/firmware" "$P/nexmon/build"
# app (working tree minus tests/venv/caches)
cp -r "$REPO/traffic_tester" "$REPO/requirements.txt" "$P/app/"; mkdir -p "$P/app/scripts" "$P/app/docs"
for f in "$REPO"/scripts/install-*.sh "$REPO"/scripts/net-split.sh "$REPO"/scripts/tt-*; do [ -f "$f" ] && cp "$f" "$P/app/scripts/"; done
cp "$REPO/README.md" "$P/app/"; cp "$REPO/docs/SPEC.md" "$REPO/docs/INSTALL-PI.md" "$P/app/docs/"
cp "$REPO/docs/INSTALL-OFFLINE.md" "$P/app/docs/"
# read-only key for the online update channel (Settings -> Software update -> Check online); lives outside git, never printed
[ -f "$HOME/.config/pi-traffic-tester/release-deploy.key" ] && { mkdir -p "$P/app/update"; install -m 600 "$HOME/.config/pi-traffic-tester/release-deploy.key" "$P/app/update/deploy_key"; }
find "$P/app" -name __pycache__ -type d -prune -exec rm -rf {} +
# apt repos, one per OS release
for d in bookworm trixie; do [ -d "$WD/apt-$d" ] && cp -r "$WD/apt-$d" "$P/apt-$d"; done
# python wheels for the Pi (aarch64): Python 3.11 = Bookworm, 3.13 = Trixie; pure-Python wheels are shared
for pv in 3.11 3.13; do
  python3 -m pip download -q -r "$REPO/requirements.txt" -d "$P/wheels" --only-binary=:all: --python-version $pv --implementation cp \
    --platform manylinux2014_aarch64 --platform manylinux_2_17_aarch64 --platform manylinux_2_28_aarch64 --platform manylinux_2_34_aarch64 --platform linux_aarch64 --platform any
done
# nexmon: prebuilt drivers per kernel, firmware, source for rebuilds
for d in "$NX"/out/log-*/*+rpt-*/brcmfmac.ko; do k="$(basename "$(dirname "$d")")"; mkdir -p "$P/nexmon/modules/$k"; cp "$d" "$P/nexmon/modules/$k/brcmfmac.ko"; done
cp "$NX/fw/usr/lib/firmware/cypress/cyfmac43455-sdio-standard.bin" "$P/nexmon/firmware/cyfmac43455-sdio-nexmon.bin"
tar -C "$NX/patched" -czf "$P/nexmon/build/brcmfmac-nexmon-src.tar.gz" .
cp "$REPO/scripts/pkg/build-on-pi.sh" "$P/nexmon/build/"; cp "$REPO/scripts/pkg/nexmon-patches/"*.patch "$P/nexmon/build/"
cp "$NX/dkms.deb" "$P/nexmon/build/brcmfmac-nexmon-dkms_6.12.2_all.deb"; cp "$NX/fw.deb" "$P/nexmon/build/firmware-nexmon_0.2_all.deb"
cat > "$P/nexmon/NOTICE.txt" <<'N'
Built-in Wi-Fi sniffing (Raspberry Pi 5) uses Nexmon (https://github.com/seemoo-lab/nexmon, GPL-2.0 / ISC) as packaged by Kali Linux
(brcmfmac-nexmon-dkms 6.12.2, firmware-nexmon 0.2). The driver source is patched so it compiles on Raspberry Pi OS kernels
(patch in build/). The Wi-Fi firmware is Cypress/Infineon's binary, patched by Nexmon; their licence terms apply.
The original .deb files are included unchanged in build/.
N
cp "$REPO/scripts/pkg/install-offline.sh" "$P/"; cp "$REPO/docs/INSTALL-OFFLINE.md" "$P/INSTALL-OFFLINE.md"
python3 "$REPO/scripts/pkg/md2txt.py" "$REPO/docs/INSTALL-OFFLINE.md" > "$P/INSTALL-OFFLINE.txt"
chmod +x "$P/install-offline.sh" "$P/app/scripts/"* "$P/nexmon/build/"*.sh
( cd "$P" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
# the tar.gz keeps permissions; the zip around it opens natively on Windows
( cd "$WD" && tar -czf "$FULL.tar.gz" --owner=root --group=root "$NAME" )
mkdir -p "$WD/dl"; rm -rf "$WD/dl"/*
cp "$WD/$FULL.tar.gz" "$WD/dl/"; cp "$P/INSTALL-OFFLINE.txt" "$P/INSTALL-OFFLINE.md" "$WD/dl/"
( cd "$WD/dl" && sha256sum "$FULL.tar.gz" > SHA256SUMS.txt )
( cd "$WD/dl" && python3 - "$FULL" <<'PY'
import sys, zipfile
name = sys.argv[1]
with zipfile.ZipFile(f"../{name}.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for f in ("INSTALL-OFFLINE.txt", "INSTALL-OFFLINE.md", "SHA256SUMS.txt"):
        z.write(f, f"{name}/{f}", compress_type=zipfile.ZIP_DEFLATED)
    z.write(f"{name}.tar.gz", f"{name}/{name}.tar.gz", compress_type=zipfile.ZIP_STORED)       # already compressed
PY
)
echo "$WD/$FULL.zip  $(du -h "$WD/$FULL.zip" | cut -f1)"
