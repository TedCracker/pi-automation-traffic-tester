#!/usr/bin/env python3
"""Generator smoke test on localhost (two app instances, no root, no netem): UDP + TCP streams, verdict, CSV, PDF.
Used for the arm64 container check (scripts/arm64-inner.sh) and as a quick sanity run anywhere."""
import json, os, signal, subprocess, sys, tempfile, time, urllib.request, urllib.error
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
TMP = tempfile.mkdtemp(prefix="tt-gensmoke-"); procs = []; bad = []


def http(port, m, path, body=None, raw=False):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=m, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as f:
            d = f.read(); return d if raw else (json.loads(d) if d else None)
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "detail": e.read().decode()[:200]}


def app(role, port):
    env = dict(os.environ, TT_ASSUME_SYNCED="1", PYTHONPATH=ROOT)
    p = subprocess.Popen([PY, "-m", "traffic_tester", "--role", role, "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{role}"], env=env,
                         stdout=open(f"{TMP}/{role}.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(120):
        try: http(port, "GET", "/api/status"); return
        except Exception: time.sleep(0.5)
    raise RuntimeError("app did not start")


def check(name, ok, ev):
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {ev}", flush=True)
    if not ok: bad.append(name)


try:
    A, B = 18280, 18281
    app("A", A); app("B", B)
    st = http(A, "GET", "/api/gen/node/status")
    check("GEN smoke: node status API", "interfaces" in st and not st["busy"], f"hardware={st['hardware']}")
    S = lambda n, t, **k: {"name": n, "type": t, "rate": {"mbps": 20}, "size": {"mode": "fixed", "size": 1200}, "rtt_probe_pps": 20 if t != "tcp" else 0, **k}
    prof = {"name": "smoke", "kind": "generator", "net": {"peer_host": "127.0.0.1"},
            "gen": {"streams": [S("udp", "udp"), S("tcp", "tcp", rate={"mbps": 8}), S("imix", "udp", size={"mode": "imix"}, rate={"mbps": 10}, direction="b_to_a")], "duration_s": 6,
                    "peer_api": f"http://127.0.0.1:{B}", "local_host": "127.0.0.1", "interface": "lo", "peer_interface": "lo", "rule": {"max_loss_pct": 0.5, "max_latency_ms": 50}}}
    r = http(A, "POST", "/api/runs", {"profile": prof}); rid = r["run_id"]
    for _ in range(120):
        time.sleep(1); x = http(A, "GET", f"/api/runs/{rid}")
        if x["status"] != "running": break
    s = x["summary"]
    check("GEN smoke: 3 streams (UDP A→B, TCP A→B, IMIX UDP B→A) finish and PASS", x["status"] == "finished" and x["verdict"] == "PASS", f"status={x['status']} verdict={x['verdict']} err={s.get('error')} reasons={s.get('verdict', {}).get('reasons')}")
    for t in s.get("streams", []):
        print(f"   {t['name']:5s} tx {t['tx']['mbps']:7.2f} Mb/s  rx {t['rx']['mbps']:7.2f} Mb/s  lost {t['lost']}  delay p99 {t['lat_ms'] and t['lat_ms']['p99']} ms  jitter {t['jitter_ms']} ms  rtt {t['rtt_ms'] and t['rtt_ms']['avg']} ms")
    tot = s.get("totals", {})
    check("GEN smoke: no loss, throughput delivered, one-way delay valid", tot.get("lost") == 0 and abs(tot.get("rx_mbps", 0) - 38) < 4 and tot.get("lat_ms"), f"totals={tot}")
    csv = http(A, "GET", f"/api/runs/{rid}/export.csv?kind=gen", raw=True).decode()
    check("GEN smoke: per-second CSV", len(csv.splitlines()) > 12, f"{len(csv.splitlines())} lines")
    pdf = http(A, "GET", f"/api/report.pdf?run_ids={rid}", raw=True)
    check("GEN smoke: PDF report builds (reportlab)", pdf[:5] == b"%PDF-" and len(pdf) > 4000, f"{len(pdf)} bytes")
finally:
    for p in procs:
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: pass
    time.sleep(0.5)
    subprocess.run(["rm", "-rf", TMP])
print("# gen smoke:", "ALL PASSED" if not bad else f"FAILED {bad}")
sys.exit(1 if bad else 0)
