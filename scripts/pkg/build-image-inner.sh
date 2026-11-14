#!/bin/bash
# Runs INSIDE the privileged debian:trixie container started by build-image.sh. /w = build-image/ (base xz, stage/, result work.img).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
say() { printf '\033[1;32m[image]\033[0m %s\n' "$*"; }
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends mount fdisk e2fsprogs dosfstools xz-utils rsync ca-certificates >/dev/null
for i in $(seq 0 63); do [ -e /dev/loop$i ] || mknod /dev/loop$i b 7 $i; done        # the container sees a static /dev: make the loop nodes the kernel may hand out
IMG=/w/work.img; R=/mnt/root; GROW_MB="${GROW_MB:-2500}"
say "Unpacking the base image"
xz -dc "$BASE_XZ" > "$IMG"
truncate -s +"${GROW_MB}M" "$IMG"
echo ", +" | sfdisk -q -N 2 "$IMG"                                   # root partition takes the new space (disk id and PARTUUIDs stay, so fstab/cmdline.txt stay valid)
part() { sfdisk -d "$IMG" | sed -n "s|^$IMG${1} *: *start= *\([0-9]*\), *size= *\([0-9]*\).*|\1 \2|p"; }
read -r S1 N1 <<<"$(part 1)"; read -r S2 N2 <<<"$(part 2)"
LB=$(losetup -f --show -o $((S1*512)) --sizelimit $((N1*512)) "$IMG"); LR=$(losetup -f --show -o $((S2*512)) --sizelimit $((N2*512)) "$IMG")
e2fsck -fp "$LR" >/dev/null || true; resize2fs "$LR" >/dev/null
mkdir -p $R; mount "$LR" $R; mkdir -p $R/boot/firmware; mount -t vfat "$LB" $R/boot/firmware
mount --bind /dev $R/dev; mount -t devpts devpts $R/dev/pts; mount -t proc proc $R/proc; mount -t sysfs sys $R/sys
cleanup() { for m in $R/sys $R/proc $R/dev/pts $R/dev $R/boot/firmware $R; do umount -l "$m" 2>/dev/null || true; done; losetup -D 2>/dev/null || true; }
trap cleanup EXIT
KERNEL="$(ls $R/lib/modules | grep -- '-rpi-2712$' | sort -V | tail -1)"; say "Card kernel: $KERNEL; base: $(. $R/etc/os-release; echo $PRETTY_NAME)"

# network + no daemons starting inside the chroot
if [ -L $R/etc/resolv.conf ]; then RESOLV_LINK="$(readlink $R/etc/resolv.conf)"; else RESOLV_LINK=""; fi
rm -f $R/etc/resolv.conf; cp /etc/resolv.conf $R/etc/resolv.conf
printf '#!/bin/sh\nexit 101\n' > $R/usr/sbin/policy-rc.d; chmod +x $R/usr/sbin/policy-rc.d
# the card starts as "pitester" (http://pitester.local:8080); naming the Pi on the phone changes it
echo pitester > $R/etc/hostname
if grep -q '^127\.0\.1\.1' $R/etc/hosts; then sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tpitester/' $R/etc/hosts; else printf '127.0.1.1\tpitester\n' >> $R/etc/hosts; fi

say "Copying the app in and running the installer (this is the slow part)"
mkdir -p $R/tmp/ttsrc; cp -a /w/stage/. $R/tmp/ttsrc/
NXARG=""; [ -d /w/stage/nexmon-dir ] && NXARG="--nexmon /tmp/ttsrc/nexmon-dir"
chroot $R /bin/bash -c "cd /tmp/ttsrc && ./scripts/install-pi.sh --image $NXARG" 2>&1 | sed 's/^/    /'

say "Cleaning the image (no identity, no build leftovers)"
rm -rf $R/tmp/ttsrc $R/usr/sbin/policy-rc.d
chroot $R apt-get clean; rm -rf $R/var/lib/apt/lists/* $R/var/cache/apt/*.bin $R/root/.cache $R/root/.wget-hsts
# nothing of this build's app state may be on the card: every Pi made from it must create its own id / first-setup time / password
rm -f $R/var/lib/traffic-tester/*.db $R/var/lib/traffic-tester/*.db-* $R/var/lib/traffic-tester-update/firstboot-done
find $R/var/log -type f -exec truncate -s 0 {} + 2>/dev/null || true
rm -rf $R/var/log/journal/* $R/root/.bash_history $R/etc/ssh/ssh_host_* 2>/dev/null || true
echo uninitialized > $R/etc/machine-id; rm -f $R/var/lib/dbus/machine-id
rm -f $R/etc/resolv.conf; if [ -n "$RESOLV_LINK" ]; then ln -s "$RESOLV_LINK" $R/etc/resolv.conf; else : > $R/etc/resolv.conf; fi
sync

say "Checks on the finished root file system"
fail=0
ck() { if eval "$2"; then echo "    ok   $1"; else echo "    FAIL $1"; fail=1; fi; }
W=$R/etc/systemd/system/multi-user.target.wants; NMK=$R/etc/NetworkManager/system-connections/tt-ap.nmconnection
ck "first-boot marker present"          "[ -e $R/var/lib/traffic-tester/first-boot ]"
ck "service enabled"                    "[ -L $W/traffic-tester.service ]"
ck "netwatch (emergency AP) enabled"    "[ -L $W/traffic-tester-netwatch.service ]"
ck "first-boot unit enabled"            "[ -L $W/traffic-tester-firstboot.service ]"
ck "AP profile pi-setup / password / on" "grep -q '^ssid=pi-setup' $NMK && grep -q '^psk=password' $NMK && grep -q '^autoconnect=true' $NMK"
ck "AP profile root-only (0600)"        "[ \$(stat -c %a $NMK) = 600 ]"
ck "captive DNS conf present"           "grep -q 'address=/#/10.42.0.1' $R/etc/NetworkManager/dnsmasq-shared.d/tt-captive.conf"
ck "polkit rule for the update unit"    "[ -f $R/etc/polkit-1/rules.d/50-traffic-tester-update.rules ]"
ck "update helper (root-owned)"         "[ -f $R/opt/traffic-tester/update-helper/traffic_tester/update/helper.py ]"
ck "release public key"                 "[ -s $R/etc/traffic-tester/update-pubkey.hex ]"
ck "deploy key (0600)"                  "[ -f $R/etc/traffic-tester/update/deploy_key ] && [ \$(stat -c %a $R/etc/traffic-tester/update/deploy_key) = 600 ]"
ck "FTDI udev rule"                     "grep -q ftdi_sio $R/etc/udev/rules.d/99-traffic-tester-ftdi.rules"
ck "chrony conf"                        "[ -f $R/etc/chrony/conf.d/traffic-tester.conf ]"
ck "venv imports the app"               "chroot $R /opt/traffic-tester/venv/bin/python -c 'import sys; sys.path.insert(0, \"/opt/traffic-tester/app\"); import traffic_tester, fastapi, uvicorn, serial, websockets, zeroconf'"
ck "dumpcap capabilities"               "chroot $R getcap /usr/bin/dumpcap | grep -q cap_net_raw"
ck "no app database on the card"        "! ls $R/var/lib/traffic-tester/*.db >/dev/null 2>&1"
ck "SSH is off (no ssh unit enabled)"   "! ls $W/ssh* $R/etc/systemd/system/sockets.target.wants/ssh* >/dev/null 2>&1"
ck "preset keeps SSH off at the first boot" "grep -q '^disable ssh.service' $R/etc/systemd/system-preset/00-traffic-tester-ssh-off.preset"
ck "no SSH host keys"                   "! ls $R/etc/ssh/ssh_host_* >/dev/null 2>&1"
ck "no account has a usable password (the stock placeholder user 'pi' is locked + nologin)" "! awk -F: '\$2 !~ /^[!*]/ {print \$1}' $R/etc/shadow | grep -q ."
ck "boot files untouched (cmdline.txt)" "grep -q 'root=PARTUUID=' $R/boot/firmware/cmdline.txt"
ck "hostname pitester"                  "[ \"\$(cat $R/etc/hostname)\" = pitester ]"
if [ -d /w/stage/nexmon-dir ]; then ck "Nexmon module for the card kernel" "[ -f $R/opt/traffic-tester/nexmon/$KERNEL/brcmfmac.ko ]"; fi
[ "$fail" = 0 ] || { echo "IMAGE CHECKS FAILED"; exit 1; }

say "Shrinking"
trap - EXIT; cleanup
LR=$(losetup -f --show -o $((S2*512)) --sizelimit $((N2*512)) "$IMG")
e2fsck -fy "$LR" >/dev/null || true
resize2fs -M "$LR" >/dev/null 2>&1
e2fsck -fn "$LR" >/dev/null
BLK=$(dumpe2fs -h "$LR" 2>/dev/null | sed -n 's/^Block count: *//p'); BS=$(dumpe2fs -h "$LR" 2>/dev/null | sed -n 's/^Block size: *//p')
losetup -d "$LR"
NEWSECT=$(( (BLK*BS + 64*1024*1024) / 512 ))                           # +64 MB headroom: the stock first boot expands to the whole card anyway
echo ", $NEWSECT" | sfdisk -q -N 2 "$IMG"
END=$(( S2 + NEWSECT )); truncate -s $(( END*512 )) "$IMG"
LR=$(losetup -f --show -o $((S2*512)) --sizelimit $((NEWSECT*512)) "$IMG"); resize2fs "$LR" >/dev/null 2>&1 || true
e2fsck -fn "$LR" >/dev/null && say "root fs clean after shrink"; losetup -d "$LR"
sfdisk -d "$IMG" | tail -3
ls -la "$IMG"
say "done"
