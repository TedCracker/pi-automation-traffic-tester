#!/usr/bin/env python3
"""Settings screen for 'no fixed roles' in a real browser (phone 375 px, light + dark): This Pi (name), Pis on this network (list, add by IP, password, remove),
Time source, Network placeholder, Software update -> 'Also update another Pi'. Two app instances on one PC (no default reflector, no mDNS).
Run via scripts/e2e-settings-peers.sh (system python3 with playwright). Prints 'CHECK name | pass/FAIL | evidence'. Screenshots: docs/screenshots/settings-peers-*.png"""
import re, json, os, shutil, signal, subprocess, sys, tempfile, time, traceback, urllib.request, urllib.error
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "docs", "screenshots")
TMP = tempfile.mkdtemp(prefix="tt-setpeers-")
procs, results = [], []
PA, PB = 18360, 18361


def check(name, ok, ev=""):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:500]}", flush=True)


def http(port, method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as f:
            d = f.read(); return json.loads(d) if d else None
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "detail": json.loads(e.read() or b"{}").get("detail")}


def start(name, port):
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{name}"],
                         env=dict(os.environ, PYTHONPATH=ROOT, TT_ASSUME_SYNCED="1", TT_NO_DEFAULT_REFLECTOR="1", TT_NO_DISCOVERY="1"), stdout=open(f"{TMP}/{name}.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try:
            http(port, "GET", "/api/status"); return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"{name} did not start; see {TMP}/{name}.log")


AUDIT = r"""(root) => {
  const vw = innerWidth, SEL = 'button, a[href], input:not([type=hidden]), select, textarea, summary, label.chk';
  const vis = el => { try { return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) && el.getClientRects().length > 0; } catch { return el.getClientRects().length > 0; } };
  const nm = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.dataset.testid ? `[${el.dataset.testid}]` : '');
  const small = [], fonts = [], noid = [];
  for (const el of document.querySelector(root).querySelectorAll(SEL)) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 43.5 || r.height < 43.5) small.push(`${nm(el)} ${r.width.toFixed(0)}x${r.height.toFixed(0)}`);
    if (el.matches('input:not([type=checkbox]):not([type=file]), select, textarea') && parseFloat(getComputedStyle(el).fontSize) < 16) fonts.push(nm(el));
    if (!el.dataset.testid) noid.push(nm(el));
  }
  const de = document.documentElement;
  return { sw: de.scrollWidth, vw, small, fonts, noid };
}"""


def main():
    start("a", PA); start("b", PB)
    me_b = http(PB, "GET", "/api/pi")
    check("Two instances start without roles (no 'role' in status)", "role" not in http(PA, "GET", "/api/status") and "role" not in http(PB, "GET", "/api/status"), "")
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for theme in ("light", "dark"):
            ctx = br.new_context(viewport={"width": 375, "height": 800}, device_scale_factor=2, has_touch=True, is_mobile=True, color_scheme=theme)
            pg = ctx.new_page()
            pg.add_init_script(f"localStorage.setItem('tt_theme','{theme}')")
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(f"http://127.0.0.1:{PA}/#settings"); pg.wait_for_selector('[data-testid="thispi-card"]', timeout=15000)
            if theme == "light":
                pg.wait_for_selector('[data-testid="peers-empty"]', timeout=10000)
            else:
                pg.wait_for_selector('[data-testid="peer-row"]', timeout=10000)
            tag = f"[{theme}] "
            html = pg.inner_text("#main")
            check(tag + "Settings shows This Pi, Pis on this network and Time source", all(x in html for x in ("This Pi", "Pis on this network", "Time source")), "")
            if theme == "light":
                check(tag + "Empty state explains how Pis appear", "No other Pi found yet." in pg.inner_text('[data-testid="peers-empty"]') and "add one by its IP address" in pg.inner_text('[data-testid="peers-empty"]'), pg.inner_text('[data-testid="peers-empty"]'))
            check(tag + "No old role words anywhere in Settings", not (re.search(r"\bPi [AB]\b", html) or any(w in html for w in ("Responder", "Tester ·", "partner"))), "")
            check(tag + "Always-on answering is stated (port 20500)", "Always answers UDP and TCP test messages on port 20500" in pg.inner_text('[data-testid="thispi-reflector"]'), "")
            check(tag + "Network card mount exists (module arrives at merge; empty is fine)", pg.locator("#netCard").count() == 1, "")
            if theme == "light":
                # ---- rename this Pi
                pg.fill('[data-testid="thispi-name-input"]', "lab-one"); pg.click('[data-testid="thispi-name-save"]')
                pg.wait_for_function("document.querySelector('[data-testid=\"thispi-name-msg\"]').textContent.includes('lab-one')", timeout=8000)
                check("Rename this Pi (PUT /api/pi) and show it", http(PA, "GET", "/api/pi")["name"] == "lab-one", "")
                pg.fill('[data-testid="thispi-name-input"]', "bad/name!"); pg.click('[data-testid="thispi-name-save"]'); pg.wait_for_timeout(500)
                check("A bad name shows the plain 1-32 character rule", "1-32" in pg.inner_text('[data-testid="thispi-name-msg"]'), pg.inner_text('[data-testid="thispi-name-msg"]'))
                pg.fill('[data-testid="thispi-name-input"]', ""); pg.click('[data-testid="thispi-name-save"]'); pg.wait_for_timeout(600)
                check("Empty name falls back to the host name", http(PA, "GET", "/api/pi")["name"] == http(PA, "GET", "/api/pi")["default_name"], "")
                # ---- ports (advanced)
                pg.click('[data-testid="thispi-advanced"]'); pg.fill('[data-testid="thispi-ports-input"]', "20500, 20501"); pg.click('[data-testid="thispi-ports-save"]'); pg.wait_for_timeout(500)
                check("Extra answering port saved (PUT /api/reflector/ports)", http(PA, "GET", "/api/pi")["reflector_ports"] == [20500, 20501], "")
                pg.fill('[data-testid="thispi-ports-input"]', "0, abc"); pg.click('[data-testid="thispi-ports-save"]'); pg.wait_for_timeout(300)
                check("Bad port list gets a plain message", "port numbers" in pg.inner_text('[data-testid="thispi-ports-msg"]'), "")
                pg.fill('[data-testid="thispi-ports-input"]', "20500"); pg.click('[data-testid="thispi-ports-save"]'); pg.wait_for_timeout(300)
                # ---- add by IP
                pg.fill('[data-testid="peer-add-host"]', "127.0.0.1:9"); pg.click('[data-testid="peer-add-go"]')
                pg.wait_for_selector('[data-testid="peer-add-error"]:not([hidden])', timeout=15000)
                check("Add by IP: nothing there -> plain error shown", "Cannot reach" in pg.inner_text('[data-testid="peer-add-error"]'), pg.inner_text('[data-testid="peer-add-error"]'))
                pg.fill('[data-testid="peer-add-host"]', f"127.0.0.1:{PB}"); pg.click('[data-testid="peer-add-go"]')
                pg.wait_for_selector('[data-testid="peer-row"]', timeout=15000)
                row = pg.locator('[data-testid="peer-row"]').first
                txt = row.inner_text()
                check("Added Pi shows name, address, ✓ reachable and clock state", me_b["name"] in txt and f"127.0.0.1:{PB}" in txt and "✓ reachable" in txt and ("clock synced" in txt or "not synced" in txt), txt.replace("\n", " | ")[:300])
                check("Hand-added Pi has Remove, Open its page, Set password, Update it too, clock-source buttons",
                      all(row.locator(f'[data-testid="{t}"]').count() == 1 for t in ("peer-remove", "peer-open", "peer-password", "peer-update", "peer-clock-source")), "")
                check("Open its page = new tab to http://host:port", row.locator('[data-testid="peer-open"]').get_attribute("href") == f"http://127.0.0.1:{PB}" and row.locator('[data-testid="peer-open"]').get_attribute("target") == "_blank", "")
                pg.screenshot(path=os.path.join(SHOTS, "settings-peers-375-light.png"), full_page=False)
                # ---- password form
                row.locator('[data-testid="peer-password"]').click()
                row.locator('[data-testid="peer-password-input"]').fill("secret1"); row.locator('[data-testid="peer-password-save"]').click(); pg.wait_for_timeout(1500)
                pl = http(PA, "GET", "/api/peers")["peers"]
                check("Set password saves it for that Pi (POST /api/peers/{id}/password)", pl and pl[0]["has_password"] is True, json.dumps({k: pl[0].get(k) for k in ("has_password", "auth")}))
                # ---- time source
                ci = pg.inner_text('[data-testid="clock-info"]')
                check("Time source shows this Pi's clock, who it follows and whether it was applied", "This Pi's clock" in ci and "Follows" in ci and "Change applied" in ci, ci.replace("\n", " | ")[:300])
                pg.select_option('[data-testid="clock-source-select"]', "keep"); pg.click('[data-testid="clock-save"]'); pg.wait_for_timeout(800)
                check("Time source 'Leave as the installer set it' saved (PUT /api/clock/source)", http(PA, "GET", "/api/clock/source")["setting"] == "keep", "")
                opts = pg.locator('[data-testid="clock-source-select"] option').all_inner_texts()
                check("Time source lists Automatic, This Pi, the other Pi and Leave as installer", any("Automatic" in o for o in opts) and any(o.startswith("This Pi") for o in opts) and any(me_b["name"] in o for o in opts) and any("installer" in o for o in opts), opts)
                pg.select_option('[data-testid="clock-source-select"]', "auto"); pg.click('[data-testid="clock-save"]'); pg.wait_for_timeout(600)
                # ---- "Update it too" -> software update section
                row.locator('[data-testid="peer-update"]').click(); pg.wait_for_timeout(1500)
                sel = pg.locator('[data-testid="upd-other-select"]')
                check("'Update it too' opens 'Also update another Pi' with that Pi chosen", pg.locator('[data-testid="upd-other"]').get_attribute("open") is not None and me_b["name"] in sel.locator("option:checked").inner_text(), sel.locator("option:checked").inner_text())
                check("Software update says 'another Pi' (no partner / Pi B)", "Also update another Pi" in pg.inner_text('[data-testid="upd-other"]') and "partner" not in pg.inner_text('[data-testid="upd-other"]').lower(), "")
                pg.select_option('[data-testid="upd-other-select"]', "__addr")
                check("Typing an address stays possible", pg.locator('[data-testid="upd-other-host"]').is_visible(), "")
                # ---- remove
                row.locator('[data-testid="peer-remove"]').click(); pg.click("#mYes"); pg.wait_for_selector('[data-testid="peers-empty"]', timeout=8000)
                check("Remove a hand-added Pi", http(PA, "GET", "/api/peers")["peers"] == [], "")
                http(PA, "POST", "/api/peers", {"host": f"127.0.0.1:{PB}"})
            else:
                http(PA, "POST", "/api/peers", {"host": f"127.0.0.1:{PB}"}); pg.reload(); pg.wait_for_selector('[data-testid="peer-row"]', timeout=15000)
                pg.screenshot(path=os.path.join(SHOTS, "settings-peers-375-dark.png"))
            pg.wait_for_timeout(500)
            a = pg.evaluate(AUDIT, "#main")
            check(tag + "No sideways scroll at 375 px", a["sw"] <= a["vw"], f"scrollWidth={a['sw']} vw={a['vw']}")
            mine = lambda l: [x for x in l if any(k in x for k in ("thispi", "peer", "clock", "upd-other", "#pi", "#clk", "#updP"))]
            check(tag + "Every control of the new cards is >= 44 px", not mine(a["small"]), "; ".join(mine(a["small"])[:8]))
            check(tag + "Every input is >= 16 px (no iOS zoom)", not mine(a["fonts"]), "; ".join(a["fonts"][:8]))
            check(tag + "Every control of the new cards has a data-testid", not [x for x in a["noid"] if any(k in x for k in ("#pi", "#peer", "#clk", "#updP"))], "; ".join(a["noid"][:8]))
            check(tag + "No script errors", not errs, "; ".join(errs[:3]))
            ctx.close()
        # desktop width: layout holds
        ctx = br.new_context(viewport={"width": 1280, "height": 900}); pg = ctx.new_page()
        pg.goto(f"http://127.0.0.1:{PA}/#settings"); pg.wait_for_selector('[data-testid="peer-row"]', timeout=15000)
        pg.screenshot(path=os.path.join(SHOTS, "settings-peers-1280.png"))
        check("Desktop 1280 px: no sideways scroll", pg.evaluate("document.documentElement.scrollWidth <= innerWidth"), "")
        br.close()


try:
    os.makedirs(SHOTS, exist_ok=True)
    main()
except BaseException as ex:
    traceback.print_exc()
    check("e2e script ran to completion", False, f"{type(ex).__name__}: {ex}")
finally:
    for p in procs:
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: pass
    time.sleep(0.5)
    shutil.rmtree(TMP, ignore_errors=True)
    bad = [n for n, ok in results if not ok]
    print(f"# {len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""), flush=True)
    sys.exit(1 if bad else 0)
