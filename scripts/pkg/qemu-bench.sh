#!/bin/bash
# Runs INSIDE the tt-qemu-image container (privileged). Boots copies of the card image in QEMU (aarch64 "virt", TCG) on a bridge with DHCP + NAT.
#   qemu-bench.sh setup
#   qemu-bench.sh prep  <name> <image> [seed-dir]    copy the card image, grow the root partition (what the stock first boot does on a real card), give it a kernel's modules; seed-dir = files for the boot partition (Imager-style user-data / network-config)
#   qemu-bench.sh start <name> <last-octet> <host-port>   boots it; the app (guest 8080) is forwarded to <host-port> of this container
#   qemu-bench.sh stop  <name>                      power off, then fsck the copy
#   qemu-bench.sh cat   <name> <path>...            print files from the (stopped) copy's root file system
#   qemu-bench.sh log   <name>                      the serial console log
set -euo pipefail
V=/vm; mkdir -p $V; BR=br0; NET=10.99.0
for i in $(seq 0 63); do [ -e /dev/loop$i ] || mknod /dev/loop$i b 7 $i 2>/dev/null || true; done
mac() { printf '52:54:00:99:00:%02x' "$1"; }
cmd="${1:-}"; name="${2:-}"
case "$cmd" in
setup)
  ip link show $BR >/dev/null 2>&1 || { ip link add $BR type bridge; ip addr add $NET.1/24 dev $BR; ip link set $BR up; }
  sysctl -q -w net.ipv4.ip_forward=1
  iptables -t nat -C POSTROUTING -s $NET.0/24 ! -o $BR -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -s $NET.0/24 ! -o $BR -j MASQUERADE
  iptables -C FORWARD -i $BR -j ACCEPT 2>/dev/null || { iptables -I FORWARD -i $BR -j ACCEPT; iptables -I FORWARD -o $BR -j ACCEPT; }
  { echo "interface=$BR"; echo "bind-interfaces"; echo "dhcp-range=$NET.50,$NET.99,12h"; echo "dhcp-option=3,$NET.1"; echo "dhcp-option=6,$NET.1"
    for n in 11 12 13 14; do echo "dhcp-host=$(mac $n),$NET.$n"; done; echo "server=1.1.1.1"; echo "server=8.8.8.8"; echo "no-resolv"; } > $V/dnsmasq.conf
  pgrep dnsmasq >/dev/null || dnsmasq -C $V/dnsmasq.conf --pid-file=$V/dnsmasq.pid
  echo "bridge ready" ;;
prep)
  img="$3"; seed="${4:-}"; out=$V/$name.img
  cp --sparse=always "$img" "$out"; truncate -s +1500M "$out"
  echo ", +" | sfdisk -q -N 2 "$out"
  rd() { sfdisk -d "$out" | sed -n "s|^$out${1} *: *start= *\([0-9]*\), *size= *\([0-9]*\).*|\1 \2|p"; }
  read -r S1 N1 <<<"$(rd 1)"; read -r S2 N2 <<<"$(rd 2)"
  LR=$(losetup -f --show -o $((S2*512)) --sizelimit $((N2*512)) "$out"); e2fsck -fp "$LR" >/dev/null || true; resize2fs "$LR" >/dev/null 2>&1
  mkdir -p /mnt/$name; mount "$LR" /mnt/$name
  KV="$(cat /kernel/kver)"; mkdir -p /mnt/$name/lib/modules; rm -rf /mnt/$name/lib/modules/$KV; cp -a /kernel/modules /mnt/$name/lib/modules/$KV; depmod -b /mnt/$name "$KV"
  if [ -n "$seed" ] && [ -d "$seed" ]; then
    LB=$(losetup -f --show -o $((S1*512)) --sizelimit $((N1*512)) "$out"); mkdir -p /mnt/$name-boot; mount -t vfat "$LB" /mnt/$name-boot
    cp -r "$seed"/. /mnt/$name-boot/; sync; umount /mnt/$name-boot; losetup -d "$LB"
  fi
  sync; umount /mnt/$name; losetup -d "$LR"; echo "prepared $out" ;;
start)
  oct="$3"; hport="$4"; tap="tap$oct"
  ip link show $tap >/dev/null 2>&1 || { ip tuntap add dev $tap mode tap; ip link set $tap master $BR up; }
  nohup qemu-system-aarch64 -M virt -cpu cortex-a72 -smp 4 -m 2048 -display none \
    -kernel /kernel/vmlinuz -initrd /kernel/initrd.img -append "console=ttyAMA0 root=/dev/vda2 rootwait rw loglevel=4" \
    -drive file=$V/$name.img,if=none,id=d0,format=raw -device virtio-blk-pci,drive=d0 \
    -netdev tap,id=n0,ifname=$tap,script=no,downscript=no -device virtio-net-pci,netdev=n0,mac=$(mac "$oct"),romfile= \
    -serial file:$V/$name.log -monitor unix:$V/$name.mon,server,nowait > $V/$name.qemu.out 2>&1 &
  echo $! > $V/$name.pid
  nohup socat TCP-LISTEN:$hport,fork,reuseaddr TCP:$NET.$oct:8080 > /dev/null 2>&1 &
  echo $! > $V/$name.socat; echo "started $name ($NET.$oct, port $hport)" ;;
stop)
  [ -f $V/$name.pid ] && { kill "$(cat $V/$name.pid)" 2>/dev/null || true; sleep 1; }
  [ -f $V/$name.socat ] && kill "$(cat $V/$name.socat)" 2>/dev/null || true
  rd() { sfdisk -d "$V/$name.img" | sed -n "s|^$V/$name.img${1} *: *start= *\([0-9]*\), *size= *\([0-9]*\).*|\1 \2|p"; }
  read -r S2 N2 <<<"$(rd 2)"; LR=$(losetup -f --show -o $((S2*512)) --sizelimit $((N2*512)) "$V/$name.img"); e2fsck -fp "$LR" >/dev/null 2>&1 || true; losetup -d "$LR"; echo stopped ;;
cat)
  shift 2
  rd() { sfdisk -d "$V/$name.img" | sed -n "s|^$V/$name.img${1} *: *start= *\([0-9]*\), *size= *\([0-9]*\).*|\1 \2|p"; }
  read -r S2 N2 <<<"$(rd 2)"; LR=$(losetup -f --show -o $((S2*512)) --sizelimit $((N2*512)) "$V/$name.img"); mkdir -p /mnt/$name.cat; mount -o ro,noload "$LR" /mnt/$name.cat 2>/dev/null || mount -o ro "$LR" /mnt/$name.cat
  for p in "$@"; do echo "### $p"; if [ -d "/mnt/$name.cat$p" ]; then ls -la "/mnt/$name.cat$p"; else cat "/mnt/$name.cat$p" 2>&1; fi; done
  umount /mnt/$name.cat; losetup -d "$LR" ;;
log) cat $V/$name.log ;;
*) echo "usage: $0 setup|prep|start|stop|cat|log ..." >&2; exit 2 ;;
esac
