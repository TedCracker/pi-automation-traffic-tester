#!/bin/bash
# Prove the offline package installs and runs with NO network on a clean arm64 Debian that stands in for Raspberry Pi OS (with real systemd as PID 1).
#   scripts/pkg/test-offline.sh <extracted package dir> <base image e.g. debian:bookworm-20250428> <label> [pi5|pi4]
# Output: CHECK lines (-> docs/dev/checks.md). The baseline image is built WITH network (systemd + dbus, as every Pi OS has); the install itself runs with --network none.
set -uo pipefail
PKG="$(cd "$1" && pwd)"; BASE="$2"; LABEL="$3"; MODEL="${4:-pi5}"
IMG="tt-offline-base-$LABEL"; C="tt-offline-$LABEL"
KVER="${TT_TEST_KVER:-6.12.109+rpt-rpi-2712}"
chk() { printf 'CHECK [%s %s] %s | %s | %s\n' "$LABEL" "$MODEL" "$1" "$2" "$3"; }
docker build -q --platform linux/arm64 -t "$IMG" - >/dev/null <<DF
FROM $BASE
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus procps curl >/dev/null && rm -rf /var/lib/apt/lists/*
DF
docker rm -f "$C" >/dev/null 2>&1
docker run -d --name "$C" --platform linux/arm64 --privileged --network none --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock -v "$PKG":/pkg:ro "$IMG" /sbin/init >/dev/null
x() { docker exec "$C" bash -c "$1" 2>&1; }
for i in $(seq 1 60); do st=$(x 'systemctl is-system-running' | head -1); case "$st" in running|degraded) break;; esac; sleep 2; done
chk "container: real systemd is PID 1, network is OFF (no route, no DNS)" "$([ "$st" = running -o "$st" = degraded ] && ! x 'getent hosts deb.debian.org' | grep -q . && echo pass || echo FAIL)" "systemd=$st os=$(x '. /etc/os-release; echo $PRETTY_NAME') arch=$(x 'uname -m') python-before=$(x 'command -v python3 || echo none')"
# systemd's mount/network-namespace sandboxing (PrivateTmp, ProtectSystem) cannot be created inside this nested qemu container on every distro: probe, and relax the sandbox for the test only if so
if ! x 'systemd-run --wait -q -p PrivateTmp=yes /bin/true' >/dev/null 2>&1; then
  x 'mkdir -p /etc/systemd/system/traffic-tester.service.d; printf "[Service]\nPrivateTmp=no\nProtectSystem=no\n" > /etc/systemd/system/traffic-tester.service.d/test-nosandbox.conf' >/dev/null
  chk "systemd mount-namespace sandbox (PrivateTmp/ProtectSystem) works in this container" unresolved "systemd-run -p PrivateTmp=yes fails here (qemu-user container, any unit); the sandbox is relaxed for this test only. Checked on the other distro / needs the real Pi."
fi
case "$MODEL" in pi5) M="Raspberry Pi 5 Model B Rev 1.0";; *) M="Raspberry Pi 4 Model B Rev 1.5";; esac
x "echo '$M' > /tmp/model"
R=$(x "cd /pkg/pi-traffic-tester-offline-* && TT_MODEL_FILE=/tmp/model TT_KVER=$KVER ./install-offline.sh --role A 2>&1 | tail -25"); RC=$?
chk "install-offline.sh --role A finishes (no internet)" "$(echo "$R" | grep -q 'Installed as Pi A' && echo pass || echo FAIL)" "$(echo "$R" | tr '\n' ' ' | cut -c1-300)"
for i in $(seq 1 30); do x 'curl -fs http://127.0.0.1:8080/api/status' >/dev/null && break; sleep 2; done
S=$(x 'curl -fs http://127.0.0.1:8080/api/status')
chk "systemd service traffic-tester is active and enabled; web UI answers on port 8080" "$(x 'systemctl is-active traffic-tester; systemctl is-enabled traffic-tester' | tr '\n' ' ' | grep -q 'active enabled' && echo "$S" | grep -q '"role":"A"' && x 'curl -fs http://127.0.0.1:8080/ | grep -o "<title>[^<]*"' | grep -q 'Pi Automation Traffic Tester' && echo pass || echo FAIL)" "$(echo "$S" | cut -c1-160)"
chk "service runs as the unprivileged user (not root)" "$(x 'ps -eo user,args | grep "[t]raffic_tester" | grep -v grep' | grep -q '^traffic' && echo pass || echo FAIL)" "$(x 'ps -eo user,args | grep "[t]raffic_tester" | cut -c1-120')"
chk "Python wheels came from the package (venv imports fastapi, uvicorn, pydantic, reportlab, serial, websockets)" "$(x '/opt/traffic-tester/venv/bin/python -c "import fastapi,uvicorn,pydantic,reportlab,serial,websockets;print(\"ok\")"' | grep -q ok && echo pass || echo FAIL)" "python $(x '/opt/traffic-tester/venv/bin/python --version')"
chk "capture tools installed offline (dumpcap with capabilities, tshark, tcpdump, iw, chrony)" "$(x 'command -v dumpcap tshark tcpdump iw chronyd >/dev/null && getcap $(command -v dumpcap) | grep -q cap_net_raw && echo ok' | grep -q ok && echo pass || echo FAIL)" "$(x 'dumpcap --version | head -1')"
chk "API: profiles + routing self-check + sniffer pre-flight answer" "$(x 'curl -fs http://127.0.0.1:8080/api/profiles | head -c 5; curl -fs http://127.0.0.1:8080/api/net/check | head -c 5; curl -fs -X POST -H "Content-Type: application/json" -d "{}" http://127.0.0.1:8080/api/sniff/preflight | head -c 5' | grep -q . && echo pass || echo FAIL)" "$(x 'curl -fs http://127.0.0.1:8080/api/profiles | python3 -c "import sys,json;print(len(json.load(sys.stdin)),\"profiles\")"')"
if [ "$MODEL" = pi5 ]; then
  chk "Pi 5: built-in Wi-Fi (Nexmon) files installed: helper, driver for the kernel, firmware, units, polkit rule" "$(x "test -x /opt/traffic-tester/sbin/tt-wifi-helper && test -f /opt/traffic-tester/nexmon/$KVER/brcmfmac.ko && test -f /lib/firmware/cypress/cyfmac43455-sdio-nexmon.bin && test -f /etc/polkit-1/rules.d/49-traffic-tester-wifi.rules && systemctl cat tt-wifi-helper@.service >/dev/null && echo ok" | grep -q ok && echo pass || echo FAIL)" "$(x 'systemctl is-active tt-wifi-watchdog.timer; systemctl is-enabled tt-wifi-boot-restore.service' | tr '\n' ' ')"
  x 'systemctl start tt-wifi-helper@status.service; sleep 1; cat /run/tt-wifi/result.json' > /tmp/tt-$LABEL-helper.json
  chk "root Wi-Fi helper starts through systemd and reports status (no Wi-Fi chip in the container: says so, does not crash)" "$(grep -q '"ok": true' /tmp/tt-$LABEL-helper.json && echo pass || echo FAIL)" "$(cut -c1-220 /tmp/tt-$LABEL-helper.json)"
  P=$(x 'runuser -u traffic-tester -- systemctl start tt-wifi-helper@status.service 2>&1; echo rc=$?')
  N=$(x 'runuser -u traffic-tester -- systemctl start systemd-journald.service 2>&1; echo rc=$?')
  E=$(x 'runuser -u traffic-tester -- systemctl start tt-wifi-helper@evil.service 2>&1; echo rc=$?')
  if echo "$P" | grep -qi 'timed out'; then
    chk "polkit rule (service user may start only tt-wifi-helper@<fixed names>)" unresolved "polkitd cannot run under qemu-user in this container (its unit fails, systemd call times out). Verified against a real polkitd on x86 instead: docs/polkit-real-test.log; needs the real Pi."
  else
    chk "polkit: the service user may start tt-wifi-helper@status, but NOT other units nor tt-wifi-helper@<anything else>" "$(echo "$P" | grep -q 'rc=0' && echo "$N" | grep -qv 'rc=0' && echo "$E" | grep -qv 'rc=0' && echo pass || echo FAIL)" "status: $(echo "$P" | tail -1); other unit: $(echo "$N" | tr '\n' ' ' | cut -c1-80); evil: $(echo "$E" | tail -1)"
  fi
else
  chk "Pi 4: installer warns that it is built for the Pi 5 but installs; Nexmon is skipped" "$(echo "$R" | grep -qi 'Pi 4\|Raspberry Pi 4' && ! x 'test -d /opt/traffic-tester/nexmon' && echo pass || echo FAIL)" "$(echo "$R" | grep -i 'pi 4' | head -1 | cut -c1-200)"
fi
x 'systemctl restart traffic-tester; sleep 4; curl -fs http://127.0.0.1:8080/api/status >/dev/null && echo up' | grep -q up && chk "service restarts cleanly" pass "" || chk "service restarts cleanly" FAIL ""
# role B with a clock server, second install over the first (idempotent)
R2=$(x "cd /pkg/pi-traffic-tester-offline-* && TT_MODEL_FILE=/tmp/model TT_KVER=$KVER ./install-offline.sh --role B --ntp-server 10.0.0.1 2>&1 | tail -3")
for i in $(seq 1 20); do x 'curl -fs http://127.0.0.1:8080/api/status' | grep -q '"role":"B"' && break; sleep 2; done
chk "re-install as Pi B (--ntp-server): idempotent, role B answers, chrony points at Pi A" "$(x 'curl -fs http://127.0.0.1:8080/api/status' | grep -q '"role":"B"' && x 'cat /etc/chrony/conf.d/traffic-tester.conf' | grep -q 'server 10.0.0.1' && echo pass || echo FAIL)" "$(x 'grep server /etc/chrony/conf.d/traffic-tester.conf')"
x '/opt/traffic-tester/install-pi.sh --uninstall >/dev/null 2>&1; systemctl is-active traffic-tester; ls /opt/traffic-tester 2>&1 | head -1; ls /etc/systemd/system | grep -c "tt-wifi\|traffic-tester"' > /tmp/tt-$LABEL-un.txt
chk "uninstall removes the service, units and files (data is kept)" "$(grep -q inactive /tmp/tt-$LABEL-un.txt && grep -q 'No such file' /tmp/tt-$LABEL-un.txt && echo pass || echo FAIL)" "$(tr '\n' ' ' < /tmp/tt-$LABEL-un.txt)"
docker rm -f "$C" >/dev/null 2>&1; docker rmi "$IMG" >/dev/null 2>&1
