"""Regression (real Pis, 2026-11-12): a 30 s generator run (preset "Mirrored-Bits-like small frames", duration overridden to 30 s, other Pi given by its
web address only) ended as 'stopped' after 10.8 s. It must last the whole 30 s, finish by itself and never be marked stopped; and a Stop that someone
does press is traceable (log + 'stopped_by' in the result)."""
import json, os, subprocess, sys, tempfile, time, urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free():
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _http(port, m, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=m, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=20).read() or b"{}")


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    ports = {"A": _free(), "B": _free()}
    procs = []
    for role in ("A", "B"):
        d = tmp_path_factory.mktemp(role)
        procs.append(subprocess.Popen([sys.executable, "-m", "traffic_tester", "--role", role, "--port", str(ports[role]), "--host", "127.0.0.1", "--data-dir", str(d)],
                                      cwd=str(d), env=dict(os.environ, PYTHONPATH=ROOT, TT_ASSUME_SYNCED="1"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for p in ports.values():
        for _ in range(100):
            try:
                _http(p, "GET", "/api/status"); break
            except Exception:
                time.sleep(0.2)
    yield ports
    for q in procs:
        q.terminate()


def _profile(ports, **gen):
    p = next(x for x in _http(ports["A"], "GET", "/api/profiles") if x["name"].startswith("Mirrored-Bits-like"))
    p.pop("id")
    p["gen"].update(duration_s=30, peer_api=f"127.0.0.1:{ports['B']}", **gen)
    p["net"]["peer_host"] = ""                                             # only the web address is given: the data address defaults to it
    return p


def _wait(port, rid, limit):
    t0 = time.time()
    while time.time() - t0 < limit:
        if not _http(port, "GET", "/api/status")["run"]["running"]:
            break
        time.sleep(0.4)
    return time.time() - t0, _http(port, "GET", f"/api/runs/{rid}")


def test_a_30_second_run_lasts_30_seconds_and_finishes_by_itself(pair):
    rid = _http(pair["A"], "POST", "/api/runs", {"profile": _profile(pair)})["run_id"]
    took, run = _wait(pair["A"], rid, 60)
    assert run["status"] == "finished", run.get("summary")
    assert 29.5 <= run["summary"]["duration_s"] <= 31 and 29.5 <= took <= 40
    assert not run["summary"].get("stopped_early") and "stopped_by" not in run["summary"]
    assert run["summary"]["totals"]["tx_pkts"] > 30 * 200 * 0.9           # ~200 packets a second for the whole 30 s, not 10


def test_a_pressed_stop_is_recorded_with_who_asked(pair):
    rid = _http(pair["A"], "POST", "/api/runs", {"profile": _profile(pair)})["run_id"]
    time.sleep(4)
    _http(pair["A"], "POST", f"/api/runs/{rid}/stop")
    took, run = _wait(pair["A"], rid, 30)
    assert run["status"] == "stopped" and run["summary"]["stopped_early"] and run["summary"]["stopped_by"] == "127.0.0.1" and took < 15
    # real Pis 2026-11-13: a 60 s test stopped after 10 s said 'duration_s 60.0' (PDF banner, Compare's Length): it must say how long it really ran
    assert 2.5 <= run["summary"]["duration_s"] <= 9, run["summary"]["duration_s"]
