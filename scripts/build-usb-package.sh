#!/usr/bin/env bash
# Rebuild the USB-stick install package from the committed tree (HEAD) + the read-only update key, so a fresh install gets the newest app AND the online update channel.
#   scripts/build-usb-package.sh [DIR]      (default /mnt/d/pi-usb; also gets install.sh, install-A.sh / install-B.sh and the newest .ptt written by publish-update.sh)
# There are no fixed Pi roles any more: install.sh is the ONE installer script on the stick. install-A.sh and install-B.sh are kept as aliases for old instructions;
# they call install.sh (an address given to them is passed on as the optional --ntp-server).
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-${TT_RELEASE_USB-/mnt/d/pi-usb}}"; KEY="${TT_DEPLOY_KEY:-$HOME/.config/pi-traffic-tester/release-deploy.key}"
[ -d "$OUT" ] || { echo "no such folder: $OUT" >&2; exit 1; }
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
mkdir -p "$W/pi-traffic-tester"
git archive HEAD | tar -x -C "$W/pi-traffic-tester"
if [ -f "$KEY" ]; then install -D -m 600 "$KEY" "$W/pi-traffic-tester/update/deploy_key"; else echo "warning: no read-only update key at $KEY: installs from this package cannot use 'Check online'" >&2; fi
tar czf "$W/pi-traffic-tester.tar.gz" -C "$W" pi-traffic-tester
cp "$W/pi-traffic-tester.tar.gz" "$OUT/pi-traffic-tester.tar.gz"

# --- the scripts on the stick
cat > "$OUT/install.sh" <<'INSTALL'
#!/bin/bash
# Pi Automation Traffic Tester: install on this Pi from the USB stick. Every Pi is the same.   Run: bash /media/$USER/*/install.sh
# A fresh install makes the Pi's Wi-Fi an access point (name = the Pi's name, password "password", http://10.42.0.1:8080); Ethernet stays a DHCP client.
# Optional: --ntp-server <IP>  (take the clock from another Pi), --wifi-mode ap|keep, --port N ... (see install-pi.sh --help)
set -e; D=$(dirname "$(readlink -f "$0")")
tar xzf "$D/pi-traffic-tester.tar.gz" -C "$HOME"
cd "$HOME/pi-traffic-tester" && sudo bash scripts/install-pi.sh "$@"
echo; echo "DONE. This Pi's address: $(hostname -I | awk '{print $1}')   (or join its Wi-Fi, password: password, and open http://10.42.0.1:8080)"
INSTALL
for r in A B; do
cat > "$OUT/install-$r.sh" <<ALIAS
#!/bin/bash
# Old name, kept so old instructions still work: there are no fixed roles any more, this is the same as install.sh. An address given here is used as the clock source (--ntp-server).
D=\$(dirname "\$(readlink -f "\$0")")
if [ \$# -gt 0 ] && echo "\$1" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\$'; then a="\$1"; shift; exec bash "\$D/install.sh" --ntp-server "\$a" "\$@"; fi
exec bash "\$D/install.sh" "\$@"
ALIAS
done
chmod +x "$OUT"/install.sh "$OUT"/install-A.sh "$OUT"/install-B.sh 2>/dev/null || true
echo "wrote $OUT/pi-traffic-tester.tar.gz ($(( $(stat -c %s "$OUT/pi-traffic-tester.tar.gz") / 1024 )) KB, version $(sed -n 's/.*__version__ *= *"\(.*\)".*/\1/p' traffic_tester/__init__.py)) + install.sh, install-A.sh, install-B.sh"
