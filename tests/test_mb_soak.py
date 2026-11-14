"""Mirrored Bits over real byte paths: socat pty pairs at every baud rate, both ways, continuously (MB_SOAK_S seconds, default 60 as the task
asks); UDP and TCP serial-over-IP on loopback, including a TCP reconnect; exact counts after injected corruption / a cut."""
import os
import socket
import threading
import time

import pytest

from mbfix import Chan, Socat, run_pair, wait_for
from traffic_tester.mbemu import Emulator, Settings, get_codec
from traffic_tester.mbemu.engine import default_pair
from traffic_tester.mbemu.links import MBSerialLink, TcpLink, UdpLink

BAUDS = [2400, 4800, 9600, 19200, 38400, 115200]
SOAK_S = float(os.environ.get("MB_SOAK_S", "60"))


def free_port(kind=socket.SOCK_DGRAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


WORKER = r"""
import json, sys, time
sys.path.insert(0, %(root)r); sys.path.insert(0, %(tests)r)
from mbfix import Socat
from mbemu_worker import soak
print(json.dumps(soak(int(sys.argv[1]), float(sys.argv[2]))))
"""


def _run_baud_in_its_own_process(baud, seconds=None):
    import subprocess, sys, json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = WORKER % {"root": root, "tests": os.path.join(root, "tests")}
    seconds = seconds or SOAK_S
    r = subprocess.run([sys.executable, "-c", code, str(baud), str(seconds)], capture_output=True, text=True, timeout=seconds + 60)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _check_baud(r, b):
    assert r["ok_a"] is not None and r["ok_b"] is not None, (b, "all-ones did not cross", r)
    for who in ("a", "b"):
        c = r[who]
        assert c["rx_rejected"] == 0 and c["rok_drops"] == 0 and c["rbad_events"] == 0 and c["cbad_events"] == 0, (b, who, c)
        assert r[who + "_avail"] == 100.0, (b, who, r)
        assert c["rx_good"] > 0.8 * r["seconds"] * 1e9 / r["nominal_ns"], (b, who, c["rx_good"], r["nominal_ns"])


@pytest.mark.parametrize("baud", BAUDS)
def test_every_baud_both_ways_over_a_pty_pair_short(baud):
    """Quick version for every run: 10 s per baud, one at a time (the host must be quiet: a Python process on a loaded machine stalls for a few ms now
    and then, which a relay-grade timing check rightly sees as a late message)."""
    r = _run_baud_in_its_own_process(baud, 10.0)
    _check_baud(r, baud)


@pytest.mark.skipif(not os.environ.get("MB_SOAK"), reason="full soak: MB_SOAK=1 (6 baud rates x MB_SOAK_S seconds, sequential, default 60 s each)")
def test_every_baud_continuous_both_ways_for_a_minute_each():
    """The full soak (task: >= 60 s at each baud, both ways): no message rejected, ROK never drops, all 8 bits cross both ways (0xFF too: PARMRK doubling)."""
    res = {}
    for b in BAUDS:
        res[b] = _run_baud_in_its_own_process(b, SOAK_S)
        _check_baud(res[b], b)
    print("SOAK", res)
    open("/tmp/mb-soak-report.txt", "w").write(repr(res))


def test_exact_counts_after_injected_corruption_and_a_cut():
    chan = Chan("socat", 19200)
    la, lb = chan.links()
    codec = get_codec()
    left = {"n": 0}
    real = la.send

    def send(data):
        if left["n"] > 0:
            left["n"] -= 1
            data = codec.corrupt_copy(data, 0, 0)
        return real(data)
    la.send = send

    def corrupt5(a, b):
        wait_for(lambda: b.ch.rok == 1)
        left["n"] = 5

    def cut(a, b):
        a.set_impairment({"break_link": True})
        time.sleep(0.3)
        a.set_impairment({"break_link": False})
    try:
        ea, eb = run_pair(chan, 4.0, links=(la, lb), b={"rok_n": 2}, script=[(0.8, corrupt5), (2.0, cut)])
    finally:
        chan.close()
    assert eb.ch.rejects["consistency"] == 5
    st = eb.ch.stats(eb.mono())
    assert eb.ch.rok_drops == 2, st        # 5 consecutive corrupted messages = ONE drop (ROK is already 0 after the first); the 300 ms cut = the second
    assert eb.ch.rejects["missing"] == 1 and eb.ch.rejects["timing"] == 0 and eb.ch.rok == 1
    assert 250 <= st["longest_outage_ms"] <= 450
    rok = [x for x in eb.events if x[2] == "ROK"]
    assert [x[3] for x in rok] == [1, 0, 1, 0, 1]


def test_udp_serial_over_ip_both_ways_with_a_cut_and_the_echo():
    port = free_port()
    la, lb = UdpLink("A", "127.0.0.1", port), UdpLink("B", "", port, bind_host="127.0.0.1")
    chan = Chan("internal", 19200)

    def cut(a, b):
        wait_for(lambda: a.ch.rok == 1 and b.ch.rok == 1)
        a.set_tmb(2, 1)
        wait_for(lambda: b.rx.state[2] == 1)
        a.set_impairment({"break_link": True})
        b.set_impairment({"break_link": True})
        time.sleep(0.5)
        a.set_impairment({"break_link": False})
        b.set_impairment({"break_link": False})
    ea, eb = run_pair(chan, 4.0, links=(la, lb), script=[(0.6, cut)])
    assert ea.ch.rok_drops == 1 and eb.ch.rok_drops == 1 and ea.ch.rok == 1 and eb.ch.rok == 1
    assert eb.rx.state[2] == 1 and ea.rtt_n > 50 and ea.rtt_alarm_events <= 2
    assert ea.ch.rejects["framing"] == 0 and eb.ch.rejects["consistency"] == 0


def test_tcp_serial_over_ip_both_ways_with_reconnect():
    port = free_port(socket.SOCK_STREAM)
    la, lb = TcpLink("A", "127.0.0.1", port), TcpLink("B", "", port, bind_host="127.0.0.1")
    chan = Chan("internal", 19200)

    def drop(a, b):
        wait_for(lambda: a.ch.rok == 1 and b.ch.rok == 1)
        la._drop()                                       # the TCP session is cut; A reconnects by itself (generation bumps -> framer reset)
        la.last_attempt = time.monotonic() + 0.3         # ... after a pause of ~0.5 s
    ea, eb = run_pair(chan, 5.0, links=(la, lb), script=[(1.0, drop)])
    assert la.sessions >= 2 and ea.ch.rok == 1 and eb.ch.rok == 1
    assert ea.ch.rok_drops >= 1 and eb.ch.rok_drops >= 1
    assert ea.ch.msgs_good > 100 and eb.ch.msgs_good > 100
    assert ea.ch.rejects["address"] == 0 and eb.ch.rejects["address"] == 0


def test_tcp_sends_every_message_at_once_nodelay():
    port = free_port(socket.SOCK_STREAM)
    la, lb = TcpLink("A", "127.0.0.1", port), TcpLink("B", "", port, bind_host="127.0.0.1")
    lb.open()
    la.open()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not (la.connected and lb.recv(0.01) is not None and lb.connected):
        time.sleep(0.01)
    assert la.conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1 and lb.conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1
    t0 = time.monotonic()
    for i in range(20):
        la.send(bytes([i] * 4))
    got = b""
    while len(got) < 80 and time.monotonic() - t0 < 1.0:
        for d, _, _ in lb.recv(0.05):
            got += d
    assert len(got) == 80 and time.monotonic() - t0 < 0.5          # no Nagle batching / delayed-ack waits
    la.close()
    lb.close()
