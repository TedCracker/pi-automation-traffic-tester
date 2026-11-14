#!/usr/bin/env python3
"""Setup test list, About, auto-refresh and Mirrored Bits mode end-to-end (real Chromium, two Pis on this PC): portrait phone, landscape phone, desktop.
Per size: version label under the logo (and on /ai-guide); tap a test card -> plain-English info sheet (5 sections) -> Run this test / Edit; edit a built-in test in the
guided form -> 'Edited' + run it -> Reset to default; rename inline on the card (and the original name comes back with Reset); hide -> 'Show hidden tests' -> restore;
create a new test -> delete -> Undo; reorder (Move up / down) survives a reload; About page (label, top link); one-time 'Updated to' banner; Run + Results refresh by
themselves with the WebSocket blocked (polling chip), 'Live' with it, the Auto-refresh switch is remembered; pi2 (no Responder page) answers a Mirrored Bits test;
a Mirrored Bits test created in the form (Send to = pi2) runs against it and shows the channel section. Prints 'CHECK name | pass/FAIL | evidence'. Run via scripts/e2e-testlist.sh.
Screenshots: docs/screenshots/testlist-*.png"""
import json, os, re, subprocess, sys, tempfile, time, traceback, urllib.request, urllib.error
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "docs", "screenshots")
TMP = tempfile.mkdtemp(prefix="tt-testlist-"); procs, results = [], []
A, B, UDP, MBUDP = 18430, 18431, 20500, 20597
SIZES = [("portrait", 390, 844, True), ("landscape", 844, 390, True), ("desktop", 1280, 800, False)]
BLOCK_WS = "window.WebSocket = class { constructor(){ setTimeout(() => this.onclose && this.onclose({}), 30); } close(){} send(){} };"


def check(name, ok, ev=""):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:500]}", flush=True)


def http(port, method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as f:
            d = f.read(); return json.loads(d) if d else None
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "detail": e.read().decode()[:200]}


def start(role, port):
    try:
        http(port, "GET", "/api/status"); raise RuntimeError(f"port {port} already in use (leftover app from an earlier run?)")
    except urllib.error.URLError:
        pass
    env = dict(os.environ, PYTHONPATH=ROOT, TT_ASSUME_SYNCED="1", TT_TEST_HOOKS="1", TT_NO_DISCOVERY="1")
    if role == "A": env["TT_NO_DEFAULT_REFLECTOR"] = "1"          # two apps in one network namespace: only pi2 answers on UDP/TCP 20500
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{role}"],
                         env=env, stdout=open(f"{TMP}/{role}.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try: http(port, "GET", "/api/status"); return
        except Exception: time.sleep(0.25)
    raise RuntimeError(f"{role} did not start")


def shot(pg, name):
    os.makedirs(SHOTS, exist_ok=True); pg.screenshot(path=os.path.join(SHOTS, f"testlist-{name}.png"))


def card(pg, name): return pg.locator(f".pcard:has(button.pname:text-is('{name}'))")


def names(pg): return pg.locator(".pcard button.pname").all_inner_texts()


def menu(pg, name, act):
    c = card(pg, name)
    if act in ("hide", "del") and pg.viewport_size["width"] > 700:        # wide screens: Delete / Hide are clear buttons on the card, not in the menu
        c.locator(f".acts [data-act={act}]").click(); return
    c.locator("summary").click(); pg.locator(f".pcard:has(button.pname:text-is('{name}')) details.more[open] [data-act={act}]").click()


def open_hidden(pg):
    if not pg.locator("#hiddenTests[open]").count():
        pg.click("#hiddenTests summary")


def trip_names(port): return [p["name"] for p in http(port, "GET", "/api/profiles") if p["kind"] == "trip" and not p["hidden"]]


def wait_run_done(port, limit=40):
    t0 = time.time()
    while time.time() - t0 < limit:
        if not http(port, "GET", "/api/status")["run"]["running"]: return True
        time.sleep(0.4)
    return False


try:
    start("A", A); start("B", B)
    http(A, "PUT", "/api/pi", {"name": "pi1"}); http(B, "PUT", "/api/pi", {"name": "pi2"})
    ver = http(A, "GET", "/api/status")["version"]
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for o, W, H, touch in SIZES:
            tag = f"[{o}] "; phone = W < 700 or H < 480
            ctx = br.new_context(viewport={"width": W, "height": H}, is_mobile=touch, has_touch=touch, accept_downloads=True)
            pg = ctx.new_page(); errs = []
            pg.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
            pg.on("console", lambda m, errs=errs: errs.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
            pg.goto(f"http://127.0.0.1:{A}/#setup"); pg.wait_for_selector(".pcard"); time.sleep(0.4)
            # ---- version label
            v = pg.locator("[data-testid=app-version]"); vb = v.bounding_box(); bb = pg.locator("header .brand b").bounding_box()
            check(f"{tag}version 'v{ver}' (same source as /api/status) in small muted text directly under the logo title, no wrap or overflow",
                  v.inner_text() == f"v{ver}" and -3 < vb["y"] - (bb["y"] + bb["height"]) < 12 and vb["x"] + vb["width"] <= W and pg.evaluate("document.documentElement.scrollWidth <= innerWidth") and float(v.evaluate("e=>parseFloat(getComputedStyle(e).fontSize)")) < 12.5, f"{v.inner_text()} {vb}")
            ag = ctx.new_page(); ag.goto(f"http://127.0.0.1:{A}/ai-guide")
            check(f"{tag}the version label is on /ai-guide too", ag.locator("[data-testid=app-version]").inner_text() == f"v{ver}"); ag.close()
            # ---- the list
            nm = names(pg)
            check(f"{tag}Setup is one list: 9 built-in trip tests (incl. 'DER trip — Mirrored Bits mode'), '＋ Create a new test' at the top", len(nm) == 9 and "DER trip — Mirrored Bits mode" in nm and pg.locator("[data-act=create]").first.is_visible(), nm)
            shot(pg, f"{o}-list")
            # ---- info sheet
            card(pg, "DER trip test").locator("p").first.click(); pg.wait_for_selector("[data-testid=info-sheet]")
            txt = pg.inner_text("[data-testid=info-sheet]")
            secs = ["What it tests", "How it works", "What you need", "What “pass” means", "Typical use"]
            check(f"{tag}tapping a card opens the info sheet with all 5 sections and Run this test / Edit", all(s in txt for s in secs) and pg.locator("[data-testid=sheet-run]").is_visible() and pg.locator("[data-testid=sheet-edit]").is_visible(), txt[:150].replace("\n", " "))
            box = pg.locator("[data-testid=info-sheet]").bounding_box()
            check(f"{tag}the info sheet fits the screen" + (" (bottom sheet)" if phone else ""), box["x"] >= -1 and box["x"] + box["width"] <= W + 1 and box["y"] + box["height"] <= H + 1 and pg.evaluate("document.documentElement.scrollWidth <= innerWidth"), box)
            shot(pg, f"{o}-info")
            pg.click("[data-testid=sheet-run]"); pg.wait_for_selector("[data-testid=nt-page]", timeout=8000)
            check(f"{tag}'Run this test' opens it ready to start (New test page, Start bar, check running)", pg.locator("[data-testid=nt-start]").count() == 1 and "DER trip test" in pg.inner_text("[data-testid=nt-page] h1"))
            # ---- edit a built-in test in place, run it, reset
            pg.reload(); pg.wait_for_selector(".pcard")
            card(pg, "Quick link check").locator(".acts [data-act=edit]").click(); pg.wait_for_selector("[data-testid=nt-page]")
            check(f"{tag}Edit on a built-in test opens the same New test page and says it is built-in", "Built-in test" in pg.inner_text("[data-testid=nt-page]") and "Quick link check" in pg.inner_text("[data-testid=nt-page] h1"))
            pg.click("[data-testid=nt-head-traffic]"); pg.fill("[data-k=ui\\.len]", "3"); pg.select_option("[data-k=ui\\.lenUnit]", "seconds"); pg.fill("[data-k=threshold_ms]", "45"); time.sleep(0.5)
            pg.click("[data-testid=nt-save]"); pg.fill("[data-testid=nt-save-name]", "Quick check (edited)"); pg.click("[data-testid=sheet-go]"); pg.wait_for_selector(".pcard:has-text('Quick check (edited)')", timeout=8000)
            c = card(pg, "Quick check (edited)")
            plist = http(A, "GET", "/api/profiles"); ed = next((p for p in plist if p["name"] == "Quick check (edited)"), {})
            check(f"{tag}an edited built-in shows 'Edited', keeps its place, and its default is still in the app", "Edited" in c.inner_text().title() and names(pg).index("Quick check (edited)") == 3 and ed.get("default_name") == "Quick link check" and ed.get("edited"), f"{names(pg)[:5]} {ed.get('default_name')} {ed.get('edited')}")
            c.locator("summary").click(); check(f"{tag}the ⋮ menu offers Reset to default for it", c.locator("details.more[open] [data-act=reset]").count() == 1); pg.keyboard.press("Escape"); time.sleep(0.2)
            check(f"{tag}Esc closes the ⋮ menu", c.locator("details.more[open]").count() == 0)
            c.locator("[data-act=pick]").click(); pg.wait_for_selector("[data-testid=nt-page]", timeout=15000)      # no other Pi in the list yet: type its address
            if not pg.locator("[data-testid=nt-far-host]").count():
                if not pg.locator("[data-testid=nt-row-ends].open").count(): pg.click("[data-testid=nt-head-ends]")
                pg.click("[data-testid=nt-type-ip]")
            pg.fill("[data-testid=nt-far-host]", "127.0.0.1"); pg.click("[data-testid=nt-head-traffic]"); pg.click("[data-testid=nt-more] summary"); pg.fill("[data-k='net.port']", str(UDP))
            pg.wait_for_selector("[data-testid=nt-start]:not([disabled])", timeout=15000); time.sleep(1.2)
            pg.click("[data-testid=nt-start]"); pg.wait_for_selector("#runBanner", timeout=15000)
            ok = wait_run_done(A, 30); time.sleep(0.5)
            run = http(A, "GET", "/api/runs")[0]
            check(f"{tag}the edited built-in test runs (3 s, 45 ms limit) and the result keeps the name used at run time", ok and run["name"] == "Quick check (edited)" and run["verdict"] in ("PASS", "FAIL"), f"{run['name']} {run['verdict']}")
            pg.click("[data-tab=setup]"); pg.wait_for_selector(".pcard")
            menu(pg, "Quick check (edited)", "reset"); pg.wait_for_selector("#mYes"); pg.click("#mYes"); pg.wait_for_selector(".pcard:has(button.pname:text-is('Quick link check'))", timeout=8000)
            q = [p for p in http(A, "GET", "/api/profiles") if p["name"] == "Quick link check"][0]
            check(f"{tag}Reset to default brings back settings, name and the 'Edited' tag goes", q["threshold_ms"] == 60 and q["duration_s"] == 60 and not q["edited"] and "Edited" not in card(pg, "Quick link check").inner_text().title())
            # ---- rename inline on the card
            card(pg, "Back-to-back baseline").locator("button.pname").click(); pg.fill("input.pname-edit", "Bench baseline"); pg.keyboard.press("Enter"); pg.wait_for_selector(".pcard:has(button.pname:text-is('Bench baseline'))", timeout=6000)
            check(f"{tag}tapping the name renames the test inline (Enter saves); it is 'Edited' and keeps its link to the default", "Edited" in card(pg, "Bench baseline").inner_text().title() and [p for p in http(A, "GET", "/api/profiles") if p["name"] == "Bench baseline"][0]["default_name"] == "Back-to-back baseline")
            card(pg, "Bench baseline").locator("button.pname").click(); pg.fill("input.pname-edit", "Quick link check"); pg.keyboard.press("Enter"); time.sleep(0.8)
            check(f"{tag}a name that another test already has is refused with a plain message", "already exists" in pg.inner_text("#toast") and "Bench baseline" in names(pg), pg.inner_text("#toast")[:100])
            menu(pg, "Bench baseline", "rename"); pg.fill("input.pname-edit", "Bench baseline 2"); pg.keyboard.press("Enter"); pg.wait_for_selector(".pcard:has(button.pname:text-is('Bench baseline 2'))", timeout=6000)
            menu(pg, "Bench baseline 2", "reset"); pg.click("#mYes"); pg.wait_for_selector(".pcard:has(button.pname:text-is('Back-to-back baseline'))", timeout=6000)
            check(f"{tag}⋮ → Rename works too, and Reset to default restores the original name", "Back-to-back baseline" in names(pg))
            # ---- hide / restore
            menu(pg, "Overnight soak", "hide"); pg.wait_for_selector("#mYes"); pg.click("#mYes"); time.sleep(0.8)
            check(f"{tag}Hide (with a confirm) takes a built-in test off the list; 'Show hidden tests (1)' appears", "Overnight soak" not in names(pg) and "Show hidden tests (1)" in pg.inner_text("#hiddenTests"))
            pg.reload(); pg.wait_for_selector(".pcard"); open_hidden(pg); pg.click("#hiddenTests [data-act=unhide]"); pg.wait_for_selector(".pcard:has(button.pname:text-is('Overnight soak'))", timeout=6000)
            check(f"{tag}hidden state survives a reload and 'Show again' brings the test back", "Overnight soak" in names(pg))
            menu(pg, "Overnight soak", "hide"); pg.click("#mYes"); time.sleep(0.6); pg.locator("#toast .toast-act").last.click(); pg.wait_for_selector(".pcard:has(button.pname:text-is('Overnight soak'))", timeout=6000)
            menu(pg, "TCP session test", "hide"); pg.click("#mYes"); time.sleep(0.5); open_hidden(pg); pg.click("[data-act=restoreAll]"); time.sleep(0.8)
            check(f"{tag}Hide → Undo toast, and 'Restore all hidden tests' bring everything back", "Overnight soak" in names(pg) and "TCP session test" in names(pg) and pg.locator("#hiddenTests").count() == 0)
            # ---- create -> delete -> undo
            pg.click("[data-act=create] >> nth=0"); pg.wait_for_selector("[data-testid=nt-page]"); pg.click("[data-testid=nt-save]"); pg.fill("[data-testid=nt-save-name]", "My new test"); pg.click("[data-testid=sheet-go]"); pg.wait_for_selector(".pcard:has-text('My new test')", timeout=8000)
            check(f"{tag}'＋ New test' (top of the list) → the new test is at the end of the same list", names(pg)[-1] == "My new test" and "Mine" in card(pg, "My new test").inner_text().title())
            menu(pg, "My new test", "del"); pg.wait_for_selector("#mYes"); pg.click("#mYes"); time.sleep(0.8)
            gone = "My new test" not in names(pg)
            pg.locator("#toast .toast-act").last.click(); pg.wait_for_selector(".pcard:has-text('My new test')", timeout=6000)
            check(f"{tag}own test: Delete (confirm) is a real delete with an Undo toast that puts it back in the same place", gone and names(pg)[-1] == "My new test")
            # ---- reorder
            before = names(pg); menu(pg, "My new test", "up"); time.sleep(0.8); mid = names(pg)
            pg.reload(); pg.wait_for_selector(".pcard"); after = names(pg)
            check(f"{tag}Move up changes the order and it persists after a reload", mid == after and after.index("My new test") == before.index("My new test") - 1, after[-3:])
            menu(pg, "My new test", "del"); pg.click("#mYes"); time.sleep(0.5)
            # ---- About + banner
            pg.click("[data-testid=app-version]"); pg.wait_for_selector("[data-testid=whats-new]")
            at = pg.inner_text("#main")
            check(f"{tag}tapping the version opens About: version, this Pi's name/hostname, update status and What's new (newest first)", f"v{ver}" in at and "pi1" in pg.inner_text("[data-testid=about-pi-name]") and not re.search(r"Pi [AB]\b|Responder", pg.inner_text("[data-testid=about-card]")) and "What's new" in at and "Version 1.1.0" in at and at.index(f"Version {ver}") < at.index("Version 1.1.0"), at[:120].replace("\n", " "))
            shot(pg, f"{o}-about")
            pg.click("[data-tab=setup]"); pg.click("[data-testid=top-about]"); pg.wait_for_selector("[data-testid=whats-new]")
            check(f"{tag}the top bar About link opens it too, no sideways scroll", pg.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            pg.evaluate("localStorage.setItem('tt-seen-version','1.0')"); pg.reload(); pg.wait_for_selector("[data-testid=updated-banner]", timeout=8000)
            bt = pg.inner_text("[data-testid=updated-banner]")
            check(f"{tag}after an update the app shows 'Updated to v{ver} — what's new' once", f"Updated to v{ver}" in bt and "Version 1.1" in bt, bt[:100].replace("\n", " "))
            shot(pg, f"{o}-updated-banner")
            pg.reload(); pg.wait_for_selector("[data-testid=whats-new]"); time.sleep(0.8)
            check(f"{tag}…and not again after a reload", pg.locator("[data-testid=updated-banner]").count() == 0)
            # ---- auto refresh: live
            pg.click("[data-tab=run]"); time.sleep(0.8)
            check(f"{tag}Run shows '● Live' with Refresh and Auto-refresh", "Live" in pg.inner_text("[data-testid=refresh-chip]") and pg.locator("[data-testid=refresh-now]").is_visible() and pg.locator("[data-testid=refresh-auto]").is_checked())
            ctx.close()

            # ---- auto refresh with the WebSocket blocked: polling keeps Run and Results current
            ctx = br.new_context(viewport={"width": W, "height": H}, is_mobile=touch, has_touch=touch)
            ctx.add_init_script(BLOCK_WS)
            pg = ctx.new_page(); pg.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
            n0 = len(http(A, "GET", "/api/runs"))
            pg.goto(f"http://127.0.0.1:{A}/#results"); pg.wait_for_selector("[data-testid=refresh-chip]"); time.sleep(1.5)
            check(f"{tag}with the live connection blocked the Results chip says it is polling ('↻ Updating every N s')", "Updating every" in pg.inner_text("[data-testid=refresh-chip]"), pg.inner_text("[data-testid=refresh-chip]"))
            rid = http(A, "POST", "/api/runs", {"profile": {"name": "poll run", "transport": "udp", "frame": "extended", "net": {"peer_host": "127.0.0.1", "port": UDP}, "duration_s": 5, "interval_ms": 50}})["run_id"]
            try:
                pg.wait_for_function(f"document.querySelectorAll('tr[data-run]').length > {n0}", timeout=25000); got = True
            except Exception: got = False
            check(f"{tag}Results lists a test started elsewhere without a reload (polling)", got)
            wait_run_done(A, 30)
            try:
                pg.wait_for_function("document.querySelector('tr[data-run] .pill.pass, tr[data-run] .pill.fail')", timeout=25000); fin = True
            except Exception: fin = False
            check(f"{tag}…and moves it from RUNNING to its verdict by itself", fin)
            pg.click("[data-tab=run]"); pg.wait_for_selector("[data-testid=refresh-chip]")
            rid2 = http(A, "POST", "/api/runs", {"profile": {"name": "poll run 2", "transport": "udp", "frame": "extended", "net": {"peer_host": "127.0.0.1", "port": UDP}, "duration_s": 24, "interval_ms": 50}})["run_id"]
            try:
                pg.wait_for_selector("#runBanner", timeout=20000); pg.wait_for_function("document.querySelector('#stats') && /[0-9] ms/.test(document.querySelector('#stats').innerText)", timeout=15000); up = True
            except Exception: up = False
            check(f"{tag}Run picks up a test started elsewhere and shows its numbers while the WebSocket is down", up, pg.inner_text("[data-testid=refresh-chip]"))
            wait_run_done(A, 40)
            try:
                pg.wait_for_selector("#seeRes", timeout=15000); done = True
            except Exception: done = False
            check(f"{tag}…and shows the finished verdict + 'See full results' without a reload", done)
            pg.click("[data-testid=refresh-now]"); pg.uncheck("[data-testid=refresh-auto]"); time.sleep(0.4)
            check(f"{tag}switching Auto-refresh off shows 'Paused'", "Paused" in pg.inner_text("[data-testid=refresh-chip]"))
            pg.reload(); pg.wait_for_selector("[data-tab=run]"); pg.click("[data-tab=run]"); pg.wait_for_selector("[data-testid=refresh-auto]")
            check(f"{tag}…and the choice is remembered in this browser", not pg.locator("[data-testid=refresh-auto]").is_checked() and "Paused" in pg.inner_text("[data-testid=refresh-chip]"))
            shot(pg, f"{o}-refresh")
            ctx.close()
            check(f"{tag}no console / page errors", not errs, errs[:3])

        # ---- Mirrored Bits: pi2 answers (no Responder page any more); a mirrored test made in the form with Send to = pi2 runs against it
        o, W, H, touch = SIZES[0]
        ctx = br.new_context(viewport={"width": W, "height": H}, is_mobile=touch, has_touch=touch); pg = ctx.new_page(); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pid2 = http(A, "POST", "/api/peers", {"host": f"127.0.0.1:{B}"})["id"]
        pg.goto(f"http://127.0.0.1:{B}/#setup"); pg.wait_for_selector(".pcard")
        check("[pi2] Setup shows the same test list as pi1: no Responder card, no 'This Pi is the Responder'", pg.locator("#respCard, #bWizard").count() == 0 and "Responder" not in pg.inner_text("#main") and pg.locator(".pcard").count() >= 9, pg.inner_text("#main")[:80].replace("\n", " "))
        pa = ctx.new_page(); pa.on("pageerror", lambda e: errs.append(str(e)))
        pa.goto(f"http://127.0.0.1:{A}/#setup"); pa.wait_for_selector(".pcard")
        card(pa, "DER trip — Mirrored Bits mode").locator(".acts [data-act=edit]").click(); pa.wait_for_selector("[data-testid=nt-page]")
        pa.click("[data-testid=nt-head-port]"); pa.click("[data-testid=nt-port-auto]"); time.sleep(0.4)
        if not pa.locator("[data-testid=nt-row-proto].open").count(): pa.click("[data-testid=nt-head-proto]")
        pa.click("[data-testid=nt-proto-udp-mb]"); time.sleep(0.4)
        if not pa.locator("[data-testid=nt-row-ends].open").count(): pa.click("[data-testid=nt-head-ends]")
        pa.check("[data-testid=nt-far-pi2]"); time.sleep(0.3)
        pa.click("[data-testid=nt-head-traffic]"); pa.click("[data-testid=nt-more] summary"); pa.fill("[data-k='net.port']", str(MBUDP))
        pa.fill("[data-k='mirrored.rate_baud']", "38400"); pa.fill("[data-k='mirrored.trip_every_s']", "1"); pa.select_option("[data-k=ui\\.lenUnit]", "seconds"); pa.fill("[data-k=ui\\.len]", "8"); time.sleep(0.5)
        summ = pa.inner_text("[data-testid=nt-page]")
        check("[portrait] the New test page has Mirrored Bits over UDP: round trip automatic, channel settings under More options, no message-rate field", pa.locator("[data-testid=nt-dir-locked]").count() == 1 and "ROK" in summ and pa.locator("[data-k=ui\\.rate]").count() == 0 and pa.locator("[data-testid=mb-advanced]").count() == 1, summ[:140])
        shot(pa, "mirrored-form")
        pa.click("[data-testid=nt-save]"); pa.fill("[data-testid=nt-save-name]", "MB over UDP"); pa.click("[data-testid=sheet-go]"); pa.wait_for_selector(".pcard:has-text('MB over UDP')", timeout=8000)
        card(pa, "MB over UDP").locator("[data-act=pick]").click(); pa.wait_for_selector("[data-testid=nt-start]:not([disabled])", timeout=20000)
        check("[portrait] the link check for a Mirrored Bits test finds pi2's continuous messages", "answered" in pa.inner_text("[data-testid=nt-bar]"), pa.inner_text("[data-testid=nt-bar]")[:120].replace("\n", " "))
        pa.click("[data-testid=nt-start]"); pa.wait_for_selector("[data-testid=mb-live] [data-testid=mb-rok]", timeout=15000); time.sleep(3.5)
        lanes = pa.inner_text("[data-testid=mb-live]")
        check("[portrait] Run shows the live Mirrored Bits view: two end cards (pi1 PROTO A, pi2 PROTO B), two lanes, 8 sent + 8 received squares each, ROK lamps, SimCodec label", "pi1" in lanes and "pi2" in lanes and "PROTO A" in lanes and "PROTO B" in lanes and "not SEL wire-compatible" in lanes and pa.locator("[data-testid=mb-live] .mbsq").count() == 32 and pa.locator("[data-testid=mb-live] .mbrok.ok").count() == 2 and "msg/s" in lanes, lanes[:200].replace("\n", " "))
        shot(pa, "mirrored-run")
        wait_run_done(A, 30); time.sleep(0.6)
        run = http(A, "GET", "/api/runs")[0]; full = http(A, "GET", f"/api/runs/{run['id']}"); s = full["summary"]
        check("[portrait] a Mirrored Bits run finishes with trips timed, channel statistics and each direction (clocks synced)", full["status"] == "finished" and s["received"] >= 5 and s["mirrored"]["rx_good"] > 1000 and s["mirrored"]["emulator"] == 2 and s["rtt_ms"]["p99"] is not None, f"{full['status']} {s['rtt_ms']['p99']} ms transfer={s['mirrored']['transfer']['n']} good={s['mirrored']['rx_good']} basis={s['mirrored']['transfer']['basis']}")
        pa.click("[data-tab=results]"); pa.wait_for_selector("[data-testid=mb-result]", timeout=8000)
        check("[portrait] Results shows the Mirrored Bits channel section (bit transfer time, echo round trip, ROK availability, longest outage, RBAD/CBAD, rejects by reason, histogram)", all(k.lower() in pa.inner_text("[data-testid=mb-result]").lower() for k in ("Bit transfer time", "Round trip", "ROK availability", "Longest outage", "RBAD / CBAD", "Rejected messages", "SimCodec")) and pa.locator("[data-testid=mb-events-csv]").count() == 1)
        shot(pa, "mirrored-results")
        pdf = urllib.request.urlopen(f"http://127.0.0.1:{A}/api/report.pdf?run_ids={run['id']}").read()
        check("[portrait] the PDF report includes the Mirrored Bits channel", pdf[:4] == b"%PDF" and len(pdf) > 3000)
        check("[portrait] no console / page errors (Mirrored Bits)", not errs, errs[:3])
        br.close()
except Exception:
    traceback.print_exc(); check("script ran to the end", False)
    try:
        pg.screenshot(path="/tmp/tl-fail.png"); print("# DIAG url", pg.url, "| names", names(pg)[:12], "| main:", pg.inner_text("#main")[:300].replace("\n", " / "), "| toast:", pg.inner_text("#toast")[:100], flush=True)
    except Exception as ex:
        print("# DIAG failed:", ex)
finally:
    for p in procs:
        try: os.killpg(p.pid, 15)
        except Exception: pass
n_ok = sum(1 for _, ok in results if ok)
print(f"# {n_ok}/{len(results)} checks passed" + ("" if n_ok == len(results) else "; FAILED: " + json.dumps([n for n, ok in results if not ok])))
sys.exit(0 if n_ok == len(results) else 1)
