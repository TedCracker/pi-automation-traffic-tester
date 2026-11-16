#!/usr/bin/env python3
"""Phase 8 network split, proven end to end in network namespaces (needs sudo -n): two identical Pis (alpha and bravo; there are no roles) each have an Ethernet TEST port (eth0, no default route)
and a Wi-Fi-like MANAGEMENT port (wlan0, default route to a fake internet at 1.1.1.1). Heavy "update / internet" traffic is pushed out of wlan0 on both Pis while
test traffic runs alpha->bravo and bravo->alpha: the test path must show no extra delay/loss/bytes. Also: the routing self-check flags a default route on a test port.
Run via scripts/e2e-netsplit.sh. Prints 'CHECK name | pass/FAIL | evidence'."""
import json, os, signal, subprocess, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "venv", "bin", "python")
TMP = tempfile.mkdtemp(prefix="tt-split-")
results, procs = [], []
PORT = {"A": 18301, "B": 18302}
NAME = {"A": "alpha", "B": "bravo"}          # the two apps are identical; `--role` is still passed (accepted and ignored)
IP = {"A": "10.50.0.1", "B": "10.50.0.2"}


def check(name, ok, ev):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:420]}", flush=True)


def sh(*a, ns=None, **kw):
    cmd = ["sudo", "-n"] + (["ip", "netns", "exec", ns] if ns else []) + list(a)
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def call(ns, port, method, path, body=None):
    """HTTP call from inside namespace `ns` (the apps listen on 127.0.0.1 there)."""
    code = ("import urllib.request,urllib.error,json,sys;b=sys.argv[4];r=urllib.request.Request('http://127.0.0.1:%s%s'%(sys.argv[1],sys.argv[2]),method=sys.argv[3],"
            "data=b.encode() if b else None,headers={'Content-Type':'application/json'})\n"
            "try: print(urllib.request.urlopen(r,timeout=60).read().decode() or 'null')\n"
            "except urllib.error.HTTPError as e: print(json.dumps({'_http':e.code,'detail':e.read().decode()[:200]}))")
    r = sh(PY, "-c", code, str(port), path, method, json.dumps(body) if body is not None else "", ns=ns)
    try:
        return json.loads(r.stdout or "null")
    except ValueError:
        raise RuntimeError(f"{method} {path} in {ns}: {r.stdout[:200]} {r.stderr[:200]}")


def wait_run(ns, port, rid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = call(ns, port, "GET", f"/api/runs/{rid}")
        if r["status"] != "running":
            return r
        time.sleep(0.4)
    raise TimeoutError


def counters(ns, dev):
    f = lambda k: int(sh("cat", f"/sys/class/net/{dev}/statistics/{k}", ns=ns).stdout.strip() or 0)
    return {"tx": f("tx_bytes"), "rx": f("rx_bytes")}


FLOOD = ("import socket,time,sys;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);d=bytes(1200);t_end=time.time()+float(sys.argv[1]);pps=float(sys.argv[2]);n=0;t0=time.time()\n"
         "while time.time()<t_end:\n"
         "  due=int((time.time()-t0)*pps)\n"
         "  while n<due: s.sendto(d,('1.1.1.1',9999));n+=1\n"
         "  time.sleep(0.002)\n"
         "print(n*1200)")
SINK = "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('1.1.1.1',9999))\nwhile True: s.recv(2000)"


def cleanup():
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except Exception:
            pass
    sh("pkill", "-f", TMP)
    time.sleep(0.5)
    for n in ("ttsA", "ttsB", "ttsG"):
        sh("ip", "netns", "del", n)
    subprocess.run(["sudo", "-n", "rm", "-rf", TMP], capture_output=True)


def stat(r, k="rtt_ms"):
    return r["summary"][k]


try:
    if sh("true").returncode != 0:
        print("CHECK netsplit: sudo available | FAIL | sudo -n failed"); sys.exit(1)
    for n in ("ttsA", "ttsB", "ttsG"):
        sh("ip", "netns", "add", n); sh("ip", "link", "set", "lo", "up", ns=n)
    # test link A<->B (eth0 on both); management links A<->G, B<->G (wlan0 on A and B)
    sh("ip", "link", "add", "ta", "type", "veth", "peer", "name", "tb")
    sh("ip", "link", "set", "ta", "netns", "ttsA"); sh("ip", "link", "set", "tb", "netns", "ttsB")
    for ns, old, ip in (("ttsA", "ta", "10.50.0.1/24"), ("ttsB", "tb", "10.50.0.2/24")):
        sh("ip", "link", "set", old, "name", "eth0", ns=ns); sh("ip", "addr", "add", ip, "dev", "eth0", ns=ns); sh("ip", "link", "set", "eth0", "up", ns=ns)
    for side, sub in (("A", 50), ("B", 51)):
        a, g = f"w{side.lower()}", f"g{side.lower()}"
        sh("ip", "link", "add", a, "type", "veth", "peer", "name", g)
        sh("ip", "link", "set", a, "netns", f"tts{side}"); sh("ip", "link", "set", g, "netns", "ttsG")
        sh("ip", "link", "set", a, "name", "wlan0", ns=f"tts{side}")
        sh("ip", "addr", "add", f"192.168.{sub}.10/24", "dev", "wlan0", ns=f"tts{side}"); sh("ip", "link", "set", "wlan0", "up", ns=f"tts{side}")
        sh("ip", "route", "add", "default", "via", f"192.168.{sub}.1", "dev", "wlan0", "metric", "600", ns=f"tts{side}")
        sh("ip", "addr", "add", f"192.168.{sub}.1/24", "dev", g, ns="ttsG"); sh("ip", "link", "set", g, "up", ns="ttsG")
    sh("ip", "addr", "add", "1.1.1.1/32", "dev", "lo", ns="ttsG"); sh("sysctl", "-qw", "net.ipv4.ip_forward=1", ns="ttsG")
    sink = subprocess.Popen(["sudo", "-n", "ip", "netns", "exec", "ttsG", PY, "-c", SINK], start_new_session=True); procs.append(sink)
    env = ["env", "TT_ASSUME_SYNCED=1", f"PYTHONPATH={ROOT}", f"TT_MARK={TMP}"]
    for side in ("A", "B"):
        d = os.path.join(TMP, side); os.makedirs(d, exist_ok=True)
        p = subprocess.Popen(["sudo", "-n", "ip", "netns", "exec", f"tts{side}", *env, PY, "-m", "traffic_tester", "--role", side, "--port", str(PORT[side]),
                              "--host", "0.0.0.0", "--data-dir", d], stdout=open(f"{TMP}/{side}.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
        procs.append(p)
    for side in ("A", "B"):
        for _ in range(80):
            try:
                call(f"tts{side}", PORT[side], "GET", "/api/status"); break
            except Exception:
                time.sleep(0.3)
    ping = sh("ping", "-c", "2", "-W", "1", "10.50.0.2", ns="ttsA"); ping2 = sh("ping", "-c", "2", "-W", "1", "1.1.1.1", ns="ttsB")
    check("Namespace rig: alpha<->bravo over eth0 (test) and both Pis reach the fake internet over wlan0 (management)", ping.returncode == 0 and ping2.returncode == 0, f"alpha->bravo {ping.stdout.splitlines()[-1:]}; bravo->1.1.1.1 {ping2.stdout.splitlines()[-1:]}")

    for tester, other in (("A", "B"), ("B", "A")):
        tag = f"[{NAME[tester]}→{NAME[other]}] "
        tns, ons = f"tts{tester}", f"tts{other}"
        # ---------------------------------------------------------------- routing self-check on both Pis (UI/API + script)
        for side in ("A", "B"):
            r = call(f"tts{side}", PORT[side], "GET", "/api/net/check")
            peer_if = [x["dev"] for x in call(f"tts{side}", PORT[side], "GET", "/api/net/check")["peers"]]
            check(f"{tag}Routing self-check on {NAME[side]}: OK - internet route via wlan0 (management), no default route on eth0 (test port)",
                  r["status"] in ("ok", "warn") and r["internet"]["ipv4"] == "wlan0" and r["management"] == ["wlan0"] and "eth0" in r["test_ports"] and not [x for x in r["default_routes"]["v4"] if x["dev"] == "eth0"],
                  f"status={r['status']} internet={r['internet']} management={r['management']} test_ports={r['test_ports']} defaults={r['default_routes']['v4']}")
        rt = sh("ip", "route", "get", "1.1.1.1", ns=tns).stdout; rp = sh("ip", "route", "get", IP[other], ns=tns).stdout
        check(f"{tag}`ip route get`: 1.1.1.1 leaves via wlan0, the other Pi ({IP[other]}) via eth0", " dev wlan0 " in rt and " dev eth0 " in rp, f"{rt.splitlines()[0] if rt else ''} | {rp.splitlines()[0] if rp else ''}")

        def trip_run():
            call(ons, PORT[other], "POST", "/api/responder/stop")
            call(ons, PORT[other], "POST", "/api/responder/start", {"transport": "udp", "net": {"port": 18700}, "frame": "extended"})
            rid = call(tns, PORT[tester], "POST", "/api/runs", {"profile": {"name": "split", "transport": "udp", "frame": "extended", "net": {"peer_host": IP[other], "port": 18700},
                       "duration_s": 6, "interval_ms": 20, "threshold_ms": 60, "timeout_ms": 400, "idle_gaps_s": [], "summary_interval_s": 1}})
            if "run_id" not in rid:
                pl = [(x["name"], x["host"], x["ips"], x["source"], x["reachable"]) for x in call(tns, PORT[tester], "GET", "/api/peers")["peers"]]
                raise RuntimeError(f"test did not start: {rid} (peers: {pl})")
            rid = rid["run_id"]
            out = wait_run(tns, PORT[tester], rid); call(ons, PORT[other], "POST", "/api/responder/stop"); return out

        def gen_run():
            S = lambda n, d: {"name": n, "type": "udp", "direction": d, "rate": {"mbps": 20}, "size": {"mode": "fixed", "size": 1000}, "rtt_probe_pps": 20 if d == "a_to_b" else 0}
            p = {"name": "split gen", "kind": "generator", "net": {"peer_host": IP[other]},
                 "gen": {"streams": [S("fwd", "a_to_b"), S("back", "b_to_a")], "duration_s": 6, "interface": "eth0", "peer_interface": "eth0", "peer_api": f"http://{IP[other]}:{PORT[other]}",
                         "local_host": IP[tester], "rule": {"max_loss_pct": 0.5}}}
            rr = call(tns, PORT[tester], "POST", "/api/runs", {"profile": p})
            if "_http" in rr:
                raise RuntimeError(rr)
            out = wait_run(tns, PORT[tester], rr["run_id"], 180)
            if out["status"] == "failed":
                raise RuntimeError((out.get("summary") or {}).get("error"))
            return out

        def measured(with_flood):
            c0t, c0w = counters(tns, "eth0"), counters(tns, "wlan0")
            floods = []
            if with_flood:      # Update / internet traffic from BOTH Pis out of wlan0, 40 Mb/s each, for the whole measurement
                for ns in (tns, ons):
                    f = subprocess.Popen(["sudo", "-n", "ip", "netns", "exec", ns, PY, "-c", FLOOD, "16", "4200"], stdout=subprocess.PIPE, text=True, start_new_session=True); procs.append(f); floods.append(f)
                time.sleep(0.7)
            tr = trip_run(); gn = gen_run()
            sent = sum(int(f.communicate()[0] or 0) for f in floods)
            c1t, c1w = counters(tns, "eth0"), counters(tns, "wlan0")
            return tr, gn, {"eth0_tx": c1t["tx"] - c0t["tx"], "wlan0_tx": c1w["tx"] - c0w["tx"], "flood_bytes": sent}

        tr0, gn0, c0 = measured(False)
        tr1, gn1, c1 = measured(True)
        g = lambda gr, i: gr["summary"]["streams"][i]
        r0, r1 = stat(tr0), stat(tr1)
        check(f"{tag}Baseline (no internet traffic): UDP trip test and 2-stream generator PASS", tr0["verdict"] == "PASS" and gn0["verdict"] == "PASS",
              f"trip p50={r0['p50']} p99={r0['p99']} ms lost={tr0['summary']['lost']}; gen fwd lat p50={g(gn0, 0)['lat_ms']['p50']} back p50={g(gn0, 1)['lat_ms']['p50']} loss={gn0['summary']['totals']['loss_pct']}%")
        check(f"{tag}With 2 x 40 Mb/s update/internet traffic leaving over wlan0: test results unchanged (PASS, no loss)", tr1["verdict"] == "PASS" and gn1["verdict"] == "PASS" and tr1["summary"]["lost"] == 0 and gn1["summary"]["totals"]["lost"] == 0,
              f"trip p50={r1['p50']} p99={r1['p99']} lost={tr1['summary']['lost']}; gen fwd p50={g(gn1, 0)['lat_ms']['p50']} back p50={g(gn1, 1)['lat_ms']['p50']} lost={gn1['summary']['totals']['lost']}")
        d_rtt = abs(r1["p50"] - r0["p50"]); d_fwd = abs(g(gn1, 0)["lat_ms"]["p50"] - g(gn0, 0)["lat_ms"]["p50"]); d_back = abs(g(gn1, 1)["lat_ms"]["p50"] - g(gn0, 1)["lat_ms"]["p50"])
        check(f"{tag}Timing unaffected: median round trip / one-way delay shift < 0.5 ms with the internet load running", d_rtt < 0.5 and d_fwd < 0.5 and d_back < 0.5,
              f"trip p50 {r0['p50']}->{r1['p50']} ms; gen fwd p50 {g(gn0, 0)['lat_ms']['p50']}->{g(gn1, 0)['lat_ms']['p50']}; back {g(gn0, 1)['lat_ms']['p50']}->{g(gn1, 1)['lat_ms']['p50']}; jitter {g(gn0, 0)['jitter_ms']}->{g(gn1, 0)['jitter_ms']}; p99 trip {r0['p99']}->{r1['p99']}")
        ratio = c1["eth0_tx"] / max(c0["eth0_tx"], 1)
        check(f"{tag}The internet traffic did NOT use the test port: eth0 sent the same bytes with and without it; wlan0 carried the load", 0.9 <= ratio <= 1.12 and c1["wlan0_tx"] >= 0.8 * c1["flood_bytes"] / 2 and c0["wlan0_tx"] < 0.02 * c1["wlan0_tx"],
              f"eth0 tx without={c0['eth0_tx']} with={c1['eth0_tx']} (x{ratio:.3f}); wlan0 tx without={c0['wlan0_tx']} with={c1['wlan0_tx']} (flood sent by this Pi ~{c1['flood_bytes'] // 2})")

        # ---------------------------------------------------------------- misconfiguration is caught
        sh("ip", "route", "add", "default", "via", IP[other], "dev", "eth0", "metric", "100", ns=tns)
        rb = call(tns, PORT[tester], "GET", "/api/net/check")
        sc = subprocess.run(["sudo", "-n", "ip", "netns", "exec", tns, "env", f"PYTHONPATH={ROOT}", PY, "-m", "traffic_tester.netcheck"], capture_output=True, text=True)
        check(f"{tag}Misconfiguration (default route added on the test port eth0) is flagged: status PROBLEM, internet would use eth0, fix command shown, script exit 2",
              rb["status"] == "bad" and rb["internet"]["ipv4"] == "eth0" and any("net-split.sh --apply" in c["fix"] for c in rb["checks"]) and sc.returncode == 2,
              f"status={rb['status']} internet={rb['internet']} script rc={sc.returncode} first={sc.stdout.splitlines()[:2]}")
        sh("ip", "route", "del", "default", "via", IP[other], "dev", "eth0", "metric", "100", ns=tns)
        rok = call(tns, PORT[tester], "GET", "/api/net/check")
        check(f"{tag}After removing it the self-check is OK again", rok["status"] in ("ok", "warn") and rok["internet"]["ipv4"] == "wlan0", f"status={rok['status']} internet={rok['internet']}")
        # ---------------------------------------------------------------- fully offline: no Wi-Fi route at all
        sh("ip", "route", "del", "default", ns=tns)
        roff = call(tns, PORT[tester], "GET", "/api/net/check"); trf = trip_run()
        check(f"{tag}Offline (no default route): self-check says the app works fully offline, and a test still PASSES", roff["status"] == "ok" and any("No internet route" in c["title"] for c in roff["checks"]) and trf["verdict"] == "PASS",
              f"status={roff['status']} verdict={trf['verdict']} p99={stat(trf)['p99']} ms")
        sub = 50 if tester == "A" else 51
        sh("ip", "route", "add", "default", "via", f"192.168.{sub}.1", "dev", "wlan0", "metric", "600", ns=tns)
except BaseException as ex:
    import traceback; traceback.print_exc()
    check("netsplit e2e ran to completion", False, f"{type(ex).__name__}: {ex}")
finally:
    cleanup()
    bad = [n for n, ok in results if not ok]
    print(f"# {len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""), flush=True)
    sys.exit(1 if bad else 0)
