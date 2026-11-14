# Test image for the network helper end to end: plain Debian Bookworm (same base as Raspberry Pi OS) + what install-pi.sh and the helper need.
# NetworkManager itself is NOT used: a fake nmcli keeps profiles in a JSON file (see scripts/e2e_netcfg.py). Needs internet at run time (the real installer runs apt + pip).
FROM debian:bookworm
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 python3-venv python3-pip sudo iproute2 procps chrony git ca-certificates curl util-linux >/dev/null \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -s /bin/bash tt
