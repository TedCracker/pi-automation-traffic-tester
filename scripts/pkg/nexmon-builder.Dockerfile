# arm64 Debian toolchain to build kernel modules for Raspberry Pi OS (Pi 5). The kernel headers are fetched per kernel by nexmon-inner-build.sh.
#   docker build --platform linux/arm64 -t tt-nexmon-builder        -f scripts/pkg/nexmon-builder.Dockerfile scripts/pkg                      (Bookworm, gcc-12)
#   docker build --platform linux/arm64 -t tt-nexmon-builder-trixie --build-arg DIST=trixie -f scripts/pkg/nexmon-builder.Dockerfile scripts/pkg   (Trixie, gcc-14: newer Pi kernels are built with it)
ARG DIST=bookworm
FROM debian:${DIST}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates build-essential bc kmod patch libssl-dev flex bison pahole python3 libelf-dev >/dev/null \
 && rm -rf /var/lib/apt/lists/*
