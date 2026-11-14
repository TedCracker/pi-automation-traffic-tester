#!/bin/bash
# The permission rule against a REAL polkitd (x86 Debian Bookworm container with systemd as PID 1; polkitd cannot run under qemu-user, so not on arm64).
#   scripts/pkg/test-polkit-real.sh <extracted offline package dir>      -> prints start/stop results for the service user
set -uo pipefail
PKG="$(cd "$1" && pwd)"
docker rm -f ttpk >/dev/null 2>&1
docker build -q -t ttpk-img - >/dev/null <<DF
FROM debian:bookworm
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus polkitd procps >/dev/null && useradd -m traffic-tester
DF
docker run -d --name ttpk --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock -v "$PKG":/pkg:ro ttpk-img /sbin/init >/dev/null
sleep 8
docker exec ttpk bash -c '
cd /pkg/pi-traffic-tester-offline-*
SBIN=/opt/traffic-tester/sbin; POLKIT=/etc/polkit-1/rules.d/49-traffic-tester-wifi.rules; SVC_USER=traffic-tester
eval "$(sed -n "/^cat > \"\$POLKIT\" <<RULE/,/^RULE\$/p" app/scripts/install-wifi-nexmon.sh)"; sed -i "/^polakit_unused/d" $POLKIT
printf "[Service]\nType=oneshot\nExecStart=/bin/echo helper %%i\n" > "/etc/systemd/system/tt-wifi-helper@.service"; systemctl daemon-reload; systemctl restart polkit; sleep 2; echo "polkit: $(systemctl is-active polkit)"
for u in tt-wifi-helper@status.service tt-wifi-helper@monitor.service tt-wifi-helper@channel.service tt-wifi-helper@restore.service tt-wifi-helper@inject-test.service tt-wifi-helper@evil.service tt-wifi-helper@status.service.evil ssh.service systemd-journald.service; do runuser -u traffic-tester -- systemctl start $u >/dev/null 2>&1; echo "service user: start $u -> $([ $? = 0 ] && echo ALLOWED || echo denied)"; done
runuser -u traffic-tester -- systemctl stop tt-wifi-helper@status.service >/dev/null 2>&1; echo "service user: stop tt-wifi-helper@status -> $([ $? = 0 ] && echo ALLOWED || echo denied)"
runuser -u traffic-tester -- systemctl restart tt-wifi-helper@status.service >/dev/null 2>&1; echo "service user: restart tt-wifi-helper@status -> $([ $? = 0 ] && echo ALLOWED || echo denied)"'
docker rm -f ttpk >/dev/null 2>&1; docker rmi ttpk-img >/dev/null 2>&1
