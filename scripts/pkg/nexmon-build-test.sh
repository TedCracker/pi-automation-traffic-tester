#!/bin/bash
# Runs INSIDE an arm64 Debian Bookworm container: does Kali's brcmfmac-nexmon-dkms build against the Raspberry Pi OS Pi 5 kernel headers?
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
echo "### $(uname -m) $(. /etc/os-release; echo $PRETTY_NAME)"
apt-get update -qq && apt-get install -y -qq --no-install-recommends curl gnupg ca-certificates >/dev/null
curl -fsSL https://archive.raspberrypi.com/debian/raspberrypi.gpg.key | gpg --dearmor -o /usr/share/keyrings/raspberrypi-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.gpg] http://archive.raspberrypi.com/debian/ bookworm main" > /etc/apt/sources.list.d/raspi.list
apt-get update -qq
KVER=$(apt-cache depends linux-headers-rpi-2712 | awk '/Depends:/{print $2}' | sed 's/linux-headers-//')
echo "### Pi 5 kernel flavour headers: $KVER ($(apt-cache policy linux-headers-rpi-2712 | awk '/Candidate/{print $2}'))"
apt-get install -y -qq --no-install-recommends linux-headers-rpi-2712 build-essential dkms bc kmod >/dev/null 2>&1; echo "headers install rc=$?"
ls /lib/modules/ ; ls /usr/src | head
dpkg -i /nexmon/brcmfmac-nexmon-dkms_6.12.2_all.deb 2>&1 | tail -25
echo "### dkms status"; dkms status
echo "### build log tail"; tail -30 /var/lib/dkms/brcmfmac-nexmon/6.12.2/build/make.log 2>/dev/null
echo "### built modules"; find /lib/modules -name "brcmfmac*.ko*" | head
for k in $(find /lib/modules -name "brcmfmac.ko*" -path "*updates*"); do modinfo $k | grep -E "^(filename|version|vermagic|description|depends|retpoline|name)"; mkdir -p /out; cp $k /out/; done
modinfo -k "$(ls /lib/modules | head -1)" brcmfmac 2>/dev/null | grep -E "^(filename|vermagic|depends)"
echo "### DONE"
