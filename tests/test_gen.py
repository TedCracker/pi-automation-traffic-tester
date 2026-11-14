"""Traffic generator: protocol, histogram, sequence tracking, node (loopback UDP/TCP), verdict, profiles, report."""
import socket
import tempfile
import time

import pytest

from traffic_tester import profiles as P
from traffic_tester.gen import node as G, proto as GP, run as GR


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def stream(i=0, type_="udp", rate=None, size=None, **kw):
    d = {"id": i, "name": f"s{i}", "type": type_, "rate": rate or {"pps": 500}, "size": size or {"mode": "fixed", "size": 300}, "burst_packets": 1,
         "burst_gap_ms": 0, "duration_s": 2, "rtt_probe_pps": 20, "dscp": 0, "ttl": 64, "dst_ip": "127.0.0.1", "dst_port": free_port(), "seed": 1}
    d.update(kw)
    return d


def run_node(streams, secs=None):
    n = G.Node()
    n.start(streams, streams, "lo")
    time.sleep((secs or max(s.get("duration_s") or 2 for s in streams)) + 0.9)
    return n.stop()


# ---------------------------------------------------------------- protocol
def test_header_roundtrip_and_size():
    b = bytearray(40)
    GP.pack_header(b, 8, GP.FLAG_ECHO_REQ, 7, 1000, 2 ** 40 + 5, 123456789012345)
    assert GP.HDR_LEN == 24
    assert GP.parse_header(b, 8) == (GP.FLAG_ECHO_REQ, 7, 1000, 2 ** 40 + 5, 123456789012345)
    assert GP.parse_header(bytearray(40), 0) is None


def test_size_tables_modes_and_clamping():
    assert GP.size_table("fixed", 1000, 0, 0, 1, "udp") == [1000]
    assert GP.size_table("fixed", 10, 0, 0, 1, "udp") == [66]                    # clamped: header + UDP/IP/Ethernet must fit
    assert GP.size_table("fixed", 10, 0, 0, 1, "l2") == [60]
    r = GP.size_table("range", 0, 100, 300, 5, "udp")
    assert min(r) >= 100 and max(r) <= 300 and len(set(r)) > 50 and r == GP.size_table("range", 0, 100, 300, 5, "udp")   # seeded = repeatable
    im = GP.size_table("imix", 0, 0, 0, 1, "l2")
    assert im.count(64) == 7 and im.count(594) == 4 and im.count(1514) == 1 and max(im) == GP.STD_MTU_FRAME


def test_l2_prefix_vlan():
    p = GP.l2_prefix(bytes(range(6)), bytes(6), 0x88B5, True, 100, 5)
    assert len(p) == 18 and p[12:14] == b"\x81\x00" and p[16:18] == b"\x88\xb5" and p[14] >> 5 == 5
    assert len(GP.l2_prefix(bytes(6), bytes(6), 0x88B5, False, 0, 0)) == 14


def test_histogram_accuracy_merge_roundtrip():
    import random
    rnd = random.Random(3)
    vals = [rnd.randint(50, 90000) for _ in range(30000)]
    h = GP.Hist()
    for v in vals:
        h.add(v)
    s = sorted(vals)
    for p in (50, 95, 99):
        exact = s[-(-len(s) * p // 100) - 1]
        assert abs(h.pct(p) - exact) / exact < 0.04
    assert h.pct(100) == max(vals) and h.stats_ms()["min"] == min(vals) / 1000.0
    h2 = GP.Hist.from_dict(h.to_dict())
    assert h2.pct(99) == h.pct(99) and h2.n == h.n
    m = GP.Hist(); m.merge(h); m.merge(h2)
    assert m.n == 2 * h.n and m.pct(50) == h.pct(50)
    assert GP.Hist().pct(50) is None and GP.Hist().stats_ms()["n"] == 0


def test_seq_tracker_loss_dup_reorder():
    t = GP.SeqTracker()
    for q in [0, 1, 2, 5, 3, 3, 6, 9]:
        t.see(q)
    assert t.count == 7 and t.dup == 1 and t.ooo == 1 and sorted(t.missing) == [4, 7, 8] and t.lost_so_far == 3
    t2 = GP.SeqTracker(); t2.see(3)
    assert sorted(t2.missing) == [0, 1, 2]                                        # first packet 3: 0..2 were lost
    u = GP.SeqTracker()
    assert [u.see(q) for q in [0, 1, 3, 2, 2, 4]] == [1, 1, 1, 2, 0, 1]             # new max / new max / new max / late / duplicate / new max
    t3 = GP.SeqTracker(); t3.CAP = 10; t3.see(0); t3.see(1000)
    assert t3.overflow == 999 and t3.lost_so_far == 999                           # huge gap is still counted as lost


# ---------------------------------------------------------------- node over loopback
def test_udp_rate_counts_no_loss_and_rtt():
    r = run_node([stream(rate={"pps": 1000}, size={"mode": "fixed", "size": 500}, duration_s=3)])
    tx, rx = r["tx"]["0"], r["rx"]["0"]
    assert 2850 <= tx["pkts"] <= 3050 and tx["achieved_pps"] == pytest.approx(1000, rel=0.05)
    assert rx["unique"] == tx["pkts"] and rx["dup"] == 0 and rx["ooo"] == 0 and rx["lost_gap"] == 0
    assert rx["bytes"] == tx["bytes"]                                             # whole-frame accounting on both ends
    assert GP.Hist.from_dict(tx["rtt"]).n >= 30                                   # ~20 echo probes/s answered
    assert r["errors"] == []


def test_mbps_rate_converts_with_frame_size():
    r = run_node([stream(rate={"mbps": 8}, size={"mode": "fixed", "size": 1000}, duration_s=3)])
    assert r["tx"]["0"]["achieved_mbps"] == pytest.approx(8, rel=0.05)


def test_packet_count_limit_and_burst_mode():
    r = run_node([stream(rate={"pps": 2000}, duration_s=None, packets=777)], secs=2)
    assert r["tx"]["0"]["pkts"] == 777 and r["rx"]["0"]["unique"] == 777
    b = run_node([stream(rate={"pps": 100}, burst_packets=25, burst_gap_ms=200, duration_s=2)])
    assert 250 <= b["tx"]["0"]["pkts"] <= 300 and b["tx"]["0"]["pkts"] % 25 == 0 and b["rx"]["0"]["unique"] == b["tx"]["0"]["pkts"]   # whole bursts only


def test_imix_and_range_sizes_on_the_wire():
    r = run_node([stream(0, rate={"pps": 2400}, size={"mode": "imix"}, duration_s=2), stream(1, rate={"pps": 1000}, size={"mode": "range", "min": 200, "max": 400}, duration_s=2)])
    t0, t1 = r["tx"]["0"], r["tx"]["1"]
    assert t0["bytes"] / t0["pkts"] == pytest.approx((7 * 66 + 4 * 594 + 1514) / 12, rel=0.02)      # UDP clamps 64 -> 66
    assert 290 <= t1["bytes"] / t1["pkts"] <= 310 and r["rx"]["1"]["bytes"] == t1["bytes"]


def test_several_streams_independent_counters():
    r = run_node([stream(0, rate={"pps": 300}), stream(1, rate={"pps": 900}, size={"mode": "fixed", "size": 700}), stream(2, rate={"pps": 100})])
    assert [r["rx"][str(i)]["unique"] == r["tx"][str(i)]["pkts"] for i in range(3)] == [True] * 3
    assert r["tx"]["1"]["pkts"] > 2.5 * r["tx"]["0"]["pkts"]


def test_tcp_stream_throughput_and_framing():
    s = stream(0, "tcp", rate={"mbps": 6}, size={"mode": "range", "min": 200, "max": 1400}, duration_s=3, rtt_probe_pps=0)
    r = run_node([s])
    tx, rx = r["tx"]["0"], r["rx"]["0"]
    assert tx["achieved_mbps"] == pytest.approx(6, rel=0.08) and rx["unique"] == tx["pkts"] and rx["bytes"] == tx["bytes"] and rx["lost_gap"] == 0


def test_start_errors_are_plain_and_leave_nothing_running():
    n = G.Node()
    with pytest.raises(G.GenError) as e:
        n.tx_start([stream(type_="tcp", dst_port=free_port())], "lo")                    # nobody listening
    assert "cannot connect" in str(e.value).lower() and not n.busy()
    s = stream()
    n.start([s], [s], "lo"); assert n.busy()
    with pytest.raises(G.GenError):
        n.tx_start([s], "lo")                                                           # already sending
    n.stop(); assert not n.busy()


def test_jitter_is_zero_for_steady_and_clock_independent():
    r = run_node([stream(rate={"pps": 400}, duration_s=2)])
    assert r["rx"]["0"]["jitter_us"] is not None and r["rx"]["0"]["jitter_us"] < 2000      # loopback, steady stream


# ---------------------------------------------------------------- verdict
def summary(**kw):
    x = {"name": "S", "loss_pct": 0.0, "jitter_ms": 1.0, "lat_ms": {"p50": 5, "p95": 8, "p99": 10, "max": 12}, "warnings": []}
    x.update(kw)
    return {"streams": [x], "totals": {"tx_pkts": 1000, "rx_pkts": 1000, "rx_mbps": 10.0}, "clocks": {"limit_ms": 2, "a_offset_ms": None, "b_offset_ms": 0.1}}


def test_verdict_each_criterion():
    rule = {"max_loss_pct": 0.1, "max_latency_ms": 50, "latency_percentile": 99, "max_jitter_ms": 5, "min_throughput_mbps": 5}
    assert GR.verdict(summary(), rule)["pass"]
    assert "lost" in GR.verdict(summary(loss_pct=2.0), rule)["reasons"][0]
    assert any("jitter" in r for r in GR.verdict(summary(jitter_ms=9), rule)["reasons"])
    assert any("within 80 ms" in r for r in GR.verdict(summary(lat_ms={"p50": 5, "p95": 8, "p99": 80, "max": 90}), rule)["reasons"])
    assert any("Throughput" in r for r in GR.verdict({**summary(), "totals": {"tx_pkts": 10, "rx_pkts": 10, "rx_mbps": 1.0}}, rule)["reasons"])
    assert not GR.verdict(summary(lat_ms={"p50": 5, "p95": 8, "p99": 10, "max": 80}), {**rule, "latency_percentile": 100})["pass"]


def test_verdict_never_passes_a_delay_limit_it_could_not_check():
    v = GR.verdict(summary(lat_ms=None), {"max_loss_pct": 1, "max_latency_ms": 50})
    assert not v["pass"] and "clocks are not synced" in v["reasons"][0]
    assert GR.verdict(summary(lat_ms=None), {"max_loss_pct": 1})["pass"]                    # no delay limit -> fine


def test_verdict_nothing_sent_or_received_and_rate_shortfall():
    assert not GR.verdict({"streams": [], "totals": {"tx_pkts": 0, "rx_pkts": 0, "rx_mbps": 0}, "clocks": {}}, {})["pass"]
    s = summary(); s["totals"]["rx_pkts"] = 0
    assert any("Nothing arrived" in r for r in GR.verdict(s, {})["reasons"])
    assert not GR.verdict(summary(warnings=["This Pi could only send 100 packets/s of the 5000 requested."]), {"max_loss_pct": 1})["pass"]


# ---------------------------------------------------------------- profiles / report
def test_generator_profiles_valid_and_validated():
    gens = [p for p in P.builtin_presets() if p["kind"] == "generator"]
    assert {p["name"] for p in gens} >= {"Mirrored-Bits-like small frames", "SCADA / DNP3 polling", "Video / CCTV stream", "Fill a 5G link", "IMIX (mixed packet sizes)", "Burst stress"}
    assert all(p["transport"] == "generator" and p["builtin"] for p in gens)
    for bad in ({"rate": {}}, {"rate": {"pps": 1, "mbps": 1}}, {"size": {"size": 5}}, {"burst_packets": 0}, {"dst_mac": "zz"}):
        with pytest.raises(ValueError):
            P.validate_profile({"name": "x", "kind": "generator", "gen": {"streams": [bad]}})
    with pytest.raises(ValueError):
        P.validate_profile({"name": "x", "kind": "generator", "gen": {"streams": []}})


def test_export_never_contains_the_peer_password():
    p = P.validate_profile({"name": "x", "kind": "generator", "gen": {"peer_password": "hunter2", "streams": [{}]}})
    doc = P.export_profiles([p])
    assert "hunter2" not in str(doc) and doc["profiles"][0]["gen"]["peer_password"] == ""


def test_store_generator_rows_headline_exports_and_pdf():
    from traffic_tester.store import Store
    from traffic_tester import report
    s = Store(tempfile.mkdtemp())
    prof = [p for p in P.builtin_presets() if p["kind"] == "generator"][3]
    rid = s.create_run(1, prof, "A", "http://10.0.0.2:8080")
    s.add_gen_intervals(rid, [{"t_offset_s": t, "stream": 0, "tx_pkts": 100, "tx_bytes": 140000, "rx_pkts": 100, "rx_bytes": 140000, "lost_cum": 0, "lat_avg_us": 900 + t, "lat_p95_us": 1500,
                               "lat_max_us": 2000, "jitter_us": 40.0, "rtt_avg_us": 1800} for t in range(10)])
    stream_ = {"id": 0, "name": "Bulk", "type": "udp", "direction": "a_to_b", "size": "1400 B", "tx": {"pkts": 1000, "bytes": 1400000, "pps": 100.0, "mbps": 1.1, "target_pps": 100, "err": 0, "duration_s": 10},
               "rx": {"pkts": 1000, "unique": 1000, "bytes": 1400000, "pps": 100.0, "mbps": 1.1, "dup": 0, "ooo": 0}, "lost": 0, "loss_pct": 0.0,
               "lat_ms": {"min": 0.8, "avg": 0.9, "p50": 0.9, "p95": 1.5, "p99": 1.9, "max": 2.0, "n": 1000}, "jitter_ms": 0.04, "jitter_max_ms": 0.1, "rtt_ms": {"avg": 1.8}, "warnings": []}
    s.finish_run(rid, "finished", {"kind": "generator", "streams": [stream_], "totals": {"tx_pkts": 1000, "rx_pkts": 1000, "lost": 0, "loss_pct": 0.0, "tx_mbps": 1.1, "rx_mbps": 1.1, "tx_pps": 100.0, "lat_ms": {"p99": 1.9, "max": 2.0}},
                                   "clocks": {"valid": True, "limit_ms": 2}, "duration_s": 10, "verdict": {"pass": True, "reasons": []}})
    run = s.list_runs(1)[0]
    assert run["verdict"] == "PASS" and run["headline"]["sent"] == 1000 and run["headline"]["p99_ms"] == 1.9 and run["headline"]["mbps"] == 1.1
    assert s.gen_csv(rid).splitlines()[0].startswith("t_s,stream,tx_pkts") and len(s.gen_csv(rid).splitlines()) == 11
    assert len(s.run_gen_intervals(rid)) == 10 and s.run_json(rid)["intervals"] == s.run_gen_intervals(rid)
    import io, zipfile
    z = zipfile.ZipFile(io.BytesIO(s.session_zip(1)))
    assert f"run-{rid}/generator-per-second.csv" in z.namelist()
    pdf = report.build_report(s, [rid], "Test report")
    assert pdf[:5] == b"%PDF-" and len(pdf) > 3000
    s.delete_run(rid); assert s.run_gen_intervals(rid) == []


def test_api_node_status_and_missing_peer(monkeypatch):
    from fastapi.testclient import TestClient
    from traffic_tester.server import create_app
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        st = c.get("/api/gen/node/status").json()
        assert st["busy"] is False and "interfaces" in st and "clock" in st and "hardware" in st
        gp = [p for p in c.get("/api/profiles").json() if p["kind"] == "generator"][0]
        gp.pop("id")
        r = c.post("/api/runs", json={"profile": gp})                                      # no peer address -> the run fails with a plain reason
        assert r.status_code == 200
        for _ in range(50):
            time.sleep(0.2)
            run = c.get(f"/api/runs/{r.json()['run_id']}").json()
            if run["status"] != "running":
                break
        assert run["status"] == "failed" and "web address" in run["summary"]["error"]
        assert c.get("/api/report.pdf?run_ids=%d" % run["id"]).content[:5] == b"%PDF-"
        assert c.post("/api/gen/node/rx/start", json={"streams": [{"id": 0, "type": "udp", "dst_port": 1}]}).status_code in (200, 422)
        c.post("/api/gen/node/stop")


def test_late_packets_count_for_delay_and_jitter_in_arrival_order():
    st = G.RxStat({"id": 0})
    # arrival order 0,2,1,3: transit times 10,30,20,10 ms -> every unique packet is in the delay histogram
    for seq, transit_ms in [(0, 10), (2, 30), (1, 20), (3, 10)]:
        st.on_packet(seq, 1_000_000_000, 1_000_000_000 + transit_ms * 1_000_000, 100, 1.0)
    st.on_packet(3, 1_000_000_000, 1_000_000_000 + 10_000_000, 100, 1.0)             # duplicate: ignored for delay/jitter
    r = st.result()
    assert r["unique"] == 4 and r["dup"] == 1 and r["ooo"] == 1 and GP.Hist.from_dict(r["lat"]).n == 4
    assert r["jitter_us"] is not None and r["jitter_us"] > 0
