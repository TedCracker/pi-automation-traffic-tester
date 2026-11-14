#!/usr/bin/env python3
"""Web UI + API sweep of two Pis (pi1 and pi2: identical apps, no roles): every read endpoint answers, every tab renders in a real browser without console errors,
sessions/settings/password/profile actions work, data survives an app restart. Run via scripts/e2e-ui-sweep.sh (system python3 with playwright).
Prints 'CHECK name | pass/FAIL | evidence'."""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time, traceback, urllib.request, urllib.error
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="tt-sweep-"); procs, results, errors = [], [], []
PORTS = {"A": 18320, "B": 18321}


def check(name, ok, ev=""):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:380]}", flush=True)


def http(port, method, path, body=None, cookie=None, raw=False):
    h = {"Content-Type": "application/json"}
    if cookie: h["Cookie"] = cookie
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as f:
            d = f.read(); return f.status, (d if raw else (json.loads(d) if d else None))
    except urllib.error.HTTPError as e:
        d = e.read()
        try: return e.code, json.loads(d)
        except Exception: return e.code, d


def start(role):
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--port", str(PORTS[role]), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{role}"],
                         env=dict(os.environ, PYTHONPATH=ROOT, TT_ASSUME_SYNCED="1", TT_TEST_HOOKS="1", TT_NO_DEFAULT_REFLECTOR="1", TT_NO_DISCOVERY="1"), stdout=open(f"{TMP}/{role}.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try: http(PORTS[role], "GET", "/api/status"); return p
        except Exception: time.sleep(0.25)
    raise RuntimeError(f"{role} did not start")


def ck2(P):
    req = urllib.request.Request(f"http://127.0.0.1:{P}/api/auth/login", method="POST", data=json.dumps({"password": "sweep-pw-1"}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as f: return f.headers.get("Set-Cookie", "").split(";")[0]


def stop(p):
    os.killpg(p.pid, signal.SIGTERM); p.wait(10)


try:
    ap = {r: start(r) for r in ("A", "B")}
    for r, nm in (("A", "pi1"), ("B", "pi2")):
        http(PORTS[r], "PUT", "/api/pi", {"name": nm})
    GETS = ["/api/status", "/api/time", "/api/ports", "/api/profiles", "/api/sessions", "/api/runs", "/api/usb", "/api/net/check", "/api/netem-presets", "/api/sniff/filters",
            "/api/gen/node/status", "/api/wifi/builtin/status", "/api/claude/status", "/api/profiles/export"]
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for role in ("A", "B"):
            P = PORTS[role]; tag = f"[{ {'A': 'pi1', 'B': 'pi2'}[role] }] "
            st, status = http(P, "GET", "/api/status")
            pname = {"A": "pi1", "B": "pi2"}[role]
            check(f"{tag}status: no role, the Pi's name, version, hostname reported", st == 200 and "role" not in status and status["pi"]["name"] == pname and status["version"] and status.get("hostname"), json.dumps({k: status.get(k) for k in ("pi", "version", "hostname")}))
            bad = []
            for g in GETS:
                code, _ = http(P, "GET", g, raw=True)
                if code != 200: bad.append((g, code))
            pdf422 = http(P, "GET", "/api/report.pdf", raw=True)[0]
            check(f"{tag}every read endpoint answers 200 ({len(GETS)} checked); the PDF endpoint without run ids says 422, not a crash", not bad and pdf422 == 422, f"{bad} pdf={pdf422}")
            code, html = http(P, "GET", "/", raw=True)
            assets = [a for a in ("app.js", "ui.js", "state.js", "endpoints.js", "setup.js", "run.js", "monitor.js", "results.js", "generator.js", "sniffer.js", "settings.js", "claude.js", "style.css") if http(P, "GET", f"/{a}", raw=True)[0] != 200] + [a for a in ("vendor/uPlot.min.js", "vendor/uPlot.min.css") if http(P, "GET", f"/{a}", raw=True)[0] != 200]
            check(f"{tag}web page and all static files are served (title, no missing asset)", code == 200 and b"Pi Automation Traffic Tester" in html and not assets, f"missing={assets}")
            ctx = br.new_context(viewport={"width": 1280, "height": 860}); pg = ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e, errs=errs: errs.append(str(e))); pg.on("console", lambda m, errs=errs: errs.append(m.text) if m.type == "error" else None)
            pg.goto(f"http://127.0.0.1:{P}/"); pg.wait_for_selector("[data-tab=setup]")
            seen = {}
            for tab in ("setup", "run", "results", "generator", "sniffer", "settings"):
                pg.click(f"[data-tab={tab}]"); pg.wait_for_selector(f"[data-tab={tab}][aria-selected=true]"); time.sleep(0.6)
                seen[tab] = len(pg.inner_text("#main").strip())
            check(f"{tag}all 6 screens open and render content in a real browser, zero console/page errors", all(v > 30 for v in seen.values()) and not errs, f"text sizes={seen} errors={errs[:3]}")
            pg.click("[data-tab=setup]"); time.sleep(0.5)
            top = pg.inner_text("header.top")
            check(f"{tag}top bar: only the Pi's name (no role label), window title says the name; Setup has no Responder card", pname in pg.inner_text("[data-testid=top-pi-name]") and pg.title() == f"{pname} · Pi Automation Traffic Tester" and not re.search(r"Pi [AB]\b|Responder|(?<!Traffic )Tester", top) and pg.locator("#respCard").count() == 0, f"title={pg.title()!r} top={top[:90]!r}")
            # sniffer tab: presets incl. the built-in Wi-Fi (Nexmon) one, warning + acknowledgement + restore button
            pg.click("[data-tab=sniffer]"); pg.wait_for_selector(".pcard"); time.sleep(0.5)
            n = pg.locator(".pcard").count()
            pg.click(".pcard:has-text('built-in Wi-Fi') >> nth=0"); time.sleep(0.8)
            txt = pg.inner_text("#main")
            check(f"{tag}Sniffer tab: {n} presets; choosing the built-in Wi-Fi preset shows the Nexmon panel with the warning, the acknowledgement box and 'Restore normal Wi-Fi'",
                  n >= 6 and "Experimental" in txt and "Wi-Fi (and internet over Wi-Fi) goes off" in txt and pg.locator("input[data-p='wifi.builtin_ack']").count() == 1 and "Restore normal Wi-Fi" in txt, f"{n} presets")
            pg.click(".pcard:has-text('Inline Ethernet tap') >> nth=0"); time.sleep(0.4)
            check(f"{tag}Ethernet inline tap and SPAN stay selectable next to Wi-Fi (capture sources: inline, SPAN, serial, Wi-Fi)", all(pg.locator(f".pcard:has-text('{t}')").count() >= 1 for t in ("Inline Ethernet tap", "SPAN port", "RS-485", "RS-232", "Wi-Fi")), "")
            pg.click("[data-tab=generator]"); time.sleep(0.5)
            check(f"{tag}Generator tab shows its preset cards", pg.locator(".pcard").count() >= 3, f"{pg.locator('.pcard').count()} cards")
            ctx.close()
            # sessions, profile and settings actions through the API
            sc, ses = http(P, "POST", "/api/sessions", {"name": f"sweep {role}"})
            sid = ses["id"]
            http(P, "PUT", f"/api/sessions/{sid}", {"name": f"sweep {role} renamed"})
            names = [s["name"] for s in http(P, "GET", "/api/sessions")[1]]
            http(P, "DELETE", f"/api/sessions/{sid}")
            gone = f"sweep {role} renamed" not in [s["name"] for s in http(P, "GET", "/api/sessions")[1]]
            check(f"{tag}sessions: create, rename, delete", f"sweep {role} renamed" in names and gone, names[:4])
            profs = http(P, "GET", "/api/profiles")[1]
            mine = http(P, "POST", f"/api/profiles/{profs[0]['id']}/duplicate")[1]
            http(P, "PUT", f"/api/profiles/{mine['id']}", {**mine, "name": f"keep me {role}"})
            # password: set, then API needs it, login works, then remove it
            c1, _ = http(P, "POST", "/api/auth/set", {"password": "sweep-pw-1"})
            c2, _ = http(P, "GET", "/api/profiles")
            c3, lg = http(P, "POST", "/api/auth/login", {"password": "sweep-pw-1"})
            check(f"{tag}web password: set -> API refuses without it (401) -> login works", c1 == 200 and c2 == 401 and c3 == 200, f"set={c1} anon={c2} login={c3}")
            ck = None
            req = urllib.request.Request(f"http://127.0.0.1:{P}/api/auth/login", method="POST", data=json.dumps({"password": "sweep-pw-1"}).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as f: ck = f.headers.get("Set-Cookie", "").split(";")[0]
            c5, _ = http(P, "POST", "/api/auth/logout", cookie=ck)
            c6, _ = http(P, "GET", "/api/profiles", cookie=ck)
            check(f"{tag}sign out: the old cookie stops working (401) while the password stays set", c5 == 200 and c6 == 401, f"logout={c5} after={c6}")
            c4, _ = http(P, "POST", "/api/auth/set", {"password": ""}, cookie=ck2(P))
            check(f"{tag}web password removed again; API open", c4 == 200 and http(P, "GET", "/api/profiles")[0] == 200, f"remove={c4}")
        # restart both: saved profiles and settings survive
        for role in ("A", "B"):
            stop(ap[role]); ap[role] = start(role)
            names = [p["name"] for p in http(PORTS[role], "GET", "/api/profiles")[1]]
            check(f"[{ {'A': 'pi1', 'B': 'pi2'}[role] }] data survives an app restart (saved profile still there)", f"keep me {role}" in names, names[-3:])
        br.close()
except BaseException as ex:
    traceback.print_exc(); check("sweep ran to completion", False, repr(ex))
finally:
    for p in procs:
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: pass
    shutil.rmtree(TMP, ignore_errors=True)
    bad = [n for n, ok in results if not ok]
    print(f"# {len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""), flush=True)
    sys.exit(1 if bad else 0)
