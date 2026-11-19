#!/usr/bin/env python3
"""Tests page regressions (1.12.1), real Chromium, two Pis on this PC, phone (390x844) and desktop (1440x900).
 1. "Sometimes clicking a test does nothing until I reload": a test page left open by another tab (Home, Results, the Tests tab itself) left its draft behind and the list ignored every tap.
 2. "On a test's page I see port / protocol / from / to but no Run button": the page's own Start was hidden (only the small START in the transport bar was left).
Prints 'CHECK name | pass/FAIL | evidence'. Run via scripts/e2e-testspage.sh. Screenshots: docs/screenshots/testspage-*.png"""
import json, os, subprocess, sys, tempfile, time, traceback, urllib.request, urllib.error
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "docs", "screenshots")
TMP = tempfile.mkdtemp(prefix="tt-testspage-"); procs, results = [], []
A, B = 18450, 18451
SIZES = [("desktop", 1440, 900, False), ("phone", 390, 844, True)]


def check(name, ok, ev=""):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:500]}", flush=True)


def http(port, method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as f:
            d = f.read(); return json.loads(d) if d else None
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "detail": e.read().decode()[:200]}


def start(role, port):
    try:
        http(port, "GET", "/api/status"); raise RuntimeError(f"port {port} already in use (leftover app from an earlier run?)")
    except urllib.error.URLError:
        pass
    env = dict(os.environ, PYTHONPATH=ROOT, TT_ASSUME_SYNCED="1", TT_TEST_HOOKS="1", TT_NO_DISCOVERY="1")
    if role == "A": env["TT_NO_DEFAULT_REFLECTOR"] = "1"          # two apps in one network namespace: only pi2 answers on UDP/TCP 20500
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{role}"],
                         env=env, stdout=open(f"{TMP}/{role}.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try: http(port, "GET", "/api/status"); return
        except Exception: time.sleep(0.25)
    raise RuntimeError(f"{role} did not start")


def shot(pg, name):
    os.makedirs(SHOTS, exist_ok=True); pg.screenshot(path=os.path.join(SHOTS, f"testspage-{name}.png"))


def card(pg, name): return pg.locator(f".pcard:has(button.pname:text-is('{name}'))")
def ts_card(pg, name): return pg.locator(f".pcard:has(.pname.ro:text-is('{name}'))")


def in_view(pg, sel):
    """The element is on screen right now (no scrolling), not covered by another element at its centre."""
    return pg.evaluate("""s => { const e = document.querySelector(s); if (!e) return 'missing'; const r = e.getBoundingClientRect(); if (r.width < 40 || r.height < 20) return 'size ' + r.width + 'x' + r.height;
      if (r.top < 0 || r.bottom > innerHeight || r.left < 0 || r.right > innerWidth) return 'off screen ' + JSON.stringify([r.top, r.bottom, innerHeight]);
      const t = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return t && (t === e || e.contains(t)) ? 'ok' : 'covered by ' + (t && (t.id || t.className || t.tagName)); }""", sel)


def goto_tests(pg, port=A):
    pg.goto(f"http://127.0.0.1:{port}/#tests"); pg.wait_for_selector(".pcard")


def run_all(br):
    for o, W, H, touch in SIZES:
        tag = f"[{o}] "
        ctx = br.new_context(viewport={"width": W, "height": H}, is_mobile=touch, has_touch=touch); pg = ctx.new_page(); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        click = (lambda sel: pg.tap(sel)) if touch else (lambda sel: pg.click(sel))
        tab = lambda t: pg.locator(f"#tabs [data-tab={t}]:visible").first.click()
        goto_tests(pg)

        # ---- 1. a test page left open must not make the list dead
        card(pg, "Quick link check").locator("[data-act=pick]").click(); pg.wait_for_selector("[data-testid=nt-page]")
        tab("home"); pg.wait_for_selector("[data-testid=home-title], #main h1, #main h2", timeout=5000); time.sleep(0.4)
        tab("tests"); pg.wait_for_selector(".pcard")
        card(pg, "TCP session test").locator("[data-act=pick]").click(); time.sleep(1.0)
        check(f"{tag}1a: Run on a test, go Home, come back to Tests, Run on another test -> its page opens (no reload)", pg.locator("[data-testid=nt-page] h1").count() == 1 and "TCP session test" in pg.inner_text("[data-testid=nt-page] h1"), pg.inner_text("#main")[:60].replace("\n", " "))
        tab("tests"); time.sleep(0.8)
        check(f"{tag}1b: tapping the Tests tab while a test page is open gives the list", pg.locator(".pcard").count() >= 9 and pg.locator("[data-testid=nt-page]").count() == 0)
        card(pg, "Quick link check").locator("[data-act=pick]").click(); time.sleep(1.0)
        check(f"{tag}1c: ...and Run works straight away after that", pg.locator("[data-testid=nt-page]").count() == 1)
        tab("results"); time.sleep(0.5); tab("tests"); pg.wait_for_selector(".pcard")
        card(pg, "Quick link check").locator(".ptop").click(); time.sleep(0.8)
        check(f"{tag}1d: after Results and back, tapping a test card opens its info sheet", pg.locator("#modal:not([hidden])").count() == 1 and "What it tests" in pg.inner_text("#modal"))
        pg.keyboard.press("Escape")
        goto_tests(pg)

        # ---- 2. the page has its own, visible Run test button (same action as START in the bar)
        pid2 = http(A, "POST", "/api/peers", {"host": f"127.0.0.1:{B}"})["id"]
        goto_tests(pg)
        card(pg, "Quick link check").locator("[data-act=pick]").click(); pg.wait_for_selector("[data-testid=nt-page]")
        pg.wait_for_function("() => { const b = document.querySelector('[data-testid=nt-start]'); return b && !b.disabled; }", timeout=20000)
        txt = pg.inner_text("[data-testid=nt-start]").strip()
        check(f"{tag}2a: the test page shows a primary 'Run test' button", "Run test" in txt and "primary" in (pg.get_attribute("[data-testid=nt-start]", "class") or ""), txt)
        v = in_view(pg, "[data-testid=nt-start]")
        check(f"{tag}2b: it is on screen without scrolling, not covered (with the Traffic row open too)", v == "ok", v)
        pg.click("[data-testid=nt-head-ends]") if pg.locator("[data-testid=nt-row-ends]:not(.open)").count() else None
        pg.click("[data-testid=nt-head-traffic]"); time.sleep(0.4)
        v2 = in_view(pg, "[data-testid=nt-start]")
        check(f"{tag}2c: still on screen with all rows open", v2 == "ok", v2)
        shot(pg, f"{o}-run-button")
        before = http(A, "GET", "/api/status")["run"]["running"]
        click("[data-testid=nt-start]"); time.sleep(2.5)
        st = http(A, "GET", "/api/status")["run"]
        check(f"{tag}2d: pressing it starts the run (same path as START in the bar: RUNNING, run is live on the Pi)", not before and st["running"] and "RUNNING" in pg.inner_text("[data-testid=tb-word]"), f"running={st['running']} word={pg.inner_text('[data-testid=tb-word]')!r}")
        http(A, "POST", f"/api/runs/{st['run_id']}/stop"); time.sleep(3)
        # a finished run, then the list must still react
        tab("tests"); time.sleep(0.8); pg.wait_for_selector(".pcard", timeout=8000)
        card(pg, "TCP session test").locator("[data-act=pick]").click(); time.sleep(1.2)
        check(f"{tag}1e: after a run has been stopped, Tests -> Run still opens a test page", pg.locator("[data-testid=nt-page]").count() == 1)
        tab("tests"); time.sleep(0.6)

        # ---- 3. a test set's page has it too
        ts_card(pg, "FAN mix · SCADA + NMS + camera").locator("[data-act=pick]").click(); pg.wait_for_selector("[data-testid=ts-page]"); time.sleep(1.5)
        tt = pg.inner_text("[data-testid=ts-start]").strip(); vt = in_view(pg, "[data-testid=ts-start]")
        check(f"{tag}3a: a test-set page shows 'Run test' too, on screen without scrolling", "Run test" in tt and vt == "ok", f"{tt!r} {vt}")
        shot(pg, f"{o}-testset-run-button")
        # ---- 4. the generator keeps its single START (the bar)
        tab("generator"); time.sleep(1.0)
        check(f"{tag}4: Generator / Sniffer keep the bar's START only (their in-page start stays hidden)", pg.locator("[data-testid=gen-start]:visible").count() == 0)
        check(f"{tag}no console / page errors", not errs, errs[:3])
        ctx.close()
        http(A, "DELETE", f"/api/peers/{pid2}")


def main():
    start("B", B); start("A", A)
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            try: run_all(br)
            finally: br.close()
    except Exception:
        traceback.print_exc(); check("script ran to the end", False, traceback.format_exc()[-300:])
    finally:
        for p in procs:
            try: os.killpg(p.pid, 15)
            except Exception: pass
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed"); sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
