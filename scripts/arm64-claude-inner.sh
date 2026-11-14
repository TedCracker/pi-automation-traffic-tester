#!/usr/bin/env bash
# Runs INSIDE an arm64 Debian Bookworm container (see scripts/arm64-claude-check.sh): installs with --with-claude from Anthropic's apt repo and probes the REAL claude CLI.
set -uo pipefail
echo "### uname -m: $(uname -m)  $(. /etc/os-release; echo "$PRETTY_NAME")"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq --no-install-recommends sudo curl procps git >/dev/null 2>&1
echo "### 1. install-pi.sh --with-claude (no systemd in a container)"
bash /src/scripts/install-pi.sh --role A --port 8080 --no-service --no-wifi-nexmon --with-claude 2>&1 | tail -25; echo "install rc=${PIPESTATUS[0]}"
echo "### 2. Claude Code is installed from the signed repo"
command -v claude; claude --version; dpkg -s claude-code | grep -E "^(Package|Version|Architecture)"; cat /etc/apt/sources.list.d/claude-code.list
echo "### 3. layout: app in git, contained Claude dirs, safety files root-owned"
git -c safe.directory=/opt/traffic-tester/app -C /opt/traffic-tester/app log --format='%h %an: %s' | head -3
ls -ld /var/lib/traffic-tester/claude /var/lib/traffic-tester/claude/config /var/lib/traffic-tester/claude/home; cat /var/lib/traffic-tester/claude/state.json
stat -c '%U:%G %a %n' /opt/traffic-tester/app/traffic_tester/claude /opt/traffic-tester/app/scripts /opt/traffic-tester/app/traffic_tester/server.py
grep -E "TT_APP_DIR|TT_WIPE_STRAY" /etc/traffic-tester/config.env
sudo -u traffic-tester sh -c 'touch /opt/traffic-tester/app/traffic_tester/claude/x 2>&1 | head -1; touch /opt/traffic-tester/app/traffic_tester/static/ok && echo "service user can write the normal app files" && rm /opt/traffic-tester/app/traffic_tester/static/ok'
echo "### 4. probe the REAL claude CLI as the service user"
sudo -u traffic-tester env PYTHONPATH=/opt/traffic-tester/app TT_APP_DIR=/opt/traffic-tester/app /opt/traffic-tester/venv/bin/python /opt/traffic-tester/app/scripts/claude_real_probe.py --data-dir /tmp/ttprobe; ls -l /opt/traffic-tester/app/scripts/
echo "### 5. app endpoints with the REAL claude: off by default, refuses, then CLI status"
install -d -o traffic-tester -g traffic-tester /tmp/ttdata
( cd /opt/traffic-tester/app && sudo -u traffic-tester env TT_APP_DIR=/opt/traffic-tester/app /opt/traffic-tester/venv/bin/python -m traffic_tester --role A --port 8080 --data-dir /tmp/ttdata >/tmp/app.log 2>&1 & )
for i in $(seq 1 60); do curl -fs localhost:8080/api/status >/dev/null 2>&1 && break; sleep 1; done
echo "claude status: $(curl -s localhost:8080/api/claude/status)"
echo "chat while off: $(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' -d '{"message":"hi"}' localhost:8080/api/claude/chat)"
echo "tool while off: $(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' -d '{}' localhost:8080/api/claude/tool/get_screen)"
echo "login without password: $(curl -s -X POST localhost:8080/api/claude/login/start | head -c 200)"
echo "net check: $(curl -s localhost:8080/api/net/check | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["status"], [c["title"] for c in d["checks"]])')"
echo "### 6. real-CLI log-out sequence on a real (empty) config: nothing left"
curl -s -X POST localhost:8080/api/claude/logout | python3 -c 'import sys,json; d=json.load(sys.stdin); print("verified:", d["verified"], d["steps"])'
pkill -f "traffic_tester --role A --port 8080" || true
echo "### 7. Claude tests on arm64"
mkdir -p /work && cp -r /src/traffic_tester /src/tests /src/scripts /src/requirements.txt /work/ && ln -sfn /opt/traffic-tester/venv /work/venv
/opt/traffic-tester/venv/bin/pip install -q pytest httpx websockets
cd /work && PYTHONDONTWRITEBYTECODE=1 /opt/traffic-tester/venv/bin/python -m pytest -q -p no:cacheprovider tests/test_claude.py tests/test_netsplit.py 2>&1 | tail -5
echo "### 8. re-install keeps the git history, then uninstall removes the Claude data"
bash /src/scripts/install-pi.sh --role A --port 8080 --no-service --no-wifi-nexmon --with-claude >/tmp/second.log 2>&1; echo "second install rc=$?"
git -c safe.directory=/opt/traffic-tester/app -C /opt/traffic-tester/app log --format='%h %an: %s' | head -3
mkdir -p /var/lib/traffic-tester/claude/config; echo '{"claudeAiOauth":{"accessToken":"sk-ant-oat01-PLANTEDPLANTEDPLANTED"}}' > /var/lib/traffic-tester/claude/config/.credentials.json
bash /src/scripts/install-pi.sh --uninstall >/tmp/un.log 2>&1; echo "uninstall rc=$?"
ls /var/lib/traffic-tester/claude 2>&1 | head -1; ls /opt/traffic-tester 2>&1 | head -1
grep -rl "sk-ant-oat01-PLANTED" /var/lib /opt /etc /tmp /root /home 2>/dev/null | head -3; echo "(nothing listed above = no planted token left)"
echo "### DONE"
