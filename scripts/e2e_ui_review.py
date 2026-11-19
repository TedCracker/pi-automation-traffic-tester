#!/usr/bin/env python3
"""2026-11 review: the streamlined UI in a real browser (pi1 + pi2 on loopback UDP; pi2's always-on reflector answers). Proves the short paths (pick -> start, read the
result, export, run again) and the UI bugs fixed in the review. Run with system python3 (playwright): scripts/e2e-ui-review.sh
Output: CHECK lines + a count; exit 1 on any failure."""
import json, os, signal, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="tt-uirev-"); procs = []
A, B, UDP = 18220, 18221, 20500
results = []


def check(name, ok, evidence=""):
    results.append(bool(ok)); print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(evidence)[:300]}", flush=True)


def http(port, method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as f:
        d = f.read(); return json.loads(d) if d else None


def app(role, port):
    env = dict(os.environ, TT_TEST_HOOKS="1", TT_ASSUME_SYNCED="1", PYTHONPATH=ROOT, TT_NO_DISCOVERY="1")
    if role == "A": env["TT_NO_DEFAULT_REFLECTOR"] = "1"          # two apps in one network namespace: only pi2 answers on UDP/TCP 20500
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{role}"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try: http(port, "GET", "/api/status"); return
        except Exception: time.sleep(0.25)
    raise RuntimeError("app did not start")


def wait_idle(port, t=60):
    for _ in range(t * 4):
        if not http(port, "GET", "/api/status")["run"]["running"]: return True
        time.sleep(0.25)
    return False


try:
    app("A", A); app("B", B)
    http(A, "PUT", "/api/pi", {"name": "pi1"}); http(B, "PUT", "/api/pi", {"name": "pi2"})
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        errs = []
        def page(port, w=1280, h=900, scheme="light", **kw):
            ctx = br.new_context(viewport={"width": w, "height": h}, color_scheme=scheme, accept_downloads=True, **kw); pg = ctx.new_page()
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
            pg.on("response", lambda r: errs.append(f"{r.status} {r.request.method} {r.url.split('/', 3)[-1]}") if r.status >= 400 else None)    # the normal paths must not produce a single failed request
            pg.goto(f"http://127.0.0.1:{port}/#tests"); pg.wait_for_selector("#tabs [aria-selected=true]")
            return pg

        # ---------------------------------------------------------------- pi2: the same app, nothing to pair
        b = page(B); b.wait_for_selector(".pcard")
        check("[pi2] opens on the same Setup list as pi1: no Responder card to set up first", b.locator("#respCard, #bWizard, #rTransport").count() == 0 and b.locator(".pcard").count() >= 9)
        # bug: after a re-render of the screen every wizard click used to act twice
        n0 = len(http(B, "GET", "/api/profiles"))
        b.click(".pcard:has-text('Quick link check') summary"); b.click(".pcard:has-text('Quick link check') details[open] [data-act=dup]"); b.wait_for_selector(".pcard:has-text('(copy)')")
        time.sleep(0.6)
        n1 = len(http(B, "GET", "/api/profiles"))
        check("[pi2] a click acts once (Duplicate makes exactly one copy)", n1 == n0 + 1, f"profiles {n0} -> {n1}")

        # ---------------------------------------------------------------- Pi A: header, navigation
        a = page(A)
        over = a.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        vis = a.evaluate("(() => { const r = document.querySelector('#themeBtn').getBoundingClientRect(); return r.right <= window.innerWidth && r.left >= 0; })()")
        check("[pi1] header fits a 1280 px laptop screen: no sideways scroll, theme button on screen", over <= 0 and vis, f"overflow={over}px themeVisible={vis}")
        tabs = a.eval_on_selector_all("#tabs button", "els => els.map(e => e.textContent.trim())")
        check("[pi1] five tabs and Setup (Home · Tests · Generator · Sniffer · Results, Setup at the foot of the rail)", tabs == ["Home", "Tests", "Generator", "Sniffer", "Results", "Setup"], tabs)
        a.click("[data-tab=results]"); a.wait_for_selector("[data-tab=results][aria-selected=true]"); a.click("[data-tab=config] >> visible=true"); a.wait_for_selector("[data-tab=config][aria-selected=true]")
        a.go_back(); a.wait_for_selector("[data-tab=results][aria-selected=true]", timeout=4000)
        check("[pi1] browser Back returns to the previous screen", a.get_attribute("[data-tab=results]", "aria-selected") == "true")
        a.goto(f"http://127.0.0.1:{A}/#monitor"); a.reload(); a.wait_for_selector("[data-tab=tests][aria-selected=true]")
        check("[pi1] an old #monitor link opens the Run screen", True)

        # ---------------------------------------------------------------- Pi A: run a preset in 2 clicks (+ the address, typed once)
        a.click("[data-tab=tests]"); a.wait_for_selector(".pcard")
        clicks = 0
        a.click(".pcard:has-text('Quick link check') >> nth=0 >> [data-act=pick]"); clicks += 1
        a.wait_for_selector("[data-testid=nt-page]")
        check("[pi1] ▶ Run opens the test on one page (Port, Protocol, From → To, Traffic) with the Start bar, no numbered wizard", a.locator(".ntrow").count() == 4 and a.locator(".step").count() == 0 and a.locator("[data-testid=tb-start]").count() == 1)
        a.wait_for_selector("[data-testid=nt-hint].bad", timeout=10000)
        check("[pi1] with no far end the check says what is missing and Start is disabled", a.is_disabled("[data-testid=tb-start]") and "where to send" in a.inner_text("[data-testid=nt-bar]").lower(), a.inner_text("[data-testid=nt-bar]")[:120].replace("\n", " "))
        if not a.locator("[data-testid=nt-far-host]").count(): a.click("[data-testid=nt-type-ip]")
        a.click("[data-testid=nt-head-traffic]"); a.click("[data-testid=nt-more] summary")
        a.fill("[data-k='net.port']", str(UDP)); a.fill("[data-k='ui.len']", "4"); a.select_option("[data-k='ui.lenUnit']", "seconds")
        a.fill("[data-k='threshold_ms']", "35"); a.fill("[data-k='timeout_ms']", "300")
        a.type("[data-testid=nt-far-host]", "127.0.0.1")           # typed, not "changed": the check must start by itself
        a.wait_for_selector("[data-testid=nt-checks] .ntck.ok", timeout=10000); a.wait_for_selector("[data-testid=tb-start]:not([disabled])", timeout=10000)
        check("[pi1] typing the address runs the link check by itself and enables Start", a.is_enabled("[data-testid=tb-start]"), a.inner_text("[data-testid=nt-bar]")[:120].replace("\n", " "))
        a.click("[data-testid=tb-start]"); clicks += 1
        a.wait_for_selector("#runBanner", timeout=8000)
        check("[pi1] run a preset: 2 clicks (▶ Run, ▶ Start) once the far end is known", clicks == 2 and a.get_attribute("[data-tab=tests]", "aria-selected") == "true", f"clicks={clicks}")
        check("[pi1] the five number tiles are on the Run screen at once (placeholders before the first message)", a.locator("#stats .stat").count() == 5, a.locator("#stats .stat").count())
        a.wait_for_selector("#chart canvas"); a.wait_for_selector("#bits")
        check("[pi1] the Run screen shows numbers, link diagram, live chart and message bits together", all(vis4 := [a.is_visible(q) for q in ("#stats", "#wire", "#chart canvas", "#bits")]), dict(zip(("stats", "wire", "chart", "bits"), vis4)))
        # bug: a page opened mid-run drew the limit line at 60 ms whatever the test's limit was
        a2 = page(A); a2.wait_for_selector("#runBanner", timeout=6000)
        thr = a2.evaluate("import('./state.js').then(m => m.S.live.thr)")
        check("[pi1] a page opened mid-run lands on Run and uses the test's own limit (35 ms, not 60)", thr == 35, f"thr={thr}")
        check("[pi1] the Run tab shows a 'running' dot on a freshly opened page", a2.locator("[data-tab=tests] .live").count() == 1)
        a2.close()
        a.wait_for_selector("#runBanner .banner.pass, #runBanner .banner.fail", timeout=30000)
        check("[pi1] read the result: verdict appears on the same screen with no click", a.inner_text("#runBanner .big").strip() in ("✓ PASS", "✕ FAIL"), a.inner_text("#runBanner")[:100].replace("\n", " "))
        with a.expect_download() as dl:
            a.click("#runPdf")
        pdf = open(dl.value.path(), "rb").read(5)
        check("[pi1] export: PDF report is 1 click from the finished run", pdf == b"%PDF-", pdf)
        runs0 = len(http(A, "GET", "/api/runs"))
        a.click("#again"); a.wait_for_selector("#runBanner", timeout=8000)
        runs1 = len(http(A, "GET", "/api/runs"))
        check("[pi1] 'Run again' repeats the same test in 1 click", runs1 == runs0 + 1 and http(A, "GET", "/api/runs")[0]["profile"]["threshold_ms"] == 35, f"runs {runs0} -> {runs1}")
        wait_idle(A); a.wait_for_selector("#seeRes", timeout=15000)
        a.click("#seeRes"); a.wait_for_selector("#detail .banner")
        tiles = a.locator("#detail .tiles .stat").count()
        rows = a.evaluate("(() => { const ys = [...document.querySelectorAll('#detail .tiles .stat')].map(e => Math.round(e.getBoundingClientRect().top)); return [...new Set(ys)].length; })()")
        check("[pi1] Results: the 8 numbers sit in an even 4 x 2 grid (no orphan tile)", tiles == 8 and rows == 2, f"tiles={tiles} rows={rows}")
        order = a.evaluate("(() => { const y = s => document.querySelector(s).getBoundingClientRect().top; return y('.runlist') < y('#detail') })()")
        check("[pi1] Results: the list of tests is above the detail; the open test is highlighted", order and a.locator(".runlist tr.on").count() == 1)
        check("[pi1] Results: chart legends no longer show '--' placeholders", a.locator("#detail .u-legend .u-value").count() == 0 or not a.locator("#detail .u-legend .u-value").first.is_visible())
        with a.expect_download() as dl:
            a.click("#detail a:has-text('Messages (CSV)')")
        head = open(dl.value.path()).readline().strip()
        check("[pi1] export: CSV is 1 click from the result", head.startswith("seq,phase"), head)
        a.click("#sMore summary");
        check("[pi1] Results: rarely used session tools (Rename, Clear) are under 'More'", a.is_visible("#sRen") and a.is_visible("#sClr"))
        a.click("h2 >> nth=0", force=True) if a.locator("h2").count() else None
        # the address typed once is remembered for the next test
        a.click("[data-tab=tests]"); a.wait_for_selector(".pcard"); a.click(".pcard:has-text('DER trip test (IP)') >> nth=0 >> [data-act=pick]")
        a.wait_for_selector("[data-testid=nt-page]")
        if not a.locator("[data-testid=nt-far-host]").count(): a.click("[data-testid=nt-head-ends]")
        a.wait_for_selector("[data-testid=nt-far-host]", timeout=5000)
        check("[pi1] the address is remembered for the next test (typed once)", a.input_value("[data-testid=nt-far-host]") == "127.0.0.1", a.input_value("[data-testid=nt-far-host]"))
        a.click("[data-testid=nt-cancel]"); time.sleep(0.3)
        if a.locator("#modal:not([hidden]) #mYes").count(): a.click("#mYes")
        # generator / sniffer: delete used to double every later click
        a.click("[data-tab=generator]"); a.wait_for_selector(".pcard")
        n0 = len(http(A, "GET", "/api/profiles"))
        a.click(".pcard >> nth=0 >> [data-act=dup]"); time.sleep(0.8)
        mine = [p for p in http(A, "GET", "/api/profiles") if not p["builtin"] and p.get("kind") == "generator"]
        a.click(f".pcard:has-text('{mine[0]['name']}') [data-act=del]"); a.click("#mYes"); time.sleep(0.8)
        a.click(".pcard >> nth=1 >> [data-act=dup]"); time.sleep(0.8)
        n2 = len(http(A, "GET", "/api/profiles"))
        check("[pi1] Generator: after deleting a profile, Duplicate still makes exactly one copy", n2 == n0 + 1, f"profiles {n0} -> {n2}")
        # generator / sniffer editors: the common fields in view, the rarely used ones folded (and opened by themselves when in use)
        a.click(".pcard:has-text('Mirrored-Bits-like small frames') >> nth=0"); time.sleep(0.4)
        vis = [k for k in ("peer_api", "peer_host", "local_host", "interface", "peer_interface", "peer_password") if a.is_visible(f"input[data-conn={k}]")]
        check("[pi1] Generator: each stream shows its own From and To pickers; the typed-Pi addresses, ports and password are folded under 'Advanced'", vis == [] and a.is_visible("#gConnAdv > summary") and a.is_visible("[data-testid=gen-stream-1-from]") and a.is_visible("[data-testid=gen-stream-1-to]"), vis)
        a.click("#gConnAdv > summary")
        check("[pi1] Generator: one click on 'Advanced' shows all six fields", all(a.is_visible(f"input[data-conn={k}]") for k in ("peer_api", "peer_host", "local_host", "interface", "peer_interface", "peer_password")))
        check("[pi1] Generator: bursts are under the stream's Advanced, closed for a plain stream", not a.is_visible("input[data-k=burst_packets]") and a.is_visible("input[data-rate=val]"))
        a.click(".pcard:has-text('Burst stress') >> nth=0"); time.sleep(0.4)
        check("[pi1] Generator: a burst preset opens Advanced by itself and shows its burst values", a.is_visible("input[data-k=burst_packets]") and a.input_value("input[data-k=burst_packets]") == "50", a.input_value("input[data-k=burst_packets]"))
        a.click("[data-tab=sniffer]"); a.wait_for_selector(".pcard"); time.sleep(0.5)
        a.click(".pcard:has-text('SPAN port') >> nth=0"); time.sleep(0.4)
        check("[pi1] Sniffer: 'Stop after' and the USB choice show; file rotation and free space are under Advanced",
              a.is_visible("input[data-p=duration_min]") and a.is_visible("input[data-p='storage.usb']") and not a.is_visible("input[data-p='storage.ring_mb']") and a.is_visible("#sStoreAdv > summary"))
        a.click("#sStoreAdv > summary")
        check("[pi1] Sniffer: one click on Advanced shows the file settings", all(a.is_visible(f"input[data-p='storage.{k}']") for k in ("ring_mb", "ring_files", "ring_minutes", "min_free_mb")))
        box_k, box_v = a.eval_on_selector("#fk", "e => e.getBoundingClientRect().height"), a.eval_on_selector("#fv", "e => e.getBoundingClientRect().height")
        check("[pi1] Sniffer: the quick-filter picker and value box are the same height as the other fields", abs(box_k - box_v) < 2 and box_k >= 40, f"{box_k} / {box_v}")

        # ---------------------------------------------------------------- phone
        m = page(A, 390, 844, "dark", device_scale_factor=2)
        over = m.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        m.click("[data-tab=config] >> visible=true"); m.wait_for_selector("[data-tab=config][aria-selected=true]")
        check("[phone 390 px] no sideways scroll; every tab reachable in the tab strip", over <= 0 and m.get_attribute("[data-tab=config] >> visible=true", "aria-selected") == "true", f"overflow={over}px")
        m.click("[data-tab=tests]"); m.wait_for_selector(".pcard"); m.click(".pcard:has-text('Quick link check') >> nth=0 >> [data-act=pick]"); m.wait_for_selector("[data-testid=tb-start]")
        box = m.evaluate("(() => { const r = document.querySelector('[data-testid=tb-start]').getBoundingClientRect(); return [r.left, r.right, window.innerWidth]; })()")
        check("[phone 390 px] START (transport bar, at the thumb) is on screen and wide enough for a gloved tap", box[0] >= 0 and box[1] <= box[2] and box[1] - box[0] >= 96, box)
        check("no browser console or page errors during the whole run", not errs, errs[:3])
        br.close()
finally:
    for p in procs:
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: pass
    time.sleep(0.5)
    subprocess.run(["rm", "-rf", TMP])
print(f"# {sum(results)}/{len(results)} checks passed")
sys.exit(0 if results and all(results) else 1)
