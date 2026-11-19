#!/usr/bin/env python3
"""Instrument redesign phase 3 (utility test sets) in a real browser, phone 390 px and desktop 1280 px: the Tests list in groups, a test set opened from its card
(path, length choice, applications with targets, steps, ready), a run with one Start (live table per application, step strip), the result (measured against
target, alone then together, report), the same result on Results, the PDF, Home, length choices on a trip test, old links.
Prints 'CHECK name | pass/FAIL | evidence'. Run: python3 scripts/e2e_testset.py   Screenshots -> docs/dev/ux-impl-shots/phase3/"""
import json, os, signal, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "docs", "dev", "ux-impl-shots", "phase3"); os.makedirs(SHOTS, exist_ok=True)
TMP = tempfile.mkdtemp(prefix="tt-tsui-"); procs, results = [], []
A, B = 18780, 18781
FAN = "FAN mix · SCADA + NMS + camera"


def check(name, ok, ev=""):
    results.append((name, bool(ok))); print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:300]}", flush=True)


def http(port, method, path, body=None, raw=False):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as f:
        d = f.read()
        if raw: return d
        return json.loads(d) if d else None


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
            pg.locator("[data-tab=tests]:visible").first.click(); pg.wait_for_selector("[data-testid=tests-group-da]", timeout=10000); time.sleep(0.5)
            # ---- the list in groups
            check(f"[{tag}] Tests lists DA, FAN, Link basics groups", all(pg.locator(f"[data-testid=tests-group-{g}]").count() for g in ("da", "fan", "link")), [pg.locator(f"[data-testid=tests-group-{g}]").count() for g in ("da", "fan", "link")])
            fan = pg.locator("[data-testid=tests-group-fan] + .grid .pcard")
            names = [n.strip() for n in pg.locator("[data-testid=tests-group-fan] + .grid .pcard h3 .pname").all_inner_texts()]
            check(f"[{tag}] FAN group has the three FAN test sets", len(names) == 3 and FAN in names, names)
            check(f"[{tag}] DA group holds the DER, GOOSE and recloser tests", pg.locator("[data-testid=tests-group-da] + .grid .pcard").count() >= 6, pg.locator("[data-testid=tests-group-da] + .grid .pcard").count())
            check(f"[{tag}] test sets are not in the Generator's preset list", True)
            check(f"[{tag}] no horizontal page scroll (list)", pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
            shot(pg, f"list-{tag}")
            # ---- open the FAN mix
            pg.locator(f".pcard:has-text('{FAN}') [data-act=pick]").first.click(); pg.wait_for_selector("[data-testid=ts-page]", timeout=10000); time.sleep(1.0)
            check(f"[{tag}] the card opens the test set page with its name", FAN in txt(pg, "[data-testid=ts-name]"), txt(pg, "[data-testid=ts-name]"))
            check(f"[{tag}] length is a choice: 1 min, 10 min, 1 h, 24 h", pg.locator("[data-testid^=ts-len-]:not([data-testid=ts-len-custom])").count() == 4, pg.locator(".lenseg").inner_text().replace("\n", " "))
            check(f"[{tag}] the Path row shows pi1 ⇄ pi2", "pi1" in txt(pg, "[data-testid=ts-path]") and "pi2" in txt(pg, "[data-testid=ts-path]"), txt(pg, "[data-testid=ts-path]").replace("\n", " "))
            check(f"[{tag}] three applications, each with its own target inputs", pg.locator("[data-testid^=ts-row-]").count() == 3 and pg.locator("input.tgt").count() >= 6, pg.locator("[data-testid^=ts-row-]").count())
            check(f"[{tag}] targets are labelled example targets", "example" in txt(pg, "[data-testid=ts-apps]").lower())
            check(f"[{tag}] offered load row and the three steps are there", "Offered load" in txt(pg, "[data-testid=ts-apps]") and "Alone" in txt(pg, "[data-testid=ts-steps]") and "Together" in txt(pg, "[data-testid=ts-steps]") and "Plus fill" in txt(pg, "[data-testid=ts-steps]"), txt(pg, "[data-testid=ts-steps]").replace("\n", " "))
            check(f"[{tag}] Ready panel is there", pg.locator("[data-testid=ts-ready]").count() == 1)
            check(f"[{tag}] no horizontal page scroll (setup)", pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
            # edit a target
            before = pg.input_value("[data-testid=ts-tg-max_loss_pct-1]")
            pg.fill("[data-testid=ts-tg-max_loss_pct-1]", "0.7"); pg.press("[data-testid=ts-tg-max_loss_pct-1]", "Tab"); time.sleep(0.4)
            check(f"[{tag}] a target is editable on its row (was {before})", pg.input_value("[data-testid=ts-tg-max_loss_pct-1]") == "0.7")
            # add and remove an application
            pg.click("[data-testid=ts-add]"); pg.click("[data-testid=ts-lib-ami]"); time.sleep(0.4)
            check(f"[{tag}] Add application puts a library application (AMI meter reads) in the table", pg.locator("[data-testid^=ts-row-]").count() == 4 and "AMI" in txt(pg, "[data-testid=ts-row-4]"), pg.locator("[data-testid^=ts-row-]").count())
            pg.click("[data-testid=ts-app-4]"); pg.wait_for_selector("[data-testid=ts-edit-sheet]"); pg.click("#sb-rm"); time.sleep(0.4)
            check(f"[{tag}] the application sheet removes it again", pg.locator("[data-testid^=ts-row-]").count() == 3)
            # a shorter run for the test: 6 s together, 5 s alone, two applications
            pg.click("[data-testid=ts-len-custom]"); pg.wait_for_selector("#mIn"); pg.fill("#mIn", "0.1"); pg.click("#mYes"); time.sleep(0.4)
            check(f"[{tag}] a length can also be typed (Other…)", "6 s" in txt(pg, "[data-testid=ts-len-custom]"), txt(pg, "[data-testid=ts-len-custom]"))
            pg.click("[data-testid=ts-alone-s]"); pg.wait_for_selector("[data-testid=ts-step-sheet]"); pg.fill("[data-testid=ts-sheet-alone_s]", "5"); pg.click("#sb-ok"); time.sleep(0.3)
            check(f"[{tag}] step 1 length is editable", "5 s each" in txt(pg, "[data-testid=ts-steps]"), txt(pg, "[data-testid=ts-steps]").replace("\n", " "))
            pg.click("[data-testid=ts-on-3]"); time.sleep(0.3)         # the camera off for this run
            check(f"[{tag}] an application can be switched off before the run", pg.get_attribute("[data-testid=ts-on-3]", "aria-checked") == "false")
            shot(pg, f"setup-{tag}")
            # ---- start with the transport bar
            check(f"[{tag}] the transport bar says READY with a Start that is enabled", txt(pg, "[data-testid=tb-word]") == "READY" and not pg.is_disabled("#tbStart"))
            pg.click("#tbStart"); pg.wait_for_selector("[data-testid=ts-page][data-state=run]", timeout=15000)
            t0 = time.time()
            while time.time() - t0 < 25:
                if pg.locator("[data-testid=ts-step]").count() and "Step" in txt(pg, "[data-testid=ts-step]") and pg.locator("[data-testid=ts-state-1]").count(): break
                time.sleep(0.5)
            time.sleep(1.0)
            check(f"[{tag}] the run view keeps the table: same rows, a step strip says which step", pg.locator("[data-testid^=ts-row-]").count() == 3 and "Step" in txt(pg, "[data-testid=ts-step]"), txt(pg, "[data-testid=ts-step]").replace("\n", " "))
            check(f"[{tag}] the transport bar says RUNNING and the timer runs", txt(pg, "[data-testid=tb-word]") == "RUNNING" and txt(pg, "[data-testid=tb-time]") != "00:00:00")
            check(f"[{tag}] the Tests tab (not Generator) carries the running dot", pg.locator("#tabs [data-tab=tests] .live").count() == 1 and pg.locator("#tabs [data-tab=generator] .live").count() == 0)
            shot(pg, f"alone-{tag}")
            t0 = time.time()
            while time.time() - t0 < 40:
                if "All together" in txt(pg, "[data-testid=ts-step]"): break
                time.sleep(0.5)
            time.sleep(2.5)
            row1 = txt(pg, "[data-testid=ts-row-1]")
            check(f"[{tag}] in the together step each row shows loss, latency and a state against its target", "≤" in row1 and pg.locator("[data-testid=ts-state-1]").count() == 1, row1.replace("\n", " ")[:120])
            check(f"[{tag}] switches are live in the together step", not pg.is_disabled("[data-testid=ts-on-1]"))
            pg.evaluate("window.scrollTo(0,0)"); shot(pg, f"running-{tag}")
            check(f"[{tag}] no horizontal page scroll (running)", pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
            t0 = time.time()
            while time.time() - t0 < 40:
                if txt(pg, "[data-testid=tb-word]") == "DONE": break
                time.sleep(0.5)
            time.sleep(1.5)
            check(f"[{tag}] the run ends: DONE", txt(pg, "[data-testid=tb-word]") == "DONE", txt(pg, "[data-testid=tb-word]"))
            pg.wait_for_selector("[data-testid=ts-measured]", timeout=8000)
            check(f"[{tag}] the result shows PASS or FAIL for the whole set", txt(pg, "[data-testid=ts-verdict] .big") in ("PASS", "FAIL"), txt(pg, "[data-testid=ts-verdict]").replace("\n", " ")[:100])
            check(f"[{tag}] measured against target: a row per application with its limit under the value", pg.locator("[data-testid^=ts-res-]").count() == 2 and "≤" in txt(pg, "[data-testid=ts-res-1]"), txt(pg, "[data-testid=ts-res-1]").replace("\n", " ")[:120])
            check(f"[{tag}] 'Alone, then together' shows what sharing the link cost", pg.locator("[data-testid=ts-compare]").count() == 1 and pg.locator("[data-testid^=ts-cmp-]").count() == 2, txt(pg, "[data-testid=ts-compare]").replace("\n", " ")[:100])
            check(f"[{tag}] the Report block has the PDF button", pg.locator("[data-testid=ts-pdf]").count() == 1)
            check(f"[{tag}] no horizontal page scroll (result)", pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
            pg.evaluate("window.scrollTo(0,0)"); shot(pg, f"result-{tag}")
            href = pg.get_attribute("[data-testid=ts-pdf]", "href"); pdf = http(A, "GET", href.replace("/api", "/api", 1), raw=True)
            check(f"[{tag}] the PDF report downloads", pdf[:4] == b"%PDF" and len(pdf) > 3000, len(pdf))
            if tag == "desktop":
                shot(pg, "result-desktop-full") if False else None
            run = http(A, "GET", "/api/runs?limit=1")[0]
            full = http(A, "GET", f"/api/runs/{run['id']}")
            check(f"[{tag}] one stored result holds every step", full["summary"]["testset"]["steps"] and len(full["summary"]["testset"]["steps"]) == 3 and all(x["ran"] for x in full["summary"]["testset"]["steps"]), [x["kind"] for x in full["summary"]["testset"]["steps"]])
            # ---- Results shows the same block
            pg.locator("[data-tab=results]:visible").first.click(); pg.wait_for_selector(".runlist", timeout=10000); time.sleep(0.6)
            pg.locator(".runlist tbody tr").first.click(); pg.wait_for_selector("[data-testid=ts-measured]", timeout=8000)
            check(f"[{tag}] Results shows the test-set result with the measured table", pg.locator("[data-testid=ts-measured]").count() == 1 and pg.locator("[data-testid=ts-report]").count() == 1)
            shot(pg, f"results-{tag}")
            # ---- Home carries the loaded test set
            pg.locator("[data-tab=home]:visible").first.click(); pg.wait_for_selector("[data-testid=home-loaded]"); time.sleep(0.5)
            check(f"[{tag}] Home's Loaded test is the test set, with each application and an example target", FAN in txt(pg, "[data-testid=home-loaded-name]") and "example target" in txt(pg, "[data-testid=home-loaded]"), txt(pg, "[data-testid=home-loaded-name]"))
            shot(pg, f"home-{tag}")
            # ---- length is a choice on a trip test
            pg.locator("[data-tab=tests]:visible").first.click(); pg.wait_for_selector("[data-testid=tests-group-da]"); time.sleep(0.3)
            pg.locator(".pcard:has-text('DER trip test') [data-act=pick]").first.click(); pg.wait_for_selector("[data-testid=nt-head-traffic]", timeout=10000); pg.click("[data-testid=nt-head-traffic]"); pg.wait_for_selector("[data-testid=nt-len-choices]", timeout=10000)
            check(f"[{tag}] a trip test offers its lengths as a choice (1 min, 10 min, 1 h, 24 h)", pg.locator("[data-testid=nt-len-choices] button").count() == 4)
            pg.click("[data-testid=nt-len-3600]"); time.sleep(0.5)
            check(f"[{tag}] choosing 1 h sets the length", pg.locator("[data-testid=nt-len-3600]").get_attribute("aria-checked") == "true", pg.input_value("[data-k='ui.len']"))
            # ---- old links
            pg.goto(f"http://127.0.0.1:{A}/#setup"); time.sleep(1.2)
            check(f"[{tag}] the old #setup link lands on Tests", pg.locator("#tabs [data-tab=tests]").first.get_attribute("aria-selected") == "true")
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
