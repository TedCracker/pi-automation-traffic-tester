#!/usr/bin/env python3
"""Network card UI in a real browser (Playwright/Chromium, phone 375x812 + 360x740 dark + desktop 1280x800) against the real static files and the real netcfg API on a FAKE NetworkManager
(scripts/netcfg_ui_harness.py). Run via scripts/e2e-netcfg-ui.sh (system python3 with playwright). Prints 'CHECK name | pass/FAIL | evidence'. Screenshots: docs/screenshots/network-*.png"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "docs", "screenshots")
TMP = tempfile.mkdtemp(prefix="tt-netui-")
PORT = 18470
BASE = f"http://127.0.0.1:{PORT}"
results = []
proc = None


def check(name, ok, ev=""):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:400]}", flush=True)


def http(method, path, body=None):
    r = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as f:
        d = f.read()
        return json.loads(d) if d else None


def venv_python():
    """The repo's venv (fastapi/uvicorn); a git worktree has none of its own, so TT_VENV_PYTHON or the main checkout's can be used."""
    for c in (os.environ.get("TT_VENV_PYTHON"), os.path.join(ROOT, "venv/bin/python"), os.path.expanduser("~/pi-traffic-tester/venv/bin/python")):
        if c and os.path.exists(c):
            return c
    return sys.executable


def nm_props():
    """The fake NetworkManager's profiles (the harness keeps them in <tmp>/*/nm.json)."""
    f = next(os.path.join(r, n) for r, _, fs in os.walk(TMP) for n in fs if n == "nm.json")
    return {p["name"]: p["props"] for p in json.load(open(f))["profiles"]}


def T(tid):
    return f'[data-testid="{tid}"]'


AUDIT = r"""() => {
  const vw = innerWidth, SEL = 'button, a[href], input:not([type=hidden]), select, textarea, summary, label.chk';
  const vis = el => el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) && el.getClientRects().length > 0;
  const nm = el => el.tagName.toLowerCase() + (el.dataset.testid ? `[${el.dataset.testid}]` : '') + ((el.textContent || '').trim().slice(0, 18) ? ` "${(el.textContent || '').trim().slice(0, 18)}"` : '');
  const small = [], fonts = [];
  let n = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (!vis(el)) continue; n++;
    const r = el.getBoundingClientRect();
    if (r.width < 43.5 || r.height < 43.5) small.push(`${nm(el)} ${r.width.toFixed(0)}x${r.height.toFixed(0)}`);
    if (el.matches('input:not([type=checkbox]):not([type=radio]), select, textarea') && parseFloat(getComputedStyle(el).fontSize) < 16) fonts.push(nm(el));
  }
  const de = document.documentElement, bo = document.body, off = [];
  if (de.scrollWidth > vw || bo.scrollWidth > vw) for (const el of document.querySelectorAll('body *')) { const r = el.getBoundingClientRect(); if (r.width && r.right > vw + 1 && vis(el)) { off.push(nm(el) + ' right=' + r.right.toFixed(0)); if (off.length > 4) break; } }
  return { vw, sw: de.scrollWidth, bw: bo.scrollWidth, n, small: [...new Set(small)], fonts: [...new Set(fonts)], off };
}"""


def audit(pg, label, tap=True):
    a = pg.evaluate(AUDIT)
    check(f"{label}: no sideways scroll", a["sw"] <= a["vw"] and a["bw"] <= a["vw"], f"scrollWidth {a['sw']}/{a['bw']} <= {a['vw']} {a['off']}")
    if tap:
        check(f"{label}: all {a['n']} controls >= 44 x 44 px", not a["small"], "; ".join(a["small"][:6]))
        check(f"{label}: inputs and selects >= 16 px text", not a["fonts"], "; ".join(a["fonts"][:6]))


def shot(pg, name):
    os.makedirs(SHOTS, exist_ok=True)
    pg.screenshot(path=os.path.join(SHOTS, name))


def open_card(ctx):
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE + "/harness.html")
    pg.wait_for_selector(T("net-card") + " " + T("net-wifi"))
    pg.errs = errs
    return pg


def main():
    global proc
    proc = subprocess.Popen([venv_python(), os.path.join(ROOT, "scripts/netcfg_ui_harness.py"), str(PORT), TMP], stdout=open(f"{TMP}/h.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(80):
        try:
            http("GET", "/api/net/pending")
            break
        except Exception:
            time.sleep(0.25)
    else:
        raise RuntimeError("harness did not start: " + open(f"{TMP}/h.log").read()[-400:])
    with sync_playwright() as p:
        br = p.chromium.launch()
        phone = br.new_context(viewport={"width": 375, "height": 812}, device_scale_factor=2, has_touch=True, is_mobile=True)
        pg = open_card(phone)
        # ---- what it shows
        check("shows which port you are connected through", "eth0 (Ethernet, Internet + Test)" in pg.inner_text(T("net-through")), pg.inner_text(T("net-through")))
        live = pg.inner_text(T("net-live-eth0"))
        check("live link, speed and IP per port", "link up" in live and "1000 Mb/s" in live and "192.168.1.50" in live and "no cable" in pg.inner_text(T("net-live-eth1")), live)
        check("'you are here' marks eth0 only", pg.locator(T("net-you-eth0")).count() == 1 and pg.locator(T("net-you-eth1")).count() == 0)
        check("Wi-Fi: current mode explained in plain words", "joined" in pg.inner_text(T("net-wifi-now")).lower() and "HomeNet" in pg.inner_text(T("net-wifi-now")), pg.inner_text(T("net-wifi-now")))
        audit(pg, "375px card")
        shot(pg, "network-375.png")
        # ---- Wi-Fi: access point form, validation, harmless change applies with a warning sheet and no countdown
        pg.click(T("net-wifi-mode-ap"))
        check("access point form has SSID, password, band, channel; SSID defaults to the Pi's name", all(pg.locator(T(t)).count() for t in ("net-ssid", "net-password", "net-band", "net-channel")) and pg.input_value(T("net-ssid")) != "", pg.input_value(T("net-ssid")))
        pg.fill(T("net-ssid"), "lab-pi")
        pg.fill(T("net-password"), "short")
        pg.click(T("net-wifi-apply"))
        pg.wait_for_selector("#toast div")
        check("short password is refused with a plain message", "8 to 63" in pg.inner_text("#toast"), pg.inner_text("#toast"))
        pg.fill(T("net-password"), "password")
        pg.click(T("net-wifi-apply"))
        pg.wait_for_selector(T("net-confirm"))
        txt = pg.inner_text(T("net-confirm"))
        check("default password triggers a warning sheet (not the 'you are cut off' one)", "factory default" in txt and "connection you are using" not in txt.lower() and "access point named 'lab-pi'" in txt, txt[:300])
        audit(pg, "375px confirm sheet")
        shot(pg, "network-confirm-wifi-375.png")
        pg.click(T("net-confirm-yes"))
        pg.wait_for_function("() => document.querySelector('[data-testid=net-wifi-now]') && /access point/i.test(document.querySelector('[data-testid=net-wifi-now]').innerText)", timeout=15000)
        check("access point applied; no countdown (you are on Ethernet, not affected)", pg.locator(T("net-pending-banner")).is_hidden() and http("GET", "/api/net/pending")["pending"] is None, pg.inner_text(T("net-wifi-now")))
        # ---- scan + join (the Pi is an access point now: scanning is refused in plain words; type the name)
        pg.click(T("net-wifi-mode-client"))
        pg.click(T("net-scan"))
        pg.wait_for_selector(T("net-scan-list") + " .check")
        check("scan while access point: explained, not an error dump", "access point" in pg.inner_text(T("net-scan-list")).lower(), pg.inner_text(T("net-scan-list"))[:160])
        pg.fill(T("net-ssid"), "HomeNet")
        pg.fill(T("net-password"), "homepassword")
        pg.click(T("net-wifi-apply"))
        pg.wait_for_function("() => /joined/i.test(document.querySelector('[data-testid=net-wifi-now]').innerText)", timeout=15000)
        check("joining a Wi-Fi network from the card (no sheet: you are on Ethernet)", pg.locator(T("net-confirm")).count() == 0 and pg.locator(T("net-pending-banner")).is_hidden(), pg.inner_text(T("net-wifi-now")))
        pg.click(T("net-wifi-mode-client"))
        pg.click(T("net-scan"))
        pg.wait_for_selector(T("net-scan-item"))
        names = [x.inner_text() for x in pg.locator(T("net-scan-item")).all()]
        check("scan lists the networks (strongest first, duplicates merged)", len(names) == 2 and names[0].startswith("HomeNet") and names[1].startswith("Cafe"), names)
        pg.click(T("net-scan-item") + " >> nth=1")
        check("tapping a network fills the name", pg.input_value(T("net-ssid")) == "Cafe")
        pg.click(T("net-wifi-mode-off"))
        pg.click(T("net-wifi-apply"))
        pg.wait_for_function("() => /Now: off/i.test(document.querySelector('[data-testid=net-wifi-now]').innerText)", timeout=15000)
        check("Wi-Fi off needs no sheet while Ethernet has a link (nothing is cut off)", pg.locator(T("net-confirm")).count() == 0 and pg.locator(T("net-pending-banner")).is_hidden())

        # ---- Ethernet: a change to the port you use asks, then counts down; Cancel does nothing
        pg.click(T("net-port-method-eth0-static"))
        pg.fill(T("net-port-ip-eth0"), "192.168.50.2")
        pg.fill(T("net-port-gw-eth0"), "192.168.50.1")
        pg.select_option(T("net-port-label-eth0"), "test")
        before = http("GET", "/__ctl/profiles")
        pg.click(T("net-port-apply-eth0"))
        pg.wait_for_selector(T("net-confirm"))
        txt = pg.inner_text(T("net-confirm"))
        check("changing the port you use: confirm sheet names it, lists the steps and the safety net",
              "connection you are using" in txt.lower() and "eth0" in txt and "fixed address 192.168.50.2/24" in txt and "Keep" in txt and "20 seconds" in txt, txt[:360])
        audit(pg, "375px confirm sheet (your port)")
        shot(pg, "network-confirm-port-375.png")
        pg.click(T("net-confirm-no"))
        check("Cancel changes nothing", http("GET", "/__ctl/profiles") == before and http("GET", "/api/net/pending")["pending"] is None)
        pg.click(T("net-port-apply-eth0"))
        pg.wait_for_selector(T("net-confirm"))
        pg.click(T("net-confirm-yes"))
        pg.wait_for_selector(T("net-pending-banner") + ":not([hidden])", timeout=15000)
        t0 = pg.inner_text(T("net-countdown"))
        check("countdown banner 'Keep these settings? Reverting in N s' with Keep and Revert now", "Keep these settings? Reverting in" in t0 and pg.locator(T("net-keep")).is_visible() and pg.locator(T("net-revert-now")).is_visible(), t0)
        audit(pg, "375px countdown banner")
        shot(pg, "network-banner-375.png")
        time.sleep(3)
        t1 = pg.inner_text(T("net-countdown"))
        n0, n1 = int(t0.split("in ")[1].split(" s")[0]), int(t1.split("in ")[1].split(" s")[0])
        check("the countdown runs down", n1 < n0 <= 20, f"{n0} -> {n1}")
        prof = nm_props()
        check("eth0 now has the static profile, labelled Test (never default route)", prof.get("tt-eth-eth0", {}).get("ipv4.method") == "manual" and prof["tt-eth-eth0"]["ipv4.never-default"] == "yes")
        # ---- reload mid-countdown (= reconnecting): the banner comes back by itself
        pg.reload()
        pg.wait_for_selector(T("net-pending-banner") + ":not([hidden])", timeout=15000)
        t2 = pg.inner_text(T("net-countdown"))
        n2 = int(t2.split("in ")[1].split(" s")[0])
        check("after a reload / reconnect the banner and its remaining time are still there", 0 < n2 <= n1 and "Keep these settings?" in t2, t2)
        pg.click(T("net-keep"))
        pg.wait_for_selector(T("net-pending-banner"), state="hidden", timeout=10000)
        check("Keep makes it permanent (banner gone, no revert later)", http("GET", "/api/net/pending")["pending"] is None and "tt-eth-eth0" in http("GET", "/__ctl/profiles")["names"])
        time.sleep(1)
        check("saved static address shows in the form after Keep", pg.input_value(T("net-port-ip-eth0")) == "192.168.50.2" and pg.is_checked(T("net-port-method-eth0-static")))

        # ---- Revert now
        pg.click(T("net-port-method-eth0-dhcp"))
        pg.select_option(T("net-port-label-eth0"), "both")
        pg.click(T("net-port-apply-eth0"))
        pg.wait_for_selector(T("net-confirm"))
        pg.click(T("net-confirm-yes"))
        pg.wait_for_selector(T("net-pending-banner") + ":not([hidden])", timeout=15000)
        pg.click(T("net-revert-now"))
        pg.wait_for_selector(T("net-pending-banner"), state="hidden", timeout=15000)
        time.sleep(2.5)
        check("Revert now restores the kept static settings", http("GET", "/api/net/pending")["pending"] is None and pg.input_value(T("net-port-ip-eth0")) == "192.168.50.2", pg.input_value(T("net-port-ip-eth0")))

        # ---- nobody presses Keep: it undoes itself
        pg.click(T("net-port-method-eth0-dhcp"))
        pg.click(T("net-port-apply-eth0"))
        pg.wait_for_selector(T("net-confirm"))
        pg.click(T("net-confirm-yes"))
        pg.wait_for_selector(T("net-pending-banner") + ":not([hidden])", timeout=15000)
        pg.wait_for_selector(T("net-pending-banner"), state="hidden", timeout=70000)
        time.sleep(2.5)
        prof = nm_props()
        check("no Keep within the time: the Pi goes back to the previous settings by itself, and the page says so", prof["tt-eth-eth0"]["ipv4.method"] == "manual" and pg.locator(T("net-last-event")).count() == 1
              and "restored" in pg.inner_text(T("net-last-event")).lower(), pg.inner_text("#main")[:120])
        # ---- the emergency access point switch
        pg.click(T("net-fallback"))
        time.sleep(0.8)
        check("emergency access point switch saves", http("GET", "/api/net/config")["fallback"]["enabled"] is False)
        phone.close()

        dark = br.new_context(viewport={"width": 360, "height": 740}, device_scale_factor=2, has_touch=True, is_mobile=True, color_scheme="dark")
        pd = open_card(dark)
        pd.click(T("net-wifi-mode-ap"))
        audit(pd, "360px dark card (access point form)")
        shot(pd, "network-360-dark.png")
        dark.close()

        desk = br.new_context(viewport={"width": 1280, "height": 800})
        pk = open_card(desk)
        audit(pk, "1280px card", tap=False)
        shot(pk, "network-1280.png")
        check("no script errors", not pg.errs and not pd.errs and not pk.errs, pg.errs + pd.errs + pk.errs)
        desk.close()
        br.close()


try:
    main()
except Exception as e:                                                       # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("UI test ran to the end", False, repr(e))
finally:
    if proc:
        proc.terminate()
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
