"""Throughput in steps (docs/SPEC.md 13.9, docs/COMPETITIVE-UI-REVIEW.md M4): the same traffic at 1/8, 1/4, 1/2, 3/4 and all of a top rate;
a step is clean when its loss and delay are inside the test's pass limits; the result is "Clean up to N Mb/s".

Pure logic is tested with made-up numbers; the real runs use 127.0.0.1 and a second Node in this process as the other Pi (no HTTP).
A lossy link is made with the receiver-side test hook TT_GEN_TEST_DROP (only honoured with TT_TEST_HOOKS=1)."""
import os
import socket
import threading
import time

import pytest

from traffic_tester import profiles as P
from traffic_tester.gen import node as G, run as GR

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port(kind=socket.SOCK_DGRAM):
    s = socket.socket(socket.AF_INET, kind); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class FakeRemote:
    """Pi B without HTTP: a second generator node in this process."""

    def __init__(self, caps=True):
        self.node = G.Node()
        self.calls = []
        self.caps = caps

    def status(self):
        d = {"busy": self.node.busy(), "interfaces": {}, "default_iface": "lo", "clock": {"synced": True, "offset_ms": 0.0}, "hardware": "fake", "version": "1.3.2"}
        if self.caps:
            d["caps"] = {"steps": True}
        return d

    def rx_start(self, streams, iface):
        self.calls.append(("rx", [s["id"] for s in streams]))
        self.node.rx_start(streams, iface)

    def tx_start(self, streams, iface, start_in_s):
        self.calls.append(("tx", [s["id"] for s in streams]))
        self.node.tx_start(streams, iface, start_in_s)

    def live(self, since):
        return self.node.live(since)

    def stop_senders(self):
        self.node.stop_senders()

    def stop(self):
        self.calls.append(("stop", []))
        return self.node.stop()


def step_profile(steps=None, rule=None, streams=None, **gen):
    streams = streams or [{"name": "Throughput", "type": "udp", "rate": {"mbps": 10}, "size": {"mode": "fixed", "size": 500}, "rtt_probe_pps": 0}]
    for s in streams:
        s.setdefault("dst_port", free_port(socket.SOCK_STREAM if s.get("type") == "tcp" else socket.SOCK_DGRAM))
    st = {"enabled": True, "top_mbps": 4, "step_s": 1, "flood_s": 1}
    st.update(steps or {})
    return P.validate_profile({"name": "steps", "kind": "generator", "net": {"peer_host": "127.0.0.1"},
                               "gen": {"streams": streams, "local_host": "127.0.0.1", "steps": st, "rule": rule or {"max_loss_pct": 0.5}, **gen}})


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    from traffic_tester import system
    monkeypatch.setattr(system, "clock_state", lambda *a, **k: {"synced": True, "offset_ms": 0.0, "source": "test"})
    monkeypatch.setattr(GR.GeneratorRun, "SETTLE_S", 0.4)
    monkeypatch.setenv("TT_TEST_HOOKS", "1")
    monkeypatch.delenv("TT_GEN_TEST_DROP", raising=False)


def go(prof, remote=None, events=None):
    a = G.Node()
    b = remote or FakeRemote()
    run = GR.GeneratorRun(prof, a, emit=(events.append if events is not None else (lambda e: None)), remote=b)
    s = run.run()
    assert not a.busy() and not b.node.busy()
    return run, s, b


# ---------------------------------------------------------------- settings
def test_profile_defaults_old_profiles_and_validation():
    old = P.validate_profile({"name": "old", "kind": "generator", "gen": {"streams": [{"name": "s"}]}})
    assert old["gen"]["steps"]["enabled"] is False                                   # a test saved before steps existed runs as before (one rate, Flood / fixed)
    assert old["gen"]["rule"]["min_clean_mbps"] is None
    st = step_profile({"top_mbps": None})["gen"]["steps"]
    assert st["fractions"] == [0.125, 0.25, 0.5, 0.75, 1.0] and st["step_s"] == 1 and st["stop_after_fail"] == 2 and st["top_mbps"] is None
    assert P.GenSteps().step_s == 10 and P.GenSteps().flood_s == 5                    # the defaults Alex approved: 5 s flood, 5 steps of 10 s
    with pytest.raises(Exception, match="rising shares"):
        step_profile({"fractions": [0.5, 0.25]})
    with pytest.raises(Exception, match="steady rate"):
        step_profile(streams=[{"name": "b", "burst_packets": 10, "burst_gap_ms": 20}])
    with pytest.raises(Exception, match="stop after"):
        step_profile(streams=[{"name": "b", "packets": 100}])
    names = [p["name"] for p in P.generator_presets()]
    thr = next(p for p in P.generator_presets() if p["name"].startswith("Throughput"))
    assert thr["gen"]["steps"]["enabled"] and thr["gen"]["steps"]["top_mbps"] is None    # the Throughput preset: Steps is the default method, top rate Auto
    assert sum(1 for p in P.generator_presets() if p["gen"]["steps"]["enabled"]) == 1, names


# ---------------------------------------------------------------- the judge (pure)
def _got(tx=1000, rx=1000, delay=None, name="s", dur=10, size=1000):
    return {"name": name, "src": "pi1", "tx_pkts": tx, "tx_bytes": tx * size, "rx_pkts": rx, "rx_bytes": rx * size, "delay_ms": delay, "jitter_ms": None, "dur": dur}


def test_step_is_clean_only_inside_the_loss_and_delay_limits():
    ph = {"n": 3, "of": 5, "share": 0.5, "asked_mbps": 0.8}
    rule = {"max_loss_pct": 0.1, "max_latency_ms": 60, "latency_percentile": 99}
    ok = GR.step_row(ph, [_got(1000, 1000, 12.0)], rule, True)
    assert ok["clean"] and ok["why"] == "" and ok["lost"] == 0 and ok["delay_ms"] == 12.0 and ok["delay_pct"] == 99
    assert ok["sent_mbps"] == pytest.approx(0.8) and ok["got_mbps"] == pytest.approx(0.8) and ok["clean_mbps"] == 0.8
    edge = GR.step_row(ph, [_got(1000, 999, 60.0)], rule, True)                      # 0.1 % lost, 60 ms: exactly on both limits = inside
    assert edge["clean"] and edge["loss_pct"] == 0.1
    lossy = GR.step_row(ph, [_got(1000, 990, 12.0)], rule, True)
    assert not lossy["clean"] and "1 % lost (limit 0.1 %)" in lossy["why"]
    slow = GR.step_row(ph, [_got(1000, 1000, 84.0)], rule, True)
    assert not slow["clean"] and "p99 delay 84 ms (limit 60 ms)" in slow["why"]
    unsynced = GR.step_row(ph, [_got(1000, 1000, 84.0)], rule, False)                # clocks apart: the delay cannot be judged, and is not shown
    assert unsynced["clean"] and unsynced["delay_ms"] is None
    dead = GR.step_row(ph, [_got(1000, 0)], {"max_loss_pct": None}, True)
    assert not dead["clean"] and "nothing arrived" in dead["why"]                    # even with no loss limit, a dead link is not clean
    two = GR.step_row(ph, [_got(1000, 1000, name="bulk"), _got(1000, 900, name="trip")], rule, True)
    assert not two["clean"] and two["why"].startswith("trip: 10 % lost") and two["loss_pct"] == 5.0


def test_a_step_the_pi_could_not_send_counts_for_what_was_really_sent():
    ph = {"n": 5, "of": 5, "share": 1.0, "asked_mbps": 100.0}
    row = GR.step_row(ph, [_got(75000, 75000, dur=10, size=1000)], {"max_loss_pct": 0.1}, True)      # 60 Mb/s sent of the 100 asked
    assert row["clean"] and row["limited"] and row["clean_mbps"] == pytest.approx(60.0) and "pi1 could only send 60 of the 100 Mb/s asked" in row["note"]


def _rows(*clean):
    return [{"n": i + 1, "of": len(clean), "share": 0, "asked_mbps": m, "clean_mbps": m, "clean": c, "why": "" if c else "2 % lost (limit 0.1 %)", "state": "done" if c is not None else "skipped"}
            for i, (m, c) in enumerate(zip([1, 2, 4, 6, 8], clean))]


ST = {"step_s": 10, "fractions": [0.125, 0.25, 0.5, 0.75, 1.0], "stop_after_fail": 2}


def test_headline_is_the_highest_rate_with_every_step_below_it_clean():
    r = GR.steps_result(_rows(True, True, True, False, False), 8.0, False, None, ST, {"max_loss_pct": 0.1}, False, True)
    assert r["headline"] == "Clean up to 4 Mb/s" and r["clean_mbps"] == 4 and r["clean_step"] == 3 and r["of"] == 5 and r["method"] == "steps"
    r = GR.steps_result(_rows(True, False, True, True, True), 8.0, False, None, ST, {"max_loss_pct": 0.1}, False, True)
    assert r["clean_mbps"] == 1 and r["clean_step"] == 1                             # a clean step above a failed one does not count
    r = GR.steps_result(_rows(False, False, None, None, None), 8.0, True, None, ST, {"max_loss_pct": 0.1}, True, True)
    assert r["clean_step"] is None and r["clean_mbps"] == 0 and r["headline"] == "No clean rate found: even 1 Mb/s was not clean"
    assert r["stopped_after_fails"] and any("Stopped after 2 failed steps in a row" in n for n in r["notes"])
    r = GR.steps_result(_rows(True, True, True, True, True), 8.0, False, None, ST, {"max_loss_pct": 0.1, "max_latency_ms": 60}, False, False)
    assert r["headline"] == "Clean up to 8 Mb/s" and any("may carry more" in n for n in r["notes"]) and any("judged on loss only" in n for n in r["notes"])
    assert GR.fmt_mbps(1000) == "1000" and GR.fmt_mbps(94.26) == "94.3" and GR.fmt_mbps(0.5) == "0.5" and GR.fmt_mbps(12.0) == "12"


def _summary(rows, **kw):
    sp = GR.steps_result(rows, 8.0, False, None, ST, kw.get("rule", {"max_loss_pct": 0.1}), False, True)
    return {"steps": sp, "totals": {"tx_pkts": 5000}, "streams": [{"lat_ms": {"p99": 1}}], "clocks": {"limit_ms": 2, "nodes": {}}, "stopped_early": kw.get("stopped", False)}


def test_verdict_is_about_the_clean_rate_not_about_loss_at_the_top_steps():
    v = GR.steps_verdict(_summary(_rows(True, True, True, False, False)), {"max_loss_pct": 0.1})
    assert v == {"pass": True, "reasons": []}                                        # finding where the link breaks is the result, not a failure
    v = GR.steps_verdict(_summary(_rows(True, True, True, False, False)), {"max_loss_pct": 0.1, "min_clean_mbps": 6})
    assert not v["pass"] and v["reasons"] == ["Clean up to 4 Mb/s - at least 6 Mb/s clean is required."]
    assert GR.steps_verdict(_summary(_rows(True, True, True, False, False)), {"max_loss_pct": 0.1, "min_clean_mbps": 4})["pass"]
    v = GR.steps_verdict(_summary(_rows(False, False, None, None, None)), {"max_loss_pct": 0.1})
    assert not v["pass"] and "No clean rate: the lowest step (1 Mb/s) already failed - 2 % lost (limit 0.1 %)." in v["reasons"]
    v = GR.steps_verdict(_summary(_rows(None, None, None, None, None), stopped=True), {})
    assert not v["pass"] and "stopped before the first step ended" in v["reasons"][0]
    s = _summary(_rows(True, True, True, True, True))
    s["streams"] = [{"lat_ms": None}]
    v = GR.steps_verdict(s, {"max_loss_pct": 0.1, "max_latency_ms": 60})             # a delay limit that could not be checked never passes silently
    assert not v["pass"] and "Delay could not be checked" in v["reasons"][0]


# ---------------------------------------------------------------- real runs on 127.0.0.1
def test_five_steps_on_a_clean_link_each_with_its_own_exact_counts():
    events = []
    run, s, b = go(step_profile({"top_mbps": 4}), events=events)
    assert run.status == "finished" and run.error is None, s
    sp = s["steps"]
    assert [r["asked_mbps"] for r in sp["rows"]] == [0.5, 1.0, 2.0, 3.0, 4.0] and sp["top_mbps"] == 4 and sp["auto"] is False and sp["flood"] is None
    assert all(r["clean"] and r["state"] == "done" and r["lost"] == 0 and r["tx_pkts"] == r["rx_pkts"] > 0 for r in sp["rows"]), sp["rows"]
    for r in sp["rows"]:
        assert r["sent_mbps"] == pytest.approx(r["asked_mbps"], rel=0.12) and r["got_mbps"] == pytest.approx(r["sent_mbps"], rel=0.02)
        assert r["delay_ms"] is not None and r["delay_pct"] == 99 and r["duration_s"] == pytest.approx(1.0, abs=0.15)
    assert sp["headline"] == "Clean up to 4 Mb/s" and sp["clean_mbps"] == 4 and sp["clean_step"] == 5
    assert s["verdict"]["pass"] and s["verdict"]["reasons"] == []
    # the other Pi listened to every step from the start and was told to send nothing; this Pi sent one step at a time
    assert b.calls[0] == ("rx", [0, 1, 2, 3, 4]) and [c[0] for c in b.calls] == ["rx", "stop"]
    # the streams of the result are the steps; the totals are an average over the time spent sending, not a sum of the step rates
    assert [x["name"] for x in s["streams"]] == [f"Step {i}" for i in range(1, 6)] and s["totals"]["lost"] == 0
    assert 1.5 < s["totals"]["tx_mbps"] < 2.6 and s["pairs"][0]["tx_mbps"] == pytest.approx(s["totals"]["tx_mbps"], rel=0.01)
    # live: the progress line and the table filling as steps finish
    texts = [e["step"]["text"] for e in events if e.get("ev") == "gstate" and e.get("step")]
    assert "Step 1 of 5 · 0.5 Mb/s" in texts and "Step 3 of 5 · 2 Mb/s" in texts and "Step 5 of 5 · 4 Mb/s" in texts
    seen = [len(e["steps"]["rows"]) for e in events if e.get("ev") == "gstate"]
    assert seen == sorted(seen) and seen[-1] == 5 and 2 in seen
    started = next(e for e in events if e["ev"] == "started")
    assert started["steps"]["of"] == 5 and started["total_s"] == pytest.approx(run.total_s) and 5 * 1 < run.total_s < 5 * 1 + 12
    done = events[-1]
    assert done["ev"] == "done" and done["summary"]["steps"]["headline"] == "Clean up to 4 Mb/s"


def test_a_lossy_top_step_is_marked_failed_and_clean_up_to_is_the_last_clean_rate(monkeypatch):
    monkeypatch.setenv("TT_GEN_TEST_DROP", "4:20")                                    # stream 4 = step 5: every 20th packet never arrives (5 % loss)
    run, s, _ = go(step_profile({"top_mbps": 4}))
    assert run.status == "finished", s
    rows = s["steps"]["rows"]
    assert [r["clean"] for r in rows] == [True, True, True, True, False]
    assert rows[4]["loss_pct"] == pytest.approx(5.0, abs=0.3) and "% lost (limit 0.5 %)" in rows[4]["why"]
    assert s["steps"]["headline"] == "Clean up to 3 Mb/s" and s["steps"]["clean_mbps"] == 3 and s["steps"]["clean_step"] == 4
    assert s["verdict"]["pass"]                                                       # no "needs at least" rule: the clean rate is the result
    monkeypatch.setenv("TT_GEN_TEST_DROP", "4:20")
    run, s, _ = go(step_profile({"top_mbps": 4}, rule={"max_loss_pct": 0.5, "min_clean_mbps": 3.5}))
    assert not s["verdict"]["pass"] and s["verdict"]["reasons"] == ["Clean up to 3 Mb/s - at least 3.5 Mb/s clean is required."]


def test_stops_after_two_failed_steps_in_a_row_and_does_not_send_the_rest(monkeypatch):
    monkeypatch.setenv("TT_GEN_TEST_DROP", "1:10")                                    # every step from the 2nd on loses 10 %
    events = []
    run, s, b = go(step_profile({"top_mbps": 4}), events=events)
    assert run.status == "finished", s
    rows = s["steps"]["rows"]
    assert [r["clean"] for r in rows] == [True, False, False, None, None] and [r["state"] for r in rows] == ["done", "done", "done", "skipped", "skipped"]
    assert rows[3]["asked_mbps"] == 3.0 and rows[3]["why"] == "not run"
    assert s["steps"]["stopped_after_fails"] and s["steps"]["headline"] == "Clean up to 0.5 Mb/s"
    assert [x["name"] for x in s["streams"]] == ["Step 1", "Step 2", "Step 3"]        # nothing was sent for steps 4 and 5
    assert run.total_s > 5 and sum(1 for e in events if e.get("ev") == "gstate") < 2 * (3 * (1 + 2.2)) + 6


def test_auto_top_rate_comes_from_a_flood_first():
    events = []
    run, s, b = go(step_profile({"top_mbps": None, "flood_mbps": 6, "fractions": [0.5, 1.0]}), events=events)
    assert run.status == "finished", s
    sp = s["steps"]
    assert sp["auto"] and sp["flood"] and sp["flood"]["got_mbps"] == pytest.approx(6, rel=0.15) and sp["flood"]["duration_s"] == pytest.approx(1.0, abs=0.2)
    assert sp["top_mbps"] == pytest.approx(6, rel=0.15) and float(f"{sp['top_mbps']:.3g}") == sp["top_mbps"]       # what arrived, to 3 figures
    assert [r["asked_mbps"] for r in sp["rows"]] == [pytest.approx(sp["top_mbps"] / 2, abs=1e-3), sp["top_mbps"]] and all(r["clean"] for r in sp["rows"])
    assert b.calls[0] == ("rx", [2, 0, 1])                                             # the flood has its own stream id (after the steps' ids)
    assert [x["name"] for x in s["streams"]] == ["Step 1", "Step 2"]                  # the flood is reported with the steps, not as a stream of the test
    assert any(e.get("step", {}).get("phase") == "flood" and "Measuring the top rate (1 s flood)" in e["step"]["text"] for e in events if e.get("ev") == "gstate")
    # per-second rows of the flood are kept for the chart
    assert s["duration_s"] == pytest.approx(3.0, abs=0.4)


def test_auto_flood_with_nothing_arriving_ends_with_a_plain_error(monkeypatch):
    monkeypatch.setenv("TT_GEN_TEST_DROP", "0:1")                                     # nothing arrives at all
    run, s, _ = go(step_profile({"top_mbps": None, "flood_mbps": 2}))
    assert run.status == "failed" and "Nothing arrived during the flood" in s["error"] and "type a top rate instead of Auto" in s["error"]
    assert not s["verdict"]["pass"] and all(r["state"] == "skipped" for r in s["steps"]["rows"])


def test_both_directions_and_two_streams_share_the_top_rate():
    streams = [{"name": "down", "type": "udp", "rate": {"mbps": 3}, "size": {"mode": "fixed", "size": 500}, "rtt_probe_pps": 0},
               {"name": "up", "type": "udp", "direction": "b_to_a", "rate": {"mbps": 1}, "size": {"mode": "fixed", "size": 500}, "rtt_probe_pps": 0}]
    run, s, b = go(step_profile({"top_mbps": 4, "fractions": [0.5, 1.0]}, streams=streams))
    assert run.status == "finished", s
    rows = s["steps"]["rows"]
    assert [r["asked_mbps"] for r in rows] == [2.0, 4.0] and all(r["clean"] for r in rows)
    by = {x["name"]: x for x in s["streams"]}
    assert set(by) == {"Step 1 · down", "Step 1 · up", "Step 2 · down", "Step 2 · up"}
    assert by["Step 2 · down"]["tx"]["mbps"] == pytest.approx(3, rel=0.12) and by["Step 2 · up"]["tx"]["mbps"] == pytest.approx(1, rel=0.12)   # 3 : 1, as configured
    assert [c for c in b.calls if c[0] == "tx"] == [("tx", [1]), ("tx", [3])]         # the other Pi sent its stream once per step


def test_stop_during_a_step_keeps_the_finished_steps_and_does_not_judge_the_cut_one():
    prof = step_profile({"top_mbps": 4, "step_s": 2})
    a, b = G.Node(), FakeRemote()
    run = GR.GeneratorRun(prof, a, remote=b)
    out = {}
    t = threading.Thread(target=lambda: out.update(s=run.run()))
    t.start()
    time.sleep(2 + 0.4 + 0.4 + 1.6)                                                   # step 1 done and judged, step 2 under way
    run.stop()
    t.join(15)
    assert not t.is_alive() and run.status == "stopped"
    s = out["s"]
    rows = s["steps"]["rows"]
    assert rows[0]["clean"] is True and rows[1]["state"] == "stopped" and rows[1]["clean"] is None and rows[1]["tx_pkts"] > 0
    assert [r["state"] for r in rows[2:]] == ["skipped"] * 3 and s["stopped_early"]
    assert s["steps"]["headline"] == "Clean up to 0.5 Mb/s" and not a.busy() and not b.node.busy()


def test_an_older_pi_is_refused_with_a_plain_sentence_before_anything_is_sent():
    b = FakeRemote(caps=False)
    run, s, _ = go(step_profile(), remote=b)
    assert run.status == "failed" and "older version of the app (1.3.2) that cannot run Throughput in steps" in s["error"] and "Update it" in s["error"]
    assert b.calls == []                                                              # not even told to listen


def test_without_steps_a_run_is_exactly_as_before():
    prof = step_profile({"enabled": False}, rule={"max_loss_pct": 0})
    prof["gen"]["duration_s"] = 1.5
    events = []
    run, s, b = go(prof, events=events)
    assert run.status == "finished" and "steps" not in s and s["verdict"]["pass"] and run.total_s == 1.5
    assert [c[0] for c in b.calls] == ["rx", "stop"] and [x["id"] for x in s["streams"]] == [0]
    assert all("step" not in e for e in events) and s["totals"]["tx_mbps"] == pytest.approx(10, rel=0.1)


def test_node_takes_the_next_step_only_when_the_last_one_is_done_and_reports_live_percentiles():
    port = free_port()
    mk = lambda i, d: {"id": i, "name": f"s{i}", "type": "udp", "rate": {"pps": 400}, "size": {"mode": "fixed", "size": 200}, "burst_packets": 1, "burst_gap_ms": 0,
                       "duration_s": d, "rtt_probe_pps": 0, "dscp": 0, "ttl": 64, "dst_ip": "127.0.0.1", "dst_port": port, "seed": 1}
    n = G.Node()
    try:
        n.rx_start([mk(0, 1), mk(1, 1)], "lo")
        n.tx_start([mk(0, 1)], "lo", 0.0)
        with pytest.raises(G.GenError, match="already sending"):
            n.tx_start([mk(1, 1)], "lo", 0.0)                                         # a second test (or a step too early) is still refused
        time.sleep(1.3)
        assert n.senders_done()
        with pytest.raises(G.GenError, match="already sent these streams"):
            n.tx_start([mk(0, 1)], "lo", 0.0)
        n.tx_start([mk(1, 1)], "lo", 0.0)
        assert not n.senders_done()
        time.sleep(1.4)
        lv = n.live(0)
        assert lv["done"] and lv["tx"]["0"]["pkts"] == lv["rx"]["0"]["unique"] > 300 and lv["tx"]["1"]["pkts"] == lv["rx"]["1"]["unique"] > 300
        assert lv["rx"]["0"]["p99_us"] >= lv["rx"]["0"]["p50_us"] >= 0 and lv["rx"]["0"]["max_us"] >= lv["rx"]["0"]["p99_us"]
    finally:
        n.stop()
    assert GR.local_status()["caps"]["steps"] is True                                # what a controller checks before asking an older Pi


# ---------------------------------------------------------------- stored result: JSON, CSV, zip, PDF, API
def test_stored_step_result_exports_json_csv_zip_and_pdf(monkeypatch):
    import io, json, tempfile, zipfile
    from fastapi.testclient import TestClient
    from traffic_tester import report
    from traffic_tester.server import create_app
    from traffic_tester.store import Store
    monkeypatch.setenv("TT_GEN_TEST_DROP", "2:10")                                    # step 2 of 2 loses 10 % (stream ids: steps 0 and 1, flood 2)
    prof = step_profile({"top_mbps": None, "flood_mbps": 4, "fractions": [0.5, 1.0]})
    run, summ, _ = go(prof)
    assert run.status == "finished" and summ["steps"]["flood"]["loss_pct"] == pytest.approx(10, abs=1)
    d = tempfile.mkdtemp()
    with TestClient(create_app(d, "A")) as c:
        s = Store(d)
        rid = s.create_run(1, prof, "A", "pi2")
        s.finish_run(rid, "finished", json.loads(json.dumps(summ)))
        plain = s.create_run(1, P.validate_profile({"name": "flood", "kind": "generator", "gen": {"streams": [{"name": "s"}]}}), "A", "pi2")
        s.finish_run(plain, "failed", {"kind": "generator", "error": "x"})
        got = c.get(f"/api/runs/{rid}").json()
        assert got["summary"]["steps"]["headline"] == summ["steps"]["headline"] and got["verdict"] == ("PASS" if summ["verdict"]["pass"] else "FAIL")
        js = c.get(f"/api/runs/{rid}/export.json").json()
        assert js["summary"]["steps"]["rows"][0]["clean"] in (True, False) and js["profile"]["gen"]["steps"]["enabled"] is True
        r = c.get(f"/api/runs/{rid}/export.csv?kind=steps")
        assert r.status_code == 200 and "run-%d-steps.csv" % rid in r.headers["content-disposition"]
        lines = r.text.splitlines()
        assert lines[0].startswith("step,of,share_of_top,asked_mbps,sent_mbps,got_mbps") and lines[1].startswith("flood,2,") and lines[2].startswith("1,2,0.5,") and lines[3].startswith("2,2,1.0,")
        assert lines[-1].startswith("headline," + summ["steps"]["headline"]) and "auto (flood)" in lines[-1]
        assert c.get(f"/api/runs/{plain}/export.csv?kind=steps").status_code == 404   # a run that was not in steps has no step table
        z = zipfile.ZipFile(io.BytesIO(s.session_zip(1)))
        assert f"run-{rid}/steps.csv" in z.namelist() and f"run-{plain}/steps.csv" not in z.namelist()
        pdf = c.get(f"/api/report.pdf?run_ids={rid}").content
        assert pdf[:5] == b"%PDF-" and len(pdf) > 3000
    story = report._steps_block(summ)
    text = " ".join(getattr(x, "text", "") for x in story)
    assert "Throughput in steps" in text and summ["steps"]["headline"] in text and "measured by a 1 s flood" in text
    assert report._steps_block({"kind": "generator"}) == []
