#!/usr/bin/env python3
"""Mobile sweep (2026-11): every tab and the main dialogs in a real browser at phone, tablet and desktop sizes, light and dark, with automatic checks:
  (a) no sideways page scroll (documentElement and body scrollWidth <= innerWidth)
  (b) every visible interactive element is >= 44 x 44 CSS px on phones/tablets (button, a[href], input, select, textarea, summary, [role=tab|button], label.chk, [tabindex], clickable table rows)
  (c) input / select / textarea computed font-size >= 16 px on phones/tablets (iOS does not zoom)
  (d) the fixed bottom tab bar is visible, has the 5 tabs (Setup is the gear in the strip), sits below the transport bar and does not cover the sticky action bar or the end of the page
  (e) dialogs (confirm, prompt, "More" menu, toast) and the generator / sniffer / profile editors fit the screen with their buttons reachable
plus: manifest + icons + meta, desktop look unchanged at 1280 px, charts re-fit after a rotation.
Run via scripts/e2e-mobile-sweep.sh (system python3 with playwright). Prints 'CHECK name | pass/FAIL | evidence' and 'N/M checks passed'.
Screenshots: docs/screenshots/mobile-<tab>-<WxH>[-dark].png"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time, traceback, urllib.request, urllib.error
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "docs", "screenshots")
TMP = tempfile.mkdtemp(prefix="tt-mobile-"); procs, results = [], []
A, B, UDP = 18340, 18341, 20593
PEER = [None]      # the id of pi2 in pi1's list of Pis
TABS = ("home", "setup", "run", "results", "generator", "sniffer", "settings")
TOL = 0.5          # px of rounding slack on the 44 px rule (sub-pixel layout); 43.5 and up counts as 44
EXCLUDED = []      # (selector, reason): only for elements that are not tap targets by design. Kept empty on purpose; anything listed is printed.

# name, width, height, touch/mobile emulation, theme, run the dialog checks, screenshot tabs
CONFIGS = [
    ("360x780", 360, 780, True, "light", True, True),
    ("360x780-dark", 360, 780, True, "dark", False, False),
    ("390x844", 390, 844, True, "light", True, True),
    ("390x844-dark", 390, 844, True, "dark", True, True),
    ("844x390", 844, 390, True, "light", True, True),
    ("844x390-dark", 844, 390, True, "dark", False, False),
    ("768x1024", 768, 1024, True, "light", True, False),
    ("1024x768", 1024, 768, True, "light", False, False),       # tablet landscape: touch, but wide enough for the top header
    ("1280x800", 1280, 800, False, "light", False, False),
]


def check(name, ok, ev=""):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:600]}", flush=True)


def http(port, method, path, body=None, raw=False):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as f:
        d = f.read(); return d if raw else (json.loads(d) if d else None)


def start(role, port):
    try:
        http(port, "GET", "/api/status"); raise RuntimeError(f"port {port} is already in use (a leftover app from an earlier aborted run?)")
    except urllib.error.URLError:
        pass
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{role}"],
                         env=dict(os.environ, PYTHONPATH=ROOT, TT_ASSUME_SYNCED="1", TT_TEST_HOOKS="1", TT_NO_DEFAULT_REFLECTOR="1", TT_NO_DISCOVERY="1"), stdout=open(f"{TMP}/{role}.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try: http(port, "GET", "/api/status"); return
        except Exception: time.sleep(0.25)
    raise RuntimeError(f"{role} did not start")


def wait_run(rid, port=A, t=90):
    for _ in range(t * 4):
        x = http(port, "GET", f"/api/runs/{rid}")
        if x["status"] != "running":
            for _ in range(80):                                    # the stored result appears a moment before the run is fully torn down (reflectors released): wait for that too
                if not http(port, "GET", "/api/status")["run"]["running"]: break
                time.sleep(0.1)
            return x
        time.sleep(0.25)
    raise TimeoutError(rid)


def trip(name, dur=4, thr=60, interval=50):
    return http(A, "POST", "/api/runs", {"profile": {"name": name, "transport": "udp", "frame": "extended", "dest": {"pi": PEER[0]}, "net": {"peer_host": "", "port": UDP}, "duration_s": dur, "interval_ms": interval,
                                                      "threshold_ms": thr, "timeout_ms": 300, "idle_gaps_s": [], "summary_interval_s": 1}})["run_id"]


# the words of the old fixed roles must be nowhere on screen (the product name "Pi Automation Traffic Tester" is fine; the server-written "For AI assistants" note is the AI guide's own text and is checked by its own tests)
BANNED = re.compile(r"Pi [AB]\b|Responder|(?<!Traffic )\bTester\b", re.I)
TEXT_JS = """() => { const hide = [...document.querySelectorAll('#aiContext, [data-testid=whats-new], [data-testid=updated-banner]')], old = hide.map(e => e.style.display); hide.forEach(e => e.style.display = 'none'); const t = document.body.innerText + ' ' + [...document.querySelectorAll('[title],[aria-label]')].map(e => (e.title || '') + ' ' + (e.getAttribute('aria-label') || '')).join(' '); hide.forEach((e, i) => e.style.display = old[i]); return t; }"""
AUDIT_JS = r"""(phone) => {
  const vw = innerWidth, vh = innerHeight;
  const SEL = 'button, a[href], input:not([type=hidden]), select, textarea, summary, [role=tab], [role=button], label.chk, [tabindex]:not([tabindex="-1"]), tr[data-run]';
  const vis = el => { try { return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) && el.getClientRects().length > 0; } catch { return el.getClientRects().length > 0; } };
  const name = el => { let s = el.tagName.toLowerCase(); if (el.id) s += '#' + el.id; const t = el.dataset && (el.dataset.testid || el.dataset.act || el.dataset.tab); if (t) s += `[${t}]`; if (el.className && typeof el.className === 'string') s += '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.'); if (el.type && el.tagName === 'INPUT') s += `(${el.type})`; const tx = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 24); return s + (tx ? ` "${tx}"` : ''); };
  const root = document.querySelector(phone.root || 'body');
  const small = [], fonts = [];
  let n = 0;
  for (const el of root.querySelectorAll(SEL)) {
    if (!vis(el)) continue;
    if (phone.skip && el.closest(phone.skip)) continue;
    n++;
    const r = el.getBoundingClientRect();
    if (phone.tap && (r.width < 43.5 || r.height < 43.5)) small.push(`${name(el)} ${r.width.toFixed(0)}x${r.height.toFixed(0)}`);
    if (phone.tap && el.matches('input:not([type=checkbox]):not([type=radio]):not([type=file]):not([type=range]), select, textarea')) { const f = parseFloat(getComputedStyle(el).fontSize); if (f < 16) fonts.push(`${name(el)} ${f}px`); }
  }
  const de = document.documentElement, bo = document.body;
  const off = [];
  if (de.scrollWidth > vw || bo.scrollWidth > vw) for (const el of document.querySelectorAll('body *')) { const r = el.getBoundingClientRect(); if (r.width && r.right > vw + 1 && vis(el) && !el.closest('.scroll,.runlist,table,pre,.u-wrap,#tabs')) { off.push(`${name(el)} right=${r.right.toFixed(0)}`); if (off.length > 4) break; } }
  return { vw, vh, sw: de.scrollWidth, bw: bo.scrollWidth, n, small: [...new Set(small)], fonts: [...new Set(fonts)], off };
}"""

BARS_JS = r"""() => {
  const vw = innerWidth, vh = innerHeight;
  const vis = el => el && el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) && el.getClientRects().length > 0;
  const nav = document.querySelector('#tabs'), cs = getComputedStyle(nav), r = nav.getBoundingClientRect();
  const tabs = [...nav.querySelectorAll('[role=tab]')];
  const out = { pos: cs.position, navTop: r.top, navBottom: r.bottom, navH: r.height, navL: r.left, navR: r.right, vw, vh, tabs: tabs.length, tabsVis: tabs.filter(vis).length,
    tabsIn: tabs.filter(t => { const q = t.getBoundingClientRect(); return q.left >= -0.5 && q.right <= vw + 0.5 && q.bottom <= vh + 0.5; }).length,
    sel: tabs.filter(t => t.getAttribute('aria-selected') === 'true').length, tabCount: [...new Set(tabs.map(t => Math.round(t.getBoundingClientRect().top)))].length,
    icons: tabs.filter(t => vis(t.querySelector('.ti'))).length, labels: tabs.map(t => t.textContent.trim()), bars: [] };
  for (const q of ['.actionbar', '#runActs', '#transport']) {
    const el = document.querySelector(q); if (!vis(el)) continue;
    const b = el.getBoundingClientRect();
    const btn = el.querySelector('button:not([disabled]), a.btn'); let reach = null;
    if (btn) { const bb = btn.getBoundingClientRect(); const hit = document.elementFromPoint(bb.left + bb.width / 2, bb.top + bb.height / 2); reach = !!hit && (hit === btn || btn.contains(hit)); }
    out.bars.push({ q, top: b.top, bottom: b.bottom, left: b.left, right: b.right, abovNav: b.bottom <= r.top + 1, inView: b.left >= -0.5 && b.right <= vw + 0.5 && b.top >= 0, reach });
  }
  return out;
}"""

END_JS = r"""() => {
  scrollTo(0, document.documentElement.scrollHeight);
  const nav = document.querySelector('#tabs').getBoundingClientRect(); let lim = nav.top;
  const ra = document.querySelector('#runActs'); if (ra && ra.checkVisibility() && getComputedStyle(ra).position === 'fixed') lim = Math.min(lim, ra.getBoundingClientRect().top);
  const tb = document.querySelector('#transport'); if (tb && tb.checkVisibility()) lim = Math.min(lim, tb.getBoundingClientRect().top);
  const fe = document.querySelector('#about'), foot = fe.getBoundingClientRect(); const fpad = parseFloat(getComputedStyle(fe).paddingBottom) || 0;       // its bottom padding is empty space, not text
  const last = [...document.querySelectorAll('#main button, #main a, #main input, #main select, #main summary')].filter(e => e.checkVisibility() && e.getBoundingClientRect().height && !e.closest('.scroll,.runlist,#runActs')).pop();
  const lb = last ? last.getBoundingClientRect().bottom : 0;
  const r = { lim, footBottom: foot.bottom - fpad, lastBottom: lb, scrolled: scrollY };
  scrollTo(0, 0); return r;
}"""


def fmt(l, k=6):
    return "; ".join(l[:k]) + (f" (+{len(l) - k} more)" if len(l) > k else "")


def shot(pg, tab, cfg, theme):
    os.makedirs(SHOTS, exist_ok=True)
    pg.screenshot(path=os.path.join(SHOTS, f"mobile-{tab}-{cfg.replace('-dark', '')}{'-dark' if theme == 'dark' else ''}.png"))


try:
    start("A", A); start("B", B)
    http(A, "PUT", "/api/pi", {"name": "pi1"}); http(B, "PUT", "/api/pi", {"name": "pi2"})
    PEER[0] = http(A, "POST", "/api/peers", {"host": f"127.0.0.1:{B}"})["id"]          # pi2 is in pi1's list of Pis: every test below goes pi1 -> pi2
    # ---- seed: a saved profile, two trip runs (one PASS, one FAIL), one generator run, so Results is not empty
    presets = http(A, "GET", "/api/profiles")
    base = next(p for p in presets if p["name"] == "Quick link check")
    saved = http(A, "POST", f"/api/profiles/{base['id']}/duplicate")
    http(A, "PUT", f"/api/profiles/{saved['id']}", {**saved, "name": "Site A link", "dest": {"pi": PEER[0], "name": "pi2"}, "net": {**saved["net"], "peer_host": "", "port": UDP}})
    r1 = wait_run(trip("seed pass", 4, 60)); r2 = wait_run(trip("seed fail", 4, 0.02))
    gen = {"name": "seed generator", "kind": "generator", "net": {"peer_host": "127.0.0.1"},
           "gen": {"streams": [{"name": "udp", "type": "udp", "rate": {"mbps": 10}, "size": {"mode": "fixed", "size": 1200}, "rtt_probe_pps": 20}], "duration_s": 4, "peer_api": f"http://127.0.0.1:{B}",
                   "local_host": "127.0.0.1", "interface": "lo", "peer_interface": "lo", "rule": {"max_loss_pct": 0.5, "max_latency_ms": 50}}}
    try: r3 = wait_run(http(A, "POST", "/api/runs", {"profile": gen})["run_id"])
    except Exception as e: r3 = {"status": repr(e)}
    check("seed data: two trip runs (PASS and FAIL) and a generator run exist so Results/Run are not empty", r1["verdict"] != r2["verdict"] and len(http(A, "GET", "/api/runs")) >= 3,
          f"{r1['verdict']}/{r2['verdict']}/{r3.get('verdict', r3.get('status'))} runs={len(http(A, 'GET', '/api/runs'))}")

    # ---- manifest, icons, head tags (no browser needed)
    html = http(A, "GET", "/", raw=True).decode()
    req = urllib.request.urlopen(f"http://127.0.0.1:{A}/manifest.webmanifest")
    mt, man = req.headers.get("Content-Type", ""), json.loads(req.read())
    icons = {i["src"]: i for i in man.get("icons", [])}
    sizes_ok = True; ev = []
    from struct import unpack
    for i in man.get("icons", []):
        d = http(A, "GET", "/" + i["src"], raw=True); w, h = unpack(">II", d[16:24]); ev.append(f"{i['src']} {w}x{h} {i.get('purpose')}")
        sizes_ok &= d[:8] == b"\x89PNG\r\n\x1a\n" and f"{w}x{h}" == i["sizes"]
    check("web app manifest: served as application/manifest+json, name/short_name/start_url '/'/display standalone/colors, icons 192 + 512 + maskable are real PNGs of the stated size",
          "manifest+json" in mt and man["name"] and man["short_name"] and man["start_url"] == "/" and man["display"] == "standalone" and man["theme_color"] and man["background_color"]
          and any(i.get("purpose") == "maskable" for i in man["icons"]) and {"192x192", "512x512"} <= {i["sizes"] for i in man["icons"]} and sizes_ok, f"{mt}; " + ", ".join(ev))
    at = http(A, "GET", "/icons/apple-touch-icon.png", raw=True)
    check("index.html has the manifest link, apple-touch-icon (180x180 PNG), theme-color and viewport-fit=cover; no external (CDN) URL anywhere in the page",
          'rel="manifest"' in html and 'rel="apple-touch-icon"' in html and at[:4] == b"\x89PNG" and unpack(">II", at[16:24]) == (180, 180) and 'name="theme-color"' in html and "viewport-fit=cover" in html
          and "http://" not in html.replace("http://www.w3.org", "") and "https://" not in html, f"apple-touch {unpack('>II', at[16:24])}")

    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for cname, W, H, touch, theme, dialogs, shots in [c for c in CONFIGS if not os.environ.get("MS_ONLY") or c[0] in os.environ["MS_ONLY"].split(",")]:
            tag = f"[{cname}] "; phone = touch; compact = W <= 820 or (H <= 480 and W <= 1000)       # compact = the layout with the bottom tab bar (same rule as style.css)
            ctx = br.new_context(viewport={"width": W, "height": H}, device_scale_factor=1, is_mobile=touch, has_touch=touch, color_scheme=theme, accept_downloads=True)
            pg = ctx.new_page(); errs = []
            pg.on("pageerror", lambda e, errs=errs: errs.append(str(e) + " @ " + " | ".join(str(getattr(e, "stack", "") or "").split("\n")[1:3])))
            pg.on("console", lambda m, errs=errs: errs.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
            rid = trip(f"live {cname}", 9, 60, 50)                      # a test is running while the Run tab is audited
            pg.goto(f"http://127.0.0.1:{A}/"); pg.wait_for_selector("#tabs [aria-selected=true]"); time.sleep(0.8)
            P = {"tap": phone, "skip": None}
            all_small, all_fonts, all_off, ns, bars = {}, {}, {}, {}, {}

            all_roles = {}

            def audit_state(label, root="body", skip=None):
                a = pg.evaluate(AUDIT_JS, {"tap": phone, "root": root, "skip": skip})
                txt = pg.evaluate(TEXT_JS)
                bad_words = BANNED.findall(txt)
                if bad_words: all_roles[label] = sorted(set(bad_words))
                all_off[label] = a["sw"] <= a["vw"] and a["bw"] <= a["vw"]
                if not all_off[label]: all_off[label] = f"scrollWidth {a['sw']}/{a['bw']} > {a['vw']}: {fmt(a['off'], 4)}"
                if a["small"]: all_small[label] = a["small"]
                if a["fonts"]: all_fonts[label] = a["fonts"]
                ns[label] = a["n"]
                return a

            def go(tab):
                # old screen names: setup = the test list, run = the live screen (both on Tests now), settings = Setup (gear)
                t = {"setup": "tests", "run": "tests", "settings": "config"}.get(tab, tab)
                pg.locator(f"[data-tab={t}]:visible").first.click(); pg.wait_for_selector(f"[data-tab={t}][aria-selected=true]"); time.sleep(0.9)
                if tab == "setup" and pg.locator("#runBanner").count():           # Tests shows the live screen while something runs: wait for it to end, then open the list
                    pg.wait_for_function("document.querySelector('[data-testid=tb-word]').textContent !== 'RUNNING'", timeout=90000)
                    pg.locator(f"[data-tab=tests]:visible").first.click(); time.sleep(0.9)

            def bar_state(label):
                if not compact: return
                b = pg.evaluate(BARS_JS); e = pg.evaluate(END_JS); bars[label] = (b, e)

            # ---------------- every tab (+ its richer states)
            for tab in ("run",) + tuple(t for t in TABS if t != "run"):
                go(tab)
                if tab == "run": pg.wait_for_selector("#runBanner", timeout=8000); pg.wait_for_selector("#chart canvas"); time.sleep(0.4)
                audit_state(tab); bar_state(tab)
                if shots: shot(pg, tab, cname, theme)
                if tab == "setup":
                    # ▶ Run on a saved test: the New test page with every row closed to its line, then each row open
                    pg.click(".pcard:has-text('Site A link') [data-act=pick]"); pg.wait_for_selector("[data-testid=nt-page]"); time.sleep(1.2)
                    audit_state("setup/new test (rows closed, Start bar)"); bar_state("setup/new test")
                    if shots: shot(pg, "setup-connect", cname, theme)
                    for r in ("port", "proto", "ends", "traffic"):
                        if not pg.locator(f"[data-testid=nt-row-{r}].open").count(): pg.click(f"[data-testid=nt-head-{r}]"); time.sleep(0.3)
                    for d in pg.locator("#ntPage details").all(): d.evaluate("e => e.open = true")
                    time.sleep(0.4); audit_state("setup/new test (every row and More options open)"); bar_state("setup/new test (open)")
                    if shots: shot(pg, "setup-review", cname, theme)
                    pg.click("[data-testid=nt-type-ip]"); pg.wait_for_selector("[data-testid=nt-far-host]"); time.sleep(0.4); audit_state("setup/new test (typed address)")
                    pg.click("[data-testid=nt-dir-both]"); time.sleep(0.8); audit_state("setup/new test (both ways)")
                    pg.click("[data-testid=nt-cancel]"); time.sleep(0.4)
                    if pg.locator("#modal:not([hidden]) .box").count(): audit_state("new test: leave-without-saving dialog", root="#modal .box"); pg.click("#mYes"); time.sleep(0.4)
                    # ＋ New test: a fresh page (rows 1-3 open), an inline error, serial, TCP, greyed-out GOOSE
                    pg.click("[data-testid=setup-create-custom-test]"); pg.wait_for_selector("[data-testid=nt-page]"); time.sleep(0.8)
                    audit_state("setup/new test (fresh)"); bar_state("setup/new test (fresh)")
                    if shots: shot(pg, "setup-custom", cname, theme)
                    pg.click("[data-testid=nt-head-traffic]"); pg.fill("[data-k='ui.rate']", "0"); time.sleep(0.4); audit_state("setup/new test (inline error shown)")
                    pg.fill("[data-k='ui.rate']", "50")
                    ser = pg.locator("[data-testid^=nt-port-tty]")
                    if ser.count(): ser.first.click(); time.sleep(0.4); audit_state("setup/new test (serial)"); bar_state("setup/new test (serial)")
                    pg.click("[data-testid=nt-port-auto]"); time.sleep(0.3); pg.click("[data-testid=nt-proto-tcp]"); time.sleep(0.3)
                    for d in pg.locator("#ntPage details").all(): d.evaluate("e => e.open = true")
                    time.sleep(0.3); audit_state("setup/new test (TCP, More options open)")
                    if pg.locator("#ntPage .ntchip.off").count(): pg.locator("#ntPage .ntchip.off").first.scroll_into_view_if_needed(); audit_state("setup/new test (greyed-out options)")
                    if shots: shot(pg, "setup-custom-greyed", cname, theme)
                    pg.click("[data-testid=nt-cancel]"); time.sleep(0.4)
                    if pg.locator("#modal:not([hidden]) .box").count(): audit_state("new test: discard-changes dialog", root="#modal .box"); pg.click("#mYes"); time.sleep(0.4)
                if tab == "settings":
                    for sub in ("pis", "network", "time", "update", "password", "help", "demo"):
                        pg.evaluate(f"location.hash = '#config/{sub}'"); time.sleep(1.2); audit_state(f"setup/{sub}")
                    pg.evaluate("location.hash = '#config'"); time.sleep(0.5)
                if tab == "results":
                    rows = pg.locator(".runlist tr[data-run]").count()
                    for i in range(min(rows, 3)):
                        pg.locator(".runlist tr[data-run]").nth(i).click(); pg.wait_for_selector("#detail .banner"); time.sleep(1.0)
                        audit_state(f"results/detail{i}"); bar_state(f"results/detail{i}")
                        if shots and i == 0: shot(pg, "results-detail", cname, theme)
                if tab == "generator":
                    pg.locator("[data-testid=gen-row-1]").first.click(); time.sleep(0.8)
                    for d in pg.locator("#main details").all(): d.evaluate("e => e.open = true")
                    time.sleep(0.4); audit_state("generator/editor"); bar_state("generator/editor")
                    if shots: shot(pg, "generator-editor", cname, theme)
                if tab == "sniffer":
                    pg.locator(".pcard:has-text('built-in Wi-Fi')").first.click(); time.sleep(0.8)
                    for d in pg.locator("#main details").all(): d.evaluate("e => e.open = true")
                    time.sleep(0.4); audit_state("sniffer/editor(wi-fi)"); bar_state("sniffer/editor")
                    pg.locator(".pcard:has-text('Inline Ethernet tap')").first.click(); time.sleep(0.6)
                    audit_state("sniffer/editor(inline tap)")
                    if shots: shot(pg, "sniffer-editor", cname, theme)
            rid = trip(f"finish {cname}", 5, 60, 50); time.sleep(0.6)               # the finished screen stays on Tests until you leave it: open Tests while a short test runs, wait for its verdict there
            go("run"); pg.wait_for_selector("#runBanner", timeout=20000)
            try: pg.wait_for_selector("#seeRes", timeout=30000)
            except Exception: pass
            audit_state("run/finished"); bar_state("run/finished")
            if shots: shot(pg, "run-finished", cname, theme)
            wait_run(rid)

            # ---------------- a generator test while it runs (live charts, stream table) and after it finished
            gp = {**gen, "name": f"live gen {cname}", "gen": {**gen["gen"], "duration_s": 9}}
            grid = http(A, "POST", "/api/runs", {"profile": gp})["run_id"]; time.sleep(1.0)
            pg.reload(); pg.wait_for_selector("#tabs [aria-selected=true]"); go("generator")
            try:
                pg.wait_for_selector("#gC1 canvas", timeout=8000); time.sleep(2.5)
                audit_state("generator/live"); bar_state("generator/live")
                if shots: shot(pg, "generator-live", cname, theme)
                wait_run(grid)                                    # (its finished result is the generator row in Results, swept above)
            except Exception as ex:
                all_off["generator/live (could not render)"] = repr(ex)[:200]; wait_run(grid)
            # ---------------- a sniffer capture while it runs: no capture tool on this host, so the browser is fed the same events the server would send (live numbers, packet list)
            pg.reload(); pg.wait_for_selector("#tabs [aria-selected=true]"); go("settings")
            pg.evaluate("""async () => { const { S } = await import('./state.js'); S.live.running = true; S.live.runId = 9999; S.live.done = null; S.live.profile = { kind: 'sniffer', name: 'synthetic', sniff: { mode: 'eth_span' } };
                S.sniff.xs = []; S.sniff.pps = []; S.sniff.mbps = []; S.sniff.rows = []; S.sniff.last = { mode: 'eth_span', elapsed_s: 40, total_s: 60, packets: 18452, bytes: 2.4e7, pps: 312, mbps: 1.52, free_mb: 5120, file_bytes: 3.2e6 }; }""")
            rows = [{"n": n, "t_ns": 1_700_000_000_000_000_000 + n * 20_000_000, "src": f"192.168.100.{n % 9 + 1}:20000", "dst": "224.0.1.129", "sip": "192.168.100.%d" % (n % 9 + 1), "proto": "UDP" if n % 3 else "GOOSE", "len": 60 + n,
                     "info": f"stNum=1 sqNum={n} allData [Mod=on Beh=on Health=ok]", "errors": 1 if n == 7 else 0, "retrans": n == 9, "resp_ms": 144.1 if n % 5 == 0 else None, "delta_ms": 144.1 if n % 5 == 0 else 20.0, "dir": "→", "linktype": 1} for n in range(1, 61)]
            view = {"rows": rows, "total": 18452, "shown": 60, "errors": 1, "sampling": True, "seen": 18452, "decoded": 600, "mode": "eth_span"}
            pg.route("**/api/sniff/view*", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(view if "after=0" in r.request.url else {**view, "rows": []})))
            pg.route("**/api/sniff/flows*", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"flows": [{"key": "k", "label": ".1 → 224.0.1.129:20000", "a": "192.168.100.1:20000", "b": "224.0.1.129:20000", "proto": "GOOSE", "packets": 600, "bytes": 51000, "errors": 1, "retrans": 0, "responses": 0, "resp_avg_ms": None}]})))
            pg.route("**/api/runs/9999", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"id": 9999, "status": "running", "profile": {"kind": "sniffer", "name": "synthetic", "sniff": {"mode": "eth_span", "eth": {"interface": "eth0"}, "filter": {"bpf": "ether proto 0x88b8"}}}, "summary": None})))
            go("sniffer"); pg.wait_for_selector("#pkList .pr"); time.sleep(0.5)
            pg.route("**/api/sniff/packet/*", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"proto": "GOOSE", "n": 3, "len": 66, "hex": "01 0c cd 01 00 01 00 1a 4b 11 22 33 88 b8 00 01 00 5e 00 00 00 00 61 54 80 0a 67 63 62 31 2f 4c 4c 30 24 47 4f".replace(" ", ""),
                "layers": [{"name": "Frame", "fields": {"Number": 3, "Length": "66 bytes"}, "range": [0, 66]}, {"name": "Ethernet", "fields": {"Destination": "01:0c:cd:01:00:01", "Source": "00:1a:4b:11:22:33", "Type": "GOOSE (0x88b8)"}, "range": [0, 14]},
                           {"name": "GOOSE", "fields": {"gocbRef": "IED1/LLN0$GO$gcb1", "datSet": "IED1/LLN0$DataSet1", "stNum": "1", "allData": ["Mod = on", "Beh = on", "Health = ok"]}, "range": [14, 33]}]})))
            audit_state("sniffer/live capture"); bar_state("sniffer/live capture")
            pg.locator("#pkList .pr").nth(2).click(); pg.wait_for_selector("#decBody .lay"); time.sleep(0.8)
            audit_state("sniffer/live capture + packet detail"); bar_state("sniffer/live capture + packet detail")
            if shots: shot(pg, "sniffer-live", cname, theme)
            for u in ("**/api/sniff/packet/*", "**/api/sniff/view*", "**/api/sniff/flows*", "**/api/runs/9999"): pg.unroute(u)
            # ---------------- pi2: the same app, the same six screens (no Responder page), and its Setup step 2 with pi1 in the list
            pg.goto(f"http://127.0.0.1:{B}/"); pg.wait_for_selector("#tabs [aria-selected=true]"); time.sleep(0.8)
            for tab in TABS:
                go(tab); audit_state(f"pi2/{tab}"); bar_state(f"pi2/{tab}")
                if tab == "setup":
                    if shots: shot(pg, "setup-pi2", cname, theme)
                    all_roles_b = pg.locator("#respCard, #bWizard").count()
                    if all_roles_b: all_roles["pi2/setup has a Responder card"] = ["respCard"]
            pg.goto(f"http://127.0.0.1:{A}/"); pg.wait_for_selector("#tabs [aria-selected=true]"); time.sleep(0.8)

            # ---------------- top bar with a password set: the Pi's name, "Signed in" and "Sign out" (then sign out, sign in again, remove the password)
            http(A, "POST", "/api/auth/set", {"password": "sweep-pw"}); pg.reload(); pg.wait_for_selector("#pw"); pg.fill("#pw", "sweep-pw"); pg.click("#pwGo"); pg.wait_for_selector("[data-testid=top-sign-out]"); time.sleep(0.6)
            audit_state("top bar with a password set (Signed in / Sign out)")
            tb = pg.evaluate("() => { const r = e => { const b = document.querySelector(e).getBoundingClientRect(); return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; }; return { name: r('[data-testid=top-pi-name]'), si: r('[data-testid=top-signed-in]'), so: r('[data-testid=top-sign-out]'), vw: innerWidth, txt: document.querySelector('header.top').innerText + ' ' + document.querySelector('footer#about').innerText }; }")
            check(f"{tag}password set: the strip shows the Pi's name; the footer shows 'Signed in' and a 'Sign out' link; all inside the screen width", "pi1" in tb["txt"] and "Signed in" in tb["txt"] and "Sign out" in tb["txt"] and all(b[0] >= 0 and b[0] + b[2] <= tb["vw"] + 1 for b in (tb["name"], tb["si"], tb["so"])), tb)
            pg.click("[data-testid=top-sign-out]"); pg.wait_for_selector("#pw"); time.sleep(0.3); audit_state("login screen after Sign out")
            pg.fill("#pw", "sweep-pw"); pg.click("#pwGo"); pg.wait_for_selector("[data-testid=top-sign-out]")
            pg.evaluate("fetch('/api/auth/set', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: '' }) })"); time.sleep(0.5); pg.reload(); pg.wait_for_selector("#tabs [aria-selected=true]"); time.sleep(0.5)
            check(f"{tag}top bar without a password: only the Pi's name, no 'Signed in' / 'Sign out'", pg.locator("[data-testid=top-sign-out]").is_hidden() and "Signed in" not in pg.inner_text("header.top") and "pi1" in pg.inner_text("header.top"), pg.inner_text("header.top")[-60:])

            # ---------------- summarise per config
            OTHER_OWNER = {"setup", "settings", "pi2/setup", "pi2/settings", "top bar with a password set (Signed in / Sign out)"}      # the Setup test list (built-in test texts, profiles.py) and Settings (settings.js / update.js) are written in other files
            mine_w = {k: v for k, v in all_roles.items() if k not in OTHER_OWNER}; other_w = {k: v for k, v in all_roles.items() if k in OTHER_OWNER}
            check(f"{tag}(f) the words Pi A / Pi B / Responder / Tester appear nowhere on the {len(all_off) - len(OTHER_OWNER & set(all_off))} screens and states built from the UI scripts (wizard steps, Run, Results, Generator, Sniffer, custom form, About, dialogs, top bar; not the change log history; page title '<name> · Pi Automation Traffic Tester')", not mine_w and BANNED.search(pg.title()) is None, "; ".join(f"{k}: {v}" for k, v in mine_w.items()) or "none found")
            check(f"{tag}(f2) …and nowhere on the Setup test list / info sheets and Settings (texts from profiles.py, settings.js, update.js)", not other_w, "; ".join(f"{k}: {v}" for k, v in other_w.items()) or "none found")
            check(f"{tag}(a) no sideways page scroll on any screen/state ({len(all_off)} states: all 6 tabs of pi1 and pi2, wizard steps, editors, result details, live generator and sniffer captures)", all(v is True for v in all_off.values()),
                  "; ".join(f"{k}: {v}" for k, v in all_off.items() if v is not True) or f"{len(all_off)} states, scrollWidth <= {W}")
            if phone:
                check(f"{tag}(b) every visible tap target >= 44 x 44 px (button, a, input, select, textarea, summary, tab, label.chk, [tabindex], table rows; {sum(ns.values())} elements measured; exclusions: {EXCLUDED or 'none'})",
                      not all_small, "; ".join(f"{k}: {fmt(v, 5)}" for k, v in all_small.items()) or f"all >= 44 px (tolerance {TOL} px)")
                check(f"{tag}(c) input / select / textarea font-size >= 16 px", not all_fonts, "; ".join(f"{k}: {fmt(v, 4)}" for k, v in all_fonts.items()) or "all >= 16px")
            if compact:
                bad = []; seen_bar = set()
                for lab, (b, e) in bars.items():
                    if b["pos"] != "fixed" or abs(b["navBottom"] - b["vh"]) > 1 or b["navL"] > 0.5 or b["navR"] < b["vw"] - 0.5 or b["tabs"] != 5 or b["tabsVis"] != 5 or b["tabsIn"] != 5 or b["sel"] != (0 if "settings" in lab else 1) or b["tabCount"] != 1 or b["icons"] != 5: bad.append(f"{lab}: nav {json.dumps({k: b[k] for k in ('pos', 'navBottom', 'vh', 'tabs', 'tabsVis', 'tabsIn', 'sel', 'tabCount', 'icons')})}")
                    for x in b["bars"]:
                        seen_bar.add(x["q"])
                        if not (x["abovNav"] and x["inView"] and x["reach"] is not False) and not (x["q"] == ".actionbar" and False): bad.append(f"{lab}: {x['q']} bottom={x['bottom']:.0f} navTop={b['navTop']:.0f} inView={x['inView']} reach={x['reach']}")
                    if e["footBottom"] > e["lim"] + 1 or e["lastBottom"] > e["lim"] + 1: bad.append(f"{lab}: end of page under the bars (foot {e['footBottom']:.0f}, last control {e['lastBottom']:.0f}, limit {e['lim']:.0f})")
                labels = next(iter(bars.values()))[0]["labels"]
                check(f"{tag}(d) fixed bottom tab bar visible on every state (5 tabs with icons {labels}, exactly one selected, inside the screen), sticky action bars ({sorted(seen_bar)}) above it and reachable, page end clears the bars",
                      not bad, "; ".join(bad[:4]) or f"{len(bars)} states checked")
            else:
                b = pg.evaluate("() => { const n = document.querySelector('#tabs'), r = [...n.querySelectorAll('[role=tab]')].map(t => Math.round(t.getBoundingClientRect().top)); const h = document.querySelector('header.top');"
                                " return { pos: getComputedStyle(n).position, rows: new Set(r).size, icons: [...n.querySelectorAll('.ti')].filter(i => i.getClientRects().length).length, sticky: getComputedStyle(h).position, h: Math.round(h.getBoundingClientRect().height) } }")
                check(f"{tag}wide layout (desktop / tablet landscape): sticky 40 px lamp strip on top, the five tabs + Setup as a left rail with icons, the transport bar under the strip", b["pos"] == "fixed" and b["rows"] == 6 and b["icons"] == 6 and b["sticky"] == "sticky" and b["h"] < 90, b)

            # ---------------- dialogs / panels
            if dialogs:
                def dlg(label, sel, tap=True, expect_full=False, reach=None, skip=None):
                    """fit: box inside the screen; no sideways scroll; buttons >= 44 and reachable (elementFromPoint hits them)."""
                    a = pg.evaluate(AUDIT_JS, {"tap": phone and tap, "root": sel, "skip": skip})
                    g = pg.evaluate("""([sel, reach]) => { const e = document.querySelector(sel), r = e.getBoundingClientRect(), vw = innerWidth, vh = innerHeight;
                        const nav = document.querySelector('#tabs').getBoundingClientRect(); const btns = [...e.querySelectorAll(reach || 'button')].filter(b => b.checkVisibility());
                        const bad = btns.filter(b => { b.scrollIntoView({ block: 'nearest' }); const q = b.getBoundingClientRect(); const h = document.elementFromPoint(q.left + q.width / 2, q.top + q.height / 2); return !(h && (h === b || b.contains(h))); }).map(b => (b.id || b.textContent.trim().slice(0, 20)));
                        return { l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height, vw, vh, nav: nav.top, navVisible: getComputedStyle(document.querySelector('#tabs')).position === 'fixed', unreachable: bad, nbtn: btns.length, sw: document.documentElement.scrollWidth }; }""", [sel, reach])
                    fits = g["l"] >= -0.5 and g["r"] <= g["vw"] + 0.5 and g["t"] >= -0.5 and g["b"] <= g["vh"] + 0.5 and g["sw"] <= g["vw"]
                    if expect_full: fits &= g["w"] >= g["vw"] - 1 and g["h"] >= g["vh"] - 1
                    check(f"{tag}(e) {label}: fits the {W}x{H} screen{' (full screen)' if expect_full else ''}, no sideways scroll, {g['nbtn']} buttons all reachable" + (", all tap targets >= 44 px, inputs >= 16 px" if phone and tap else ""),
                          fits and not g["unreachable"] and not a["small"] and not a["fonts"],
                          f"box {g['l']:.0f},{g['t']:.0f}-{g['r']:.0f},{g['b']:.0f} of {g['vw']}x{g['vh']}; unreachable={g['unreachable']} small={fmt(a['small'], 4)} fonts={fmt(a['fonts'], 3)}")
                    return g

                # confirm modal (delete a saved profile)
                go("setup"); time.sleep(0.4)
                card_ = ".pcard:has-text('Site A link')"
                if pg.locator(card_ + " .acts [data-act=del]").count(): pg.locator(card_ + " .acts [data-act=del]").click()           # wide screens: Delete is a clear button on the card
                else: pg.locator(card_ + " summary").click(); pg.locator(card_ + " details[open] [data-act=del]").click()                # phones: it is in the ⋮ menu
                pg.wait_for_selector("#modal .box"); time.sleep(0.3)
                dlg("confirm dialog (Delete profile?)", "#modal .box"); shot_ok = pg.screenshot(path=os.path.join(SHOTS, f"mobile-dialog-confirm-{cname.replace('-dark', '')}{'-dark' if theme == 'dark' else ''}.png")) if shots else None
                pg.click("#mNo"); time.sleep(0.2)
                # "Add a Pi by IP" sheet and the Save sheet (New test page)
                pg.click(".pcard:has-text('Site A link') [data-act=pick]"); pg.wait_for_selector("[data-testid=nt-page]"); pg.click("[data-testid=nt-head-ends]"); pg.click("[data-testid=nt-add-pi]"); pg.wait_for_selector("[data-testid=add-pi-sheet]"); time.sleep(0.3)
                dlg("Add a Pi by IP sheet", "#modal .box"); pg.click("[data-testid=add-pi-cancel]"); time.sleep(0.2)
                pg.click("[data-testid=nt-save]"); pg.wait_for_selector("[data-testid=nt-save-sheet]"); time.sleep(0.3)
                dlg("Save sheet (name + description)", "#modal .box"); pg.click("[data-testid=sheet-no]"); time.sleep(0.2)
                pg.click("[data-testid=nt-cancel]"); time.sleep(0.3)
                if pg.locator("#modal:not([hidden]) #mYes").count(): pg.click("#mYes"); time.sleep(0.3)
                # Setup test list (2026-11): info sheet, the ⋮ menu, the Mirrored Bits form, About
                pg.locator(".pcard .meta").first.click(); pg.wait_for_selector("[data-testid=info-sheet]"); time.sleep(0.3)
                dlg("test info sheet (5 sections + Run this test / Edit)", "#modal .box"); pg.click("[data-testid=sheet-close]"); time.sleep(0.2)
                pg.locator(".pcard summary").first.click(); time.sleep(0.3); audit_state("setup/test card ⋮ menu open"); pg.keyboard.press("Escape"); time.sleep(0.2)
                pg.click("[data-testid=setup-create-custom-test]"); pg.wait_for_selector("[data-testid=nt-page]"); pg.click("[data-testid=nt-proto-udp-mb]"); time.sleep(0.5)
                audit_state("setup/new test (Mirrored Bits)"); bar_state("setup/new test (Mirrored Bits)")
                pg.click("[data-testid=nt-cancel]"); time.sleep(0.4)
                if pg.locator("#mYes").count(): pg.click("#mYes"); time.sleep(0.3)
                pg.click("[data-testid=app-version]" if pg.locator("[data-testid=app-version]").is_visible() else "[data-testid=foot-about]"); pg.wait_for_selector("[data-testid=whats-new]"); time.sleep(0.4); audit_state("about"); bar_state("about")
                go("setup")
                # Results 'More' menu + toast
                go("results"); pg.click("#sMore summary"); time.sleep(0.3)
                g = dlg("Results 'More' menu (Rename / Clear...)", "#sMore > div")
                check(f"{tag}(e) the 'More' menu stays above the tab bar", g["b"] <= g["nav"] + 1 or not phone, f"menu bottom {g['b']:.0f}, tab bar top {g['nav']:.0f}")
                pg.click("#sMore summary"); time.sleep(0.2)
                pg.evaluate("import('./ui.js').then(m => { m.toast('Profile saved. This is a long toast message to see how it wraps on a narrow screen.'); m.toast('Something failed: example error', true); })"); time.sleep(0.4)
                t = pg.evaluate("() => { const r = document.querySelector('#toast').getBoundingClientRect(), n = document.querySelector('#tabs').getBoundingClientRect(); return { l: r.left, r: r.right, b: r.bottom, nt: n.top, vw: innerWidth, nfixed: getComputedStyle(document.querySelector('#tabs')).position === 'fixed', sw: document.documentElement.scrollWidth } }")
                check(f"{tag}(e) toasts are inside the screen and not under the bottom tab bar", t["l"] >= -0.5 and t["r"] <= t["vw"] + 0.5 and (t["b"] <= t["nt"] + 1 or not t["nfixed"]) and t["sw"] <= t["vw"], t)
                pg.wait_for_function("document.querySelectorAll('#toast div').length == 0", timeout=8000)
                # Run tab: the Stop confirm while a test is running
                rid2 = trip(f"stopme {cname}", 12, 60, 50); time.sleep(1.0); pg.reload(); pg.wait_for_selector("#tabs [aria-selected=true]"); go("run"); pg.wait_for_selector("[data-testid=tb-start]", timeout=8000)
                pg.click("[data-testid=tb-start]"); pg.wait_for_selector("#modal .box"); time.sleep(0.3)
                dlg("Stop-test confirm dialog (over the fixed Stop bar)", "#modal .box"); pg.click("#mNo"); time.sleep(0.2)
                pg.click("[data-testid=tb-start]"); pg.wait_for_selector("#mYes"); pg.click("#mYes"); wait_run(rid2); time.sleep(0.5)
                # charts follow a rotation: Run/Results chart widths equal their box after the viewport is swapped
                go("results"); pg.locator(".runlist tr[data-run]").first.click(); pg.wait_for_selector("#detail .u-wrap canvas"); time.sleep(0.6)
                pg.set_viewport_size({"width": H, "height": W}); time.sleep(1.0)
                cw = pg.evaluate("() => [...document.querySelectorAll('#detail .chart')].map(c => [Math.round(c.clientWidth), Math.round(c.querySelector('.u-wrap')?.getBoundingClientRect().width || 0)])")
                check(f"{tag}(e) charts re-fit after rotating {W}x{H} -> {H}x{W} (uPlot width == its box)", cw and all(abs(a - b) <= 2 for a, b in cw) and pg.evaluate("document.documentElement.scrollWidth <= innerWidth"), cw)
                pg.set_viewport_size({"width": W, "height": H}); time.sleep(0.8)
                cw = pg.evaluate("() => [...document.querySelectorAll('#detail .chart')].map(c => [Math.round(c.clientWidth), Math.round(c.querySelector('.u-wrap')?.getBoundingClientRect().width || 0)])")
                check(f"{tag}(e) charts re-fit after rotating back to {W}x{H}", cw and all(abs(a - b) <= 2 for a, b in cw), cw)
            check(f"{tag}no console / page errors during the whole sweep of this size", not errs, errs[:3])
            ctx.close()
        br.close()
except BaseException as ex:
    traceback.print_exc(); check("sweep ran to completion", False, repr(ex))
finally:
    for p in procs:
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: pass
    shutil.rmtree(TMP, ignore_errors=True)
    bad = [n for n, ok in results if not ok]
    print(f"# {len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {len(bad)}" if bad else ""), flush=True)
    sys.exit(1 if bad else 0)
