"""Regression tests for docs/REVIEW-1.1.2.md (Fable 5.2 review of 1.1.2 -> 1.3.2). One test per fixed finding; the id is in the name."""
import inspect, json, os, socket, tempfile, time, urllib.error, urllib.request
import pytest
from fastapi.testclient import TestClient

from traffic_tester import clock as CK, orchestrate as OR, peers as PE, profiles as P, reflector as RF, remote as RM, runmgr
from traffic_tester.mbemu.status import Channel, Settings, NS
from traffic_tester.server import create_app
from traffic_tester.store import Store
from netfix import Pi, free_udp_port

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def pis():
    made = []

    def make(*names, password=None):
        out = []
        for n in names:
            p = Pi(n, password=password if n == names[-1] and password else None)
            made.append(p)
            out.append(p)
        return out
    yield make
    for p in made:
        p.stop()


def http(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ------------------------------------------------------------ R1
def test_r1_peer_logs_in_again_after_the_other_pi_restarted(pis):
    a, b = pis("pi1", "pi2", password="s3cret")
    e = a.st.peers.add_manual(b.addr, "s3cret")
    c = a.st.peers.client(e)
    assert isinstance(c.get("/api/ports"), (dict, list))
    b.st.tokens.clear()                                    # what a restart of pi2 does: its sessions live in memory
    assert isinstance(c.get("/api/ports"), (dict, list))  # was: PeerAuthError "asks for a password" for ever
    assert a.st.peers.probe(e["id"])["auth"] == "ok"
    b.st.store.set_setting("password_hash", b.st.hash_pw("other"))
    b.st.tokens.clear()
    with pytest.raises(PE.PeerAuthError, match="did not accept the saved password"):
        c.get("/api/ports")


# ------------------------------------------------------------ R2
def test_r2_owner_names_the_controller():
    class Me:
        id, name = "ab12cd34", "pi1"

        @staticmethod
        def ips():
            return []
    o = OR.new_owner(OR.Services(identity=Me()))
    assert o.startswith("pi1:ab12cd34:") and RF.controller_of(o) == "pi1:ab12cd34" and RF.controller_of(o + "-probe") == "pi1:ab12cd34"
    assert RF.controller_of("legacy") == "" and RF.controller_of("pi1:abc123") == ""      # older owners say nothing: never taken over


def test_r2_reflector_frees_what_the_same_controller_left_behind():
    svc = RF.ReflectorService(hooks=True)
    cfg = {"transport": "udp", "net": {"port": free_udp_port()}}
    try:
        svc.ensure(cfg, "pi1:aa11:t1", 5.0)                # not shared (a test hook is set): the port belongs to this test
        with pytest.raises((RF.Busy, OSError)):            # another Pi cannot have it
            svc.ensure(cfg, "pi3:cc33:t9", 5.0)
        assert [sorted(e.owners) for e in svc.entries.values()] == [["pi1:aa11:t1"]]
        d = svc.ensure(cfg, "pi1:aa11:t2", 5.0)            # pi1 lost contact before it could release t1 and now starts its next test
        assert d["running"] and [sorted(e.owners) for e in svc.entries.values()] == [["pi1:aa11:t2"]]
        assert svc.release("pi1:aa11:t2") == 1 and not svc.entries
    finally:
        svc.stop_all()


# ------------------------------------------------------------ R3 + R14
def _rx_stream():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return {"id": 0, "name": "s0", "type": "udp", "rate": {"pps": 100}, "size": {"mode": "fixed", "size": 200}, "burst_packets": 1, "burst_gap_ms": 0,
            "duration_s": 2, "rtt_probe_pps": 0, "dscp": 0, "ttl": 64, "dst_ip": "127.0.0.1", "dst_port": port, "seed": 1}


def test_r3_far_generator_end_frees_itself_when_its_controller_is_gone():
    app = create_app(tempfile.mkdtemp())
    st = app.state.tt
    with TestClient(app) as c:
        assert c.post("/api/gen/node/rx/start", json={"streams": [_rx_stream()], "interface": "lo"}).status_code == 200
        try:
            assert c.get("/api/gen/node/status").json()["busy"] and c.get("/api/peer/info").json()["busy"]
            assert st.updates.busy()                       # R14: no software update in the middle of another Pi's test
            st.gen_touched = time.monotonic() - 60         # a controller that is only slow keeps its far end
            assert c.get("/api/gen/node/status").json()["busy"]
            c.get("/api/gen/node/live")                    # ... and every question from it counts as a sign of life
            assert time.monotonic() - st.gen_touched < 5
            st.gen_touched = time.monotonic() - st.GEN_ORPHAN_S - 1
            assert c.get("/api/gen/node/status").json()["busy"] is False
            assert not st.gen_node.busy() and not st.updates.busy()
        finally:
            st.gen_node.stop()


# ------------------------------------------------------------ R4
class _FakeStore(dict):
    def get_setting(self, k, d=None):
        return self.get(k, d)

    def set_setting(self, k, v):
        self[k] = v


class _FakePeers:
    def __init__(self, peers):
        self.peers = peers

    def list(self):
        return self.peers

    def get(self, pid):
        return next((p for p in self.peers if p["id"] == pid), None)


def test_r4_time_source_is_not_changed_for_one_missed_probe_or_during_a_test():
    class Me:
        id, name, setup_at = "a", "pi1", "2026-11-02T00:00:00"
    peer = {"id": "b", "name": "pi2", "host": "10.0.0.2", "ips": ["10.0.0.2"], "reachable": True, "setup_at": "2026-11-01T00:00:00", "last_seen": time.time()}
    calls = []
    cm = CK.ClockManager(_FakeStore(), Me(), _FakePeers([peer]), lambda mode, server: calls.append((mode, server)) or {"ok": True})
    cm.reconcile()
    assert calls == [("client", "10.0.0.2")]
    peer.update(reachable=False, last_seen=time.time() - 30)           # one missed probe
    r = cm.reconcile()
    assert calls == [("client", "10.0.0.2")] and r["result"].get("held")
    peer.update(last_seen=time.time() - CK.HOLD_S - 5)                 # really gone
    assert cm.reconcile(busy=True)["result"].get("held") and len(calls) == 1      # ... but a test is running: later
    cm.reconcile()
    assert calls[-1] == ("serve", None) and len(calls) == 2
    # a set-up time that is not a timestamp never makes a device the time source
    liar = {**peer, "reachable": True, "setup_at": "0", "last_seen": time.time()}
    assert CK.decide({"id": "a", "name": "pi1", "setup_at": Me.setup_at}, [liar], "auto")["mode"] == "serve"


# ------------------------------------------------------------ R5
def test_r5_stop_and_delete_work_with_the_id_of_any_link(pis):
    a, b = pis("pi1", "pi2")
    a.st.peers.add_manual(b.addr)
    a.st.runs.start(P.validate_profile({"name": "t", "transport": "udp", "frame": "extended", "interval_ms": 20, "duration_s": 30, "timeout_ms": 400,
                                        "dest": {"pi": "pi2"}, "direction": "both", "net": {"port": free_udp_port()}}))
    ids = a.st.runs.run_ids()
    assert len(ids) == 2
    time.sleep(1.0)
    assert http("DELETE", f"{a.url}/api/runs/{ids[1]}")[0] == 409      # was: deleted under the running test
    assert a.st.store.get_run(ids[1]) is not None
    assert http("POST", f"{a.url}/api/runs/{ids[1]}/stop", {})[0] == 200
    a.wait_done(15)                                                     # was: nothing happened, the test ran its 30 s
    assert all(a.st.store.get_run(i)["status"] == "stopped" for i in ids)


# ------------------------------------------------------------ R6
class _FakeClient:
    def __init__(self, tester_ref):
        self.calls, self.ref, self.run_gets = [], tester_ref, 0

    def post(self, path, body=None, timeout=8):
        self.calls.append(("POST", path))
        if path == "/api/runs":
            self.ref[0].stop_evt.set()                    # Stop is pressed while the start request is on its way
            return {"run_id": 7}
        return {}

    def get(self, path, timeout=8):
        self.calls.append(("GET", path))
        if path.endswith("/live"):
            e = PE.PeerError("not running")
            e.status = 404
            raise e
        if path == "/api/runs/7":
            self.run_gets += 1
            if self.run_gets == 1:
                raise PE.PeerError("Cannot reach pi2: timed out")      # one blip at the very end
            return {"status": "stopped", "summary": {"verdict": {"pass": True}}, "intervals": []}
        return []


def test_r6_remote_leg_passes_a_late_stop_on_and_survives_a_blip_at_the_end(monkeypatch):
    monkeypatch.setattr(RM.RemoteTester, "POLL_S", 0.01)
    ref = [None]
    c = _FakeClient(ref)
    t = RM.RemoteTester({"one_way": False}, c, "pi2")
    ref[0] = t
    s = t.run()
    assert ("POST", "/api/runs/7/stop") in c.calls
    assert t.status == "stopped" and t.error is None and s["verdict"]["pass"] and c.run_gets == 2


# ------------------------------------------------------------ R7
def test_r7_what_other_devices_announce_is_cleaned():
    props = {b"app": b"pi-traffic-tester", b"id": b"ab12 <x>", b"name": b"=HYPERLINK(\"http://x\")<script>", b"version": b"1.2.0<b>", b"ips": b"10.0.0.5,evil.example/x?,999.1.1.1"}
    d = PE.peer_from_service("x._pitester._tcp.local.", props, ["10.0.0.5", "not-an-ip"], 8080, ["10.0.0.9"])
    assert d["ips"] == ["10.0.0.5"] and d["host"] == "10.0.0.5" and d["id"] == "ab12x" and d["version"] == "1.2.0b"
    assert d["name"] == "HYPERLINKhttpxscript" and PE.NAME_RE.fullmatch(d["name"])
    i = PE.clean_info({"app": "pi-traffic-tester", "id": "ab12", "name": "@SUM(1+1)|pi 2", "ips": ["10.0.0.5", "a;b"], "setup_at": "0", "version": "1.3.2", "hardware": "Raspberry Pi 5 <img>",
                       "clock": "x", "busy": True})
    assert i["name"] == "SUM1+1pi 2".replace("+", "") and i["ips"] == ["10.0.0.5"] and i["setup_at"] is None and i["clock"] is None and i["busy"] is True
    assert i["hardware"] == "Raspberry Pi 5 img"
    assert PE.clean_info({"id": "x", "name": "pi2", "setup_at": "2026-11-01T10:00:00"})["setup_at"] == "2026-11-01T10:00:00"


# ------------------------------------------------------------ R8, R9, R10 (docs/MIRRORED-BITS-SPEC.md 3.3 and 4)
def test_r8_tx_id_and_rx_id_must_differ():
    with pytest.raises(ValueError, match="TX_ID and RX_ID must be different"):
        Settings.from_dict({"tx_id": 3, "rx_id": 3})
    Settings.from_dict({"tx_id": 3, "rx_id": 3, "loopback_test": True})   # loop-back test mode expects its own ID
    with pytest.raises(ValueError):
        P.validate_profile({"name": "m", "transport": "udp", "mode": "mirrored", "mirrored": {"settings": {"tx_id": 5, "rx_id": 5}}, "net": {"peer_host": "10.0.0.2"}})


def test_r9_rok_needs_n_consecutive_good_messages():
    s = Settings.from_dict({"baud": 9600, "rok_n": 2})
    ch = Channel(s, 0)
    n = ch.nominal_ns
    t = n
    ch.good(t)
    assert ch.rok == 0 and ch.ok_run == 1
    t += ch.late_ns + 5 * n                                # a message went missing while ROK was 0
    ch.tick(t)
    assert ch.ok_run == 0
    ch.good(t)
    assert ch.rok == 0                                     # was: ROK = 1 after good, missing, good
    ch.good(t + n)
    assert ch.rok == 1


def test_r10_cbad_asserts_when_the_channel_never_comes_up():
    s = Settings.from_dict({"baud": 9600, "cbad_window_s": 5, "cbad_pu_pct": 10, "rbad_pu_s": 100})
    ch = Channel(s, 0)
    ch.tick(int(4.9 * NS))
    assert ch.cbad == 0 and ch.unavailability_pct(int(4.9 * NS)) == 0.0        # the start-up seconds do not count
    ch.tick(int(5.1 * NS))
    assert ch.cbad == 1 and ch.cbad_events == 1
    up = Channel(s, 0)                                     # a channel that comes up at once: no CBAD, as before
    t = 0
    for _ in range(50):
        t += up.nominal_ns
        up.good(t)
    up.tick(int(6 * NS) if False else t + up.nominal_ns // 2)
    assert up.rok == 1 and up.cbad == 0 and up.cbad_events == 0


# ------------------------------------------------------------ R11
def test_r11_a_refused_upload_leaves_no_temporary_file(monkeypatch):
    from tests.test_update_api import build_app
    from tests.test_update import make
    app, root, _ = build_app(monkeypatch)
    good, _ = make(tempfile.mkdtemp(), "1.1.0")
    inbox = app.state.tt.updates.inbox
    with TestClient(app) as c:
        assert c.post("/api/update/upload", content=b"this is not a bundle").status_code == 422
        assert c.post("/api/update/upload", content=b"").status_code == 422
        assert os.listdir(inbox) == []
        assert c.post("/api/update/upload", content=open(good, "rb").read()).status_code == 200
        assert os.listdir(inbox) == ["staged.ptt"]


# ------------------------------------------------------------ R12, R13
def test_r12_undo_delete_with_a_damaged_position():
    st = Store(tempfile.mkdtemp())
    p = P.validate_profile({"name": "mine", "transport": "udp"})
    for bad in ("abc", None, True, [1]):
        back = st.undelete_profile({**p, "position": bad, "id": 99})
        assert back["name"].startswith("mine")


def test_r13_no_duplicate_definitions_in_the_run_manager():
    src = inspect.getsource(runmgr)
    assert src.count("class Busy(") == 1 and src.count("class LoadGen(") == 1


def test_r16_lamp_send_rate_uses_the_test_clock_not_the_sampling_time(monkeypatch):
    """R16: the lamp's send-rate check took the message count of the last state (sent once a second) but divided by the wall-clock gap
    between its own samples: a healthy Pi read "sent only 85 % of the asked rate" whenever the two drifted apart."""
    import types, time as _t
    from traffic_tester import runmgr, health
    rm = runmgr.RunManager.__new__(runmgr.RunManager)
    rm.services = types.SimpleNamespace(me=lambda: {"name": "pi1"})
    rm.busy = lambda: True
    rm._lamp_static = lambda: {}
    monkeypatch.setattr(health, "cpu_percent", lambda *a, **k: 10.0)
    leg = types.SimpleNamespace(leg=None, profile={"kind": "trip", "interval_ms": 50}, state={"phase": "steady", "sent": 100, "elapsed_s": 5.0})
    rm.legs = [leg]
    clock = [1000.0]
    monkeypatch.setattr(runmgr.time, "monotonic", lambda: clock[0])
    assert rm.health_inputs() == []
    clock[0] += 2.4                                          # the lamp samples 2.4 s later, but the newest state is only 2 s newer: 40 messages = 20/s
    leg.state = {"phase": "steady", "sent": 140, "elapsed_s": 7.0}
    assert not [r for r in rm.health_inputs() if r["code"] == "rate"]


def test_r16b_lamp_ignores_the_last_second_of_a_trip_test(monkeypatch):
    """R16 (second half): the last state of a test counts the final, partly-sent second: no "Pi is the limit" for that."""
    import types
    from traffic_tester import runmgr, health
    rm = runmgr.RunManager.__new__(runmgr.RunManager)
    rm.services = types.SimpleNamespace(me=lambda: {"name": "pi1"})
    rm.busy = lambda: True
    rm._lamp_static = lambda: {}
    monkeypatch.setattr(health, "cpu_percent", lambda *a, **k: 10.0)
    leg = types.SimpleNamespace(leg=None, profile={"kind": "trip", "interval_ms": 20}, state={"phase": "steady", "sent": 150, "elapsed_s": 3.0, "total_s": 5.0})
    rm.legs = [leg]
    assert rm.health_inputs() == []
    leg.state = {"phase": "steady", "sent": 194, "elapsed_s": 4.0, "total_s": 5.0}      # 44 of 50 in the last second
    assert not [r for r in rm.health_inputs() if r["code"] == "rate"]


def test_r17_a_handed_over_link_does_not_prepare_the_far_end_again():
    """R17 (listed by the first review pass as a possible bug, not reproduced): pi1 controls a test pi2 -> pi3 on a port other than 20500.
    It must run and pi3 must be left with nothing claimed."""
    import time as _t
    from tests.test_newtest_backend import Pi, prof, free_udp_port
    a, b, c = Pi("pi1"), Pi("pi2"), Pi("pi3")
    try:
        for x in (b, c):
            a.st.peers.add_manual(x.addr, "")
        b.st.peers.add_manual(c.addr, "")
        a.st.runs.start(prof(source={"pi": "pi2"}, dest={"pi": "pi3"}, net={"port": free_udp_port()}, duration_s=3))
        a.wait_done()
        rs = a.runs()
        assert len(rs) == 1 and rs[0]["status"] == "finished" and rs[0]["verdict"] == "PASS", rs
        assert c.st.reflectors.status()["items"] == []                       # released by the controller
    finally:
        for x in (a, b, c):
            x.stop()
