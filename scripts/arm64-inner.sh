#!/usr/bin/env bash
# Runs INSIDE an arm64 Debian Bookworm container (see scripts/arm64-check.sh). Mimics a Raspberry Pi OS Bookworm install.
set -uo pipefail
echo "### uname -m: $(uname -m)"; echo "### $(. /etc/os-release; echo "$PRETTY_NAME")"; echo "### python before install: $(command -v python3 || echo none)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq --no-install-recommends sudo curl procps util-linux iputils-ping socat >/dev/null 2>&1
echo "### 1. install-pi.sh (role A, no systemd in a container)"
bash /src/scripts/install-pi.sh --role A --port 8080 --no-service; echo "install rc=$?"
echo "### 1b. second run is idempotent"
bash /src/scripts/install-pi.sh --role A --port 8080 --no-service >/tmp/second.log 2>&1; echo "second install rc=$?"; tail -3 /tmp/second.log
echo "### 1c. installed files"
ls -l /etc/traffic-tester/config.env /etc/udev/rules.d/99-traffic-tester-ftdi.rules /etc/chrony/conf.d/traffic-tester.conf; cat /etc/traffic-tester/config.env; id traffic-tester
echo "### 1d. sniffer tools installed by install-pi.sh (arm64)"
for t in dumpcap tshark tcpdump iw ethtool; do printf "%s: %s\n" "$t" "$(command -v $t || echo MISSING)"; done
dumpcap --version 2>&1 | head -1; getcap "$(command -v dumpcap)" 2>&1; id traffic-tester
tcpdump -d "tcp port 20000 or udp port 20000" 2>&1 | head -2
echo "### 2. start app as the service user, hit the API"
install -d -o traffic-tester -g traffic-tester /tmp/ttdata
( cd /opt/traffic-tester/app && sudo -u traffic-tester /opt/traffic-tester/venv/bin/python -m traffic_tester --role A --port 8080 --data-dir /tmp/ttdata >/tmp/app.log 2>&1 & )
for i in $(seq 1 60); do curl -fs localhost:8080/api/status >/dev/null 2>&1 && break; sleep 1; done
echo "status: $(curl -s localhost:8080/api/status | head -c 300)"
echo "profiles: $(curl -s localhost:8080/api/profiles | python3 -c 'import sys,json; print(len(json.load(sys.stdin)), "profiles")')"
echo "ui: $(curl -s localhost:8080/ | grep -o '<title>[^<]*')"
pkill -f "traffic_tester --role A --port 8080" || true
echo "### python after install: $(python3 --version)"
echo "### 3. unit tests on arm64"
mkdir -p /work && cp -r /src/traffic_tester /src/tests /src/scripts /src/requirements.txt /work/ && ln -sfn /opt/traffic-tester/venv /work/venv
/opt/traffic-tester/venv/bin/pip install -q pytest httpx websockets
cd /work && PYTHONDONTWRITEBYTECODE=1 /opt/traffic-tester/venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -5
echo "### 3a. sniffer smoke on arm64: serial tap over pty pairs, DNP3/Modbus decode, pcapng"
/opt/traffic-tester/venv/bin/python -m pytest -q -p no:cacheprovider tests/test_sniff.py 2>&1 | tail -2
echo "### 3b. traffic generator smoke on arm64 (two instances, UDP + TCP + IMIX, PDF)"
/opt/traffic-tester/venv/bin/python scripts/gen_smoke.py 2>&1 | tail -12
echo "### 4. e2e on arm64: two instances, serial pty pair (socat), UDP, TCP"
mkdir -p /work/docs
/opt/traffic-tester/venv/bin/python scripts/e2e_local.py serial hooks profiles udp tcp 2>&1 | tail -45
echo "### 5. netem + GOOSE (privileged container, veth namespaces)"
/opt/traffic-tester/venv/bin/python scripts/e2e_local.py netem goose 2>&1 | tail -45
echo "### 5b. REVERSE direction on arm64: Pi B is the tester, Pi A the responder (serial, hooks, profiles, UDP, TCP, netem, GOOSE)"
E2E_REVERSE=1 /opt/traffic-tester/venv/bin/python scripts/e2e_local.py serial hooks profiles udp tcp netem goose 2>&1 | tail -40
echo "### 5c. network split (Wi-Fi = internet, Ethernet = test) in namespaces on arm64, both directions"
/opt/traffic-tester/venv/bin/python scripts/e2e_netsplit.py 2>&1 | tail -30
echo "### 5d. sniffer on arm64: live capture needs real kernel interfaces - probe, then decode the two-Pi sample with the arm64 tshark"
ip link add pz0 type veth peer name pz1 && ip link set pz0 up && ip link set pz1 up
timeout 10 dumpcap -q -n -i pz0 -c 1 -w /tmp/probe.pcapng >/dev/null 2>/tmp/probe.err &
sleep 4; ping -c1 -W1 -I pz1 1.1.1.1 >/dev/null 2>&1; wait
if [ -s /tmp/probe.pcapng ]; then echo "CHECK arm64 dumpcap can capture a live interface | pass | file written"
else echo "CHECK arm64 dumpcap can capture a live interface under QEMU | unresolved | $(tr '\n' ' ' < /tmp/probe.err | cut -c1-200) (qemu-user cannot run the capture ioctls; tcpdump fails the same way; needs the real Pi)"; fi
S=/src/docs/sniff-samples/sample-two-pi-both-ways-goose-udp.pcapng
G=$(tshark -r $S -Y goose 2>/dev/null | wc -l); R=$(tshark -r $S -Y "r-goose" 2>/dev/null | wc -l); M=$(tshark -r $S -Y "_ws.malformed" 2>/dev/null | wc -l)
echo "CHECK arm64 tshark (aarch64) dissects the two-Pi sample: GOOSE L2 frames=$G, R-GOOSE frames=$R, malformed=$M | $([ "$G" -gt 20 ] && [ "$R" -gt 20 ] && [ "$M" = 0 ] && echo pass || echo FAIL) | $(tshark --version | head -1)"
echo "### 6. uninstall"
bash /src/scripts/install-pi.sh --uninstall >/tmp/un.log 2>&1; echo "uninstall rc=$?"; tail -2 /tmp/un.log; ls /opt/traffic-tester 2>&1 | head -1
echo "### DONE"
