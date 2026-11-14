"""Engine tests over UDP loopback (the serial/TCP/netem paths are covered by scripts/e2e_local.py)."""
import os, socket
import pytest
from traffic_tester import engine, profiles as P
from traffic_tester.transports import UdpTransport


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def run_test(delay_ms=0, drop_pct=0, **kw):
    port = free_port()
    resp = engine.Responder(UdpTransport("responder", "", port), delay_ms, drop_pct, hooks_enabled=True)
    resp.start()
    prof = P.validate_profile({"name": "t", "transport": "udp", "frame": "extended", "interval_ms": 20, "duration_s": 2, "timeout_ms": 400, **kw})
    t = engine.Tester(prof, UdpTransport("tester", "127.0.0.1", port))
    try:
        return t.run(), t
    finally:
        resp.stop()


def test_clean_run_passes():
    s, t = run_test()
    assert t.status == "finished" and 95 <= s["sent"] <= 101 and s["lost"] == 0 and s["verdict"]["pass"]


def test_delay_marks_late_and_fails():
    s, t = run_test(delay_ms=80)
    assert s["over_threshold"] >= 0.9 * s["sent"] and not s["verdict"]["pass"] and s["rtt_ms"]["p50"] >= 80


def test_drops_counted_as_lost():
    s, t = run_test(drop_pct=30, duration_s=3)
    assert 10 <= s["loss_pct"] <= 50 and not s["verdict"]["pass"]


def test_idle_probe_stats():
    s, t = run_test(idle_gaps_s=[0.5], idle_repeats=2, duration_s=1)
    assert s["idle"]["count"] == 2 and s["idle"]["lost"] == 0 and s["idle"]["gaps"][0]["gap_s"] == 0.5


def test_no_responder_fails_plainly():
    prof = P.validate_profile({"name": "t", "transport": "udp", "frame": "extended", "duration_s": 1})
    t = engine.Tester(prof, UdpTransport("tester", "127.0.0.1", free_port()))
    t.run()
    assert t.status == "failed" and "did not answer" in t.error


def test_plan_steps():
    p = P.validate_profile({"name": "t", "duration_s": 100, "idle_gaps_s": [5, 30], "idle_repeats": 2})
    assert engine.plan_steps(p) == [("steady", 100), ("idle", 5), ("idle", 5), ("idle", 30), ("idle", 30)]
    p["idle_mode"] = "interleaved"
    st = engine.plan_steps(p)
    assert sum(v for k, v in st if k == "steady") == pytest.approx(100) and [v for k, v in st if k == "idle"] == [5, 5, 30, 30]
