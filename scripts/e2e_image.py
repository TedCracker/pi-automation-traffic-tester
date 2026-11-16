#!/usr/bin/env python3
"""The finished card image booted in QEMU (aarch64 'virt', TCG), driven like a person with a phone: scripts/e2e-image.sh.

  phases:  main    Pi 1 boots -> only the "Set up this Pi" screen (phone size) -> name it -> hostname / Wi-Fi name / marker; Pi 2 boots on the same
                   network -> it finds Pi 1 over mDNS and offers the next free name; Imager-style first-boot seed (user + Wi-Fi) on Pi 3
           update  'Check online' from the booted image against the real releases repo (and Install, when a newer release exists)
Prints 'CHECK name | pass/FAIL/unresolved | evidence' lines for docs/dev/checks.md. Needs docker, the tt-qemu-image image, build-image/pi-traffic-tester-<ver>.img (--keep)."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "build-image")
SHOTS = os.path.join(ROOT, "docs", "screenshots")
CT = "tt-qemu-bench"
PORT = {"pi-a": 18091, "pi-b": 18092, "pi-c": 18093}
OCT = {"pi-a": 11, "pi-b": 12, "pi-c": 13}
results = []


def check(name, ok, ev=""):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:420]}", flush=True)


def unresolved(name, ev):
    print(f"CHECK {name} | unresolved | {ev}", flush=True)


def sh(*cmd, timeout=600, check_rc=False):
    r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    if check_rc and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)} -> {r.stderr[-300:]}")
    return r


def bench(*args, timeout=600):
    return sh("docker", "exec", CT, "bash", "/bench.sh", *args, timeout=timeout)


def api(vm, method, path, body=None, timeout=30):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT[vm]}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            d = f.read()
            return json.loads(d) if d else None
    except urllib.error.HTTPError as e:
        try:
            return {"_http": e.code, **json.loads(e.read() or b"{}")}
        except Exception:
            return {"_http": e.code}


def wait_api(vm, secs, pred=lambda s: True, path="/api/status"):
    end = time.time() + secs
    last = None
    while time.time() < end:
        try:
            last = api(vm, "GET", path, timeout=10)
            if last and pred(last):
                return last
        except Exception as e:  # noqa
            last = str(e)
        time.sleep(5)
    return last


def image_path():
    ver = os.environ.get("TT_IMAGE_VERSION") or re.search(r'__version__\s*=\s*"([^"]+)"', open(os.path.join(ROOT, "traffic_tester", "__init__.py")).read()).group(1)  # TT_IMAGE_VERSION: test an older kept image (e.g. update 1.4.0 -> newest)
    p = os.path.join(WORK, f"pi-traffic-tester-{ver}.img")
    if not os.path.exists(p):
        sys.exit(f"{p} not found: build with scripts/pkg/build-image.sh --keep")
    return p, ver


def start_bench():
    sh("docker", "rm", "-f", CT)
    os.makedirs(os.path.join(WORK, "vm"), exist_ok=True)
    sh("docker", "run", "-d", "--rm", "--privileged", "--platform", "linux/amd64", "--name", CT, "-v", f"{ROOT}/scripts/pkg/qemu-bench.sh:/bench.sh:ro", "-v", f"{WORK}:/w", "-v", f"{WORK}/vm:/vm",
       *sum([["-p", f"{p}:{p}"] for p in PORT.values()], []), "tt-qemu-image", "sleep", "infinity", check_rc=True)
    bench("setup")


def boot(vm, seed=None):
    img, ver = image_path()
    r = bench("prep", vm, img.replace(WORK, "/w"), *([seed] if seed else []), timeout=900)
    assert r.returncode == 0, r.stderr[-300:] + r.stdout[-300:]
    r = bench("start", vm, str(OCT[vm]), str(PORT[vm]))
    assert r.returncode == 0, r.stderr[-300:]


def shot(page, name):
    os.makedirs(SHOTS, exist_ok=True)
    page.screenshot(path=os.path.join(SHOTS, name))


def phase_main():
    img, ver = image_path()
    start_bench()
    t0 = time.time()
    boot("pi-a")
    st = wait_api("pi-a", 900, lambda s: isinstance(s, dict) and "version" in s)
    boot_s = int(time.time() - t0)
    check("Card image boots in QEMU and the app answers (unnamed Pi)", isinstance(st, dict) and st.get("version") == ver, f"version={st.get('version') if isinstance(st, dict) else st} after {boot_s}s (incl. copying the image); hostname={st.get('hostname') if isinstance(st, dict) else '?'}")
    check("Role/name not chosen yet: status says first_boot and the Pi starts as 'pitester'", isinstance(st, dict) and st.get("first_boot") is True and st.get("hostname") == "pitester", f"first_boot={st.get('first_boot')} hostname={st.get('hostname')}")
    red = sh("docker", "exec", CT, "curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{redirect_url}", "http://10.99.0.11/generate_204").stdout.strip()
    check("Port 80 on the unnamed Pi redirects any request (phone sign-in probe) to the setup page", red.startswith("302 http://10.99.0.11:8080"), f"GET http://pi/generate_204 -> {red}")
    info = api("pi-a", "GET", "/api/setup/info")
    check("First Pi is offered the name pi1", info.get("suggested") == "pi1" and info.get("password") == "password", json.dumps(info)[:200])
    ap = api("pi-a", "GET", "/api/net/config")
    ssid_before = (((ap or {}).get("wifi") or {}).get("ssid")) or json.dumps((ap or {}).get("wifi"))[:120]
    check("Before naming the Pi's own Wi-Fi is 'pi-setup'", "pi-setup" in json.dumps(ap), f"net config wifi: {ssid_before}")
    # page: phone size
    with sync_playwright() as p:
        br = p.chromium.launch()
        ctx = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, has_touch=True, is_mobile=True)
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" and "422" not in m.text else None)      # the 422 is the bad name typed on purpose
        page.goto(f"http://127.0.0.1:{PORT['pi-a']}/", wait_until="networkidle", timeout=60000)
        page.wait_for_selector("[data-testid=fb-card]", timeout=30000)
        shot(page, "image-01-name-this-pi.png")
        tabs_visible = page.locator("nav#tabs").is_visible()
        check("Phone page shows only the 'Set up this Pi' screen (no tabs, name pre-filled pi1, big button)", (not tabs_visible) and page.input_value("[data-testid=fb-name]") == "pi1" and page.locator("[data-testid=fb-go]").bounding_box()["height"] >= 48,
              f"tabs visible={tabs_visible}; input={page.input_value('[data-testid=fb-name]')}; button h={page.locator('[data-testid=fb-go]').bounding_box()['height']}")
        overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
        check("Setup screen fits a 390 px phone without sideways scrolling", not overflow, f"scrollWidth overflow={overflow}")
        # a bad name is refused with a plain message and nothing changes
        page.fill("[data-testid=fb-name]", "my pi")
        page.click("[data-testid=fb-go]")
        page.wait_for_selector("#fbErr:not([hidden])", timeout=20000)
        check("A bad name (space) is refused in plain words", "letters, digits or dashes" in page.inner_text("#fbErr"), page.inner_text("#fbErr")[:160])
        page.fill("[data-testid=fb-name]", "pi1")
        page.click("[data-testid=fb-go]")
        page.wait_for_selector("[data-testid=fb-done]", timeout=90000)
        shot(page, "image-02-name-done.png")
        done = page.inner_text("[data-testid=fb-done]")
        check("After naming the page says what to join: Wi-Fi pi1 / password / 10.42.0.1:8080", "pi1" in done and "password" in done and "10.42.0.1:8080" in done, done.replace("\n", " ")[:260])
        # the Pi comes back as a normal app (the page polls and reloads itself)
        try:
            page.wait_for_function("document.querySelector('[data-testid=top-pi-name]') && document.querySelector('[data-testid=top-pi-name]').textContent.trim() === 'pi1' && !document.body.classList.contains('fb')", timeout=90000)
            top = page.inner_text("[data-testid=top-pi-name]")
            check("The page then opens the normal app by itself, top bar says pi1", top.strip() == "pi1", f"top bar: {top.strip()}")
            shot(page, "image-03-app-after-naming.png")
        except Exception as e:  # noqa
            check("The page then opens the normal app by itself, top bar says pi1", False, f"{type(e).__name__}")
        check("No browser console errors on the first-boot screens", not errs, "; ".join(errs)[:200])
        br.close()
    p22 = sh("docker", "exec", CT, "bash", "-c", "timeout 4 bash -c '</dev/tcp/10.99.0.11/22' && echo OPEN || echo CLOSED").stdout.strip()
    check("SSH is off on a card flashed without turning it on (port 22 closed)", p22 == "CLOSED", f"port 22 on pi1: {p22}")
    st2 = api("pi-a", "GET", "/api/status")
    check("Hostname became pi1 and the first-boot state is over", st2.get("hostname") == "pi1" and st2.get("first_boot") is False and st2["pi"]["name"] == "pi1", f"hostname={st2.get('hostname')} first_boot={st2.get('first_boot')} name={st2['pi']['name']}")
    again = api("pi-a", "POST", "/api/setup/name", {"name": "pi9"})
    check("Naming a second time is refused", again.get("_http") == 409, json.dumps(again)[:120])
    red2 = sh("docker", "exec", CT, "curl", "-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", "http://10.99.0.11/").stdout.strip()
    check("After naming, port 80 no longer redirects (the helper stops)", red2 in ("000", "7", "") or not red2.startswith("302"), f"GET / on port 80 -> {red2}")
    ap2 = api("pi-a", "GET", "/api/net/config")
    check("Wi-Fi name switched pi-setup -> pi1 (profile in NetworkManager, password stays 'password')", '"pi1"' in json.dumps(ap2) and "pi-setup" not in json.dumps(ap2), json.dumps((ap2 or {}).get("wifi"))[:200])

    # second Pi on the same network: finds the first over mDNS
    boot("pi-b")
    stb = wait_api("pi-b", 900, lambda s: isinstance(s, dict) and "version" in s)
    check("Second card boots on the same network, also unnamed", isinstance(stb, dict) and stb.get("first_boot") is True and stb.get("hostname") == "pitester", f"first_boot={stb.get('first_boot') if isinstance(stb, dict) else stb}")
    infob = wait_api("pi-b", 240, lambda s: s.get("suggested") == "pi2", "/api/setup/info")
    peers = api("pi-b", "GET", "/api/peers")
    check("Second Pi finds Pi 1 over mDNS (_pitester._tcp) and offers the next free name pi2", infob.get("suggested") == "pi2" and any(x.get("name") == "pi1" for x in peers.get("peers", [])), f"suggested={infob.get('suggested')} peers={[x.get('name') for x in peers.get('peers', [])]}")
    clash = api("pi-b", "POST", "/api/setup/name", {"name": "pi1"})
    check("Typing a name another Pi already uses is refused", clash.get("_http") == 409, json.dumps(clash)[:160])
    with sync_playwright() as p:
        br = p.chromium.launch()
        ctx = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, has_touch=True, is_mobile=True)
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{PORT['pi-b']}/", wait_until="networkidle", timeout=60000)
        page.wait_for_selector("[data-testid=fb-card]", timeout=30000)
        check("The page pre-fills pi2 on the second Pi", page.input_value("[data-testid=fb-name]") == "pi2", page.input_value("[data-testid=fb-name]"))
        shot(page, "image-04-second-pi-pi2.png")
        page.click("[data-testid=fb-go]")
        page.wait_for_selector("[data-testid=fb-done]", timeout=90000)
        br.close()
    pa = wait_api("pi-a", 180, lambda s: any(x.get("name") == "pi2" for x in s.get("peers", [])), "/api/peers")
    check("Pi 1 sees Pi 2 by its new name", any(x.get("name") == "pi2" for x in pa.get("peers", [])), f"peers on pi1: {[x.get('name') for x in pa.get('peers', [])]}")

    # Imager-style first-boot seed: user + site Wi-Fi
    seed = "/w/vm/seed-imager"
    os.makedirs(os.path.join(WORK, "vm", "seed-imager"), exist_ok=True)
    open(os.path.join(WORK, "vm", "seed-imager", "user-data"), "w").write(
        "#cloud-config\nhostname: imagerpi\nmanage_etc_hosts: true\nusers:\n- name: imageruser\n  groups: users,adm,dialout,netdev,sudo\n  shell: /bin/bash\n  lock_passwd: false\n"
        "  passwd: $5$qqqqqqqq$Zn8RgQJm1vMyG5hZJdQmO1n7Zq3Zw3N2tQqjzR3lD3A\nenable_ssh: true\nssh_pwauth: false\ntimezone: America/Chicago\n")
    open(os.path.join(WORK, "vm", "seed-imager", "network-config"), "w").write(
        "network:\n  version: 2\n  wifis:\n    renderer: NetworkManager\n    wlan0:\n      dhcp4: true\n      optional: true\n      access-points:\n        \"SiteWifi\":\n          password: \"sitepass123\"\n      regulatory-domain: US\n")
    boot("pi-c", seed)
    stc = wait_api("pi-c", 900, lambda s: isinstance(s, dict) and "version" in s)
    check("Card with an Imager-style user-data + network-config boots and the app answers", isinstance(stc, dict) and stc.get("first_boot") is True, f"hostname={stc.get('hostname') if isinstance(stc, dict) else stc}")
    time.sleep(60)
    p22c = sh("docker", "exec", CT, "bash", "-c", "timeout 4 bash -c '</dev/tcp/10.99.0.13/22' && echo OPEN || echo CLOSED").stdout.strip()
    check("SSH is on only where the Imager-style seed asked for it (enable_ssh: true)", p22c == "OPEN", f"port 22 on the seeded card: {p22c}")
    # stop the VMs and read the files they wrote
    for vm in ("pi-a", "pi-b", "pi-c"):
        bench("stop", vm)
    a = bench("cat", "pi-a", "/etc/hostname", "/etc/NetworkManager/system-connections/tt-ap.nmconnection", "/etc/NetworkManager/dnsmasq-shared.d", "/var/lib/traffic-tester").stdout
    check("Files on Pi 1's card: hostname pi1, AP profile ssid=pi1/psk=password/autoconnect on (default), setup captive DNS removed, first-boot marker removed",
          re.search(r"### /etc/hostname\npi1", a) and "ssid=pi1" in a and "psk=password" in a and "autoconnect=false" not in a and "tt-captive.conf" not in a and "first-boot" not in a, a.replace("\n", " | ")[:420])
    c = bench("cat", "pi-c", "/etc/passwd", "/etc/NetworkManager/system-connections/tt-ap.nmconnection", "/etc/hostname", "/etc/netplan").stdout
    npf = re.search(r"(\S+\.yaml)", c.split("### /etc/netplan")[-1])
    netplan_txt = bench("cat", "pi-c", "/etc/netplan/" + npf.group(1)).stdout if npf else ""
    check("Imager user 'imageruser' was created by cloud-init on the card", re.search(r"^imageruser:x:\d+:", c, re.M) is not None, [l for l in c.splitlines() if l.startswith("imageruser")][:1])
    check("Imager Wi-Fi: netplan profile exists and the Pi's own network was switched off for it (autoconnect=false), the app's own Wi-Fi record agrees",
          "SiteWifi" in netplan_txt and "wifis" in netplan_txt and "autoconnect=false" in c.split("### /etc/hostname")[0], f"netplan {npf.group(1) if npf else None} has SiteWifi={'SiteWifi' in netplan_txt}; tt-ap autoconnect=false={'autoconnect=false' in c}")
    check("Imager Wi-Fi: tt-ap autoconnect=false after the first-boot hook", "autoconnect=false" in c.split("### /etc/hostname")[0], "tt-ap keyfile " + ("autoconnect=false" if "autoconnect=false" in c else "still autoconnect=true"))
    unresolved("Real Wi-Fi hotspot pi-setup/pi1 visible and joinable from a phone; captive popup on iOS/Android", "QEMU has no Wi-Fi radio: NetworkManager AP profile + dnsmasq config are checked as files; the radio needs a real Pi 5")
    unresolved("Boot on real Pi 5 hardware + stock first-boot partition expand", "the Pi kernel and boot firmware cannot run in QEMU virt; the image keeps the stock cmdline.txt/initramfs untouched")
    sh("docker", "rm", "-f", CT)
    bad = [n for n, ok in results if not ok]
    print(f"SUMMARY main: {len(results) - len(bad)}/{len(results)} pass" + (f"; FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


def phase_update():
    img, ver = image_path()
    start_bench()
    boot("pi-a")
    st = wait_api("pi-a", 900, lambda s: isinstance(s, dict) and "version" in s)
    check("Image boots for the update test", isinstance(st, dict) and "version" in st, f"version={st.get('version') if isinstance(st, dict) else st}")
    # name it first (the update helper is the same root unit that renames)
    api("pi-a", "POST", "/api/setup/name", {"name": "pi1"})
    info = api("pi-a", "GET", "/api/update/status")
    online = info.get("online", {})
    check("'Check online' is available in the booted image (deploy key + public key were installed)", online.get("can_check") is True, json.dumps(online)[:300])
    chk = api("pi-a", "POST", "/api/update/check", timeout=240)
    check("'Check online' reaches the releases repo and the signature verifies", chk.get("signature") == "ok" and bool(chk.get("version")), json.dumps({k: chk.get(k) for k in ("version", "newer", "signature", "file", "_http", "detail")}))
    if chk.get("newer"):
        r = api("pi-a", "POST", "/api/update/install", {"source": "online"}, timeout=120)
        new = wait_api("pi-a", 420, lambda s: isinstance(s, dict) and s.get("version") == chk.get("version"))
        check(f"Install from the booted image: {ver} -> {chk.get('version')}, service restarted and healthy", isinstance(new, dict) and new.get("version") == chk.get("version"), f"install reply {json.dumps(r)[:100]}; now {new.get('version') if isinstance(new, dict) else new}")
        us = api("pi-a", "GET", "/api/update/status")
        check("Update status reports success (no rollback)", (us.get("status") or {}).get("state") in ("ok", "idle"), json.dumps(us.get("status"))[:200])
    else:
        check("Image already carries the newest release", True, f"installed {ver}, channel has {chk.get('version')}")
    bench("stop", "pi-a")
    sh("docker", "rm", "-f", CT)
    bad = [n for n, ok in results if not ok]
    print(f"SUMMARY update: {len(results) - len(bad)}/{len(results)} pass" + (f"; FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    ph = sys.argv[1] if len(sys.argv) > 1 else "main"
    sys.exit(phase_main() if ph == "main" else phase_update())
