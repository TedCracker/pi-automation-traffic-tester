#!/usr/bin/env python3
"""E2E of software updates (see scripts/e2e-update.sh). Prints 'CHECK name | pass/FAIL | evidence'."""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.request, urllib.error, importlib.util, tarfile, io
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from traffic_tester.update import bundle as B, ed25519
spec = importlib.util.spec_from_file_location("rk", os.path.join(ROOT, "scripts", "release-keys.py")); RK = importlib.util.module_from_spec(spec); spec.loader.exec_module(RK)
SEED = RK.load_seed()
TMP = tempfile.mkdtemp(prefix="tt-upd-")
BARE = f"{TMP}/releases.git"; USB = f"{TMP}/usb"; REPO = f"{TMP}/repo"; SHOTS = os.path.join(ROOT, "docs", "screenshots")
PA, PB = 18450, 18451
results = []
IMG = "ttupd-installed"


def check(name, ok, ev=""):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:360]}", flush=True)


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


def dx(name, cmd, timeout=900):
    return subprocess.run(["docker", "exec", name, "bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)


def http(port, method, path, body=None, cookie=None, raw=None):
    h = {"Content-Type": "application/octet-stream" if raw is not None else "application/json"}
    if cookie: h["Cookie"] = cookie
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=raw if raw is not None else (None if body is None else json.dumps(body).encode()), headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as f:
            d = f.read(); return f.status, (json.loads(d) if d else None), f.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        d = e.read()
        try: return e.code, json.loads(d), None
        except Exception: return e.code, d, None
    except Exception as e:
        return 0, str(e), None


def wait_up(port, secs=90):
    for _ in range(secs):
        if http(port, "GET", "/api/status")[0] == 200: return True
        time.sleep(1)
    return False


def version(port):
    c, j, _ = http(port, "GET", "/api/status"); return j.get("version") if c == 200 else None


def wait_status(port, states=("ok", "failed", "rolled_back", "refused"), secs=200, cookie=None):
    t0 = time.time(); last = None
    while time.time() - t0 < secs:
        c, j, _ = http(port, "GET", "/api/update/status", cookie=cookie)
        if c == 200:
            last = j["status"]
            if last.get("state") in states: return last
        time.sleep(2)
    return last


def sync_repo(name):
    dx(name, "rm -rf /srv/releases.git; mkdir -p /srv")
    subprocess.run(["docker", "cp", BARE, f"{name}:/srv/releases.git"], capture_output=True)


def start_container(name, port, role):
    sh(f"docker rm -f {name}")
    r = sh(f"docker run -d --name {name} --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock -p 127.0.0.1:{port}:8080 {IMG} /sbin/init")
    assert r.returncode == 0, r.stderr
    time.sleep(6)
    # the containers are copies of one installed image: give each its own Pi id (a real Pi gets it at its first start), or they would refuse to see each other as two Pis
    newid = r'''python3 -c "import sqlite3;c=sqlite3.connect('/var/lib/traffic-tester/traffic-tester.db');c.execute('DELETE FROM settings WHERE key LIKE \"pi_%\" OR key=\"setup_at\"');c.commit()"'''
    dx(name, f"sed -i 's/^TT_ROLE=.*/TT_ROLE={role}/' /etc/traffic-tester/config.env; grep -q TT_UPDATE_REPO /etc/traffic-tester/config.env || echo TT_UPDATE_REPO=/srv/releases.git >> /etc/traffic-tester/config.env; systemctl stop traffic-tester; {newid}; systemctl start traffic-tester")
    sync_repo(name)
    return wait_up(port)


def publish(notes, *extra):
    env = dict(os.environ, TT_RELEASES_REMOTE=BARE, TT_RELEASE_USB=USB)
    return subprocess.run(["bash", "scripts/publish-update.sh", notes, "--no-tests", *extra], cwd=REPO, env=env, capture_output=True, text=True, timeout=600)


def make_bundle(src_ptt, out, version, edit=None, seed=SEED):
    d = tempfile.mkdtemp(dir=TMP); B.verify_bundle(src_ptt, ed25519.public_key(SEED), dest=d)
    if edit: edit(d)
    files = {}
    for dp, _, fn in os.walk(d):
        for f in fn:
            full = os.path.join(dp, f); files[os.path.relpath(full, d)] = full
    files["app/traffic_tester/__init__.py"] = f'__version__ = "{version}"\n'.encode()
    return B.build_bundle(out, files, seed, version=version, notes=f"e2e bundle {version}")


def tamper(src, dst):
    with tarfile.open(src, "r:gz") as a, tarfile.open(dst, "w:gz") as b:
        for m in a:
            d = a.extractfile(m).read()
            if m.name == "app/traffic_tester/engine.py": d = b"X" + d[1:]
            ti = tarfile.TarInfo(m.name); ti.size = len(d); ti.mode = m.mode; b.addfile(ti, io.BytesIO(d))


try:
    # repo copy: publish-update.sh commits there, never in the real repo
    sh(f"mkdir -p {REPO} {USB}; rsync -a --exclude venv --exclude '__pycache__' --exclude '*.log*' {ROOT}/ {REPO}/ && ln -s {ROOT}/venv {REPO}/venv")
    sh(f"git init -q --bare -b stable {BARE}")
    import re
    v0 = re.search(r'__version__\s*=\s*"([^"]+)"', open(f"{REPO}/traffic_tester/__init__.py").read()).group(1)
    vfirst = B.bump_version(v0, "patch")
    r0 = publish("e2e: no changelog entry", "--version", vfirst)                      # the changelog guard: no entry for the new version, no release
    check("publish-update.sh REFUSES to publish when CHANGELOG.md has no entry for the new version (nothing pushed, no .ptt)",
          r0.returncode != 0 and "no entry" in r0.stderr and not any(f.endswith(".ptt") for f in os.listdir(USB)), (r0.stdout + r0.stderr)[-250:])
    cl = open(f"{REPO}/CHANGELOG.md").read()
    open(f"{REPO}/CHANGELOG.md", "w").write(cl.replace("\n## ", f"\n## {vfirst} — 2026-01-01\n- e2e release notes come from the changelog entry.\n\n## ", 1))
    r = publish("e2e: first release", "--version", vfirst)
    check("publish-update.sh (tests skipped here): builds + signs, pushes to the bare repo, copies the .ptt to the USB folder",
          r.returncode == 0 and any(f.endswith(".ptt") for f in os.listdir(USB)), (r.stdout + r.stderr)[-300:])
    sh(f"cd {REPO} && sed -i 's/__version__ = \".*\"/__version__ = \"{v0}\"/' traffic_tester/__init__.py")   # the Pi is installed at v0, one version behind the channel
    # a real systemd container with the real installer
    sh(f"docker rm -f ttupd-seed >/dev/null 2>&1; docker rmi -f {IMG} >/dev/null 2>&1")
    open(f"{TMP}/Dockerfile", "w").write("FROM debian:bookworm\nENV DEBIAN_FRONTEND=noninteractive\nRUN apt-get update -qq && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus polkitd procps sudo git python3 python3-venv python3-pip iproute2 ca-certificates curl openssh-client >/dev/null\n")
    assert sh(f"docker build -q -t ttupd-base {TMP}").returncode == 0
    sh("docker run -d --name ttupd-seed --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock ttupd-base /sbin/init"); time.sleep(6)
    subprocess.run(["docker", "cp", REPO + "/.", "ttupd-seed:/src"], capture_output=True)
    dx("ttupd-seed", "rm -rf /src/venv")
    r = dx("ttupd-seed", "cd /src && ./scripts/install-pi.sh --role A --no-wifi-nexmon --no-net-split 2>&1 | tail -25", timeout=1500)
    active = dx("ttupd-seed", "systemctl is-active traffic-tester; ls /opt/traffic-tester /opt/traffic-tester/update-helper/traffic_tester/update | head -30; ls -l /etc/polkit-1/rules.d/50-traffic-tester-update.rules /etc/traffic-tester/update.conf; systemctl cat traffic-tester-update.service | grep ExecStart").stdout
    check("install-pi.sh (real) installs the update helper: root-owned helper copy, update.conf, polkit rule, oneshot unit; service active",
          active.split("\n")[0].strip() == "active" and "helper.py" in active and "50-traffic-tester-update.rules" in active, (r.stdout[-200:] + active)[-420:])
    sh(f"docker commit ttupd-seed {IMG} >/dev/null; docker rm -f ttupd-seed >/dev/null")
    assert start_container("ttupd-a", PA, "A") and start_container("ttupd-b", PB, "B"), "containers did not come up"
    check("two containers up (alpha, bravo: the same app, no roles) at the same installed version", version(PA) == version(PB) == v0, f"A={version(PA)} B={version(PB)}")
    ok1 = dx("ttupd-a", "runuser -u traffic-tester -- systemctl start --no-block traffic-tester-update.service; echo rc=$?").stdout
    time.sleep(8)
    bad = dx("ttupd-a", "runuser -u traffic-tester -- systemctl start ssh.service 2>&1; echo rc=$?; runuser -u traffic-tester -- systemctl restart traffic-tester.service 2>&1; echo rc=$?").stdout
    check("polkit (real): the web service user may start the update unit and nothing else (ssh.service and restarting itself are denied)", "rc=0" in ok1 and bad.count("rc=0") == 0, ok1.strip() + " | " + bad.strip().replace("\n", " ")[:200])
    wait_up(PA, 30)

    with sync_playwright() as pw:
        br = pw.chromium.launch()
        ctx = br.new_context(viewport={"width": 1100, "height": 1000}); pg = ctx.new_page()
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))

        def settings(port=PA):
            pg.goto(f"http://127.0.0.1:{port}/#config/update"); pg.reload(); pg.wait_for_selector("#setUpdate h2", timeout=20000); pg.wait_for_timeout(800)

        settings()
        txt = pg.inner_text("#setUpdate")
        check("Settings shows the installed version + date, both options (Upload a file / Check online) and roll-back greyed out", v0 in txt and "Upload a file" in txt and "Check online" in txt and pg.is_disabled("#updRoll"), txt[:160].replace("\n", " "))
        c, prof, _ = http(PA, "POST", "/api/profiles/1/duplicate"); marker = prof.get("id") if isinstance(prof, dict) else None
        # ---------------------------------------------------------------- online route
        v1 = B.bump_version(v0, "major")          # not the next minor: CHANGELOG.md may already carry that entry (1.2.0)
        cl = open(f"{REPO}/CHANGELOG.md").read()
        open(f"{REPO}/CHANGELOG.md", "w").write(cl.replace("\n## ", f"\n## {v1} — 2026-01-02\n- e2e: online release with notes\n\n## ", 1))
        r = publish("e2e: online release with notes", "--version", v1)
        sync_repo("ttupd-a"); sync_repo("ttupd-b")
        c, j, _ = http(PA, "POST", "/api/update/check", {})
        print("   (api check:", c, str(j)[:300], ")", flush=True)
        settings(); pg.click("#updCheck"); pg.wait_for_selector("#updInstallOnline", timeout=60000)
        txt = pg.inner_text("#updOnline")
        check("Check online (local bare repo standing in for GitHub): newest version + its notes + 'signature checked'", v1 in txt and "online release with notes" in txt and "signature checked" in txt, txt[:160].replace("\n", " "))
        pg.screenshot(path=f"{SHOTS}/update-1-check-online.png")
        pg.click("#updInstallOnline"); pg.click("#mYes"); pg.wait_for_timeout(2500)
        st = wait_status(PA); wait_up(PA); time.sleep(2)
        check("Install from Check online: signature verified, swapped, restarted, health check OK -> status ok, new version running", st and st["state"] == "ok" and version(PA) == v1, json.dumps(st)[:300])
        files = dx("ttupd-a", "ls -d /opt/traffic-tester/app /opt/traffic-tester/app.prev; cat /opt/traffic-tester/app.prev/traffic_tester/__init__.py; ls -ld /opt/traffic-tester/app/traffic_tester; systemctl is-active traffic-tester").stdout
        check("old app kept as app.prev; the app folder stays root-owned; service active", v0 in files and "root root" in files and files.strip().endswith("active"), files.replace("\n", " ")[:300])
        check("saved tests (profile created before the update) survive", any(p["id"] == marker for p in http(PA, "GET", "/api/profiles")[1]), f"marker profile {marker}")
        settings()
        pg.screenshot(path=f"{SHOTS}/update-2-after-install.png")
        check("UI after the update shows the new version; roll-back button names the previous one", v1 in pg.inner_text("#setUpdate") and v0 in pg.inner_text("#updRoll") and not pg.is_disabled("#updRoll"), pg.inner_text("#updRoll"))
        pg.click("#updRoll"); pg.click("#mYes"); pg.wait_for_timeout(2500)
        st = wait_status(PA); wait_up(PA); time.sleep(2)
        check("Roll back to previous version (UI): old version runs again, data untouched", st and st["state"] == "ok" and version(PA) == v0 and any(p["id"] == marker for p in http(PA, "GET", "/api/profiles")[1]), json.dumps(st)[:240])
        # ---------------------------------------------------------------- upload route (a real file through the browser)
        ptt = [os.path.join(USB, f) for f in os.listdir(USB) if f.endswith(".ptt")][0]
        settings(); pg.set_input_files("#updFile", ptt); pg.wait_for_selector("#updInstallFile", timeout=60000)
        txt = pg.inner_text("#updStaged")
        check("Upload a file (real .ptt picked in the browser): version, date, notes and 'signature checked' shown before installing", v1 in txt and "signature checked" in txt, txt[:140].replace("\n", " "))
        pg.screenshot(path=f"{SHOTS}/update-3-upload-ready.png")
        pg.click("#updInstallFile"); pg.click("#mYes"); pg.wait_for_timeout(2500)
        st = wait_status(PA); wait_up(PA); time.sleep(2)
        check("Install from the uploaded file: status ok, new version running", st and st["state"] == "ok" and version(PA) == v1, json.dumps(st)[:240])
        # ---------------------------------------------------------------- refused bundles
        good = ptt; bad_dir = tempfile.mkdtemp(dir=TMP)
        tamper(good, f"{bad_dir}/tampered.ptt")
        make_bundle(good, f"{bad_dir}/wrongkey.ptt", B.bump_version(v1, "patch"), seed=bytes(range(32)))
        for fn, word in (("tampered.ptt", "checksum"), ("wrongkey.ptt", "not signed by the release key")):
            settings(); pg.set_input_files("#updFile", f"{bad_dir}/{fn}"); pg.wait_for_selector("#updStaged .check.bad", timeout=60000)
            t = pg.inner_text("#updStaged")
            check(f"{fn} is refused in the UI with a plain reason; installed version unchanged", word in t and version(PA) == v1, t[:160].replace("\n", " "))
        pg.screenshot(path=f"{SHOTS}/update-4-refused.png")
        make_bundle(good, f"{bad_dir}/older.ptt", "0.0.9")
        c, j, _ = http(PA, "POST", "/api/update/upload", raw=open(f"{bad_dir}/older.ptt", "rb").read())
        c2, j2, _ = http(PA, "POST", "/api/update/install", {"source": "upload"})
        check("downgrade refused unless 'allow older' is ticked (API 422 + reason)", c == 200 and j["older"] and c2 == 422 and "older" in j2["detail"] and version(PA) == v1, f"{c2} {j2}")
        settings(); check("the 'Allow an older version' tick is offered for an older staged bundle", pg.locator("#updOlder").count() == 1)
        http(PA, "POST", "/api/update/discard", {})
        # ---------------------------------------------------------------- broken (but validly signed) bundle -> automatic rollback
        def breakit(d):
            with open(f"{d}/app/traffic_tester/__main__.py", "w") as f: f.write("raise SystemExit('broken on purpose (e2e)')\n")
        v2 = B.bump_version(v1, "patch")
        make_bundle(good, f"{bad_dir}/broken.ptt", v2, edit=breakit)
        settings(); pg.set_input_files("#updFile", f"{bad_dir}/broken.ptt"); pg.wait_for_selector("#updInstallFile", timeout=60000)
        pg.click("#updInstallFile"); pg.click("#mYes"); pg.wait_for_timeout(2500)
        t0 = time.time(); st = wait_status(PA, secs=260); wait_up(PA, 60)
        act = dx("ttupd-a", "systemctl is-active traffic-tester").stdout.strip()
        check("validly signed but BROKEN bundle: health check fails -> automatic rollback; old version runs, service active, clear message",
              st and st["state"] == "rolled_back" and version(PA) == v1 and act == "active" and "Rolled back automatically" in st["message"], f"{int(time.time()-t0)}s {json.dumps(st)[:300]} active={act}")
        settings()
        txt = pg.inner_text("#updStatus")
        check("UI shows 'Update failed: rolled back automatically' with the reason", "rolled back" in txt.lower(), txt[:200].replace("\n", " "))
        pg.screenshot(path=f"{SHOTS}/update-5-rolled-back.png")
        check("saved tests still intact after the failed update", any(p["id"] == marker for p in http(PA, "GET", "/api/profiles")[1]))
        # ---------------------------------------------------------------- password gate
        http(PA, "POST", "/api/auth/set", {"password": "pw1234"})
        codes = [http(PA, "GET", "/api/update/status")[0], http(PA, "POST", "/api/update/check", {})[0], http(PA, "POST", "/api/update/install", {})[0],
                 http(PA, "POST", "/api/update/rollback", {})[0], http(PA, "POST", "/api/update/upload", raw=b"x")[0]]
        c, _, ck = http(PA, "POST", "/api/auth/login", {"password": "pw1234"}); cookie = (ck or "").split(";")[0]
        okc = http(PA, "GET", "/api/update/status", cookie=cookie)[0]
        check("password gate: with a web password set, every update call is 401 until signed in", codes == [401] * 5 and okc == 200, f"{codes} signed-in={okc}")
        http(PA, "POST", "/api/auth/set", {"password": ""}, cookie=cookie)
        # ---------------------------------------------------------------- update another Pi: alpha sends the bundle to bravo (picked from the list of Pis)
        ip = sh("docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ttupd-b").stdout.strip()
        settings(); pg.set_input_files("#updFile", good); pg.wait_for_selector("#updInstallFile", timeout=60000)
        pc, pj, _ = http(PA, "POST", "/api/peers", {"host": ip}, cookie=cookie)              # alpha finds bravo (by IP: Docker bridges do not always pass mDNS)
        print("   (add peer:", pc, str(pj)[:200], ")", flush=True)
        settings(); pg.wait_for_selector("#updInstallFile", timeout=60000)
        pg.click("details.upd-other summary")
        sel_txt = pg.locator("#updPSel option:checked").inner_text()
        check("'Also update another Pi' lists the Pis on this network (no partner / Pi B words) and typing an address stays possible",
              ip in sel_txt and "partner" not in pg.inner_text("details.upd-other").lower() and pg.locator("#updPSel option[value='__addr']").count() == 1, sel_txt)
        pg.click("#updPGo")
        t0 = time.time(); done = None
        while time.time() - t0 < 240:
            c, j, _ = http(PA, "GET", "/api/update/status")
            if c == 200 and j["partner"].get("state") in ("done", "failed"): done = j["partner"]; break
            time.sleep(3)
        wait_up(PB, 30)
        check("'Also update another Pi': alpha sends the verified bundle to bravo, bravo installs, restarts and reports the new version", done and done["state"] == "done" and version(PB) == v1, f"{json.dumps(done)[:200]} B now {version(PB)}")
        pg.screenshot(path=f"{SHOTS}/update-6-other-pi.png")
        mob = br.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True); mp = mob.new_page()
        mp.goto(f"http://127.0.0.1:{PA}/#config/update"); mp.wait_for_selector("#setUpdate h2"); mp.wait_for_timeout(800)
        sw = mp.evaluate("document.documentElement.scrollWidth"); iw = mp.evaluate("window.innerWidth")
        check("software-update card fits a 390 px phone (no horizontal scroll)", sw <= iw, f"scrollWidth {sw} <= innerWidth {iw}")
        mp.locator("#setUpdate").screenshot(path=f"{SHOTS}/update-7-phone.png")
        check("no JavaScript errors on the update screens", not errs, errs[:3])
        br.close()
except Exception as e:
    import traceback; traceback.print_exc()
    print(dx("ttupd-a", "journalctl -u traffic-tester -n 15 --no-pager 2>&1; ls -la /var/lib/traffic-tester/update /srv").stdout[-1500:])
    check("e2e ran to the end", False, repr(e))
finally:
    sh("docker rm -f ttupd-a ttupd-b ttupd-seed >/dev/null 2>&1")
    p = sum(1 for _, ok in results if ok)
    print(f"# {p}/{len(results)} checks passed", flush=True)
    sys.exit(0 if p == len(results) else 1)
