#!/usr/bin/env bash
# Claude Code part of the Pi Automation Traffic Tester installer. Called by install-pi.sh (you normally never run it by hand).
#   install-claude.sh --apply [--with-claude] [--no-net-split]   after the app files and config.env are in place
#   install-claude.sh --uninstall                                forget the Claude sign-in, remove the Remote Control units, undo the network split
# Always (cheap, no internet): record the app in git (so any change can be shown and undone) and make the Ethernet test ports "never default route".
# With --with-claude: install Claude Code from Anthropic's signed apt repository (stays OFF until you log in from the app) and the
# Remote Control plumbing. The web app never gets root: it writes a flag into state.json; a root-owned systemd path unit acts on it.
set -euo pipefail
APP=/opt/traffic-tester; DATA=/var/lib/traffic-tester; CONF=/etc/traffic-tester/config.env; SVC_USER=traffic-tester
MODE=""; WITH_CLAUDE=0; NET_SPLIT=1
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$SRC/scripts/tt-rc-run" ] || SRC="$APP/app"                      # when run from the installed copy ($APP/install-claude.sh)
RC_UNIT=/etc/systemd/system/traffic-tester-claude-rc.service; RC_PATH=/etc/systemd/system/traffic-tester-claude-rc.path; RC_SYNC=/etc/systemd/system/traffic-tester-claude-rc-sync.service
CLAUDE_LIST=/etc/apt/sources.list.d/claude-code.list; CLAUDE_KEY=/etc/apt/keyrings/claude-code.asc; CLAUDE_FPR=31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE
HAVE_SYSTEMD=0; [ -d /run/systemd/system ] && [ "${TT_NO_SYSTEMD:-0}" != 1 ] && HAVE_SYSTEMD=1

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) MODE=apply; shift;;
    --uninstall) MODE=uninstall; shift;;
    --with-claude) WITH_CLAUDE=1; shift;;
    --no-net-split) NET_SPLIT=0; shift;;
    --app) APP="$2"; shift 2;;
    --data) DATA="$2"; shift 2;;
    --conf) CONF="$2"; shift 2;;
    --user) SVC_USER="$2"; shift 2;;
    *) die "Unknown option: $1";;
  esac
done
[ -n "$MODE" ] || die "usage: $0 --apply [--with-claude] [--no-net-split] | --uninstall"
[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."

# ------------------------------------------------------------------ uninstall
if [ "$MODE" = uninstall ]; then
  if [ -d "$DATA/claude" ]; then
    if [ -x "$APP/venv/bin/python" ] && id "$SVC_USER" >/dev/null 2>&1; then
      (cd "$APP/app" && sudo -u "$SVC_USER" env PYTHONPATH="$APP/app" "$APP/venv/bin/python" -m traffic_tester.claude.cli logout --data-dir "$DATA" >/dev/null 2>&1) || true
    fi
    rm -rf "$DATA/claude"                                               # whatever the CLI could not do, the sign-in is gone
  fi
  if [ "$HAVE_SYSTEMD" -eq 1 ]; then systemctl disable --now traffic-tester-claude-rc.path traffic-tester-claude-rc.service 2>/dev/null || true; fi
  rm -f "$RC_UNIT" "$RC_PATH" "$RC_SYNC"
  [ "$HAVE_SYSTEMD" -eq 1 ] && systemctl daemon-reload || true
  [ -x "$APP/app/scripts/net-split.sh" ] && "$APP/app/scripts/net-split.sh" --undo >/dev/null 2>&1 || true
  say "Claude sign-in removed, Remote Control units removed, network split undone. (Claude Code itself, if installed: sudo apt remove claude-code)"
  exit 0
fi

# ------------------------------------------------------------------ apply
# 1. helper scripts next to the app (root-owned below if Claude is installed)
mkdir -p "$APP/app/scripts"
for f in net-split.sh tt-rc-run tt-rc-sync claude_real_probe.py; do [ -f "$SRC/scripts/$f" ] && install -m 755 "$SRC/scripts/$f" "$APP/app/scripts/$f"; done

# 2. the app is a git repository: every change (the installer's, and any Claude suggested and you approved) is a commit that can be undone
if command -v git >/dev/null 2>&1 || apt-get install -y -qq --no-install-recommends git >/dev/null 2>&1; then
  GITID=(-c user.name="Pi Automation Traffic Tester (installer)" -c user.email=installer@localhost)
  VER="$(sed -n 's/.*__version__ *= *"\(.*\)".*/\1/p' "$APP/app/traffic_tester/__init__.py" 2>/dev/null | head -1)"
  ( cd "$APP/app" && { [ -d .git ] || git init -q; } && printf '__pycache__/\n*.pyc\n' > .gitignore \
    && git -c safe.directory="$APP/app" add -A && { git -c safe.directory="$APP/app" "${GITID[@]}" commit -q -m "Installer: Pi Automation Traffic Tester ${VER:-}" || true; } ) || warn "Could not record the app in git; 'Ask Claude' will not be able to apply changes."
else
  warn "git is not available: 'Ask Claude' cannot apply or undo changes."
fi

# 3. config the app reads
grep -q '^TT_APP_DIR=' "$CONF" 2>/dev/null || echo "TT_APP_DIR=$APP/app" >> "$CONF"
grep -q '^TT_WIPE_STRAY=' "$CONF" 2>/dev/null || echo "TT_WIPE_STRAY=1" >> "$CONF"

# 4. network split: Wi-Fi = internet + this web page, Ethernet test ports never the default route
if [ "$NET_SPLIT" -eq 1 ] && [ "$HAVE_SYSTEMD" -eq 1 ]; then
  say "Network: Wi-Fi = internet and this web page; Ethernet test ports never become the default route"
  "$APP/app/scripts/net-split.sh" --apply || warn "Could not apply the network split. Check later in the app: Settings -> Network."
fi

[ "$WITH_CLAUDE" -eq 1 ] || { say "Claude Code was not requested (add it later with: sudo $APP/install-pi.sh --with-claude)."; exit 0; }

# 5. Claude Code from Anthropic's signed apt repository (key fingerprint checked)
say "Installing Claude Code from Anthropic's signed apt repository (needs internet once)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends tmux curl gnupg ca-certificates >/dev/null
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://downloads.claude.ai/keys/claude-code.asc -o "$CLAUDE_KEY" || die "Could not download Anthropic's signing key (the Pi needs internet for this one step)."
GOT="$(gpg --show-keys --with-colons "$CLAUDE_KEY" 2>/dev/null | awk -F: '$1=="fpr"{print $10; exit}')"
[ "$GOT" = "$CLAUDE_FPR" ] || { rm -f "$CLAUDE_KEY"; die "Signing key fingerprint is '$GOT', expected '$CLAUDE_FPR'. Claude Code was NOT installed."; }
echo "deb [signed-by=$CLAUDE_KEY] https://downloads.claude.ai/claude-code/apt/stable stable main" > "$CLAUDE_LIST"
apt-get update -qq
apt-get install -y -qq claude-code >/dev/null || die "Claude Code could not be installed."
command -v claude >/dev/null 2>&1 || die "Claude Code installed but 'claude' is not on the PATH."
say "   $(claude --version 2>/dev/null | head -1) (stays OFF until you log in from the app)"

# 6. everything Claude stores lives in ONE folder, so log-out can wipe it and check that nothing is left
install -d -m 700 -o "$SVC_USER" -g "$SVC_USER" "$DATA/claude" "$DATA/claude/config" "$DATA/claude/home"
[ -f "$DATA/claude/state.json" ] || { echo '{"enabled": false, "remote_control": false}' > "$DATA/claude/state.json"; chown "$SVC_USER":"$SVC_USER" "$DATA/claude/state.json"; chmod 600 "$DATA/claude/state.json"; }
# the service user owns the app files (so approved changes can be applied), EXCEPT the parts that keep Claude Code safe: root-owned, read-only
chown -R "$SVC_USER":"$SVC_USER" "$APP/app"
chown -R root:root "$APP/app/traffic_tester/claude" "$APP/app/scripts"
chmod -R go-w "$APP/app/traffic_tester/claude" "$APP/app/scripts"

# 7. Remote Control: OFF until switched on in the app
if [ "$HAVE_SYSTEMD" -eq 1 ]; then
  cat > "$RC_UNIT" <<RCF
[Unit]
Description=Pi Automation Traffic Tester - Claude Code Remote Control (only while switched on in the app)
After=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_USER
Environment=TT_CLAUDE_BASE=$DATA/claude
RuntimeDirectory=traffic-tester-rc
RuntimeDirectoryMode=0700
ExecStart=$APP/app/scripts/tt-rc-run
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true
RCF
  cat > "$RC_SYNC" <<RCS
[Unit]
Description=Pi Automation Traffic Tester - match Remote Control to the app's switch

[Service]
Type=oneshot
Environment=TT_CLAUDE_BASE=$DATA/claude
ExecStart=$APP/app/scripts/tt-rc-sync
RCS
  cat > "$RC_PATH" <<RCP
[Unit]
Description=Pi Automation Traffic Tester - watch the Claude on/off switch

[Path]
PathChanged=$DATA/claude/state.json
Unit=traffic-tester-claude-rc-sync.service

[Install]
WantedBy=multi-user.target
RCP
  systemctl daemon-reload
  systemctl enable --now traffic-tester-claude-rc.path >/dev/null 2>&1 || warn "Could not enable the Remote Control switch watcher."
fi
say "Claude Code is installed and OFF. Turn it on in the app: Settings -> Claude Code -> Log in (set a web password first)."
