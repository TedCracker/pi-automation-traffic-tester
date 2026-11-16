#!/usr/bin/env python3
"""End-to-end local test: two identical app instances (named alpha and bravo; there are no roles) over socat pty pairs, UDP, TCP and netem,
plus the no-roles way: each instance adds the other with POST /api/peers and a test names it as destination (round trip, both ways at once, one way each way).
Run via scripts/e2e-local.sh. Prints 'CHECK <name> | pass/fail | evidence' lines (-> docs/dev/checks.md)."""
import asyncio, json, os, re, shutil, signal, subprocess, sys, tempfile, time, zipfile, io
import urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "venv", "bin", "python")
TMP = tempfile.mkdtemp(prefix="tt-e2e-")
procs, results = [], []
ONLY = set(sys.argv[1:])           # optional: run only these sections
REV = os.environ.get("E2E_REVERSE") == "1"   # reverse run: the tests are started on the second instance (bravo) and name the first (alpha) as destination
TAG = "[bravo→alpha] " if REV else ""


def check(name, ok, evidence):
    name = TAG + name
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {evidence}", flush=True)


def unresolved(name, evidence):
    print(f"CHECK {TAG}{name} | unresolved | {evidence}", flush=True)


def http(port, method, path, body=None, raw=False):
    hp = port if isinstance(port, str) else f"127.0.0.1:{port}"
    req = urllib.request.Request(f"http://{hp}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = r.read()
            return d if raw else (json.loads(d) if d else None)
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "detail": json.loads(e.read() or b"{}").get("detail")}


def start_app(role, port, name, sudo=False, env=None, ns=None, reach=None, host=None):
    """`--role A|B` is still accepted (older install scripts pass it) and ignored: every instance is the same."""
    d = os.path.join(TMP, name)
    cmd = [PY, "-m", "traffic_tester", "--role", role, "--port", str(port), "--host", host or ("127.0.0.1" if not ns else "0.0.0.0"), "--data-dir", d]
    e = dict(os.environ, TT_TEST_HOOKS="1", TT_ASSUME_SYNCED="1", PYTHONPATH=ROOT)
    e.update(env or {})
    if sudo or ns:
        pre = ["sudo", "-n"] + (["ip", "netns", "exec", ns] if ns else []) + ["env", "TT_TEST_HOOKS=1", f"TT_ASSUME_SYNCED={(env or {}).get('TT_ASSUME_SYNCED', '1')}", f"PYTHONPATH={ROOT}",
                                                                                                                  "TT_NO_DISCOVERY=1", "TT_NO_DEFAULT_REFLECTOR=1"]      # sudo drops the environment: no mDNS, no shared port 20500
        cmd = pre + cmd
    p = subprocess.Popen(cmd, env=e, stdout=open(os.path.join(TMP, name + ".log"), "w"), stderr=subprocess.STDOUT, start_new_session=True)
    procs.append((p, sudo or bool(ns)))
    for _ in range(60):
        try:
            http(reach or port, "GET", "/api/status"); return p
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"{name} did not start; see {TMP}/{name}.log")


def wait_run(port, rid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = http(port, "GET", f"/api/runs/{rid}")
        if r["status"] != "running":
            return r
        time.sleep(0.4)
    raise TimeoutError("run did not finish")


def prof(**kw):
    base = {"name": "e2e", "transport": "serial", "duration_s": 6, "interval_ms": 50, "threshold_ms": 60, "timeout_ms": 500,
            "idle_gaps_s": [], "frame": "compact", "summary_interval_s": 1}
    base.update(kw)
    return base


def run(port, p, **extra):
    r = http(port, "POST", "/api/runs", {"profile": p, **extra})
    if "_http" in r:
        raise RuntimeError(f"start failed: {r}")
    out = wait_run(port, r["run_id"])
    if "rtt_ms" not in (out.get("summary") or {}):
        raise RuntimeError(f"run produced no results: status={out['status']} summary={out.get('summary')}")
    return out


def cleanup():
    for p, sudo in procs:
        try:
            if sudo:
                subprocess.run(["sudo", "-n", "pkill", "-TERM", "-g", str(p.pid)], capture_output=True)
                subprocess.run(["sudo", "-n", "pkill", "-f", f"traffic_tester.*{TMP}"], capture_output=True)
            else:
                os.killpg(p.pid, signal.SIGTERM)
        except Exception:
            pass
    time.sleep(1)
    for p, _ in procs:
        try:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            pass
    subprocess.run(["sudo", "-n", "ip", "netns", "del", "tt-b"], capture_output=True)
    subprocess.run(["sudo", "-n", "ip", "link", "del", "ttveth-a"], capture_output=True)
    subprocess.run(["sudo", "-n", "rm", "-rf", TMP], capture_output=True)
    shutil.rmtree(TMP, ignore_errors=True)


def sec(name):
    return not ONLY or name in ONLY


PA, PB = 18080, 18081
A, B = (PB, PA) if REV else (PA, PB)      # A = the instance the tests are started on, B = the one they name as destination / that answers
try:
    print(f"# e2e tmp={TMP}", flush=True)
    start_app("A", PA, "a"); start_app("B", PB, "b")        # `--role` is accepted and ignored
    http(PA, "PUT", "/api/pi", {"name": "alpha"}); http(PB, "PUT", "/api/pi", {"name": "bravo"})
    sa, sb = http(PA, "GET", "/api/status"), http(PB, "GET", "/api/status")
    check("Two identical instances start (no role in /api/status; --role is accepted and ignored; each has its own name and id)",
          "role" not in sa and "role" not in sb and sa["pi"]["name"] == "alpha" and sb["pi"]["name"] == "bravo" and sa["pi"]["id"] != sb["pi"]["id"],
          f"alpha:{PA} bravo:{PB} dirs={TMP}/a,{TMP}/b; tests are started on {'bravo' if REV else 'alpha'}; pi={sa['pi']} / {sb['pi']}")

    # ---------------------------------------------------------------- serial
    sc = subprocess.Popen(["socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0"], stderr=subprocess.PIPE, text=True, start_new_session=True)
    procs.append((sc, False))
    ptys = []
    while len(ptys) < 2:
        m = re.search(r"PTY is (\S+)", sc.stderr.readline())
        if m: ptys.append(m.group(1))
    print(f"# socat ptys: {ptys}", flush=True)
    rb = http(B, "POST", "/api/responder/start", {"transport": "serial", "serial": {"port": ptys[0], "baud": 19200}})
    check("Answering end starts on pty 1 (POST /api/responder/start, the alias of the reflector claim)", rb.get("running"), json.dumps(rb)[:160])
    ser_prof = lambda **k: prof(serial={"port": ptys[1], "baud": 19200}, **k)

    if sec("serial"):
        pr = http(A, "POST", "/api/probe", ser_prof())
        check("Pairing probe finds the answering end over serial", pr.get("ok"), json.dumps(pr)[:200])
        # built-in preset with overrides = real 'DER trip test' settings, shortened
        presets = {p["name"]: p for p in http(A, "GET", "/api/profiles")}
        der = presets["DER trip test"]
        r = run(A, {**{k: v for k, v in der.items() if k not in ("id",)}, "serial": {**der["serial"], "port": ptys[1]},
                    "duration_s": 8, "idle_gaps_s": [1, 2], "idle_repeats": 2})
        s = r["summary"]
        check("Serial DER-trip run finishes with PASS", r["status"] == "finished" and r["verdict"] == "PASS",
              f"status={r['status']} verdict={r['verdict']} p99={s['rtt_ms']['p99']}ms max={s['rtt_ms']['max']}ms")
        check("Serial: ~160 messages sent, none lost, none late", 150 <= s["sent"] <= 165 and s["lost"] == 0 and s["over_threshold"] == 0,
              f"sent={s['sent']} received={s['received']} lost={s['lost']} late={s['over_threshold']} dup={s['duplicates']} ooo={s['out_of_order']} corrupted={s['corrupted']}")
        check("Serial: percentiles + jitter reported", all(s["rtt_ms"][k] is not None for k in ("p50", "p95", "p99", "max")) and s["jitter_ms"] is not None,
              f"rtt_ms={s['rtt_ms']} jitter_ms={s['jitter_ms']}")
        check("Serial: idle-first stats for each configured gap", s["idle"]["count"] == 4 and [g["gap_s"] for g in s["idle"]["gaps"]] == [1.0, 2.0],
              f"idle={json.dumps(s['idle'])[:300]}")
        msgs = http(A, "GET", f"/api/runs/{r['id']}/messages")
        check("Serial: per-message rows + per-second intervals stored in SQLite",
              len(msgs) == s["sent"] + 4 and len(r["intervals"]) >= 7, f"messages={len(msgs)} intervals={len(r['intervals'])}")
        csv = http(A, "GET", f"/api/runs/{r['id']}/export.csv", raw=True).decode()
        csvi = http(A, "GET", f"/api/runs/{r['id']}/export.csv?kind=intervals", raw=True).decode()
        js = json.loads(http(A, "GET", f"/api/runs/{r['id']}/export.json", raw=True))
        check("CSV (messages + intervals) and JSON export work", csv.startswith("seq,phase") and len(csv.splitlines()) == len(msgs) + 1 and csvi.startswith("t_offset_s") and js["summary"]["sent"] == s["sent"],
              f"messages.csv lines={len(csv.splitlines())} intervals.csv lines={len(csvi.splitlines())} json.keys={sorted(js)[:6]}")
        sid = r["session_id"]
        z = zipfile.ZipFile(io.BytesIO(http(A, "GET", f"/api/sessions/{sid}/export.zip", raw=True)))
        check("Session ZIP export contains every run", f"run-{r['id']}/messages.csv" in z.namelist(), f"{z.namelist()[:5]}")
        import re as _re0, zlib as _z0, base64 as _b0
        pdf0 = http(A, "GET", f"/api/report.pdf?run_ids={r['id']}", raw=True)
        txt0 = b""
        for m0 in _re0.finditer(rb"stream\r?\n(.*?)endstream", pdf0, _re0.S):
            try: txt0 += _z0.decompress(_b0.a85decode(m0.group(1).strip(), adobe=True))
            except Exception: pass
        check("PDF report of a trip-test run (verdict, percentiles, charts) is generated", pdf0[:5] == b"%PDF-" and len(pdf0) > 3000 and b"PASS" in txt0, f"pdf {len(pdf0)} bytes; text has PASS={b'PASS' in txt0}")
        usb = os.path.join(TMP, "usb"); os.makedirs(usb)
        # USB export via TT_USB_DIR is set at app start -> test with a dedicated instance
        ua = start_app("A", 18090, "usb-a", env={"TT_USB_DIR": usb})
        no_usb = http(A, "POST", "/api/usb/save", {})
        check("USB save refuses cleanly when no drive is mounted", no_usb.get("_http") == 409, json.dumps(no_usb))
        sv = http(18090, "POST", "/api/usb/save", {})
        files = os.listdir(usb)
        check("USB save writes a session ZIP to the mounted drive", sv.get("ok") and files and files[0].endswith(".zip"), f"{sv} files={files}")

    if sec("hooks"):
        # --- late / lost detection and pass/fail flip, via responder test hooks (serial)
        http(B, "POST", "/api/responder/stop")
        http(B, "POST", "/api/responder/start", {"transport": "serial", "serial": {"port": ptys[0], "baud": 19200}, "delay_ms": 80})
        r = run(A, ser_prof(duration_s=5, idle_gaps_s=[1], idle_repeats=1))
        s = r["summary"]
        check("Late messages counted and verdict flips to FAIL (answering end +80 ms)", r["verdict"] == "FAIL" and s["over_threshold"] >= 0.9 * s["sent"] and s["lost"] == 0,
              f"verdict={r['verdict']} late={s['over_threshold']}/{s['sent']} p50={s['rtt_ms']['p50']}ms reasons={s['verdict']['reasons']}")
        http(B, "POST", "/api/responder/stop")
        http(B, "POST", "/api/responder/start", {"transport": "serial", "serial": {"port": ptys[0], "baud": 19200}, "drop_pct": 20})
        r = run(A, ser_prof(duration_s=6))
        s = r["summary"]
        check("Lost messages counted (answering end drops 20 %) and verdict FAIL", r["verdict"] == "FAIL" and 8 <= s["loss_pct"] <= 35,
              f"loss={s['lost']}/{s['sent']} ({s['loss_pct']} %) reasons={s['verdict']['reasons']}")
        http(B, "POST", "/api/responder/stop")
        r = run(A, ser_prof(duration_s=2)) if False else None
        # no responder at all
        rr = http(A, "POST", "/api/runs", {"profile": ser_prof(duration_s=2)})
        r = wait_run(A, rr["run_id"])
        check("Nobody answering: run fails with a plain 'did not answer' message", r["status"] == "failed" and "did not answer" in (r["summary"] or {}).get("error", ""),
              f"status={r['status']} error={(r['summary'] or {}).get('error')}")
        http(B, "POST", "/api/responder/start", {"transport": "serial", "serial": {"port": ptys[0], "baud": 19200}})
        # rule: duplicates / second run rejected while busy
        rr = http(A, "POST", "/api/runs", {"profile": ser_prof(duration_s=4)})
        busy = http(A, "POST", "/api/runs", {"profile": ser_prof(duration_s=4)})
        check("Only one run at a time (second start returns 409)", busy.get("_http") == 409, json.dumps(busy))
        time.sleep(2.0)       # let pairing finish and some messages flow
        http(A, "POST", f"/api/runs/{rr['run_id']}/stop")
        r = wait_run(A, rr["run_id"])
        check("Stop button ends a run early; partial results kept", r["status"] == "stopped" and r["summary"]["sent"] >= 1, f"status={r['status']} sent={r['summary']['sent']}")

    # ---------------------------------------------------------------- profiles API
    if sec("profiles"):
        profs = http(A, "GET", "/api/profiles")
        b = [p for p in profs if p["builtin"]]
        trip = [p for p in b if p.get("kind", "trip") == "trip"][0]
        genp = [p for p in b if p.get("kind") == "generator"][0]
        e1 = http(A, "PUT", f"/api/profiles/{trip['id']}", {**trip, "threshold_ms": 55})            # built-in trip tests can be edited (kept as an override) ...
        d = http(A, "PUT", f"/api/profiles/{genp['id']}", {**genp, "name": genp["name"]})
        check("Built-in tests: a trip test can be edited, generator presets stay read-only (edit → 403), none can be deleted (→ 403)",
              e1.get("_http") is None and d.get("_http") == 403 and http(A, "DELETE", f"/api/profiles/{trip['id']}").get("_http") == 403, f"edit trip -> threshold {e1.get('threshold_ms', e1)}; edit generator preset -> {d.get('_http')}")
        http(A, "PUT", f"/api/profiles/{trip['id']}", {**trip})
        c = http(A, "POST", f"/api/profiles/{b[0]['id']}/duplicate")
        check("Duplicate to edit creates an editable saved copy", not c["builtin"] and c["name"].endswith("(copy)"), c["name"])
        c2 = http(A, "PUT", f"/api/profiles/{c['id']}", {**c, "threshold_ms": 45})
        exp = json.loads(http(A, "GET", f"/api/profiles/export?ids={c['id']}", raw=True))
        imp = http(B, "POST", "/api/profiles/import", exp)
        check("Profile export (JSON file) → import on the other Pi round-trips", c2["threshold_ms"] == 45 and imp["imported"][0]["threshold_ms"] == 45 and exp["format"] == "traffic-tester-profiles",
              f"exported {exp['profiles'][0]['name']} → imported id={imp['imported'][0]['id']}")
        bad = http(A, "POST", "/api/profiles/import", {"format": "nope"})
        check("Import rejects a non-profile file with a plain message", bad.get("_http") == 422, json.dumps(bad))


    # ---------------------------------------------------------------- no fixed roles: peers + Send from / Send to / Direction
    if sec("peers"):
        import re as _rp, zlib as _zp, base64 as _bp
        pa, pb = ("127.0.0.1", PA), ("127.0.0.1", PB)
        ea = http(PB, "POST", "/api/peers", {"host": f"127.0.0.1:{PA}"})          # bravo adds alpha ...
        eb = http(PA, "POST", "/api/peers", {"host": f"127.0.0.1:{PB}"})          # ... and alpha adds bravo (by IP: no mDNS on one host)
        check("Add a Pi by IP: each instance finds the other, with its name, id and clock",
              "_http" not in ea and "_http" not in eb and eb["name"] == "bravo" and ea["name"] == "alpha" and eb["reachable"] and ea["reachable"] and eb["id"] == http(PB, "GET", "/api/pi")["id"] and eb["source"] == "manual",
              f"alpha sees {eb.get('name')} reachable={eb.get('reachable')} clock={eb.get('clock')}; bravo sees {ea.get('name')}")
        bad_add = http(PA, "POST", "/api/peers", {"host": "127.0.0.1:9"})
        check("Add a Pi by IP: nothing there -> plain 422 message", bad_add.get("_http") == 422 and bad_add.get("detail"), json.dumps(bad_add)[:160])
        lst = http(PA, "GET", "/api/peers")
        check("GET /api/peers: me + peers + discovery state; the list carries no passwords", lst["me"]["name"] == "alpha" and [p["name"] for p in lst["peers"]] == ["bravo"] and '"password"' not in json.dumps(lst) and all("has_password" in x for x in lst["peers"]), json.dumps(lst)[:260])
        ID = {"alpha": ea["id"], "bravo": eb["id"]}
        # --- a UDP round trip started on either instance, naming the other as destination (the reflecting end is claimed for the test and released after)
        for who, frm, to, dest_id, port in (("alpha", PA, PB, eb["id"], 18610), ("bravo", PB, PA, ea["id"], 18611)):
            other = "bravo" if who == "alpha" else "alpha"
            r = run(frm, prof(transport="udp", frame="extended", net={"port": port}, dest={"pi": dest_id}, duration_s=5, interval_ms=20))
            s = r["summary"]; ep = s.get("endpoints") or {}
            refl = http(to, "GET", "/api/status")["reflector"]
            check(f"Test started on {who} naming {other} as destination: PASS, the answering end needs nothing set up",
                  r["verdict"] == "PASS" and s["lost"] == 0 and 200 <= s["sent"] <= 260 and ep.get("source") == who and ep.get("dest") == other and ep.get("kind") == "round_trip",
                  f"verdict={r['verdict']} sent={s['sent']} lost={s['lost']} p99={s['rtt_ms']['p99']}ms endpoints={ep}")
            check(f"{other}'s reflector claim is released after the test", not [i for i in refl.get("items", []) if i.get("transport") == "udp"] and not [k for k in (refl.get("claims") or {}) if k.startswith(("udp", "tcp"))], json.dumps(refl)[:200])
            check(f"Result row names who sent and who answered ({who} → {other})", (r.get("leg") or {}).get("label") == f"{who} \u2192 {other}" and r.get("group_id") is None, json.dumps(r.get("leg")))
        # --- both ways at once: two runs, one group
        both = prof(transport="udp", frame="extended", source={"port": 18612}, dest={"pi": eb["id"], "port": 18613}, direction="both", duration_s=5, interval_ms=20)
        rr = http(PA, "POST", "/api/runs", {"profile": both})
        check("Both ways at once: one start returns one run id per direction", "_http" not in rr and len(rr.get("run_ids", [])) == 2, json.dumps(rr))
        rs = [wait_run(PA, i) for i in rr["run_ids"]]
        labels = sorted((r["leg"] or {}).get("label", "") for r in rs)
        check("Both ways at once: two runs of one group, alpha → bravo and bravo → alpha, both PASS, nothing lost",
              labels == ["alpha \u2192 bravo", "bravo \u2192 alpha"] and len({r["group_id"] for r in rs}) == 1 and rs[0]["group_id"] and all(r["verdict"] == "PASS" and r["summary"]["lost"] == 0 for r in rs),
              f"labels={labels} group={[r['group_id'] for r in rs]} verdicts={[r['verdict'] for r in rs]} sent={[r['summary']['sent'] for r in rs]}")
        csvh = http(PA, "GET", f"/api/runs/{rs[0]['id']}/export.csv", raw=True).decode().splitlines()
        check("CSV export carries the leg label and group id", csvh[0].endswith("group_id,leg,sender,answering_pi,basis") and csvh[1].split(",")[-5] == str(rs[0]["group_id"]) and rs[0]["leg"]["label"] in csvh[1], f"{csvh[0][-60:]} | {csvh[1][-60:]}")
        pdf = http(PA, "GET", f"/api/report.pdf?run_ids={rs[0]['id']},{rs[1]['id']}", raw=True)
        txt = b""
        for m in _rp.finditer(rb"stream\r?\n(.*?)endstream", pdf, _rp.S):
            try: txt += _zp.decompress(_bp.a85decode(m.group(1).strip(), adobe=True))
            except Exception: pass
        check("PDF report names the Pis that sent and answered (no A/B)", pdf[:5] == b"%PDF-" and b"Sent by" in txt and b"alpha" in txt and b"bravo" in txt and b"answered by" in txt and not re.search(rb"Pi [AB]\b", txt),
              f"pdf {len(pdf)} bytes; Sent by={b'Sent by' in txt} alpha={b'alpha' in txt} bravo={b'bravo' in txt}")
        # --- one way, each way (clocks are assumed synced on one host: TT_ASSUME_SYNCED=1)
        for dname, d_, src_name in (("One way: from → to", "forward", "alpha"), ("One way: to → from", "reverse", "bravo")):
            r = run(PA, prof(transport="udp", frame="extended", net={"port": 18614}, dest={"pi": eb["id"]}, direction=d_, duration_s=5, interval_ms=20))
            s = r["summary"]; ep = s.get("endpoints") or {}
            check(f"{dname} (clocks assumed synced): judged on the one-way delay, sender is {src_name}", r["verdict"] == "PASS" and s.get("basis") == "up" and s.get("one_way") and s["up_ms"]["p50"] is not None and ep.get("source") == src_name and ep.get("kind") == "one_way",
                  f"verdict={r['verdict']} basis={s.get('basis')} up_ms={s.get('up_ms')} endpoints={ep}")
        # --- one way needs synced clocks: an instance that is not verifiably synced refuses, with the reason
        ua2 = start_app("A", 18095, "a-unsynced2", env={"TT_ASSUME_SYNCED": "0"})
        e2 = http(18095, "POST", "/api/peers", {"host": f"127.0.0.1:{PB}"})
        rr = http(18095, "POST", "/api/runs", {"profile": prof(transport="udp", frame="extended", net={"port": 18615}, dest={"pi": e2["id"]}, direction="forward", duration_s=3)})
        r = wait_run(18095, rr["run_id"]) if "run_id" in rr else rr
        err = ((r.get("summary") or {}).get("error") or r.get("detail") or "")
        check("One way without synced clocks refuses to start and says why (never a silent round trip)", r.get("status") == "failed" and "clock" in err.lower(), f"status={r.get('status')} error={err[:200]}")
        # --- serial: pick which Pi's port answers; both ways is refused for serial
        http(B, "POST", "/api/responder/stop")                       # the serial port is claimed by the test below: it must not be in use
        r = run(A, ser_prof(duration_s=4, dest={"pi": ID["bravo"] if not REV else ID["alpha"], "serial_port": ptys[0]}))
        check("Serial test naming the other Pi's port as destination: its port is claimed for the test, PASS", r["verdict"] == "PASS" and r["summary"]["lost"] == 0 and (r["summary"].get("endpoints") or {}).get("dest") == ("bravo" if not REV else "alpha"),
              f"verdict={r['verdict']} sent={r['summary']['sent']} endpoints={r['summary'].get('endpoints')}")
        http(B, "POST", "/api/responder/start", {"transport": "serial", "serial": {"port": ptys[0], "baud": 19200}})
        sb2 = http(A, "POST", "/api/runs", {"profile": ser_prof(direction="both")})
        check("Both ways at once is refused for serial with a plain reason", sb2.get("_http") in (400, 422) and "cannot send and reflect" in json.dumps(sb2), json.dumps(sb2)[:200])
        # --- a Pi with a web password: "needs password" until its password is saved here (a third instance, so alpha and bravo stay open for the rest of the script)
        start_app("B", 18096, "pw-b"); http(18096, "PUT", "/api/pi", {"name": "locked"}); http(18096, "POST", "/api/auth/set", {"password": "pw-e2e-1234"})
        ew = http(PA, "POST", "/api/peers", {"host": "127.0.0.1:18096"})
        rr = http(PA, "POST", "/api/runs", {"profile": prof(transport="udp", frame="extended", net={"port": 18616}, dest={"pi": ew["id"]}, duration_s=3)})
        check("A password-protected Pi shows 'needs password' and a test against it is refused with a plain message", ew["auth"] == "needs_password" and ew["password_required"] and rr.get("_http") in (409, 422) and "password" in json.dumps(rr), json.dumps(rr)[:200])
        sp = http(PA, "POST", f"/api/peers/{ew['id']}/password", {"password": "pw-e2e-1234"})
        r = run(PA, prof(transport="udp", frame="extended", net={"port": 18617}, dest={"pi": ew["id"]}, duration_s=3, interval_ms=20))
        check("After saving its password a test against it runs (one login per peer, password never listed)", sp.get("auth") == "ok" and r["verdict"] == "PASS" and "pw-e2e-1234" not in json.dumps(http(PA, "GET", "/api/peers")), f"auth={sp.get('auth')} verdict={r['verdict']}")
        http(PA, "DELETE", f"/api/peers/{ew['id']}")
        # --- remove
        dl = http(PA, "DELETE", f"/api/peers/{eb['id']}")
        check("A hand-added Pi can be removed", dl == {"ok": True} and http(PA, "GET", "/api/peers")["peers"] == [], json.dumps(dl))
        http(PB, "DELETE", f"/api/peers/{ea['id']}")                  # forget alpha on bravo too (REVERSE runs start tests on bravo)
        # (bravo is forgotten again: a Pi that is in the list is controlled, an address that is not is only ever answered; the sections below test the typed-address way)

    # ---------------------------------------------------------------- UDP
    if sec("udp"):
        http(B, "POST", "/api/responder/stop")
        rb = http(B, "POST", "/api/responder/start", {"transport": "udp", "net": {"port": 18500}, "frame": "extended"})
        r = run(A, prof(transport="udp", frame="extended", net={"peer_host": "127.0.0.1", "port": 18500}, duration_s=6, interval_ms=20, one_way=True, idle_gaps_s=[1], idle_repeats=1))
        s = r["summary"]
        check("UDP localhost run PASS, ~300 msgs, none lost", r["verdict"] == "PASS" and 280 <= s["sent"] <= 305 and s["lost"] == 0,
              f"verdict={r['verdict']} sent={s['sent']} lost={s['lost']} p99={s['rtt_ms']['p99']}ms")
        check("UDP one-way (up/down) reported when clocks are within limit", s["one_way"] and s["up_ms"]["p50"] is not None and s["down_ms"]["p50"] is not None,
              f"up_ms={s['up_ms']} down_ms={s['down_ms']} peer={s['peer']}")
        os.environ.pop("TT_ASSUME_SYNCED", None)
        a2 = start_app("A", 18091, "a-unsynced", env={"TT_ASSUME_SYNCED": "0"})
        r2 = run(18091, prof(transport="udp", frame="extended", net={"peer_host": "127.0.0.1", "port": 18500}, duration_s=2, interval_ms=50, one_way=True))
        s2 = r2["summary"]
        check("One-way (\"Measure each direction\") falls back to round-trip-only when the sending Pi's clock is not verifiably synced", not s2["one_way"] and "up_ms" not in s2 and "one_way_unavailable" in s2["peer"],
              f"one_way={s2['one_way']} reason={s2['peer'].get('one_way_unavailable')}")
        pr = http(A, "POST", "/api/probe", prof(transport="udp", net={"peer_host": "127.0.0.1", "port": 18599}, frame="compact"))
        check("UDP with nobody listening: probe says it did not answer", pr.get("ok") is False and "did not answer" in pr.get("error", ""), json.dumps(pr))
        http(B, "POST", "/api/responder/stop")

    # ---------------------------------------------------------------- TCP
    if sec("tcp"):
        rb = http(B, "POST", "/api/responder/start", {"transport": "tcp", "net": {"port": 18501}, "frame": "extended"})
        rr = http(A, "POST", "/api/runs", {"profile": prof(transport="tcp", frame="extended", net={"peer_host": "127.0.0.1", "port": 18501}, duration_s=12, interval_ms=50, timeout_ms=400)})
        time.sleep(4)
        http(B, "POST", "/api/responder/stop"); time.sleep(1.5)
        http(B, "POST", "/api/responder/start", {"transport": "tcp", "net": {"port": 18501}, "frame": "extended"})
        r = wait_run(A, rr["run_id"]); s = r["summary"]; t = s["extra"]["tcp"]
        check("TCP: session set-up time measured", t["setup_ms"] is not None and t["sessions"] >= 2, f"setup_ms={t['setup_ms']} sessions={t['sessions']} avg_connect_ms={t['connect_ms_avg']}")
        check("TCP: session drop + reconnect timed (outage_ms, connect_ms)", len(t["reconnects"]) >= 1 and t["reconnects"][0].get("outage_ms", 0) > 1000,
              f"reconnects={t['reconnects']} lost={s['lost']}/{s['sent']}")
        check("TCP: messages during the outage counted as lost, run completes", s["lost"] > 10 and r["status"] == "finished", f"status={r['status']} lost={s['lost']}")
        http(B, "POST", "/api/responder/stop")

    # ---------------------------------------------------------------- netem in network namespaces
    sh = lambda *a: subprocess.run(["sudo", "-n", *a], capture_output=True, text=True)
    NSB, RA = "10.99.0.2:18082", 18083      # NSB = the second instance inside namespace tt-b (10.99.0.2); RA = the first (root namespace, 10.99.0.1)
    # TST = the instance the tests are started on; RSP = the one that answers; the other names follow the direction (alpha -> bravo normal, bravo -> alpha reverse)
    TST, RSP = (NSB, f"127.0.0.1:{RA}") if REV else (RA, NSB)
    TIF, PIF = ("ttveth-b", "ttveth-a") if REV else ("ttveth-a", "ttveth-b")          # sending / answering test interface
    TIP, PIP = ("10.99.0.2", "10.99.0.1") if REV else ("10.99.0.1", "10.99.0.2")
    TNS = ["ip", "netns", "exec", "tt-b"] if REV else []                                   # how to reach the sending end's interface with tc / sniffers
    PEER_API = f"http://10.99.0.1:{RA}" if REV else "http://10.99.0.2:18082"
    ns_ready = [False]
    netem_ok, netem_why = [True], [""]

    def ensure_ns():
        if ns_ready[0]:
            return True
        if sh("true").returncode != 0:
            check("netns: sudo available", False, "sudo -n failed")
            return False
        sh("ip", "netns", "add", "tt-b"); sh("ip", "link", "add", "ttveth-a", "type", "veth", "peer", "name", "ttveth-b")
        sh("ip", "link", "set", "ttveth-b", "netns", "tt-b")
        sh("ip", "addr", "add", "10.99.0.1/24", "dev", "ttveth-a"); sh("ip", "link", "set", "ttveth-a", "up")
        sh("ip", "netns", "exec", "tt-b", "ip", "addr", "add", "10.99.0.2/24", "dev", "ttveth-b")
        sh("ip", "netns", "exec", "tt-b", "ip", "link", "set", "ttveth-b", "up"); sh("ip", "netns", "exec", "tt-b", "ip", "link", "set", "lo", "up")
        start_app("B", 18082, "nsb", ns="tt-b", reach=NSB)
        start_app("A", RA, "rootA", sudo=True, host="0.0.0.0" if REV else None)
        r = sh("tc", "qdisc", "replace", "dev", "ttveth-a", "root", "netem", "delay", "1ms")
        netem_ok[0], netem_why[0] = r.returncode == 0, (r.stderr or r.stdout).strip()[:120]
        sh("tc", "qdisc", "del", "dev", "ttveth-a", "root")
        ns_ready[0] = True
        return True

    if sec("netem") and ensure_ns() and not netem_ok[0]:
        unresolved("netem impairment presets (delay/loss over veth namespaces)", f"tc cannot configure netem in this environment: {netem_why[0]}")
    if sec("netem") and ns_ready[0] and netem_ok[0]:
        if True:
            http(RSP, "POST", "/api/responder/start", {"transport": "udp", "net": {"port": 18502}, "frame": "extended"})
            netprof = lambda **k: prof(transport="udp", frame="extended", net={"peer_host": PIP, "port": 18502}, duration_s=6, interval_ms=25, timeout_ms=800, impair_iface=TIF, **k)
            r0 = run(TST, netprof())
            check("netem baseline over veth (no impairment): PASS", r0["verdict"] == "PASS", f"p99={r0['summary']['rtt_ms']['p99']}ms lost={r0['summary']['lost']}")
            r1 = run(TST, netprof(impairment="Good cell"))
            s1 = r1["summary"]
            qd = sh(*TNS, "tc", "qdisc", "show", "dev", TIF).stdout
            check("netem 'Good cell' (≈25 ms) adds delay but still PASS", r1["verdict"] == "PASS" and 20 <= s1["rtt_ms"]["p50"] <= 45, f"p50={s1['rtt_ms']['p50']}ms p99={s1['rtt_ms']['p99']}ms impairment={s1.get('impairment')}")
            r2 = run(TST, netprof(impairment="Saturated"))
            s2 = r2["summary"]
            check("netem 'Saturated' (≈120 ms) → late messages counted, verdict flips to FAIL", r2["verdict"] == "FAIL" and s2["over_threshold"] > 0.5 * s2["sent"],
                  f"late={s2['over_threshold']}/{s2['sent']} p50={s2['rtt_ms']['p50']}ms verdict={r2['verdict']} reasons={s2['verdict']['reasons'][:1]}")
            qd2 = sh(*TNS, "tc", "qdisc", "show", "dev", TIF).stdout
            check("netem qdisc removed after each run (no leftovers)", "netem" not in qd and "netem" not in qd2, f"tc qdisc after runs: {qd2.strip()}")
            r3 = run(TST, netprof(impairment="Bad cell"))
            s3 = r3["summary"]
            check("netem 'Bad cell' (60±25 ms, 1 % loss): loss and over-limit counted", s3["over_threshold"] > 0 and r3["verdict"] == "FAIL", f"late={s3['over_threshold']} lost={s3['lost']}/{s3['sent']} p99={s3['rtt_ms']['p99']}ms")
            # latency under load
            r4 = run(TST, netprof(load_mbps=8))
            check("Latency-under-load: background UDP load generated during the run", r4["summary"].get("load_mbps") == 8 and r4["status"] == "finished", f"load_mbps={r4['summary'].get('load_mbps')} p99={r4['summary']['rtt_ms']['p99']}ms (unloaded p99={r0['summary']['rtt_ms']['p99']}ms)")
            # non-root instance: impairment must be refused gracefully
            rr = http(A, "POST", "/api/runs", {"profile": netprof(impairment="Bad cell")})
            check("Impairment without permission is refused with a plain reason (non-root instance)", rr.get("_http") == 422 and "permission" in rr.get("detail", ""), json.dumps(rr))
            st = http(A, "GET", "/api/status")
            check("Status reports netem availability", st["netem"]["ok"] is False and "CAP_NET_ADMIN" in st["netem"]["reason"], json.dumps(st["netem"]))

    # ---------------------------------------------------------------- GOOSE (L2 + L3 multicast) over veth namespaces
    if sec("goose") and ensure_ns():
        import statistics
        sniff = lambda iface, secs: subprocess.Popen(["sudo", "-n", *TNS, PY, os.path.join(ROOT, "scripts", "goose_sniff.py"), iface, str(secs)], stdout=subprocess.PIPE, text=True)
        for mode, tr in (("L2", "goose_l2"), ("L3 multicast", "goose_l3")):
            http(RSP, "POST", "/api/responder/stop")
            bcfg = {"transport": tr, "goose": {"interface": PIF}, "frame": "extended"}
            rb = http(RSP, "POST", "/api/responder/start", bcfg)
            check(f"GOOSE {mode}: answering end (own netns) starts", rb.get("running"), json.dumps(rb)[:160])
            gp = lambda **k: prof(**{"transport": tr, "frame": "extended", "goose": {"interface": TIF}, "duration_s": 8, "interval_ms": 200, "timeout_ms": 800, **k})
            pr = http(TST, "POST", "/api/probe", gp())
            check(f"GOOSE {mode}: pairing probe finds the answering end", pr.get("ok"), json.dumps(pr)[:200])
            sn = sniff(TIF, 12)
            r = run(TST, gp(idle_gaps_s=[2], idle_repeats=1))
            s = r["summary"]; g = s["extra"]["goose"]
            check(f"GOOSE {mode}: event round trip PASS (60 ms limit), none lost", r["verdict"] == "PASS" and s["lost"] == 0 and 35 <= s["sent"] <= 41,
                  f"verdict={r['verdict']} sent={s['sent']} lost={s['lost']} p50={s['rtt_ms']['p50']}ms p99={s['rtt_ms']['p99']}ms idle={s['idle']['gaps']}")
            check(f"GOOSE {mode}: retransmissions received, no sqNum gaps, no TAL expiry, no foreign frames",
                  g["events_rx"] >= s["sent"] and g["retx_rx"] > 10 and g["sq_gaps"] == 0 and g["tal_expiries"] == 0 and g["foreign_frames"] == 0 and g["duplicates"] == 0,
                  f"goose={json.dumps(g)[:380]}")
            check(f"GOOSE {mode}: frame small enough for GRE (<300 B)", 0 < g["frame_size"] < 300, f"frame_size={g['frame_size']} B")
            if mode == "L2":
                check("GOOSE L2: VLAN tag + priority 4 survive the path (seen by the sender; the answering Pi reports the same)", g["vlan_seen"] is True and g["prio_seen"] == 4 and (g["b_saw"] or {}).get("vlan") and (g["b_saw"] or {}).get("prio") == 4,
                      f"sender saw vlan={g['vlan_seen']} prio={g['prio_seen']}; answering Pi saw {g['b_saw']}")
            else:
                check("GOOSE L3: events delivered over joined multicast groups (sender 239.192.0.1, mirror 239.192.0.2)", g["events_rx"] > 0 and g["foreign_frames"] == 0, f"mode={g['mode']} iface={g['interface']} events_rx={g['events_rx']}")
            frames = [f for f in json.loads(sn.communicate()[0] or "[]") if not f["out"] or True]
            a_app = min({f["appid"] for f in frames}) if frames else None
            ev = {}
            for f in frames:
                if f["appid"] == 1 and f["gocb"].startswith("TESTER_A"):
                    ev.setdefault(f["st"], []).append(f)
            gaps_all = []
            for st_, fl in ev.items():
                fl.sort(key=lambda x: x["sq"])
                if len(fl) >= 5 and st_ < 0xFFFF0000:
                    gaps_all.append([round((b["t"] - a["t"]) * 1000, 1) for a, b in zip(fl, fl[1:6])])
            ok = bool(gaps_all)
            med = [statistics.median(c) for c in zip(*gaps_all)] if gaps_all else []
            ok = ok and all(2.5 <= med[0] <= 9 and 1.5 <= med[i + 1] / med[i] <= 2.6 for i in range(len(med) - 1))
            hb = [b["t"] - a["t"] for a, b in zip(sorted((f for f in frames if f["appid"] == 1 and f["gocb"].startswith("TESTER_A")), key=lambda x: x["t"]), sorted((f for f in frames if f["appid"] == 1 and f["gocb"].startswith("TESTER_A")), key=lambda x: x["t"])[1:])]
            check(f"GOOSE {mode}: retransmission profile T1=4 ms doubling (captured on the wire)", ok, f"median inter-frame gaps after an event (ms): {med} from {len(gaps_all)} events; max gap overall {max(hb)*1000 if hb else None:.0f} ms (T0 cap 1000 ms)")
            # netem delay -> FAIL
            if netem_ok[0]:
                r2 = run(TST, gp(impairment="Saturated", impair_iface=TIF))
                s2 = r2["summary"]
                check(f"GOOSE {mode}: netem delay → late events counted, verdict FAIL", r2["verdict"] == "FAIL" and s2["over_threshold"] > 0.5 * s2["sent"], f"late={s2['over_threshold']}/{s2['sent']} p50={s2['rtt_ms']['p50']}ms")
            else:
                unresolved(f"GOOSE {mode}: netem delay → verdict FAIL", f"tc cannot configure netem here: {netem_why[0]}")
            # TAL expiry: kill the responder mid-run
            rr = http(TST, "POST", "/api/runs", {"profile": gp(duration_s=10, timeout_ms=500)})
            time.sleep(4); http(RSP, "POST", "/api/responder/stop")
            r3 = wait_run(TST, rr["run_id"]); s3 = r3["summary"]; g3 = (s3.get("extra") or {}).get("goose", {})
            check(f"GOOSE {mode}: publisher silence → TAL expiry detected, events lost", g3.get("tal_expiries", 0) >= 1 and s3["lost"] > 10, f"tal_expiries={g3.get('tal_expiries')} lost={s3['lost']}/{s3['sent']}")
        http(RSP, "POST", "/api/responder/stop")
        import shutil as _sh
        if not _sh.which("tshark"):
            unresolved("GOOSE / R-GOOSE frames dissect in Wireshark (tshark)", "tshark is not installed on this PC - this check runs in scripts/e2e-sniff.sh (Debian container with tshark 4.0.17): L2 GOOSE and R-GOOSE both dissect with no errors")

    # ---------------------------------------------------------------- traffic generator (veth namespaces + netem)
    if sec("gen") and ensure_ns():
        import re as _re, zlib, base64
        NSB_URL = PEER_API
        tcq = lambda ns, dev, *a: subprocess.run(["sudo", "-n"] + (["ip", "netns", "exec", ns] if ns else []) + ["tc", "qdisc", a[0], "dev", dev] + list(a[1:]), capture_output=True, text=True)
        def impair(a_args=None, b_args=None):
            for ns, dev, args in (("tt-b" if REV else None, TIF, a_args), (None if REV else "tt-b", PIF, b_args)):
                tcq(ns, dev, "del", "root")
                if args:
                    r = subprocess.run(["sudo", "-n"] + (["ip", "netns", "exec", ns] if ns else []) + ["tc", "qdisc", "replace", "dev", dev, "root", "netem"] + args.split(), capture_output=True, text=True)
                    if r.returncode:
                        raise RuntimeError("tc failed: " + r.stderr)
        def S(name="S", type_="udp", direction="a_to_b", **k):
            d = {"name": name, "type": type_, "direction": direction, "rate": {"mbps": 20}, "size": {"mode": "fixed", "size": 1000}, "rtt_probe_pps": 20}
            d.update(k)
            return d
        def gprof(streams, secs=8, rule=None, **gk):
            return {"name": "e2e generator", "kind": "generator", "net": {"peer_host": PIP},
                    "gen": {"streams": streams, "duration_s": secs, "interface": TIF, "peer_interface": PIF, "peer_api": NSB_URL, "local_host": TIP,
                            "rule": {"max_loss_pct": 0.5, **(rule or {})}, **gk}}
        def grun(p):
            rr = http(TST, "POST", "/api/runs", {"profile": p})
            if "_http" in rr:
                raise RuntimeError(f"start failed: {rr}")
            out = wait_run(TST, rr["run_id"], 180)
            if out["status"] == "failed":
                raise RuntimeError(f"generator run failed: {(out.get('summary') or {}).get('error')}")
            return out
        st1 = lambda r, i=0: r["summary"]["streams"][i]
        impair()
        # 1. baseline
        r = grun(gprof([S(rate={"mbps": 20})]))
        s = st1(r)
        check("GEN UDP baseline over veth: 20 Mb/s sent = received, no loss, PASS", r["verdict"] == "PASS" and s["loss_pct"] == 0 and abs(s["tx"]["mbps"] - 20) < 1 and abs(s["rx"]["mbps"] - 20) < 1,
              f"tx={s['tx']['mbps']} Mb/s rx={s['rx']['mbps']} Mb/s pkts={s['tx']['pkts']} lost={s['lost']} verdict={r['verdict']}")
        check("GEN one-way delay + jitter + round trip reported when clocks are synced", s["lat_ms"] and s["lat_ms"]["p99"] < 5 and s["jitter_ms"] is not None and s["rtt_ms"] and s["rtt_ms"]["n"] > 50,
              f"lat_ms={s['lat_ms']} jitter_ms={s['jitter_ms']} rtt_ms={s['rtt_ms']} clocks={r['summary']['clocks']}")
        ints = http(TST, "GET", f"/api/runs/{r['id']}/gen-intervals")
        check("GEN per-second rows stored (sessions) and exportable as CSV", len(ints) >= 8 and len(http(TST, "GET", f"/api/runs/{r['id']}/export.csv?kind=gen", raw=True).decode().splitlines()) == len(ints) + 1, f"{len(ints)} per-second rows")
        # 2. injected delay 40 ms on A's egress (A->B only)
        impair(a_args="delay 40ms")
        r = grun(gprof([S(rate={"mbps": 10})], rule={"max_latency_ms": 20}))
        s = st1(r)
        check("GEN measured one-way delay matches netem 40 ms (from → to): p50 within ±3 ms; round trip ≈ 40 ms (reply path undelayed)", abs(s["lat_ms"]["p50"] - 40) <= 3 and abs(s["rtt_ms"]["avg"] - 40) <= 4,
              f"one-way p50={s['lat_ms']['p50']} ms p99={s['lat_ms']['p99']} ms rtt_avg={s['rtt_ms']['avg']} ms (injected 40 ms)")
        check("GEN delay limit 20 ms → verdict FAIL with plain reason; limit 60 ms → PASS", r["verdict"] == "FAIL" and "limit is 20" in " ".join(r["summary"]["verdict"]["reasons"]) and grun(gprof([S(rate={"mbps": 10})], rule={"max_latency_ms": 60}))["verdict"] == "PASS",
              f"reasons={r['summary']['verdict']['reasons']}")
        # 3. loss 5 %
        impair(a_args="loss 5%")
        r = grun(gprof([S(rate={"mbps": 20})], rule={"max_loss_pct": 1}))
        s = st1(r)
        check("GEN measured loss matches netem 5 % (±1.5) and flips verdict (limit 1 % FAIL, limit 10 % PASS)", abs(s["loss_pct"] - 5) <= 1.5 and r["verdict"] == "FAIL" and grun(gprof([S(rate={"mbps": 20})], rule={"max_loss_pct": 10}))["verdict"] == "PASS",
              f"loss={s['loss_pct']} % ({s['lost']}/{s['tx']['pkts']}) injected 5 %")
        # 4. jitter: delay 20ms ±10ms
        impair(a_args="delay 20ms 10ms")
        r = grun(gprof([S(rate={"mbps": 10})], rule={"max_jitter_ms": 2}))
        s = st1(r)
        check("GEN jitter: netem 20 ms ±10 ms → jitter 3-12 ms, delay spread visible, reordering counted; limit 2 ms → FAIL", 3 <= s["jitter_ms"] <= 12 and (s["lat_ms"]["p99"] - s["lat_ms"]["p50"]) > 5 and r["verdict"] == "FAIL",
              f"jitter={s['jitter_ms']} ms lat p50={s['lat_ms']['p50']} p99={s['lat_ms']['p99']} ooo={s['rx']['ooo']} reasons={r['summary']['verdict']['reasons'][:1]}")
        # 5. throughput cap 10 mbit
        impair(a_args="rate 10mbit")
        r = grun(gprof([S(rate={"mbps": 30})], rule={"min_throughput_mbps": 25, "max_loss_pct": None}))
        s = st1(r)
        check("GEN link capped at 10 Mb/s: received ≈ 10 (8-11), loss ≈ 2/3, min-throughput 25 → FAIL", 8 <= s["rx"]["mbps"] <= 11 and 55 <= s["loss_pct"] <= 75 and r["verdict"] == "FAIL",
              f"sent {s['tx']['mbps']} Mb/s, received {s['rx']['mbps']} Mb/s, loss {s['loss_pct']} %")
        # 6. bidirectional, asymmetric delay, several streams at once
        impair(a_args="delay 40ms", b_args="delay 15ms")
        r = grun(gprof([S("from to", rate={"mbps": 5}), S("to from", direction="b_to_a", rate={"mbps": 5}), S("small", rate={"pps": 200}, size={"mode": "fixed", "size": 100})], rule={"max_loss_pct": 0.5}))
        ab, ba, sm = st1(r, 0), st1(r, 1), st1(r, 2)
        check("GEN bidirectional + 3 streams at once: from→to p50 ≈ 40 ms, to→from p50 ≈ 15 ms (asymmetric netem), no loss",
              abs(ab["lat_ms"]["p50"] - 40) <= 3 and abs(ba["lat_ms"]["p50"] - 15) <= 3 and abs(sm["lat_ms"]["p50"] - 40) <= 3 and r["summary"]["totals"]["lost"] == 0,
              f"from→to p50={ab['lat_ms']['p50']} ms, to→from p50={ba['lat_ms']['p50']} ms, small p50={sm['lat_ms']['p50']} ms; rtt A→B probe={ab['rtt_ms']['avg']} ms; lost={r['summary']['totals']['lost']}")
        impair()
        # 7. raw L2, VLAN, raw IP, TCP
        r = grun(gprof([S("L2", "l2", rate={"pps": 2000}, size={"mode": "fixed", "size": 300}, vlan=True, vlan_prio=5, ethertype=0x88B5)], 6))
        s = st1(r)
        check("GEN raw Ethernet (L2, VLAN prio 5, EtherType 0x88B5): sent = received, byte counts equal, PASS", r["verdict"] == "PASS" and s["lost"] == 0 and s["tx"]["bytes"] == s["rx"]["bytes"] and s["rtt_ms"] and s["rtt_ms"]["n"] > 30,
              f"pkts={s['tx']['pkts']} bytes tx/rx={s['tx']['bytes']}/{s['rx']['bytes']} lat p50={s['lat_ms']['p50']} ms rtt n={s['rtt_ms']['n']}")
        r = grun(gprof([S("IP", "ip", rate={"mbps": 20}, size={"mode": "range", "min": 100, "max": 900}, ip_proto=253)], 6))
        s = st1(r)
        check("GEN raw IP (protocol 253, random sizes 100-900 B): no loss, PASS", r["verdict"] == "PASS" and s["lost"] == 0 and abs(s["tx"]["mbps"] - 20) < 1.5, f"tx={s['tx']['mbps']} rx={s['rx']['mbps']} Mb/s lost={s['lost']}")
        r = grun(gprof([S("TCP", "tcp", rate={"mbps": 20}, size={"mode": "fixed", "size": 1200}, rtt_probe_pps=0)], 6))
        s = st1(r)
        check("GEN TCP stream 20 Mb/s: throughput delivered, no loss, PASS", r["verdict"] == "PASS" and s["lost"] == 0 and abs(s["rx"]["mbps"] - 20) < 1.5, f"tx={s['tx']['mbps']} rx={s['rx']['mbps']} Mb/s")
        # 8. IMIX + burst presets over veth
        r = grun(gprof([S("IMIX", rate={"mbps": 30}, size={"mode": "imix"}), S("burst", rate={"mbps": 5}, burst_packets=40, burst_gap_ms=50)], 6))
        check("GEN IMIX + burst-mode streams delivered without loss", r["verdict"] == "PASS" and r["summary"]["totals"]["lost"] == 0, f"totals={r['summary']['totals']}")
        # 9. measured maximum rates (information: the honest ceiling of the Python fast path on this PC)
        mx = {}
        for label, size, target in (("1400 B", 1400, 2500), ("200 B", 200, 800), ("66 B", 66, 300)):
            rate = {"mbps": target} if size == 1400 else {"pps": target * 1000}
            r = grun(gprof([S("max", rate=rate, size={"mode": "fixed", "size": size}, rtt_probe_pps=0)], 8, rule={"max_loss_pct": None}))
            s = st1(r)
            mx[label] = (s["tx"]["pps"], s["tx"]["mbps"], s["rx"]["pps"], s["rx"]["mbps"], s["loss_pct"])
        check("GEN measured maximum rates over veth (from → to, two separate processes): runs complete, numbers recorded", all(v[0] > 1000 for v in mx.values()),
              "; ".join(f"{k}: sent {v[0]:.0f} pps / {v[1]:.0f} Mb/s, received {v[2]:.0f} pps / {v[3]:.0f} Mb/s, loss {v[4]} %" for k, v in mx.items()))
        # 10. errors are plain
        bad = http(TST, "POST", "/api/runs", {"profile": gprof([S()], 3, peer_api=f"http://{PIP}:9")})
        out = wait_run(TST, bad["run_id"], 30)
        check("GEN unreachable other Pi → run fails with a plain message", out["status"] == "failed" and "Cannot reach" in out["summary"]["error"] and PIP in out["summary"]["error"], out["summary"].get("error", ""))
        # 11. PDF report with a generator section
        pdf = http(TST, "GET", f"/api/report.pdf?run_ids={r['id']},{r['id'] - 1}", raw=True)
        txt = b""
        for m in _re.finditer(rb"stream\r?\n(.*?)endstream", pdf, _re.S):
            try:
                txt += zlib.decompress(base64.a85decode(m.group(1).strip(), adobe=True))
            except Exception:
                pass
        check("GEN PDF report contains the generator section (title, stream table, charts)", pdf[:5] == b"%PDF-" and b"Traffic generator" in txt and b"Streams" in txt and b"Throughput per second" in txt, f"pdf {len(pdf)} bytes; decoded text markers found: {[k for k in (b'Traffic generator', b'Streams', b'Throughput per second') if k in txt]}")
        # 12. netem left behind?
        tcq("tt-b" if REV else None, TIF, "del", "root"); tcq(None if REV else "tt-b", PIF, "del", "root")
        left = sh(*TNS, "tc", "qdisc", "show", "dev", TIF).stdout + sh("tc", "qdisc", "show", "dev", "ttveth-a").stdout
        check("GEN test leaves no netem qdisc behind", "netem" not in left, left.strip())
except BaseException as ex:      # never let a crash look like success
    import traceback; traceback.print_exc()
    check("e2e script ran to completion", False, f"{type(ex).__name__}: {ex}")
finally:
    cleanup()
    bad = [n for n, ok in results if not ok]
    print(f"# {len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""), flush=True)
    sys.exit(1 if bad else 0)
