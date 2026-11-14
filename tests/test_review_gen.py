"""Regression tests for the traffic-generator review findings (gen/proto.py, net.py, node.py, run.py; docs/SPEC.md 13).

Everything runs on 127.0.0.1 with ports picked by the kernel. The "other Pi" is FakeRemote: a second Node in this process
that takes the place of run.RemoteNode (no HTTP)."""
import os
import re
import socket
import threading
import time

import pytest

from traffic_tester import profiles as P
from traffic_tester.gen import net as GN, node as G, proto as GP, run as GR

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port(kind=socket.SOCK_DGRAM):
    s = socket.socket(socket.AF_INET, kind); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def stream(i=0, type_="udp", rate=None, size=None, **kw):
    """A resolved stream as the node gets it (same shape as tests/test_gen.py)."""
    d = {"id": i, "name": f"s{i}", "type": type_, "rate": rate or {"pps": 500}, "size": size or {"mode": "fixed", "size": 300}, "burst_packets": 1,
         "burst_gap_ms": 0, "duration_s": 2, "rtt_probe_pps": 0, "dscp": 0, "ttl": 64, "dst_ip": "127.0.0.1", "dst_port": free_port(), "seed": 1}
    d.update(kw)
    return d


def run_node(streams, secs):
    n = G.Node()
    n.start(streams, streams, "lo")
    time.sleep(secs)
    return n.stop()


class FakeRemote:
    """Pi B without HTTP. `calls` is shared with the test so the order of calls on both Pis can be checked."""

    def __init__(self, calls=None):
        self.node = G.Node()
        self.calls = [] if calls is None else calls
        self.fail_live = 0          # this many next live() calls fail
        self.dead = False           # the Pi is gone: live() and stop() fail

    def status(self):
        return {"busy": self.node.busy(), "interfaces": {}, "default_iface": "lo", "clock": {"synced": True, "offset_ms": 0.0}, "hardware": "fake"}

    def rx_start(self, streams, iface):
        self.calls.append("b.rx_start")
        self.node.rx_start(streams, iface)

    def tx_start(self, streams, iface, start_in_s):
        self.calls.append("b.tx_start")
        self.node.tx_start(streams, iface, start_in_s)

    def live(self, since):
        if self.dead or self.fail_live > 0:
            self.fail_live -= 1
            raise GR.RemoteError("Cannot reach the other Pi at http://fake: timed out")
        return self.node.live(since)

    def stop(self):
        self.calls.append("b.stop")
        if self.dead:
            raise GR.RemoteError("Cannot reach the other Pi at http://fake: timed out")
        return self.node.stop()


def gen_profile(streams):
    for s in streams:
        s.setdefault("dst_port", free_port(socket.SOCK_STREAM if s.get("type") == "tcp" else socket.SOCK_DGRAM))
        s.setdefault("rtt_probe_pps", 0)
    return P.validate_profile({"name": "review", "kind": "generator", "net": {"peer_host": "127.0.0.1"},
                               "gen": {"streams": streams, "local_host": "127.0.0.1", "rule": {"max_loss_pct": 0}}})


@pytest.fixture(autouse=True)
def _same_host_clock(monkeypatch):
    monkeypatch.setenv("TT_ASSUME_SYNCED", "1")       # no chronyc child process in these tests


# ---------------------------------------------------------------- 1: Pi B freed when Pi A's own receiver cannot start
def test_f1_peer_is_stopped_when_this_pis_receiver_cannot_start():
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("0.0.0.0", 0)); occupied.listen(1)
    try:
        prof = gen_profile([{"name": "out", "type": "udp", "direction": "a_to_b", "rate": {"pps": 100}, "duration_s": 1},
                            {"name": "back", "type": "tcp", "direction": "b_to_a", "rate": {"pps": 100}, "duration_s": 1, "dst_port": occupied.getsockname()[1]}])
        a, b = G.Node(), FakeRemote()
        run = GR.GeneratorRun(prof, a, remote=b)
        summary = run.run()
        assert run.status == "failed" and "Cannot listen for tcp" in summary["error"]
        assert b.calls == ["b.rx_start", "b.stop"]                 # B had started listening and was told to stop
        assert not b.node.busy() and not a.busy()
        assert b.status()["busy"] is False                         # so the next run is not refused with "already running"
    finally:
        occupied.close()


# ---------------------------------------------------------------- 2: the run ends when Pi B disappears
def test_f2_run_ends_with_plain_error_when_other_pi_is_lost(monkeypatch):
    monkeypatch.setattr(GR.GeneratorRun, "LOST_GRACE_S", 1.0)      # default 10 s past the planned end; shortened to keep the test quick
    prof = gen_profile([{"name": "back", "type": "udp", "direction": "b_to_a", "rate": {"pps": 200}, "duration_s": 1}])
    a, b = G.Node(), FakeRemote()
    run = GR.GeneratorRun(prof, a, remote=b)
    started = threading.Event()
    orig_tx = b.tx_start

    def tx_then_die(*args):                                        # reachable for the start, then gone: live() and stop() fail
        orig_tx(*args)
        b.dead = True
        started.set()
    b.tx_start = tx_then_die
    t0 = time.monotonic()
    try:
        summary = run.run()
        took = time.monotonic() - t0
        assert started.is_set() and took < 8, took                 # before the fix this never returned
        assert run.status == "failed" and summary["error"] == "Lost contact with the other Pi during the test."
        assert not a.busy() and a.senders == [] and a.rxsockets == {}   # this Pi's receivers were still stopped
        assert "b.stop" in b.calls                                 # and a stop of the other Pi was at least tried
    finally:
        b.node.stop()


def test_f2_short_outage_of_the_control_link_does_not_end_the_run():
    prof = gen_profile([{"name": "back", "type": "udp", "direction": "b_to_a", "rate": {"pps": 200}, "duration_s": 2}])
    a, b = G.Node(), FakeRemote()
    b.fail_live = 6                                                # ~3 s without an answer, longer than the 2 s stream, then B is back
    run = GR.GeneratorRun(prof, a, remote=b)
    summary = run.run()
    assert run.status == "finished" and run.error is None
    assert summary["totals"]["tx_pkts"] > 300 and summary["totals"]["lost"] == 0 and summary["verdict"]["pass"]


# ---------------------------------------------------------------- 3: burst gap sets the real rate; bursts above one batch
def test_f3_burst_gap_target_is_burst_per_gap_not_the_rate():
    r = run_node([stream(rate={"pps": 1250}, burst_packets=10, burst_gap_ms=100, duration_s=2)], 2.6)["tx"]["0"]
    assert r["target_pps"] == 100.0                                # 10 packets every 100 ms; the 1250 pps rate is not used
    assert r["achieved_pps"] == pytest.approx(100, rel=0.08)
    assert r["achieved_pps"] >= 0.93 * r["target_pps"]             # run.py's "could only send" check no longer fires
    assert G.stream_pps(stream(rate={"pps": 1250}))[1] == 1250     # no gap: the rate is the target, as before
    assert G.stream_pps(stream(rate={"mbps": 8}, size={"mode": "fixed", "size": 1000}))[1] == pytest.approx(1000)


def test_f3_burst_larger_than_one_batch_is_sent_whole():
    r = run_node([stream(0, rate={"pps": 100}, burst_packets=200, burst_gap_ms=250, duration_s=1.1),
                  stream(1, rate={"pps": 800}, burst_packets=200, burst_gap_ms=0, duration_s=1.1)], 1.8)["tx"]
    for sid in ("0", "1"):                                         # bursts at 0, 0.25 .. 1.0 s: 5 x 200 (before the fix: 64 per burst)
        assert r[sid]["pkts"] % 200 == 0 and 800 <= r[sid]["pkts"] <= 1200, r[sid]


# ---------------------------------------------------------------- 4: one ticker, one row per second
def test_f4_failed_tx_start_leaves_no_second_ticker():
    def tickers():
        return {t for t in threading.enumerate() if t.name == "gen-tick" and t.is_alive()}
    before = tickers()
    n = G.Node()
    s = stream(rate={"pps": 200}, duration_s=2)
    n.rx_start([s], "lo")
    assert len(tickers() - before) == 1
    with pytest.raises(G.GenError):
        n.tx_start([stream(1, "tcp", dst_port=free_port(socket.SOCK_STREAM))], "lo")     # nobody listening: the start fails, receivers were open
    assert tickers() == before                                     # the ticker of the failed start is gone
    n.stop()
    time.sleep(0.3)                                                # (any other phase than the next run's ticker)
    n.start([s], [s], "lo")
    assert len(tickers() - before) == 1
    time.sleep(2.75)                                               # ticks at 1 s and 2 s, final row at ~2.75 s
    rows = n.stop()["rows"]
    keys = [(r["t"], r["dir"], r["stream"]) for r in rows]
    assert len(keys) == len(set(keys)) and len(keys) >= 4, sorted(keys)
    assert tickers() == before


# ---------------------------------------------------------------- 6: jumbo frames counted with their real length
BIG_LO = "127.0.0.2"       # not 127.0.0.1: WSL2 "mirrored" networking routes that one through a 1500-byte device that drops fragments


def test_f6_jumbo_frames_are_counted_whole_by_the_receiver():
    r = run_node([stream(rate={"pps": 200}, size={"mode": "fixed", "size": 9000}, duration_s=1, dst_ip=BIG_LO)], 1.6)
    tx, rx = r["tx"]["0"], r["rx"]["0"]
    assert tx["pkts"] > 150 and tx["bytes"] == 9000 * tx["pkts"]
    assert rx["unique"] == tx["pkts"] and rx["bytes"] == tx["bytes"]         # before the fix: 2090 bytes per frame (2048-byte slot)


def test_f6_batchrx_reports_real_length_beyond_the_slot():
    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); a.bind((BIG_LO, 0))
    b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        b.sendto(b"\x01" * 5000, a.getsockname()); b.sendto(b"\x02" * 100, a.getsockname())
        time.sleep(0.05)
        rx = GN.BatchRx(4, 2048)
        assert rx.recv(a.fileno()) == 2
        assert rx.length(0) == 5000 and rx.length(1) == 100
        assert rx.mv[2047] == 1 and rx.mv[2048] == 2 and rx.mv[2048 + 100] == 0          # stored bytes stay inside each slot
    finally:
        a.close(); b.close()


# ---------------------------------------------------------------- 7: interface "auto" does not stick
def test_f7_auto_interface_is_replaced_by_a_named_port():
    n = G.Node()
    try:
        n.rx_start([stream()], "auto")                             # UDP only: no port needed yet
        assert n.iface == "auto"
        assert n._begin("lo") == "lo"                              # tx_start with a raw stream resolves the port: it must be used
        assert n._begin("eth9") == "lo"                            # a named port stays for the rest of the run
    finally:
        n.stop()


# ---------------------------------------------------------------- 8: user Stop stops the senders before the receivers
def test_f8_user_stop_stops_senders_first_and_counts_no_loss():
    prof = gen_profile([{"name": "out", "type": "udp", "direction": "a_to_b", "rate": {"pps": 2000}, "duration_s": 30},
                        {"name": "back", "type": "udp", "direction": "b_to_a", "rate": {"pps": 2000}, "duration_s": 30}])
    calls = []
    a, b = G.Node(), FakeRemote(calls)
    stop_senders, stop_all = a.stop_senders, a.stop
    a.stop_senders = lambda: (calls.append("a.stop_senders"), stop_senders())[1]
    a.stop = lambda: (calls.append("a.stop"), stop_all())[1]
    run = GR.GeneratorRun(prof, a, remote=b)
    out = {}
    t = threading.Thread(target=lambda: out.update(s=run.run()))
    t.start()
    time.sleep(2.0)
    run.stop()
    t.join(15)
    assert not t.is_alive() and run.status == "stopped"
    assert calls == ["b.rx_start", "b.tx_start", "a.stop_senders", "b.stop", "a.stop"]   # A's senders, then B (senders + receivers), then A's receivers
    s = out["s"]
    assert s["stopped_early"] and s["totals"]["tx_pkts"] > 2000
    assert [x["lost"] for x in s["streams"]] == [0, 0] and s["totals"]["lost"] == 0


# ---------------------------------------------------------------- 10: packet-count streams have a real planned time
def test_f10_packet_count_stream_total_time_is_packets_over_rate():
    prof = gen_profile([{"name": "count", "type": "udp", "direction": "a_to_b", "rate": {"pps": 600}, "packets": 300, "start_offset_s": 0.5}])
    run = GR.GeneratorRun(prof, G.Node(), remote=FakeRemote())
    summary = run.run()
    assert run.status == "finished" and run.total_s == pytest.approx(1.0)      # 0.5 s offset + 300 / 600 pps (was 3600.5)
    assert summary["duration_s"] == 1.0 and summary["totals"]["tx_pkts"] == 300 and summary["totals"]["lost"] == 0


# ---------------------------------------------------------------- 11: SPEC section 13 matches the code
def test_f11_spec_generator_section_matches_the_code():
    from traffic_tester.server import create_app
    import tempfile
    text = open(os.path.join(ROOT, "docs", "SPEC.md"), encoding="utf-8").read()
    sec = text[text.index("## 13. Traffic generator"):text.index("## 14.")]
    in_spec = {p for p in re.findall(r"/api/gen/node/[a-z/]+", sec) if not p.endswith("/")}
    in_code = {r.path for r in create_app(tempfile.mkdtemp(), "A").routes if getattr(r, "path", "").startswith("/api/gen/node/")}
    assert in_spec == in_code, (in_spec, in_code)
    assert "start|stop" not in sec and "`both`" not in sec and "62 B" not in sec and "with a pattern" not in sec
    assert GP.min_frame("udp") == 66 and "66 B for UDP" in sec and GP.min_frame("l2") == GP.min_frame("ip") == 60
    with pytest.raises(ValueError):
        P.validate_profile({"name": "x", "kind": "generator", "gen": {"streams": [{"direction": "both"}]}})
    buf = GN.BatchTx(1, 200).bufs[0]                                # padding really is zeros
    GP.pack_header(buf, 0, 0, 1, 200, 7, 123)
    assert bytes(buf[GP.HDR_LEN:]) == bytes(200 - GP.HDR_LEN)


# ---------------------------------------------------------------- 12: dead code stays removed
def test_f12_removed_dead_names_are_gone():
    assert not hasattr(GP, "STREAM_TYPES") and not hasattr(GN.BatchRx, "src_sockaddr")
    st = G.RxStat({"id": 0})
    st.on_packet(0, 2_000_000_000, 1_000_000_000, 100, 1.0)        # clock behind: delay is clamped to 0, not negative
    res = st.result()
    assert not {"neg_latency", "jitter_last_us", "max_seq"} & set(res)
    assert res["unique"] == 1 and GP.Hist.from_dict(res["lat"]).max == 0
