#!/bin/bash
# Host side: download every arm64 .deb the installer needs (plus dependencies) from Debian bookworm + the Raspberry Pi archive,
# and build a flat local apt repository from them.   scripts/pkg/build-offline-apt.sh <outdir>
# Runs in an arm64 Debian bookworm container (needs internet and docker).
set -euo pipefail
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"; HERE="$(cd "$(dirname "$0")" && pwd)"; DIST="${2:-bookworm}"
PKGS="$(cat "$HERE/apt-packages.txt")"
rm -rf "$OUT/apt-$DIST" && mkdir -p "$OUT/apt-$DIST"
docker run --rm --platform linux/arm64 -v "$OUT/apt-$DIST:/out" -e PKGS="$PKGS" -e DIST="$DIST" debian:$DIST bash -c '
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq --no-install-recommends curl gnupg ca-certificates apt-utils >/dev/null
curl -fsSL https://archive.raspberrypi.com/debian/raspberrypi.gpg.key | gpg --dearmor -o /usr/share/keyrings/raspberrypi-archive-keyring.gpg
echo "deb [trusted=yes] http://archive.raspberrypi.com/debian/ $DIST main" > /etc/apt/sources.list.d/raspi.list
apt-get update -qq
echo "wireshark-common wireshark-common/install-setuid boolean true" | debconf-set-selections 2>/dev/null || true
rm -f /var/cache/apt/archives/*.deb
# 1) what apt would install on a bare bookworm; 2) the dependencies it considers already satisfied too (so an older Pi image can be upgraded offline)
# everything the base image could still upgrade (security/point releases): packages are version-locked to each other (e.g. util-linux needs libsmartcols1 = same version), so a Pi image older than today needs the whole family
apt-get dist-upgrade -y --download-only >/dev/null
apt-get install -y --no-install-recommends --download-only $PKGS >/dev/null
cp /var/cache/apt/archives/*.deb /out/
# transitive closure: also fetch every dependency the container already had (an older Pi image may need them upgraded), newest version, until nothing new is added
cd /out
for round in 1 2 3 4 5 6 7 8 9 10; do
  before=$(ls *.deb | wc -l)
  for d in *.deb; do { dpkg-deb -f "$d" Depends; dpkg-deb -f "$d" Pre-Depends; } 2>/dev/null | tr "," "\n" | sed "s/|.*//; s/(.*//; s/:any//; s/ //g"; done | sort -u | grep -v "^$" > /tmp/deps.txt
  for p in $(cat /tmp/deps.txt); do ls "$p"_*.deb >/dev/null 2>&1 || apt-get download "$p" >/dev/null 2>&1 || true; done
  [ "$(ls *.deb | wc -l)" = "$before" ] && break
done
apt-ftparchive packages . > Packages && gzip -kf Packages && apt-ftparchive release . > Release
echo "debs: $(ls /out/*.deb | wc -l)  size: $(du -sh /out | cut -f1)"
'
