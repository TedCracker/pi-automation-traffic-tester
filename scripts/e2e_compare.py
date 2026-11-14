#!/usr/bin/env python3
"""Compare runs, baseline and job info end-to-end in a real Chromium (phone 390x844 and desktop), against one app with stored results (no test is run).
Results list: job line under the test name, Select mode with tick boxes, 'Pick results of the same kind.', Compare lights up with 2 to 4 of one kind.
Compare page: A / B chips, table with Change against A, the sentence, the overlaid chart, setup differences, Set A as baseline, PDF comparison (one PDF page).
Baseline: later runs of the same saved test show 'vs baseline' in the list and under the verdict. Job info: block on the result, edit sheet, the Start-bar line
widget (pre-filled from the last run, never blocks), CSV / JSON carry it. Prints 'CHECK name | pass/FAIL | evidence'. Run via scripts/e2e-compare.sh.
Screenshots: docs/ui-review/fable-1.1/compare-*.png, job-*.png"""
import json, os, re, subprocess, sys, tempfile, time, traceback, urllib.request, urllib.error
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "docs", "ui-review", "fable-1.1")
TMP = tempfile.mkdtemp(prefix="tt-compare-"); procs, results = [], []
PORT = 18470
SIZES = [("phone", 390, 844, True), ("desktop", 1280, 800, False)]


def check(name, ok, ev=""):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:400]}", flush=True)


def http(method, path, body=None, raw=False):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as f:
            d = f.read(); return d if raw else (json.loads(d) if d else None)
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "detail": e.read().decode()[:300]}


PY = os.environ.get("TT_PY") or os.path.join(ROOT, "venv/bin/python")          # the app's own Python (this script runs under the system one, which has Playwright)
SEED = r"""
import json, sys
from traffic_tester.store import Store
from _cmpfix import trip_run, gen_run, mb_run
st = Store(sys.argv[1]); ids = {}
JOB = {"site": "Utility · Site A", "by": "Alex"}
ids["bench"] = trip_run(st, name="DER trip", p50=2.1, p99=2.9, sent=12000)
st.set_job(ids["bench"], {**JOB, "link": "Bench cable"})
ids["g1"] = gen_run(st, name="Fill the link", rx=5.0, p99=20.0, secs=20)
ids["mb"] = mb_run(st)
ids["radio"] = trip_run(st, name="DER trip", p50=18.5, p99=41.2, lost=5, sent=12000)
st.set_job(ids["radio"], {**JOB, "link": "5G radio · -87 dBm", "note": "Antenna on the roof, light rain"})
ids["g2"] = gen_run(st, name="Fill the link", rx=8.0, loss=0.4, p99=31.0, secs=20)
ids["l1"] = trip_run(st, name="Two links", tester="pi1", refl="pi2", p99=20.0, gid=900)
ids["l2"] = trip_run(st, name="Two links", tester="pi1", refl="pi3", p99=70.0, gid=900)
ids["late"] = trip_run(st, name="DER trip", p50=21.0, p99=79.0, lost=30, sent=12000)
st.set_job(ids["late"], {**JOB, "link": "5G radio · -101 dBm"})
print(json.dumps(ids))
"""


def seed():
    """Stored results, as if the tests had been run: the same saved test on the bench and over the radio, two generator runs, a Mirrored Bits run, a two-link test."""
    env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.path.join(ROOT, "tests"), TT_NO_DEFAULT_REFLECTOR="1", TT_NO_DISCOVERY="1")
    out = subprocess.run([PY, "-c", SEED, f"{TMP}/A"], env=env, capture_output=True, text=True, cwd=ROOT)
    if out.returncode:
        raise RuntimeError("seeding failed: " + out.stderr[-400:])
    return json.loads(out.stdout.strip().splitlines()[-1])


def start():
    env = dict(os.environ, PYTHONPATH=ROOT, TT_ASSUME_SYNCED="1", TT_TEST_HOOKS="1", TT_NO_DISCOVERY="1", TT_NO_DEFAULT_REFLECTOR="1")
    p = subprocess.Popen([PY, "-m", "traffic_tester", "--port", str(PORT), "--host", "127.0.0.1", "--data-dir", f"{TMP}/A"],
                         env=env, stdout=open(f"{TMP}/A.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try:
            if isinstance(http("GET", "/api/status"), dict): return
        except Exception: time.sleep(0.25)
    raise RuntimeError("the app did not start")


def shot(pg, name):
    os.makedirs(SHOTS, exist_ok=True); pg.screenshot(path=os.path.join(SHOTS, f"{name}.png"), full_page=False)


def no_overflow(pg): return pg.evaluate("document.scrollingElement.scrollWidth <= window.innerWidth + 1")


def run_size(pw, ids, name, w, h, touch):
    br = pw.chromium.launch()
    cx = br.new_context(viewport={"width": w, "height": h}, has_touch=touch, is_mobile=touch, device_scale_factor=2 if touch else 1)
    pg = cx.new_page(); errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    T = lambda s: f"[{name}] {s}"
    row = lambda rid: pg.locator(f"tr[data-run='{rid}']")
    pg.goto(f"http://127.0.0.1:{PORT}/#results"); pg.wait_for_selector(".runlist tr[data-run]")

    # --- results list: job line, Select mode
    check(T("results list shows the job under the test name"), "Utility · Site A · 5G radio" in row(ids["radio"]).inner_text(), row(ids["radio"]).inner_text()[:120])
    check(T("no tick boxes before Select"), pg.locator("input[data-pick]").count() == 0 and pg.locator("[data-testid=results-select]").is_visible())
    pg.click("[data-testid=results-select]"); pg.wait_for_selector("[data-testid=results-selbar]")
    n_boxes = pg.locator("input[data-pick]").count()
    check(T("Select mode: a tick box on every result with numbers (the links of a test one by one)"), n_boxes == 8, n_boxes)
    hint = lambda: pg.inner_text("[data-testid=results-select-hint]")
    check(T("Select mode: says what to do, Compare is off"), "Tick 2 to 4 results of the same kind" in hint() and pg.locator("[data-testid=results-compare]").is_disabled(), hint())
    row(ids["bench"]).click()
    check(T("one picked: asks for one more, Compare still off"), "1 picked" in hint() and pg.locator("[data-testid=results-compare]").is_disabled(), hint())
    pg.locator(f"[data-testid=results-pick-{ids['g1']}]").check()
    check(T("mixed kinds: 'Pick results of the same kind.'"), hint() == "Pick results of the same kind." and pg.locator("[data-testid=results-compare]").is_disabled(), hint())
    pg.locator(f"[data-testid=results-pick-{ids['g1']}]").uncheck()
    row(ids["radio"]).click()
    check(T("two of one kind: Compare lights up"), "2 picked" in hint() and pg.locator("[data-testid=results-compare]").is_enabled(), hint())
    pg.evaluate("window.scrollTo(0, 0); document.querySelector('[data-testid=results-selbar]').scrollIntoView({block:'start'}); window.scrollBy(0, -70)"); shot(pg, f"compare-select-{name}-{w}x{h}")
    pg.click("[data-testid=results-compare]"); pg.wait_for_selector("[data-testid=compare-page]")

    # --- compare page
    pg.wait_for_selector("[data-testid=compare-chart] canvas")
    a, b = pg.inner_text("[data-testid=compare-run-A]"), pg.inner_text("[data-testid=compare-run-B]")
    check(T("compare: A and B chips with name, job and date"), "DER trip" in a and "Bench cable" in a and "5G radio" in b and ("Antenna on the roof" in b or "RSRP" in b) and re.search(r"\d{1,2}:\d\d", a), a[:140])
    p99 = pg.inner_text("[data-testid=compare-row-p99]")
    check(T("compare: table of headline numbers with the change against A"), "2.9" in p99 and "41.2" in p99 and "+38.3" in p99 and "worse" in p99 and pg.locator("[data-testid=compare-table] tbody tr").count() >= 7, p99)
    say = pg.inner_text("[data-testid=compare-sentence]")
    check(T("compare: one plain sentence"), "typical message took 16.4 ms longer in B than in A" in say and "38.3 ms longer" in say, say)
    legend = pg.inner_text("[data-testid=compare-chart] .u-legend")
    check(T("compare: one overlaid chart with a legend naming A and B"), pg.locator("[data-testid=compare-chart] canvas").count() == 1 and "A (" in legend and "B (" in legend, legend[:80])
    check(T("compare: setup differences in one line"), "The setup was the same in every result." in pg.inner_text("[data-testid=compare-setup]"), pg.inner_text("[data-testid=compare-setup]"))
    check(T("compare page fits the screen (no sideways scroll)"), no_overflow(pg))
    fits = pg.evaluate("(() => { const b = document.querySelector('[data-testid=compare-table]').closest('.scroll'); return b.scrollWidth <= b.clientWidth + 1; })()")
    check(T("compare: the two-result table shows every column without scrolling"), fits)
    pg.evaluate("window.scrollTo(0, 0)"); shot(pg, f"compare-{name}-{w}x{h}")
    pg.evaluate("document.querySelector('[data-testid=compare-chart]').closest('.card').scrollIntoView({block:'start'}); window.scrollBy(0, -70)"); shot(pg, f"compare-chart-{name}-{w}x{h}")
    href = pg.get_attribute("[data-testid=compare-pdf]", "href")
    pdf = http("GET", href, raw=True)
    check(T("PDF comparison is one PDF page"), isinstance(pdf, bytes) and pdf[:5] == b"%PDF-" and len(re.findall(rb"/Type\s*/Page(?!s)", pdf)) == 1, href)
    # --- baseline
    check(T("compare: 'Set A as baseline' offered"), pg.inner_text("[data-testid=compare-baseline]") == "Set A as baseline")
    pg.click("[data-testid=compare-baseline]"); pg.wait_for_function("document.querySelector('[data-testid=compare-baseline]')?.textContent.includes('Baseline: A ✓')")
    check(T("compare: A is now the baseline"), "A · Baseline" in pg.inner_text("[data-testid=compare-run-A]"))
    pg.evaluate("document.querySelector('[data-testid=compare-baseline]').scrollIntoView({block:'center'})"); shot(pg, f"compare-baseline-{name}-{w}x{h}")
    pg.click("[data-testid=compare-back]"); pg.wait_for_selector(".runlist tr[data-run]")
    check(T("back in the list: the baseline is tagged, later runs show 'vs baseline'"), "Baseline" in row(ids["bench"]).inner_text() and "vs baseline: p99 +38.3 ms" in row(ids["radio"]).inner_text()
          and "vs baseline: p99 +76.1 ms" in row(ids["late"]).inner_text() and "vs baseline" not in row(ids["g2"]).inner_text(), row(ids["radio"]).inner_text()[:160])
    row(ids["late"]).click(); pg.wait_for_selector("[data-testid=result-vs-baseline]")
    vs = pg.inner_text("[data-testid=result-vs-baseline]")
    check(T("opened result: 'vs baseline: p99 +76.1 ms' under the verdict"), "vs baseline: p99 +76.1 ms" in vs and "loss +0.25 %" in vs, vs)
    order = pg.evaluate("(() => { const b = document.querySelector('#detail .banner').getBoundingClientRect(), v = document.querySelector('[data-testid=result-vs-baseline]').getBoundingClientRect(); return v.top >= b.bottom - 1 && v.top - b.bottom < 40; })()")
    check(T("... directly under the PASS / FAIL banner"), order)
    pg.evaluate("document.querySelector('#detail').scrollIntoView(); window.scrollBy(0, -70)"); shot(pg, f"baseline-result-{name}-{w}x{h}")
    pg.click("[data-testid=result-compare-baseline]"); pg.wait_for_selector("[data-testid=compare-page]")
    check(T("'Compare with the baseline' opens the comparison with the baseline as A"), "A · Baseline" in pg.inner_text("[data-testid=compare-run-A]") and "79" in pg.inner_text("[data-testid=compare-row-p99]"))
    pg.click("[data-testid=compare-back]"); pg.wait_for_selector(".runlist tr[data-run]")

    # --- three results + generator + Mirrored Bits kinds
    pg.click("[data-testid=results-select]")
    for k in ("bench", "radio", "late"): pg.locator(f"[data-testid=results-pick-{ids[k]}]").check()
    pg.click("[data-testid=results-compare]"); pg.wait_for_selector("[data-testid=compare-run-C]")
    check(T("three results: A, B, C with the change under B and C"), pg.locator("[data-testid=compare-row-p99] .cmp-sub").count() == 2 and no_overflow(pg), pg.inner_text("[data-testid=compare-row-p99]"))
    pg.evaluate("window.scrollTo(0, 0)"); shot(pg, f"compare-three-{name}-{w}x{h}")
    pg.click("[data-testid=compare-back]"); pg.wait_for_selector(".runlist tr[data-run]")
    pg.click("[data-testid=results-select]")
    for k in ("g1", "g2"): pg.locator(f"[data-testid=results-pick-{ids[k]}]").check()
    pg.click("[data-testid=results-compare]"); pg.wait_for_selector("[data-testid=compare-chart] canvas")
    check(T("generator results: throughput and delay per second from 0 s, one chart each"), pg.locator("[data-testid=compare-chart] canvas").count() == 1 and pg.locator("[data-testid=compare-chart-delay] canvas").count() == 1
          and "B carried 3 Mb/s more than A" in pg.inner_text("[data-testid=compare-sentence]"), pg.inner_text("[data-testid=compare-sentence]"))
    pg.evaluate("document.querySelector('[data-testid=compare-chart]').scrollIntoView({block:'start'})"); shot(pg, f"compare-generator-{name}-{w}x{h}")
    pg.click("[data-testid=compare-back]"); pg.wait_for_selector(".runlist tr[data-run]")

    # --- job info on a result
    row(ids["radio"]).click(); pg.wait_for_selector("[data-testid=result-job-note]")
    job = pg.inner_text("[data-testid=result-job]")
    check(T("result: job block with Site, Link / radio, By, Note"), all(x in job for x in ("Site", "Utility · Site A", "Link / radio", "5G radio · -87 dBm", "By", "Alex", "Note")) and ("Antenna on the roof" in job or "RSRP" in job), job)
    pg.evaluate("document.querySelector('#detail').scrollIntoView(); window.scrollBy(0, -70)"); shot(pg, f"job-result-{name}-{w}x{h}")
    pg.click("[data-testid=result-job-edit]"); pg.wait_for_selector("[data-testid=job-sheet]")
    check(T("job sheet: four fields, pre-filled"), pg.input_value("[data-testid=job-site]") == "Utility · Site A" and pg.locator("[data-testid=job-sheet] [data-job]").count() == 4)
    shot(pg, f"job-sheet-{name}-{w}x{h}")
    note = f"RSRP -95 dBm ({name})"
    pg.fill("[data-testid=job-note]", note); pg.click("[data-testid=sheet-job-save]")
    pg.wait_for_function(f"document.querySelector('[data-testid=result-job-note]')?.textContent === {json.dumps(note)}")
    check(T("job edited on the result afterwards: stored"), http("GET", f"/api/runs/{ids['radio']}")["job"]["note"] == note)
    csv = http("GET", f"/api/runs/{ids['radio']}/export.csv", raw=True).decode()
    check(T("CSV and JSON carry the job"), "site,link_radio,by,note" in csv.splitlines()[0] and note in csv.splitlines()[1] and note in http("GET", f"/api/runs/{ids['radio']}/export.json", raw=True).decode())
    row(ids["g1"]).click(); pg.wait_for_selector(".jobcard.empty-job")
    check(T("a result without job info offers to add it, quietly"), "Add job info" in pg.inner_text("[data-testid=result-job-edit]") and "No job info" in pg.inner_text("[data-testid=result-job]"))

    # --- the Start-bar job line (the widget the New test page mounts)
    pg.evaluate("""async () => { const { Job } = await import('/job.js'); const d = document.createElement('div'); d.id = 'jobMount'; d.style.cssText = 'position:fixed;left:8px;right:8px;bottom:70px;z-index:50;background:var(--panel);padding:6px;border-radius:10px';
        document.body.appendChild(d); window._Job = Job; window._jobChanged = 0; Job.line(d, { onChange: () => window._jobChanged++ }); }""")
    pg.wait_for_function("document.querySelector('#jobMount [data-testid=job-line]')?.textContent.includes('Utility')")
    line = pg.inner_text("#jobMount [data-testid=job-line]")
    check(T("Start-bar job line: pre-filled from the last run on this Pi (without its note)"), "Job" in line and "Utility · Site A · 5G radio · -101 dBm · by Alex" in line and "note" not in pg.evaluate("Object.keys(window._Job.value())"), line)
    shot(pg, f"job-line-{name}-{w}x{h}")
    pg.click("#jobMount [data-testid=job-line]"); pg.wait_for_selector("[data-testid=job-sheet]")
    pg.fill("[data-testid=job-link]", "5G radio · new antenna"); pg.fill("[data-testid=job-note]", "after the move"); pg.click("[data-testid=sheet-job-save]")
    v = pg.evaluate("window._Job.value()")
    check(T("Start-bar job line: edited in the sheet, Job.value() has it, onChange fired"), v.get("link") == "5G radio · new antenna" and v.get("note") == "after the move" and pg.evaluate("window._jobChanged") == 1
          and "new antenna" in pg.inner_text("#jobMount [data-testid=job-line]"), v)
    pg.evaluate("window._Job.started()")
    check(T("after a start the note is cleared, the rest stays for the next test"), pg.evaluate("window._Job.value()") == {k: x for k, x in v.items() if k != "note"})
    pg.click("#jobMount [data-testid=job-line]"); pg.wait_for_selector("[data-testid=job-sheet]"); pg.click("[data-testid=sheet-job-clear]")
    check(T("Clear empties the job; the line then invites, never blocks"), pg.evaluate("window._Job.value()") == {} and "optional" in pg.inner_text("#jobMount [data-testid=job-line]"))
    check(T("no JavaScript errors"), not errs, errs[:3])
    br.close()


def main():
    try:
        ids = seed(); start()
        with sync_playwright() as pw:
            for s in SIZES:
                try: run_size(pw, ids, *s)
                except Exception as e:
                    traceback.print_exc(); check(f"[{s[0]}] ran to the end", False, repr(e)[:300])
                http("DELETE", f"/api/runs/{ids['bench']}/baseline")                       # each size starts without a baseline
        # API: the job goes in with POST /api/runs (the New test page sends Job.value())
        r = http("POST", "/api/runs", {"profile": {"name": "Job API", "transport": "udp", "dest": {"host": "127.0.0.1"}, "net": {"peer_host": "127.0.0.1", "port": 47912}, "duration_s": 1, "timeout_ms": 200},
                                       "job": {"site": "API site", "by": "e2e"}})
        ok = isinstance(r, dict) and r.get("run_id") and http("GET", f"/api/runs/{r['run_id']}")["job"] == {"site": "API site", "by": "e2e"}
        check("POST /api/runs stores the job with the run", ok, r)
        if ok: http("POST", f"/api/runs/{r['run_id']}/stop")
    finally:
        for p in procs:
            try: os.killpg(p.pid, 15)
            except Exception: pass
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
