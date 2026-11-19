#!/usr/bin/env python3
"""Instrument-redesign screenshots: every main screen at 390 px (phone) and 1280 px (desktop) -> docs/dev/ux-impl-shots/phaseN/.
Run with system python3 (playwright). UX_PHASE=0 (default). Two local apps (A, B) with a serial pty pair so a live Mirrored-Bits-free serial run shows numbers."""
import json, os, re, signal, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHASE = os.environ.get("UX_PHASE", "1")
OUT = os.path.join(ROOT, "docs", "dev", "ux-impl-shots", f"phase{PHASE}"); os.makedirs(OUT, exist_ok=True)
TMP = tempfile.mkdtemp(prefix="tt-uxshots-"); procs = []
A, B = 18280, 18281
errors = []


def http(port, method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as f:
        d = f.read(); return json.loads(d) if d else None


def app(role, port):
    env = dict(os.environ, TT_TEST_HOOKS="1", TT_ASSUME_SYNCED="1", PYTHONPATH=ROOT)
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--role", role, "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{role}"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(60):
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
    http(B, "POST", "/api/responder/start", {"transport": "serial", "serial": {"port": ptys[0], "baud": 19200}, "delay_ms": 18, "drop_pct": 2})
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        sizes = {"phone": (390, 844), "desk": (1280, 800)}
        pages = {}
        for k, (w, h) in sizes.items():
            c = br.new_context(viewport={"width": w, "height": h}, color_scheme="light", device_scale_factor=2 if k == "phone" else 1)
            pg = c.new_page(); pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            pages[k] = pg
        shot = lambda k, name, full=False: pages[k].screenshot(path=f"{OUT}/{k}-{name}.png", full_page=full)
        for k, pg in pages.items():
            pg.goto(f"http://127.0.0.1:{A}/#home"); pg.wait_for_selector("[data-testid=home-links]"); time.sleep(1.0); shot(k, "home")
            pg.goto(f"http://127.0.0.1:{A}/#tests"); pg.wait_for_selector(".pcard"); time.sleep(0.8)
            shot(k, "tests")
            pg.click("[data-testid=lamp-peer]"); time.sleep(0.3); shot(k, "setup-lamp-open"); pg.click("[data-testid=lamp-close]")
        # a live serial run
        pg = pages["desk"]
        pg.goto(f"http://127.0.0.1:{A}/#tests"); pg.wait_for_selector(".pcard")
        http(A, "POST", "/api/runs", {"profile": {"name": "Phase 0 demo", "transport": "serial", "serial": {"port": ptys[1], "baud": 19200}, "duration_s": 40, "interval_ms": 50, "timeout_ms": 500, "frame": "compact", "threshold_ms": 60}})
        time.sleep(1)
        for k, pg in pages.items():
            pg.goto(f"http://127.0.0.1:{A}/#tests"); pg.reload(); pg.wait_for_selector("#runBanner"); pg.wait_for_selector("#chart canvas"); time.sleep(10); shot(k, "run-live", full=True)
            pg.click("[data-testid=lamp-pi]"); time.sleep(0.3); shot(k, "run-live-lamp-open")
        for _ in range(80):
            if not http(A, "GET", "/api/status")["run"]["running"]: break
            time.sleep(1)
        for k, pg in pages.items():
            pg.goto(f"http://127.0.0.1:{A}/#tests"); pg.reload(); time.sleep(1.5); shot(k, "run-done", full=True)
            pg.goto(f"http://127.0.0.1:{A}/#results"); pg.wait_for_selector("tr[data-run]"); time.sleep(0.8); shot(k, "results", full=True)
            pg.click("tr[data-run] >> nth=0"); pg.wait_for_selector("#detail .banner"); time.sleep(2); shot(k, "results-detail", full=True)
        # generator, sniffer, settings, about, update banner
        for k, pg in pages.items():
            for tab in ("generator", "sniffer", "config", "config/pis", "config/network", "config/time", "config/update", "config/password", "config/help", "config/demo"):
                pg.goto(f"http://127.0.0.1:{A}/#{tab}"); time.sleep(1.4); shot(k, tab.replace("/", "-"), full=True)
            pg.evaluate("localStorage.setItem('tt-seen-version','1.0')"); pg.goto(f"http://127.0.0.1:{A}/#tests"); pg.reload(); time.sleep(1.5); shot(k, "update-banner")
        br.close()
    print("shots in", OUT, "| browser errors:", errors[:5])


try:
    main()
finally:
    for p in procs:
        try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception: pass
