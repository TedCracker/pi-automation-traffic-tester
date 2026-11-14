"""Built-in Wi-Fi (Nexmon) helper + client, tested against FAKE iw / modprobe / insmod / update-alternatives / nmcli / dmesg
(no real radio here: the module/firmware behaviour itself can only be verified on a Pi 5)."""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import textwrap
import time

import pytest

FAKE = r'''#!{py}
import json, os, sys
R = os.environ["TT_WIFI_ROOT"]
SP = R + "/fakestate.json"
LOG = R + "/fakecalls.log"
st = json.load(open(SP))
name = os.environ["TT_FAKE_NAME"]; a = sys.argv[1:]
open(LOG, "a").write(name + " " + " ".join(a) + "\n")
fw = R + "/lib/firmware/cypress/cyfmac43455-sdio.bin"
def save(): json.dump(st, open(SP, "w"))
def link(t):
    try: os.remove(fw)
    except OSError: pass
    os.symlink(t, fw)
if name == "iw":
    if a[:1] == ["dev"] and len(a) == 1:
        out = ""
        if st["iface"]: out += "phy#0\n\tInterface wlan0\n\t\ttype managed\n"
        if st["mon"]: out += "\tInterface wlan0mon\n\t\ttype monitor\n"
        print(out, end="")
    elif a[:1] == ["dev"] and "interface" in a and "add" in a:
        if st.get("fail_monitor"): print("command failed: Operation not supported (-95)", file=sys.stderr); sys.exit(237)
        st["mon"] = True; save()
    elif a[:1] == ["dev"] and a[-1] == "del":
        st["mon"] = False; save()
    elif "set" in a and "channel" in a:
        if st.get("fail_channel"): print("command failed: Invalid argument (-22)", file=sys.stderr); sys.exit(234)
        st["channel"] = a[a.index("channel") + 1]; save()
elif name == "ip": pass
elif name == "nmcli": pass
elif name in ("modprobe", "rmmod"):
    if a[:1] == ["-r"] or name == "rmmod":
        if st.get("busy"): print("rmmod: ERROR: Module is in use", file=sys.stderr); sys.exit(1)
        st["module"] = None; st["iface"] = False; save()
    elif a == ["brcmfmac"]:
        st["module"] = "stock"; st["iface"] = True; save()
elif name == "insmod":
    if st.get("fail_insmod"): print("insmod: ERROR: could not insert module: Invalid module format", file=sys.stderr); sys.exit(1)
    st["module"] = "nexmon"; st["iface"] = not st.get("no_iface")
    if st.get("fw_crash"): st["dmesg"] = ["brcmfmac: brcmf_sdio_readframes: firmware crashed (TRAP) dongle trap", "brcmfmac: brcmf_c_preinit_dcmds: Firmware failed -110"]
    save()
elif name == "update-alternatives":
    if a[0] == "--query": sys.exit(0 if st.get("alt_group", True) else 2)
    if a[0] == "--set": link(a[2])
    if a[0] == "--auto": link("cyfmac43455-sdio-standard.bin")
elif name == "dmesg":
    print("\n".join(st.get("dmesg", [])))
elif name == "aireplay-ng":
    print("Injection is working!" if st.get("inject_ok") else "No answer")
'''


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = str(tmp_path)
    fwdir = tmp_path / "lib/firmware/cypress"
    fwdir.mkdir(parents=True)
    (fwdir / "cyfmac43455-sdio-standard.bin").write_bytes(b"stock")
    (fwdir / "cyfmac43455-sdio-nexmon.bin").write_bytes(b"nexmon")
    os.symlink("cyfmac43455-sdio-standard.bin", fwdir / "cyfmac43455-sdio.bin")
    (tmp_path / "run/traffic-tester").mkdir(parents=True)
    kver = "6.12.109+rpt-rpi-2712"
    mod = tmp_path / f"opt/traffic-tester/nexmon/{kver}"
    mod.mkdir(parents=True)
    (mod / "brcmfmac.ko").write_bytes(b"ko")
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    fake = bindir / "_fake.py"
    fake.write_text(FAKE.replace("{py}", sys.executable))
    for n in ("iw", "ip", "nmcli", "modprobe", "rmmod", "insmod", "update-alternatives", "dmesg", "aireplay-ng", "airmon-ng"):
        (bindir / n).write_text(f"#!/bin/sh\nTT_FAKE_NAME={n} exec {sys.executable} {fake} \"$@\"\n")
        (bindir / n).chmod(0o755)
    state = {"iface": True, "mon": False, "module": "stock", "dmesg": []}
    (tmp_path / "fakestate.json").write_text(json.dumps(state))
    monkeypatch.setenv("TT_WIFI_ROOT", root)
    monkeypatch.setenv("TT_WIFI_KVER", kver)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("TT_WIFI_HELPER_DIRECT", sys.executable)
    monkeypatch.setenv("TT_WIFI_RUN_DIR", root + "/run/traffic-tester")
    monkeypatch.setenv("TT_WIFI_HELPER_DIR", root + "/run/tt-wifi")
    spec = importlib.util.spec_from_file_location("wifi_helper_under_test", os.path.join(os.path.dirname(__file__), "..", "traffic_tester", "sniff", "wifi_helper.py"))
    H = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(H)

    class E:
        helper, root_, kver_ = H, root, kver

        @staticmethod
        def fake(**kw):
            s = json.load(open(root + "/fakestate.json")); s.update(kw); json.dump(s, open(root + "/fakestate.json", "w"))

        @staticmethod
        def st():
            return json.load(open(root + "/fakestate.json"))

        @staticmethod
        def calls():
            try:
                return open(root + "/fakecalls.log").read().splitlines()
            except OSError:
                return []

        @staticmethod
        def request(iface="wlan0", channel=6, width="HT20"):
            json.dump({"iface": iface, "channel": channel, "width": width}, open(root + "/run/traffic-tester/wifi-request.json", "w"))

        @staticmethod
        def run(cmd):
            return H.COMMANDS[cmd]()
        alt = staticmethod(lambda: os.path.basename(os.path.realpath(root + "/lib/firmware/cypress/cyfmac43455-sdio.bin")))
    return E


def test_status_reports_ready_and_missing_pieces(env):
    r = env.run("status")["status"]
    assert r["ready"] and r["nexmon_module"] and r["nexmon_firmware"] and r["kernel"] == env.kver_ and r["state"] == "stock"
    os.remove(env.root_ + f"/opt/traffic-tester/nexmon/{env.kver_}/brcmfmac.ko")
    r = env.run("status")["status"]
    assert not r["ready"] and not r["nexmon_module"]


def test_monitor_success_and_restore_roundtrip(env):
    env.request(channel=11, width="HT40+")
    res = env.run("monitor")
    assert res["ok"] and res["monitor_iface"] == "wlan0mon"
    assert env.alt() == "cyfmac43455-sdio-nexmon.bin"                       # Nexmon firmware selected only while monitoring
    s = env.st()
    assert s["mon"] and s["module"] == "nexmon" and s["channel"] == "11"
    calls = env.calls()
    assert any(c.startswith("nmcli device set wlan0 managed no") for c in calls) and any("set channel 11 HT40+" in c for c in calls)
    assert env.helper.get_state()["state"] == "monitor"
    out = env.run("restore")
    assert out["ok"] and out["code"] == "restored"
    s = env.st()
    assert not s["mon"] and s["module"] == "stock" and s["iface"]
    assert env.alt() == "cyfmac43455-sdio-standard.bin"                      # stock firmware back
    calls = env.calls()
    assert any(c.startswith("nmcli device set wlan0 managed yes") for c in calls) and any(c.startswith("nmcli device connect wlan0") for c in calls) and any(c.startswith("nmcli radio wifi on") for c in calls)
    assert env.helper.get_state()["state"] == "stock"
    assert env.run("restore")["ok"]                                          # idempotent


@pytest.mark.parametrize("fail,code,words", [
    ({"fail_insmod": True}, "module_load_failed", "would not load"),
    ({"no_iface": True}, "interface_missing", "did not come back"),
    ({"fw_crash": True, "no_iface": True}, "firmware_crash", "Kernel log"),
    ({"fw_crash": True}, "firmware_crash", "reported a problem"),
    ({"fail_monitor": True}, "monitor_failed", "Monitor mode did not start"),
    ({"fail_channel": True}, "channel_failed", "Could not set channel"),
])
def test_every_failure_is_plain_and_wifi_is_restored_automatically(env, fail, code, words):
    env.fake(**fail)
    env.request()
    res = env.run("monitor")
    assert not res["ok"] and res["code"] == code and words in res["message"]
    assert "Normal Wi-Fi was restored" in res["message"] and res["restored"]
    s = env.st()
    assert s["module"] == "stock" and s["iface"] and not s["mon"] and env.alt() == "cyfmac43455-sdio-standard.bin"       # never left half-switched
    assert env.helper.get_state()["state"] == "stock"


def test_missing_nexmon_for_this_kernel_names_the_kernels_that_exist(env):
    os.rename(env.root_ + f"/opt/traffic-tester/nexmon/{env.kver_}", env.root_ + "/opt/traffic-tester/nexmon/6.12.50+rpt-rpi-2712")
    env.request()
    with pytest.raises(env.helper.HelperError) as e:
        env.helper.cmd_monitor()
    assert e.value.code == "nexmon_missing" and "6.12.50+rpt-rpi-2712" in e.value.message and env.kver_ in e.value.message
    assert env.calls() == [] or not any(c.startswith(("insmod", "rmmod")) for c in env.calls())          # nothing was touched


@pytest.mark.parametrize("req", [{"iface": "wlan0; rm -rf /", "channel": 6}, {"iface": "eth0", "channel": 6}, {"iface": "wlan0", "channel": 999}, {"iface": "wlan0", "channel": "6 -x"},
                                 {"iface": "wlan0", "channel": 6, "width": "HT99"}, {"iface": "../../etc", "channel": 1}])
def test_request_validation_rejects_hostile_input_before_running_anything(env, req):
    json.dump(req, open(env.root_ + "/run/traffic-tester/wifi-request.json", "w"))
    with pytest.raises(env.helper.HelperError) as e:
        env.helper.load_request()
    assert e.value.code == "bad_request" and env.calls() == []
    os.remove(env.root_ + "/run/traffic-tester/wifi-request.json")
    with pytest.raises(env.helper.HelperError):
        env.helper.load_request()                                            # no file at all


def test_watchdog_restores_when_the_app_stops_and_leaves_a_live_session_alone(env, monkeypatch):
    monkeypatch.setattr(env.helper, "HEARTBEAT_TTL", 2)
    env.request()
    assert env.run("monitor")["ok"]
    hb = env.root_ + "/run/traffic-tester/wifi-heartbeat"
    open(hb, "w").write("x")
    assert env.run("watchdog")["code"] == "watchdog_ok" and env.helper.get_state()["state"] == "monitor"       # fresh heartbeat: untouched
    old = time.time() - 30
    os.utime(hb, (old, old))
    s = env.helper.get_state(); s["since"] = old; json.dump(s, open(env.root_ + "/run/tt-wifi/state.json", "w"))
    res = env.run("watchdog")                                                  # app died / crashed: no heartbeat
    assert res["code"] == "restored" and "watchdog" in res["reason"] and env.helper.get_state()["state"] == "stock" and env.alt() == "cyfmac43455-sdio-standard.bin"


def test_fresh_monitor_session_gets_a_grace_period_before_the_first_heartbeat(env, monkeypatch):
    monkeypatch.setattr(env.helper, "HEARTBEAT_TTL", 30)
    env.request()
    assert env.run("monitor")["ok"]                                            # no heartbeat file yet
    assert env.run("watchdog")["code"] == "watchdog_ok"


def test_watchdog_clears_stuck_switching_and_failed_states(env, monkeypatch):
    monkeypatch.setattr(env.helper, "SWITCH_TIMEOUT", 1)
    env.helper.set_state(state="switching", base="wlan0")
    assert env.run("watchdog")["code"] == "watchdog_ok"                          # recent: still working
    s = env.helper.get_state(); s["updated"] = time.time() - 50; json.dump(s, open(env.root_ + "/run/tt-wifi/state.json", "w"))
    assert env.run("watchdog")["code"] == "restored"
    env.helper.set_state(state="failed", base="wlan0")
    assert env.run("watchdog")["code"] == "restored"


def test_boot_restore_if_needed_only_acts_when_nexmon_firmware_was_left_selected(env):
    assert env.helper.main(["x", "restore", "--if-needed"]) == 0
    assert json.load(open(env.root_ + "/run/tt-wifi/result.json"))["code"] == "nothing_to_do"
    os.remove(env.root_ + "/lib/firmware/cypress/cyfmac43455-sdio.bin")
    os.symlink("cyfmac43455-sdio-nexmon.bin", env.root_ + "/lib/firmware/cypress/cyfmac43455-sdio.bin")      # the Pi rebooted with the Nexmon firmware still selected
    assert env.helper.main(["x", "restore", "--if-needed"]) == 0
    assert json.load(open(env.root_ + "/run/tt-wifi/result.json"))["code"] == "restored" and env.alt() == "cyfmac43455-sdio-standard.bin"


def test_firmware_link_fallback_when_there_is_no_alternatives_group(env):
    env.fake(alt_group=False)
    for n in ("update-alternatives",):
        open(env.root_ + f"/fakebin/{n}", "w").write(f"#!/bin/sh\nexit 2\n")
    env.request()
    assert env.run("monitor")["ok"] and env.alt() == "cyfmac43455-sdio-nexmon.bin"
    assert env.run("restore")["ok"] and env.alt() == "cyfmac43455-sdio-standard.bin"                        # symlink backed up and put back


def test_inject_test_is_experimental_and_needs_monitor_mode(env):
    with pytest.raises(env.helper.HelperError) as e:
        env.helper.cmd_inject_test()
    assert e.value.code == "not_monitoring"
    env.request()
    env.run("monitor")
    env.fake(inject_ok=True)
    assert env.run("inject-test")["ok"]
    env.fake(inject_ok=False)
    r = env.run("inject-test")
    assert not r["ok"] and "experimental" in r["message"]


def test_usage_and_unknown_command(env, capsys):
    assert env.helper.main(["x"]) == 2 and env.helper.main(["x", "format-disk"]) == 2


# ---------------------------------------------------------------- client (what the web app uses)
def test_client_monitor_restore_heartbeat_and_errors(env):
    from traffic_tester.sniff import builtin_wifi as B
    assert B.status()["available"] and B.status()["state"] == "stock"
    mon = B.monitor("wlan0", 6)
    assert mon == "wlan0mon" and os.path.exists(env.root_ + "/run/traffic-tester/wifi-heartbeat") and B.status()["state"] == "monitor"
    B.set_channel("wlan0", 36, "HT40+")
    assert env.st()["channel"] == "36"
    out = B.restore()
    assert out["ok"] and not os.path.exists(env.root_ + "/run/traffic-tester/wifi-heartbeat")
    env.fake(fail_insmod=True)
    with pytest.raises(B.BuiltinWifiError) as e:
        B.monitor("wlan0", 6)
    assert e.value.code == "module_load_failed" and "restored" in e.value.message
    assert B.status()["state"] == "stock"


def test_nexmon_monitor_object_hops_and_always_restores(env):
    from traffic_tester.sniff import builtin_wifi as B
    m = B.NexmonMonitor("builtin", 6, "HT20", [1, 6, 11], 0.15)
    m.up()
    deadline = time.time() + 20           # each hop starts the helper process; emulated CPUs (QEMU) need seconds, a Pi needs milliseconds
    while time.time() < deadline and not set(m.visited) >= {1, 6, 11}:
        time.sleep(0.1)
    assert m.monitor_iface == "wlan0mon" and set(m.visited) >= {1, 6, 11}
    m.down()
    assert env.st()["module"] == "stock" and not env.st()["mon"] and env.alt() == "cyfmac43455-sdio-standard.bin"


def test_status_when_nexmon_not_installed_explains_what_to_do(env):
    from traffic_tester.sniff import builtin_wifi as B
    os.remove(env.root_ + f"/opt/traffic-tester/nexmon/{env.kver_}/brcmfmac.ko")
    s = B.status()
    assert not s["available"] and "not installed for this kernel" in s["reason"] and s["warning"].startswith("Internet over Wi-Fi will drop")


# ---------------------------------------------------------------- run / pre-flight / API integration
def _prof(**wifi):
    from traffic_tester import profiles as P
    return P.validate_profile({"name": "w", "kind": "sniffer", "sniff": {"mode": "wifi", "wifi": {"interface": "builtin", **wifi}}})


def test_builtin_run_needs_the_explicit_ok_and_says_why(env):
    from traffic_tester.sniff import session
    s = session.SnifferRun(_prof(), tempfile.mkdtemp(), lambda e: None).run()
    assert "normal Wi-Fi off" in s["error"] and "Tick the box" in s["error"] and "Internet over Wi-Fi will drop" in s["error"]


def test_builtin_run_failure_is_plain_and_restores(env):
    from traffic_tester.sniff import session
    env.fake(fail_insmod=True)
    s = session.SnifferRun(_prof(builtin_ack=True), tempfile.mkdtemp(), lambda e: None).run()
    assert "would not load" in s["error"] and "Normal Wi-Fi was restored" in s["error"] and env.st()["module"] == "stock"


def test_preflight_for_builtin_wifi_flags_every_risk(env, monkeypatch):
    from traffic_tester.sniff import preflight as pre
    monkeypatch.setattr(pre, "route_dev", lambda ip: "wlan0")
    monkeypatch.setattr(pre, "wifi_default_route_dev", lambda: "wlan0")
    r = pre.preflight(_prof(), "192.168.1.20")
    titles = {c["title"]: c["s"] for c in r["checks"]}
    assert titles["You are connected through Wi-Fi"] == "bad" and titles["The Pi's internet is on Wi-Fi"] == "warn" and titles["Please confirm"] == "bad" and titles["Nexmon is installed"] == "ok"
    monkeypatch.setattr(pre, "route_dev", lambda ip: "eth0")
    r = pre.preflight(_prof(builtin_ack=True), "192.168.1.20")
    assert not any(c["s"] == "bad" for c in r["checks"] if c["title"] != "Capture tool found") or all(c["s"] != "bad" for c in r["checks"] if "Wi-Fi" in c["title"] or c["title"] == "Please confirm")


def test_api_refuses_builtin_sniffing_over_wifi_and_restore_endpoint(env, monkeypatch):
    from fastapi.testclient import TestClient
    from traffic_tester.server import create_app
    from traffic_tester.sniff import preflight as pre
    monkeypatch.setattr(pre, "route_dev", lambda ip: "wlan0")
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        r = c.post("/api/runs", json={"profile": {"name": "w", "kind": "sniffer", "sniff": {"mode": "wifi", "wifi": {"interface": "builtin", "builtin_ack": True}}}})
        assert r.status_code == 422 and "over Wi-Fi" in r.json()["detail"] and "Ethernet" in r.json()["detail"]
        env.request()
        env.run("monitor")
        assert c.get("/api/wifi/builtin/status").json()["state"] == "monitor"
        rr = c.post("/api/wifi/builtin/restore")
        assert rr.status_code == 200 and rr.json()["ok"] and c.get("/api/wifi/builtin/status").json()["state"] == "stock"
        assert c.post("/api/wifi/builtin/inject-test").status_code == 409                        # not monitoring
