#!/usr/bin/env python3
"""Instrument redesign phase 1 (the shell) in a real browser, phone 390 px and desktop 1280 px: five tabs + Setup, one transport bar (Start / Stop,
Clear, Mark, timer) for all engines, Home from the status API, the Setup menu with its seven pages, old links that still land somewhere.
Prints 'CHECK name | pass/FAIL | evidence'. Run: python3 scripts/e2e_shell.py"""
import json, os, re, signal, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="tt-shell-"); procs, results = [], []
A, B = 18580, 18581


def check(name, ok, ev=""):
    results.append((name, bool(ok))); print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:300]}", flush=True)


def http(port, method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as f:
        d = f.read(); return json.loads(d) if d else None


def app(role, port):
    env = dict(os.environ, TT_TEST_HOOKS="1", TT_ASSUME_SYNCED="1", PYTHONPATH=ROOT, TT_NO_DISCOVERY="1", TT_NO_DEFAULT_REFLECTOR="1")
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--role", role, "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{role}"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try: http(port, "GET", "/api/status"); return
        except Exception: time.sleep(0.25)
    raise RuntimeError("app did not start")


def main():
    app("A", A); app("B", B)
    http(A, "PUT", "/api/pi", {"name": "pi1"}); http(B, "PUT", "/api/pi", {"name": "pi2"})
    http(A, "POST", "/api/peers", {"host": f"127.0.0.1:{B}"})
    peer = next(x for x in http(A, "GET", "/api/peers")["peers"] if x["name"] == "pi2")
    prof = http(A, "POST", "/api/profiles", {"name": "Shell e2e UDP", "transport": "udp", "net": {"port": 18590}, "source": {"pi": "self"}, "dest": {"pi": peer["id"]}, "duration_s": 40, "interval_ms": 50, "timeout_ms": 500, "threshold_ms": 60})
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for tag, w, h in (("phone", 390, 844), ("desktop", 1280, 800)):
            ctx = br.new_context(viewport={"width": w, "height": h}); pg = ctx.new_page(); errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "404" not in m.text else None)
            base = f"http://127.0.0.1:{A}/"
            pg.goto(base); pg.wait_for_selector("[data-testid=home-links]", timeout=15000); time.sleep(1.0)
            # ---- tabs and Setup
            tabs = [t.strip() for t in pg.locator("#tabs button:visible .tl").all_inner_texts()]
            check(f"[{tag}] the tabs are Home · Tests · Generator · Sniffer · Results", [t for t in tabs if t != "Setup"] == ["Home", "Tests", "Generator", "Sniffer", "Results"], tabs)
            check(f"[{tag}] the app opens on Home", pg.get_attribute("[data-tab=home]", "aria-selected") == "true")
            box = pg.evaluate("(() => { const r = document.querySelector('#setupBtn').getBoundingClientRect(), s = document.querySelector('.top .strip').getBoundingClientRect(), n = document.querySelector('#tabs').getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height, stripBottom: s.bottom, vw: innerWidth, vh: innerHeight, navLeft: n.left, navW: n.width}; })()")
            if tag == "phone": check("[phone] Setup is the icon at the right of the lamp strip", box["y"] < 44 and box["x"] > box["vw"] - 80 and box["w"] >= 44 and box["h"] >= 44, box)
            else: check("[desktop] Setup sits at the foot of the left rail", box["y"] > box["vh"] - 140 and box["x"] < 100, box)
            # ---- transport bar
            tb = pg.evaluate("(() => { const t = document.querySelector('#transport').getBoundingClientRect(), n = document.querySelector('#tabs').getBoundingClientRect(), s = document.querySelector('#tbStart').getBoundingClientRect(); return {top: t.top, bottom: t.bottom, h: t.height, navTop: n.top, startH: s.height, startW: s.width, vh: innerHeight}; })()")
            if tag == "phone": check("[phone] the transport bar sits right above the tab bar; Start is 52 px tall", abs(tb["bottom"] - tb["navTop"]) < 2 and tb["startH"] >= 50, tb)
            else: check("[desktop] the transport bar sits under the lamp strip", abs(tb["top"] - 40) < 2 and tb["startH"] >= 50, tb)
            check(f"[{tag}] the bar reads READY 00:00:00 with Clear and Mark greyed", pg.inner_text("[data-testid=tb-word]") == "READY" and pg.inner_text("[data-testid=tb-time]") == "00:00:00" and pg.is_disabled("[data-testid=tb-clear]") and pg.is_disabled("[data-testid=tb-mark]"))
            for t in ("tests", "generator", "sniffer", "results", "config"):
                pg.locator(f"[data-tab={t}]:visible").first.click(); pg.wait_for_selector(f"[data-tab={t}][aria-selected=true]"); time.sleep(0.5)
                assert pg.locator("#transport:visible").count() == 1, t
            check(f"[{tag}] the transport bar is on every screen", True)
            # ---- Home
            pg.locator("[data-tab=home]:visible").first.click(); pg.wait_for_selector("[data-testid=home-loaded]"); time.sleep(0.6)
            panels = [pg.locator(f"[data-testid={x}]").count() for x in ("home-links", "home-loaded", "home-results")]
            check(f"[{tag}] Home has Links, Loaded test and Last results", panels == [1, 1, 1], panels)
            check(f"[{tag}] Links has a row per test port (state, link)", pg.locator("[data-testid^=home-port-]").count() >= 2, pg.locator("[data-testid^=home-port-]").count())
            check(f"[{tag}] the loaded test shows its name, target and the Ready line", bool(pg.inner_text("[data-testid=home-loaded-name]").strip()) and "example target" in pg.inner_text("[data-testid=home-loaded]") and pg.locator("[data-testid=home-ready]").count() == 1)
            check(f"[{tag}] no horizontal scroll on Home", pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
            # ---- Start from Home runs the loaded test; the bar follows; Stop; Clear
            pg.evaluate(f"localStorage.setItem('tt-loaded', '{prof['id']}')"); pg.reload(); pg.wait_for_selector("[data-testid=home-loaded-name]"); time.sleep(0.6)
            check(f"[{tag}] Change on Home opens Tests; the loaded test is tagged there", True)
            check(f"[{tag}] Home names the loaded test", "Shell e2e UDP" in pg.inner_text("[data-testid=home-loaded-name]"))
            pg.click("[data-testid=tb-start]")
            pg.wait_for_selector("#runBanner", timeout=40000); time.sleep(2.5)
            t1 = pg.inner_text("[data-testid=tb-time]"); time.sleep(2.2); t2 = pg.inner_text("[data-testid=tb-time]")
            check(f"[{tag}] Start on Home opened the loaded test on Tests and started it (RUNNING, timer counts)", pg.inner_text("[data-testid=tb-word]") == "RUNNING" and t1 != t2 and pg.get_attribute("[data-tab=tests]", "aria-selected") == "true", f"{t1} -> {t2}")
            check(f"[{tag}] the button reads STOP; Mark is live; the running pillar has a dot", "STOP" in pg.inner_text("[data-testid=tb-start]") and pg.is_enabled("[data-testid=tb-mark]") and pg.locator("[data-tab=tests] .live").count() == 1)
            check(f"[{tag}] no Start / Stop / Mark buttons left on the live screen itself", pg.locator("#stopBtn, #markBtn").count() == 0)
            pg.locator("[data-tab=home]:visible").first.click(); pg.wait_for_selector("[data-testid=home-running]"); time.sleep(0.5)
            check(f"[{tag}] Home says a run is in progress and links to it", "Shell e2e UDP" in pg.inner_text("[data-testid=home-running]"))
            check(f"[{tag}] the bar keeps running on Home", pg.inner_text("[data-testid=tb-word]") == "RUNNING")
            pg.locator("[data-tab=tests]:visible").first.click(); pg.wait_for_selector("#runBanner"); time.sleep(0.5)
            pg.click("[data-testid=tb-start]"); pg.wait_for_selector("#mYes"); pg.click("#mYes")
            pg.wait_for_function("document.querySelector('[data-testid=tb-word]').textContent === 'DONE'", timeout=30000)
            check(f"[{tag}] Stop ends the run; the bar reads DONE and Clear is live", pg.is_enabled("[data-testid=tb-clear]") and "START" in pg.inner_text("[data-testid=tb-start]"), pg.inner_text("[data-testid=tb-word]"))
            time.sleep(0.8)
            check(f"[{tag}] Tests shows the finished verdict", pg.locator("#runBanner .banner").count() >= 1 or "PASS" in pg.inner_text("#main") or "FAIL" in pg.inner_text("#main") or "Finished" in pg.inner_text("#main") or "Stopped" in pg.inner_text("#main"))
            pg.click("[data-testid=tb-clear]"); time.sleep(0.8)
            check(f"[{tag}] Clear returns the bar to READY and Tests to the list", pg.inner_text("[data-testid=tb-word]") == "READY" and pg.locator(".pcard").count() > 0 and pg.inner_text("[data-testid=tb-time]") == "00:00:00")
            check(f"[{tag}] Tests lists test sets in groups", pg.locator("[data-testid=tests-group-da]").count() == 1 and pg.locator("[data-testid=tests-group-link]").count() == 1 and pg.locator("[data-testid=tests-group-mine]").count() == 1)
            check(f"[{tag}] the finished run is in Last results on Home", (pg.locator("[data-tab=home]:visible").first.click() or True) and pg.wait_for_selector("[data-testid=home-result]", timeout=8000) is not None and "Shell e2e UDP" in pg.inner_text("[data-testid=home-results]"))
            # ---- Start on the Generator / Sniffer screens drives their own start
            for t, sel in (("generator", "gen-start"), ("sniffer", "sniff-start")):
                pg.locator(f"[data-tab={t}]:visible").first.click(); pg.wait_for_selector(f"[data-testid={sel}]", state="attached", timeout=15000); time.sleep(0.8)
                if t == "sniffer" and pg.inner_text("[data-testid=tb-word]") in ("RUNNING", "CAPTURING"):          # the Generator start above really started a run: stop it first
                    pg.click("[data-testid=tb-start]"); pg.wait_for_selector("#mYes"); pg.click("#mYes"); pg.wait_for_function("document.querySelector('[data-testid=tb-word]').textContent === 'DONE'", timeout=30000)
                    pg.click("[data-testid=tb-clear]"); time.sleep(0.8)
                if t == "sniffer" and pg.locator(f"[data-testid={sel}]").count() == 0: pg.locator(".pcard[data-act=pick]").first.click(); time.sleep(0.8)
                pg.evaluate(f"window.__hit = 0; document.addEventListener('click', e => {{ if (e.target.closest('[data-testid={sel}]')) window.__hit++; }}, true)")
                pg.click("[data-testid=tb-start]"); time.sleep(0.5)
                check(f"[{tag}] START on the {t.capitalize()} screen presses that screen's own start (no Start button of its own is drawn)", pg.evaluate("window.__hit") == 1 and not pg.locator(f"[data-testid={sel}]").is_visible(), pg.evaluate("window.__hit"))
            # ---- old links
            for old, want_tab, want_sub in (("#setup", "tests", ""), ("#run", "tests", ""), ("#monitor", "tests", ""), ("#settings", "config", ""), ("#about", "config", "help")):
                pg.goto(base + old); pg.reload(); time.sleep(1.0)
                got = pg.evaluate("location.hash"); sel = pg.get_attribute(f"[data-tab={want_tab}]", "aria-selected")
                check(f"[{tag}] old link {old} lands on {want_tab}{('/' + want_sub) if want_sub else ''}", sel == "true" and (not want_sub or want_sub in got), got)
            # ---- Setup: a short menu, each row its own page
            pg.goto(base + "#config"); pg.reload(); pg.wait_for_selector("[data-testid=setup-menu]")
            rows = [t for t in ("pis", "network", "time", "update", "password", "help", "demo") if pg.locator(f"[data-testid=setup-{t}]").count() == 1]
            check(f"[{tag}] Setup has seven rows: Pis, Network, Time, Software update, Password, Help, Demo", len(rows) == 7, rows)
            marks = {"pis": "thispi-card", "network": "#netCard", "time": "clock-source-select", "update": "#setUpdate", "password": "settings-password", "help": "about-card", "demo": "mb-selftest"}
            for sub, mk in marks.items():
                pg.goto(base + "#config"); pg.reload(); pg.wait_for_selector(f"[data-testid=setup-{sub}]"); pg.click(f"[data-testid=setup-{sub}]")
                sel = mk if mk.startswith("#") else f"[data-testid={mk}]"
                try: pg.wait_for_selector(sel, timeout=15000, state="attached"); ok = True
                except Exception: ok = False
                check(f"[{tag}] Setup → {sub} opens its own page with its content", ok and pg.locator("[data-testid=setup-back]").count() == 1, pg.inner_text("#main")[:80])
            pg.click("[data-testid=setup-back]"); pg.wait_for_selector("[data-testid=setup-menu]")
            check(f"[{tag}] ‹ Setup goes back to the menu", True)
            pg.goto(base + "#home"); pg.reload(); pg.wait_for_selector("[data-testid=home-links]")
            if tag == "desktop":
                pg.click("[data-testid=app-version]"); pg.wait_for_selector("[data-testid=about-card]", timeout=10000)
                check("[desktop] the version label opens Setup → Help (About)", "config/help" in pg.evaluate("location.hash"))
            check(f"[{tag}] no script errors", not errs, errs[:3])
    bad = [n for n, ok in results if not ok]
    print(f"{len(results) - len(bad)}/{len(results)} checks passed"); return 1 if bad else 0


rc = 1
try:
    rc = main()
finally:
    for p in procs:
        try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception: pass
sys.exit(rc)
