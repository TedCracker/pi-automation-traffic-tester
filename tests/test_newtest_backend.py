"""New test page, backend side: many Pis in one trip test (one -> many, many -> one), the port list, the chosen network port, Mark / since mark,
the tester lamp, and the live numbers of every link."""
import collections, socket, time
import pytest
from fastapi.testclient import TestClient
from traffic_tester import health, marks as MK, plan as PL, profiles as P, system, transports as T
from netfix import Pi, free_udp_port


def prof(**kw):
    base = {"name": "t", "transport": "udp", "frame": "extended", "interval_ms": 20, "duration_s": 2, "timeout_ms": 400}
    base.update(kw)
    return P.validate_profile(base)


def ctx(peers=(), iface_ips=None):
    peers = list(peers)
    def resolve(ref):
        for p in peers:
            if ref in (p["id"], p["name"], p["host"]) or ref in p["ips"]:
                return p
        return None
    return PL.Context({"id": "me1", "name": "pi1", "ips": ["10.0.0.1"]}, resolve, lambda host: "10.0.0.1", lambda i: (iface_ips or {}).get(i, ""))


P2 = {"id": "p2", "name": "pi2", "host": "10.0.0.2", "port": 8080, "ips": ["10.0.0.2"]}
P3 = {"id": "p3", "name": "pi3", "host": "10.0.0.3", "port": 8080, "ips": ["10.0.0.3"]}
P4 = {"id": "p4", "name": "pi4", "host": "10.0.0.4", "port": 8080, "ips": ["10.0.0.4"]}


# ------------------------------------------------------------ planning: more than two Pis
def test_one_to_many_is_one_link_per_destination():
    legs = PL.plan_trip(prof(dest={"pi": "pi2"}, dests=[{"pi": "pi3"}, {"pi": "pi4"}]), ctx([P2, P3, P4]))
    assert [l.label for l in legs] == ["pi1 → pi2", "pi1 → pi3", "pi1 → pi4"] and [l.index for l in legs] == [0, 1, 2]
    assert all(l.tester.is_self for l in legs) and [l.data_host for l in legs] == ["10.0.0.2", "10.0.0.3", "10.0.0.4"]
    assert all(l.profile["dests"] == [] and l.profile["sources"] == [] for l in legs)        # a link is a plain two-Pi test


def test_many_to_one_and_both_ways():
    legs = PL.plan_trip(prof(source={"pi": "pi2"}, sources=[{"pi": "pi3"}], dest={"pi": "self"}), ctx([P2, P3]))
    assert [l.label for l in legs] == ["pi2 → pi1", "pi3 → pi1"] and all(l.reflector.is_self for l in legs)
    both = PL.plan_trip(prof(dest={"pi": "pi2"}, dests=[{"pi": "pi3"}], direction="both"), ctx([P2, P3]))
    assert [l.label for l in both] == ["pi1 → pi2", "pi2 → pi1", "pi1 → pi3", "pi3 → pi1"]


def test_one_way_to_many_is_judged_one_way_on_every_link():
    legs = PL.plan_trip(prof(dest={"pi": "pi2"}, dests=[{"pi": "pi3"}], direction="forward"), ctx([P2, P3]))
    assert [l.basis for l in legs] == ["up", "up"] and all(l.kind == "one_way" for l in legs)


def test_duplicates_and_self_pairs_are_dropped():
    legs = PL.plan_trip(prof(dest={"pi": "pi2"}, dests=[{"pi": "p2"}, {"host": "10.0.0.2"}, {}]), ctx([P2]))
    assert [l.label for l in legs] == ["pi1 → pi2"]
    legs = PL.plan_trip(prof(source={"pi": "self"}, sources=[{"pi": "pi2"}], dest={"pi": "self"}, dests=[{"pi": "pi2"}]), ctx([P2]))
    assert sorted(l.label for l in legs) == ["pi1 → pi2", "pi2 → pi1"]                       # every source to every destination, never a Pi to itself
    with pytest.raises(PL.PlanError, match="same Pi"):
        PL.plan_trip(prof(dest={"pi": "self"}), ctx([P2]))


def test_a_far_pi_sends_on_one_link_only():
    with pytest.raises(PL.PlanError, match="two links at once"):
        PL.plan_trip(prof(source={"pi": "pi2"}, dest={"pi": "pi3"}, dests=[{"pi": "pi4"}]), ctx([P2, P3, P4]))


def test_more_destinations_only_where_they_make_sense():
    for bad, msg in (({"transport": "serial"}, "one far end"), ({"mode": "mirrored"}, "one pair"), ({"transport": "goose_l2"}, "two Pis")):
        with pytest.raises(ValueError, match=msg):
            prof(dest={"pi": "pi2"}, dests=[{"pi": "pi3"}], **bad)
    with pytest.raises(ValueError):
        prof(dests=[{"pi": f"p{i}"} for i in range(8)])


def test_the_chosen_port_picks_the_far_address_on_its_network():
    two = {**P2, "ips": ["10.0.0.2", "192.168.50.2"]}
    legs = PL.plan_trip(prof(source={"pi": "self", "iface": "wlan0"}, dest={"pi": "pi2"}), ctx([two], {"wlan0": "192.168.50.1"}))
    assert legs[0].data_host == "192.168.50.2" and legs[0].profile["net"]["iface"] == "wlan0" and legs[0].reflect["net"]["iface"] == ""
    legs = PL.plan_trip(prof(source={"pi": "self", "iface": "auto"}, dest={"pi": "pi2"}), ctx([two], {"wlan0": "192.168.50.1"}))
    assert legs[0].data_host == "10.0.0.2" and legs[0].profile["net"]["iface"] == ""


# ------------------------------------------------------------ the port list and the port binding
def test_port_list_has_kind_state_and_address(monkeypatch):
    monkeypatch.setattr(system, "interfaces", lambda: [{"name": "eth0", "wired": True, "up": True, "mac": "aa"}, {"name": "wlan0", "wired": False, "up": True, "mac": "bb"},
                                                       {"name": "docker0", "wired": False, "up": True, "mac": "cc"}, {"name": "wlan0mon", "wired": False, "up": True, "mac": "dd"}])
    monkeypatch.setenv("TT_TEST_IFACE_IPS", "eth0=10.0.0.1")
    monkeypatch.setattr(system, "list_ports", lambda: [{"path": "/dev/ttyUSB0", "label": "FTDI FT232R", "usb": True, "kind": "usb", "debug_connector": False,
                                                        "latency_timer_ms": 16, "adapter_slow": True, "openable": True},
                                                       {"path": "/dev/ttyAMA10", "label": "Debug", "usb": False, "kind": "debug", "debug_connector": True,
                                                        "latency_timer_ms": None, "adapter_slow": False, "openable": True}])
    monkeypatch.setattr(system, "route_dev", lambda host: "wlan0")
    monkeypatch.setattr(system, "_wifi_ap", lambda n: "pi1")
    ports = system.test_ports("10.0.9.9")
    by = {p["id"]: p for p in ports}
    assert set(by) == {"eth0", "wlan0", "/dev/ttyUSB0"}                    # no virtual ports, no monitor port, no debug connector
    assert by["eth0"]["kind"] == "eth" and by["eth0"]["ip"] == "10.0.0.1" and "10.0.0.1" in by["eth0"]["state"]
    assert by["wlan0"]["kind"] == "wifi" and by["wlan0"]["you_are_here"] and "access point" in by["wlan0"]["state"]
    assert by["/dev/ttyUSB0"]["kind"] == "serial" and by["/dev/ttyUSB0"]["adapter_slow"] and "16 ms" in by["/dev/ttyUSB0"]["state"]
    assert [p["kind"] for p in ports] == ["eth", "wifi", "serial"]


def test_a_sending_socket_is_bound_to_the_chosen_port(monkeypatch):
    monkeypatch.setenv("TT_TEST_IFACE_IPS", "testif0=127.0.0.1")
    t = T.UdpTransport("tester", "127.0.0.1", free_udp_port(), iface="testif0")
    t.open()
    try:
        assert t.sock.getsockname()[0] == "127.0.0.1" and t.sock.getsockname()[1] != 0
    finally:
        t.close()
    monkeypatch.setenv("TT_TEST_IFACE_IPS", "")
    with pytest.raises(OSError, match="no IP address"):
        T.UdpTransport("tester", "127.0.0.1", free_udp_port(), iface="nosuchif0").open()
    T.UdpTransport("tester", "127.0.0.1", free_udp_port(), iface="auto").open()        # "auto" = routing decides: nothing is bound


# ------------------------------------------------------------ marks
def test_since_mark_counts_from_the_flag():
    s = MK.Since(12.34)
    for i, (st, v) in enumerate([("ok", 10.0), ("ok", 20.0), ("late", 80.0), ("lost", None)]):
        s.add({"ev": "msg", "status": st, "rtt_ms": v, "phase": "steady"})
    s.add({"ev": "msg", "status": "ok", "rtt_ms": 500.0, "phase": "idle"})           # quiet-period checks are not part of the steady numbers
    v = s.view()
    assert v == {"t_s": 12.3, "sent": 4, "lost": 1, "late": 1, "now_ms": 80.0, "p50_ms": 20.0, "p99_ms": v["p99_ms"], "max_ms": 80.0} and 70 < v["p99_ms"] <= 80
    c = MK.SinceCounters(5, {"tx_pkts": 100, "rx_pkts": 90, "lost": 10})
    assert c.view({"ev": "gstate", "tx_pkts": 150, "rx_pkts": 140, "lost": 10, "rx_mbps": 4.0, "tx_mbps": 4.0, "lat_ms": 3.0}) == {
        "t_s": 5.0, "lost": 0, "tx_pkts": 50, "rx_pkts": 50, "rx_mbps": 4.0, "tx_mbps": 4.0, "lat_max_ms": 3.0}
    marks = [{"t_s": 3.0, "name": "antenna moved"}, {"t_s": 9.0, "name": ""}]
    assert [MK.mark_at(marks, t) for t in (None, 1.0, 3.0, 8.9, 9.0)] == ["", "", "antenna moved", "antenna moved", "Mark 2"]
    assert MK.clean_name("  antenna \n moved  " + "x" * 80) == ("antenna moved " + "x" * 80)[:40]


# ------------------------------------------------------------ tester lamp
def test_lamp_reasons_name_the_pi():
    r = health.assess({"pi": "pi2", "asked_pps": 1000, "sent_pps": 610, "unit": "packets/s", "cpu": 91, "late_ms": 12, "interval_ms": 20,
                       "one_way": True, "clock_offset_ms": 4.0, "max_clock_offset_ms": 2.0, "wifi_power_save": True, "wifi_iface": "wlan0", "ftdi_latency_ms": 16, "rx_drops": 7})
    assert [x["code"] for x in r][:2] == ["rate", "rx_drops"] and all(x["pi"] == "pi2" and "pi2" in x["text"] for x in r)
    assert {x["code"] for x in r} == {"rate", "rx_drops", "late", "cpu", "clock", "wifi_ps", "ftdi"}
    assert "61 %" in r[0]["text"] and health.level_of(r) == "limit" and health.line(r)["title"] == "The Pi is the limit"
    assert health.assess({"pi": "pi1", "asked_pps": 20, "sent_pps": 19.5, "cpu": 40, "late_ms": 1.2, "interval_ms": 50, "ftdi_latency_ms": 1}) == []
    assert health.line([])["title"] == "Pi OK"


def test_lamp_keeps_the_periods_for_the_result():
    L = health.Lamp()
    late = health.assess({"pi": "pi2", "late_ms": 12, "interval_ms": 20})
    assert L.update(1, []) is not None and L.update(2, []) is None                    # only CHANGES are sent to the screen
    assert L.update(130, late)["level"] == "warn"
    for t in range(131, 146):
        L.update(t, late)
    assert L.update(146, []) is None and L.now["level"] == "warn"                     # held a moment so the line does not flicker
    assert L.update(152, [])["level"] == "ok"
    s = L.summary(600)
    assert s["level"] == "warn" and s["text"] == "pi2: sending ran late by 12 ms from 2:10 to 2:25" and len(s["periods"]) == 1
    assert health.Lamp().summary(10)["text"] == "Pi OK the whole run"
    W = health.Lamp()
    for t in range(0, 61):
        W.update(t, health.assess({"pi": "pi1", "wifi_power_save": True}))
    assert W.summary(60)["text"].endswith("the whole run")


# ------------------------------------------------------------ real apps: one -> two, marks, per-link live, the stored tester line
class Rec(collections.deque):
    def __init__(self):
        super().__init__(maxlen=8000)
        self.log = []

    def append(self, e):
        self.log.append(e)
        super().append(e)


@pytest.fixture
def pis():
    made = []
    def make(*names):
        out = [Pi(n) for n in names]
        made.extend(out)
        return out
    yield make
    for p in made:
        p.stop()


def test_one_pi_to_two_pis_marks_links_and_lamp(pis):
    a, b, c = pis("pi1", "pi2", "pi3")
    a.st.runs.ui_events = Rec()
    a.st.peers.add_manual(b.addr, "")
    a.st.peers.add_manual(c.addr, "")
    http = TestClient(a.app)
    rid = a.st.runs.start(prof(dest={"pi": "pi2", "port": free_udp_port()}, dests=[{"pi": "pi3", "port": free_udp_port()}], duration_s=4))
    ids = a.st.runs.run_ids()
    assert len(ids) == 2 and ids[0] == rid
    time.sleep(1.5)
    st = http.get("/api/status").json()["run"]
    assert [l["label"] for l in st["legs"]] == ["pi1 → pi2", "pi1 → pi3"] and st["legs"][1]["reflector"] == "pi3" and st["lamp"]["level"] in ("ok", "warn", "limit")
    assert http.post(f"/api/runs/{ids[1]}/mark", json={"name": "antenna moved"}).json()["n"] == 1         # any link's id marks the whole test
    assert http.post("/api/runs/999999/mark", json={}).status_code == 404
    time.sleep(1.2)
    live = http.get(f"/api/runs/{ids[1]}/live").json()                         # the second link has its own chart data and since-mark numbers
    assert len(live["recent"]) > 20 and live["state"]["since"]["sent"] > 10 and live["state"]["since"]["sent"] < live["state"]["sent"]
    assert live["marks"][0]["name"] == "antenna moved" and 0.5 < live["marks"][0]["t_s"] < 3.5
    assert http.get("/api/runs/999999/live").status_code == 404
    a.wait_done()
    assert http.post(f"/api/runs/{rid}/mark", json={}).status_code == 409                               # nothing running any more
    rs = a.runs()
    assert sorted(r["leg"]["label"] for r in rs) == ["pi1 → pi2", "pi1 → pi3"] and all(r["status"] == "finished" and r["verdict"] == "PASS" for r in rs)
    assert rs[0]["group_id"] == rs[1]["group_id"] == rid
    for r in rs:
        full = a.st.store.get_run(r["id"])
        assert full["summary"]["marks"][0]["name"] == "antenna moved" and full["summary"]["tester"]["title"] and "periods" in full["summary"]["tester"]
    ev = a.st.runs.ui_events.log
    done = [e for e in ev if e.get("ev") == "done"]
    assert len(done) == 1 and len(done[0]["legs"]) == 2 and done[0]["legs"][1]["sent"] > 50 and done[0]["legs"][1]["p99_ms"] is not None
    assert [e["name"] for e in ev if e.get("ev") == "mark"] == ["antenna moved"]
    assert any(e.get("ev") == "legstate" and e.get("since") for e in ev) and any(e.get("ev") == "state" and e.get("since") for e in ev)
    assert b.st.reflectors.status()["items"] == [] and c.st.reflectors.status()["items"] == []


def test_two_pis_send_to_one(pis, monkeypatch):
    a, b, c = pis("pi1", "pi2", "pi3")
    a.st.peers.add_manual(b.addr, "")
    a.st.peers.add_manual(c.addr, "")
    a.st.runs.start(prof(source={"pi": "pi2"}, sources=[{"pi": "pi3"}], dest={"pi": "self"}, net={"port": free_udp_port()}))
    a.wait_done()
    rs = a.runs()
    assert sorted(r["leg"]["label"] for r in rs) == ["pi2 → pi1", "pi3 → pi1"] and all(r["status"] == "finished" and r["verdict"] == "PASS" for r in rs)
    assert len(b.runs()) == 1 and len(c.runs()) == 1


def test_health_page_and_port_list_over_http(pis):
    a, b = pis("pi1", "pi2")
    a.st.peers.add_manual(b.addr, "")
    http = TestClient(a.app)
    h = http.get("/api/peer/health").json()
    assert h["pi"] == "pi1" and isinstance(h["reasons"], list)
    d = http.get("/api/testports").json()
    assert d["pi"]["name"] == "pi1" and isinstance(d["ports"], list) and "ok" in d["goose_raw"]
    far = http.get(f"/api/peers/{b.st.me.id}/testports").json()
    assert far["pi"]["name"] == "pi2"



def test_csv_mark_column_and_pdf_name_the_marks(tmp_path):
    """M2: every CSV row says which mark was in force ("" before the first); the PDF lists the marks and the tester line."""
    from traffic_tester.store import Store
    from traffic_tester import report
    from tests._cmpfix import trip_run
    st = Store(str(tmp_path))
    rid = trip_run(st)
    r = st.get_run(rid); s = r["summary"]
    s["marks"] = [{"n": 1, "t_s": 5.0, "name": "antenna moved", "at": ""}]
    s["tester"] = {"level": "warn", "title": "Check the Pi", "text": "pi1 sending late", "periods": []}
    st.finish_run(rid, "finished", s)
    rows = st.messages_csv(rid).splitlines()
    assert rows[0].endswith(",mark")
    assert rows[1].endswith(",")                                   # t = 0 s: before the mark
    assert rows[-1].endswith(",antenna moved")                     # t = 9.95 s: after it
    pdf = report.build_report(st, [rid])
    assert pdf[:4] == b"%PDF"
    paras = [p.text for p in report._tester_marks_para(st.get_run(rid))]
    assert any("Check the Pi" in t for t in paras) and any("Mark 1 at 5.0 s (antenna moved)" in t for t in paras)
