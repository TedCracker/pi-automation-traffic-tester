#!/usr/bin/env python3
"""Playwright screenshots of the Mirrored Bits live view (internal self-test pair) -> docs/screenshots/mb-*.png.
Desktop + phone (portrait and landscape), light + dark, link OK / broken / impaired.  Fails on any browser error or a layout overflow."""
import json, os, signal, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("SHOTS_OUT") or os.path.join(ROOT, "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)
PORT = 18190
TMP = tempfile.mkdtemp(prefix="tt-mbshots-")
errors = []
CLEAR = {"break_link": False, "latency_ms": 0, "drop_pct": 0, "jitter_ms": 0, "bit_error_pct": 0}


def http(method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as f:
        d = f.read()
        return json.loads(d) if d else None


env = dict(os.environ, TT_TEST_HOOKS="1", TT_ASSUME_SYNCED="1", PYTHONPATH=ROOT)
app = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--port", str(PORT), "--host", "127.0.0.1", "--data-dir", f"{TMP}/a"],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
try:
    for _ in range(60):
        try:
            http("GET", "/api/status")
            break
        except Exception:
            time.sleep(0.25)
    http("POST", "/api/mb/selftest", {"duration_s": 900, "baud": 9600, "a": {"cbad_window_s": 10, "cbad_pu_pct": 20}, "b": {"cbad_window_s": 10, "cbad_pu_pct": 20}})
    time.sleep(1.0)
    http("POST", "/api/mb/tmb", {"bit": 1, "value": 1})
    http("POST", "/api/mb/tmb", {"bit": 3, "value": 1})
    with sync_playwright() as pw:
        br = pw.chromium.launch()

        def page(w, h, scheme):
            ctx = br.new_context(viewport={"width": w, "height": h}, color_scheme=scheme, device_scale_factor=1, is_mobile=w < 700, has_touch=w < 700)
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            pg.goto(f"http://127.0.0.1:{PORT}/#run")
            pg.evaluate("localStorage.setItem('tt-theme','%s')" % scheme)
            pg.reload()
            pg.wait_for_selector("[data-testid=mb-live] .mbsq", timeout=15000)
            return ctx, pg

        def shot(pg, name, wait=1.8):
            time.sleep(wait)
            over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            if over > 1:
                errors.append(f"{name}: page is {over}px wider than the screen")
            pg.screenshot(path=f"{OUT}/{name}.png", full_page=True)

        for scheme in ("light", "dark"):
            ctx, pg = page(1280, 900, scheme)
            http("POST", "/api/mb/impair", {"scope": "both", "cfg": CLEAR})
            shot(pg, "mb-desktop-ok" + ("" if scheme == "light" else "-dark"))
            if scheme == "light":
                pg.click("[data-testid=mb-break-link]")
                shot(pg, "mb-desktop-broken", 2.0)
                pg.click("[data-testid=mb-break-link]")
                time.sleep(1.5)
                pg.click("[data-chip=lat]"); pg.click("[data-chip=jit]"); pg.click("[data-chip=drop]")
                shot(pg, "mb-desktop-impaired", 2.5)
                pg.click("#mbTabs [data-tab=charts]")
                shot(pg, "mb-desktop-charts", 1.0)
                pg.click("#mbTabs [data-tab=events]")
                shot(pg, "mb-desktop-events", 0.5)
                pg.click("#mbTabs [data-tab=settings]")
                shot(pg, "mb-desktop-settings", 0.5)
                http("POST", "/api/mb/impair", {"scope": "both", "cfg": CLEAR})
            ctx.close()
        for scheme in ("light", "dark"):
            ctx, pg = page(390, 844, scheme)
            suffix = "" if scheme == "light" else "-dark"
            http("POST", "/api/mb/impair", {"scope": "both", "cfg": CLEAR})
            shot(pg, f"mb-phone-ok{suffix}")
            if scheme == "light":
                pg.click("[data-testid=mb-break-link]")
                shot(pg, "mb-phone-broken", 2.0)
                pg.click("[data-testid=mb-break-link]")
                time.sleep(1.5)
                pg.click("[data-chip=lat]"); pg.click("[data-chip=drop]")
                shot(pg, "mb-phone-impaired", 2.5)
                http("POST", "/api/mb/impair", {"scope": "both", "cfg": CLEAR})
            ctx.close()
        ctx, pg = page(844, 390, "light")
        shot(pg, "mb-phone-landscape")
        ctx.close()
        br.close()
    http("POST", "/api/mb/selftest/stop", {})
finally:
    os.killpg(app.pid, signal.SIGTERM)
if errors:
    print("ERRORS:", *errors, sep="\n  ")
    sys.exit(1)
print("ok: screenshots in", OUT)
