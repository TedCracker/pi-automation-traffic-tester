# Test image for the "no fixed roles" e2e: Debian Bookworm (same base as Raspberry Pi OS) with the app's Python dependencies and socat (serial "cables").
FROM debian:bookworm
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 python3-venv python3-pip socat iproute2 procps iputils-ping curl ca-certificates tar >/dev/null \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install -q -r /tmp/requirements.txt
RUN useradd -m -s /bin/bash tt && mkdir -p /data && chown tt /data
