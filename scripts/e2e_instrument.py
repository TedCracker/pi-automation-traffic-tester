#!/usr/bin/env python3
"""Instrument skin (redesign phase 0) in a real browser, phone 390 and desktop 1280: lamp strip (six lamps, tap -> note + fix), no About / AI guide
links in the header, the AI note is in the page but not drawn, one-line test cards, state bar with a fixed-width timer while a serial test runs and a
solid verdict block after it, numbers in ink (no green numbers), one-line update banner. Prints 'CHECK name | pass/FAIL | evidence'.
Run: python3 scripts/e2e_instrument.py"""
import json, os, re, signal, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="tt-instr-"); procs, results = [], []
A, B = 18480, 18481


def check(name, ok, ev=""):
    results.append((name, bool(ok))); print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:300]}", flush=True)


def http(port, method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as f:
        d = f.read(); return json.loads(d) if d else None


def app(role, port):
    env = dict(os.environ, TT_TEST_HOOKS="1", TT_ASSUME_SYNCED="1", PYTHONPATH=ROOT, TT_NO_DISCOVERY="1")
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--role", role, "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{role}"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try: http(port, "GET", "/api/status"); return
        except Exception: time.sleep(0.25)
    raise RuntimeError("app did not start")


def main():
    sc = subprocess.Popen(["socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0"], stderr=subprocess.PIPE, text=True, start_new_session=True); procs.append(sc)
    ptys = []
    while len(ptys) < 2:
        m = re.search(r"PTY is (\S+)", sc.stderr.readline())
        if m: ptys.append(m.group(1))
    app("A", A); app("B", B)
    http(B, "POST", "/api/responder/start", {"transport": "serial", "serial": {"port": ptys[0], "baud": 19200}, "delay_ms": 18, "drop_pct": 0})
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for tag, w, h in (("phone", 390, 844), ("desktop", 1280, 800)):
            pg = br.new_context(viewport={"width": w, "height": h}).new_page(); errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(f"http://127.0.0.1:{A}/#setup"); pg.wait_for_selector(".pcard"); time.sleep(1.2)
            lamps = pg.locator("#lamps button[data-lamp]")
            check(f"[{tag}] lamp strip has the six lamps PEER CLK ETH SER PI CAP", [t.split()[0] for t in lamps.all_inner_texts()] == ["PEER", "CLK", "ETH", "SER", "PI", "CAP"], lamps.all_inner_texts())
            sh = pg.evaluate("document.querySelector('.top .strip').getBoundingClientRect().height")
            check(f"[{tag}] the strip is 40 px tall", abs(sh - 40) < 1, sh)
            if tag == "phone":
                top = pg.evaluate("document.querySelector('header.top').getBoundingClientRect().height")
                check("[phone] the whole header (strip only, tabs are at the bottom) is 40 px", abs(top - 40) < 2, top)
            pg.click("[data-testid=lamp-clk]"); pg.wait_for_selector("[data-testid=lamp-note]")
            check(f"[{tag}] tapping a lamp opens one explanation line with a fix button", "CLK" in pg.inner_text("[data-testid=lamp-note]") and pg.locator("[data-testid=lamp-close]").count() == 1)
            pg.click("[data-testid=lamp-close]"); check(f"[{tag}] the note closes", pg.locator("[data-testid=lamp-note]").count() == 0)
            check(f"[{tag}] no About / AI guide link in the header; both in the footer", pg.locator("header.top a[href='/ai-guide'], [data-testid=top-about]").count() == 0 and pg.locator("footer [data-testid=foot-about]").count() == 1 and pg.locator("footer [data-testid=foot-ai-guide]").count() == 1)
            vis = pg.evaluate("(() => { const e = document.querySelector('#aiContext'); const r = e.getBoundingClientRect(); return [r.width, r.height, e.textContent.length]; })()")
            check(f"[{tag}] the AI note is still in the page but not drawn", vis[0] <= 1 and vis[1] <= 1 and vis[2] > 40, vis)
            check(f"[{tag}] test cards are one line of meta, no paragraph, no BUILT-IN badge", pg.locator(".pcard > p:visible").count() == 0 and pg.locator(".pcard .tag.preset:visible").count() == 0)
            ff = pg.evaluate("getComputedStyle(document.querySelector('.pcard .meta')).fontVariantNumeric + '|' + getComputedStyle(document.body).fontVariantNumeric")
            check(f"[{tag}] fixed-width digits (tabular numbers on the page)", "tabular-nums" in ff, ff)
            pg.evaluate("localStorage.setItem('tt-seen-version','1.0')"); pg.reload(); pg.wait_for_selector("[data-testid=updated-banner]", timeout=8000)
            bh = pg.evaluate("document.querySelector('[data-testid=updated-banner]').getBoundingClientRect().height")
            check(f"[{tag}] the update note is one short line", bh < 64, bh)
            # a live serial test
            if True:
                http(A, "POST", "/api/runs", {"profile": {"name": "Instrument e2e", "transport": "serial", "serial": {"port": ptys[1], "baud": 19200}, "duration_s": 14, "interval_ms": 50, "timeout_ms": 500, "frame": "compact", "threshold_ms": 60}})
            pg.goto(f"http://127.0.0.1:{A}/#tests"); pg.reload(); pg.wait_for_selector("#runBanner", timeout=15000); time.sleep(1.5)
            t1 = pg.inner_text("[data-testid=tb-time]"); time.sleep(2.2); t2 = pg.inner_text("[data-testid=tb-time]")
            check(f"[{tag}] the transport bar shows RUNNING and a fixed-width timer that counts", re.fullmatch(r"\d\d:\d\d:\d\d", t1) and t1 != t2 and "RUNNING" in pg.inner_text("[data-testid=tb-word]"), f"{t1} -> {t2}")
            check(f"[{tag}] the old progress card is gone (no bar element #progBar)", pg.locator("#progBar").count() == 0)
            col = pg.evaluate("getComputedStyle(document.querySelector('.stat b')).color"); mono = pg.evaluate("getComputedStyle(document.querySelector('.stat b')).fontFamily")
            check(f"[{tag}] live tile numbers are ink (not green), in a monospace face", col != "rgb(21, 128, 61)" and "mono" in mono.lower() or "Menlo" in mono, f"{col} {mono[:40]}")
            pg.click("[data-testid=lamp-pi]"); check(f"[{tag}] PI lamp explains itself while a test runs", "PI" in pg.inner_text("[data-testid=lamp-note]")); pg.click("[data-testid=lamp-close]")
            try: pg.wait_for_selector("#runBanner .banner.pass, #runBanner .banner.fail", timeout=40000)
            except Exception: pass
            ban = pg.locator("#runBanner .banner.pass, #runBanner .banner.fail")
            if ban.count():
                bg = pg.evaluate("getComputedStyle(document.querySelector('#runBanner .banner')).backgroundColor")
                check(f"[{tag}] the finished verdict is a solid block with white letters", bg in ("rgb(21, 128, 61)", "rgb(185, 28, 28)") and pg.evaluate("getComputedStyle(document.querySelector('#runBanner .banner .big')).color") == "rgb(255, 255, 255)", bg)
            else:
                check(f"[{tag}] the finished verdict block shows", False, "no banner")
            check(f"[{tag}] no script errors", not errs, errs[:2])
        br.close()
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
