"""Phase 3 (utility test sets): application library, targets, steps (alone / together / fill) stored as one result, per-application table and verdict."""
import socket
import threading
import time

import pytest

from traffic_tester import apps as A, profiles as P
from traffic_tester.gen import node as G, testset as TS
from tests.test_gen_grid import Fake, wait_for


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setenv("TT_ASSUME_SYNCED", "1")


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def tsprof(apps=("trip", "nms"), dur=2, alone_s=5, fill=False, targets=None, **ts):
    streams = []
    for a in apps:
        s = A.new_stream(a)
        s["dst_port"] = free_port()
        s["rtt_probe_pps"] = 0
        s["type"] = "udp"                                   # loopback test: no TCP
        s["rate"] = {"pps": 100}
        s["burst_packets"], s["burst_gap_ms"] = 1, 0
        if targets and a in targets:
            s["target"] = {**s["target"], **targets[a]}
        streams.append(s)
    return P.validate_profile({"name": "ts", "kind": "generator", "net": {"peer_host": "127.0.0.1"},
                               "gen": {"streams": streams, "duration_s": dur, "local_host": "127.0.0.1", "rule": {"max_loss_pct": None},
                                       "testset": {"enabled": True, "alone": True, "alone_s": alone_s, "fill": fill, "fill_s": 5, "fill_mbps": 1, **ts}}})


def go(p):
    ev = []
    run = TS.TestSetRun(p, G.Node(), emit=ev.append, remote=Fake())
    return run, ev, run.run()


def test_library_and_builtin_sets():
    ids = {a["id"] for a in A.library()}
    assert {"trip", "scada_poll", "nms", "nms_file", "video", "ami", "scada_unsol", "goose", "bulk"} <= ids
    sets = P.testset_presets()
    assert {s["gen"]["testset"]["group"] for s in sets} == {"da", "fan"}
    assert all(s["gen"]["testset"]["enabled"] and s["builtin"] for s in sets)
    for s in sets:                                           # every application has an example target
        assert all(x["target"] and x["target"]["example"] for x in s["gen"]["streams"])
    assert TS.is_testset(sets[0]) and not TS.is_testset(P.generator_presets()[0])


def test_validation():
    with pytest.raises(ValueError):
        P.validate_profile({"name": "x", "kind": "generator", "gen": {"duration_s": 0, "testset": {"enabled": True}}})
    with pytest.raises(ValueError):
        P.validate_profile({"name": "x", "kind": "generator", "gen": {"steps": {"enabled": True}, "testset": {"enabled": True}}})
    # trip tests carry length choices
    t = next(p for p in P.builtin_presets() if p["name"] == "DER trip test")
    assert t["length_choices"] == [60, 600, 3600, 86400]


def test_judge_margins_and_unchecked_delay():
    m = {"loss_pct": 0.2, "lat_ms": {"p99": 40.0, "p95": 30.0}, "jitter_ms": 12.0, "rx_mbps": 3.0, "tx_pkts": 100, "warnings": []}
    c = {x["key"]: x for x in TS.judge("a", m, {"max_loss_pct": 0.5, "max_latency_ms": 60, "latency_percentile": 99, "max_jitter_ms": 10, "min_throughput_mbps": 3.8})}
    assert c["loss"]["pass"] and c["loss"]["margin"] == 0.3
    assert c["latency"]["pass"] and c["latency"]["margin"] == 20
    assert c["jitter"]["pass"] is False and c["jitter"]["margin"] == -2
    assert c["throughput"]["pass"] is False and c["throughput"]["margin"] == -0.8
    assert TS.app_pass(list(c.values()), m) is False
    m2 = {**m, "lat_ms": None}
    c2 = TS.judge("a", m2, {"max_latency_ms": 60})
    assert c2[0]["pass"] is None and TS.app_pass(c2, m2) is None


def test_alone_together_fill_are_one_result():
    p = tsprof(("trip", "nms"), dur=3, alone_s=5, fill=True,
               targets={"trip": {"max_loss_pct": 5, "max_latency_ms": 500, "max_jitter_ms": 500}, "nms": {"max_loss_pct": 5, "max_latency_ms": 500}})
    p["gen"]["testset"]["alone_s"] = 5
    run, ev, s = go(p)
    assert run.status == "finished", run.error
    ts = s["testset"]
    assert [x["kind"] for x in ts["steps"]] == ["alone", "alone", "together", "fill"] and all(x["ran"] for x in ts["steps"])
    assert [a["name"] for a in ts["apps"]] == ["Transfer trip", "NMS (SNMP polling)"]
    for a in ts["apps"]:
        assert a["alone"] and a["together"] and a["fill"]
        assert a["alone"]["tx_pkts"] > 0 and a["together"]["rx_pkts"] > 0 and a["checks"]
    assert s["verdict"]["pass"] is True, s["verdict"]
    assert [x["name"] for x in s["streams"]] == ["Transfer trip", "NMS (SNMP polling)"]       # the stored per-stream table is the together step
    # one done event; the timer counts the whole test; live numbers carry the step
    assert [e["ev"] for e in ev].count("done") == 1 and [e["ev"] for e in ev].count("started") == 1
    g = [e for e in ev if e.get("ev") == "gstate" and e.get("testset")]
    assert g and {e["testset"]["kind"] for e in g} >= {"alone", "together"}
    assert max(e["elapsed_s"] for e in g) > 5 and g[0]["total_s"] == run.planned_total_s()
    assert any(x["i"] == 1 for e in g if e["testset"]["kind"] == "together" for x in e.get("streams_now", []))


def test_a_missed_target_fails_with_a_plain_reason():
    p = tsprof(("trip",), dur=2, targets={"trip": {"min_throughput_mbps": 50.0, "max_loss_pct": 5}})
    run, ev, s = go(p)
    assert s["verdict"]["pass"] is False
    assert any("at least 50 Mb/s" in r for r in s["verdict"]["reasons"])
    assert [x["kind"] for x in s["testset"]["steps"]] == ["together"]          # one application: no 'alone' step


def test_stop_during_alone_is_not_judged():
    p = tsprof(("trip", "nms"), dur=3, alone_s=30)
    run = TS.TestSetRun(p, G.Node(), emit=lambda e: None, remote=Fake())
    t = threading.Thread(target=lambda: setattr(run, "result", run.run()), daemon=True); t.start()
    assert wait_for(lambda: run.cur is not None and run.cur.status == "running")
    run.stop(); t.join(15)
    s = run.result
    assert run.status == "stopped" and s["verdict"]["pass"] is False and "stopped" in s["verdict"]["reasons"][0]


def test_switch_off_an_application_in_the_together_step():
    p = tsprof(("trip", "nms"), dur=6, alone_s=5)
    p["gen"]["testset"]["alone"] = False
    ev = []
    run = TS.TestSetRun(p, G.Node(), emit=ev.append, remote=Fake())
    t = threading.Thread(target=lambda: setattr(run, "result", run.run()), daemon=True); t.start()
    assert wait_for(lambda: run.cur is not None and run.cur.status == "running")
    run.set_stream(1, False)
    assert wait_for(lambda: any(not x["on"] for e in ev if e.get("ev") == "gstate" for x in e.get("streams_now", []) if x["i"] == 1))
    run.stop(); t.join(15)


def test_report_has_the_per_application_table_and_sign_off(tmp_path):
    from traffic_tester.report import build_report
    from traffic_tester.store import Store
    p = tsprof(("trip", "nms"), dur=2, alone_s=5, targets={"trip": {"max_loss_pct": 5, "max_latency_ms": 500}, "nms": {"max_loss_pct": 5}})
    run, ev, s = go(p)
    st = Store(str(tmp_path))
    rid = st.create_run(st.current_session()["id"], p, "me", "x")
    st.finish_run(rid, "finished", s)
    pdf = build_report(st, [rid], "Test set report", pi_name="lab")
    assert pdf[:4] == b"%PDF"
    from tests.test_no_roles_wording import _pdf_text
    txt = _pdf_text(pdf)
    assert "Applications against their targets" in txt and "Sign-off" in txt and "Transfer trip" in txt and "Tested by" in txt
