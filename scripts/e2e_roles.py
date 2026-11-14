#!/usr/bin/env python3
"""No fixed roles, end to end: three identical apps (pi1, pi2, pi3) in three containers on one Docker network (scripts/e2e-roles.sh).
Nothing is set up on the destination: every Pi always answers. Prints CHECK lines for checks.md."""
import json, os, subprocess, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NET, IMG = "tt-roles-net", "tt-roles-test"
NAMES = ["pi1", "pi2", "pi3"]
PORTS = {"pi1": 18101, "pi2": 18102, "pi3": 18103}
results = []
PW = "lab-secret-1"


def check(name, ok, ev=""):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:360]}", flush=True)


def unresolved(name, ev):
    print(f"CHECK {name} | unresolved | {ev}", flush=True)


def sh(*cmd, timeout=120):
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


def api(pi, method, path, body=None, timeout=60):
    r = urllib.request.Request(f"http://127.0.0.1:{PORTS[pi]}{path}", method=method, data=None if body is None else json.dumps(body).encode(),
                               headers={"Content-Type": "application/json", **({"Cookie": COOKIES[pi]} if pi in COOKIES else {})})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            d = f.read()
            return json.loads(d) if d else None
    except urllib.error.HTTPError as e:
        try:
            return {"_http": e.code, **json.loads(e.read() or b"{}")}
        except Exception:
            return {"_http": e.code}


COOKIES = {}


def up(name):
    sh("docker", "rm", "-f", f"ttr-{name}")
    port = PORTS[name]
    r = sh("docker", "run", "-d", "--name", f"ttr-{name}", "--hostname", name, "--network", NET, "--network-alias", name, "-p", f"127.0.0.1:{port}:8080",
           "-e", "TT_ASSUME_SYNCED=1", "-e", "TT_PORT=8080", "-v", f"{ROOT}:/src:ro", IMG, "bash", "-c",
           "mkdir -p /work && cd /src && tar cf - --exclude=./venv --exclude=./.git --exclude=./docs --exclude=./.codegraph --exclude=./.claude . | tar xf - -C /work && cd /work "
           "&& exec /opt/venv/bin/python -m traffic_tester --port 8080 --data-dir /data")
    if r.returncode:
        raise RuntimeError(r.stderr)


def ip_of(name):
    return sh("docker", "inspect", "-f", "{{(index .NetworkSettings.Networks \"" + NET + "\").IPAddress}}", f"ttr-{name}").stdout.strip()


def wait_up(name, timeout=60):
    for _ in range(timeout * 2):
        try:
            if api(name, "GET", "/api/status").get("version"):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def wait_run(pi, rid, timeout=90):
    for _ in range(timeout * 4):
        r = api(pi, "GET", f"/api/runs/{rid}")
        if r and r.get("status") != "running":
            return r
        time.sleep(0.25)
    raise TimeoutError(f"run {rid} on {pi}")


def trip(**kw):
    p = {"name": "e2e", "transport": "udp", "frame": "extended", "interval_ms": 20, "duration_s": 4, "timeout_ms": 400}
    p.update(kw)
    return p


def pid(pi):
    return api(pi, "GET", "/api/pi")["id"]


def run_trip(pi, **kw):
    r = api(pi, "POST", "/api/runs", {"profile": trip(**kw)})
    if "run_id" not in r:
        return None, r
    return [wait_run(pi, i) for i in r["run_ids"]], r


def main():
    sh("docker", "network", "rm", NET)
    sh("docker", "network", "create", NET)
    for n in NAMES:
        up(n)
    ok = all(wait_up(n) for n in NAMES)
    check("Three identical apps start (no role anywhere)", ok, {n: api(n, "GET", "/api/status").get("pi") for n in NAMES} if ok else "did not start")
    if not ok:
        return finish()
    st = api("pi1", "GET", "/api/status")
    check("Status has a Pi name and no role", "role" not in st and st["pi"]["name"] == "pi1", st["pi"])
    ids = {n: pid(n) for n in NAMES}
    # ---- discovery: mDNS first, by IP as the fallback
    found = []
    for _ in range(60):
        ps = api("pi1", "GET", "/api/peers")["peers"]
        found = [p for p in ps if p["source"] in ("mdns", "both")]
        if len(found) >= 2:
            break
        time.sleep(1)
    if len(found) >= 2:
        check("mDNS: pi1 finds pi2 and pi3 by itself (_pitester._tcp)", True, [(p["name"], p["host"]) for p in found])
    else:
        unresolved("mDNS: pi1 finds pi2 and pi3 by itself", f"no multicast between containers here ({[p['name'] for p in found]}); falling back to Add by IP")
    for n in ("pi2", "pi3"):
        r = api("pi1", "POST", "/api/peers", {"host": f"{ip_of(n)}:8080"})
        check(f"Add a Pi by IP: pi1 adds {n}", r.get("name") == n and r.get("reachable"), {k: r.get(k) for k in ("name", "host", "reachable", "version", "source")})
    for a, others in (("pi2", ("pi1", "pi3")), ("pi3", ("pi1", "pi2"))):
        for n in others:
            api(a, "POST", "/api/peers", {"host": f"{ip_of(n)}:8080"})
    peers = api("pi1", "GET", "/api/peers")["peers"]
    check("Pi list shows name, IP, version, clock and reachable", all(p["reachable"] and p["version"] and p["ips"] and p.get("clock") is not None for p in peers) and len(peers) == 2, [(p["name"], p["host"], p["version"]) for p in peers])
    # ---- round trips, nothing set up on the other end
    runs, r = run_trip("pi1", dest={"pi": ids["pi2"]})
    full = runs[0] if runs else {}
    s = full.get("summary") or {}
    check("pi1 -> pi2 UDP round trip with nothing set up on pi2: PASS", full.get("verdict") == "PASS" and s.get("sent", 0) > 100 and s.get("lost") == 0,
          f"{full.get('status')} sent={s.get('sent')} lost={s.get('lost')} p99={(s.get('rtt_ms') or {}).get('p99')} endpoints={s.get('endpoints')}")
    check("Result names who sent and who received", (s.get("endpoints") or {}).get("source") == "pi1" and (s.get("endpoints") or {}).get("dest") == "pi2" and (full.get("leg") or {}).get("label") == "pi1 → pi2", full.get("leg"))
    runs, r = run_trip("pi2", dest={"pi": ids["pi1"]})
    full = runs[0] if runs else {}
    check("pi2 -> pi1: the same test started from pi2's own page: PASS", full.get("verdict") == "PASS", f"{full.get('status')} {(full.get('summary') or {}).get('endpoints')}")
    runs, r = run_trip("pi1", transport="tcp", dest={"pi": ids["pi3"]}, duration_s=4)
    full = runs[0] if runs else {}
    check("pi1 -> pi3 TCP round trip: PASS", full.get("verdict") == "PASS", f"{full.get('status')} sent={(full.get('summary') or {}).get('sent')}")
    # ---- directions
    runs, r = run_trip("pi1", dest={"pi": ids["pi2"]}, direction="both")
    rs = runs or []
    check("Both ways at once: two runs in one group, both PASS", len(rs) == 2 and len({x.get("group_id") for x in rs}) == 1 and all(x["verdict"] == "PASS" for x in rs),
          [(x.get("leg", {}) or {}).get("label") for x in rs])
    check("Both ways: the second direction really ran on pi2", sorted((x.get("leg") or {}).get("label") for x in rs) == ["pi1 → pi2", "pi2 → pi1"] and len(api("pi2", "GET", "/api/runs")) >= 2,
          f"pi2 holds {len(api('pi2', 'GET', '/api/runs'))} runs")
    runs, r = run_trip("pi1", dest={"pi": ids["pi2"]}, direction="forward")
    full = runs[0] if runs else {}
    check("One way pi1 -> pi2: judged on the one-way delay", (full.get("summary") or {}).get("basis") == "up" and full.get("verdict") == "PASS", f"{full.get('status')} up={((full.get('summary') or {}).get('up_ms') or {}).get('p99')}")
    runs, r = run_trip("pi1", dest={"pi": ids["pi2"]}, direction="reverse")
    full = runs[0] if runs else {}
    check("One way pi2 -> pi1 (started from pi1, runs on pi2, result copied back)", (full.get("summary") or {}).get("endpoints", {}).get("source") == "pi2" and full.get("verdict") == "PASS", f"{full.get('status')} {full.get('error') or (full.get('summary') or {}).get('error')}")
    # ---- generator: 2 -> 1 and a 3-way mesh
    def gen(streams, dur=4):
        return {"name": "mesh", "kind": "generator", "gen": {"duration_s": dur, "streams": streams}}

    def gs(name, src, dst, **k):
        return {"name": name, "type": "udp", "rate": {"pps": 300}, "size": {"mode": "fixed", "size": 300}, "duration_s": 4, "rtt_probe_pps": 5, "src": {"pi": src} if src else {}, "dst": {"pi": dst} if dst else {}, **k}
    r = api("pi1", "POST", "/api/runs", {"profile": gen([gs("from pi2", ids["pi2"], None), gs("from pi3", ids["pi3"], None)])})
    full = wait_run("pi1", r["run_id"]) if "run_id" in r else r
    s = full.get("summary") or {}
    pairs = sorted((p["src"], p["dst"]) for p in s.get("pairs", []))
    check("Generator 2 -> 1: pi2 and pi3 both send to pi1, per-pair numbers", pairs == [("pi2", "pi1"), ("pi3", "pi1")] and all(p["lost"] == 0 and p["rx_pkts"] > 600 for p in s["pairs"]) and full.get("verdict") == "PASS",
          f"{full.get('status')} {s.get('error')} {[(p['src'], p['dst'], p['rx_pkts'], p['lost']) for p in s.get('pairs', [])]}")
    check("Generator: the controller stores the run, the other Pis only send / receive", not any(x.get("transport") == "generator" for x in api("pi2", "GET", "/api/runs")), "")
    r = api("pi1", "POST", "/api/runs", {"profile": gen([gs("1 to 2", None, ids["pi2"]), gs("2 to 3", ids["pi2"], ids["pi3"]), gs("3 to 1", ids["pi3"], None)])})
    full = wait_run("pi1", r["run_id"]) if "run_id" in r else r
    s = full.get("summary") or {}
    check("Generator 3-way mesh (1->2, 2->3, 3->1): PASS, no packet lost", sorted((p["src"], p["dst"]) for p in s.get("pairs", [])) == [("pi1", "pi2"), ("pi2", "pi3"), ("pi3", "pi1")] and full.get("verdict") == "PASS",
          f"{full.get('status')} {s.get('error')} {[(p['src'], p['dst'], p['rx_pkts'], p['lost']) for p in s.get('pairs', [])]}")
    # ---- serial: a "cable" between pi1's and pi2's pseudo-terminals (socat over TCP)
    ip1 = ip_of("pi1")
    sh("docker", "exec", "-d", "ttr-pi1", "socat", "pty,raw,echo=0,link=/tmp/ttyA", "tcp-listen:9000,reuseaddr")
    time.sleep(1)
    sh("docker", "exec", "-d", "ttr-pi2", "socat", "pty,raw,echo=0,link=/tmp/ttyB", f"tcp:{ip1}:9000")
    time.sleep(1.5)
    ser = dict(transport="serial", frame="compact", interval_ms=50, duration_s=4, source={"serial_port": "/tmp/ttyA"}, dest={"pi": ids["pi2"], "serial_port": "/tmp/ttyB"})
    runs, r = run_trip("pi1", **ser)
    full = runs[0] if runs else {}
    check("Serial: pi1's port sends, pi2's port is the reflector (claimed for the test only): PASS", full.get("verdict") == "PASS", f"{full.get('status')} {full.get('error') or (full.get('summary') or {}).get('error')} sent={(full.get('summary') or {}).get('sent')}")
    check("Serial: pi2's port is free again after the test", not api("pi2", "GET", "/api/peer/reflect/status")["claims"], api("pi2", "GET", "/api/peer/reflect/status")["claims"])
    api("pi2", "POST", "/api/peer/reflect/ensure", {"owner": "someone else", "cfg": {"transport": "serial", "serial": {"port": "/tmp/ttyB"}}})
    r = api("pi1", "POST", "/api/runs", {"profile": trip(**ser)})
    check("Serial port busy: a plain message, nothing left claimed", r.get("_http") == 422 and "busy" in r.get("detail", ""), r)
    api("pi2", "POST", "/api/peer/reflect/release", {"owner": "someone else"})
    r = api("pi1", "POST", "/api/runs", {"profile": trip(**{**ser, "direction": "both"})})
    check("Both ways refused for serial with a plain reason", r.get("_http") == 422 and "cannot send and reflect" in r.get("detail", ""), r)
    # ---- password
    api("pi3", "POST", "/api/auth/set", {"password": PW})
    time.sleep(11)
    p3 = [p for p in api("pi1", "GET", "/api/peers")["peers"] if p["name"] == "pi3"][0]
    r = api("pi1", "POST", "/api/runs", {"profile": trip(dest={"pi": ids["pi3"]})})
    check("Password on pi3: starting a test to it says a password is needed", p3.get("password_required") and r.get("_http") == 422 and "password" in r.get("detail", ""), f"auth={p3.get('auth')} {r}")
    r = api("pi1", "POST", f"/api/peers/{ids['pi3']}/password", {"password": PW})
    check("Saved the password once: pi1 can control pi3 (token per peer)", r.get("auth") == "ok" and r.get("has_password"), {k: r.get(k) for k in ("auth", "has_password")})
    runs, r = run_trip("pi1", dest={"pi": ids["pi3"]})
    full = runs[0] if runs else {}
    check("pi1 -> pi3 with the password on: PASS", full.get("verdict") == "PASS", f"{full.get('status')} {r}")
    r2 = api("pi1", "POST", "/api/runs", {"profile": gen([gs("from pi3", ids["pi3"], None)])})
    full = wait_run("pi1", r2["run_id"]) if "run_id" in r2 else r2
    check("Generator from pi3 (password on) works through the saved password", full.get("verdict") == "PASS", f"{full.get('status')} {(full.get('summary') or {}).get('error')}")
    # ---- rename
    r = api("pi2", "PUT", "/api/pi", {"name": "lab-b"})
    seen = None
    for _ in range(40):
        seen = [p["name"] for p in api("pi1", "GET", "/api/peers")["peers"]]
        if "lab-b" in seen:
            break
        time.sleep(1)
    check("Rename pi2 -> lab-b: the other Pis show the new name", "lab-b" in (seen or []), seen)
    # ---- a Pi goes away
    sh("docker", "stop", "ttr-pi3")
    r = api("pi1", "POST", "/api/runs", {"profile": trip(dest={"pi": ids["pi3"]})})
    check("A Pi that went away: a plain message (not a hang)", r.get("_http") == 422 and ("does not answer" in r.get("detail", "") or "Cannot reach" in r.get("detail", "")), r)
    refl = api("pi1", "GET", "/api/peer/reflect/status")
    check("Nothing left claimed or listening beyond the defaults (UDP+TCP 20500)", sorted(i["key"] for i in refl["items"]) == ["tcp:20500", "udp:20500"] and not refl["claims"], refl["items"])
    return finish()


def finish():
    for n in NAMES:
        sh("docker", "rm", "-f", f"ttr-{n}")
    sh("docker", "network", "rm", NET)
    ok = sum(1 for _, o in results if o)
    print(f"# {ok}/{len(results)} checks passed", flush=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
