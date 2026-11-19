#!/usr/bin/env python3
"""AI-guide end-to-end (real Chromium, TWO identical apps: pi1 and pi2; there are no roles). Run via scripts/e2e-aiguide.sh (system python3 with playwright).
Same-host run: TT_NO_DEFAULT_REFLECTOR=1 TT_NO_DISCOVERY=1 TT_NO_NET_WATCHDOG=1 (set here) so two apps in one network namespace do not fight over UDP 20500.
Prints 'CHECK name | pass/FAIL | evidence'.

1. EXTRACTION: runs Tag Along's real page-reading code (scripts/tagalong_extractors.js, copied verbatim from its source) on every screen of both Pis and
   on /ai-guide, at page-load time and after the screen has rendered, and asserts the essentials are inside what the AI would receive and
   inside its character cap (Watch/readPage: 15 000 chars from the head; Interactive page text: 2 000; Interactive element map: 150 lines).
2. CONTROL SWEEP: every control the guide names exists on the real screen with that exact label and data-testid, and every actionable element on
   every screen is covered by the guide (both directions).
3. OPERABILITY: follows the guide's recipes using ONLY the labels/selectors printed in /ai-guide.txt (add another Pi by IP; run a test to another Pi on the New test page,
   marking a moment while it runs; read the result and export it; the generator between two Pis; one way / both ways at once; one Pi to two Pis; a custom test saved and run;
   name this Pi and set the time source; the network card), then checks the live state of the guide shows the finished run.
Three identical apps are started (pi1, pi2, pi3): pi3 is only added to pi1's list for the one-Pi-to-two-Pis recipe. Screenshots go to $TT_AIG_SHOTS (default docs/screenshots).
"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time, traceback, urllib.request, urllib.error
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="tt-aig-"); procs, results = [], []
_B = int(os.environ.get("TT_AIG_PORT_BASE", "18340"))          # another e2e script may hold 18340/18341: TT_AIG_PORT_BASE moves all three apps and the test ports
PORTS = {"pi1": _B, "pi2": _B + 1, "pi3": _B + 2}
SHOTS = os.environ.get("TT_AIG_SHOTS") or os.path.join(ROOT, "docs", "screenshots")
os.makedirs(SHOTS, exist_ok=True)
TA_SRC = "/mnt/c/Claude Projects/ai-sidebar-extension"
BANNED = re.compile(r"\bPi [AB]\b|Responder|responder|(?<!Traffic )\bTester\b")           # the old fixed roles: nowhere on a screen or in the guide
VENV = next((c for c in (os.environ.get("TT_VENV_PYTHON"), os.path.join(ROOT, "venv/bin/python"), os.path.expanduser("~/pi-traffic-tester/venv/bin/python")) if c and os.path.exists(c)), sys.executable)


def check(name, ok, ev=""):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:3000]}", flush=True)


def http(port, method, path, body=None, cookie=None, raw=False):
    h = {"Content-Type": "application/json"}
    if cookie: h["Cookie"] = cookie
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as f:
            d = f.read(); return f.status, (d if raw else (json.loads(d) if d else None))
    except urllib.error.HTTPError as e:
        d = e.read()
        try: return e.code, json.loads(d)
        except Exception: return e.code, d


def _cleanup(*_):
    for p in procs:
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: pass
    shutil.rmtree(TMP, ignore_errors=True)
    if _:
        sys.exit(143)


signal.signal(signal.SIGTERM, _cleanup)          # a timeout/kill must not leave stale servers that the next run would silently reuse


def start(name):
    port = PORTS[name]
    try:
        http(port, "GET", "/api/status"); raise RuntimeError(f"port {port} is already in use (stale server from an earlier run?) - refusing to reuse it")
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        if isinstance(e, RuntimeError): raise
    p = subprocess.Popen([VENV, "-m", "traffic_tester", "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{name}"],
                         env=dict(os.environ, PYTHONPATH=ROOT, TT_ASSUME_SYNCED="1", TT_TEST_HOOKS="1", TT_NO_DEFAULT_REFLECTOR="1", TT_NO_DISCOVERY="1", TT_NO_NET_WATCHDOG="1"),
                         stdout=open(f"{TMP}/{name}.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try: http(port, "GET", "/api/status"); break
        except Exception: time.sleep(0.25)
    else:
        raise RuntimeError(f"{name} did not start")
    http(port, "PUT", "/api/pi", {"name": name})
    return p


EXTRACTORS = open(os.path.join(ROOT, "scripts", "tagalong_extractors.js"), encoding="utf-8").read()
CAP_WATCH, CAP_INTERACTIVE_TEXT, CAP_MAP_LINES = 15000, 2000, 150


def inject(page):
    page.evaluate("() => { window.__ta = undefined }")
    page.add_script_tag(content=EXTRACTORS)


def ta_watch(page):          # what Watch / readPage hands to the model
    return page.evaluate("() => window.__ta.extractPageContent()")


def ta_text(page, limit=CAP_INTERACTIVE_TEXT):   # what Interactive mode's observation hands to the model
    return page.evaluate("(l) => window.__ta.extractRichestText(l)", limit)["text"]


def ta_map(page):            # Interactive mode's element map
    return page.evaluate("() => (" + EXTRACTORS.split("elementMap: ", 1)[1].rsplit("\n};", 1)[0].rstrip().rstrip(",") + ")()")


def drift_report():
    """Compare the verbatim copies with the live Tag Along source when it is present (read-only)."""
    try:
        bg = open(f"{TA_SRC}/background.js", encoding="utf-8").read(); pn = open(f"{TA_SRC}/sidepanel/panel.js", encoding="utf-8").read()
    except OSError:
        return "Tag Along source not present here; verbatim copies used"
    norm = lambda s: re.sub(r"\s+", "", s)
    ok = [norm(x) in norm(bg + pn) for x in (re.search(r"extractPageContent: function\(\) \{(.*?)\n\},\nextractRichestText", EXTRACTORS, re.S).group(1)[:300],
                                              re.search(r"extractRichestText: function\(limit\) \{(.*?)\n\},\nelementMap", EXTRACTORS, re.S).group(1)[:300])]
    return "copies match the live Tag Along source (first 300 chars of each body)" if all(ok) else "WARNING: Tag Along source differs from the copies; refresh scripts/tagalong_extractors.js"


def recipes_from_guide(txt):
    """Parse the published guide (plain text) into {id or title: [steps]} using only what it prints."""
    out, cur = {}, None
    for line in txt.splitlines():
        m = re.match(r"Recipe: (.*?)(?:  \[id (\S+)\])?$", line)
        if m: cur = m.group(2) or m.group(1); out.setdefault(cur, []); continue
        if cur and re.match(r"\d+\. ", line):
            out[cur].append(re.sub(r"^\d+\. ", "", line))
        elif cur and line.strip() == "" and out[cur]:
            pass
        elif cur and re.match(r"[A-Z0-9 ]+$", line.strip()) and out[cur]:
            cur = None
    return out


STEP = [
    (re.compile(r'^Open the tab "(.*?)" \(selector \[data-testid="(.*?)"\]\)'), "goto"),
    (re.compile(r'^Click "(.*?)" \(selector \[data-testid="(.*?)"\]\)'), "click"),
    (re.compile(r"^Type '(.*?)' into the field \"(.*?)\" \(selector \[data-testid=\"(.*?)\"\]\)"), "type"),
    (re.compile(r'^In the dropdown "(.*?)" choose "(.*?)" \(selector \[data-testid="(.*?)"\]\)'), "choose"),
]


def pick_option(el, opt):
    """Choose the dropdown entry the way a person would: by the name shown (a Pi is listed as '<name> ✓ · clock synced', 'This Pi (<name>)')."""
    labels = el.evaluate("e => [...e.options].map(o => o.textContent)")
    hit = next((l for l in labels if l == opt), None) or next((l for l in labels if l.startswith(opt)), None)
    assert hit is not None, f"no option {opt!r} in {labels}"
    el.select_option(label=hit)


def is_box(el):
    return el.evaluate("e => e.tagName === 'INPUT' && (e.type === 'radio' || e.type === 'checkbox')")


def tap(el):
    """Tap a control like a person: a tick box / radio button through the label next to it; a box that is already ticked is left alone (the recipes say so)."""
    if is_box(el):
        if not el.is_checked():
            el.locator("xpath=ancestor::label[1]").click()
    else:
        el.click()


def run_steps(page, steps, subs=None):
    """Follow guide steps with ONLY the printed label/selector. Returns list of (step, ok, why). Placeholders in <angle brackets> (a Pi's name in a selector, a typed value) come from subs."""
    subs = subs or {}
    log = []
    for s in steps:
        for k, v in subs.items():
            if k.startswith("<"): s = s.replace(k, v)
        for rx, kind in STEP:
            m = rx.match(s)
            if not m: continue
            try:
                if kind in ("goto", "click"):
                    label, tid = m.group(1), m.group(2)
                    el = page.locator(f'[data-testid="{tid}"]')
                    el.wait_for(state="visible", timeout=8000)
                    txt = (el.inner_text() or "") + " " + (el.get_attribute("aria-label") or "")
                    base = re.sub(r"^[^\w]+|[^\w…)]+$", "", label)
                    assert re.sub(r"\s+", " ", label).strip("() ") in re.sub(r"\s+", " ", txt) or base in txt, f"label {label!r} not on element ({txt.strip()[:60]!r})"
                    tap(el)
                elif kind == "type":
                    val, label, tid = m.group(1), m.group(2), m.group(3)
                    val = subs.get(val, val)
                    el = page.locator(f'[data-testid="{tid}"]'); el.wait_for(state="visible", timeout=8000)
                    assert (el.get_attribute("aria-label") or "").startswith(label), f"aria-label {el.get_attribute('aria-label')!r} != {label!r}"
                    el.fill(str(val)); el.dispatch_event("change")
                elif kind == "choose":
                    label, opt, tid = m.group(1), m.group(2), m.group(3)
                    el = page.locator(f'[data-testid="{tid}"]'); el.wait_for(state="visible", timeout=8000)
                    assert (el.get_attribute("aria-label") or "").startswith(label), f"aria-label {el.get_attribute('aria-label')!r} != {label!r}"
                    pick_option(el, subs.get(opt, opt))
                page.wait_for_timeout(250)
                log.append((s, True, ""))
            except Exception as e:
                log.append((s, False, str(e)[:200]))
            break
        else:
            log.append((s, True, "(observation step, no action)"))
    return log


def idle(port, t=90):
    for _ in range(t * 4):
        if not http(port, "GET", "/api/status")[1]["run"]["running"]: return True
        time.sleep(0.25)
    return False


def banned_on_screen(pg):
    t = pg.evaluate("""() => { const hide = [...document.querySelectorAll('#aiContext, [data-testid=whats-new], [data-testid=updated-banner]')], old = hide.map(e => e.style.display); hide.forEach(e => e.style.display = 'none');
      const t = document.body.innerText + ' ' + [...document.querySelectorAll('[title],[aria-label]')].map(e => (e.title || '') + ' ' + (e.getAttribute('aria-label') || '')).join(' '); hide.forEach((e, i) => e.style.display = old[i]); return t; }""")
    return BANNED.findall(t)


try:
    from traffic_tester.aiguide import content as K, build as B
    for n in ("pi1", "pi2", "pi3"): start(n)
    P1, P2, P3 = PORTS["pi1"], PORTS["pi2"], PORTS["pi3"]
    print("# " + drift_report(), flush=True)
    guide = {n: http(PORTS[n], "GET", "/ai-guide.txt", raw=True)[1].decode() for n in ("pi1", "pi2")}
    guide_a = guide["pi1"]
    check("the guide has NO role words (Pi A / Pi B / Tester / Responder) on either Pi and says which Pi it describes in its title",
          not BANNED.findall(guide["pi1"].replace("/api/responder/", "/api/alias/")) and not BANNED.findall(guide["pi2"].replace("/api/responder/", "/api/alias/"))
          and guide["pi1"].startswith("Pi Automation Traffic Tester - AI guide (pi1, ") and guide["pi2"].startswith("Pi Automation Traffic Tester - AI guide (pi2, "), guide["pi1"].splitlines()[0] + " | " + guide["pi2"].splitlines()[0])
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        ctx = br.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True); page = ctx.new_page()
        errs = []; page.on("pageerror", lambda e: errs.append(str(e))); page.on("console", lambda m: errs.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
        TABS = [k for k, _ in B.tab_labels()]
        LABELS = dict(B.tab_labels())

        # ---------------------------------------------------------------- 1. extraction
        for pi in ("pi1", "pi2"):
            tag = f"[{pi}] "; port = PORTS[pi]
            for tab in TABS:
                for phase in ("at page load", "after render"):
                    page.goto(f"http://127.0.0.1:{port}/#{tab}", wait_until="load")
                    if phase == "after render":
                        page.wait_for_timeout(1200)
                    inject(page)
                    w = ta_watch(page)
                    need = [f"named {pi}", "For AI assistants", "/ai-guide", "AI guide", LABELS["tests"], LABELS["config"], "No test is running"]
                    miss = [n for n in need if n not in w]
                    pos = max(w.find(n) for n in need[:3]) if not miss else -1
                    check(f"{tag}Watch/readPage text on {LABELS[tab]} ({phase}) contains the Pi's name, live state, screen list and the AI guide link", not miss and 0 <= pos < CAP_WATCH, f"missing={miss} furthest essential at char {pos} of cap {CAP_WATCH}; length {len(w)}")
                t = ta_text(page)
                miss = [n for n in (pi, "AI guide") if n not in t]
                check(f"{tag}Interactive page text on {LABELS[tab]} (2000-char cap) contains the Pi's name and the AI guide link", not miss, f"missing={miss}; first 160 chars: {t[:160]!r}")
                mp = ta_map(page)
                check(f"{tag}Interactive element map on {LABELS[tab]} lists the AI guide link and the tabs with labels", "AI guide" in mp and LABELS["results"] in mp and len(mp.splitlines()) <= CAP_MAP_LINES + 1, f"{len(mp.splitlines())} lines")
            page.goto(f"http://127.0.0.1:{port}/ai-guide", wait_until="load"); inject(page)
            w = ta_watch(page)
            ess = [pi, "Live state", "Test running", "Recipe: Run a trip test over serial", "Recipe: Run a test from this Pi to another (round trip)", "Add another Pi to the list", "Start test", "data-testid", "/ai-guide"]
            miss = [e for e in ess if e not in w]
            far = max((w.find(e) for e in ess if e in w), default=-1)
            check(f"{tag}/ai-guide read by Watch: the Pi's name, live state, trip-test recipes, link all inside the 15 000-char cap", not miss and far < CAP_WATCH, f"missing={miss}; furthest essential at char {far}; total extracted {len(w)}")
            t = ta_text(page, 10000)
            check(f"{tag}/ai-guide read by Interactive read_page (10 000 chars) has the Pi's name and live state", pi in t and ("Live state" in t or "LIVE STATE" in t.upper()), f"{len(t)} chars")
            page.screenshot(path=os.path.join(SHOTS, f"ai-guide-{pi}-portrait.png"), full_page=False)
        check("no browser console/page errors while loading every screen", not errs, errs[:3])

        # ---------------------------------------------------------------- 2. control sweep (pi1, before any other Pi is listed)
        page.goto(f"http://127.0.0.1:{P1}/", wait_until="load"); page.wait_for_timeout(800)
        import fnmatch
        def screen_ids():
            return page.evaluate("""() => [...document.querySelectorAll('[data-testid]')].filter(e => e.offsetParent !== null || e.tagName === 'SUMMARY').map(e => ({t: e.dataset.testid, label: e.getAttribute('aria-label') || '', text: (e.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80), tag: e.tagName.toLowerCase()}))""")
        def is_cond(tid): return any(fnmatch.fnmatchcase(tid, c) for c in K.CONDITIONAL)
        def reach(steps):
            page.goto(f"http://127.0.0.1:{P1}/", wait_until="load"); page.wait_for_timeout(500)
            if not steps or not steps[0][1].startswith("tab-"): page.evaluate("location.hash = '#tests'"); page.wait_for_timeout(600)      # screens that start on the test list (Home is the landing page now)
            for s in steps:
                loc = page.locator(f'[data-testid="{s[1]}"]') if s[1] != "tab-config" else page.locator('[data-testid="tab-config"]:visible, [data-testid="tab-config-rail"]:visible').first
                try: loc.wait_for(state="visible", timeout=12000)
                except Exception:
                    print("# DIAG reach failed at", s, "url", page.url, "main text:", page.evaluate("() => document.querySelector('#main').innerText.slice(0, 160)"), "errs", errs[-2:], flush=True); raise
                if s[0] == "click": tap(loc)
                elif s[0] == "type": loc.fill(s[2]); loc.dispatch_event("change")
                elif s[0] == "choose": loc.select_option(label=s[2])
                page.wait_for_timeout(600 if "sniffer" in s[1] else 250)
            page.wait_for_timeout(1200 if (steps and steps[0][1] == "tab-config") else 900)
        def reach_cfg(row):
            page.locator('[data-testid="tab-config"]:visible, [data-testid="tab-config-rail"]:visible').first.click(); page.wait_for_selector(f'[data-testid="{row}"]'); page.locator(f'[data-testid="{row}"]').click()
        def view_ids(v):
            ids = {}
            for steps in [v["reach"]] + v.get("variants", []):
                reach(steps)
                for e in screen_ids(): ids.setdefault(e["t"], e)
            return list(ids.values())
        def wild(tid):
            e = re.escape(tid).replace("\\<", "<").replace("\\>", ">").replace("<n>", r"\d+")
            return re.compile("^" + re.sub(r"<[^>]+>", ".+", e) + "$")
        registry = [c for v in K.VIEWS.values() for c in v["controls"]] + K.TOP_CONTROLS + [("foot-ai-guide", "AI guide", ""), ("foot-about", "About", ""), ("ai-context-guide", "AI guide", "")] + [(f"tab-{k}", l, "") for k, l in B.tab_labels()]
        rx = [wild(t) for t, _, _ in registry]
        uncovered_all = []
        SKIP_SWEEP = ("run-live", "ask", "dialogs")
        for vid, v in K.VIEWS.items():
            if vid in SKIP_SWEEP:
                continue
            ids = view_ids(v); by = {e["t"]: e for e in ids}
            bad = []
            for tid, label, _ in v["controls"]:
                if "<" in tid:
                    m = [e for e in ids if wild(tid).match(e["t"])]
                    if not m and not is_cond(tid): bad.append(f"{tid} (no element)")
                    continue
                e = by.get(tid)
                if not e:
                    if is_cond(tid): continue                              # only shown in some states; swept again in the state that shows it
                    bad.append(f"{tid} (missing)"); continue
                lab = re.sub(r"\s+", " ", label).strip()
                got = (e["label"] or e["text"])
                if lab.lower() not in (e["text"] + " " + e["label"]).lower() and re.sub(r"^[^\w]+", "", lab) not in got:
                    bad.append(f"{tid}: label {lab!r} != {got!r}")
            check(f"guide controls of '{v['title']}' exist with their exact label and selector", not bad, bad[:4] or f"{len(v['controls'])} controls")
            unc = [e["t"] for e in ids if not any(r.match(e["t"]) for r in rx)
                   and not re.match(r"^(setup|generator|sniffer)-(duplicate-to-edit|duplicate|edit|export|delete)--", e["t"]) and not re.search(r"-help(-\d+)?$", e["t"]) and not e["t"].startswith("setup-profile-file")]
            uncovered_all += [(vid, u) for u in unc]
        missing_tid = []
        for vid, v in K.VIEWS.items():
            if vid in SKIP_SWEEP: continue
            reach(v["reach"])
            n = page.evaluate("""() => [...document.querySelectorAll('#main button, #main a[href], #main input:not([type=hidden]), #main select, #main textarea, #main summary, #main [role=button]')].filter(e => !e.dataset.testid).map(e => e.outerHTML.slice(0, 80))""")
            missing_tid += n
        check("no actionable element in a screen lacks a data-testid", not missing_tid, missing_tid[:3])
        check("guide covers every control id found on the swept screens (ids outside the guide's lists)", not uncovered_all, sorted(set(u for _, u in uncovered_all))[:40])
        # role words: none on any screen of either Pi
        seen = {}
        for port, nm in ((P1, "pi1"), (P2, "pi2")):
            for tab in TABS:
                page.goto(f"http://127.0.0.1:{port}/#{tab}", wait_until="load"); page.wait_for_timeout(900)
                w = banned_on_screen(page)
                if w: seen[f"{nm}/{tab}"] = sorted(set(w))
        check("no role words (Pi A / Pi B / Tester / Responder) on any screen of either Pi", not seen, seen)

        # the network card: every id the guide names is really in static/network.js (the sweep cannot reach the apply / keep flow without the privileged helper)
        net_js = open(os.path.join(ROOT, "traffic_tester", "static", "network.js"), encoding="utf-8").read()
        def in_js(tid):
            pat = r"(?:\$\{[^}]*\}|[\w-]+)".join(re.escape(x) for x in tid.split("<iface>"))
            if re.search(pat, net_js): return True
            if "<iface>" in tid: return tid.split("<iface>")[0] in net_js                       # built by string concatenation ('net-you-' + esc(i))
            return bool(re.match(r"net-wifi-mode-(ap|client|off)$", tid)) and "net-wifi-mode-${" in net_js
        netctl = [t for v in K.VIEWS.values() for t, _, _ in v["controls"] if t.startswith(("net-", "settings-what-do"))]
        miss = [t for t in netctl if t.startswith("net-") and not in_js(t)]
        check("every net-* id the guide names for the Network card exists in static/network.js (Keep / Revert / confirm / Wi-Fi / port fields)", not miss and len(netctl) >= 30, miss[:5] or f"{len(netctl)} ids")

        # ---------------------------------------------------------------- 3. operability: follow recipes from the published guide only
        rec = recipes_from_guide(guide_a)
        subs0 = {"<name of the other Pi>": "pi2"}
        def fresh():
            page.goto(f"http://127.0.0.1:{P1}/", wait_until="load"); page.wait_for_timeout(700)
        def shorten(secs=6):          # the guide's own 'Change the limits' steps (row 4 Traffic), with the length cut to a few seconds
            steps = [s for s in rec["change-limits"] if "nt-head-traffic" in s or "setup-test-length" in s]
            return run_steps(page, steps, subs={"60": str(secs)})
        def start_line(r):
            return next(s for s in r if 'selector [data-testid="tb-start"]' in s)
        def failures(log): return [(s, w) for s, ok, w in log if not ok]

        # --- RECIPE 1: add another Pi to the list
        fresh()
        r1 = rec.get("add-a-pi", [])
        log = run_steps(page, r1, subs={"<IP address of the other Pi>": f"127.0.0.1:{P2}"})
        bad = failures(log)
        page.wait_for_timeout(1200)
        peers = http(P1, "GET", "/api/peers")[1]["peers"]
        check("RECIPE 'Add another Pi to the list' followed on pi1 from the guide only (New test page, row 3) -> pi2 is in the list and in the 'To' tick list (nt-far-pi2) as '✓ answers · clock synced'",
              not bad and [p["name"] for p in peers] == ["pi2"] and page.locator('[data-testid="nt-far-pi2"]').count() == 1 and "clock synced" in page.locator('[data-testid="nt-far-list"]').inner_text(),
              f"{len(r1)} steps failures={bad[:2]} peers={[p['name'] for p in peers]} list={page.locator('[data-testid=nt-far-list]').inner_text()[:80]!r}")
        check("   ...and, pi2 being the only other Pi, it is ticked for us (nt-sentence names it)", page.locator('[data-testid="nt-far-pi2"]').is_checked() and "pi1 sends to pi2" in page.locator('[data-testid="nt-sentence"]').inner_text(), page.locator('[data-testid="nt-sentence"]').inner_text() if page.locator('[data-testid="nt-sentence"]').count() else "no sentence")

        # --- RECIPE 2: run a test from this Pi to another (round trip), and mark a moment while it runs
        fresh()
        r2 = rec.get("run-trip-ethernet", [])
        pre = [s for s in r2 if s != start_line(r2)]
        bad = failures(run_steps(page, pre, subs=subs0))
        sentence = page.locator('[data-testid="nt-sentence"]').inner_text() if page.locator('[data-testid="nt-sentence"]').count() else ""
        bad += failures(shorten(20))
        bad += failures(run_steps(page, [start_line(r2)]))
        page.wait_for_timeout(1500)
        runb = page.locator(".banner").first.inner_text() if page.locator(".banner").count() else ""
        check("RECIPE 'Run a test from this Pi to another (round trip)' followed on pi1 from the guide only (＋ New test, Automatic, UDP, tick pi2, ▶ Start) -> the Run screen shows RUNNING", not bad and "RUNNING" in runb and "round trip" in sentence, f"steps={len(r2)} failures={bad[:2]} banner={runb[:40]!r} sentence={sentence!r}")
        ai_live = http(P1, "GET", "/api/ai-context")[1]["live"]
        check("   ...the guide's live state says a test is RUNNING while it runs", "RUNNING" in ai_live and "pi1" in ai_live, ai_live)
        page.screenshot(path=os.path.join(SHOTS, "ai-guide-screen-with-context-run.png"))
        # recipe: mark a moment during a test (the test above is still running)
        rm = rec.get("mark-a-moment", [])
        logm = run_steps(page, rm, subs={"<what happened, e.g. antenna moved>": "antenna moved"})
        badm = failures(logm)
        since = page.locator('[data-testid="run-since-mark"]').count() == 1 and page.locator('[data-testid="run-since-whole"]').count() == 1
        sm_txt = page.locator('[data-testid="run-since-mark"]').inner_text() if page.locator('[data-testid="run-since-mark"]').count() else ""
        check("RECIPE 'Mark a moment during a test' followed from the guide only while the test runs -> the mark pop-up took the name, 'Since ⚑1' / 'Whole test' appeared", not badm and since and "⚑1" in sm_txt, f"steps={len(rm)} failures={badm[:2]} since={since} {sm_txt!r}")
        lamp = ""
        for _ in range(30):
            lamp = page.locator('[data-testid="run-lamp"]').inner_text() if page.locator('[data-testid="run-lamp"]').count() else ""
            if lamp.strip(): break
            page.wait_for_timeout(500)
        check("RECIPE 'Read the tester lamp': the lamp line under the banner (run-lamp) has text while the test runs and its title is one of the three the guide names", bool(lamp.strip()) and any(t in lamp for t in ("Pi OK", "Check the Pi", "The Pi is the limit")), repr(lamp[:120]))
        idle(P1)
        page.wait_for_timeout(800)
        banner = page.locator(".banner").first.inner_text()
        run1 = http(P1, "GET", "/api/runs")[1][0]
        NAME = run1["name"]
        check("   ...the test finishes with PASS, sent from pi1 to pi2 (the stored run names both Pis)", "PASS" in banner and run1["leg"]["tester"] == "pi1" and run1["leg"]["reflector"] == "pi2", f"{banner[:40]!r} leg={run1.get('leg')} name={NAME!r}")
        full1 = http(P1, "GET", f"/api/runs/{run1['id']}")[1]
        marks = (full1.get("summary") or {}).get("marks") or []
        st, csv = http(P1, "GET", f"/api/runs/{run1['id']}/export.csv?kind=messages", raw=True)
        head = csv.decode().splitlines()[0] if st == 200 else ""
        check("   ...the mark is stored (name 'antenna moved'), is the last column 'mark' of the Messages CSV, and the result has the tester line", len(marks) == 1 and marks[0].get("name") == "antenna moved" and head.split(",")[-1].strip() == "mark" and bool((full1.get("summary") or {}).get("tester")), f"marks={marks} csv-head={head[-40:]!r} tester={(full1.get('summary') or {}).get('tester')}")

        # --- RECIPE 3: read a result and export it
        r3 = rec.get("read-results-and-export", [])
        page.goto(f"http://127.0.0.1:{P1}/#results", wait_until="load"); page.wait_for_timeout(1200)
        dl = []
        page.on("download", lambda d: dl.append(d.suggested_filename))
        log = run_steps(page, [s for s in r3 if "Session" not in s and "USB" not in s and "ZIP" not in s])
        page.wait_for_timeout(1200)
        log += run_steps(page, [s for s in r3 if "ZIP" in s])
        page.wait_for_timeout(1200)
        bad = failures(log)
        check("RECIPE 'Read a result and export it' followed from the guide only -> CSV, PDF and ZIP downloads happen", not bad and any(f.endswith(".csv") for f in dl) and any(f.endswith(".pdf") for f in dl) and any(f.endswith(".zip") for f in dl), f"failures={bad[:2]} downloads={dl}")
        hdr = page.locator('[data-testid="result-ends-label"]').inner_text() if page.locator('[data-testid="result-ends-label"]').count() else ""
        check("   ...the opened result's header names who sent to whom (result-ends-label)", hdr == "pi1 → pi2", hdr)
        mk = page.locator('[data-testid="result-marks"]').inner_text() if page.locator('[data-testid="result-marks"]').count() else ""
        tl = page.locator('[data-testid="result-tester"]').inner_text() if page.locator('[data-testid="result-tester"]').count() else ""
        check("   ...the result shows the marks line (result-marks: '⚑1 at … · antenna moved') and the tester line (result-tester)", "⚑1" in mk and "antenna moved" in mk and bool(tl.strip()), f"marks={mk!r} tester={tl[:80]!r}")

        # --- RECIPE 4: the generator between two Pis, one stream each way
        fresh()
        r4 = rec.get("run-generator", [])
        pre4 = [s for s in r4 if 'selector [data-testid="tb-start"]' not in s]
        log = run_steps(page, pre4, subs={"<name of the sending Pi>": "This Pi", "<name of the receiving Pi>": "pi2", "<name of the second sending Pi>": "pi2", "<name of the second receiving Pi>": "This Pi", "30": "5"})
        bad = failures(log)
        sl = [s for s in r4 if 'selector [data-testid="tb-start"]' in s][0]
        bad += failures(run_steps(page, [sl]))
        page.wait_for_timeout(1500)
        gen_ui = [page.locator('[data-testid="tb-mark"]').count(), page.locator('[data-testid="run-lamp"]').count()]
        idle(P1, 60); page.wait_for_timeout(1500)
        gr = http(P1, "GET", "/api/runs")[1][0]
        full = http(P1, "GET", f"/api/runs/{gr['id']}")[1]
        pairs = sorted((x["src"], x["dst"]) for x in (full.get("summary") or {}).get("pairs", []))
        page.locator('[data-testid="generator-see-full-results"]').click(); page.wait_for_selector('[data-testid="gen-pairs"]', timeout=15000)
        gp = page.locator('[data-testid="gen-pairs"]').count()
        check("RECIPE 'Run the generator between several Pis (N → M)' followed on pi1 from the guide only -> two streams (pi1 → pi2 and pi2 → pi1) ran and the result has a 'Per pair of Pis' table",
              not bad and gr["status"] == "finished" and pairs == [("pi1", "pi2"), ("pi2", "pi1")] and gp == 1, f"steps={len(r4)} failures={bad[:2]} status={gr['status']} pairs={pairs} gen-pairs={gp}")
        check("   ...the generator's run view showed 'Mark' (tb-mark in the transport bar) and the tester lamp line (run-lamp), as the guide says", gen_ui == [1, 1], gen_ui)

        # --- RECIPE 5: one way / both ways at once
        fresh()
        r5 = rec.get("one-way-both-ways", [])
        pre5 = [s for s in r5 if s != start_line(r5)]
        log = run_steps(page, pre5, subs=subs0)
        bad = failures(log)
        both_ok = page.locator('[data-testid="nt-dir-both"]').get_attribute("aria-checked") == "true"
        page.wait_for_selector('[data-testid="nt-sentence"]:has-text("send to each other")', timeout=20000)
        bad += failures(shorten(6))
        bad += failures(run_steps(page, [start_line(r5)]))
        rows = 0
        try:
            page.wait_for_selector('[data-testid="run-links"]', timeout=15000); page.wait_for_timeout(800); rows = page.locator('[data-testid="run-leg-card"]').count()
        except Exception: pass
        idle(P1); page.wait_for_timeout(1000)
        runs = http(P1, "GET", "/api/runs")[1][:2]
        banner = page.locator("#runBanner").inner_text() if page.locator("#runBanner").count() else ""
        lb = page.locator('[data-testid="run-links-banner"]').inner_text() if page.locator('[data-testid="run-links-banner"]').count() else ""
        check("RECIPE 'One way or both ways at once' followed on pi1 from the guide only -> 'Both ways' ran: two stored runs share a group id and Run shows ONE links table (run-links) with a row per link (run-leg-card)",
              not bad and both_ok and len(runs) == 2 and runs[0]["group_id"] and runs[0]["group_id"] == runs[1]["group_id"] and rows == 2 and "All 2 links meet the requirement" in banner,
              f"failures={bad[:2]} both-selected={both_ok} rows={rows} links-banner={lb!r} runs={[(r['id'], r.get('group_id'), r['verdict']) for r in runs]} banner={banner[:60]!r}")
        page.goto(f"http://127.0.0.1:{P1}/#results", wait_until="load"); page.wait_for_selector('[data-testid^="results-group-"]', timeout=15000); page.wait_for_timeout(500)
        grp = page.locator('[data-testid^="results-group-"]').first
        ends = page.locator('[data-testid="result-ends"]').first.inner_text()
        grp.click(); page.wait_for_timeout(700)
        subrows = page.locator('[data-testid^="results-row-direction-"]').count()
        links_tbl = page.locator('[data-testid="result-links"]').count(), page.locator('[data-testid^="result-link-"]').count(), page.locator('[data-testid="result-links-banner"]').inner_text() if page.locator('[data-testid="result-links-banner"]').count() else ""
        page.locator('[data-testid^="results-row-direction-"]').nth(1).click(); page.wait_for_timeout(900)
        line = page.locator('[data-testid="result-ends-line"]').inner_text()
        other = page.locator('[data-testid^="result-link-"]:not(.sel)').count()
        page.locator('[data-testid^="result-link-"]:not(.sel)').first.click(); page.wait_for_timeout(900)
        pdfboth = page.locator('[data-testid="result-pdf-both"]').count()
        check("Results shows the both-ways test as ONE entry (results-group-<n>, 'pi1 ⇄ pi2 · Both ways at once') that opens to one line per link (results-row-direction-<n>), the result starts with the links table (result-links, result-link-<id> rows, banner), the header says 'one of 2 links' (result-ends-line), a click on another row opens that link and 'PDF report with every link' exists",
              "pi1 ⇄ pi2" in ends and subrows == 2 and links_tbl[0] == 1 and links_tbl[1] == 2 and "2 links" in links_tbl[2] and "one of 2 links" in line and other == 1 and pdfboth == 1,
              f"ends={ends!r} sub={subrows} links-table={links_tbl} line={line[:70]!r} other-rows={other} pdf-both={pdfboth}")

        # --- RECIPE 5b: one Pi to two Pis (pi3 joins pi1's list through the API; the recipe then ticks both)
        http(P1, "POST", "/api/peers", {"host": f"127.0.0.1:{P3}"}); page.wait_for_timeout(800)
        fresh()
        r5b = rec.get("run-one-to-many", [])
        pre5b = [s for s in r5b if s != start_line(r5b)]
        log = run_steps(page, pre5b, subs={"<name of the other Pi>": "pi2", "<name of a second Pi>": "pi3"})
        bad = failures(log)
        sent2 = page.locator('[data-testid="nt-sentence"]').inner_text() if page.locator('[data-testid="nt-sentence"]').count() else ""
        ends2 = page.locator('[data-testid="nt-line-ends"]').inner_text()
        bad += failures(shorten(6))
        # same-host only: pi2 and pi3 share this PC's loopback, so two reflectors would fight over one UDP port; give each link its own test port the way scripts/e2e_newtest.py does
        page.evaluate("async ([a, b]) => { const m = await import('/newtest.js'); const p = m.ntState.c.p; p.dest.port = a; p.dests[0].port = b; }", [_B + 560, _B + 561])
        page.wait_for_timeout(300); page.locator('[data-testid="nt-head-traffic"]').click(); page.wait_for_timeout(300); page.locator('[data-testid="nt-head-traffic"]').click(); page.wait_for_timeout(300)
        page.evaluate("async ([a, b]) => { const m = await import('/newtest.js'); const p = m.ntState.c.p; p.dest.port = a; p.dests[0].port = b; }", [_B + 560, _B + 561])
        page.wait_for_function("() => { const b = document.querySelector('[data-testid=tb-start]'); return b && !b.disabled; }", timeout=20000)
        bad += failures(run_steps(page, [start_line(r5b)]))
        rows = 0
        try:
            page.wait_for_selector('[data-testid="run-links"]', timeout=15000); page.wait_for_timeout(800); rows = page.locator('[data-testid="run-leg-card"]').count()
        except Exception: pass
        idle(P1); page.wait_for_timeout(1000)
        runs = http(P1, "GET", "/api/runs")[1][:2]
        banner = page.locator("#runBanner").inner_text() if page.locator("#runBanner").count() else ""
        legs = sorted((r["leg"]["tester"], r["leg"]["reflector"]) for r in runs)
        check("RECIPE 'One Pi to two or more Pis' followed on pi1 from the guide only (tick pi2 and pi3) -> the sentence says '2 links', ONE test started two runs (pi1 → pi2, pi1 → pi3, one group id), Run shows ONE table with two rows and the banner counts the links",
              not bad and "2 links" in sent2 and "2 links" in ends2 and len(runs) == 2 and runs[0]["group_id"] and runs[0]["group_id"] == runs[1]["group_id"] and legs == [("pi1", "pi2"), ("pi1", "pi3")] and rows == 2 and "All 2 links meet the requirement" in banner,
              f"failures={bad[:2]} sentence={sent2!r} ends={ends2!r} rows={rows} legs={legs} banner={banner[:90]!r} reasons={[ (http(P1, 'GET', '/api/runs/%d' % r['id'])[1].get('summary') or {}).get('verdict') for r in runs]}")
        page.goto(f"http://127.0.0.1:{P1}/#results", wait_until="load"); page.wait_for_selector('[data-testid^="results-group-"]', timeout=15000); page.wait_for_timeout(500)
        gl = page.locator('[data-testid="result-ends"]').first.inner_text()
        check("   ...Results lists it as ONE entry 'pi1 → pi2, pi3 · 2 links' (result-ends)", gl == "pi1 → pi2, pi3 · 2 links", gl)

        # --- RECIPE 6: create a custom test (saved), then run it from its card
        fresh()
        r6 = rec.get("create-custom-test", [])
        log = run_steps(page, r6, subs={"<a name for the test>": "Guide recipe test", **subs0})
        bad = failures(log)
        page.wait_for_timeout(1800)
        made = next((p for p in http(P1, "GET", "/api/profiles")[1] if p["name"] == "Guide recipe test"), None)
        pid2 = next(p["id"] for p in http(P1, "GET", "/api/peers")[1]["peers"] if p["name"] == "pi2")
        dests = json.dumps((made or {}).get("dests") or (made or {}).get("dest") or {})
        check("RECIPE 'Create your own test from scratch' followed on pi1 from the guide only (＋ New test, UDP, pi2, row 4 values, 💾 Save + name) -> a saved test with the typed values and pi2 as the far Pi, and its card is on the list",
              not bad and made and made["transport"] == "udp" and made["interval_ms"] == 50 and made["duration_s"] == 600 and made["threshold_ms"] == 60 and not made["builtin"] and pid2 in dests and page.locator('[data-testid="setup-run--guide-recipe-test"]').count() == 1,
              f"steps={len(r6)} failures={bad[:2]} dests={dests[:120]} made={json.dumps(made)[:160] if made else None}")
        page.locator('[data-testid="setup-run--guide-recipe-test"]').click(); page.wait_for_selector('[data-testid="nt-checks"] .ntck.ok', timeout=20000)
        chips = page.locator('[data-testid="nt-checks"]').inner_text().lower()
        check("   ...▶ Run on that card opens the New test page; the check ran by itself (chip 'pi2 answered') and START (tb-start) is enabled", "pi2 answered" in chips and page.locator('[data-testid="tb-start"]').is_enabled(), chips)
        if made: http(P1, "DELETE", f"/api/profiles/{made['id']}")

        # ---------------------------------------------------------------- state-dependent controls (a finished run, another Pi listed, a saved test, a password)
        page.locator('[data-testid="tab-results"]').click()
        try: page.wait_for_selector('[data-testid^="results-row-"], [data-testid^="results-group-"]', timeout=15000)
        except Exception:
            print("# DIAG results rows missing; main text:", page.evaluate("() => document.querySelector('#main').innerText.slice(0, 400)"), flush=True); raise
        page.locator('[data-testid^="results-group-"], [data-testid^="results-row-"]').first.click(); page.wait_for_timeout(700)
        ids = {e["t"]: e for e in screen_ids()}
        need = [t for t, _, _ in K.VIEWS["results"]["controls"] if "<" not in t and not is_cond(t)]
        miss = [t for t in need if t not in ids]
        check("Results screen with finished runs: every control the guide names is present (list, exports, delete)", not miss, f"missing={miss}")
        normal = next((r for r in http(P1, "GET", "/api/runs")[1] if not r.get("group_id") and r["name"] == NAME), None)
        page.locator(f'[data-testid="results-row-{normal["id"]}"]').click(); page.wait_for_timeout(900)
        ids = {e["t"]: e for e in screen_ids()}
        have = [t for t in ("results-messages-csv", "results-summary-per-second-csv", "results-everything-json", "results-pdf-report-2", "results-delete-this-test", "result-header", "result-ends-label", "result-ends-line") if t in ids]
        check("Results: an opened single test shows its header (who sent to whom) and the export row the guide names", len(have) == 8, have)
        reach([("click", "tab-config"), ("click", "setup-pis")]); page.wait_for_selector('[data-testid="peer-row"]', timeout=15000); page.wait_for_timeout(500)
        ids = {e["t"] for e in screen_ids()}
        want = ["peers-list", "peer-row", "peer-name", "peer-address", "peer-reachable", "peer-clock", "peer-open", "peer-password", "peer-clock-source", "peer-update", "peer-remove", "peer-add-host", "peer-add-go"]
        check("Settings with another Pi listed shows every row control the guide names (peer-*)", all(t in ids for t in want) and all(any(t == c[0] for v in K.VIEWS.values() for c in v["controls"]) for t in want), [t for t in want if t not in ids])
        reach([("click", "setup-create-custom-test")])
        tap(page.locator('[data-testid="nt-far-pi2"]')); page.wait_for_timeout(800)
        ids = {e["t"] for e in screen_ids()}
        check("New test page with listed Pis shows the 'To' tick list (nt-far-pi2, nt-far-pi3), the sentence (nt-sentence), the direction control, and no typed-address box and no empty-list note until '＋ Type an IP address'",
              {"nt-far-list", "nt-far-pi2", "nt-far-pi3", "nt-sentence", "nt-dir-one_way", "nt-dir-round_trip", "nt-dir-both", "nt-swap", "nt-refresh-pis", "nt-add-pi", "nt-type-ip"} <= ids and "nt-far-host" not in ids and "nt-no-pis" not in ids, sorted(t for t in ids if t.startswith("nt-"))[:30])
        page.locator('[data-testid="nt-type-ip"]').click(); page.wait_for_timeout(400)
        check("   ...'＋ Type an IP address' adds the box nt-far-host", page.locator('[data-testid="nt-far-host"]').count() == 1, "")
        page.locator('[data-testid="nt-dir-one_way"]').click(); page.wait_for_timeout(400)
        page.locator('[data-testid="nt-swap"]').click(); page.wait_for_timeout(500)
        sw = page.locator('[data-testid="nt-sentence"]').inner_text() if page.locator('[data-testid="nt-sentence"]').count() else ""
        check("   ...'⇅ Swap' turns it round: the far Pi sends to this Pi (nt-sentence)", "sends to pi1" in sw or "send to pi1" in sw, sw)
        http(P1, "POST", "/api/profiles/1/duplicate")
        reach([("click", "tab-tests")])
        names = [e["t"] for e in screen_ids() if e["t"].startswith("setup-delete--")]
        check("Setup with a saved test shows 'Delete' for it (setup-delete--<test name>)", bool(names), names[:2])

        # --- RECIPE 7: name this Pi / set the time source
        page.goto(f"http://127.0.0.1:{P1}/", wait_until="load"); page.wait_for_timeout(700)
        r7 = rec.get("name-pi-and-time-source", [])
        log = run_steps(page, r7, subs={"<new name, e.g. substation-1>": "substation-1", "This Pi (<this Pi's name>)": "This Pi"})
        bad = [(s, w) for s, ok, w in log if not ok]
        page.wait_for_timeout(900)
        me = http(P1, "GET", "/api/pi")[1]; cs = http(P1, "GET", "/api/clock/source")[1]
        page.locator('[data-testid="tab-tests"]').click(); page.wait_for_timeout(900)
        top = page.locator('[data-testid="top-pi-name"]').inner_text()
        check("RECIPE 'Name this Pi / set the time source' followed from the guide only -> the Pi is renamed (top bar, API) and its time source is 'This Pi'", not bad and me["name"] == "substation-1" and top == "substation-1" and cs.get("setting") in ("self", me["id"]), f"steps={len(r7)} failures={bad[:2]} name={me['name']} top={top} setting={cs.get('setting')}")
        http(P1, "PUT", "/api/pi", {"name": "pi1"}); http(P1, "PUT", "/api/clock/source", {"source": "auto"})

        # --- RECIPE 8: network settings (what can be followed without the privileged helper: the Wi-Fi / port forms; the apply + Keep flow is in e2e_netcfg_ui.py)
        page.goto(f"http://127.0.0.1:{P1}/", wait_until="load"); page.wait_for_timeout(700)
        r8 = rec.get("network-settings", [])
        upto = [s for s in r8 if "Apply Wi-Fi settings" not in s]
        log = run_steps(page, upto)
        bad = [(s, w) for s, ok, w in log if not ok]
        page.wait_for_timeout(700)
        ids = {e["t"] for e in screen_ids()}
        ap = {"net-ssid", "net-password", "net-band", "net-channel"} <= ids
        page.locator('[data-testid="net-wifi-mode-client"]').evaluate("e => e.closest('label').click()"); page.wait_for_timeout(400)
        ids = {e["t"] for e in screen_ids()}
        cl = {"net-scan", "net-ssid", "net-password", "net-wifi-hidden"} <= ids
        iface = page.evaluate("() => { const b = document.querySelector('.net-port[data-iface]'); return b ? b.dataset.iface : null }")
        static_ok = False
        if iface:
            page.locator(f'[data-testid="net-port-method-{iface}-static"]').evaluate("e => e.closest('label').click()"); page.wait_for_timeout(400)
            ids = {e["t"] for e in screen_ids()}
            static_ok = all(f"net-port-{k}-{iface}" in ids for k in ("ip", "mask", "gw", "dns", "vlan", "label", "apply")) or all(f"net-port-{k}-{iface}" in ids for k in ("ip", "mask", "gw", "dns", "vlan", "label")) and f"net-port-apply-{iface}" in ids
        check("RECIPE 'Network: ...' followed from the guide as far as this install allows -> the access point form (name, password, band, channel), the join form (scan, hidden), and a port's static fields (IP, mask, gateway, DNS, VLAN, label) are where the guide says",
              not bad and ap and cl and static_ok and "net-wifi-apply" in ids and "net-fallback" in ids, f"failures={bad[:2]} ap={ap} join={cl} static={static_ok} iface={iface}")

        # --- password: top bar + the guide's login rule
        reach_cfg("setup-password"); page.wait_for_selector('[data-testid="settings-password"]', timeout=15000); page.wait_for_timeout(600)
        for t, v in (("settings-password", "pw-aig-1234"),):
            page.locator(f'[data-testid="{t}"]').fill(v)
        page.locator('[data-testid="settings-set-password"]').click(); page.wait_for_timeout(1500)
        ids = {e["t"]: e for e in screen_ids()}
        check("Settings with a password set shows 'Remove password' (settings-remove-password) and 'Change password'", "settings-remove-password" in ids and "Change password" in (ids.get("settings-change-password", {}).get("text", "") or ids.get("settings-set-password", {}).get("text", "") or ""), [t for t in ids if "password" in t])
        page.locator('[data-testid="tab-tests"]').click(); page.wait_for_timeout(900)
        ids2 = {e["t"]: e for e in screen_ids()}
        check("top bar with a password set (from the next screen on): only the Pi's name plus 'Signed in' and 'Sign out' (top-pi-name, top-signed-in, top-sign-out), as the guide says",
              ids2.get("top-pi-name", {}).get("text") == "pi1" and ids2.get("top-signed-in", {}).get("text") == "Signed in" and ids2.get("top-sign-out", {}).get("text") == "Sign out", [(t, ids2[t]["text"]) for t in ids2 if t in ("top-pi-name", "top-signed-in", "top-sign-out")])
        reach_cfg("setup-password"); page.wait_for_selector('[data-testid="settings-remove-password"]', timeout=15000); page.wait_for_timeout(500)
        g_locked = ctx.request.get(f"http://127.0.0.1:{P1}/ai-guide.txt").text()
        check("password set + this browser signed in (cookie): the guide shows the live state", "Test running:" in g_locked and "LIVE STATE" in g_locked and "Hidden: this app is password-protected" not in g_locked and "Last result: #" in g_locked, g_locked[g_locked.find("LIVE STATE"):g_locked.find("LIVE STATE") + 160].replace("\n", " "))
        anon = http(P1, "GET", "/ai-guide.txt", raw=True)[1].decode()
        check("   ...cookie-less request: live state hidden (same login rule as the app), no host-specific facts", "LIVE STATE" in anon and "Hidden: this app is password-protected" in anon and "Test running:" not in anon and "Last result" not in anon.split("3. QUICK START")[0].split("LIVE STATE")[1], anon[anon.find("LIVE STATE"):anon.find("LIVE STATE") + 220].replace("\n", " "))
        page.locator('[data-testid="settings-remove-password"]').click(); page.wait_for_timeout(500)
        dlg = [(e["t"], e["text"]) for e in screen_ids() if e["t"].startswith("dialog-")]
        check("confirmation dialog buttons have ids (dialog-cancel + action) and the guide's 'Cancel' label", any(t == "dialog-cancel" and tx == "Cancel" for t, tx in dlg), dlg)
        page.locator('[data-testid="dialog-remove"]').click(); page.wait_for_timeout(1200)
        check("   ...'Remove' in that dialog removes the password (guide recipe state restored)", http(P1, "GET", "/api/status")[1]["password_enabled"] is False, "")
        g2 = http(P1, "GET", "/ai-guide.txt", raw=True)[1].decode()
        m = re.search(r"Last result: (#\d+.*)", g2)
        check("guide live state shows the finished run (name, verdict, p99, lost)", bool(m) and NAME in m.group(1) and "verdict PASS" in m.group(1) and "p99" in m.group(1) and "lost 0 of" in m.group(1), m.group(1)[:200] if m else "no 'Last result' line")
        check("guide live state lists pi2 with its address and reachability, the reflector and the time source", f"pi2 at 127.0.0.1:{P2} reachable" in g2 and "Reflector (" in g2 and "Time source setting:" in g2, g2[g2.find("Other Pis this Pi knows"):g2.find("Other Pis this Pi knows") + 160].replace("\n", " "))
        page.locator('[data-testid="tab-tests"]').click(); page.wait_for_timeout(1500)
        page.screenshot(path=os.path.join(SHOTS, "ai-guide-screen-with-context-landscape.png"), full_page=True)
        w = ta_watch(page) if (inject(page) or True) else ""
        check("after the test, Watch text carries 'Last result' PASS in the page note", f"Last result: '{NAME}' PASS" in w, w[w.find("For AI"):w.find("For AI") + 420])
        ctx2 = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2); p2 = ctx2.new_page()
        p2.goto(f"http://127.0.0.1:{P1}/#results", wait_until="load"); p2.wait_for_timeout(1500)
        p2.screenshot(path=os.path.join(SHOTS, "ai-guide-screen-with-context-portrait.png"), full_page=True)
        p2.goto(f"http://127.0.0.1:{P1}/ai-guide", wait_until="load"); p2.wait_for_timeout(500)
        p2.screenshot(path=os.path.join(SHOTS, "ai-guide-page-portrait.png"))
        p3 = br.new_context(viewport={"width": 1280, "height": 760}).new_page(); p3.goto(f"http://127.0.0.1:{P1}/ai-guide#recipes", wait_until="load"); p3.wait_for_timeout(500)
        p3.screenshot(path=os.path.join(SHOTS, "ai-guide-page-landscape.png"))
        br.close()
except BaseException as ex:
    traceback.print_exc(); check("e2e ran to completion", False, repr(ex))
finally:
    for p in procs:
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: pass
    shutil.rmtree(TMP, ignore_errors=True)
    bad = [n for n, ok in results if not ok]
    print(f"# {len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""), flush=True)
    sys.exit(1 if bad else 0)
