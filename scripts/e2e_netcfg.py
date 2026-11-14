#!/usr/bin/env python3
"""E2E of the network settings, run INSIDE a Debian Bookworm container as root (see scripts/e2e-netcfg.sh). Prints 'CHECK name | pass/FAIL | evidence'.

Part 1  the REAL scripts/install-pi.sh: fresh install -> Wi-Fi access point default (keyfile, because NetworkManager is not running here), --role ignored with a note,
        no TT_ROLE, chrony allow lines only; a re-run keeps the network; --wifi-mode ap forces it; an app-chosen time source survives a re-run.
Part 2  the REAL root helper (the copy install-pi.sh made), started the way polkit lets the service user start it (systemctl shim -> detached root run), driven by the
        app-side client as the unprivileged service user, with a fake nmcli (profiles in a JSON file) and a fake systemd-run (records the scheduled revert):
        apply -> snapshot -> Keep; apply -> timer fires -> exact restore; failed join -> immediate restore; clock op; fallback AP after 3 minutes and back; hostile requests refused.
Part 3  uninstall removes only the tt-* profiles."""
import json
import os
import re
import subprocess
import sys
import time

results = []
APP = "/opt/traffic-tester"
HELP = f"{APP}/update-helper/traffic_tester/update"
STATE = "/var/lib/traffic-tester-update"
DATA = "/var/lib/traffic-tester"
FAKE_NM = "/var/lib/fakenm/nm.json"
SYSD = "/var/lib/fake-systemd"
SVC = "traffic-tester"


def check(name, ok, ev=""):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:300]}", flush=True)


def sh(cmd, timeout=1800, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, **kw)


def rj(path):
    try:
        return json.load(open(path))
    except Exception:
        return None


def write_file(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(text)
    os.chmod(path, mode)


# ------------------------------------------------------------------ fake sysfs + shims (installed after part 1: part 1 needs NetworkManager to look absent)
def make_fake_sys():
    for n in ("wlan0", "eth0", "eth1"):
        os.makedirs(f"/fakesys/{n}", exist_ok=True)
        if n.startswith("wl"):
            os.makedirs(f"/fakesys/{n}/wireless", exist_ok=True)
        else:
            write_file(f"/fakesys/{n}/type", "1\n")
            write_file(f"/fakesys/{n}/carrier", "1\n")


def carrier(n, up):
    write_file(f"/fakesys/{n}/carrier", "1\n" if up else "0\n")


def install_shims():
    write_file("/usr/local/bin/fake-sysd", f'''#!/usr/bin/env python3
import json, os, subprocess, sys
name = os.path.basename(sys.argv[0]); a = sys.argv[1:]
D = "{SYSD}"; os.makedirs(D, exist_ok=True)
with open(D + "/calls.log", "a") as f: f.write(json.dumps([name] + a) + "\\n")
if name == "systemd-run":
    unit = secs = None; i = 0
    while i < len(a) and a[i].startswith("--"):
        if a[i].startswith("--on-active="): secs = int(a[i].split("=", 1)[1].rstrip("s"))
        elif a[i].startswith("--unit="): unit = a[i].split("=", 1)[1]
        i += 1
    json.dump({{"unit": unit, "seconds": secs, "argv": a[i:]}}, open("%s/%s.json" % (D, unit), "w")); sys.exit(0)
if a[:2] == ["start", "--no-block"] and a[2:3] == ["traffic-tester-update.service"]:       # what polkit lets the service user do: start ONE fixed root unit (detached, like systemd)
    subprocess.Popen(["sudo", "-n", "/usr/local/bin/tt-helper-run"], stdin=subprocess.DEVNULL, stdout=open(D + "/helper.out", "a"), stderr=subprocess.STDOUT, start_new_session=True); sys.exit(0)
if a[:1] == ["stop"] and a[1:2] and a[1].endswith(".timer"):
    p = "%s/%s.json" % (D, a[1][:-6])
    if os.path.exists(p): os.unlink(p)
sys.exit(0)
''', 0o755)
    for n in ("systemd-run", "systemctl"):
        if os.path.lexists(f"/usr/local/bin/{n}"):
            os.unlink(f"/usr/local/bin/{n}")
        os.symlink("/usr/local/bin/fake-sysd", f"/usr/local/bin/{n}")
    env = "export PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin TT_NET_SYSFS=/fakesys TT_NET_TEST_CLOCK=1"
    write_file("/usr/local/bin/tt-helper-run", f"#!/bin/sh\n{env}\nexec /usr/bin/python3 -I {HELP}/helper.py run\n", 0o755)           # = the ExecStart of traffic-tester-update.service
    write_file("/usr/local/bin/tt-root-py", f"#!/bin/sh\n{env}\nexec /usr/bin/python3 -I \"$@\"\n", 0o755)                              # root runs of netops.py / the recorded revert command
    write_file("/usr/local/bin/tt-nmcli-root", f"#!/bin/sh\nexport TT_FAKE_NM_STATE={FAKE_NM}\nexec /usr/bin/python3 {HELP}/netfake.py \"$@\"\n", 0o755)
    write_file("/usr/local/bin/nmcli", "#!/bin/sh\nexec sudo -n /usr/local/bin/tt-nmcli-root \"$@\"\n", 0o755)                          # (an unprivileged read in real life)
    write_file("/etc/sudoers.d/tt-e2e", f"{SVC} ALL=(root) NOPASSWD: /usr/local/bin/tt-helper-run, /usr/local/bin/tt-nmcli-root\n", 0o440)
    os.makedirs(os.path.dirname(FAKE_NM), exist_ok=True)
    os.makedirs(SYSD, exist_ok=True)
    os.chmod(SYSD, 0o777)                                                      # the service user's systemctl shim logs here
    for f in ("calls.log", "helper.out"):
        write_file(f"{SYSD}/{f}", "", 0o666)


def nm_state():
    return rj(FAKE_NM) or {}


def nm_update(**kw):
    d = nm_state()
    d.update(kw)
    write_file(FAKE_NM, json.dumps(d))


def nm_view():
    d = nm_state()
    return {"profiles": sorted((p["name"], p["type"], tuple(sorted(p["props"].items()))) for p in d.get("profiles", [])), "active": sorted((a["name"], a["device"]) for a in d.get("active", [])), "radio": d.get("radio")}


def seed_nm():
    write_file(FAKE_NM, json.dumps({"profiles": [
        {"name": "HomeNet", "uuid": "u1", "type": "wifi", "props": {"connection.interface-name": "wlan0", "connection.autoconnect": "yes", "connection.autoconnect-priority": "0", "802-11-wireless.ssid": "HomeNet"}},
        {"name": "Wired connection 1", "uuid": "u2", "type": "ethernet", "props": {"connection.interface-name": "eth0", "connection.autoconnect": "yes", "connection.autoconnect-priority": "0"}}],
        "active": [{"name": "Wired connection 1", "uuid": "u2", "device": "eth0", "type": "ethernet", "mode": ""}], "radio": "enabled", "running": True, "fail": {}, "calls": [], "nmcli_calls": [], "seq": 2,
        "scan": [{"ssid": "HomeNet", "signal": 80, "security": "WPA2", "chan": 6}]}), 0o666)


def root_py(*args):
    return sh("tt-root-py " + " ".join(args))


def app(*args, timeout=120):
    """The app-side client as the unprivileged service user (what the web server does)."""
    env = f"TT_UPDATE_MODE=system TT_DATA_DIR={DATA} PYTHONPATH={APP}/app"
    r = sh(f"cd /tmp && runuser -u {SVC} -- env {env} {APP}/venv/bin/python -m traffic_tester.netcfg " + " ".join(f"'{a}'" for a in args), timeout=timeout)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "message": f"no JSON: {r.stdout[-200:]} {r.stderr[-300:]}"}


def timer():
    return rj(f"{SYSD}/tt-net-revert.json")


# =================================================================== part 1: the real installer
def part1():
    host = sh("hostname").stdout.strip()
    r = sh("bash /work/scripts/install-pi.sh --role B --no-service --no-wifi-nexmon", timeout=2400)
    out = r.stdout + r.stderr
    check("fresh install: installer succeeds", r.returncode == 0, out[-300:] if r.returncode else "exit 0")
    check("--role is accepted and ignored with the one-line note", "There are no fixed roles any more: every Pi is the same. --role is ignored." in out)
    check("final text is 'Installed. Pi name: <hostname>' (no Pi A/B, no Start Responder)", f"Installed. Pi name: {host}" in out and "Installed as Pi" not in out and "Responder" not in out, out[-400:])
    cfg = open("/etc/traffic-tester/config.env").read()
    check("config.env has no TT_ROLE", "TT_ROLE" not in cfg, cfg.replace("\n", " | "))
    kf = "/etc/NetworkManager/system-connections/tt-ap.nmconnection"
    txt = open(kf).read() if os.path.exists(kf) else ""
    ssid = host if len(host.encode()) <= 32 else host[:32]
    check("fresh install: the Wi-Fi is an access point, SSID = the Pi's name", f"ssid={ssid}" in txt and "mode=ap" in txt, txt[:200].replace("\n", " | "))
    check("access point: WPA2 password 'password', 10.42.0.1/24 shared (DHCP by NetworkManager), starts at boot", "psk=password" in txt and "key-mgmt=wpa-psk" in txt and "address1=10.42.0.1/24" in txt
          and "method=shared" in txt and "autoconnect=true" in txt)
    check("keyfile is root-only (0600)", oct(os.stat(kf).st_mode & 0o777) == "0o600" if txt else False)
    check("installer says where to connect", "http://10.42.0.1:8080" in out and "(password: password)" in out, "")
    pub = rj(f"{STATE}/net-state.json") or {}
    check("saved state: wifi_mode ap, no password in the world-readable file", pub.get("wifi_mode") == "ap" and "psk" not in json.dumps(pub) and oct(os.stat(f"{STATE}/net-private.json").st_mode & 0o777) == "0o600", json.dumps(pub)[:160])
    ch = open("/etc/chrony/conf.d/traffic-tester.conf").read()
    check("chrony: private-range allow lines, no local stratum, no server", all(f"allow {x}" in ch for x in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")) and "local stratum" not in ch and "\nserver" not in ch, ch.replace("\n", " | "))
    check("Ethernet is not marked never-default on the AP layout (no net-split)", "net split" not in out.lower() or "Network: Wi-Fi = internet" not in out)
    unit = "/etc/systemd/system/traffic-tester-netwatch.service"
    if os.path.exists(unit):
        u = open(unit).read()
        check("netwatch unit runs the root helper copy's netops.py watch", f"ExecStart=/usr/bin/python3 -I {HELP}/netops.py watch" in u and "Restart=always" in u, u[:200].replace("\n", " | "))
    else:
        check("netwatch unit is written when /etc/systemd/system exists", False, "no unit file (container without systemd dirs)") if os.path.isdir("/etc/systemd/system") else check("netwatch unit skipped without systemd", True, "no /etc/systemd/system in this container")
    check("root-owned helper copy has netops.py + netfake.py", all(os.path.exists(f"{HELP}/{f}") for f in ("netops.py", "netfake.py", "helper.py")) and os.stat(f"{HELP}/netops.py").st_uid == 0)

    # --- re-run on an existing install: the network is kept
    open("/etc/traffic-tester/config.env", "a").write("TT_ROLE=B\n")                  # a Pi installed before the roles went away
    kfa = open(kf).read()
    open(kf, "w").write(kfa.replace(f"ssid={ssid}", "ssid=keepme"))
    r = sh("bash /work/scripts/install-pi.sh --no-service --no-wifi-nexmon --ntp-server 192.168.9.9", timeout=2400)
    out = r.stdout + r.stderr
    kept = open(kf).read()
    check("re-run on an existing install keeps its Wi-Fi as it is (no automatic switch)", r.returncode == 0 and "ssid=keepme" in kept and "left as it is" in out, out[-200:])
    check("an existing TT_ROLE line survives a re-run (harmless; never written on a fresh install)", "TT_ROLE=B" in open("/etc/traffic-tester/config.env").read())
    ch = open("/etc/chrony/conf.d/traffic-tester.conf").read()
    check("--ntp-server writes exactly one server line (and still no local)", "server 192.168.9.9 iburst prefer minpoll 0 maxpoll 4" in ch.splitlines() and "local stratum" not in ch)
    r = sh("bash /work/scripts/install-pi.sh --no-service --no-wifi-nexmon --wifi-mode ap", timeout=2400)
    check("--wifi-mode ap on an existing install recreates the access point", r.returncode == 0 and f"ssid={ssid}" in open(kf).read(), (r.stdout + r.stderr)[-200:])
    sh(f"/usr/bin/python3 -I {HELP}/netops.py clock serve")                  # the app chooses "this Pi is the time source"
    r = sh("bash /work/scripts/install-pi.sh --no-service --no-wifi-nexmon", timeout=2400)
    check("a time source chosen in the app survives an installer re-run", "keeping the time source chosen in the app (serve)" in (r.stdout + r.stderr) and "local stratum 10" in open("/etc/chrony/conf.d/traffic-tester.conf").read())
    sh(f"/usr/bin/python3 -I {HELP}/netops.py clock none")


# =================================================================== part 2: the privileged helper, driven by the service user
def part2():
    host = sh("hostname").stdout.strip()
    make_fake_sys()
    install_shims()
    seed_nm()
    r = root_py(f"{HELP}/netops.py", "setup-default-ap", "--activate")
    ap = next((p for p in nm_state().get("profiles", []) if p["name"] == "tt-ap"), None)
    props = (ap or {}).get("props", {})
    check("nmcli path: default AP profile = what the installer chose (psk password, shared, 10.42.0.1/24, ap mode)", ap and props.get("wifi-sec.psk") == "password" and props.get("ipv4.method") == "shared"
          and props.get("ipv4.addresses") == "10.42.0.1/24" and props.get("802-11-wireless.mode") == "ap" and props.get("802-11-wireless.ssid") == host[:32], r.stdout + r.stderr)
    check("... and it is active on wlan0", ("tt-ap", "wlan0") in nm_view()["active"], nm_view()["active"])

    # ---- permissions: the service user reads the public state, never the secrets, never writes the state dir
    pr = sh(f"runuser -u {SVC} -- cat {STATE}/net-state.json >/dev/null && echo READ_OK")
    sec = sh(f"runuser -u {SVC} -- cat {STATE}/net-private.json")
    wr = sh(f"runuser -u {SVC} -- touch {STATE}/x")
    check("service user can read net-state.json but not net-private.json, and cannot write the state dir", "READ_OK" in pr.stdout and sec.returncode != 0 and wr.returncode != 0, (sec.stderr + wr.stderr)[:160])

    # ---- apply -> pending + the scheduled revert -> Keep
    before = nm_view()
    chg = {"ports": {"eth0": {"method": "static", "address": "192.168.50.2/24", "gateway": "192.168.50.1", "label": "test"}}}
    res = app("apply", json.dumps(chg))
    t = timer()
    check("apply through the helper: ok, 90 s countdown pending", res.get("ok") and (res.get("pending") or {}).get("state") == "waiting" and res["pending"]["seconds"] == 90, json.dumps(res)[:200])
    check("revert was scheduled with systemd-run: 90 s, unit tt-net-revert, root helper copy, 'net-revert'", t and t["seconds"] == 90 and t["unit"] == "tt-net-revert" and t["argv"][-1] == "net-revert" and t["argv"][-3:-1] == ["-I", f"{HELP}/helper.py"], json.dumps(t))
    prof = {p["name"]: p["props"] for p in nm_state()["profiles"]}
    check("eth0 profile tt-eth-eth0: static, label Test = never default route", prof.get("tt-eth-eth0", {}).get("ipv4.method") == "manual" and prof["tt-eth-eth0"].get("ipv4.never-default") == "yes")
    check("pending file is world-readable, snapshot is root-only, result holds no secrets", oct(os.stat(f"{STATE}/net-pending.json").st_mode & 0o777) == "0o644" and oct(os.stat(f"{STATE}/net-snapshot.json").st_mode & 0o777) == "0o600"
          and "psk" not in open(f"{STATE}/net-result.json").read())
    pend = app("pending")
    check("pending is readable by the app (works after a reconnect)", (pend.get("pending") or {}).get("seconds_left", 0) > 0, json.dumps(pend)[:160])
    k = app("keep")
    check("Keep cancels the revert timer and makes the change permanent", k.get("ok") and timer() is None and not os.path.exists(f"{STATE}/net-pending.json") and "tt-eth-eth0" in str(nm_view()), json.dumps(k)[:160])

    # ---- apply -> the timer fires -> exact restore
    before = nm_view()
    files_before = {f: (rj(f"{STATE}/{f}") or {}) for f in ("net-state.json", "net-private.json")}
    for f in files_before.values():
        f.pop("last_event", None)
        f.pop("updated", None)
    res = app("apply", json.dumps({"wifi": {"mode": "client", "ssid": "HomeNet", "password": "homepassword1"}, "ports": {"eth1": {"method": "dhcp", "label": "internet", "vlan": 30}}}))
    changed = nm_view()
    check("second apply changed Wi-Fi + eth1 (VLAN 30)", res.get("ok") and changed != before and "tt-wifi-1" in str(changed) and "tt-eth-eth1-v30" in str(changed) and ("tt-wifi-1", "wlan0") in changed["active"], json.dumps(res)[:200])
    t = timer()
    early = sh(" ".join(["tt-root-py"] + t["argv"][1:]))                      # the timer fires too early (the countdown was extended): nothing happens
    check("a timer firing before the deadline does nothing", nm_view() == changed, early.stdout.strip())
    pend = rj(f"{STATE}/net-pending.json")
    pend["deadline"] = time.time() - 5
    write_file(f"{STATE}/net-pending.json", json.dumps(pend), 0o644)
    fired = sh(" ".join(["tt-root-py"] + t["argv"][1:]))                      # == systemd running the recorded command after 90 s
    after = nm_view()
    files_after = {f: (rj(f"{STATE}/{f}") or {}) for f in ("net-state.json", "net-private.json")}
    for f in files_after.values():
        f.pop("last_event", None)
        f.pop("updated", None)
    check("revert at the deadline restores the previous profiles, active connections and radio exactly", after == before and files_after == files_before, f"{fired.stdout.strip()} {fired.stderr.strip()[:120]}")
    check("... and the app can show what happened", (rj(f"{STATE}/net-state.json") or {}).get("last_event", {}).get("reason") == "timeout" and not os.path.exists(f"{STATE}/net-pending.json") and timer() is None)

    # ---- a failed join is undone at once
    nm_update(fail={"tt-wifi-1": "(7) Secrets were required, but not provided."})
    before = nm_view()
    res = app("apply", json.dumps({"wifi": {"mode": "client", "ssid": "HomeNet", "password": "wrongpassword1"}}))
    check("wrong Wi-Fi password: refused with a plain reason and everything put back at once", res.get("ok") is False and "password" in res.get("message", "") and nm_view() == before and timer() is None, res.get("message", "")[:200])
    nm_update(fail={})

    # ---- clock op
    c = app("clock", "client", "--server", "192.168.50.1")
    conf = open("/etc/chrony/conf.d/traffic-tester.conf").read()
    check("clock client: server line + allow lines, no local", c.get("ok") and "server 192.168.50.1 iburst prefer minpoll 0 maxpoll 4" in conf.splitlines() and "local stratum" not in conf and "allow 192.168.0.0/16" in conf, json.dumps(c)[:160])
    c = app("clock", "serve")
    conf = open("/etc/chrony/conf.d/traffic-tester.conf").read()
    check("clock serve: local stratum 10 + allow lines, no server", c.get("ok") and "local stratum 10" in conf and "\nserver" not in conf and "allow 10.0.0.0/8" in conf)
    bad = app("clock", "client", "--server", "1.2.3.4; reboot")
    check("clock refuses an invalid server and keeps the file", bad.get("ok") is False and conf == open("/etc/chrony/conf.d/traffic-tester.conf").read(), bad.get("message", "")[:120])
    calls = [json.loads(x) for x in open(f"{SYSD}/calls.log")]
    check("chrony was restarted by the helper", ["systemctl", "restart", "chrony"] in calls)
    st = app("clock", "status")
    check("clock status reports serve", st.get("mode") == "serve", json.dumps(st)[:120])

    # ---- hostile requests written straight into the request file by the service user
    for name, req in (("unknown op with a command", {"action": "net", "id": "h1", "op": "exec", "cmd": "touch /pwned"}), ("shell metacharacters in an interface", {"action": "net", "id": "h2", "op": "apply", "changes": {"ports": {"eth0;touch /pwned": {"method": "dhcp"}}}}),
                      ("short WPA password", {"action": "net", "id": "h3", "op": "apply", "changes": {"wifi": {"mode": "ap", "ssid": "x", "password": "short"}}}), ("unknown action", {"action": "reboot", "id": "h4"})):
        sh(f"runuser -u {SVC} -- tee {DATA}/update/request.json >/dev/null", input=json.dumps(req))
        subprocess.run(["runuser", "-u", SVC, "--", "systemctl", "start", "--no-block", "traffic-tester-update.service"])
        for _ in range(40):
            time.sleep(0.25)
            r = rj(f"{STATE}/net-result.json") or {}
            if r.get("id") == req["id"]:
                break
        check(f"hostile request refused: {name}", not os.path.exists("/pwned") and (r.get("ok") is False or req["action"] == "reboot"), json.dumps(r)[:160])
        time.sleep(0.3)

    # ---- fallback: no network for 3 minutes -> emergency AP; back -> no AP; retry cycle
    carrier("eth0", False)
    carrier("eth1", False)
    nm_update(active=[], radio="enabled")
    saved = sorted(p["name"] for p in nm_state()["profiles"])
    sh(f"rm -f {STATE}/net-watch.json")
    acts = {}
    for tsec in range(0, 181, 10):
        r = root_py(f"{HELP}/netops.py", "watch", "--once", "--now", str(tsec))
        if r.stdout.strip():
            acts[tsec] = r.stdout.strip()
    active = nm_view()["active"]
    fb = next((p for p in nm_state()["profiles"] if p["name"] == "tt-fallback-ap"), None)
    check("no network for 3 minutes -> the emergency access point comes up (not before)", list(acts) == [180] and ("tt-fallback-ap", "wlan0") in active, json.dumps(acts))
    check("fallback AP is a separate profile (no autoconnect, password 'password'); the saved ones are untouched", fb and fb["props"]["connection.autoconnect"] == "no" and fb["props"]["wifi-sec.psk"] == "password"
          and sorted(p["name"] for p in nm_state()["profiles"] if p["name"] != "tt-fallback-ap") == saved)
    carrier("eth0", True)
    r = root_py(f"{HELP}/netops.py", "watch", "--once", "--now", "190")
    check("network back (cable) -> the emergency AP goes away", "ap_down" in r.stdout and ("tt-fallback-ap", "wlan0") not in nm_view()["active"], r.stdout.strip())
    # retry cycle: a Pi whose saved mode is "join the home Wi-Fi" (the home Wi-Fi is gone), not "access point" (an AP that is already up needs no fallback)
    carrier("eth0", False)
    d = nm_state()
    for p in d["profiles"]:
        if p["name"] == "tt-ap":
            p["props"]["connection.autoconnect"] = "no"
    d.update(active=[], fail={"HomeNet": "no network with ssid 'HomeNet' could be found"})
    write_file(FAKE_NM, json.dumps(d), 0o666)
    pub = rj(f"{STATE}/net-state.json")
    pub["wifi_mode"] = "client"
    write_file(f"{STATE}/net-state.json", json.dumps(pub))
    sh(f"rm -f {STATE}/net-watch.json")
    for tsec in range(0, 181, 10):
        root_py(f"{HELP}/netops.py", "watch", "--once", "--now", str(tsec))
    seen = []
    for tsec in range(190, 521, 10):
        r = root_py(f"{HELP}/netops.py", "watch", "--once", "--now", str(tsec))
        if r.stdout.strip():
            seen.append((tsec, json.loads(r.stdout.strip().splitlines()[-1])["action"]))
    check("retry cycle: 5 minutes after the AP came up the normal profiles are tried for 30 s, then the AP returns", seen == [(480, "retry"), (510, "ap_up")], json.dumps(seen))


# =================================================================== part 3: uninstall
def part3():
    nm_update(profiles=nm_state()["profiles"] + [{"name": "tt-wifi-9", "uuid": "z", "type": "wifi", "props": {}}])
    r = sh("bash /work/scripts/install-pi.sh --uninstall --no-service", timeout=600)
    names = [p["name"] for p in nm_state()["profiles"]]
    check("uninstall removes the app's tt-* profiles and the AP keyfile, keeps everybody else's", not [n for n in names if n.startswith("tt-")] and "HomeNet" in names and "Wired connection 1" in names
          and not os.path.exists("/etc/NetworkManager/system-connections/tt-ap.nmconnection"), f"{names} {r.stdout[-100:]}")


def main():
    for fn in (part1, part2, part3):
        try:
            fn()
        except Exception as e:                                                 # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{fn.__name__} ran to the end", False, repr(e))
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
