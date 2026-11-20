"""1.12.3: the timed (trip-frame) codec is the default for Mirrored Bits tests; saved tests keep their format; the verdict of a timed test is judged on the
round trip (one-way limit = half the round trip, labelled as an estimate); results and the PDF say which format was used."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from traffic_tester import profiles as P
from traffic_tester.mbemu import engine as E
from traffic_tester.store import Store

PRESET = "DER trip — Mirrored Bits mode"


def _mb(codec=None, **kw):
    m = {} if codec is None else {"codec": codec}
    return P.validate_profile({"name": "t", "transport": "serial", "mode": "mirrored", "mirrored": {**m, **kw}})


# ------------------------------------------------------------------ defaults
def test_a_new_mirrored_bits_test_defaults_to_the_timed_codec():
    p = _mb()
    assert p["mirrored"]["codec"] == "trip" and p["mirrored"]["limit_basis"] == "one_way_est"
    d = P.form_limits()["defaults"]["mirrored"]                       # what the New test / custom form starts from
    assert d["codec"] == "trip" and d["limit_basis"] == "one_way_est"


def test_an_explicit_sim_test_stays_sim_and_an_old_trip_test_keeps_the_round_trip_limit():
    assert _mb("sim")["mirrored"]["codec"] == "sim"
    old = _mb("trip")                                                   # a trip test saved before limit_basis existed was judged on the full round trip
    assert old["mirrored"]["limit_basis"] == "round_trip"
    assert _mb("trip", limit_basis="one_way_est")["mirrored"]["limit_basis"] == "one_way_est"


def test_the_builtin_preset_is_timed_and_the_orbit_budget_test_still_judges_the_round_trip():
    pre = {p["name"]: p for p in P.builtin_presets()}
    m = pre[PRESET]
    assert m["mirrored"]["codec"] == "trip" and m["mirrored"]["limit_basis"] == "one_way_est" and m["threshold_ms"] == 60
    assert "Timed" in m["description"] and "Real Mirrored Bits load" in m["description"]
    assert "timed" in m["info"]["what"].lower() and "real mirrored bits load" in m["info"]["what"].lower() and "estimate" in m["info"]["how"]
    o = pre["DER transfer trip — Orbit radio (2 s budget)"]
    assert o["mirrored"]["codec"] == "trip" and o["mirrored"]["limit_basis"] == "round_trip" and o["threshold_ms"] == 200


def test_saved_tests_keep_their_format_across_the_update():
    d = tempfile.mkdtemp()
    st = Store(d)
    sim = st.create_profile({"name": "my sim", "transport": "serial", "mode": "mirrored", "mirrored": {"codec": "sim"}})
    old = st.create_profile({"name": "my old", "transport": "serial", "mode": "mirrored"})
    assert sim["mirrored"]["codec"] == "sim" and old["mirrored"]["codec"] == "trip"      # (a new test made without a format is a timed one)
    # a test stored by a version that had no codec yet (before 1.6.0): rewrite its stored JSON without the field, then start the app again
    raw = json.loads(st.db.execute("SELECT json FROM profiles WHERE id=?", (old["id"],)).fetchone()["json"])
    raw["mirrored"].pop("codec")
    raw["mirrored"].pop("limit_basis", None)
    st.db.execute("UPDATE profiles SET json=? WHERE id=?", (json.dumps(raw), old["id"]))
    st.db.close()
    st2 = Store(d)
    assert st2.get_profile(old["id"])["mirrored"]["codec"] == "sim"
    assert st2.get_profile(sim["id"])["mirrored"]["codec"] == "sim"
    assert P.validate_profile(st2.get_profile(old["id"]))["mirrored"]["codec"] == "sim"            # and it still validates (runs) as a sim test


def test_an_edited_builtin_preset_adopts_the_new_default_only_where_the_user_did_not_choose():
    d = tempfile.mkdtemp()
    st = Store(d)
    row = next(p for p in st.list_profiles() if p.get("default_name") == PRESET)
    assert row["mirrored"]["codec"] == "trip"
    q = {**row, "threshold_ms": 80}                                      # the user edited only the limit
    st.update_profile(row["id"], q)
    ov = json.loads(st.db.execute("SELECT override_json FROM profiles WHERE id=?", (row["id"],)).fetchone()["override_json"])
    assert "codec" not in ov.get("mirrored", {}) and ov["threshold_ms"] == 80
    q2 = {**st.get_profile(row["id"])}
    q2["mirrored"] = {**q2["mirrored"], "codec": "sim"}                  # a user who picked the SimCodec keeps it
    st.update_profile(row["id"], q2)
    assert st.get_profile(row["id"])["mirrored"]["codec"] == "sim"


# ------------------------------------------------------------------ verdict
def _m(p99, p999=None, half=True, lost=0.0, max_=None):
    rt = {"n": 10000, "p50": p99 / 2, "p95": p99 * 0.9, "p99": p99, "p999": p999 or p99, "max": max_ or (p999 or p99) + 1}
    return {"frame_mode": True, "limit_basis": "one_way_est" if half else "round_trip", "rtt": rt,
            "frames": {"good": 10000, "lost": 0, "lost_pct": lost}, "unavail_pct": 0.0, "rok_drops": 0, "longest_outage_ms": 0, "rbad_events": 0}


def test_timed_verdict_holds_the_one_way_limit_against_half_the_round_trip():
    assert E.verdict(_m(100.0), 60, 99, 0.1, 0.1)["pass"]                         # 100 ms round trip = 50 ms one way (estimate)
    v = E.verdict(_m(130.0), 60, 99, 0.1, 0.1)
    assert not v["pass"] and "65" in v["reasons"][0] and "estimated as half the round trip" in v["reasons"][0] and "60" in v["reasons"][0]
    assert not E.verdict(_m(100.0, half=False), 60, 99, 0.1, 0.1)["pass"]         # the older round-trip basis: the same 100 ms round trip fails a 60 ms limit
    assert not E.verdict(_m(100.0, lost=1.0), 60, 99, 0.1, 0.1)["pass"]           # lost frames still fail it


def test_timed_summary_labels_the_estimate_and_counts_lost_ooo_and_crc_separately():
    r = E.selftest(duration_s=3, a={"codec": "trip", "baud": 38400, "trip_every_s": 0.5}, b={"codec": "trip", "baud": 38400})
    ea = r["a"]
    s = ea.summary(threshold_ms=60.0, percentile=99, max_loss_pct=0.1, max_down_pct=0.1, limit_basis="one_way_est")
    m = s["mirrored"]
    assert s["verdict"]["pass"], s["verdict"]
    assert m["frame_mode"] and m["limit_basis"] == "one_way_est" and m["limit_rtt_ms"] == 120.0 and s["threshold_ms"] == 120.0
    assert m["rtt"]["one_way_est_ms"] is not None and "GPS" in m["one_way_gps"]
    for k in ("lost", "crc_errors", "out_of_order", "duplicates"):
        assert k in m["frames"]
    assert "Trip frame" in m["codec"]
    rt = ea.summary(threshold_ms=60.0, percentile=99, max_loss_pct=0.1, max_down_pct=0.1, limit_basis="round_trip")
    assert rt["mirrored"]["limit_rtt_ms"] == 60.0


def test_sim_summary_is_unchanged_and_has_no_limit_basis():
    r = E.selftest(duration_s=2, a={"codec": "sim", "baud": 38400, "trip_every_s": 0.5}, b={"codec": "sim", "baud": 38400})
    m = r["summary"]["mirrored"]
    assert not m["frame_mode"] and m["limit_basis"] is None and m["limit_rtt_ms"] is None and "SimCodec" in m["codec"]


# ------------------------------------------------------------------ results: the PDF names the format
def _stored(st, codec):
    prof = P.validate_profile({"name": f"MB {codec}", "transport": "serial", "mode": "mirrored", "mirrored": {"codec": codec, "limit_basis": "one_way_est"}})
    r = E.selftest(duration_s=3, a={"codec": codec, "baud": 38400, "trip_every_s": 0.5}, b={"codec": codec, "baud": 38400})
    s = r["a"].summary(threshold_ms=60.0, percentile=99, max_loss_pct=0.1, max_down_pct=0.1, limit_basis=prof["mirrored"]["limit_basis"])
    rid = st.create_run(st.current_session()["id"], prof, "pi1", "serial", leg={"label": "pi1 → pi2", "kind": "round_trip", "basis": "rtt", "tester": "pi1", "reflector": "pi2", "controller": "pi1"})
    s["endpoints"] = {"source": "pi1", "dest": "pi2", "kind": "round_trip", "basis": "rtt", "controller": "pi1", "label": "pi1 → pi2"}
    st.finish_run(rid, "finished", s)
    return rid


def test_pdf_report_says_which_format_was_used_and_labels_the_one_way_estimate():
    from _cmpfix import pdf_text
    from traffic_tester.report import build_report
    st = Store(tempfile.mkdtemp())
    timed, sim = _stored(st, "trip"), _stored(st, "sim")
    t = pdf_text(build_report(st, [timed]))
    assert "Timed" in t and "One way \\(ESTIMATE" in t and "out of order" in t and "CRC errors" in t
    u = pdf_text(build_report(st, [sim]))
    assert "Real Mirrored Bits load" in u and "One way \\(ESTIMATE" not in u
