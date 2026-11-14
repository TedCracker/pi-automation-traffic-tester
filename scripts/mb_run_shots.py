#!/usr/bin/env python3
"""Two apps on this PC (pi1, pi2) run a real Mirrored Bits test over UDP; Playwright captures the live view of BOTH Pis during the run and the Results card,
and checks the PDF + CSV.  -> docs/screenshots/mb-run-*.png / mb-results*.png.  Fails on any browser error."""
import json, os, signal, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("SHOTS_OUT") or os.path.join(ROOT, "docs", "screenshots")
A, B, UDP = 18192, 18193, 20531
TMP = tempfile.mkdtemp(prefix="tt-mbrun-")
procs, errors = [], []


def http(port, method, path, body=None, raw=False):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=40) as f:
        d = f.read()
        return d if raw else (json.loads(d) if d else None)


def app(name, port):
    env = dict(os.environ, TT_TEST_HOOKS="1", TT_ASSUME_SYNCED="1", PYTHONPATH=ROOT)
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{name}"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try:
            http(port, "GET", "/api/status")
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("app did not start")


try:
    app("pi1", A); app("pi2", B)
    http(A, "POST", "/api/pi", {"name": "pi1"}) if False else None
    pid2 = http(A, "POST", "/api/peers", {"host": f"127.0.0.1:{B}"})["id"]
    prof = {"name": "MB shots", "transport": "udp", "mode": "mirrored", "dest": {"pi": pid2}, "net": {"port": UDP}, "duration_s": 25, "timeout_ms": 500, "threshold_ms": 60,
            "mirrored": {"rate_baud": 9600, "trip_every_s": 1.0}}
    r = http(A, "POST", "/api/runs", {"profile": prof})
    rid = r["run_id"]
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        def page(port, w, h, scheme="light"):
            ctx = br.new_context(viewport={"width": w, "height": h}, color_scheme=scheme, is_mobile=w < 700, has_touch=w < 700)
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(f"{port}: {e}"))
            pg.on("console", lambda m: errors.append(f"{port}: {m.text}") if m.type == "error" else None)
            pg.goto(f"http://127.0.0.1:{port}/#run")
            pg.wait_for_selector("[data-testid=mb-live] .mbsq", timeout=20000)
            return ctx, pg
        ctx, pg = page(A, 1280, 900)
        time.sleep(3)
        pg.click("[data-flip='3'][data-side=local]")                      # tap TMB3 on pi1 ...
        time.sleep(1.0)
        pg.screenshot(path=f"{OUT}/mb-run-pi1.png", full_page=True)
        rm = pg.evaluate("[...document.querySelectorAll('[data-testid=mb-card-b] [data-testid=mb-rmb] .mbsq')].map(e => e.classList.contains('on') ? 1 : 0).join('')")
        print("pi2 RMB seen from pi1's screen:", rm)
        if rm[2] != "1":
            errors.append(f"flipping TMB3 on pi1 did not show on pi2's RMB3 (saw {rm})")
        pg.click("[data-testid=mb-card-b] [data-flip='2'][data-side=remote]")   # ... and TMB2 on pi2, through pi1's screen
        time.sleep(1.0)
        rm = pg.evaluate("[...document.querySelectorAll('[data-testid=mb-card-a] [data-testid=mb-rmb] .mbsq')].map(e => e.classList.contains('on') ? 1 : 0).join('')")
        print("pi1 RMB after tapping TMB2 on pi2:", rm)
        if rm[1] != "1":
            errors.append(f"flipping TMB2 on pi2 (via pi1's screen) did not show on pi1's RMB2 (saw {rm})")
        ctx.close()
        ctx2, pg2 = page(B, 390, 844, "dark")                              # the answering Pi's own screen, phone, dark
        time.sleep(2)
        pg2.screenshot(path=f"{OUT}/mb-run-pi2-phone-dark.png", full_page=True)
        ctx2.close()
        t0 = time.time()
        while time.time() - t0 < 60:
            if http(A, "GET", f"/api/runs/{rid}")["status"] != "running":
                break
            time.sleep(0.5)
        run = http(A, "GET", f"/api/runs/{rid}")
        s = run["summary"]
        print("status", run["status"], "verdict", s.get("verdict"), "transfer", s["mirrored"]["transfer"]["basis"], s["mirrored"]["transfer"]["n"])
        ctx, pg = page(A, 1280, 900) if False else (br.new_context(viewport={"width": 1280, "height": 900}), None)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(f"results: {e}"))
        pg.on("console", lambda m: errors.append(f"results: {m.text}") if m.type == "error" else None)
        pg.goto(f"http://127.0.0.1:{A}/#results")
        pg.wait_for_selector(".runrow, [data-open-run], .rrow, table", timeout=15000)
        time.sleep(1.0)
        try:
            pg.wait_for_selector("[data-testid=mb-result]", timeout=6000)
        except Exception:
            pg.click("text=MB shots")
            try:
                pg.wait_for_selector("[data-testid=mb-result]", timeout=10000)
            except Exception:
                print("RESULTS PAGE TEXT:", pg.inner_text("#main")[:1200].replace("\n", " | "))
                print("ERRORS SO FAR:", errors)
                pg.screenshot(path="/tmp/mb-results-debug.png", full_page=True)
                raise
        pg.screenshot(path=f"{OUT}/mb-results.png", full_page=True)
        br.close()
    pdf = http(A, "GET", f"/api/report.pdf?run_ids={rid}", raw=True)
    csvb = http(A, "GET", f"/api/runs/{rid}/export.csv?kind=mbevents", raw=True).decode()
    print("pdf bytes", len(pdf), "csv lines", len(csvb.splitlines()))
    if pdf[:4] != b"%PDF" or len(csvb.splitlines()) < 3:
        errors.append("PDF or CSV export missing")
finally:
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except Exception:
            pass
if errors:
    print("ERRORS:", *errors, sep="\n  ")
    sys.exit(1)
print("ok")
