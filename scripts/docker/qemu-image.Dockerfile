# Test bench for the card image: QEMU (aarch64 "virt", TCG) + a generic Debian arm64 kernel/initrd that can mount the card's root file system + a bridge with DHCP and NAT.
#   docker build --platform linux/amd64 -t tt-qemu-image -f scripts/docker/qemu-image.Dockerfile scripts/docker
# The Raspberry Pi kernel itself cannot run on the QEMU "virt" machine (no Pi hardware), so the VM boots the card's userspace (systemd, NetworkManager, avahi,
# cloud-init, our service and helper units) with Debian's cloud arm64 kernel. What this cannot show (radio, Pi boot firmware, the stock first-boot resize) stays "needs a real Pi".
FROM --platform=linux/arm64 debian:trixie AS kernel
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends initramfs-tools linux-image-cloud-arm64 >/dev/null \
 && mkdir -p /out && cp /boot/vmlinuz-* /out/vmlinuz && cp /boot/initrd.img-* /out/initrd.img && ls /lib/modules | head -1 > /out/kver && cp -a /lib/modules/$(cat /out/kver) /out/modules

FROM --platform=linux/amd64 debian:trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends qemu-system-arm qemu-utils dnsmasq iproute2 iptables socat e2fsprogs fdisk dosfstools kmod mount xz-utils ca-certificates curl procps >/dev/null \
 && rm -rf /var/lib/apt/lists/*
COPY --from=kernel /out /kernel
