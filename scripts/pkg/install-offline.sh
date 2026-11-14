#!/usr/bin/env bash
# Pi Automation Traffic Tester - install on a Raspberry Pi 4 / 5 (64-bit Raspberry Pi OS Bookworm or Trixie) with NO internet. Everything (system packages, Python libraries, the built-in
# Wi-Fi sniffing driver) is inside this folder.
#   sudo ./install-offline.sh                       (every Pi is the same; a fresh install makes the Pi's Wi-Fi an access point: SSID = the Pi's name, password "password", http://10.42.0.1:8080)
#   sudo ./install-offline.sh --ntp-server <another Pi's IP>     (optional: take the clock from that address)
# --role A|B is still accepted but ignored. More options: see  ./app/scripts/install-pi.sh --help   (--wifi-mode ap|keep, --port N, --no-wifi-nexmon, --no-service ...)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "Run with sudo:  sudo ./install-offline.sh"
[ -f "$HERE/SHA256SUMS" ] || die "SHA256SUMS is missing - the folder is incomplete."
echo "==> Checking the files (a copy can be damaged by a transfer)"
( cd "$HERE" && sha256sum -c --quiet SHA256SUMS ) || die "Some files are damaged or missing. Copy the package to the Pi again."
case "$(uname -m)" in aarch64|arm64) ;; *) die "This package is for the 64-bit Raspberry Pi OS (aarch64). This is $(uname -m).";; esac
. /etc/os-release 2>/dev/null || true
case "${VERSION_CODENAME:-}" in bookworm|trixie) ;; *) die "This offline package is for Raspberry Pi OS 64-bit BOOKWORM or TRIXIE (Pi 4 / Pi 5). This is '${PRETTY_NAME:-unknown}'. Re-flash the SD card with Raspberry Pi OS (64-bit).";; esac
{ find "$HERE" -name "*.sh" -exec chmod +x {} + 2>/dev/null; chmod +x "$HERE"/app/scripts/tt-* "$HERE"/nexmon/build/*.sh 2>/dev/null; } || true     # in case the copy lost the execute bits
exec "$HERE/app/scripts/install-pi.sh" --offline "$HERE" "$@"
