#!/bin/bash
# CPU cost of the Mirrored Bits emulator on arm64 (QEMU, Debian Bookworm = Raspberry Pi OS userland): both nodes continuous at 9600 / 19200 / 38400 baud over a
# socat pty pair, in one process, plus a tiny benchmark run natively and under QEMU so the QEMU number can be scaled (QEMU user-mode is several times slower
# than a Pi 5, so the raw figure is an upper bound). Output -> docs/arm64/mirrored-cpu.log
exec 9>/tmp/tt-test.lock; flock 9
cd "$(dirname "$0")/.." || exit 1
mkdir -p docs/arm64
NAME=tt-arm64-mbcpu
docker rm -f $NAME >/dev/null 2>&1
cat > /tmp/mbcpu.py <<'PY'
import os, subprocess, sys, time
sys.path.insert(0, os.environ.get("TT_SRC", "/src"))
from traffic_tester.mbemu import Emulator
from traffic_tester.mbemu.engine import default_pair
from traffic_tester.mbemu.links import MBSerialLink

def bench():
    t = time.process_time(); x = 0
    for i in range(3_000_000): x = (x * 31 + i) & 0xFFFFFFFF
    return time.process_time() - t

def run(baud, secs=8):
    p = subprocess.Popen(["socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0"], stderr=subprocess.PIPE, text=True)
    devs = []
    while len(devs) < 2:
        ln = p.stderr.readline()
        if "PTY is" in ln: devs.append(ln.strip().split()[-1])
    time.sleep(0.3)
    sa, sb = default_pair({"baud": baud, "trip_every_s": 1.0}, {"baud": baud})
    a = Emulator(sa, MBSerialLink(devs[0], baud), "A", duration_s=secs); b = Emulator(sb, MBSerialLink(devs[1], baud), "B", duration_s=secs + 2)
    b.start(); time.sleep(0.5)
    w0 = time.time(); a.run(); wall = time.time() - w0
    cpu = a.cpu_s; st = a.counters(); st["rok_drops"] = a.ch.rok_drops; st["rtt_p50"] = a.rtt_stats()["p50"]; b.stop(); p.terminate()
    return wall, cpu, st

print("benchmark (3M-iteration python loop): %.2f s" % bench(), flush=True)
for baud in (9600, 19200, 38400, 115200):
    wall, cpu, st = run(baud)
    print(f"{baud:6d} baud both ways: wall {wall:.1f} s, CPU of node A's thread {cpu:.2f} s = {100*cpu/wall:.1f} % of one core (node B, in the same process, is about the same); A received {st['rx_good']} good, {st['rx_rejected']} rejected, ROK drops {st['rok_drops']}; echo RTT p50 {st['rtt_p50']} ms", flush=True)
PY
{
  echo "### native (this PC): $(uname -m)"
  TT_SRC="$PWD" python3 /tmp/mbcpu.py 2>&1 || true
  echo "### arm64 under QEMU (python3 from Debian Bookworm, as on Raspberry Pi OS)"
  docker run --rm --name $NAME --platform linux/arm64 -v "$PWD":/src:ro -v /tmp/mbcpu.py:/mbcpu.py:ro debian:bookworm bash -c \
    'apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends python3 python3-serial socat >/dev/null 2>&1; echo "uname -m: $(uname -m)"; PYTHONDONTWRITEBYTECODE=1 python3 /mbcpu.py'
} 2>&1 | tee docs/arm64/mirrored-cpu.log
