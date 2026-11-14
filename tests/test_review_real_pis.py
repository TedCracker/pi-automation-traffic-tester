"""Found on the real Pis (2026-11-13, generator + sniffer over the direct cable): regression tests."""
import os

from traffic_tester import system


def _port(root, name, type_="1", wireless=False, mac="02:00:00:00:00:01", state="up"):
    d = root / name
    d.mkdir()
    (d / "type").write_text(type_ + "\n")
    (d / "address").write_text(mac + "\n")
    (d / "operstate").write_text(state + "\n")
    if wireless:
        (d / "wireless").mkdir()


def test_tunnel_device_is_not_a_wired_ethernet_port(tmp_path, monkeypatch):
    """tail0 (type 65534, no MAC) was offered as a wired Ethernet / capture / generator port next to eth0."""
    _port(tmp_path, "eth0")
    _port(tmp_path, "wlan0", wireless=True)
    _port(tmp_path, "tail0", type_="65534", mac="")
    _port(tmp_path, "tun0", type_="65534", mac="")
    _port(tmp_path, "veth9", type_="1")
    monkeypatch.setattr(system, "NET_ROOT", str(tmp_path))
    ifs = {i["name"]: i for i in system.interfaces()}
    assert ifs["eth0"]["wired"] is True
    assert not ifs["wlan0"]["wired"] and not ifs["veth9"]["wired"]
    assert ifs["tail0"]["wired"] is False and ifs["tun0"]["wired"] is False
    assert [i["name"] for i in system.interfaces() if i["wired"]] == ["eth0"]


def test_ethernet_port_with_unreadable_type_still_counts_as_wired(tmp_path, monkeypatch):
    d = tmp_path / "eth1"
    d.mkdir()
    (d / "operstate").write_text("up\n")
    monkeypatch.setattr(system, "NET_ROOT", str(tmp_path))
    assert system.interfaces()[0]["wired"] is True


def test_generator_and_capture_refuse_a_bad_link_preset_instead_of_ignoring_it():
    """A generator profile carrying impairment="Bad cell" used to run perfectly clean (no netem, no message). It is refused now."""
    import tempfile
    from fastapi.testclient import TestClient
    from traffic_tester.server import create_app
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        g = next(p for p in c.get("/api/profiles").json() if p["builtin"] and p["kind"] == "generator")
        g.pop("id")
        r = c.post("/api/runs", json={"profile": {**g, "impairment": "Bad cell"}})
        assert r.status_code == 422 and "trip tests only" in r.json()["detail"] and "Bad cell" in r.json()["detail"]
        s = next(p for p in c.get("/api/profiles").json() if p["builtin"] and p["kind"] == "sniffer")
        s.pop("id")
        r = c.post("/api/runs", json={"profile": {**s, "impairment": "Good cell"}})
        assert r.status_code == 422 and "trip tests only" in r.json()["detail"]
        assert c.get("/api/status").json()["run"]["running"] is False and c.get("/api/runs").json() == []     # nothing was recorded



def test_a_running_capture_does_not_stop_this_pi_from_being_the_far_end_of_a_generator_test():
    """Real Pis 2026-11-13: with a SPAN capture running on pi2, pi1's generator test failed with 'pi2 is already running a test'."""
    from types import SimpleNamespace as NS
    from traffic_tester.runmgr import RunManager

    class Alive:
        def is_alive(self):
            return True
    rm = RunManager.__new__(RunManager)
    rm.legs = [NS(thread=Alive(), done=False, profile={"kind": "sniffer"})]
    assert rm.busy() and not rm.busy_except_capture()
    rm.legs = [NS(thread=Alive(), done=False, profile={"kind": "generator"})]
    assert rm.busy() and rm.busy_except_capture()
    rm.legs = [NS(thread=Alive(), done=True, profile={"kind": "trip"})]
    assert not rm.busy() and not rm.busy_except_capture()
    rm.legs = [NS(thread=None, done=False, profile={"kind": "trip"})]
    assert not rm.busy_except_capture()


def test_gen_node_status_is_not_busy_while_only_a_capture_runs():
    import tempfile
    from types import SimpleNamespace as NS
    from fastapi.testclient import TestClient
    from traffic_tester.server import create_app

    class Alive:
        def is_alive(self):
            return True
    app = create_app(tempfile.mkdtemp(), "A")
    with TestClient(app) as c:
        rm = next(v for v in app.router.routes if getattr(v, "path", "") == "/api/gen/node/status").endpoint.__closure__
        # reach the app state through the closure of the route (the same object the server uses)
        st = next(cell.cell_contents for cell in rm if hasattr(cell.cell_contents, "runs"))
        st.runs.legs = [NS(thread=Alive(), done=False, profile={"kind": "sniffer"})]
        assert c.get("/api/gen/node/status").json()["busy"] is False
        st.runs.legs = [NS(thread=Alive(), done=False, profile={"kind": "trip"})]
        assert c.get("/api/gen/node/status").json()["busy"] is True
        assert c.post("/api/gen/node/rx/start", json={"streams": [], "interface": "auto"}).status_code == 409
        st.runs.legs = []


def test_inline_tap_never_picks_a_vpn_device_as_the_second_port(tmp_path, monkeypatch):
    """Real pi2 (one Ethernet port + tail0): the inline tap chose tail0 as leg B, built the bridge, enslaved eth0 and failed on
    'ip link set dev tail0 master ttbr0'. With no second Ethernet port it must refuse up front ('needs two different network ports')."""
    from traffic_tester import profiles as P
    from traffic_tester.sniff import preflight as pre
    _port(tmp_path, "eth0")
    _port(tmp_path, "wlan0", wireless=True)
    _port(tmp_path, "tail0", type_="65534", mac="")
    monkeypatch.setattr(system, "NET_ROOT", str(tmp_path))
    ports = [{**i, "usb": False} for i in system.interfaces()]
    prof = P.validate_profile({"name": "t", "kind": "sniffer", "sniff": {"mode": "eth_bridge"}})
    assert pre.bridge_legs(prof, ports) == ("eth0", None)
    assert pre.span_iface(P.validate_profile({"name": "t", "kind": "sniffer", "sniff": {"mode": "eth_span"}}), ports) == "eth0"


def _sniffer_run_with_drops(drops):
    import tempfile
    from traffic_tester import profiles as P
    from traffic_tester.store import Store
    s = Store(tempfile.mkdtemp())
    prof = next(p for p in P.builtin_presets() if p["kind"] == "sniffer")
    rid = s.create_run(1, prof, "A", "eth_span")
    s.finish_run(rid, "stopped", {"kind": "sniffer", "mode": "eth_span", "stop_reason": "user", "location": "/tmp/x", "elapsed_s": 6.0, "files": [{"name": "capture.pcapng", "bytes": 1000}],
                                  "engine": {"engine": "dumpcap", "kernel_received": 217610, "kernel_dropped": drops, "packets_captured": 217610},
                                  "analysis": {"packets": 217610, "bytes": 1, "duration_s": 6, "protocols": {"UDP": 217610}, "top_talkers": [], "errors": 0}})
    return s, rid


def test_report_warns_when_a_capture_lost_packets():
    """Real Pis: a 450 Mb/s SPAN capture lost 9 % of the packets; the PDF only had a small 'packets the kernel dropped' line."""
    from traffic_tester import report
    from _cmpfix import pdf_text
    s, rid = _sniffer_run_with_drops(11178)
    txt = " ".join(pdf_text(report.build_report(s, [rid], "Capture")).split())
    assert "The capture is not complete: the system dropped 11,178 packets" in txt
    s, rid = _sniffer_run_with_drops(0)
    assert "not complete" not in " ".join(pdf_text(report.build_report(s, [rid], "Capture")).split())


def test_capture_that_lost_packets_does_not_end_with_a_pi_ok_lamp():
    from types import SimpleNamespace as NS
    from traffic_tester import health
    from traffic_tester.runmgr import RunManager
    rm = RunManager.__new__(RunManager)
    rm.lamp = health.Lamp()
    rm.services = NS(me=lambda: {"name": "pi2", "id": "x"})
    rm.last_gstate = None
    rm._final_health({"kind": "sniffer"}, {"engine": {"kernel_dropped": 11178}, "elapsed_s": 6.0})
    t = rm.lamp.summary(6.0)
    assert t["level"] == "limit" and "pi2" in t["text"] and "11,178" in t["text"]
    rm.lamp = health.Lamp()
    rm._final_health({"kind": "sniffer"}, {"engine": {"kernel_dropped": 0}, "elapsed_s": 6.0})
    assert rm.lamp.summary(6.0)["level"] == "ok"


def test_results_page_js_shows_the_capture_loss_warning_at_the_top():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "traffic_tester", "static", "results.js")).read()
    assert 'data-testid="capture-lost"' in src and "${banner}${lost}" in src and "s.engine.kernel_dropped > 0" in src
