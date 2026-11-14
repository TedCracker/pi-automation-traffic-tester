#!/usr/bin/env python3
"""New-test page end-to-end (real Chromium, three Pis on this PC): phone (390x844) and desktop (1280x800).
Setup list -> ＋ New test -> Port / Protocol / From -> To / Traffic -> Start: 1->1 round trip, 1->1 one way, 1->2 (one table of links),
⚑ Mark + Since mark / Whole test, tester lamp, result links table + marks line, Save as a test, ▶ Run from its card, Swap, no "two Pis" wording.
Prints 'CHECK name | pass/FAIL | evidence'. Run via scripts/e2e-newtest.sh. Screenshots: docs/ui-review/fable-1.1/after/newtest-*.png
Same host: every app runs without the shared reflector on UDP 20500 (TT_NO_DEFAULT_REFLECTOR), so each test asks its far Pis to open
their own port (Test port under More options; per far Pi for the 1->2 case). Real Pis all answer on 20500."""
import json, os, shutil, subprocess, sys, tempfile, time, traceback, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "docs/ui-review/fable-1.1/after")
TMP = tempfile.mkdtemp(prefix="tt-newtest-")
PORTS = {"pi1": 18281, "pi2": 18282, "pi3": 18283}
SIZES = [("phone", 390, 844), ("desktop", 1280, 800)]
RESULTS, PROCS = [], []


def check(name, ok, ev=""):
    RESULTS.append(bool(ok)); print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:220]}", flush=True)


def http(port, method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=15) as f:
        return json.loads(f.read() or b"null")


def start(name, port):
    env = dict(os.environ, PYTHONPATH=ROOT, TT_ASSUME_SYNCED="1", TT_TEST_HOOKS="1", TT_NO_DISCOVERY="1", TT_NO_DEFAULT_REFLECTOR="1")
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{name}"],
                         env=env, stdout=open(f"{TMP}/{name}.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
    PROCS.append(p)
    for _ in range(80):
        try: http(port, "GET", "/api/status"); break
        except Exception: time.sleep(0.25)
    http(port, "PUT", "/api/pi", {"name": name})


def wait_done(port, secs=40):
    t0 = time.time()
    while time.time() - t0 < secs:
        if not http(port, "GET", "/api/status")["run"]["running"]: return True
        time.sleep(0.5)
    return False


def open_new(pg):
    pg.goto(f"http://127.0.0.1:{PORTS['pi1']}/#setup"); pg.wait_for_selector("[data-testid=setup-create-custom-test]")
    pg.click("[data-testid=setup-create-custom-test]"); pg.wait_for_selector("[data-testid=nt-page]"); pg.wait_for_timeout(600)


def traffic(pg, secs, port=None, rate_ms=20):
    pg.click("[data-testid=nt-head-traffic]"); pg.wait_for_timeout(200)
    pg.select_option("[data-k='ui.lenUnit']", "seconds"); pg.fill("[data-k='ui.len']", str(secs))
    pg.fill("[data-k='ui.rate']", str(rate_ms)); pg.wait_for_timeout(200)
    if port:
        pg.click("[data-testid=nt-more] summary"); pg.fill("[data-k='net.port']", str(port)); pg.wait_for_timeout(200)


def ready(pg, secs=15):
    pg.wait_for_function("() => { const b = document.querySelector('[data-testid=nt-start]'); return b && !b.disabled; }", timeout=secs * 1000)


def run_size(pw, label, w, h):
    b = pw.chromium.launch(); pg = b.new_page(viewport={"width": w, "height": h})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    shot = lambda n: pg.screenshot(path=os.path.join(SHOTS, f"newtest-{label}-{n}.png"), full_page=True)
    base = 18700 + (0 if label == "phone" else 50)

    # -- the list
    pg.goto(f"http://127.0.0.1:{PORTS['pi1']}/#setup"); pg.wait_for_selector(".pcard")
    check(f"{label}: Setup is the list of tests with ＋ New test", pg.locator(".pcard").count() > 0 and pg.locator("[data-testid=setup-create-custom-test]").is_visible(), pg.locator("h1").first.inner_text())
    check(f"{label}: no numbered wizard on Setup", pg.locator("#stepper").count() == 0)
    shot("list")

    # -- 1 -> 1 round trip
    open_new(pg)
    check(f"{label}: rows 1-3 open, Traffic closed", all(pg.locator(f"[data-testid=nt-row-{r}].open").count() == 1 for r in ("port", "proto", "ends")) and pg.locator("[data-testid=nt-row-traffic].open").count() == 0)
    check(f"{label}: port row offers Automatic", pg.locator("[data-testid=nt-port-auto]").count() == 1)
    check(f"{label}: protocol chips UDP / TCP", pg.locator("[data-testid=nt-proto-udp]").count() == 1 and pg.locator("[data-testid=nt-proto-tcp]").count() == 1)
    check(f"{label}: both far Pis listed as tick boxes", pg.locator("[data-testid=nt-far-pi2]").get_attribute("type") == "checkbox" and pg.locator("[data-testid=nt-far-pi3]").count() == 1)
    pg.check("[data-testid=nt-far-pi2]"); traffic(pg, 5, base + 1)
    ready(pg)
    sent = pg.inner_text("[data-testid=nt-sentence]") if pg.locator("[data-testid=nt-sentence]").count() else pg.inner_text("[data-testid=nt-hint]")
    check(f"{label}: sentence says pi1 sends to pi2, round trip", "pi1 sends to pi2" in sent and "round trip" in sent, sent)
    body = pg.inner_text("[data-testid=nt-page]")
    check(f"{label}: no 'two Pis' wording", "two Pis" not in body and "2 Pis" not in body)
    shot("new-1to1")
    pg.click("[data-testid=nt-start]"); pg.wait_for_selector("#runBanner"); pg.wait_for_timeout(2000)
    pg.click("[data-testid=run-mark]"); pg.fill("[data-testid=run-mark-name]", "antenna moved"); pg.click("[data-testid=sheet-go]"); pg.wait_for_timeout(1500)
    check(f"{label}: ⚑ Mark gives Since mark / Whole test", pg.locator("[data-testid=run-since-mark]").count() == 1 and pg.locator("[data-testid=run-since-whole]").count() == 1)
    check(f"{label}: tester lamp line shown", pg.locator("[data-testid=run-lamp] .tlamp").count() == 1, pg.inner_text("[data-testid=run-lamp]"))
    shot("run-1to1")
    wait_done(PORTS["pi1"]); pg.wait_for_timeout(1500)
    ban = pg.inner_text("#runBanner")
    check(f"{label}: 1->1 round trip PASS", "PASS" in ban, ban)
    pg.click("#seeRes"); pg.wait_for_selector("[data-testid=result-header]"); pg.wait_for_timeout(800)
    check(f"{label}: result shows the mark and the tester line", "antenna moved" in pg.inner_text("[data-testid=result-marks]") and pg.locator("[data-testid=result-tester]").count() == 1)
    shot("result-1to1")

    # -- 1 -> 1 one way
    open_new(pg); pg.check("[data-testid=nt-far-pi2]"); pg.click("[data-testid=nt-dir-one_way]"); traffic(pg, 4, base + 2); ready(pg)
    check(f"{label}: one way sentence", "one way" in pg.inner_text("[data-testid=nt-sentence]"))
    pg.click("[data-testid=nt-start]"); pg.wait_for_selector("#runBanner"); wait_done(PORTS["pi1"]); pg.wait_for_timeout(1500)
    ban = pg.inner_text("#runBanner")
    check(f"{label}: 1->1 one way PASS", "PASS" in ban, ban)

    # -- 1 -> 2
    open_new(pg); pg.check("[data-testid=nt-far-pi2]"); pg.check("[data-testid=nt-far-pi3]"); traffic(pg, 5)
    pg.evaluate("async ([a, b]) => { const m = await import('/newtest.js'); const p = m.ntState.c.p; p.dest.port = a; p.dests[0].port = b; }", [base + 3, base + 4])
    pg.click("[data-testid=nt-head-ends]"); pg.wait_for_timeout(300); pg.click("[data-testid=nt-head-ends]"); pg.wait_for_timeout(300)
    pg.evaluate("async ([a, b]) => { const m = await import('/newtest.js'); const p = m.ntState.c.p; p.dest.port = a; p.dests[0].port = b; }", [base + 3, base + 4])
    ready(pg)
    s = pg.inner_text("[data-testid=nt-sentence]")
    check(f"{label}: 1->2 sentence names both and 2 links", "pi2 and pi3" in s and "2 links" in s, s)
    shot("new-1to2")
    pg.click("[data-testid=nt-start]"); pg.wait_for_selector("[data-testid=run-links]", timeout=15000); pg.wait_for_timeout(2500)
    rows = pg.locator("[data-testid=run-links] tr[data-leg]").count()
    check(f"{label}: Run shows ONE table with 2 links", rows == 2 and pg.locator("[data-testid=run-links]").count() == 1, pg.inner_text("[data-testid=run-links-banner]"))
    pg.click("[data-testid=run-links] tr[data-leg='1']"); pg.wait_for_timeout(800)
    check(f"{label}: tap a link row shows its tiles", pg.locator("[data-testid=run-leg-tiles]").count() == 1)
    shot("run-1to2")
    wait_done(PORTS["pi1"]); pg.wait_for_timeout(1500)
    ban = pg.inner_text("#runBanner")
    check(f"{label}: 1->2 PASS on both links", "PASS" in ban and "2 links" in ban, ban)
    pg.click("#seeRes"); pg.wait_for_selector("[data-testid=result-links]"); pg.wait_for_timeout(600)
    check(f"{label}: result starts with the links table", pg.locator("[data-testid=result-links] tbody tr").count() == 2)
    shot("result-1to2")

    # -- Save, then ▶ Run from its card; Swap
    open_new(pg)
    check(f"{label}: the Pi used last is ticked already", pg.is_checked("[data-testid=nt-far-pi2]"))
    pg.uncheck("[data-testid=nt-far-pi2]"); pg.check("[data-testid=nt-far-pi3]"); traffic(pg, 3, base + 5); pg.wait_for_timeout(800)
    name = f"Site test {label}"
    pg.click("[data-testid=nt-save]"); pg.fill("[data-testid=nt-save-name]", name); pg.click("[data-testid=sheet-go]"); pg.wait_for_timeout(1200)
    card = pg.locator(".pcard", has_text=name)
    check(f"{label}: saved test is in the list", card.count() == 1)
    card.locator("[data-act=pick]").click(); pg.wait_for_selector("[data-testid=nt-page]"); pg.wait_for_timeout(800)
    line = pg.inner_text("[data-testid=nt-line-ends]")
    check(f"{label}: ▶ Run opens it with rows closed to one line", pg.locator(".ntrow.open").count() == 0 and "pi3" in line, line)
    pg.click("[data-testid=nt-head-ends]"); pg.click("[data-testid=nt-swap]"); pg.wait_for_timeout(500)
    s = pg.inner_text("[data-testid=nt-sentence]")
    check(f"{label}: Swap: the far Pi sends to this Pi", "pi3 sends to pi1" in s, s)
    pg.click("[data-testid=nt-cancel]"); pg.wait_for_timeout(300)
    if pg.locator("#modal:not([hidden]) .btn.danger, #modal:not([hidden]) #mYes").count(): pg.locator("#modal:not([hidden]) button").last.click(); pg.wait_for_timeout(300)
    check(f"{label}: ← Tests goes back to the list", pg.locator(".pcard").count() > 0)
    check(f"{label}: no page errors", not errs, errs)
    b.close()


def main():
    os.makedirs(SHOTS, exist_ok=True)
    try:
        for n, p in PORTS.items(): start(n, p)
        for n in ("pi2", "pi3"): http(PORTS["pi1"], "POST", "/api/peers", {"host": f"127.0.0.1:{PORTS[n]}"})
        with sync_playwright() as pw:
            for label, w, h in SIZES:
                try: run_size(pw, label, w, h)
                except Exception as e: traceback.print_exc(); check(f"{label}: ran to the end", False, e)
    finally:
        for p in PROCS:
            try: os.killpg(p.pid, 15)
            except Exception: pass
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"RESULT {sum(RESULTS)}/{len(RESULTS)} passed", flush=True)
    return 0 if RESULTS and all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
