#!/usr/bin/env bash
# Install / update / remove the Pi Automation Traffic Tester on Raspberry Pi OS 64-bit (Bookworm or Trixie, arm64).
#
#   sudo ./scripts/install-pi.sh                     # every Pi is the same; a fresh install makes the Pi's Wi-Fi an access point (SSID = the Pi's name, password "password")
#   sudo ./scripts/install-pi.sh --ntp-server 192.168.1.10   # sync this Pi's clock to another Pi / a time server
#   sudo ./scripts/install-pi.sh --uninstall [--purge]
#
# Options: --port N (web port, default 8080)  --ntp-server IP (sync this Pi's clock to that address; otherwise the app decides the time source later)
#          --wifi-mode ap|keep  ap = the Pi's Wi-Fi becomes an access point at http://10.42.0.1:8080 (default for a FRESH install); keep = leave the Wi-Fi as it is
#                               (default when re-running on an existing install, so a Pi you reach over Wi-Fi never drops off the network)
#          --role A|B  accepted and ignored (there are no fixed roles any more)
#          --no-service (install only; do not create/start the systemd service - used in containers)
#          --image   build step of the ready-made SD-card image (scripts/pkg/build-image.sh, runs in a chroot): install everything and ENABLE the units but start nothing, write the
#                    Pi's own Wi-Fi as a setup network `pi-setup` (password "password") and the first-boot "Name this Pi" marker
#          --ap-ssid NAME   Wi-Fi name of the Pi's own network for a fresh install (default: the Pi's name)
#          --nexmon DIR  install the built-in Wi-Fi sniffing driver from DIR (modules/ firmware/) even though this is not a Pi 5 (image build)
#          --offline DIR  install fully offline from the offline package folder DIR (apt/ wheels/ nexmon/): no internet needed
#          --no-wifi-nexmon  skip the built-in Wi-Fi (Nexmon) sniffing step
#          --with-claude  also install Claude Code (Anthropic's signed apt repo): "Ask Claude" + Remote Control. OFF until you log in from the app.
#          --no-net-split  do not set the Ethernet test ports to "never default route" (default: Wi-Fi = internet, Ethernet = test traffic only)
#          --uninstall  --purge (with --uninstall: also delete saved tests/results)
# Safe to run again: it updates the files and restarts the service.
set -euo pipefail

PORT=8080; NTP=""; WIFI_MODE=""; FRESH=1; NO_SERVICE=0; UNINSTALL=0; PURGE=0; OFFLINE=""; NO_NEXMON=0; WITH_CLAUDE=0; NET_SPLIT=1; IMAGE=0; AP_SSID_OPT=""; NEXMON_OPT=""
APP=/opt/traffic-tester; DATA=/var/lib/traffic-tester; CONF=/etc/traffic-tester/config.env
UNIT=/etc/systemd/system/traffic-tester.service; UDEV=/etc/udev/rules.d/99-traffic-tester-ftdi.rules; SYSCTL=/etc/sysctl.d/99-traffic-tester.conf; NMCONF=/etc/NetworkManager/conf.d/99-traffic-tester.conf
CHRONY=/etc/chrony/conf.d/traffic-tester.conf; SVC_USER=traffic-tester
UPD_UNIT=/etc/systemd/system/traffic-tester-update.service; UPD_POLKIT=/etc/polkit-1/rules.d/50-traffic-tester-update.rules; UPD_STATE=/var/lib/traffic-tester-update
UPD_NETOPS_REL=traffic_tester/update/netops.py; NETWATCH_UNIT=/etc/systemd/system/traffic-tester-netwatch.service
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --role) say "There are no fixed roles any more: every Pi is the same. --role is ignored."; shift; [ $# -gt 0 ] && case "$1" in -*) ;; *) shift;; esac;;
    --wifi-mode) WIFI_MODE="${2:-}"; shift; [ $# -gt 0 ] && shift;;
    --port) PORT="${2:-}"; shift 2;;
    --ntp-server) NTP="${2:-}"; shift 2;;
    --no-service) NO_SERVICE=1; shift;;
    --image) IMAGE=1; NO_SERVICE=1; shift;;
    --ap-ssid) AP_SSID_OPT="${2:-}"; shift 2;;
    --nexmon) NEXMON_OPT="${2:-}"; shift 2;;
    --offline) OFFLINE="${2:-}"; shift 2;;
    --no-wifi-nexmon) NO_NEXMON=1; shift;;
    --uninstall) UNINSTALL=1; shift;;
    --purge) PURGE=1; shift;;
    --with-claude) WITH_CLAUDE=1; shift;;
    --no-net-split) NET_SPLIT=0; shift;;
    -h|--help) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "Unknown option: $1 (try --help)";;
  esac
done
case "$WIFI_MODE" in ""|ap|keep) ;; *) die "--wifi-mode must be ap or keep";; esac
case "$PORT" in ''|*[!0-9]*) die "--port must be a number";; esac
[ "$(id -u)" -eq 0 ] || die "Run with sudo:  sudo $0 $*"
[ "$IMAGE" -eq 1 ] && { FRESH=1; WIFI_MODE=ap; [ -n "$AP_SSID_OPT" ] || AP_SSID_OPT=pi-setup; }
# A FRESH install (no config yet) makes the Wi-Fi an access point; re-running on an existing install keeps its network as it is (a Pi reached over Wi-Fi must not drop off).
[ -f "$CONF" ] && FRESH=0
if [ -z "$WIFI_MODE" ]; then
  if [ "$FRESH" -eq 1 ]; then WIFI_MODE="ap"; else WIFI_MODE="keep"; fi
fi
# the Wi-Fi is the access point, so Ethernet is the internet + test link: do not mark it "never default route" (that split is for installs that use Wi-Fi as the internet link)
[ "$WIFI_MODE" = ap ] && NET_SPLIT=0

HAVE_SYSTEMD=0
[ -d /run/systemd/system ] && HAVE_SYSTEMD=1
[ "$NO_SERVICE" -eq 1 ] && HAVE_SYSTEMD=0
# units are written when systemd runs here OR when building a card image (units are enabled with `systemctl enable`, which needs no running systemd)
WRITE_UNITS=0
{ [ "$HAVE_SYSTEMD" -eq 1 ] || [ "$IMAGE" -eq 1 ]; } && WRITE_UNITS=1

# ------------------------------------------------------------------ uninstall
if [ "$UNINSTALL" -eq 1 ]; then
  say "Removing Pi Automation Traffic Tester"
  if [ -e "$UNIT" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now traffic-tester.service 2>/dev/null || true
  fi
  [ -x "$APP/install-wifi-nexmon.sh" ] && "$APP/install-wifi-nexmon.sh" --uninstall || true
  [ -x "$APP/install-claude.sh" ] && "$APP/install-claude.sh" --uninstall || true     # forget any Claude sign-in, remove Remote Control units, undo the network split
  if [ -e "$NETWATCH_UNIT" ] && command -v systemctl >/dev/null 2>&1; then systemctl disable --now traffic-tester-netwatch.service 2>/dev/null || true; fi
  if command -v nmcli >/dev/null 2>&1; then          # the network profiles this app created (all are named tt-*: tt-ap, tt-fallback-ap, tt-wifi-N, tt-eth-*): other profiles are never touched
    nmcli -t -f NAME connection show 2>/dev/null | grep -E '^tt-[a-z0-9][a-z0-9._-]*$' | while IFS= read -r c; do nmcli connection delete id "$c" >/dev/null 2>&1 || true; done
  fi
  rm -f /etc/NetworkManager/system-connections/tt-ap.nmconnection
  rm -f "$UNIT" "$UDEV" "$CHRONY" "$SYSCTL" "$NMCONF" "$UPD_UNIT" "$UPD_POLKIT" "$NETWATCH_UNIT"
  rm -rf "$APP" /etc/traffic-tester /var/lib/traffic-tester-update
  command -v systemctl >/dev/null 2>&1 && [ "$HAVE_SYSTEMD" -eq 1 ] && systemctl daemon-reload || true
  command -v udevadm >/dev/null 2>&1 && udevadm control --reload-rules 2>/dev/null || true
  command -v systemctl >/dev/null 2>&1 && [ "$HAVE_SYSTEMD" -eq 1 ] && systemctl restart chrony 2>/dev/null || true
  if [ "$PURGE" -eq 1 ]; then rm -rf "$DATA"; userdel "$SVC_USER" 2>/dev/null || true; say "Saved tests and results deleted."
  else say "Saved tests and results kept in $DATA (use --purge to delete them)."; fi
  say "Done."; exit 0
fi

# ------------------------------------------------------------------ checks
ARCH="$(uname -m)"
case "$ARCH" in aarch64|arm64) ;; *) warn "This is $ARCH, not 64-bit (aarch64). Raspberry Pi OS 64-bit is required. Continuing anyway.";; esac
[ -r /etc/os-release ] && . /etc/os-release || true
case "${VERSION_CODENAME:-}" in
  bookworm|trixie) ;;
  bullseye|buster|stretch) die "Raspberry Pi 4/5 need Raspberry Pi OS Bookworm or newer (64-bit). This is '${PRETTY_NAME:-unknown}'.";;
  *) warn "Built for Raspberry Pi OS Bookworm or newer (64-bit); this is '${PRETTY_NAME:-unknown}'.";;
esac
# Target hardware is the Raspberry Pi 5: warn (never block) on anything else.
MODEL="$(tr -d '\0' < "${TT_MODEL_FILE:-/proc/device-tree/model}" 2>/dev/null || true)"
case "$MODEL" in
  *"Raspberry Pi 5"*) IS_PI5=1; say "Detected: $MODEL";;
  *"Raspberry Pi"*)   IS_PI5=0; warn "This is a '$MODEL'. Pi Automation Traffic Tester is built and timed for the Raspberry Pi 5; it should still run here (serial ports and timing figures differ). Continuing.";;
  *)                  IS_PI5=0; warn "This does not look like a Raspberry Pi (no model found). Pi Automation Traffic Tester is built for the Raspberry Pi 5. Continuing.";;
esac
[ -f "$SRC/traffic_tester/__main__.py" ] || die "Cannot find the app next to this script ($SRC). Run it from the copied traffic-tester folder."

# ------------------------------------------------------------------ packages
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(); PIP_OPTS=(); OFFLINE_REPO=""
if [ -n "$OFFLINE" ]; then
  OFFLINE="$(cd "$OFFLINE" && pwd)" || die "--offline: folder not found"
  case "$ARCH" in aarch64|arm64) ;; *) die "The offline package contains 64-bit (aarch64) software only. This system is $ARCH (32-bit Raspberry Pi OS?). Re-flash the SD card with Raspberry Pi OS (64-bit).";; esac
  OFFLINE_APT="$OFFLINE/apt-${VERSION_CODENAME:-bookworm}"; [ -d "$OFFLINE_APT" ] || OFFLINE_APT="$OFFLINE/apt"
  [ -d "$OFFLINE_APT" ] && [ -d "$OFFLINE/wheels" ] || die "--offline: no packages for '${VERSION_CODENAME:-unknown}' in $OFFLINE (this package has: $(cd "$OFFLINE" && ls -d apt-* 2>/dev/null | sed 's/apt-//' | tr '\n' ' '))."
  say "Installing system packages from the offline package (no internet needed)"
  OFFLINE_REPO=/var/lib/traffic-tester-offline-repo
  rm -rf "$OFFLINE_REPO"; mkdir -p "$OFFLINE_REPO"; cp -r "$OFFLINE_APT/." "$OFFLINE_REPO/"; chmod -R a+rX "$OFFLINE_REPO"
  echo "deb [trusted=yes] file:$OFFLINE_REPO ./" > /tmp/tt-offline.list
  APT_OPTS=(-o Dir::Etc::SourceList=/tmp/tt-offline.list -o Dir::Etc::SourceParts=/dev/null -o APT::Get::List-Cleanup=0 -o Acquire::Languages=none)
  PIP_OPTS=(--no-index --find-links "$OFFLINE/wheels")
else
  say "Installing system packages (needs internet once)"
fi
apt-get "${APT_OPTS[@]}" update -qq
# Wireshark's capture engine (dumpcap) may be used by non-root users in group "wireshark" (answer the debconf question before installing)
command -v debconf-set-selections >/dev/null 2>&1 && echo "wireshark-common wireshark-common/install-setuid boolean true" | debconf-set-selections || true
apt-get "${APT_OPTS[@]}" install -y -qq --no-install-recommends python3 python3-venv python3-pip chrony iproute2 socat ca-certificates tshark tcpdump iw ethtool kmod git >/dev/null
# built-in Wi-Fi sniffing: polkit lets the service start the root Wi-Fi helper (separate step so one failure cannot hide the other); aircrack-ng is only for the optional injection test
apt-get "${APT_OPTS[@]}" install -y -qq --no-install-recommends polkitd >/dev/null 2>&1 || warn "polkitd could not be installed: built-in Wi-Fi sniffing will not work (a USB Wi-Fi adapter does)."
apt-get "${APT_OPTS[@]}" install -y -qq --no-install-recommends aircrack-ng >/dev/null 2>&1 || warn "aircrack-ng could not be installed: only the optional packet-injection test is missing."
# the access point needs NetworkManager and its DHCP helper (dnsmasq-base); mDNS gives the Pi a name (<hostname>.local). --no-upgrade: never restart a running NetworkManager (and drop the Wi-Fi) mid-install.
apt-get "${APT_OPTS[@]}" install -y -qq --no-install-recommends --no-upgrade network-manager dnsmasq-base avahi-daemon >/dev/null 2>&1 || warn "NetworkManager / dnsmasq-base / avahi-daemon could not be installed: the Wi-Fi access point and the <name>.local address may not work."
[ -n "$OFFLINE_REPO" ] && rm -f /tmp/tt-offline.list
python3 - <<'PY' || die "Python 3.10 or newer is required."
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

# ------------------------------------------------------------------ files
say "Copying the app to $APP"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$SVC_USER"
getent group dialout >/dev/null && usermod -aG dialout "$SVC_USER"
getent group wireshark >/dev/null && usermod -aG wireshark "$SVC_USER"            # sniffer: dumpcap is executable for this group
if command -v dumpcap >/dev/null 2>&1; then
  setcap cap_net_raw,cap_net_admin=eip "$(command -v dumpcap)" 2>/dev/null || true   # capture without running anything as root
fi
mkdir -p "$APP" "$DATA" /etc/traffic-tester
mkdir -p "$APP/app"; find "$APP/app" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +     # keep .git: the history of approved changes survives updates
cp -r "$SRC/traffic_tester" "$SRC/requirements.txt" "$APP/app/"
find "$APP/app" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$SRC/scripts/install-pi.sh" "$APP/install-pi.sh" 2>/dev/null || true
cp "$SRC/scripts/install-wifi-nexmon.sh" "$APP/install-wifi-nexmon.sh" 2>/dev/null || true
cp "$SRC/scripts/install-claude.sh" "$APP/install-claude.sh" 2>/dev/null || true
chown -R "$SVC_USER":"$SVC_USER" "$DATA"
chmod 750 "$DATA"

say "Setting up the Python environment (this takes a minute on a Pi)"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
[ -n "$OFFLINE" ] || "$APP/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$APP/venv/bin/pip" install -q "${PIP_OPTS[@]}" -r "$APP/app/requirements.txt"
"$APP/venv/bin/python" - <<PY
import sys; sys.path.insert(0, "$APP/app")
import traffic_tester, fastapi, uvicorn, serial, websockets  # fail loudly (websockets: without it the live view never connects) if anything did not install
print("   app", traffic_tester.__version__, "· python", sys.version.split()[0])
PY

OLD_ROLE="$({ sed -n 's/^TT_ROLE=//p' "$CONF" 2>/dev/null || true; } | head -1)"           # no longer written, but an existing one is kept (the installed app may still read it)
cat > "$CONF" <<CFG
# Pi Automation Traffic Tester settings (edit, then: sudo systemctl restart traffic-tester)
TT_PORT=$PORT
TT_HOST=0.0.0.0
TT_DATA_DIR=$DATA
CFG
case "$OLD_ROLE" in A|B) echo "TT_ROLE=$OLD_ROLE" >> "$CONF";; esac
chmod 644 "$CONF"

# git record of the app, network split (Wi-Fi = internet), and (with --with-claude) Claude Code + Remote Control plumbing
"$SRC/scripts/install-claude.sh" --apply --app "$APP" --data "$DATA" --conf "$CONF" --user "$SVC_USER" $([ "$WITH_CLAUDE" -eq 1 ] && echo --with-claude) $([ "$NET_SPLIT" -eq 0 ] && echo --no-net-split)

# Bigger kernel socket buffers for the traffic generator (the default cap, ~200 KB, can drop packets on a brief stall).
if [ "$HAVE_SYSTEMD" -eq 1 ] || [ -d /etc/sysctl.d ]; then
  mkdir -p /etc/sysctl.d
  printf 'net.core.rmem_max=16777216\nnet.core.wmem_max=16777216\n' > "$SYSCTL"
  sysctl -q -p "$SYSCTL" 2>/dev/null || true
fi

# The inline-tap bridge must never be managed by NetworkManager (it has no address on purpose).
if [ -d /etc/NetworkManager/conf.d ]; then
  printf '[keyfile]\nunmanaged-devices=interface-name:ttbr0\n' > "$NMCONF"
  [ "$HAVE_SYSTEMD" -eq 1 ] && systemctl reload NetworkManager 2>/dev/null || true
fi

# USB-serial adapters (FTDI) hold data up to 16 ms by default: set it to 1 ms every time one is plugged in.
mkdir -p "$(dirname "$UDEV")"
cat > "$UDEV" <<'RULE'
ACTION=="add", SUBSYSTEM=="usb-serial", DRIVER=="ftdi_sio", ATTR{latency_timer}="1"
RULE
if command -v udevadm >/dev/null 2>&1; then udevadm control --reload-rules 2>/dev/null || true; udevadm trigger --subsystem-match=usb-serial 2>/dev/null || true; fi

# ------------------------------------------------------------------ clock (one-way timing)
if [ -d /etc/chrony ]; then
  mkdir -p /etc/chrony/conf.d
  grep -q '^confdir /etc/chrony/conf.d' /etc/chrony/chrony.conf 2>/dev/null || echo 'confdir /etc/chrony/conf.d' >> /etc/chrony/chrony.conf
  if [ -f "$CHRONY" ] && grep -q '^# mode:' "$CHRONY" && [ -z "$NTP" ]; then
    say "Clock: keeping the time source chosen in the app ($(sed -n 's/^# mode: //p' "$CHRONY"))"
  else
    {
      echo "# Written by Pi Automation Traffic Tester install-pi.sh. Which Pi is the time source is decided in the app (Settings), not here."
      echo "allow 10.0.0.0/8"; echo "allow 172.16.0.0/12"; echo "allow 192.168.0.0/16"
      [ -n "$NTP" ] && echo "server $NTP iburst prefer minpoll 0 maxpoll 4"
    } > "$CHRONY"
    [ "$HAVE_SYSTEMD" -eq 1 ] && systemctl restart chrony 2>/dev/null || true
  fi
fi

# ------------------------------------------------------------------ built-in Wi-Fi sniffing (Nexmon) - optional, never replaces the stock Wi-Fi driver
NEXMON_SRC=""
[ -n "$NEXMON_OPT" ] && [ -d "$NEXMON_OPT" ] && { NEXMON_SRC="$NEXMON_OPT"; IS_PI5=1; }       # card image: the target is a Pi 5 even though this chroot is not
[ -z "$NEXMON_SRC" ] && [ -n "$OFFLINE" ] && [ -d "$OFFLINE/nexmon" ] && NEXMON_SRC="$OFFLINE/nexmon"
[ -z "$NEXMON_SRC" ] && [ -d "$SRC/nexmon" ] && NEXMON_SRC="$SRC/nexmon"
if [ "$NO_NEXMON" -eq 0 ] && [ -n "$NEXMON_SRC" ] && [ "$IS_PI5" -eq 1 ]; then
  say "Installing built-in Wi-Fi sniffing (Nexmon)"
  KV=""; [ "$IMAGE" -eq 1 ] && KV="$(ls /lib/modules 2>/dev/null | grep -- '-rpi-2712$' | sort -V | tail -1)"       # in a chroot uname -r is the build host's kernel, not the card's
  SVC_USER="$SVC_USER" TT_KVER="${KV:-$(uname -r)}" "$SRC/scripts/install-wifi-nexmon.sh" "$NEXMON_SRC" || warn "Built-in Wi-Fi sniffing could not be installed (the rest works; a USB Wi-Fi adapter is the alternative)."
elif [ "$NO_NEXMON" -eq 0 ] && [ -n "$NEXMON_SRC" ]; then
  warn "Skipping built-in Wi-Fi sniffing: this is not a Raspberry Pi 5."
fi


# ------------------------------------------------------------------ software updates (Settings -> Software update)
# The web app is NOT root. It only asks systemd to start ONE fixed root unit (polkit rule below); that unit runs a root-owned copy of the update helper,
# which re-checks the signature against its own root-owned copy of the public key before it touches anything.
say "Software update: installing the update helper"
UPD_HELP="$APP/update-helper"
rm -rf "$UPD_HELP"; mkdir -p "$UPD_HELP/traffic_tester/update"
cp "$SRC/traffic_tester/__init__.py" "$UPD_HELP/traffic_tester/__init__.py"
cp "$SRC"/traffic_tester/update/*.py "$UPD_HELP/traffic_tester/update/"
chown -R root:root "$UPD_HELP"; chmod -R go-w "$UPD_HELP"
mkdir -p "$UPD_STATE"; chown root:root "$UPD_STATE"; chmod 755 "$UPD_STATE"
mkdir -p "$DATA/update/inbox"; chown -R "$SVC_USER":"$SVC_USER" "$DATA/update"
python3 - "$SRC" > /etc/traffic-tester/update-pubkey.hex <<'PYK'
import sys; sys.path.insert(0, sys.argv[1]); from traffic_tester.update.pubkey import PUBLIC_KEY_HEX; print(PUBLIC_KEY_HEX)
PYK
chmod 644 /etc/traffic-tester/update-pubkey.hex
cat > /etc/traffic-tester/update.conf <<UCONF
# Read by the root update helper only (the web app cannot change it)
APP_DIR=$APP/app
DATA_DIR=$DATA
STATE_DIR=$UPD_STATE
WORK_DIR=$UPD_STATE/work
SERVICE=traffic-tester.service
PORT=$PORT
VENV_PYTHON=$APP/venv/bin/python
SVC_USER=$SVC_USER
UCONF
chmod 644 /etc/traffic-tester/update.conf
UPD_KEY_SRC=""
for k in "$SRC/update/deploy_key" "${TT_UPDATE_DEPLOY_KEY:-/nonexistent}"; do [ -f "$k" ] && UPD_KEY_SRC="$k" && break; done
if [ -n "$UPD_KEY_SRC" ]; then
  install -d -m 755 /etc/traffic-tester/update
  install -m 600 -o "$SVC_USER" -g "$SVC_USER" "$UPD_KEY_SRC" /etc/traffic-tester/update/deploy_key
  say "   online update key installed (read-only access to the update channel)"
else
  warn "No online-update key in this package: 'Check for updates' stays greyed out; 'Upload a file' works."
fi
grep -q '^TT_UPDATE_MODE=' "$CONF" || printf 'TT_UPDATE_MODE=system\nTT_UPDATE_STATE_DIR=%s\n' "$UPD_STATE" >> "$CONF"
if [ "$WRITE_UNITS" -eq 1 ] || [ -d /etc/systemd/system ]; then
  cat > "$UPD_UNIT" <<UUNIT
[Unit]
Description=Pi Automation Traffic Tester software update (verify, swap, restart, health check, rollback)
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I $UPD_HELP/traffic_tester/update/helper.py run
TimeoutStartSec=900
UUNIT
  mkdir -p "$(dirname "$UPD_POLKIT")"
  cat > "$UPD_POLKIT" <<URULE
// Pi Automation Traffic Tester: the web app's service user may start ONLY the update helper unit, nothing else.
polkit.addRule(function(action, subject) {
  if (action.id == "org.freedesktop.systemd1.manage-units" && subject.user == "$SVC_USER") {
    var unit = action.lookup("unit"), verb = action.lookup("verb");
    if (verb == "start" && unit == "traffic-tester-update.service") { return polkit.Result.YES; }
  }
});
URULE
  chmod 644 "$UPD_POLKIT"
fi

# ------------------------------------------------------------------ network: Wi-Fi = access point (fresh installs), Ethernet = DHCP client for internet + testing
# Settings -> Network changes all of this later (with an automatic revert if a change cuts you off). The privileged part is netops.py in the root-owned helper copy above.
NETOPS="$UPD_HELP/$UPD_NETOPS_REL"
AP_SSID=""; AP_NOW=0
ssh_over_wifi() {            # is a remote shell (this one) running over Wi-Fi? then the access point must not start under it
  command -v ss >/dev/null 2>&1 || return 1
  for peer in $(ss -Htn state established '( sport = :22 )' 2>/dev/null | awk '{print $4}' | sed 's/:[0-9]*$//; s/^\[//; s/\]$//' | sort -u); do
    case "$(ip route get "$peer" 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)" in wl*) return 0;; esac
  done
  return 1
}
if [ "$WIFI_MODE" = ap ]; then
  command -v rfkill >/dev/null 2>&1 && rfkill unblock wifi 2>/dev/null || true
  if [ "$HAVE_SYSTEMD" -eq 1 ] && ! ssh_over_wifi; then AP_NOW=1; fi
  AP_OUT="$(mktemp)"
  APX=(); [ -n "$AP_SSID_OPT" ] && APX+=(--ssid "$AP_SSID_OPT"); [ "$IMAGE" -eq 1 ] && APX+=(--keyfile-only)
  if python3 -I "$NETOPS" setup-default-ap $([ "$AP_NOW" -eq 1 ] && echo --activate) "${APX[@]}" >"$AP_OUT" 2>&1; then
    AP_SSID="$(python3 -I "$NETOPS" ap-ssid 2>/dev/null || true)"
    say "Wi-Fi: the Pi's own network '${AP_SSID:-$(hostname)}' (password: password) at http://10.42.0.1:8080. Ethernet stays a DHCP client for internet and testing."
    [ "$AP_NOW" -eq 1 ] || say "   (it starts at the next reboot so this remote session is not cut off; or run: sudo nmcli connection up tt-ap)"
  else
    warn "The Wi-Fi access point could not be set up: $(tail -1 "$AP_OUT"). The Wi-Fi was left as it was; set it up later in Settings -> Network."
    WIFI_MODE=keep
  fi
  rm -f "$AP_OUT"
  [ -x /usr/sbin/dnsmasq ] || warn "dnsmasq (package dnsmasq-base) is missing: the access point cannot hand out addresses until it is installed."
  command -v iw >/dev/null 2>&1 && iw reg get 2>/dev/null | grep -q '^country 00' && warn "No Wi-Fi country is set. Set it once so the Wi-Fi may transmit: sudo raspi-config nonint do_wifi_country <US|DE|...>"
else
  say "Wi-Fi: left as it is (existing install, or --wifi-mode keep). Settings -> Network can make it an access point."
fi
if [ "$WIFI_MODE" = ap ] && { [ "$WRITE_UNITS" -eq 1 ] || [ -d /etc/systemd/system ]; }; then
  # the dead-man switch that also works while the app is down: no network for 3 minutes -> the Pi starts its own Wi-Fi so it is never unreachable
  cat > "$NETWATCH_UNIT" <<NWUNIT
[Unit]
Description=Pi Automation Traffic Tester - network watch (own Wi-Fi when the Pi has no network for 3 minutes)
After=NetworkManager.service

[Service]
ExecStart=/usr/bin/python3 -I $NETOPS watch
Restart=always
RestartSec=5
Nice=10

[Install]
WantedBy=multi-user.target
NWUNIT
  if [ "$HAVE_SYSTEMD" -eq 1 ]; then systemctl daemon-reload; systemctl enable --now traffic-tester-netwatch.service >/dev/null 2>&1 || warn "The network watch service did not start."
  elif [ "$IMAGE" -eq 1 ]; then systemctl enable traffic-tester-netwatch.service >/dev/null 2>&1 || warn "The network watch service could not be enabled."; fi
fi

# ------------------------------------------------------------------ service
if [ "$WRITE_UNITS" -eq 1 ]; then
  say "Creating the background service"
  cat > "$UNIT" <<UNITF
[Unit]
Description=Pi Automation Traffic Tester
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_USER
SupplementaryGroups=dialout
EnvironmentFile=$CONF
WorkingDirectory=$APP/app
RuntimeDirectory=traffic-tester
RuntimeDirectoryPreserve=yes
ExecStart=$APP/venv/bin/python -m traffic_tester
Restart=on-failure
RestartSec=3
# raw network access for GOOSE, bad-link simulation (tc/netem), port 102, realtime priority
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_SYS_NICE
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_SYS_NICE
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNITF
  [ "$HAVE_SYSTEMD" -eq 1 ] && systemctl daemon-reload
  systemctl enable traffic-tester.service >/dev/null 2>&1 || warn "The service could not be enabled."
  if [ "$HAVE_SYSTEMD" -eq 1 ]; then
    systemctl restart traffic-tester.service
    sleep 2
    if systemctl is-active --quiet traffic-tester.service; then say "Service is running."
    else warn "The service did not start. See: journalctl -u traffic-tester -n 50"; fi
  else say "Service created and enabled; it starts at the next boot."; fi
else
  warn "No systemd here (or --no-service): the service was not created. Start by hand:"
  echo "   sudo -u $SVC_USER $APP/venv/bin/python -m traffic_tester --port $PORT --data-dir $DATA"
fi

# ------------------------------------------------------------------ card image: first boot
if [ "$IMAGE" -eq 1 ]; then
  say "Card image: first-boot setup (name this Pi) + Imager Wi-Fi hand-over"
  : > "$DATA/first-boot"; chown "$SVC_USER":"$SVC_USER" "$DATA/first-boot"      # while this file exists and the Pi has no name, the page shows only "Name this Pi"
  mkdir -p /etc/NetworkManager/dnsmasq-shared.d                                  # the setup network answers every name with the Pi, so a phone opens the page by itself (removed when the Pi is named)
  python3 -I - "$NETOPS" > /etc/NetworkManager/dnsmasq-shared.d/tt-captive.conf <<'PYC'
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(sys.argv[1]))))
from traffic_tester.update import netops; print(netops.CAPTIVE_TEXT, end="")
PYC
  cat > /etc/systemd/system/traffic-tester-firstboot.service <<FBUNIT
[Unit]
Description=Pi Automation Traffic Tester - first boot (radio on; use the Wi-Fi from Raspberry Pi Imager if there is one)
After=cloud-init-network.service cloud-config.service NetworkManager.service
ConditionPathExists=!/var/lib/traffic-tester-update/firstboot-done

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I $NETOPS firstboot
ExecStartPost=/usr/bin/touch /var/lib/traffic-tester-update/firstboot-done
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
FBUNIT
  systemctl enable traffic-tester-firstboot.service >/dev/null 2>&1 || warn "The first-boot service could not be enabled."
  # systemd applies "presets" at the very first boot and enables every unit no preset mentions - on this base that switched SSH on. SSH stays OFF unless the person turned it on in Imager
  # (Imager / cloud-init enable it explicitly later, which this does not prevent).
  mkdir -p /etc/systemd/system-preset
  printf '# Pi Automation Traffic Tester card image: no SSH unless it was switched on on purpose\ndisable ssh.service\ndisable sshd.service\ndisable ssh.socket\n' > /etc/systemd/system-preset/00-traffic-tester-ssh-off.preset
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
if [ "$IMAGE" -eq 1 ]; then say "Installed (card image). The Pi is named on a phone at first boot."; else say "Installed. Pi name: $(hostname)"; fi
if [ "$WIFI_MODE" = ap ]; then
  echo "   Wi-Fi: join the network '${AP_SSID:-$(hostname)}' (password: password) with a phone or laptop, then open   http://10.42.0.1:8080   (or http://$(hostname).local:$PORT)"
  echo "   Ethernet: plug it into your network for internet and software updates, or into the equipment you test (it is a DHCP client)."
else
  echo "   Open this in a browser on the same network:   http://${IP:-<this-pi-ip>}:$PORT"
fi
[ "${IS_PI5:-0}" = 1 ] && echo "   Power: use the official 5 V / 5 A USB-C supply (27 W). On a 3 A supply the Pi 5 caps its USB ports and USB-serial adapters can misbehave."
echo "   Sniffer: capture runs as the service user with CAP_NET_RAW/CAP_NET_ADMIN (never root). For the inline tap open this page over Wi-Fi."
echo "   Serial ports (Pi 5): USB adapter = /dev/ttyUSB*; GPIO 14/15 = /dev/ttyAMA0 (add 'dtparam=uart0=on' to /boot/firmware/config.txt and reboot); debug connector = /dev/ttyAMA10."
[ -n "$OFFLINE_REPO" ] && rm -rf "$OFFLINE_REPO"
echo "   Remove later with:   sudo $APP/install-pi.sh --uninstall"
