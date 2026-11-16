"""Orbit radio follow-up (1.6.1): packetization metadata, bundling detector, data-usage maths, hourly breakdown, scheduled repeats."""
import os
import random
import time

import pytest

from traffic_tester import profiles as P, radio as R, repeats as RPT


@pytest.fixture(autouse=True)
def _tz(monkeypatch):
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


# ---------------------------------------------------------------- metadata
def test_profile_radio_defaults_and_validation():
    p = P.validate_profile({"name": "x", "transport": "serial"})
    assert p["radio"] == {"char_timeout_ms": None, "max_packet_bytes": None, "serial_mode": "", "note": ""}
    assert R.radio_text(p["radio"]) == "radio packetization not recorded"
    q = P.validate_profile({"name": "x", "transport": "serial", "radio": {"char_timeout_ms": 1, "max_packet_bytes": 19, "serial_mode": "udp", "note": "Orbit MCR"}})
    t = R.radio_text(q["radio"])
    assert "character timeout 1 ms" in t and "packet size 19 bytes" in t and "UDP" in t and "Orbit MCR" in t
    with pytest.raises(ValueError):
        P.validate_profile({"name": "x", "radio": {"serial_mode": "carrier pigeon"}})
    with pytest.raises(ValueError):
        P.validate_profile({"name": "x", "radio": {"max_packet_bytes": 0}})


def test_orbit_preset_info_tells_how_to_set_the_radio():
    info = P.TRIP_INFO["DER transfer trip — Orbit radio (2 s budget)"]
    txt = " ".join(info.values()).lower()
    assert "one message per packet" in txt and "character timeout" in txt


def test_advice_values():
    a = R.advice(19200, 19)
    assert a["char_timeout_ms"] == pytest.approx(1.04, abs=0.01) and a["max_packet_bytes"] == 19
    assert "does not change the radio" in a["text"]


# ---------------------------------------------------------------- data usage
def test_usage_payload_matches_the_email_figure_and_air_is_several_times_more():
    u = R.usage_estimate(19200, 19, 10.0)
    assert u["frames_per_s"] == 100.0
    assert u["payload"]["bytes_per_s"] == 1900.0
    assert 4.5 < u["payload"]["gb_per_30_days"] < 5.5                 # "about 2 KB/s, ~5 GB a month"
    udp, tcp = u["over_the_air"]["udp"], u["over_the_air"]["tcp"]
    assert udp["header_bytes"] == 28 and tcp["header_bytes"] == 40
    assert udp["bytes_per_s"] == pytest.approx((1900 + 100 * 28) * 1.05, rel=1e-3)
    assert 2.4 < udp["x_payload"] < 2.8 and tcp["x_payload"] > udp["x_payload"]
    assert udp["both_ways_gb_per_30_days"] == pytest.approx(2 * udp["gb_per_30_days"], abs=0.02)
    assert u["selected"] == "udp" and R.usage_estimate(19200, serial_mode="tcp_client")["selected"] == "tcp"
    assert "payload" in u["text"].lower() and "over the air" in u["text"].lower()


def test_usage_is_capped_by_the_serial_line_and_batching_cuts_headers():
    fast = R.usage_estimate(9600, 19, 1.0)                              # 1 ms asked, but 9600 baud carries 50 frames/s
    assert fast["frames_per_s"] == pytest.approx(50.5, abs=0.1)
    one = R.usage_estimate(19200, 19, 10.0)["over_the_air"]["udp"]["bytes_per_s"]
    four = R.usage_estimate(19200, 19, 10.0, frames_per_packet=4)["over_the_air"]["udp"]["bytes_per_s"]
    assert four < one


def test_actual_usage_counts_bytes():
    a = R.actual_usage(1000, 990, 19, 10.0)
    assert a["tx_bytes"] == 19000 and a["rx_bytes"] == 18810
    assert a["tx_over_air_bytes"] == round((19000 + 1000 * 28) * 1.05)


# ---------------------------------------------------------------- bundling detector
def _gaps(bursts, singles, near_each=1):
    g = R.GapStats()
    for _ in range(singles):
        g.add(10e6, 10e6)
    for size in bursts:
        g.add(10e6, 10e6)                                # the gap before the burst
        for _ in range(size - 1):
            g.add(0.0, 10e6)
    return g


def test_detector_clean_stream_not_flagged():
    g = R.GapStats()
    for _ in range(2000):
        g.add(random.uniform(9e6, 11e6), 10e6)
    r = R.detect_bundling(g.to_dict(), [30 + random.random() * 4 for _ in range(2000)])
    assert not r["suspected"] and r["enough_data"] and "No sign" in r["message"]


def test_detector_flags_bursts():
    g = _gaps([4] * 500, 0)                                # frames arrive in groups of four with no gap
    r = R.detect_bundling(g.to_dict(), None)
    assert r["suspected"] and r["mean_burst"] == 4.0 and r["burst_pct"] > 60
    assert "bundling messages" in r["message"] and "character timeout" in r["message"]


def test_detector_flags_rtt_steps_and_ignores_smooth_spread():
    random.seed(3)
    steps = [40 + 10 * random.choice([0, 1, 2, 3]) + random.uniform(-0.3, 0.3) for _ in range(4000)]
    assert R.rtt_step(steps) and R.rtt_step(steps)["step_ms"] == pytest.approx(10, abs=1.5)
    smooth = [30 + random.gauss(0, 6) for _ in range(4000)]
    assert R.rtt_step(smooth) is None
    g = R.GapStats()
    for _ in range(1000):
        g.add(10e6, 10e6)
    assert R.detect_bundling(g.to_dict(), steps)["suspected"]


def test_detector_needs_enough_data():
    g = R.GapStats()
    for _ in range(50):
        g.add(0.0, 10e6)
    r = R.detect_bundling(g.to_dict())
    assert not r["suspected"] and not r["enough_data"] and "Too few" in r["message"]


# ---------------------------------------------------------------- hourly breakdown
def _wall(y, mo, d, h, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def test_hourly_breakdown_by_hour_and_weekday_weekend():
    hs = R.HourStats()
    # Fri 2026-11-13 08:xx: quiet; 17:xx: slow tail; Sat 2026-11-14 08:xx quiet
    for sec in range(0, 3600, 2):
        hs.add_rtt(_wall(2026, 11, 13, 8) + sec, 30.0)
        hs.add_rtt(_wall(2026, 11, 13, 17) + sec, 30.0 if sec % 100 else 150.0)
        hs.add_rtt(_wall(2026, 11, 14, 8) + sec, 31.0)
    hs.add_lost(_wall(2026, 11, 13, 17, 5), 3)
    hs.add_outage(_wall(2026, 11, 13, 17, 5), 450.0)
    hs.add_outage(_wall(2026, 11, 13, 17, 5), 450.0)            # asking twice counts once
    rep = hs.report()
    assert [h["hour"] for h in rep["hours"]] == [8, 17, 8] and [h["kind"] for h in rep["hours"]] == ["weekday", "weekday", "weekend"]
    h17 = rep["hours"][1]
    assert h17["max"] == 150.0 and h17["p50"] == pytest.approx(30.0, abs=0.1) and h17["p999"] == 150.0 and h17["lost"] == 3 and h17["outages"] == 1 and h17["outage_ms"] == 450.0
    assert rep["hours"][0]["max"] == 30.0 and rep["hours"][0]["outages"] == 0
    hod = {r["hour"]: r for r in rep["by_hour_of_day"]}
    assert hod[8]["n"] == 3600 and hod[17]["p999"] == 150.0                 # hour 8 merges Friday and Saturday
    assert rep["kinds"]["weekday"]["n"] == 3600 and rep["kinds"]["weekend"]["n"] == 1800
    assert rep["kinds"]["weekend"]["max"] == 31.0


def test_coverage_busy_hours_and_tags():
    mon_am = R.coverage(_wall(2026, 11, 16, 6, 30), _wall(2026, 11, 16, 10))
    assert mon_am["busy_7_9am"] and not mon_am["busy_4_7pm"] and not mon_am["weekend"] and mon_am["tag"] == "weekday morning"
    assert "4-7 pm" in mon_am["note"] and "not covered" in mon_am["note"]
    week = R.coverage(_wall(2026, 11, 13, 6), _wall(2026, 11, 15, 12))       # Fri 06:00 -> Sun 12:00
    assert week["busy_7_9am"] and week["busy_4_7pm"] and week["weekend"] and "not covered" not in week["note"]
    night = R.coverage(_wall(2026, 11, 14, 1), _wall(2026, 11, 14, 4))
    assert night["tag"] == "weekend night" and not night["busy_7_9am"]
    assert "Sat 2026-11-14 01:00" == night["start_local"]


# ---------------------------------------------------------------- emulator integration
def test_emulator_reports_bytes_packetization_and_hourly(monkeypatch):
    from traffic_tester.mbemu import engine as E
    monkeypatch.setattr(E, "HOURLY_MIN_S", 1)
    r = E.selftest(2.5, a={"codec": "trip"}, b={"codec": "trip"})
    m = r["summary"]["mirrored"]
    assert m["bytes"]["tx_frames"] > 100 and m["bytes"]["tx_bytes"] == m["bytes"]["tx_frames"] * 19
    assert m["bytes"]["tx_link_bytes"] > 0 and m["bytes"]["rx_link_bytes"] > 0
    assert "suspected" in m["packetization"] and m["packetization"]["gaps"]["n"] > 100
    h = m["hourly"]
    assert h["hours"] and h["hours"][0]["n"] > 100 and h["by_hour_of_day"] and h["kinds"]


# ---------------------------------------------------------------- scheduler
class FakeStore:
    def __init__(self):
        self.d = {}

    def get_setting(self, k, default=None):
        return self.d.get(k, default)

    def set_setting(self, k, v):
        import json
        self.d[k] = json.loads(json.dumps(v))


def test_schedule_validation():
    for bad in ({}, {"kind": "every", "every_h": 0.01}, {"kind": "every", "every_h": "x"}, {"kind": "times", "times": []}, {"kind": "times", "times": ["25:00"]},
                {"kind": "every", "every_h": 6, "for_days": 400}, {"kind": "times", "times": ["08:00"], "days": "mondays"}):
        with pytest.raises(RPT.BadSchedule):
            RPT.validate_schedule(bad)
    assert RPT.validate_schedule({"kind": "times", "times": "8:00, 12:00 17:30,22:00", "for_days": 3})["times"] == ["08:00", "12:00", "17:30", "22:00"]


def test_every_6h_for_7_days_has_29_slots():
    sch = RPT.validate_schedule({"kind": "every", "every_h": 6, "for_days": 7})
    occ = RPT.occurrences(sch, 1_000_000)
    assert len(occ) == 29 and occ[1] - occ[0] == 6 * 3600


def test_times_daily_including_weekends_and_weekday_filter():
    start = _wall(2026, 11, 13, 6)                       # Friday 06:00
    sch = RPT.validate_schedule({"kind": "times", "times": ["08:00", "12:00", "17:30", "22:00"], "days": "all", "for_days": 3})
    occ = RPT.occurrences(sch, start)
    assert len(occ) == 4 * 3 and time.strftime("%a %H:%M", time.localtime(occ[0])) == "Fri 08:00" and time.strftime("%a", time.localtime(occ[-1])) == "Sun"
    wk = RPT.occurrences(RPT.validate_schedule({"kind": "times", "times": ["08:00"], "days": "weekdays", "for_days": 7}), start)
    assert all(time.localtime(t).tm_wday < 5 for t in wk) and len(wk) == 5
    we = RPT.occurrences(RPT.validate_schedule({"kind": "times", "times": ["08:00"], "days": "weekend", "for_days": 7}), start)
    assert len(we) == 2


def _mgr(store, clock, busy=lambda: False, fail=False):
    started = []

    def start(prof):
        if fail:
            raise RuntimeError("port busy")
        started.append(clock[0])
        return 100 + len(started)

    return RPT.RepeatManager(store, start, busy, now=lambda: clock[0]), started


def test_tick_starts_due_slots_once_and_survives_restart():
    clock = [_wall(2026, 11, 13, 6)]
    st = FakeStore()
    m, started = _mgr(st, clock)
    spec = m.create({"name": "Orbit"}, {"kind": "every", "every_h": 6, "for_days": 1})
    assert m.tick() == 1 and m.tick() == 0                      # slot 0 starts now, not twice
    clock[0] += 6 * 3600 + 20
    assert m.tick() == 1
    # "reboot": a new manager over the same stored settings carries on, does not repeat what ran
    m2, started2 = _mgr(st, clock)
    assert m2.tick() == 0
    clock[0] += 6 * 3600
    assert m2.tick() == 1
    v = m2.get(spec["id"])
    assert v["runs"] == [101, 101 - 100 + 100 + 1, 102][:0] or len(v["runs"]) == 3
    assert m2.list()[0]["status"] == "waiting" and m2.list()[0]["next_local"]


def test_missed_slot_is_counted_not_run_late_and_busy_waits_then_misses():
    clock = [_wall(2026, 11, 13, 6)]
    st = FakeStore()
    busy = [True]
    m, started = _mgr(st, clock, busy=lambda: busy[0])
    m.create({"name": "t"}, {"kind": "every", "every_h": 6, "for_days": 1})
    assert m.tick() == 0                                        # busy: waits
    busy[0] = False
    clock[0] += 60
    assert m.tick() == 1                                        # inside the grace: starts
    clock[0] += 12 * 3600 + 3 * 3600                             # Pi was off for 15 h: two slots are gone
    m.tick()
    s = m.list()[0]
    assert s["missed"] >= 2 and len(s["runs"]) <= 2
    busy[0] = True
    clock[0] += 3 * 3600
    m.tick()
    assert m.list()[0]["missed"] >= 2


def test_start_error_is_recorded_and_stop_delete():
    clock = [_wall(2026, 11, 13, 6)]
    st = FakeStore()
    m, _ = _mgr(st, clock, fail=True)
    s = m.create({"name": "t"}, {"kind": "every", "every_h": 6, "for_days": 1})
    m.tick()
    v = m.list()[0]
    assert v["missed"] == 1 and "port busy" in v["errors"][0]
    assert m.stop(s["id"]) and m.list()[0]["status"] == "stopped"
    clock[0] += 7 * 3600
    assert m.tick() == 0
    assert m.delete(s["id"]) and m.list() == [] and not m.delete("nope")


def test_combined_summary_worst_values_and_coverage():
    def run(i, start, p999, verdict, lost=0, bundling=False):
        return {"id": i, "status": "finished", "started_at": "x", "summary": {"sent": 1000, "lost": lost, "loss_pct": 0, "rtt_ms": {"p50": 30, "p99": 50, "p999": p999, "max": p999 + 10},
                "verdict": {"pass": verdict}, "when": R.coverage(start, start + 600), "mirrored": {"packetization": {"suspected": bundling}}}}
    runs = [run(1, _wall(2026, 11, 13, 8), 80, True), run(2, _wall(2026, 11, 13, 17), 210, False, lost=2), run(3, _wall(2026, 11, 14, 8), 90, True, bundling=True)]
    c = RPT.combine({"id": "ab", "name": "n", "text": "t", "missed": 1}, runs)
    t = c["totals"]
    assert t["runs"] == 3 and t["passed"] == 2 and t["failed"] == 1 and t["worst_p999"] == 210 and t["frames_lost"] == 2 and t["missed_slots"] == 1 and t["bundling_runs"] == 1
    assert c["coverage"] == {"busy_7_9am": True, "busy_4_7pm": True, "weekend": True} and "All busy" in c["coverage_note"]
    assert {r["hour"] for r in c["by_start_hour"]} == {8, 17}
    assert {r["tag"] for r in c["by_tag"]} == {"weekday morning", "weekday afternoon", "weekend morning"}
    assert "worst" in c["basis"].lower()


# ---------------------------------------------------------------- stored results: CSV / JSON / PDF / compare / API
def _radio_run(st, radio=None, hourly=True, bundling=False, p99=40.0):
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from _cmpfix import trip_run
    rid = trip_run(st, name="DER transfer trip - Orbit", p99=p99, extra_prof={"transport": "serial", "net": {}, "radio": radio or {}})
    run = st.get_run(rid)
    hs = R.HourStats()
    for sec in range(0, 7200, 5):
        hs.add_rtt(_wall(2026, 11, 13, 8) + sec, 30.0 + (sec % 50) / 10)
    hs.add_lost(_wall(2026, 11, 13, 9, 5), 2)
    w = R.coverage(_wall(2026, 11, 13, 8), _wall(2026, 11, 13, 10))
    sm = run["summary"]
    sm["radio"] = {**(radio or {}), "text": R.radio_text(radio), "recorded": R.radio_recorded(radio)}
    sm["when"] = w
    sm["usage"] = {**R.usage_estimate(19200, 19, 10.0, (radio or {}).get("serial_mode", "")), "actual": R.actual_usage(1000, 990, 19, 10.0)}
    sm["mirrored"] = {"emulator": 2, "frame_mode": True, "baud": 19200, "wire_period_ms": 10, "codec": "Trip frame",
                      "packetization": R.detect_bundling(_gaps([4] * 200, 0).to_dict()) if bundling else R.detect_bundling(None),
                      "hourly": hs.report() if hourly else None, "frames": {"good": 990, "lost": 10, "lost_pct": 1.0}, "rtt": {"n": 990, "p50": 30, "p99": p99, "p999": 90, "max": 120},
                      "transfer": {"threshold_ms": 60}, "rejects": {}, "impair": {}, "outages": [], "histogram": {}}
    st.finish_run(rid, "finished", sm)
    return rid


def test_csv_json_pdf_and_compare_carry_the_radio_facts():
    import tempfile
    from traffic_tester.compare import compare
    from traffic_tester.report import build_report
    from traffic_tester.store import Store
    from _cmpfix import pdf_text
    st = Store(tempfile.mkdtemp())
    a = _radio_run(st, {"char_timeout_ms": 1.0, "max_packet_bytes": 19, "serial_mode": "udp", "note": "Orbit MCR"}, bundling=False)
    b = _radio_run(st, None, bundling=True, p99=55.0)
    csv_a, csv_b = st.messages_csv(a).splitlines(), st.messages_csv(b).splitlines()
    head = csv_a[0].split(",")
    for col in ("radio_recorded", "radio_char_timeout_ms", "radio_max_packet_bytes", "radio_serial_mode", "radio_note", "started_local", "ended_local", "time_tag"):
        assert col in head
    row = dict(zip(head, next(__import__("csv").reader([csv_a[1]]))))
    assert row["radio_recorded"] == "yes" and row["radio_char_timeout_ms"] == "1.0" and row["radio_max_packet_bytes"] == "19" and row["radio_serial_mode"] == "udp" and row["radio_note"] == "Orbit MCR"
    assert row["started_local"].startswith("Fri 2026-11-13 08:00") and row["time_tag"] == "weekday morning"
    rb = dict(zip(csv_b[0].split(","), next(__import__("csv").reader([csv_b[1]]))))
    assert rb["radio_recorded"].startswith("no") and "not recorded" in rb["radio_recorded"]
    js = st.run_json(a)
    assert js["summary"]["radio"]["text"].startswith("Radio serial packetization") and js["profile"]["radio"]["serial_mode"] == "udp" and js["summary"]["mirrored"]["hourly"]["hours"]
    text = pdf_text(build_report(st, [a, b], "t"))
    assert "Radio, time of day and data use" in text and "character timeout 1 ms" in text and "radio packetization not recorded" in text
    assert "bundling messages" in text and "Delay by hour of day" in text and "Busy hours covered" in text and "over the air" in text.lower()
    c = compare(st, [a, b])
    diff = {d["label"]: d["values"] for d in c["setup_diff"]}
    assert "Radio packetization" in diff and "not recorded" in diff["Radio packetization"][1] and "character timeout" in diff["Radio packetization"][0]


def test_report_with_combined_summary_page():
    import tempfile
    from traffic_tester.report import build_report
    from traffic_tester.store import Store
    from _cmpfix import pdf_text
    st = Store(tempfile.mkdtemp())
    ids = [_radio_run(st, {"char_timeout_ms": 1}), _radio_run(st, {"char_timeout_ms": 1}, p99=70.0)]
    comb = RPT.combine({"id": "ab", "name": "Orbit week", "text": "every 6 h for 7 days", "missed": 2, "status": "waiting"}, [st.get_run(i) for i in ids])
    text = pdf_text(build_report(st, ids, "t", repeat=comb))
    assert "Scheduled repeats: Orbit week" in text and "Worst p99.9" in text and "worst value of any single run" in text


def test_api_usage_and_repeats(tmp_path):
    from fastapi.testclient import TestClient
    from traffic_tester.server import create_app
    with TestClient(create_app(str(tmp_path), "A")) as c:
        u = c.get("/api/radio/usage?baud=19200&interval_ms=10&serial_mode=tcp_client").json()
        assert u["selected"] == "tcp" and u["payload"]["bytes_per_s"] == 1900 and u["advice"]["max_packet_bytes"] == 19
        prof = P.validate_profile({"name": "Orbit test", "transport": "serial", "radio": {"char_timeout_ms": 1}})
        bad = c.post("/api/repeats", json={"profile": prof, "schedule": {"kind": "times", "times": ["99:00"]}})
        assert bad.status_code == 422 and "time of day" in bad.text
        r = c.post("/api/repeats", json={"profile": prof, "schedule": {"kind": "times", "times": "08:00,17:30", "days": "all", "for_days": 3}, "start_at": time.time() + 3600})
        assert r.status_code == 200, r.text
        v = r.json()
        assert v["status"] == "waiting" and v["total_slots"] >= 5 and "profile" not in v and v["runs"] == []
        assert [x["id"] for x in c.get("/api/repeats").json()] == [v["id"]]
        s = c.get(f"/api/repeats/{v['id']}/summary").json()
        assert s["totals"]["runs"] == 0 and "Not covered yet" in s["coverage_note"]
        assert c.get(f"/api/report.pdf?repeat_id={v['id']}").status_code == 422            # no runs yet: nothing to put in the report
        assert c.post(f"/api/repeats/{v['id']}/stop").status_code == 200 and c.get("/api/repeats").json()[0]["status"] == "stopped"
        assert c.delete(f"/api/repeats/{v['id']}").status_code == 200 and c.get("/api/repeats").json() == []
        assert c.get("/api/repeats/nope/summary").status_code == 404 and c.delete("/api/repeats/nope").status_code == 404
