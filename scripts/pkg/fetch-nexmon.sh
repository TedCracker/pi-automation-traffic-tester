#!/bin/bash
# Download Kali's Nexmon packages (checksums pinned) and apply our patch: <workdir>/patched (driver source) + <workdir>/fw (Nexmon firmware).
set -euo pipefail
WD="$1"; HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$WD"; cd "$WD"
DKMS_SHA=e84b68fe35f8c065e0e9513f5a0f542236204c0c9ab6db7a32540988b22262ae
FW_SHA=2e0b25cd01df3df877cbf614aaa111c431fc5f0d49ee708e4b99d727dadf008e
[ -f dkms.deb ] || curl -fsSL -o dkms.deb http://http.kali.org/kali/pool/contrib/b/brcmfmac-nexmon-dkms/brcmfmac-nexmon-dkms_6.12.2_all.deb
[ -f fw.deb ]   || curl -fsSL -o fw.deb   http://http.kali.org/kali/pool/non-free-firmware/f/firmware-nexmon/firmware-nexmon_0.2_all.deb
echo "$DKMS_SHA  dkms.deb" | sha256sum -c - ; echo "$FW_SHA  fw.deb" | sha256sum -c -
rm -rf dkms-x fw patched
mkdir dkms-x fw; dpkg-deb -x dkms.deb dkms-x; dpkg-deb -x fw.deb fw
cp -r dkms-x/usr/src/brcmfmac-nexmon-6.12.2 patched
for pf in "$HERE"/nexmon-patches/*.patch; do patch -d patched -p1 < "$pf"; done
echo "patched source: $WD/patched ; firmware: $WD/fw/usr/lib/firmware/cypress/cyfmac43455-sdio-standard.bin"
