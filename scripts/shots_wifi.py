#!/usr/bin/env python3
"""Screenshots of the Sniffer's built-in Wi-Fi (Nexmon) panel -> docs/screenshots/wifi-*.png. Run with system python3 (playwright).
Shot 1 = the real app on this PC (no Pi helper: it says so). Shots 2-3 = the same page with the Pi's helper answers MOCKED
(status 'ready' / 'monitor mode') to show the layout the user sees on a Pi with Nexmon installed; file names say "mocked"."""
import json, os, signal, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "screenshots"); os.makedirs(OUT, exist_ok=True)
TMP = tempfile.mkdtemp(prefix="tt-shots-wifi-"); PORT = 18191
env = dict(os.environ, TT_TEST_HOOKS="1", TT_ASSUME_SYNCED="1", PYTHONPATH=ROOT)
p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--role", "A", "--port", str(PORT), "--host", "127.0.0.1", "--data-dir", TMP],
                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
try:
    for _ in range(60):
        try: urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=2); break
        except Exception: time.sleep(0.25)
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        pg = br.new_page(viewport={"width": 1280, "height": 900}, color_scheme="light")
        pg.goto(f"http://127.0.0.1:{PORT}/#sniffer"); pg.wait_for_selector(".pcard"); time.sleep(0.8)
        pg.click(".pcard:has-text(\"built-in Wi-Fi\") >> nth=0"); time.sleep(0.8)
        pg.locator("text=Built-in Wi-Fi sniffing (Nexmon)").scroll_into_view_if_needed()
        pg.screenshot(path=f"{OUT}/wifi-01-builtin-panel-no-helper.png", full_page=True)
        ready = {"available": True, "reason": "", "state": "stock", "kernel": "6.12.109+rpt-rpi-2712", "interfaces": ["wlan0"], "monitor_ifaces": [], "tools": {}, "warning":
                 "Internet over Wi-Fi will drop while the built-in Wi-Fi is sniffing. Use an Ethernet cable for the internet (and for this web page), or a USB Wi-Fi adapter for sniffing. Normal Wi-Fi comes back when you stop, press Restore, or reboot."}
        pg.route("**/api/wifi/builtin/status", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(ready)))
        pg.reload(); pg.wait_for_selector(".pcard"); pg.click(".pcard:has-text(\"built-in Wi-Fi\") >> nth=0"); time.sleep(0.8)
        pg.locator("input[data-p='wifi.builtin_ack']").check(); time.sleep(0.3)
        pg.locator("text=Built-in Wi-Fi sniffing (Nexmon)").scroll_into_view_if_needed()
        pg.screenshot(path=f"{OUT}/wifi-02-builtin-panel-mocked-ready.png", full_page=True)
        mon = dict(ready, state="monitor", monitor_ifaces=["wlan0mon"])
        pg.unroute("**/api/wifi/builtin/status"); pg.route("**/api/wifi/builtin/status", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(mon)))
        pg.route("**/api/wifi/builtin/restore", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "message": "Normal Wi-Fi is back (wlan0 is up)."})))
        pg.reload(); pg.wait_for_selector(".pcard"); pg.click(".pcard:has-text(\"built-in Wi-Fi\") >> nth=0"); time.sleep(0.8)
        pg.click("[data-act=bw-restore]"); time.sleep(1.0)
        pg.locator("text=Built-in Wi-Fi sniffing (Nexmon)").scroll_into_view_if_needed()
        pg.screenshot(path=f"{OUT}/wifi-03-restore-clicked-mocked.png", full_page=True)
        br.close()
    print("ok", sorted(f for f in os.listdir(OUT) if f.startswith("wifi-")))
finally:
    try: os.killpg(p.pid, signal.SIGTERM)
    except Exception: pass
