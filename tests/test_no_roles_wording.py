"""No fixed roles: nothing a user can read says Pi A / Pi B / Tester / Responder / partner; reports, exports and the 'update another Pi' call name the Pis."""
import base64
import io
import json
import re
import tempfile
import zlib

import pytest
from fastapi.testclient import TestClient

from traffic_tester import profiles as P
from traffic_tester.report import build_report
from traffic_tester.server import create_app
from traffic_tester.store import Store

OLD = re.compile(r"\bPi [AB]\b|\bResponder\b|\bpartner\b|\bTester\b(?! ?\()|\bA to B\b|\bB to A\b|Pi B's|Pi A's", re.I)


def _texts(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield from _texts(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from _texts(v)


def test_trip_info_and_presets_use_the_new_words():
    bad = [t[:100] for t in _texts(P.TRIP_INFO) if OLD.search(t.replace("Pi Automation Traffic Tester", ""))]
    assert not bad, bad
    bad = [t[:100] for t in _texts(P.builtin_presets()) if OLD.search(t.replace("Pi Automation Traffic Tester", ""))]
    assert not bad, bad
    info = P.TRIP_INFO["DER trip test (IP)"]
    assert "From → To" in info["need"] and "port 20500" in info["need"] and "needs nothing set up" in info["need"]
    assert "the sending Pi" in info["how"]


def test_generator_preset_stream_names_say_from_and_to():
    g = next(p for p in P.generator_presets() if p["name"] == "SCADA / DNP3 polling")
    names = [s["name"] for s in g["gen"]["streams"]]
    assert names == ["Poll request (from → to)", "Poll response (to → from)"]
    assert [s["direction"] for s in g["gen"]["streams"]] == ["a_to_b", "b_to_a"]          # direction semantics unchanged


def _pdf_text(pdf: bytes) -> str:
    out = b""
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        try:
            out += zlib.decompress(base64.a85decode(m.group(1).strip(), adobe=True))
        except Exception:
            pass
    return out.decode("latin-1")


def _trip_run(st, sid, name, leg, gid=None, verdict=True):
    prof = P.validate_profile({"name": "Quick link check", "transport": "udp", "net": {"peer_host": "10.0.0.2"}})
    rid = st.create_run(sid, prof, leg["tester"], "udp:20500", group_id=gid, leg=leg)
    summary = {"sent": 100, "received": 100, "lost": 0, "loss_pct": 0, "over_threshold": 0, "rtt_ms": {"p50": 1.0, "p95": 1.5, "p99": 2.0, "max": 3.0}, "jitter_ms": 0.1,
               "verdict": {"pass": verdict, "reasons": []},
               "endpoints": {"source": leg["tester"], "dest": leg["reflector"], "kind": leg["kind"], "basis": leg["basis"], "controller": "lab-one", "address": "10.0.0.2", "label": leg["label"]}}
    st.finish_run(rid, "finished", summary)
    return rid


def test_report_and_csv_name_the_pis():
    d = tempfile.mkdtemp()
    st = Store(d)
    sid = st.current_session()["id"]
    l1 = {"label": "lab-one → lab-two", "kind": "round_trip", "basis": "rtt", "tester": "lab-one", "reflector": "lab-two", "controller": "lab-one"}
    l2 = {"label": "lab-two → lab-one", "kind": "one_way", "basis": "up", "tester": "lab-two", "reflector": "lab-one", "controller": "lab-one"}
    r1 = _trip_run(st, sid, "a", l1, gid=7)
    r2 = _trip_run(st, sid, "b", l2, gid=7)
    txt = _pdf_text(build_report(st, [r1, r2], "Report", pi_name="lab-one"))
    assert "Sent by" in txt and "lab-one" in txt and "lab-two" in txt and "answered by" in txt and "group 7" in txt
    assert not OLD.search(txt.replace("Pi Automation Traffic Tester", "")), OLD.search(txt)
    csv = st.messages_csv(r1).splitlines()[0]
    assert csv.startswith("seq,phase,idle_gap_s,t_send_ms") and csv.endswith("group_id,leg,sender,answering_pi,basis,radio_recorded,radio_char_timeout_ms,radio_max_packet_bytes,radio_serial_mode,radio_note,started_local,ended_local,time_tag,mark")
    assert st.intervals_csv(r2).splitlines()[0].endswith("group_id,leg,sender,answering_pi,basis,radio_recorded,radio_char_timeout_ms,radio_max_packet_bytes,radio_serial_mode,radio_note,started_local,ended_local,time_tag,mark")
    st.add_messages(r2, [{"seq": 1, "phase": "main", "idle_gap_s": None, "t_send_ms": 0, "rtt_us": 1000, "up_us": 500, "down_us": 500, "status": "ok"}])
    row = st.messages_csv(r2).splitlines()[1]
    assert row.find(",7,lab-two → lab-one,lab-two,lab-one,up,") > 0, row
    js = st.run_json(r2)
    assert js["group_id"] == 7 and js["leg"]["label"] == "lab-two → lab-one" and js["summary"]["endpoints"]["dest"] == "lab-one"


def test_report_generator_lists_pairs_and_streams_by_name():
    d = tempfile.mkdtemp()
    st = Store(d)
    sid = st.current_session()["id"]
    prof = P.validate_profile({"name": "Mesh", "kind": "generator", "gen": {"streams": [{"name": "s1"}, {"name": "s2"}]}})
    rid = st.create_run(sid, prof, "lab-one", "")
    streams = [{"id": i, "name": f"s{i + 1}", "type": "udp", "direction": "a_to_b", "src": src, "dst": dst, "size": "1000 B", "tx": {"mbps": 5, "pkts": 100, "bytes": 1}, "rx": {"mbps": 5, "pps": 100, "unique": 100},
                "loss_pct": 0, "lost": 0, "lat_ms": None, "jitter_ms": None, "rtt_ms": None, "warnings": []} for i, (src, dst) in enumerate([("lab-one", "lab-two"), ("lab-three", "lab-two")])]
    pairs = [{"src": "lab-one", "dst": "lab-two", "streams": 1, "tx_pkts": 100, "rx_pkts": 100, "lost": 0, "loss_pct": 0, "tx_mbps": 5, "rx_mbps": 5, "jitter_ms": None, "lat_ms": None},
             {"src": "lab-three", "dst": "lab-two", "streams": 1, "tx_pkts": 100, "rx_pkts": 100, "lost": 0, "loss_pct": 0, "tx_mbps": 5, "rx_mbps": 5, "jitter_ms": None, "lat_ms": None}]
    st.finish_run(rid, "finished", {"kind": "generator", "streams": streams, "pairs": pairs, "nodes": ["lab-one", "lab-two", "lab-three"], "duration_s": 5,
                                    "totals": {"tx_pkts": 200, "rx_pkts": 200, "lost": 0, "loss_pct": 0, "tx_mbps": 10, "rx_mbps": 10, "lat_ms": None}, "clocks": {"valid": False},
                                    "verdict": {"pass": True, "reasons": []}})
    txt = _pdf_text(build_report(st, [rid], "Mesh report"))
    assert "Pis taking part" in txt and "Each pair of Pis" in txt and "lab-three" in txt and "lab-one" in txt
    assert not re.search(r"A>B|B>A", txt)
    assert st.gen_csv(rid).splitlines()[0].endswith("from_pi,to_pi,mark")


@pytest.fixture()
def client():
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        yield c


def test_update_another_pi_takes_a_peer_from_the_list(client):
    assert client.post("/api/update/partner", json={"peer": "nope"}).status_code == 404
    assert client.post("/api/update/partner", json={"host": ""}).status_code == 422
    assert "Pi to update" in client.post("/api/update/partner", json={"host": ""}).json()["detail"]
    reg = client.app.state.tt.peers if hasattr(client.app.state, "tt") else None
    if reg is None:
        pytest.skip("app state not reachable")
    reg.manual.append({"id": "abc12345", "name": "lab-two", "host": "127.0.0.1", "port": 9, "password": "pw"})
    r = client.post("/api/update/partner", json={"peer": "abc12345"})
    assert r.status_code == 409 and "No update is ready" in r.json()["detail"]        # got past the peer lookup; nothing staged yet


def test_settings_wording_has_no_old_role_words():
    for f in ("config.js", "update.js", "peers.js"):
        t = open("traffic_tester/static/" + f, encoding="utf-8").read()
        m = re.search(r"\bPi [AB]\b|\bResponder\b|[Pp]artner Pi|\bTester ·", t)
        assert not m, (f, m.group(0))


# ---------------------------------------------------------------- discovery robustness (found by the network-split e2e)
def test_probe_tries_the_other_addresses_of_a_pi(tmp_path):
    from traffic_tester import peers as PE
    st = Store(str(tmp_path))
    me = PE.Identity(st)
    reg = PE.PeerRegistry(st, me)
    reg.add_mdns({"id": "p2", "name": "pi2", "host": "192.168.51.10", "port": 8080, "ips": ["192.168.51.10", "10.50.0.2"], "version": "1", "source": "mdns"})
    good = {"app": PE.APP_ID, "id": "p2", "name": "pi2", "version": "1", "ips": ["10.50.0.2"], "port": 8080}
    reg._fetch_info = lambda host, port: (good, "") if host == "10.50.0.2" else (None, f"Cannot reach {host}")
    e = reg.probe("p2")
    assert e["reachable"] and e["host"] == "10.50.0.2"          # the address that answers is kept


def test_mdns_instance_name_carries_the_pi_id(tmp_path):
    pytest.importorskip("zeroconf")
    from traffic_tester import peers as PE
    st = Store(str(tmp_path))
    me = PE.Identity(st)
    me.set_name("same name")
    d = PE.Discovery(me, PE.PeerRegistry(st, me))
    info = d._service_info()
    assert me.id in info.name and info.name.endswith(PE.SERVICE_TYPE)
