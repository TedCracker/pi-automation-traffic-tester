"""No fixed roles: planning, peers, the always-on reflector, orchestration across real apps (each Pi = a full app on its own HTTP port)."""
import collections, os, shutil, subprocess, time
import pytest
from traffic_tester import peers as PE, plan as PL, profiles as P, reflector as RF
from netfix import Pi, free_udp_port, free_tcp_port


def prof(**kw):
    base = {"name": "t", "transport": "udp", "frame": "extended", "interval_ms": 20, "duration_s": 2, "timeout_ms": 400}
    base.update(kw)
    return P.validate_profile(base)


def ctx(peers=()):
    peers = list(peers)
    def resolve(ref):
        for p in peers:
            if ref in (p["id"], p["name"], p["host"]) or ref in p["ips"]:
                return p
        return None
    return PL.Context({"id": "me1", "name": "pi1", "ips": ["10.0.0.1"]}, resolve, lambda host: "10.0.0.1")


PEER2 = {"id": "p2", "name": "pi2", "host": "10.0.0.2", "port": 8080, "ips": ["10.0.0.2"]}
PEER3 = {"id": "p3", "name": "pi3", "host": "10.0.1.3", "port": 8080, "ips": ["10.0.1.3"]}


# ------------------------------------------------------------ pure planning
def test_old_profile_keeps_working():
    legs = PL.plan_trip(prof(net={"peer_host": "10.0.0.2"}), ctx([PEER2]))
    assert len(legs) == 1 and legs[0].tester.is_self and legs[0].reflector.name == "pi2" and legs[0].data_host == "10.0.0.2"
    assert legs[0].reflect["net"]["port"] == 20500 and legs[0].basis == "rtt" and legs[0].kind == "round_trip"


def test_destination_must_be_named_for_ip_tests():
    with pytest.raises(PL.PlanError, match="Choose where to send"):
        PL.plan_trip(prof(), ctx())


def test_unknown_pi_is_a_plain_error():
    with pytest.raises(PL.PlanError, match="not in your list"):
        PL.plan_trip(prof(dest={"pi": "zzz", "name": "pi9"}), ctx([PEER2]))


def test_forward_reverse_both():
    f = PL.plan_trip(prof(dest={"pi": "pi2"}, direction="forward"), ctx([PEER2]))
    assert [l.label for l in f] == ["pi1 → pi2"] and f[0].basis == "up" and f[0].profile["one_way"]
    r = PL.plan_trip(prof(dest={"pi": "pi2"}, direction="reverse"), ctx([PEER2]))
    assert [l.label for l in r] == ["pi2 → pi1"] and not r[0].tester.is_self and r[0].reflector.is_self and r[0].basis == "up"
    assert r[0].data_host == "10.0.0.1"                     # the Pi that sends reaches this Pi on this Pi's address
    b = PL.plan_trip(prof(dest={"pi": "pi2"}, direction="both"), ctx([PEER2]))
    assert [l.label for l in b] == ["pi1 → pi2", "pi2 → pi1"] and all(l.basis == "rtt" for l in b)


def test_data_address_picks_the_shared_subnet():
    legs = PL.plan_trip(prof(source={"pi": "pi2"}, dest={"pi": "pi3"}), ctx([PEER2, {**PEER3, "ips": ["10.0.1.3", "10.0.0.33"]}]))
    assert legs[0].data_host == "10.0.0.33" and not legs[0].tester.is_self


def test_both_ways_refused_for_serial_and_goose():
    for t in ("serial", "goose_l2", "goose_l3"):
        with pytest.raises(ValueError, match="cannot send and reflect"):
            P.validate_profile({"name": "x", "transport": t, "direction": "both"})


def test_serial_ports_per_end():
    legs = PL.plan_trip(prof(transport="serial", frame="compact", source={"serial_port": "/dev/ttyUSB0"}, dest={"pi": "pi2", "serial_port": "/dev/ttyUSB1"}), ctx([PEER2]))
    assert legs[0].profile["serial"]["port"] == "/dev/ttyUSB0" and legs[0].reflect["serial"]["port"] == "/dev/ttyUSB1"
    legs = PL.plan_trip(prof(transport="serial", frame="compact", direction="reverse", source={"serial_port": "/dev/ttyUSB0"}, dest={"pi": "pi2", "serial_port": "/dev/ttyUSB1"}, ), ctx([PEER2]))
    assert legs[0].profile["serial"]["port"] == "/dev/ttyUSB1" and legs[0].reflect["serial"]["port"] == "/dev/ttyUSB0"


def test_typed_address_cannot_be_a_sender():
    with pytest.raises(PL.PlanError, match="cannot start the test there"):
        PL.plan_trip(prof(source={"host": "10.9.9.9"}, dest={"pi": "pi2"}), ctx([PEER2]))


# ------------------------------------------------------------ discovery / addresses
def test_parse_address():
    assert PE.parse_address("http://10.0.0.5:8080/") == ("10.0.0.5", 8080)
    assert PE.parse_address("pi2.local") == ("pi2.local", None)
    with pytest.raises(PE.PeerError):
        PE.parse_address("bad host!")
    with pytest.raises(PE.PeerError):
        PE.parse_address("")


def test_service_announcement_parsing():
    props = {b"app": b"pi-traffic-tester", b"id": b"ab12", b"name": b"pi2", b"version": b"1.2.0", b"ips": b"192.168.1.5,10.5.0.1"}
    d = PE.peer_from_service("pi2._pitester._tcp.local.", props, ["192.168.1.5"], 8080, ["10.5.0.9"])
    assert d["id"] == "ab12" and d["name"] == "pi2" and d["host"] == "10.5.0.1" and d["version"] == "1.2.0" and set(d["ips"]) == {"192.168.1.5", "10.5.0.1"}
    assert PE.peer_from_service("x._pitester._tcp.local.", {b"app": b"something-else"}, ["1.2.3.4"], 80) is None
    assert PE.peer_from_service("x._pitester._tcp.local.", props, ["169.254.1.1", "127.0.0.1"], 80) is not None      # the advertised list still has usable addresses
    assert PE.peer_from_service("x._pitester._tcp.local.", {b"app": b"pi-traffic-tester"}, ["169.254.1.1"], 80) is None


def test_name_rules_and_default():
    from traffic_tester.store import Store
    import tempfile
    st = Store(tempfile.mkdtemp())
    me = PE.Identity(st)
    assert me.name and me.id and me.setup_at
    assert me.set_name("pi-7") == "pi-7"
    for bad in ("bad/name", "x" * 40, "-lead"):
        with pytest.raises(ValueError):
            me.set_name(bad)
    me.set_name("")
    assert me.name != "pi-7"


# ------------------------------------------------------------ time source
def test_clock_decision():
    from traffic_tester import clock as CK
    me = {"id": "a", "name": "pi1", "setup_at": "2026-11-01T00:00:00"}
    peers = [{"id": "b", "name": "pi2", "host": "10.0.0.2", "ips": ["10.0.0.2"], "reachable": True, "setup_at": "2026-11-02T00:00:00"}]
    assert CK.decide(me, peers, "auto")["mode"] == "serve"
    peers[0]["setup_at"] = "2026-10-01T00:00:00"
    d = CK.decide(me, peers, "auto")
    assert d["mode"] == "client" and d["server"] == "10.0.0.2"
    peers[0]["reachable"] = False
    assert CK.decide(me, peers, "auto")["mode"] == "serve"
    assert CK.decide(me, peers, "keep")["mode"] == "keep" and CK.decide(me, peers, "self")["mode"] == "serve"
    assert CK.decide(me, [{**peers[0], "reachable": True}], "b")["server"] == "10.0.0.2"


def test_clock_agree():
    from traffic_tester import orchestrate as OR
    a = {"ref_ip": "", "ref_local": True}
    b = {"ref_ip": "10.0.0.1", "ref_local": False}
    assert OR.clock_agree(a, b, ["10.0.0.1"], ["10.0.0.2"])[0] is True             # pi2 follows pi1 (which serves its own clock)
    assert OR.clock_agree({"ref_ip": "", "ref_local": True}, {"ref_ip": "", "ref_local": True}, ["10.0.0.1"], ["10.0.0.2"])[0] is False   # two Pis on their own clocks
    assert OR.clock_agree({"ref_ip": "1.1.1.1"}, {"ref_ip": "1.1.1.1"}, ["10.0.0.1"], ["10.0.0.2"])[0] is True
    assert OR.clock_agree({}, {"ref_ip": "x"}, [], [])[0] is None


# ------------------------------------------------------------ real apps talking to each other
@pytest.fixture
def pis(monkeypatch):
    made = []
    def make(*names, password=None):
        out = []
        for n in names:
            p = Pi(n, password=password if n == names[-1] and password else None)
            made.append(p); out.append(p)
        return out
    yield make
    for p in made:
        p.stop()


def add(a, b, password=""):
    return a.st.peers.add_manual(b.addr, password)


def start(a, **kw):
    return a.st.runs.start(prof(**kw))


def test_peer_list_reflector_and_round_trip(pis):
    a, b = pis("pi1", "pi2")
    e = add(a, b)
    assert e["name"] == "pi2" and e["reachable"] and e["version"] and e["id"] == b.st.me.id
    port = free_udp_port()
    start(a, dest={"pi": "pi2"}, net={"port": port})
    a.wait_done()
    r = a.runs()[0]
    assert r["status"] == "finished" and r["verdict"] == "PASS" and r["leg"]["label"] == "pi1 → pi2"
    full = a.st.store.get_run(r["id"])
    assert full["summary"]["endpoints"]["dest"] == "pi2" and full["summary"]["sent"] > 50 and full["summary"]["lost"] == 0
    assert b.st.reflectors.status()["items"] == []                       # released after the test
    assert a.st.runs.status()["phase"] in ("done", "stopped")


def test_reverse_runs_on_the_other_pi_and_result_comes_back(pis, monkeypatch):
    monkeypatch.setenv("TT_ASSUME_SYNCED", "1")
    a, b = pis("pi1", "pi2")
    add(a, b)
    start(a, dest={"pi": "pi2"}, direction="reverse", net={"port": free_udp_port()})
    a.wait_done()
    r = a.runs()[0]
    full = a.st.store.get_run(r["id"])
    assert full["status"] == "finished" and full["summary"]["basis"] == "up" and full["summary"]["one_way"] and full["verdict"] == "PASS"
    assert full["summary"]["endpoints"]["source"] == "pi2" and full["summary"]["up_ms"]["p99"] is not None
    assert len(a.st.store.run_messages(r["id"])) > 50                    # the per-message rows were copied over
    assert len(b.runs()) == 1 and b.runs()[0]["status"] == "finished"    # pi2 keeps its own copy


def test_forward_refuses_without_synced_clocks(pis, monkeypatch):
    monkeypatch.delenv("TT_ASSUME_SYNCED", raising=False)
    a, b = pis("pi1", "pi2")
    add(a, b)
    start(a, dest={"pi": "pi2"}, direction="forward", net={"port": free_udp_port()})
    a.wait_done()
    full = a.st.store.get_run(a.runs()[0]["id"])
    assert full["status"] == "failed" and "clocks" in full["summary"]["error"]


class Rec(collections.deque):
    """Keeps every live event (the app drops them when no browser is listening)."""
    def __init__(self):
        super().__init__(maxlen=8000)
        self.log = []

    def append(self, e):
        self.log.append(e)
        super().append(e)


def test_both_ways_at_once_is_two_runs_in_one_group(pis):
    a, b = pis("pi1", "pi2")
    a.st.runs.ui_events = Rec()
    add(a, b)
    rid = start(a, dest={"pi": "pi2"}, direction="both", net={"port": free_udp_port()})
    a.wait_done()
    rs = a.runs()
    assert len(rs) == 2 and rs[0]["group_id"] == rs[1]["group_id"] == rid
    assert sorted(r["leg"]["label"] for r in rs) == ["pi1 → pi2", "pi2 → pi1"] and all(r["status"] == "finished" and r["verdict"] == "PASS" for r in rs)
    done = [e for e in a.st.runs.ui_events.log if e.get("ev") == "done"]
    assert len(done) == 1 and len(done[0]["legs"]) == 2


def test_password_protected_peer(pis):
    a, b = pis("pi1", "pi2", password="s3cret")
    e = add(a, b)
    assert e["password_required"] and e["auth"] == "needs_password"
    with pytest.raises(PL.PlanError, match="password"):
        start(a, dest={"pi": "pi2"}, net={"port": free_udp_port()})
    e = a.st.peers.set_password(e["id"], "s3cret")
    assert e["auth"] == "ok" and e["has_password"]
    start(a, dest={"pi": "pi2"}, net={"port": free_udp_port()})
    a.wait_done()
    assert a.runs()[0]["verdict"] == "PASS"


def test_peer_gone_gives_a_plain_message(pis):
    a, b = pis("pi1", "pi2")
    add(a, b)
    b.stop()
    with pytest.raises(PL.PlanError, match="does not answer"):
        start(a, dest={"pi": "pi2"}, net={"port": free_udp_port()})


@pytest.mark.skipif(shutil.which("socat") is None, reason="needs socat for a serial pair")
def test_serial_reflector_and_port_busy(pis):
    p = subprocess.Popen(["socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0"], stderr=subprocess.PIPE, text=True)
    try:
        paths = []
        while len(paths) < 2:
            line = p.stderr.readline()
            if "PTY is" in line:
                paths.append(line.split("PTY is")[1].strip())
        a, b = pis("pi1", "pi2")
        add(a, b)
        start(a, transport="serial", frame="compact", source={"serial_port": paths[0]}, dest={"pi": "pi2", "serial_port": paths[1]}, interval_ms=50, duration_s=2)
        a.wait_done()
        assert a.runs()[0]["verdict"] == "PASS"
        assert b.st.claims.held == {}                                     # released
        # the port is taken by something else: a plain "busy", nothing is left claimed
        b.st.claims.claim(f"serial:{paths[1]}", "another test", "x")
        with pytest.raises(PL.PlanError, match="busy"):
            start(a, transport="serial", frame="compact", source={"serial_port": paths[0]}, dest={"pi": "pi2", "serial_port": paths[1]}, duration_s=2)
        assert a.st.claims.held == {}
    finally:
        p.terminate()


def test_old_responder_api_still_works(pis):
    a, = pis("pi1")
    port = free_udp_port()
    r = a.st.reflectors.ensure({"transport": "udp", "net": {"port": port}}, "legacy")
    assert r["running"] and r["key"] == f"udp:{port}"
    start(a, net={"peer_host": "127.0.0.1", "port": port})
    a.wait_done()
    assert a.runs()[0]["verdict"] == "PASS"
    a.st.reflectors.release("legacy")
    assert a.st.reflectors.status()["items"] == []


def test_reflector_default_and_conflict(pis):
    a, = pis("pi1")
    port = free_udp_port()
    a.st.reflectors.ports = [port]
    a.st.reflectors.start_defaults()
    items = {i["key"]: i for i in a.st.reflectors.status()["items"]}
    assert items[f"udp:{port}"]["default"] and items[f"tcp:{port}"]["default"] or f"udp:{port}" in items
    # Mirrored Bits needs the same UDP port: the default steps aside, and comes back afterwards
    a.st.reflectors.ensure({"transport": "udp", "mode": "mirrored", "net": {"port": port}}, "mb-test")
    keys = [i["key"] for i in a.st.reflectors.status()["items"]]
    assert f"udp:{port}" not in keys and any(k.startswith("mb-udp") for k in keys)
    a.st.reflectors.release("mb-test")
    keys = [i["key"] for i in a.st.reflectors.status()["items"]]
    assert f"udp:{port}" in keys


def test_migration_of_an_old_install(pis, tmp_path):
    import tempfile
    from traffic_tester.store import Store
    from traffic_tester.server import create_app
    d = tempfile.mkdtemp()
    s = Store(d)
    s.set_setting("responder_cfg", {"transport": "udp", "net": {"port": 20700}})
    s.new_session("old")
    app = create_app(d, "B")
    st = app.state.tt
    assert st.migrated["from_role"] == "B" and st.migrated["existing_install"]
    assert st.store.get_setting("clock_source") == "keep" and 20700 in st.reflectors.ports
    # a brand-new install is not "kept"
    app2 = create_app(tempfile.mkdtemp(), "A")
    assert app2.state.tt.migrated["existing_install"] is False and app2.state.tt.store.get_setting("clock_source") is None


# ------------------------------------------------------------ the generator across several Pis
def gen_profile(streams, **cfg):
    return P.validate_profile({"name": "g", "kind": "generator", "gen": {"streams": streams, "duration_s": 2, "interface": "lo", **cfg}})


def gs(name, src, dst, **kw):
    return {"name": name, "type": "udp", "rate": {"pps": 200}, "size": {"mode": "fixed", "size": 200}, "duration_s": 2, "rtt_probe_pps": 0, "src": src, "dst": dst, **kw}


def test_generator_two_pis_into_one(pis, monkeypatch):
    monkeypatch.setenv("TT_ASSUME_SYNCED", "1")
    a, b, c = pis("pi1", "pi2", "pi3")
    add(a, b); add(a, c)
    a.st.runs.start(gen_profile([gs("from pi2", {"pi": "pi2", "iface": "lo"}, {"iface": "lo"}), gs("from pi3", {"pi": "pi3", "iface": "lo"}, {"iface": "lo"})]))
    a.wait_done(40)
    full = a.st.store.get_run(a.runs()[0]["id"])
    s = full["summary"]
    assert full["status"] == "finished" and full["verdict"] == "PASS", s
    assert sorted((x["src"], x["dst"]) for x in s["streams"]) == [("pi2", "pi1"), ("pi3", "pi1")]
    assert sorted((p_["src"], p_["dst"]) for p_ in s["pairs"]) == [("pi2", "pi1"), ("pi3", "pi1")] and all(p_["lost"] == 0 and p_["rx_pkts"] > 300 for p_ in s["pairs"])
    assert s["clocks"]["valid"] and len(s["clocks"]["nodes"]) == 3
    assert b.runs() == [] and c.runs() == []                   # only the controller stores the run; the nodes just send / receive


def test_generator_one_pi_to_two_and_both_ways(pis, monkeypatch):
    monkeypatch.setenv("TT_ASSUME_SYNCED", "1")
    a, b, c = pis("pi1", "pi2", "pi3")
    add(a, b); add(a, c)
    a.st.runs.start(gen_profile([gs("to pi2", {"iface": "lo"}, {"pi": "pi2", "iface": "lo"}), gs("pi3 to pi2", {"pi": "pi3", "iface": "lo"}, {"pi": "pi2", "iface": "lo"}),
                                 gs("pi2 to pi1", {"pi": "pi2", "iface": "lo"}, {"iface": "lo"})]))
    a.wait_done(40)
    s = a.st.store.get_run(a.runs()[0]["id"])["summary"]
    assert a.runs()[0]["status"] == "finished", s
    assert sorted((x["src"], x["dst"]) for x in s["streams"]) == [("pi1", "pi2"), ("pi2", "pi1"), ("pi3", "pi2")]
    assert all(p_["lost"] == 0 and p_["rx_pkts"] > 300 for p_ in s["pairs"])


def test_generator_same_pi_both_ends_is_refused(pis):
    a, b = pis("pi1", "pi2")
    add(a, b)
    a.st.runs.start(gen_profile([gs("x", {"pi": "pi2"}, {"pi": "pi2"})]))
    a.wait_done(20)
    assert a.runs()[0]["status"] == "failed" and "two different Pis" in a.st.store.get_run(a.runs()[0]["id"])["summary"]["error"]


def test_mirrored_bits_mode_runs_through_the_same_planning(pis):
    a, b = pis("pi1", "pi2")
    add(a, b)
    start(a, mode="mirrored", dest={"pi": "pi2"}, net={"port": free_udp_port()}, duration_s=4, interval_ms=50)
    a.wait_done(40)
    r = a.runs()[0]
    full = a.st.store.get_run(r["id"])
    assert full["status"] == "finished", full["summary"]
    m = full["summary"]["mirrored"]
    assert full["summary"]["endpoints"]["dest"] == "pi2" and m["emulator"] == 2 and m["rx_good"] > 10
    assert m["proto"] == "A" and m["far"]["proto"] == "B" and m["far"]["tx_id"] == 2 and m["far"]["counters"]["rx_good"] > 10   # the answering Pi got the mirror image
    assert m["transfer"]["n"] >= 1 and full["summary"]["rtt_ms"]["p50"] is not None
    assert m["settings"]["rtt_max_ms"] == 40.0 and m["rtt"]["rtt_max_ms"] == 40.0          # spec 10.6: the DEFAULT echo limit must fire at a steady +50 ms
    assert b.st.reflectors.status()["items"] == []


def test_goose_l3_plan_asks_for_a_mirror_on_the_destination():
    legs = PL.plan_trip(P.validate_profile({"name": "g", "transport": "goose_l3", "dest": {"pi": "pi2", "iface": "eth1"}, "goose": {"interface": "eth0"}}), ctx([PEER2]))
    assert legs[0].profile["goose"]["interface"] == "eth0" and legs[0].reflect["goose"]["interface"] == "eth1" and legs[0].reflect["transport"] == "goose_l3"


def test_remote_test_loses_its_pi_midway_and_says_so(pis, monkeypatch):
    from traffic_tester import remote as RM
    monkeypatch.setattr(RM.RemoteTester, "LOST_GRACE_S", 1.0)
    monkeypatch.setenv("TT_ASSUME_SYNCED", "1")
    a, b = pis("pi1", "pi2")
    add(a, b)
    real = PE.PeerClient.call
    dead = []

    def call(self, method, path, body=None, timeout=8.0, raw=False, _retry=True):
        if dead and self.port == b.port:                      # the cable is pulled: nothing reaches pi2 any more
            raise PE.PeerError(f"Cannot reach pi2 at {self.host}:{self.port}: timed out")
        return real(self, method, path, body, timeout, raw, _retry)
    monkeypatch.setattr(PE.PeerClient, "call", call)
    start(a, dest={"pi": "pi2"}, direction="reverse", net={"port": free_udp_port()}, duration_s=4)
    time.sleep(1.5)
    dead.append(1)
    a.wait_done(40)
    full = a.st.store.get_run(a.runs()[0]["id"])
    assert full["status"] == "failed" and "Lost contact with pi2" in full["summary"]["error"]
    assert a.st.claims.held == {} and a.st.runs.status()["running"] is False


def test_stop_reaches_the_other_pi(pis, monkeypatch):
    monkeypatch.setenv("TT_ASSUME_SYNCED", "1")
    a, b = pis("pi1", "pi2")
    add(a, b)
    start(a, dest={"pi": "pi2"}, direction="both", net={"port": free_udp_port()}, duration_s=30)
    time.sleep(2)
    a.st.runs.stop("test")
    a.wait_done(20)
    rs = a.runs()
    assert len(rs) == 2 and all(r["status"] == "stopped" for r in rs) and not b.st.runs.busy()


def test_a_new_test_can_start_the_moment_the_last_one_says_finished(pis):
    a, b = pis("pi1", "pi2")
    add(a, b)
    port = free_udp_port()
    for i in range(3):
        rid = start(a, dest={"pi": "pi2"}, net={"port": port}, duration_s=1)
        for _ in range(300):
            if a.st.store.get_run(rid)["status"] != "running":
                break
            time.sleep(0.01)
        # no waiting: the row says finished, so the reflector is released and the manager is free
        assert not a.st.runs.busy() and b.st.reflectors.status()["items"] == [], i


def test_a_typed_address_of_a_listed_pi_is_used_as_typed_for_the_traffic():
    peer = {**PEER2, "ips": ["10.0.0.2", "192.168.50.10"]}
    legs = PL.plan_trip(prof(net={"peer_host": "192.168.50.10"}), ctx([peer]))
    assert legs[0].reflector.name == "pi2" and legs[0].data_host == "192.168.50.10" and legs[0].profile["net"]["peer_host"] == "192.168.50.10"
    legs = PL.plan_trip(prof(dest={"pi": "pi2"}), ctx([peer]))
    assert legs[0].data_host == "10.0.0.2"


def test_chrony_reference_id_gives_the_server_address_even_when_it_prints_a_name(monkeypatch):
    from traffic_tester import system as S
    assert S.ref_ip_from_id("C00002DA") == "192.0.2.218" and S.ref_ip_from_id("7F7F0101") == ""
    out = "Reference ID    : C00002DA (pi1.Homez)\nStratum         : 4\nSystem time     : 0.000001 seconds fast of NTP time\nLeap status     : Normal\n"

    class R:
        stdout = out
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(S.shutil, "which", lambda n: "/usr/bin/chronyc")
    monkeypatch.delenv("TT_ASSUME_SYNCED", raising=False)
    st = S._clock_probe()
    assert st["ref_ip"] == "192.0.2.218" and not st["ref_local"] and st["synced"] and st["stratum"] == 4
