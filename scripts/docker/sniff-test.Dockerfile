# Test image for the sniffer: Debian Bookworm (same base as Raspberry Pi OS) with the capture tools the installer uses.
FROM debian:bookworm
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends debconf-utils >/dev/null \
 && echo "wireshark-common wireshark-common/install-setuid boolean true" | debconf-set-selections \
 && apt-get install -y -qq --no-install-recommends tshark tcpdump iproute2 iw ethtool socat python3 python3-venv python3-pip ca-certificates sudo libcap2-bin util-linux procps iputils-ping netcat-openbsd curl >/dev/null \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-dev.txt /tmp/
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install -q -r /tmp/requirements-dev.txt
RUN useradd -m -s /bin/bash tt && usermod -aG wireshark tt
