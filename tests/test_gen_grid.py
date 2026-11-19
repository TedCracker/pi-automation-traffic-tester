"""Phase 2 (generator grid): per-stream live numbers, a stream switched off and on during the run, Clear, run until stopped, switched-off streams in a profile."""
import socket
import threading
import time

import pytest

from traffic_tester import profiles as P
from traffic_tester.gen import node as G, run as GR


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setenv("TT_ASSUME_SYNCED", "1")


def prof(streams, **gen):
    for s in streams:
        s.setdefault("dst_port", free_port()); s.setdefault("rtt_probe_pps", 0)
    return P.validate_profile({"name": "grid", "kind": "generator", "net": {"peer_host": "127.0.0.1"},
                               "gen": {"streams": streams, "local_host": "127.0.0.1", "rule": {}, **gen}})


def two_streams(**kw):
    return [{"name": "a", "type": "udp", "direction": "a_to_b", "rate": {"pps": 200}, **kw}, {"name": "b", "type": "udp", "direction": "a_to_b", "rate": {"pps": 200}, **kw}]


class Fake:
    """The other Pi without HTTP."""
    def __init__(self):
        self.node = G.Node()

    def status(self):
        return {"busy": self.node.busy(), "interfaces": {}, "default_iface": "lo", "clock": {"synced": True, "offset_ms": 0.0}, "hardware": "fake"}

    def rx_start(self, s, i): self.node.rx_start(s, i)
    def tx_start(self, s, i, t): self.node.tx_start(s, i, t)
    def live(self, since): return self.node.live(since)
    def stop(self): return self.node.stop()
    def stop_senders(self): self.node.stop_senders()
    def set_stream(self, sid, on): self.node.set_stream_on(sid, on)


def run_in_thread(profile, events):
    run = GR.GeneratorRun(profile, G.Node(), emit=events.append, remote=Fake())
    t = threading.Thread(target=lambda: setattr(run, "result", run.run()), daemon=True); t.start()
    return run, t


def wait_for(cond, secs=12):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def last(events):
    g = [e for e in events if e.get("ev") == "gstate" and e.get("streams_now")]
    return g[-1] if g else None


def test_disabled_stream_is_kept_in_the_profile_but_not_sent():
    p = prof(two_streams(duration_s=1))
    p["gen"]["streams"][1]["enabled"] = False
    ev = []
    run = GR.GeneratorRun(p, G.Node(), emit=ev.append, remote=Fake())
    s = run.run()
    assert run.status == "finished", s.get("error")
    assert [x["name"] for x in s["streams"]] == ["a"]
    p2 = prof(two_streams(duration_s=1))
    for x in p2["gen"]["streams"]:
        x["enabled"] = False
    r2 = GR.GeneratorRun(p2, G.Node(), remote=Fake())
    assert "switch it on" in r2.run()["error"]


def test_old_profiles_default_to_on():
    assert P.validate_profile({"name": "x", "kind": "generator", "gen": {"streams": [{"name": "s"}]}})["gen"]["streams"][0]["enabled"] is True


def test_live_numbers_per_stream_and_off_on_and_clear():
    ev = []
    run, t = run_in_thread(prof(two_streams(), duration_s=0), ev)         # 0 = until stopped
    try:
        assert wait_for(lambda: (last(ev) or {}).get("sum", {}).get("rx_pkts", 0) > 100), "no per-stream numbers"
        e = last(ev)
        assert e["until_stopped"] is True and e["total_s"] == 0
        assert [x["i"] for x in e["streams_now"]] == [0, 1] and all(x["on"] and x["rx_pkts"] > 0 for x in e["streams_now"])
        # switch stream 2 off: its sent counter stops, the other goes on
        run.set_stream(1, False)
        time.sleep(1.5)
        a = last(ev)["streams_now"][1]["tx_pkts"], last(ev)["streams_now"][0]["tx_pkts"]
        time.sleep(1.5)
        b = last(ev)["streams_now"][1]["tx_pkts"], last(ev)["streams_now"][0]["tx_pkts"]
        assert b[0] == a[0] and b[1] > a[1] and last(ev)["streams_now"][1]["on"] is False
        run.set_stream(1, True)
        assert wait_for(lambda: last(ev)["streams_now"][1]["tx_pkts"] > b[0] + 50)
        # Clear zeroes the screen numbers, not the run
        before = last(ev)["sum"]["rx_pkts"]
        run.clear()
        assert wait_for(lambda: last(ev)["sum"]["rx_pkts"] < before / 2)
        assert last(ev)["cleared_s"] is not None and last(ev)["rx_pkts"] > before      # the run's own totals keep counting
    finally:
        run.stop(); t.join(15)
    assert run.status == "stopped" and not t.is_alive()


def test_until_stopped_result_shows_how_long_it_really_ran():
    ev = []
    run, t = run_in_thread(prof(two_streams(), duration_s=0), ev)
    time.sleep(2.5)
    run.stop(); t.join(15)
    assert run.status == "stopped" and 2.0 < run.result["duration_s"] < 6.0, run.result.get("duration_s")


def test_remote_stream_switch_goes_to_the_other_pi():
    p = prof([{"name": "back", "type": "udp", "direction": "b_to_a", "rate": {"pps": 200}}], duration_s=0)
    b = Fake(); ev = []
    run = GR.GeneratorRun(p, G.Node(), emit=ev.append, remote=b)
    t = threading.Thread(target=run.run, daemon=True); t.start()
    try:
        assert wait_for(lambda: (last(ev) or {}).get("sum", {}).get("rx_pkts", 0) > 50)
        run.set_stream(0, False); time.sleep(1.5)
        n1 = last(ev)["streams_now"][0]["tx_pkts"]; time.sleep(1.5)
        assert last(ev)["streams_now"][0]["tx_pkts"] == n1
    finally:
        run.stop(); t.join(15); b.node.stop()
