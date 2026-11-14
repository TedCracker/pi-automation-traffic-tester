"""Regression tests for the bugs found in the 2026-11 review (docs/REVIEW-2026-11.md). One test per finding."""
import json
import os
import socket
import tempfile
import threading
import time

import pytest
from fastapi.testclient import TestClient

from traffic_tester import engine, frames as F, metrics as M, profiles as P, system
from traffic_tester.runmgr import Busy, RunManager
from traffic_tester.server import create_app
from traffic_tester.store import Store
from traffic_tester.transports import TcpTransport, Transport, UdpTransport

from test_claude import app, chat, enable, env  # noqa: F401  (fixtures: the app with a stub Claude CLI)


@pytest.fixture()
def client():
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        yield c


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


# R1 -- "Apply" / "Undo" of a Claude change crashed in production: server.py used threading.Timer without importing threading
def test_r1_apply_and_undo_schedule_the_restart(app, monkeypatch):  # noqa: F811
    timers, real_timer = [], threading.Timer

    class NoExit:
        def start(self): pass
        def cancel(self): pass

    def fake_timer(delay, fn, *a, **k):          # only the 1.5 s "restart the app" timer is caught; every other timer is real
        if delay == 1.5:
            timers.append(delay); return NoExit()
        return real_timer(delay, fn, *a, **k)
    monkeypatch.setattr(threading, "Timer", fake_timer)
    monkeypatch.setenv("TT_RESTART_ON_APPLY", "1")                    # what the installed service runs with
    enable(app)
    chat(app, "scenario:feature")
    r = app.post("/api/claude/changes/apply", json={"message": "Add file"})
    assert r.status_code == 200 and r.json()["restarting"] is True and len(timers) == 1
    h = app.get("/api/claude/history").json()
    assert app.post("/api/claude/changes/revert", json={"commit": h[0]["commit"]}).status_code == 200 and len(timers) == 2


# R2 -- the clock check (chronyc) ran on the timing threads every 5 s and delayed that one echo: a periodic fake latency spike
def test_r2_clock_state_never_blocks_once_it_has_a_value(monkeypatch):
    calls = []

    def slow_probe():
        calls.append(1); time.sleep(0.4)
        return {"synced": True, "offset_ms": 0.1 * len(calls), "source": "test"}
    monkeypatch.delenv("TT_ASSUME_SYNCED", raising=False)
    monkeypatch.setattr(system, "_clock_probe", slow_probe)
    monkeypatch.setattr(system, "_clock_cache", (0.0, None))
    first = system.clock_state()                                      # nothing cached yet: this one may wait
    assert first["source"] == "test" and len(calls) == 1
    monkeypatch.setattr(system, "_clock_cache", (time.monotonic() - 6.0, first))      # 6 s old = stale
    t0 = time.monotonic()
    got = system.clock_state()
    assert time.monotonic() - t0 < 0.1, "a stale clock value must be answered at once (refresh happens in the background)"
    assert got == first
    for _ in range(40):
        if system._clock_cache[1] is not first: break
        time.sleep(0.05)
    assert system._clock_cache[1]["offset_ms"] == pytest.approx(0.2) and len(calls) == 2
    assert system.clock_state(0)["offset_ms"] == pytest.approx(0.3)    # max_age 0 = the caller wants a fresh reading and accepts the wait


def test_r2_very_old_clock_value_is_not_trusted(monkeypatch):
    monkeypatch.delenv("TT_ASSUME_SYNCED", raising=False)
    monkeypatch.setattr(system, "_clock_probe", lambda: {"synced": False, "offset_ms": None, "source": "fresh"})
    monkeypatch.setattr(system, "_clock_cache", (time.monotonic() - 3600, {"synced": True, "offset_ms": 0.0, "source": "old"}))
    assert system.clock_state()["source"] == "fresh"                  # an hour-old "synced" must not decide one-way timing


# R3 -- the other Pi's web password (generator) was written into every stored run and so into JSON / ZIP / USB exports
def test_r3_peer_password_is_not_stored_with_the_run_or_exported():
    st = Store(tempfile.mkdtemp())
    prof = P.validate_profile({"name": "g", "kind": "generator", "gen": {"peer_api": "10.0.0.2:8080", "peer_password": "hunter2-secret"}})
    rid = st.create_run(st.current_session()["id"], prof, "A", "10.0.0.2:8080")
    st.finish_run(rid, "finished", {"kind": "generator", "totals": {}})
    assert prof["gen"]["peer_password"] == "hunter2-secret"            # the caller's copy (used to run the test) is untouched
    assert "hunter2-secret" not in json.dumps(st.run_json(rid))
    assert b"hunter2-secret" not in st.session_zip(st.current_session()["id"])
    raw = st.db.execute("SELECT profile_json FROM runs WHERE id=?", (rid,)).fetchone()[0]
    assert "hunter2-secret" not in raw


# R4 -- deleting a test / clearing a session left the capture files on the SD card for ever
def test_r4_deleting_a_run_removes_its_capture_folder():
    d = tempfile.mkdtemp()
    st = Store(d)
    sid = st.current_session()["id"]
    prof = P.validate_profile({"name": "s", "kind": "sniffer"})
    rids = [st.create_run(sid, prof, "A", "eth_span") for _ in range(2)]
    for rid in rids:
        cap = os.path.join(d, "captures", f"run-{rid}")
        os.makedirs(cap)
        open(os.path.join(cap, "capture.pcapng"), "wb").write(b"x" * 100)
        st.finish_run(rid, "stopped", {"kind": "sniffer", "location": cap, "files": [{"name": "capture.pcapng", "bytes": 100}]})
    outside = tempfile.mkdtemp()                                       # a capture saved straight to a USB drive is the user's copy: never deleted
    keep = os.path.join(outside, "traffic-tester-captures", "run-99")
    os.makedirs(keep)
    rid3 = st.create_run(sid, prof, "A", "eth_span")
    st.finish_run(rid3, "stopped", {"kind": "sniffer", "location": keep, "files": []})
    st.delete_run(rids[0])
    assert not os.path.exists(os.path.join(d, "captures", f"run-{rids[0]}")) and os.path.exists(os.path.join(d, "captures", f"run-{rids[1]}"))
    st.clear_session(sid)
    assert not os.path.exists(os.path.join(d, "captures", f"run-{rids[1]}"))
    assert os.path.isdir(keep)


# R5 -- starting a generator test while the generator was still busy left a test row stuck on "running"
def test_r5_refused_start_leaves_no_stuck_run():
    class BusyNode:
        def busy(self): return True
        def stop(self): return {}
    st = Store(tempfile.mkdtemp())
    rm = RunManager(st, "A", BusyNode())
    prof = {"name": "g", "kind": "generator", "gen": {"peer_api": "127.0.0.1:1"}}
    with pytest.raises(Busy):
        rm.start(prof)
    assert st.list_runs() == []


# R6 -- one transport error (USB-serial adapter replugged on Pi B) stopped the Responder for good
class FlakyTransport(Transport):
    role = "responder"

    def __init__(self):
        super().__init__()
        self.opens = self.closes = 0
        self.fail_reads = 1
        self.inbox, self.sent = [], []

    def open(self): self.opens += 1
    def close(self): self.closes += 1
    def write(self, f): self.sent.append(f)
    def describe(self): return "flaky"

    def read(self, timeout):
        if self.opens == 1 and self.fail_reads:
            self.fail_reads -= 1
            raise OSError("device disconnected")
        time.sleep(0.01)
        out, self.inbox = self.inbox, []
        return [(f, time.monotonic_ns(), time.time_ns()) for f in out]


def test_r6_responder_reopens_the_port_after_an_error(monkeypatch):
    monkeypatch.setattr(engine, "RESPONDER_RETRY_S", 0.05)
    tr = FlakyTransport()
    r = engine.Responder(tr)
    r.start()
    try:
        for _ in range(100):
            if tr.opens >= 2: break
            time.sleep(0.02)
        assert tr.opens >= 2 and tr.closes >= 1 and r.running, "the Responder must close and reopen the port, not die"
        tr.inbox.append(F.Frame(F.T_TRIP, 7, 0x81))
        for _ in range(100):
            if tr.sent: break
            time.sleep(0.02)
        assert tr.sent and tr.sent[0].type == F.T_ECHO and tr.sent[0].seq == 7
        st = r.status()
        assert st["running"] and st["recoveries"] == 1 and st["error"] is None
    finally:
        r.stop()
    assert not r.running


def test_r6_restarting_a_dead_responder_frees_the_old_port(client):
    port = free_port()
    body = {"transport": "udp", "net": {"port": port}}
    assert client.post("/api/responder/start", json=body).status_code == 200
    st = client.app.state.tt
    e = st.reflectors.entries[f"udp:{port}"]
    e.resp.running = False                                              # as if its thread had died; the socket is still bound
    r = client.post("/api/responder/start", json=body)
    assert r.status_code == 200, r.text                                 # used to fail: "Address already in use"
    client.post("/api/responder/stop")


# R7 -- a message that could not be written (TCP session down) was counted as lost but not as sent in the live numbers
def test_r7_live_counters_count_unsendable_messages_as_sent():
    class DeadTransport(Transport):
        def read(self, timeout): time.sleep(min(timeout, 0.005)); return []
        def write(self, f): raise OSError("tcp session down")
    prof = P.validate_profile({"name": "t", "transport": "udp", "frame": "extended", "interval_ms": 10, "duration_s": 1, "timeout_ms": 100, "net": {"peer_host": "127.0.0.1"}})
    t = engine.Tester(prof, DeadTransport())
    for _ in range(5):
        t._send("steady", None)
    assert t.lost_total == 5 and t.sent_total == 5 and t.acc.sent == 5


# R8 -- malformed requests answered 500 instead of a plain 4xx
@pytest.mark.parametrize("method,path,body,want", [
    ("get", "/api/profiles/export?ids=abc", None, 422),
    ("get", "/api/report.pdf?run_ids=1,x", None, 422),
    ("post", "/api/auth/login", {"password": 12345}, 401),
    ("post", "/api/auth/set", {"password": 12345}, 422),
    ("post", "/api/usb/save", {"session_id": "nope"}, 422),
    ("put", "/api/sessions/99999", {"name": "x"}, 404),
    ("get", "/api/runs/99999/live", None, 404),
    ("post", "/api/responder/start", {"transport": "udp", "net": {"port": 20500}, "delay_ms": "abc"}, 422),
])
def test_r8_bad_input_is_a_4xx_not_a_500(client, monkeypatch, tmp_path, method, path, body, want):
    monkeypatch.setenv("TT_USB_DIR", str(tmp_path))
    if path == "/api/auth/login":
        client.post("/api/auth/set", json={"password": "abcd"})
    r = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    assert r.status_code == want, r.text


# R9 -- a TCP test could never connect over a link whose round trip is above 300 ms (fixed connect timeout)
def test_r9_tcp_connect_timeout_follows_the_test_timeout(monkeypatch):
    seen = []

    def fake_connect(addr, timeout=None):
        seen.append(timeout); raise OSError("no route")
    monkeypatch.setattr(socket, "create_connection", fake_connect)
    from traffic_tester import factory
    prof = P.validate_profile({"name": "t", "transport": "tcp", "frame": "extended", "timeout_ms": 1500, "net": {"peer_host": "10.9.9.9"}})
    tr = factory.make_transport(prof, "tester")
    assert isinstance(tr, TcpTransport)
    tr.open()
    assert seen and seen[0] == pytest.approx(1.5)
    prof2 = P.validate_profile({"name": "t", "transport": "tcp", "frame": "extended", "timeout_ms": 100, "threshold_ms": 60, "net": {"peer_host": "10.9.9.9"}})
    tr2 = factory.make_transport(prof2, "tester"); tr2.open()
    assert seen[-1] == pytest.approx(0.3)                              # never below the old 300 ms


# R10 -- status tells a reloaded page the running test's own limit (the Monitor chart drew 60 ms whatever the profile said)
def test_r10_status_reports_the_running_tests_limit(client):
    port = free_port()
    resp = engine.Responder(UdpTransport("responder", "", port)); resp.start()
    try:
        r = client.post("/api/runs", json={"profile": {"name": "t", "transport": "udp", "frame": "extended", "interval_ms": 20, "duration_s": 2, "threshold_ms": 25, "timeout_ms": 300,
                                                       "net": {"peer_host": "127.0.0.1", "port": port}}})
        assert r.status_code == 200
        assert client.get("/api/status").json()["run"]["threshold_ms"] == 25
        client.post(f"/api/runs/{r.json()['run_id']}/stop")
        client.app.state.tt.runs.wait(5)
    finally:
        resp.stop()


# R11 -- static JS/CSS were sent uncompressed (222 kB on first load); API streams must stay uncompressed
def test_r11_static_assets_are_gzipped_but_the_api_is_not(client):
    r = client.get("/setup.js", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip" and "renderSetup" in r.text
    assert client.get("/api/status", headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding") is None
    html = client.get("/").text
    assert 'rel="modulepreload"' in html


# R12 -- Claude's change check looked only at the NEW name of a moved file: moving a safety file out of traffic_tester/claude/ was not blocked
def test_r12_moving_or_oddly_naming_a_protected_file_is_blocked(tmp_path):
    import subprocess
    from traffic_tester.claude.workspace import Workspace, WorkspaceError
    live, work = tmp_path / "live", tmp_path / "ws" / "work"
    (live / "traffic_tester" / "claude").mkdir(parents=True)
    (live / "traffic_tester" / "claude" / "tools.py").write_text("SAFE = True\n" * 30)
    (live / "README.md").write_text("app\n")
    ws = Workspace(str(live), str(work))
    ws.ensure()
    os.rename(work / "traffic_tester" / "claude" / "tools.py", work / "moved_tools.py")          # a move = the protected file is gone
    (work / "traffic_tester" / "claude" / "é vil.py").write_text("x = 1\n")                        # git would quote this name
    d = ws.diff()
    assert "traffic_tester/claude/tools.py" in d["blocked"] and "traffic_tester/claude/é vil.py" in d["blocked"], d["files"]
    with pytest.raises(WorkspaceError, match="keep Claude Code safe"):
        ws.apply("move it")
    assert (live / "traffic_tester" / "claude" / "tools.py").exists()
    assert subprocess.run(["git", "status", "--porcelain"], cwd=live, capture_output=True, text=True).stdout.strip() == ""


# R13 -- Ask Claude's "open_screen" still offered the removed Monitor tab; the old name must land on Run
def test_r13_open_screen_maps_the_old_monitor_tab_to_run():
    from traffic_tester.claude.tools import ToolBox, TOOL_SPECS

    class St:
        sent = []
        def push_ui(self, ev): self.sent.append(ev)
    st = St()
    assert ToolBox(st).open_screen("monitor") == {"ok": True, "opened": "run"} and st.sent[-1]["tab"] == "run"
    assert "error" in ToolBox(st).open_screen("nowhere")
    assert "monitor" not in next(t for t in TOOL_SPECS if t["name"] == "open_screen")["description"]


# R14 -- "permission missing" messages ended with "The installed service has it.": read as "it is available" (a small AI model answered
#        "Yes, GOOSE is available" from the guide; a person on a rootless install reads it the same way)
def test_r14_missing_permission_messages_say_this_copy_lacks_it(monkeypatch):
    import socket as _s
    from traffic_tester import goose

    def no_raw(*a, **k):
        raise PermissionError(1, "Operation not permitted")
    real = _s.socket
    monkeypatch.setattr(goose.socket, "socket", lambda *a, **k: no_raw() if a and a[0] == _s.AF_PACKET else real(*a, **k))
    r = goose.raw_socket_ok()
    assert r["ok"] is False and "started without it" in r["reason"] and "has it." not in r["reason"]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for f in ("goose.py", "gen/node.py", "sniff/capture.py"):
        assert "The installed service has it" not in open(os.path.join(root, "traffic_tester", f), encoding="utf-8").read(), f
