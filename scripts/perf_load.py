#!/usr/bin/env python3
"""First-load measurement of the web UI: requests, bytes, time to the first usable screen (desktop + throttled phone).
Usage: python3 scripts/perf_load.py [repo_root] [port]   (starts its own app instance on a temp data dir)"""
import json, os, signal, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18210
tmp = tempfile.mkdtemp(prefix="tt-perf-")
p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--role", "A", "--port", str(PORT), "--host", "127.0.0.1", "--data-dir", tmp],
                     cwd=ROOT, env=dict(os.environ, PYTHONPATH=ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
try:
    for _ in range(80):
        try: urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=2); break
        except Exception: time.sleep(0.25)
    out = {}
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for name, vp, slow in (("desktop", {"width": 1280, "height": 900}, False), ("phone-slow-wifi", {"width": 390, "height": 844}, True)):
            ctx = br.new_context(viewport=vp); pg = ctx.new_page()
            reqs = []
            pg.on("response", lambda r: reqs.append((r.url, int(r.headers.get("content-length") or 0), r.headers.get("content-encoding", ""))))
            if slow:
                cdp = ctx.new_cdp_session(pg); cdp.send("Network.enable")
                cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 40, "downloadThroughput": 1.5e6 / 8, "uploadThroughput": 750e3 / 8})
                cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
            t0 = time.time(); pg.goto(f"http://127.0.0.1:{PORT}/"); pg.wait_for_selector(".pcard"); t = time.time() - t0
            static = [r for r in reqs if "/api/" not in r[0]]
            out[name] = {"usable_s": round(t, 2), "requests": len(reqs), "api_calls": len(reqs) - len(static), "static_kb_on_wire": round(sum(r[1] for r in static) / 1024, 1),
                         "compressed": sum(1 for r in static if r[2]), "overflow_px": pg.evaluate("document.documentElement.scrollWidth - window.innerWidth")}
            ctx.close()
        br.close()
    print(json.dumps(out, indent=1))
finally:
    os.killpg(p.pid, signal.SIGTERM); subprocess.run(["rm", "-rf", tmp])
