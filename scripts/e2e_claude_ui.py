#!/usr/bin/env python3
"""Browser end-to-end test of the Ask Claude sidebar and Claude Code on/off, against a real app process and the STUB claude CLI (tests/fake_claude.py).
Run with system python3 (playwright):  python3 scripts/e2e_claude_ui.py     Screenshots -> docs/screenshots/claude-*.png"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time, traceback, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "docs", "screenshots"); os.makedirs(OUT, exist_ok=True)
TMP = tempfile.mkdtemp(prefix="tt-claude-ui-"); procs = []
A, B = 18190, 18191            # A = the instance whose browser UI and Claude sidebar are tested, B = the other (identical) instance that answers the test
REV = os.environ.get("E2E_REVERSE") == "1"        # reverse run: the sidebar is tested on the second instance, the first one answers
TR, RS = ("B", "A") if REV else ("A", "B")
TAG = "[sidebar on the second instance] " if REV else ""
PFX = "rev-" if REV else ""
results, errors = [], []
HOME = os.path.join(TMP, "home"); os.makedirs(HOME)
LIVE = os.path.join(TMP, "live")


def check(name, ok, detail=""):
    name = TAG + name
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def http(port, method, path, body=None, cookie=None):
    h = {"Content-Type": "application/json"}
    if cookie: h["Cookie"] = cookie
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as f:
            d = f.read(); return f.status, (json.loads(d) if d else None)
    except urllib.error.HTTPError as e:
        d = e.read()
        try: return e.code, json.loads(d)
        except Exception: return e.code, d


def start_app(role, port, extra=None):
    env = dict(os.environ, PYTHONPATH=ROOT, HOME=HOME, TT_CLAUDE_BIN=os.path.join(ROOT, "tests", "fake_claude.py"), TT_CLAUDE_FAKE_ONLINE="1", TT_APP_DIR=LIVE,
               TT_RESTART_ON_APPLY="0", TT_WIPE_STRAY="1", TT_SYSTEMCTL=os.path.join(TMP, "systemctl-stub"), TT_RC_WAIT="3", TT_ASSUME_SYNCED="1", TT_SCAN_ROOTS=os.pathsep.join([TMP, HOME]))
    env.update(extra or {})
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--role", role, "--port", str(port), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{role}"],
                         env=env, stdout=open(f"{TMP}/{role}.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try: http(port, "GET", "/api/status"); return
        except Exception: time.sleep(0.25)
    raise RuntimeError("app did not start: " + open(f"{TMP}/{role}.log").read()[-800:])


def free_udp():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


try:
    open(os.path.join(TMP, "systemctl-stub"), "w").write("#!/bin/sh\npython3 - '%s/%s/claude/state.json' <<'PY'\nimport json,sys\ntry: d=json.load(open(sys.argv[1]))\nexcept Exception: d={}\nprint('active' if d.get('enabled') and d.get('remote_control') else 'inactive')\nPY\n" % (TMP, TR)); os.chmod(os.path.join(TMP, "systemctl-stub"), 0o755)
    # a small "installed app" the stub Claude may edit: a git repo copy of the real static + package files
    shutil.copytree(os.path.join(ROOT, "traffic_tester"), os.path.join(LIVE, "traffic_tester"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(LIVE, "scripts")); open(os.path.join(LIVE, "scripts/install-pi.sh"), "w").write("#!/bin/sh\n")
    start_app(TR, A); start_app(RS, B)
    PORT = free_udp()
    st, _ = http(B, "POST", "/api/responder/start", {"transport": "udp", "net": {"port": PORT}})
    check("the other instance answers the test (POST /api/responder/start, alias of the always-on reflector claim)", st == 200)
    URL = f"http://127.0.0.1:{A}"
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        ctx = br.new_context(viewport={"width": 1280, "height": 860}, color_scheme="light")
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "403" not in m.text else None)
        shot = lambda name, full=False: pg.screenshot(path=f"{OUT}/claude-{PFX}{name}.png", full_page=full)
        box = lambda sel: pg.locator(sel).bounding_box()

        # ---------------------------------------------------------------- OFF by default
        def to_cards():
            """Setup: the list of tests (▶ Run opens the New test page)."""
            pg.click("[data-tab=setup]"); pg.wait_for_selector(".pcard")      # the Setup tab always lands on the list of tests

        pg.goto(URL + "/#setup"); to_cards()
        check("default: Claude Code is off (API)", http(A, "GET", "/api/claude/status")[1]["enabled"] is False)
        check("sidebar is closed on first load", pg.locator("#askPanel").is_hidden())
        pg.click("#askBtn"); pg.wait_for_selector("#askPanel:not([hidden])"); pg.wait_for_selector("#askOff")
        check("off-state text shown in the sidebar", "Claude Code is off" in pg.inner_text("#askPanel"))
        check("Log in button present in off-state", pg.locator("#askLogin").is_visible())
        vw = pg.evaluate("innerWidth"); pw_ = box("#askPanel")["width"]
        check("default sidebar width is 25-33% of the window", 0.25 <= pw_ / vw <= 0.34, f"{pw_}/{vw}")
        m, p = box("#main"), box("#askPanel")
        check("main screen sits to the LEFT of the sidebar and does not overlap it", m["x"] + m["width"] <= p["x"] + 1 and p["x"] > vw * 0.5, f"{m} {p}")
        shot("01-off-state")
        for _ in range(2):                                    # main UI stays usable with the panel open
            pg.click("[data-tab=results]"); pg.wait_for_selector("[data-tab=results][aria-selected=true]")
            to_cards()
        pg.click(".pcard >> nth=0 >> [data-act=pick]"); pg.wait_for_selector("[data-testid=nt-page]")
        check("main UI usable while sidebar is open (▶ Run opens the New test page with its check)", pg.locator("[data-testid=nt-bar]").count() == 1)
        # every Claude endpoint refuses while off
        refused = [http(A, "POST", "/api/claude/chat", {"message": "hi"})[0], http(A, "GET", "/api/claude/changes")[0], http(A, "POST", "/api/claude/tool/get_screen", {})[0]]
        check("server refuses chat / changes / tools while off (403)", refused == [403, 403, 403], str(refused))

        # ---------------------------------------------------------------- log in is protected by the web password
        pg.click("#askLogin"); pg.wait_for_selector("#askLoginHost .msg.err")
        check("Log in without a web password is refused with a plain message", "password" in pg.inner_text("#askLoginHost").lower())
        pg.click("[data-tab=settings]"); pg.wait_for_selector("#pwNew")
        pg.fill("#pwNew", "secret1"); pg.click("#pwSave"); pg.wait_for_selector("text=Change password")
        check("web password set from Settings", http(A, "GET", "/api/status")[1]["password_enabled"])
        check("login endpoint now needs the password cookie", http(A, "POST", "/api/claude/login/start")[0] == 401)
        shot("02-settings-off", full=True)
        pg.click("#cLogin"); pg.wait_for_selector("#lgCode")
        check("sign-in link is shown in the app", "https://claude.ai/oauth/authorize" in pg.inner_text("#cLoginHost"))
        pg.fill("#lgCode", "wrong"); pg.click("#lgGo"); pg.wait_for_selector("#cLoginHost .msg.err")
        check("a wrong code fails cleanly and Claude stays off", http(A, "GET", "/api/claude/status")[1]["enabled"] is False)
        pg.click("#lgAgain"); pg.wait_for_selector("#lgCode"); pg.fill("#lgCode", "good-code"); pg.click("#lgGo"); pg.wait_for_selector("#setClaude .pill.pass")
        check("one-time sign-in finished: Claude Code is ON", "ON" in pg.inner_text("#setClaude"))
        shot("03-settings-on", full=True)

        # ---------------------------------------------------------------- the chat sidebar
        pg.wait_for_selector("#askIn")
        check("sidebar now shows the chat box", pg.locator("#askIn").is_visible())
        # resize by dragging the handle, remember the width, respect min/max
        h = box("#askHandle"); x0 = h["x"] + h["width"] / 2; y0 = h["y"] + h["height"] / 2
        w_before = box("#askPanel")["width"]
        pg.mouse.move(x0, y0); pg.mouse.down(); pg.mouse.move(x0 - 150, y0, steps=8); pg.mouse.up()
        w_after = box("#askPanel")["width"]
        check("dragging the handle left widens the sidebar", w_after > w_before + 100, f"{w_before}->{w_after}")
        h = box("#askHandle"); x0 = h["x"] + h["width"] / 2
        pg.mouse.move(x0, y0); pg.mouse.down(); pg.mouse.move(5, y0, steps=8); pg.mouse.up()
        wmax = box("#askPanel")["width"]
        check("width has a sensible maximum (main screen keeps >= 35%)", wmax <= vw * 0.63 and box("#main")["width"] > vw * 0.3, f"{wmax}")
        h = box("#askHandle"); x0 = h["x"] + h["width"] / 2
        pg.mouse.move(x0, y0); pg.mouse.down(); pg.mouse.move(vw - 5, y0, steps=8); pg.mouse.up()
        wmin = box("#askPanel")["width"]
        check("width has a sensible minimum", 280 <= wmin <= 320, f"{wmin}")
        h = box("#askHandle"); x0 = h["x"] + h["width"] / 2
        pg.mouse.move(x0, y0); pg.mouse.down(); pg.mouse.move(x0 - 120, y0, steps=6); pg.mouse.up()
        w_set = round(box("#askPanel")["width"])
        pg.reload(); pg.wait_for_selector("#askPanel:not([hidden])"); pg.wait_for_selector("#askIn")
        check("sidebar width and open state are remembered after reload", abs(box("#askPanel")["width"] - w_set) <= 2, f"{w_set} vs {box('#askPanel')['width']}")
        check("tabs are all reachable with the sidebar open (header does not overflow under it)", all(pg.locator(f"[data-tab={t}]").is_visible() for t in ("setup", "results", "settings")))
        to_cards()

        def ask(text, wait_done=True):
            n = pg.locator(".msg.user").count()
            pg.fill("#askIn", text); pg.press("#askIn", "Enter")
            pg.wait_for_function(f"document.querySelectorAll('.msg.user').length>{n}", timeout=10000)
            if wait_done: pg.wait_for_function("document.querySelector('#askSend').textContent==='Send'", timeout=60000)
            time.sleep(0.5)

        # read what is on screen
        name = pg.inner_text(".pcard >> nth=1 >> h3").split("\n")[0].strip()
        pg.click(".pcard >> nth=1 >> [data-act=pick]"); pg.wait_for_selector("[data-testid=nt-page]")
        pid = [p for p in http(A, "GET", "/api/profiles", cookie=None)[1] if True] if False else None
        ctxcookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
        profs = http(A, "GET", "/api/profiles", cookie=ctxcookie)[1]
        trip = [p for p in profs if p.get("kind", "trip") == "trip"][0]
        ask("scenario:explain what is on the screen")
        txt = pg.inner_text("#askBody")
        check("Claude reads the screen: answer mentions the selected tab and test", "setup tab" in txt and name.split(" ")[0] in txt, txt[-300:])
        check("action line shown for the screen read", pg.locator(".act.ok:has-text('Looked at the screen')").count() >= 1)
        shot("04-chat-read-screen")

        # start a test from chat; the LEFT screen switches to the live run view
        ask(f"scenario:start profile {trip['id']} duration 25 peer 127.0.0.1:{PORT}", wait_done=True)
        check("action line: Started a test (with result)", pg.locator(".act.ok:has-text('Started a test')").count() == 1 and "started" in pg.inner_text(".act:has-text('Started a test')"), pg.inner_text("#askBody")[-300:])
        pg.wait_for_selector("[data-tab=run][aria-selected=true]", timeout=8000)
        pg.wait_for_selector("#runBanner .banner.run", timeout=8000)
        check("left screen jumped to the live Run view while the chat stays open", pg.locator("#askPanel").is_visible() and pg.locator("#runBanner .banner.run").count() == 1)
        check("a real test is running on the Pi", http(A, "GET", "/api/status", cookie=ctxcookie)[1]["run"]["running"] is True)
        time.sleep(3); shot("05-chat-started-test")
        ask("scenario:stop now")
        check("action line: Stopped the test", pg.locator(".act.ok:has-text('Stopped the test')").count() == 1)
        for _ in range(60):
            if not http(A, "GET", "/api/status", cookie=ctxcookie)[1]["run"]["running"]: break
            time.sleep(0.5)
        check("the test really stopped", http(A, "GET", "/api/status", cookie=ctxcookie)[1]["run"]["running"] is False)
        pg.wait_for_selector("#runBanner .banner.pass, #runBanner .banner.fail", timeout=10000)
        check("left screen shows the finished result live", pg.locator("#runBanner .banner:not(.run)").count() == 1)

        # change a setting via chat; the profile list on the left updates
        to_cards()
        n0 = pg.locator(".pcard").count()
        ask(f"scenario:setting profile {trip['id']}")
        check("action line: Saved a profile", pg.locator(".act.ok:has-text('Saved a profile')").count() == 1)
        pg.wait_for_function(f"document.querySelectorAll('.pcard').length>{n0}", timeout=8000)
        check("new profile appears on the left without reloading", pg.locator(".pcard:has-text('Claude tuned')").count() >= 1)

        # feature / UI change through git: diff, confirm, apply, undo
        ask("scenario:feature please add something")
        pg.wait_for_selector(".chg")
        check("a change card with a diff is shown, nothing applied yet", "Nothing is live yet" in pg.inner_text(".chg") and not os.path.exists(f"{LIVE}/traffic_tester/static/claude-added.txt"))
        shot("06-chat-change-diff")
        pg.click(".chg [data-a=apply]"); pg.click("#mYes"); pg.wait_for_selector(".chg:has-text('Change applied')")
        check("after confirm the change is applied to the app files", os.path.isfile(f"{LIVE}/traffic_tester/static/claude-added.txt"))
        pg.click(".chg [data-a=undo]"); pg.click("#mYes"); pg.wait_for_function("document.body.innerText.includes('Change undone')", timeout=10000)
        check("Undo reverts it", not os.path.exists(f"{LIVE}/traffic_tester/static/claude-added.txt"))
        ask("scenario:protected try to edit the guard")
        pg.wait_for_selector(".chg")
        check("changes to the safety files cannot be applied", pg.locator(".chg [data-a=apply]").is_disabled())
        pg.click(".chg [data-a=discard]")

        # plain messages for the failure cases
        ask("scenario:offline hello")
        check("offline -> plain message, no crash", "Wi-Fi internet" in pg.inner_text("#askBody"))
        os.remove(f"{TMP}/{TR}/claude/config/.credentials.json")
        pg.evaluate("1")
        ask("hello there")
        check("not signed in -> plain message", "not signed in" in pg.inner_text("#askBody").lower(), pg.inner_text("#askPanel")[-400:])
        check("the app itself still works (status API)", http(A, "GET", "/api/status", cookie=ctxcookie)[0] == 200)
        # sign back in for the log-out test
        http(A, "POST", "/api/claude/login/start", cookie=ctxcookie); http(A, "POST", "/api/claude/login/code", {"code": "good-code"}, cookie=ctxcookie)
        check("signed in again", http(A, "GET", "/api/claude/status")[1]["logged_in"] is True)

        # ---------------------------------------------------------------- log out wipes everything, everything refuses
        pg.click("[data-tab=settings]"); pg.wait_for_selector("#cLogout")
        pg.click("#cRc"); pg.wait_for_function("document.querySelector('#setClaude').innerText.includes('Stop Remote Control')", timeout=30000)
        check("Remote Control switched on from Settings (state flag set, service active)", json.load(open(f"{TMP}/{TR}/claude/state.json"))["remote_control"] is True and http(A, "GET", "/api/claude/status")[1]["remote"]["active"] is True)
        shot("06b-remote-control-on", full=True)
        pg.click("#cLogout"); pg.click("#mYes"); pg.wait_for_selector("#setClaude .pill.na", timeout=60000)
        pg.wait_for_selector("#setClaude .check.ok", timeout=60000)
        check("Settings shows a verified log-out result", "No saved sign-in was found" in pg.inner_text("#setClaude"))
        check("sidebar shows 'Claude Code is off' with a Log in button", "Claude Code is off" in pg.inner_text("#askPanel") and pg.locator("#askLogin").is_visible())
        shot("07-after-logout", full=True)
        from traffic_tester.claude import scan
        left = scan.scan([TMP, HOME], max_seconds=60)
        check("independent filesystem scan finds no token or credential anywhere", left == [], str(left)[:300])
        check("credentials directory is empty", not os.path.exists(f"{TMP}/{TR}/claude/config/.credentials.json") and not os.path.exists(f"{HOME}/.claude.json"))
        st = http(A, "GET", "/api/claude/status")[1]
        check("API says off", st["enabled"] is False)
        refused = [http(A, "POST", "/api/claude/chat", {"message": "hi"}, cookie=ctxcookie)[0], http(A, "POST", "/api/claude/tool/get_screen", {}, cookie=ctxcookie)[0], http(A, "GET", "/api/claude/changes", cookie=ctxcookie)[0]]
        check("after log-out every endpoint refuses (403)", refused == [403, 403, 403], str(refused))
        check("Remote Control switched off by log-out (flag cleared)", json.load(open(f"{TMP}/{TR}/claude/state.json"))["remote_control"] is False)
        # logout needs no password: a brand-new browser with no cookie can still do it
        check("log-out works without the password", http(A, "POST", "/api/claude/logout")[0] == 200)
        check("log-in still needs the password", http(A, "POST", "/api/claude/login/start")[0] == 401)
        pg.click("#askClose"); check("sidebar can be closed", pg.locator("#askPanel").is_hidden())
        check("main area uses the full width again", box("#main")["width"] > vw * 0.7)
        check("no browser console errors", not errors, str(errors[:3]))
        br.close()
except BaseException as e:
    traceback.print_exc(); check("script ran to the end", False, repr(e))
finally:
    for p in procs:
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: pass
    shutil.rmtree(TMP, ignore_errors=True)
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    sys.exit(1 if bad else 0)
