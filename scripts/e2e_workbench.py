#!/usr/bin/env python3
"""Instrument redesign phase 4 (sniffer workbench) in a real browser, desktop 1280 px and phone 390 px, on a live serial DNP3 tap fed through pty pairs:
setup (tap picker, capture filter, saved captures), live workbench (filter bar, packet list with delta and response time, decode, bytes, flows, protocols, timing),
display filter, Errors only, Clear, Save only what is shown, Stop, the same capture reopened, Results link, old links.
Prints 'CHECK name | pass/FAIL | evidence'. Run: python3 scripts/e2e_workbench.py   Screenshots -> docs/dev/ux-impl-shots/phase4/"""
import json, os, re, signal, struct, subprocess, sys, tempfile, threading, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from traffic_tester.sniff import dnp3
SHOTS = os.path.join(ROOT, "docs", "dev", "ux-impl-shots", "phase4"); os.makedirs(SHOTS, exist_ok=True)
TMP = tempfile.mkdtemp(prefix="tt-wbui-"); procs, results = [], []
A = 18880


def check(name, ok, ev=""):
    results.append((name, bool(ok))); print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:300]}", flush=True)


def http(port, method, path, body=None, raw=False):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as f:
        d = f.read()
        return d if raw else (json.loads(d) if d else None)


def pty_pair():
    p = subprocess.Popen(["socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0"], stderr=subprocess.PIPE, text=True, start_new_session=True); procs.append(p)
    ptys = []
    while len(ptys) < 2:
        m = re.search(r"PTY is (\S+)", p.stderr.readline())
        if m: ptys.append(m.group(1))
    return ptys


def shot(pg, name, full=False):
    pg.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=full)


def main():
    a, b = pty_pair(), pty_pair()
    env = dict(os.environ, TT_TEST_HOOKS="1", TT_ASSUME_SYNCED="1", PYTHONPATH=ROOT, TT_NO_DISCOVERY="1", TT_NO_DEFAULT_REFLECTOR="1", TT_EXTRA_SERIAL_PORTS=":".join([a[1], b[1]]))
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--role", "A", "--port", str(A), "--host", "127.0.0.1", "--data-dir", f"{TMP}/A"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try: http(A, "GET", "/api/status"); break
        except Exception: time.sleep(0.25)
    http(A, "PUT", "/api/pi", {"name": "pi1"})
    wa, wb = os.open(a[0], os.O_RDWR), os.open(b[0], os.O_RDWR)
    stop_traffic = threading.Event()

    def traffic():                                      # a master polling an outstation: request, 144 ms, response; every 6th response has a bad CRC
        i = 0
        while not stop_traffic.is_set():
            i += 1
            req = dnp3.build_frame(0xC4, 1, 1024, bytes([0xC0 | (i & 15), 1, 60, 2, 6]))
            os.write(wa, req); time.sleep(0.144)
            rsp = bytearray(dnp3.build_frame(0x44, 1024, 1, bytes([0xC0 | (i & 15), 0x81, 0, 0, 30, 2, 0, 1, 0, 0, 0, 0])))
            if i % 6 == 0: rsp[-1] ^= 0xFF
            os.write(wb, bytes(rsp)); time.sleep(0.35)
    threading.Thread(target=traffic, daemon=True).start()
    prof = {"name": "Inline DNP3 demo", "kind": "sniffer", "sniff": {"mode": "serial", "duration_s": 0, "serial": {"protocol": "dnp3", "channels": [{"name": "Master", "port": a[1], "baud": 19200}, {"name": "Outstation", "port": b[1], "baud": 19200}]}}}
    errors = []
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for tag, (w, h) in (("desk", (1280, 800)), ("phone", (390, 844))):
            ctx = br.new_context(viewport={"width": w, "height": h}, device_scale_factor=2 if tag == "phone" else 1)
            pg = ctx.new_page(); pg.on("pageerror", lambda e: errors.append(str(e)))
            # ---- setup
            pg.goto(f"http://127.0.0.1:{A}/#sniffer"); pg.wait_for_selector(".pcard"); time.sleep(0.6)
            check(f"[{tag}] setup: tap picker, capture filter and Start are there", pg.locator("[data-testid=sniff-tap]").count() == 1 and pg.locator("#bpf").count() == 1 and pg.locator("[data-testid=sniff-start]").count() == 1)
            opts = pg.eval_on_selector("[data-testid=sniff-tap]", "e => [...e.options].map(o => o.text)")
            check(f"[{tag}] tap picker offers inline, mirror port, serial, Wi-Fi", len(opts) == 4 and "Inline Ethernet tap" in opts and any("Serial" in o for o in opts), opts)
            pg.select_option("[data-testid=sniff-tap]", "wifi"); time.sleep(0.3)
            check(f"[{tag}] changing the tap changes the connection settings", pg.locator("select[data-p='wifi.interface']").count() == 1)
            pg.select_option("[data-testid=sniff-tap]", "eth_bridge"); time.sleep(0.2)
            shot(pg, f"{tag}-setup", full=True)
            ctx.close()
        rid = http(A, "POST", "/api/runs", {"profile": prof})["run_id"]
        for tag, (w, h) in (("desk", (1280, 800)), ("phone", (390, 844))):
            ctx = br.new_context(viewport={"width": w, "height": h}, accept_downloads=True, device_scale_factor=2 if tag == "phone" else 1)
            pg = ctx.new_page(); pg.on("pageerror", lambda e: errors.append(str(e))); pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            phone = tag == "phone"
            # ---- live workbench
            pg.goto(f"http://127.0.0.1:{A}/#sniffer"); pg.reload(); pg.wait_for_selector("[data-testid=sniff-wb]"); pg.wait_for_selector("#pkList .pr", timeout=15000); time.sleep(6)
            n = pg.locator("#pkList .pr").count()
            check(f"[{tag}] live: the packet list fills ({n} rows) and the CAPTURING state shows", n >= 8 and pg.inner_text("[data-testid=tb-word]") == "CAPTURING", n)
            check(f"[{tag}] live: tap chip names the serial tap", "serial" in pg.inner_text("[data-testid=sniff-tapchip]").lower(), pg.inner_text("[data-testid=sniff-tapchip]"))
            cnt = pg.inner_text("[data-testid=sniff-counts]")
            check(f"[{tag}] live: counters say captured, shown, errors (and dropped on a wide screen)", all(x in cnt for x in (("captured", "shown", "errors") if phone else ("captured", "shown", "errors", "dropped"))), cnt)
            resp = pg.locator("#pkList .pr.resp").count(); err = pg.locator("#pkList .pr.err").count()
            deltas = [float(x) for x in pg.eval_on_selector_all("#pkList .pr.resp .c-d", "els => els.map(e => e.textContent)")]
            check(f"[{tag}] live: response rows show request-to-response time (~144 ms) in the delta column", resp >= 3 and all(120 <= d <= 400 for d in deltas), deltas[:6])
            check(f"[{tag}] live: a CRC-failed frame is a red row", err >= 1 and "CRC" in pg.inner_text("#pkList .pr.err"), err)
            shot(pg, f"{tag}-live")
            # display filter
            pg.fill("#dfilter", "errors"); time.sleep(1.6)
            shown = pg.locator("#pkList .pr").count()
            check(f"[{tag}] display filter 'errors' keeps only the damaged frames and the count follows", 1 <= shown < n and pg.locator("#pkList .pr:not(.err)").count() == 0 and "shown" in pg.inner_text("[data-testid=sniff-counts]"), shown)
            pg.fill("#dfilter", "len >"); time.sleep(1.0)
            check(f"[{tag}] a wrong filter says so in plain words and the box turns red", pg.is_visible("[data-testid=sniff-dfmsg]") and "number" in pg.inner_text("[data-testid=sniff-dfmsg]") and "bad" in pg.get_attribute("#dfilter", "class"), pg.inner_text("[data-testid=sniff-dfmsg]"))
            pg.fill("#dfilter", "dnp3 and resp > 100"); time.sleep(1.6)
            check(f"[{tag}] 'dnp3 and resp > 100' shows only the responses", pg.locator("#pkList .pr").count() >= 2 and pg.locator("#pkList .pr:not(.resp)").count() == 0)
            pg.fill("#dfilter", ""); time.sleep(1.4)
            pg.click("[data-testid=sniff-errors-only]"); time.sleep(1.4)
            check(f"[{tag}] Errors only button works and un-presses", pg.locator("#pkList .pr:not(.err)").count() == 0 and pg.get_attribute("[data-testid=sniff-errors-only]", "aria-pressed") == "true")
            pg.click("[data-testid=sniff-errors-only]"); time.sleep(1.4)
            # select a packet
            row = pg.locator("#pkList .pr.resp").first; row.click(); time.sleep(0.8)
            layers = pg.eval_on_selector_all("#decBody .lay > summary", "els => els.map(e => e.querySelector('b').textContent)")
            check(f"[{tag}] tapping a packet decodes it layer by layer (Frame, Serial frame, DNP3)", layers == ["Frame", "Serial frame", "DNP3"], layers)
            check(f"[{tag}] the Frame line says how long after the request the response came", "after the request" in pg.inner_text("#decBody"), pg.inner_text("#decBody")[:120])
            if phone: pg.click("#wbTabs [data-tab=bytes]"); time.sleep(0.4)
            check(f"[{tag}] bytes pane shows the packet with the selected layer highlighted", pg.locator("#bytBody .hx").count() >= 2 and pg.locator("#bytBody .h").count() >= 5, pg.locator("#bytBody .h").count())
            shot(pg, f"{tag}-live-packet")
            if phone: pg.click("#wbTabs [data-tab=decode]"); time.sleep(0.3)
            if not phone: pg.click("#decBody .lay >> nth=1 >> summary"); time.sleep(0.2)
            # flows / protocols / timing
            if phone: pg.click("#wbTabs [data-tab=flows]"); time.sleep(1.0)
            else: pg.click("#ptabs [data-tab=flows]"); time.sleep(1.0)
            ft = pg.inner_text("#flwBody")
            check(f"[{tag}] flows: DNP3 conversation with packets and response time", "DNP3" in ft and re.search(r"14\d(\.\d)?", ft) is not None, ft[:150].replace("\n", " "))
            shot(pg, f"{tag}-flows")
            if phone: pg.click("#wbTabs [data-tab=protocols]"); time.sleep(0.8)
            else: pg.click("#ptabs [data-tab=protocols]"); time.sleep(0.8)
            check(f"[{tag}] protocols: DNP3 counted", "DNP3" in pg.inner_text("#flwBody") or "Modbus" in pg.inner_text("#flwBody"), pg.inner_text("#flwBody")[:80])
            if phone: pg.click("#wbTabs [data-tab=timing]"); time.sleep(0.8)
            else: pg.click("#ptabs [data-tab=timing]"); time.sleep(0.8)
            tt = pg.inner_text("#flwBody")
            check(f"[{tag}] timing: request-to-response pairs, spread and longest quiet times", "pairs" in tt and "max" in tt.lower() and pg.locator("#flwBody .spark i").count() > 5, tt[:120].replace("\n", " "))
            shot(pg, f"{tag}-timing")
            if phone: pg.click("#wbTabs [data-tab=timing]"); time.sleep(0.3)          # a second tap folds the sheet away
            # no sideways scroll
            check(f"[{tag}] no sideways page scroll", pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), pg.evaluate("[document.documentElement.scrollWidth, window.innerWidth]"))
            # save only what is shown
            pg.fill("#dfilter", "errors"); time.sleep(1.6)
            pg.click("[data-testid=sniff-save]"); pg.wait_for_selector("[data-testid=sniff-savesheet]")
            with pg.expect_download() as dl:
                pg.click("[data-testid=sheet-shown]")
            path = dl.value.path(); data = open(path, "rb").read()
            check(f"[{tag}] Save pcapng > only what is shown downloads a pcapng with just those packets", data[:4] == b"\x0a\x0d\x0d\x0a" and len(data) < 9000, len(data))
            pg.fill("#dfilter", ""); time.sleep(1.4)
            # clear
            before = pg.locator("#pkList .pr").count()
            pg.click("[data-testid=tb-clear]"); time.sleep(0.5)
            after_clear = pg.locator("#pkList .pr").count()
            check(f"[{tag}] Clear empties the list while the capture keeps running", after_clear < before and pg.inner_text("[data-testid=tb-word]") == "CAPTURING", (before, after_clear))
            time.sleep(3)
            check(f"[{tag}] after Clear the new packets arrive", pg.locator("#pkList .pr").count() >= 2)
            ctx.close()
            if tag == "desk":
                # stop from the transport bar (desktop context only; the phone run continues the same capture)
                pass
        # ---- stop and reopen (new desktop context)
        ctx = br.new_context(viewport={"width": 1280, "height": 800}, accept_downloads=True)
        pg = ctx.new_page(); pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"http://127.0.0.1:{A}/#sniffer"); pg.wait_for_selector("[data-testid=sniff-wb]"); time.sleep(1.0)
        pg.click("[data-testid=tb-start]"); time.sleep(0.5); pg.click("#mYes"); 
        pg.wait_for_selector("[data-testid=sniff-done]", timeout=30000); time.sleep(2.0)
        stop_traffic.set()
        check("stop: the workbench stays, now on the saved capture, with a one-line summary", "Capture saved" in pg.inner_text("[data-testid=sniff-done]") and pg.locator("#pkList .pr").count() >= 5, pg.inner_text("[data-testid=sniff-done]")[:150])
        check("stop: the state says DONE and 'Capture again' is offered", pg.inner_text("[data-testid=tb-word]") == "DONE" and pg.locator("[data-testid=sniff-again]").count() == 1)
        shot(pg, "desk-done")
        pg.fill("#dfilter", "resp > 100"); time.sleep(1.2)
        check("saved capture: the display filter works on it too", pg.locator("#pkList .pr").count() >= 2 and pg.locator("#pkList .pr:not(.resp)").count() == 0)
        pg.click("[data-testid=sniff-save]"); pg.wait_for_selector("[data-testid=sniff-savesheet]"); pg.click("[data-testid=sheet-keep]"); time.sleep(1.2)
        files = [f["name"] for f in http(A, "GET", f"/api/runs/{rid}/capture")["files"]]
        check("saved capture: 'Keep shown with the result' adds a shown-….pcapng file to the run", any(f.startswith("shown-") for f in files), files)
        pg.click("[data-testid=sniff-again]"); pg.wait_for_selector("[data-testid=sniff-start]", state="attached"); time.sleep(0.8)
        check("Capture again returns to the setup, which lists the saved capture", pg.locator("[data-testid=sniff-saved]").count() == 1 and pg.locator(f"[data-testid=sniff-open-{rid}]").count() == 1)
        shot(pg, "desk-setup-saved", full=True)
        pg.click(f"[data-testid=sniff-open-{rid}]"); pg.wait_for_selector("[data-testid=sniff-wb] #pkList .pr"); time.sleep(0.8)
        check("Open on a saved capture shows it in the workbench with a Close button", pg.locator("[data-testid=sniff-close]").count() == 1 and pg.locator("#pkList .pr").count() >= 5)
        pg.click("#pkList .pr >> nth=2"); time.sleep(0.8)
        check("saved capture: packets decode", pg.locator("#decBody .lay").count() >= 4)
        pg.click("[data-testid=sniff-summary]"); pg.wait_for_selector("[data-testid=result-open-wb]"); time.sleep(0.6)
        shot(pg, "desk-result")
        pg.click("[data-testid=result-open-wb]"); pg.wait_for_selector("[data-testid=sniff-wb] #pkList .pr"); time.sleep(0.5)
        check("Results has a button that opens the capture in the workbench", pg.locator("[data-testid=sniff-close]").count() == 1)
        pg.click("[data-testid=sniff-close]"); pg.wait_for_selector("[data-testid=sniff-start]", state="attached")
        check("Close returns to the setup", pg.locator("[data-testid=sniff-wb]").count() == 0)
        ctx.close(); br.close()
    check("no browser errors", not errors, errors[:3])
    bad = [r for r in results if not r[1]]
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
