#!/usr/bin/env python3
"""Instrument redesign phase 2 (generator grid) in a real browser, phone 390 px and desktop 1280 px: stream list with on/off switches, editor beside it
(a sheet on a phone), per-stream live counters, a stream switched off and on during the run, Clear while it runs, run until stopped.
Prints 'CHECK name | pass/FAIL | evidence'. Run: python3 scripts/e2e_genui.py   Screenshots -> docs/dev/ux-impl-shots/phase2/"""
import json, os, signal, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "docs", "dev", "ux-impl-shots", "phase2"); os.makedirs(SHOTS, exist_ok=True)
TMP = tempfile.mkdtemp(prefix="tt-genui-"); procs, results = [], []
A, B = 18680, 18681


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


def shot(pg, name):
    pg.screenshot(path=os.path.join(SHOTS, name + ".png"))


def txt(pg, sel):
    return pg.inner_text(sel).strip()


def cell(pg, row, col):
    return pg.locator(f"[data-testid=gen-row-{row}] td").nth(col).inner_text().strip()


def main():
    app("A", A); app("B", B)
    http(A, "PUT", "/api/pi", {"name": "pi1"}); http(B, "PUT", "/api/pi", {"name": "pi2"})
    http(A, "POST", "/api/peers", {"host": f"127.0.0.1:{B}"})
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for tag, w, h in (("desktop", 1280, 800), ("phone", 390, 844)):
            ctx = br.new_context(viewport={"width": w, "height": h}); pg = ctx.new_page(); errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "404" not in m.text else None)
            pg.goto(f"http://127.0.0.1:{A}/"); pg.wait_for_selector("[data-testid=home-links]", timeout=15000)
            pg.locator("[data-tab=generator]:visible").first.click(); pg.wait_for_selector("[data-testid=gen-list]", timeout=10000); time.sleep(0.8)
            # ---- idle: list, switches, editor
            nrows = pg.locator("[data-testid^=gen-row-]").count()
            check(f"[{tag}] the stream list is the screen: one row per stream with an on/off switch", nrows >= 1 and pg.locator("[data-testid^=gen-on-]").count() == nrows, nrows)
            check(f"[{tag}] no horizontal page scroll (idle)", pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
            if tag == "desktop":
                check("[desktop] the editor sits beside the list", pg.is_visible("[data-testid=gen-editor]") and pg.evaluate("document.querySelector('#gEdit').getBoundingClientRect().left > document.querySelector('[data-testid=gen-list]').getBoundingClientRect().right - 4"))
            else:
                check("[phone] the editor is hidden until a row is tapped", not pg.is_visible("[data-testid=gen-editor]"))
            # new stream from a chip
            pg.click("[data-testid=gen-add-stream]"); time.sleep(0.3)
            check(f"[{tag}] + Stream adds a row and opens its editor", pg.locator("[data-testid^=gen-row-]").count() == nrows + 1 and pg.is_visible("[data-testid=gen-editor]"))
            pg.click("[data-testid=gen-chips] [data-k=video]"); time.sleep(0.3)
            check(f"[{tag}] the Video chip fills the stream (name, 4 Mb/s, UDP 1358 B)", pg.input_value("[data-testid=gen-name]") == "Video" and pg.input_value("[data-testid=gen-rate]") == "4" and "UDP 1358 B" in pg.locator(f"[data-testid=gen-row-{nrows + 1}]").inner_text(), pg.locator(f"[data-testid=gen-row-{nrows + 1}]").inner_text())
            check(f"[{tag}] the editor shows the packet preview and the maths line", "ETH 14" in txt(pg, "[data-testid=gen-preview]") and "pkt/s" in txt(pg, "#gMath"), txt(pg, "#gMath"))
            pg.fill("[data-testid=gen-rate]", "3"); time.sleep(0.2)
            check(f"[{tag}] changing the rate updates the row at once", "3 Mb/s" in pg.locator(f"[data-testid=gen-row-{nrows + 1}]").inner_text())
            shot(pg, f"idle-{tag}")
            if tag == "phone": pg.click("[data-testid=gen-ed-close]"); time.sleep(0.2)
            # switch the new stream off: it is not sent
            pg.click(f"[data-testid=gen-on-{nrows + 1}]"); time.sleep(0.2)
            check(f"[{tag}] a switch turns a stream off (row greyed) without deleting it", pg.get_attribute(f"[data-testid=gen-on-{nrows + 1}]", "aria-checked") == "false" and "off" in (pg.get_attribute(f"[data-testid=gen-row-{nrows + 1}]", "class") or ""))
            pg.click(f"[data-testid=gen-on-{nrows + 1}]")
            # ---- until stopped
            pg.locator("[data-testid=gen-run-settings]").scroll_into_view_if_needed()
            pg.select_option("[data-testid=gen-length-mode]", "until"); time.sleep(0.3)
            check(f"[{tag}] test length can be 'Until stopped'", pg.input_value("[data-testid=gen-length-mode]") == "until" and pg.locator("[data-testid=gen-length]").count() == 0)
            # ---- run it with the transport bar
            pg.click("#tbStart"); pg.wait_for_selector("#gRows .swb", timeout=10000)
            t0 = time.time()
            while time.time() - t0 < 25:
                if pg.locator("[data-testid=gen-row-1] td").nth(2 if tag == "phone" else 6).inner_text().strip() not in ("", "–"): break
                time.sleep(0.5)
            time.sleep(1.5)
            check(f"[{tag}] START begins the run; the transport bar says RUNNING and the timer runs", txt(pg, "[data-testid=tb-word]") == "RUNNING" and txt(pg, "[data-testid=tb-time]") != "00:00:00", txt(pg, "[data-testid=tb-word]"))
            rx = cell(pg, 1, 2 if tag == "phone" else 12)
            check(f"[{tag}] every stream row shows live numbers (RX Mb/s {rx})", rx not in ("", "–") and float(rx) >= 0, rx)
            check(f"[{tag}] the sum row is there", "stream" in txt(pg, "#gRows tr.sum"), txt(pg, "#gRows tr.sum")[:60])
            check(f"[{tag}] no horizontal page scroll while running", pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
            check(f"[{tag}] Start became Stop; Clear and Mark are live", txt(pg, "#tbStart").upper().endswith("STOP") and not pg.is_disabled("#tbClear") and not pg.is_disabled("#tbMark"))
            pg.evaluate("window.scrollTo(0,0)"); time.sleep(0.3); shot(pg, f"running-{tag}")
            pg.locator("#gC1").scroll_into_view_if_needed(); time.sleep(0.3); shot(pg, f"running-charts-{tag}"); pg.evaluate("window.scrollTo(0,0)")
            # switch stream 1 off during the run: its TX counter freezes
            txcol = 2 if tag == "phone" else 7
            pg.click("[data-testid=gen-on-1]"); time.sleep(2.2)
            a = pg.evaluate("document.querySelector('[data-testid=gen-row-1]').innerText")
            time.sleep(2.0)
            b = pg.evaluate("document.querySelector('[data-testid=gen-row-1]').innerText")
            check(f"[{tag}] a stream switched off during the run stops (its row freezes and greys out)", pg.get_attribute("[data-testid=gen-on-1]", "aria-checked") == "false" and a == b, (a, b))
            pg.click("[data-testid=gen-on-1]"); time.sleep(2.5)
            c = pg.evaluate("document.querySelector('[data-testid=gen-row-1]').innerText")
            check(f"[{tag}] switched on again: the stream sends again", pg.get_attribute("[data-testid=gen-on-1]", "aria-checked") == "true" and c != b, (b, c))
            # Clear during the run
            pg.click("#tbClear"); time.sleep(2.0)
            check(f"[{tag}] Clear during the run zeroes the counters and the run goes on", "since clear" in txt(pg, "#gNote") and txt(pg, "[data-testid=tb-word]") == "RUNNING", txt(pg, "#gNote"))
            # open the editor of a running stream: locked
            pg.click("[data-testid=gen-row-1] .nm"); time.sleep(0.3)
            check(f"[{tag}] the editor of a running stream is locked", pg.evaluate("document.querySelector('#gEdit .ged').inert"))
            if tag == "phone": pg.click("[data-testid=gen-ed-close]")
            # Stop
            pg.click("#tbStart"); time.sleep(0.4)
            pg.click("#mYes")
            for _ in range(40):
                if txt(pg, "[data-testid=tb-word]") != "RUNNING": break
                time.sleep(0.5)
            time.sleep(1.2)
            check(f"[{tag}] Stop ends an until-stopped run; the result is judged", txt(pg, "[data-testid=tb-word]") == "DONE" and pg.locator("#liveBox .banner").count() == 1, txt(pg, "[data-testid=tb-word]"))
            check(f"[{tag}] after the run the list keeps the last numbers", cell(pg, 1, 2 if tag == "phone" else 12) not in ("", "–"), cell(pg, 1, 2 if tag == "phone" else 12))
            pg.evaluate("window.scrollTo(0,0)"); time.sleep(0.3); shot(pg, f"result-{tag}")
            check(f"[{tag}] no console errors / page errors", not errs, errs[:3])
            ctx.close()
        br.close()
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks pass", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for p in procs:
            try: os.killpg(p.pid, signal.SIGTERM)
            except Exception: pass
    sys.exit(rc)
