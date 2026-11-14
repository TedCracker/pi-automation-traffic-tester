"""Network settings: nmcli profiles, strict validation, plan (affects the connection in use?), apply -> snapshot -> revert, Keep, the 90 s revert timer,
the dead-man fallback decision, the default access point, path_warning, and the HTTP API. No NetworkManager is touched: a fake nmcli keeps profiles in a JSON file."""
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from netcfgfix import Env, kv
from traffic_tester.netcfg import api as netapi, client, paths, plan as planmod, state as statemod, watchdog
from traffic_tester.update import netops
from traffic_tester.update.netops import NetError


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def err(fn, *a, **k):
    with pytest.raises(NetError) as e:
        fn(*a, **k)
    return e.value


AP = {"name": "tt-ap", "iface": "wlan0", "ssid": "pi1", "psk": "password", "band": "bg", "channel": 6, "autoconnect": True}


# ------------------------------------------------------------------ profile -> argv
def test_ap_profile_argv():
    (argv, name), = netops.add_commands("ap", AP)
    assert name == "tt-ap" and argv[:4] == ["nmcli", "connection", "add", "type"]
    p = kv(argv)
    assert p["con-name"] == "tt-ap" and p["ifname"] == "wlan0" and p["802-11-wireless.mode"] == "ap" and p["802-11-wireless.ssid"] == "pi1"
    assert p["ipv4.method"] == "shared" and p["ipv4.addresses"] == "10.42.0.1/24" and p["wifi-sec.key-mgmt"] == "wpa-psk" and p["wifi-sec.psk"] == "password"
    assert p["wifi-sec.proto"] == "rsn" and p["wifi-sec.pairwise"] == "ccmp" and p["802-11-wireless.band"] == "bg" and p["802-11-wireless.channel"] == "6"
    assert p["connection.autoconnect"] == "yes" and p["connection.autoconnect-priority"] == "100" and p["ipv6.method"] == "ignore"
    assert all(isinstance(x, str) for x in argv)


def test_ap_auto_channel_and_5ghz():
    spec = dict(AP, band="a", channel=36)
    p = kv(netops.add_commands("ap", spec)[0][0])
    assert p["802-11-wireless.band"] == "a" and p["802-11-wireless.channel"] == "36"
    p = kv(netops.add_commands("ap", dict(AP, channel=None))[0][0])
    assert "802-11-wireless.channel" not in p


def test_client_profile_argv_secured_open_hidden():
    spec = {"name": "tt-wifi-1", "iface": "wlan0", "ssid": "HomeNet", "psk": "hunter2hunter2", "security": "wpa-psk", "hidden": True, "autoconnect": True}
    p = kv(netops.add_commands("client", spec)[0][0])
    assert p["802-11-wireless.ssid"] == "HomeNet" and p["wifi-sec.psk"] == "hunter2hunter2" and p["wifi-sec.key-mgmt"] == "wpa-psk" and p["802-11-wireless.hidden"] == "yes"
    assert p["ipv4.method"] == "auto" and p["connection.autoconnect-priority"] == "50"
    p = kv(netops.add_commands("client", dict(spec, security="none", psk=None, hidden=False))[0][0])
    assert "wifi-sec.psk" not in p and "802-11-wireless.hidden" not in p
    assert kv(netops.add_commands("client", dict(spec, security="sae"))[0][0])["wifi-sec.key-mgmt"] == "sae"


def test_ethernet_dhcp_static_vlan_and_labels():
    dhcp = {"iface": "eth0", "method": "dhcp", "address": None, "gateway": None, "dns": [], "vlan": None, "label": "both"}
    (argv, name), = netops.add_commands("port", dhcp)
    p = kv(argv)
    assert name == "tt-eth-eth0" and p["type"] == "ethernet" and p["ipv4.method"] == "auto" and p["ipv4.never-default"] == "no" and p["ipv6.never-default"] == "no"
    p = kv(netops.add_commands("port", dict(dhcp, label="test"))[0][0])
    assert p["ipv4.never-default"] == "yes" and p["ipv6.never-default"] == "yes"                  # Test = never the default route (the old network split)
    assert kv(netops.add_commands("port", dict(dhcp, label="internet"))[0][0])["ipv4.never-default"] == "no"
    st = dict(dhcp, method="static", address="192.168.7.20/24", gateway="192.168.7.1", dns=["1.1.1.1", "9.9.9.9"], label="internet")
    p = kv(netops.add_commands("port", st)[0][0])
    assert p["ipv4.method"] == "manual" and p["ipv4.addresses"] == "192.168.7.20/24" and p["ipv4.gateway"] == "192.168.7.1" and p["ipv4.dns"] == "1.1.1.1,9.9.9.9"
    v = netops.add_commands("port", dict(st, vlan=20))
    assert [n for _, n in v] == ["tt-eth-eth0", "tt-eth-eth0-v20"]
    parent, child = kv(v[0][0]), kv(v[1][0])
    assert parent["ipv4.method"] == "disabled"                                                   # the address lives on the tagged interface
    assert child["type"] == "vlan" and child["ifname"] == "eth0.20" and child["dev"] == "eth0" and child["id"] == "20" and child["ipv4.addresses"] == "192.168.7.20/24"


def test_special_characters_stay_one_argument_never_a_shell():
    ssid = 'a; reboot "$(x)" `y`'
    (argv, _), = netops.add_commands("ap", dict(AP, ssid=ssid))
    assert argv[argv.index("802-11-wireless.ssid") + 1] == ssid and argv.count(ssid) == 1
    assert isinstance(argv, list) and "sh" not in argv[:1]


# ------------------------------------------------------------------ strict validation
def test_ssid_and_password_rules():
    assert netops.v_ssid("pi1") == "pi1" and netops.v_ssid("a" * 32)
    for bad in ("", "a" * 33, "é" * 17, "x\ny", "x\x00y", None, 5, "\x1b[31m"):
        assert err(netops.v_ssid, bad).code == "bad_ssid"
    assert netops.v_psk("12345678") and netops.v_psk("a" * 63)
    for bad in ("1234567", "a" * 64, "pässword1", "pass\nword1", None, "pass\x00word"):
        assert err(netops.v_psk, bad).code == "bad_password"


def test_ip_mask_gateway_dns_vlan_rules():
    assert netops.v_address("192.168.1.20/24") == ("192.168.1.20", 24) and netops.v_address("10.0.0.5", "255.255.0.0") == ("10.0.0.5", 16)
    for bad in ("1.2.3.4; reboot", "256.1.1.1", "1.2.3", "01.2.3.4", "1.2.3.4 ", "$(id)", "::1", "", None, "1.2.3.4/24/1"):
        assert err(netops.v_address, bad, "255.255.255.0").code in ("bad_ip", "bad_mask"), bad
    for bad in ("127.0.0.1", "0.0.0.0", "224.0.0.1", "169.254.1.1", "255.255.255.255", "192.168.1.0/24", "192.168.1.255/24"):
        assert err(netops.v_address, bad, 24 if "/" not in bad else None).code == "bad_ip", bad
    for bad in ("255.0.255.0", "255.255.255.255", "/31", "/7", 33, "abc", "255.255.255.1"):
        assert err(netops.v_prefix, bad).code == "bad_mask", bad
    assert netops.v_gateway("192.168.1.1", "192.168.1.20", 24) == "192.168.1.1"
    assert err(netops.v_gateway, "192.168.2.1", "192.168.1.20", 24).code == "bad_gateway"
    assert err(netops.v_gateway, "192.168.1.20", "192.168.1.20", 24).code == "bad_gateway"
    assert netops.v_dns("1.1.1.1, 8.8.8.8") == ["1.1.1.1", "8.8.8.8"] and netops.v_dns("") == []
    assert err(netops.v_dns, ["1.1.1.1; reboot"]).code == "bad_ip" and err(netops.v_dns, ["1.1.1.1"] * 4).code == "bad_dns"
    assert netops.v_vlan(None) is None and netops.v_vlan("") is None and netops.v_vlan(1) == 1 and netops.v_vlan("4094") == 4094
    for bad in (4095, -1, "abc", 20.5, True, [1]):
        assert err(netops.v_vlan, bad).code == "bad_vlan", bad


def test_channel_band_interface_label_rules(env):
    assert netops.v_channel("bg", 11) == ("bg", 11) and netops.v_channel("a", 149) == ("a", 149) and netops.v_channel("auto", 6) == ("bg", 6) and netops.v_channel(None, "auto") == ("bg", None)
    for band, ch in (("bg", 14), ("a", 52), ("a", 6), ("x", 1), ("bg", "abc")):
        assert err(netops.v_channel, band, ch).code in ("bad_channel", "bad_band")
    for bad in ("eth0; reboot", "ETH0", "a" * 16, "", "../x", "eth 0", None, "eth0\n"):
        assert err(netops.v_iface_name, bad).code == "bad_iface", bad
    assert netops.v_iface_name("eth0.20") and netops.v_label("test")
    assert err(netops.v_label, "wan").code == "bad_label"
    assert err(netops.normalize_changes, env.ctx, {"ports": {"eth9": {"method": "dhcp"}}}).code == "bad_iface"                 # does not exist
    assert err(netops.normalize_changes, env.ctx, {"ports": {"wlan0": {"method": "dhcp"}}}).code == "bad_iface"                # a radio is not an Ethernet port
    assert err(netops.normalize_changes, env.ctx, {"ports": {"eth0; reboot": {"method": "dhcp"}}}).code == "bad_iface"


def test_normalize_rejects_injection_unknown_keys_and_bad_values(env):
    c = env.ctx
    assert err(netops.normalize_changes, c, {"wifi": {"mode": "ap", "ssid": "x", "password": "short"}}).code == "bad_password"
    assert err(netops.normalize_changes, c, {"wifi": {"mode": "ap", "ssid": "x"}}).code == "bad_password"                      # AP needs a password the first time
    assert err(netops.normalize_changes, c, {"wifi": {"mode": "reboot"}}).code == "bad_mode"
    assert err(netops.normalize_changes, c, {"wifi": {"mode": "off", "evil": "x"}}).code == "bad_request"
    assert err(netops.normalize_changes, c, {"shell": "reboot"}).code == "bad_request"
    assert err(netops.normalize_changes, c, {"ports": {"eth0": {"method": "static", "address": "1.2.3.4; reboot", "mask": "255.255.255.0"}}}).code == "bad_ip"
    assert err(netops.normalize_changes, c, {"ports": {"eth0": {"method": "static"}}}).code == "bad_ip"
    assert err(netops.normalize_changes, c, {"ports": {"eth0": {"method": "static", "address": "192.168.1.5", "mask": "255.255.255.0", "gateway": "10.0.0.1"}}}).code == "bad_gateway"
    assert err(netops.normalize_changes, c, {"ports": {"eth0": {"method": "pppoe"}}}).code == "bad_method"
    assert err(netops.normalize_changes, c, {"ports": {"eth0": {"method": "dhcp", "vlan": 5000}}}).code == "bad_vlan"
    assert err(netops.normalize_changes, c, {"ports": {"eth0": {"method": "dhcp", "label": "wan"}}}).code == "bad_label"
    assert err(netops.normalize_changes, c, "reboot").code == "bad_request"
    assert err(netops.normalize_changes, c, {"fallback": "yes"}).code == "bad_request"


def test_static_subnets_may_not_overlap(env):
    c = env.ctx
    two = {"ports": {"eth0": {"method": "static", "address": "192.168.1.5/24"}, "eth1": {"method": "static", "address": "192.168.1.9/24"}}}
    assert err(netops.normalize_changes, c, two).code == "overlap"
    ok = {"ports": {"eth0": {"method": "static", "address": "192.168.1.5/24"}, "eth1": {"method": "static", "address": "192.168.2.9/24"}}}
    assert netops.normalize_changes(c, ok)["ports"]["eth1"]["address"] == "192.168.2.9/24"
    clash = {"wifi": {"mode": "ap", "ssid": "x", "password": "12345678"}, "ports": {"eth0": {"method": "static", "address": "10.42.0.50/24"}}}
    assert err(netops.normalize_changes, c, clash).code == "overlap"                                                         # the AP owns 10.42.0.0/24


# ------------------------------------------------------------------ default access point (fresh install)
def test_default_ap_is_what_the_installer_chooses(env):
    res = netops.setup_default_ap(env.ctx, ssid="pitest", activate=True)
    assert res["via"] == "nmcli"
    prof = next(p for p in env.nm.profiles() if p["name"] == "tt-ap")
    p = prof["props"]
    assert p["802-11-wireless.ssid"] == "pitest" and p["wifi-sec.psk"] == "password" and p["ipv4.method"] == "shared" and p["ipv4.addresses"] == "10.42.0.1/24"
    assert p["802-11-wireless.mode"] == "ap" and p["connection.autoconnect"] == "yes" and ("tt-ap", "wlan0") in env.view()["active"]
    pub = env.files()[netops.F_STATE]
    assert pub["wifi_mode"] == "ap" and pub["profiles"]["tt-ap"]["ssid"] == "pitest" and "psk" not in json.dumps(pub)        # the world-readable file never holds the password
    assert oct(os.stat(os.path.join(env.state, netops.F_PRIVATE)).st_mode & 0o777) == "0o600"


def test_default_ap_name_is_pi_name_setting_else_hostname(env, monkeypatch):
    import sqlite3
    monkeypatch.setattr(netops.socket, "gethostname", lambda: "raspberrypi")
    assert netops.default_ssid(env.ctx) == "raspberrypi"
    os.makedirs(env.data)
    con = sqlite3.connect(os.path.join(env.data, "traffic-tester.db"))
    con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO settings VALUES('pi_name', ?)", (json.dumps("Lab Pi 1"),))
    con.commit()
    con.close()
    assert netops.default_ssid(env.ctx) == "Lab Pi 1"
    monkeypatch.setattr(netops.socket, "gethostname", lambda: "h" * 60)
    os.unlink(os.path.join(env.data, "traffic-tester.db"))
    assert len(netops.default_ssid(env.ctx).encode()) <= 32


def test_default_ap_keyfile_when_networkmanager_is_not_running(env):
    d = env.nm.load()
    d["running"] = False
    env.nm.save(d)
    res = netops.setup_default_ap(env.ctx, ssid="pitest")
    assert res["via"] == "keyfile"
    path = os.path.join(env.ctx.keyfile_dir, "tt-ap.nmconnection")
    txt = open(path).read()
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    for needle in ("id=tt-ap", "type=wifi", "interface-name=wlan0", "autoconnect=true", "mode=ap", "ssid=pitest", "key-mgmt=wpa-psk", "psk=password", "address1=10.42.0.1/24", "method=shared"):
        assert needle in txt, needle
    assert env.files()[netops.F_STATE]["wifi_mode"] == "ap"
    assert err(netops.setup_default_ap, env.ctx, ssid="a;b", keyfile_only=True).code == "bad_ssid"                          # ';' cannot be written to a keyfile
    assert env.files()[netops.F_STATE]["profiles"]["tt-ap"]["ssid"] == "pitest"                                             # and the failed attempt saved nothing


# ------------------------------------------------------------------ apply -> snapshot -> revert, Keep, the 90 s timer
def apply(env, changes, **kw):
    return netops.op_apply(env.ctx, {"id": "t1", "changes": changes, "summary": ["x"], **kw})


def test_apply_arms_the_revert_timer_and_keep_cancels_it(env):
    before = env.view()
    res = apply(env, {"ports": {"eth0": {"method": "static", "address": "192.168.5.10/24", "gateway": "192.168.5.1", "label": "test"}}})
    assert res["ok"] and res["pending"]["state"] == "waiting" and res["pending"]["seconds"] == 90
    assert env.sched.scheduled == {"seconds": 90, "unit": "tt-net-revert", "args": ["net-revert"]}
    assert "tt-eth-eth0" in env.names() and env.view() != before
    pend = netops.get_pending(env.ctx)
    assert pend["deadline"] == env.t[0] + 90 and pend["summary"] == ["x"]
    k = netops.op_keep(env.ctx, {})
    assert k["code"] == "kept" and env.sched.scheduled is None and netops.get_pending(env.ctx) is None
    assert not os.path.exists(os.path.join(env.state, netops.F_SNAP))
    assert "tt-eth-eth0" in env.names()                                                                                    # kept for good
    assert netops.get_public(env.ctx)["last_event"]["type"] == "kept"


def test_revert_restores_the_previous_profiles_exactly(env):
    netops.setup_default_ap(env.ctx, ssid="pi1", activate=True)
    env.nm.update(active=[a for a in env.nm.load()["active"] if a["name"] != "HomeNet"])                                  # the AP took wlan0
    before, files = env.view(), env.files()
    changes = {"wifi": {"mode": "client", "ssid": "HomeNet", "password": "homepassword"}, "ports": {"eth0": {"method": "static", "address": "192.168.5.10/24", "label": "test", "vlan": 30}}}
    res = apply(env, changes)
    after = env.view()
    assert res["ok"] and after != before and ("tt-wifi-1", "wlan0") in after["active"] and ("tt-ap", "wlan0") not in after["active"]
    r = netops.op_revert(env.ctx, {}, "manual", True)
    assert r["code"] == "reverted" and not r["notes"]
    assert env.view() == before and env.files() == files
    assert netops.get_pending(env.ctx) is None and env.sched.scheduled is None
    assert netops.get_public(env.ctx)["last_event"]["type"] == "reverted"


def test_revert_removes_profiles_that_did_not_exist_before(env):
    before = env.view()
    apply(env, {"ports": {"eth1": {"method": "dhcp", "label": "internet"}}})
    assert "tt-eth-eth1" in env.names()
    netops.op_revert(env.ctx, {}, "manual", True)
    assert "tt-eth-eth1" not in env.names() and env.view() == before


def test_revert_fires_at_90_seconds_with_a_fake_scheduler(env):
    before = env.view()
    apply(env, {"wifi": {"mode": "off"}})
    assert env.sched.scheduled["seconds"] == 90 and env.view()["radio"] == "disabled"
    env.t[0] += 50
    early = netops.op_revert(env.ctx, {}, "timeout", False)                                   # what the timer's `helper.py net-revert` does
    assert early["code"] == "too_early" and env.view()["radio"] == "disabled"
    env.t[0] += 41                                                                              # 91 s after it was applied
    done = netops.op_revert(env.ctx, {}, "timeout", False)
    assert done["code"] == "reverted" and "automatically" in done["message"]
    assert env.view() == before
    assert netops.get_public(env.ctx)["last_event"]["reason"] == "timeout"


def test_expired_revert_is_fired_by_the_watch_loop_even_if_the_timer_was_lost(env):
    before = env.view()
    apply(env, {"wifi": {"mode": "off"}})
    env.t[0] += 60
    assert netops.check_expired_revert(env.ctx) is None
    env.t[0] += 60
    assert netops.check_expired_revert(env.ctx)["code"] == "reverted" and env.view() == before


def test_failed_apply_reverts_at_once_and_says_why(env):
    before, files = env.view(), env.files()
    env.nm.update(fail={"tt-wifi-1": "(7) Secrets were required, but not provided."})
    e = err(apply, env, {"wifi": {"mode": "client", "ssid": "HomeNet", "password": "wrongpassword"}})
    assert e.code == "activate_failed" and "password for 'HomeNet' was not accepted" in e.message and "previous settings were put back" in e.message
    assert env.view() == before and env.files() == files and netops.get_pending(env.ctx) is None and env.sched.scheduled is None


def test_ethernet_without_dhcp_is_a_warning_not_a_failure(env):
    env.nm.update(fail={"tt-eth-eth1": "IP configuration could not be reserved (no available address, timeout, etc.)"})
    res = apply(env, {"ports": {"eth1": {"method": "dhcp"}}})
    assert res["ok"] and "tt-eth-eth1" in env.names() and res["warnings"] and "keeps trying" in res["warnings"][0]


def test_unplugged_port_is_saved_but_not_activated(env):
    open(os.path.join(env.sysfs, "eth1", "carrier"), "w").write("0")
    apply(env, {"ports": {"eth1": {"method": "dhcp"}}}, auto_revert=False)
    assert "tt-eth-eth1" in env.names() and ("tt-eth-eth1", "eth1") not in env.view()["active"]


def test_no_timer_when_the_caller_says_the_change_is_harmless(env):
    res = apply(env, {"ports": {"eth1": {"method": "dhcp"}}}, auto_revert=False)
    assert res["ok"] and "pending" not in res and env.sched.scheduled is None and netops.get_pending(env.ctx) is None


def test_second_apply_is_refused_while_one_waits_for_keep(env):
    apply(env, {"wifi": {"mode": "off"}})
    assert err(apply, env, {"ports": {"eth1": {"method": "dhcp"}}}).code == "pending"


def test_scheduler_failure_changes_nothing(env, monkeypatch):
    def boom(*a, **k):
        raise NetError("timer_failed", "The safety timer could not be started, so nothing was changed.")
    monkeypatch.setattr(env.sched, "schedule", boom)
    before = env.view()
    assert err(apply, env, {"wifi": {"mode": "off"}}).code == "timer_failed"
    assert env.view() == before and netops.get_pending(env.ctx) is None


def test_wifi_modes_off_and_back_to_ap(env):
    apply(env, {"wifi": {"mode": "ap", "ssid": "pi1", "password": "password"}}, auto_revert=False)
    v = env.view()
    assert ("tt-ap", "wlan0") in v["active"] and v["radio"] == "enabled"
    apply(env, {"wifi": {"mode": "off"}}, auto_revert=False)
    ap = next(p for p in env.nm.profiles() if p["name"] == "tt-ap")
    assert env.view()["radio"] == "disabled" and ap["props"]["connection.autoconnect"] == "no"
    apply(env, {"wifi": {"mode": "ap"}}, auto_revert=False)                                                               # ssid + password are remembered
    assert env.view()["radio"] == "enabled" and ("tt-ap", "wlan0") in env.view()["active"]
    assert next(p for p in env.nm.profiles() if p["name"] == "tt-ap")["props"]["802-11-wireless.ssid"] == "pi1"


def test_labels_never_default_per_port_and_saved(env):
    apply(env, {"ports": {"eth0": {"method": "dhcp", "label": "test"}, "eth1": {"method": "dhcp", "label": "both"}}}, auto_revert=False)
    p = {x["name"]: x["props"] for x in env.nm.profiles()}
    assert p["tt-eth-eth0"]["ipv4.never-default"] == "yes" and p["tt-eth-eth1"]["ipv4.never-default"] == "no"
    assert netops.get_public(env.ctx)["labels"] == {"eth0": "test", "eth1": "both"}


def test_other_profiles_are_never_touched(env):
    before = {p["name"]: p for p in env.nm.profiles() if not p["name"].startswith("tt-")}
    apply(env, {"wifi": {"mode": "ap", "ssid": "pi1", "password": "password"}, "ports": {"eth0": {"method": "dhcp"}}}, auto_revert=False)
    after = {p["name"]: p for p in env.nm.profiles() if not p["name"].startswith("tt-")}
    assert before == after


def test_scan_dedupes_and_refuses_while_access_point(env):
    res = netops.op_scan(env.ctx, {})
    assert [n["ssid"] for n in res["networks"]] == ["HomeNet", "Cafe"] and res["networks"][0]["signal"] == 80 and res["networks"][1]["secure"] is False
    netops.setup_default_ap(env.ctx, ssid="pi1", activate=True)
    assert err(netops.op_scan, env.ctx, {}).code == "radio_busy"


def test_ensure_wifi_brings_the_access_point_back_after_a_capture(env):
    netops.setup_default_ap(env.ctx, ssid="pi1", activate=True)
    env.nm.update(active=[])                                                                                               # monitor mode dropped everything
    r = netops.op_ensure_wifi(env.ctx)
    assert r["code"] == "ap_up" and ("tt-ap", "wlan0") in env.view()["active"]
    assert netops.op_ensure_wifi(env.ctx)["message"] == "The access point is already up."


# ------------------------------------------------------------------ fallback decision (pure, fake clock)
def S(t, eth=False, wifi=False, ap_clients=0, ap=False, fb=False):
    links = [{"iface": "eth0", "kind": "eth", "carrier": eth, "ip": eth}, {"iface": "wlan0", "kind": "ap" if ap else "wifi", "carrier": wifi or ap, "ip": wifi}]
    return {"t": t, "links": links, "ap_clients": ap_clients, "ap_active": ap, "fallback_active": fb}


def run_decisions(seq, **kw):
    samples, state, out = [], {}, []
    for s in seq:
        samples.append(s)
        d = netops.fallback_decision(samples, s["t"], state, **kw)
        state = d["state"]
        out.append((s["t"], d["action"]))
    return out


def test_no_network_for_three_minutes_brings_the_ap_up():
    acts = run_decisions([S(t) for t in range(0, 241, 10)])
    first = next(t for t, a in acts if a == "ap_up")
    assert first == 180 and [a for _, a in acts].count("ap_up") == 1


def test_network_back_means_no_ap():
    seq = [S(t) for t in range(0, 150, 10)] + [S(t, eth=True) for t in range(150, 400, 10)]
    assert all(a == "none" for _, a in run_decisions(seq))
    seq = [S(t, eth=True) for t in range(0, 400, 10)]
    assert all(a == "none" for _, a in run_decisions(seq))


def test_a_short_outage_does_not_trigger():
    seq = [S(t) for t in range(0, 170, 10)] + [S(t, wifi=True) for t in range(170, 500, 10)]
    assert all(a == "none" for _, a in run_decisions(seq))


def test_ap_clients_count_as_network():
    seq = [S(t, ap=True, ap_clients=1) for t in range(0, 600, 10)]
    assert all(a == "none" for _, a in run_decisions(seq))
    assert all(a == "none" for _, a in run_decisions([S(t, ap=True) for t in range(0, 600, 10)]))                         # an AP is already up: nothing to start
    assert netops.sample_has_network(S(0, ap_clients=2)) and not netops.sample_has_network(S(0))


def test_link_local_or_unaddressed_ports_are_not_a_network():
    s = {"t": 0, "links": [{"iface": "eth0", "kind": "eth", "carrier": True, "ip": False}], "ap_clients": 0}
    assert not netops.sample_has_network(s)
    assert netops.sample_has_network({"t": 0, "links": [{"iface": "eth0", "kind": "eth", "carrier": True, "ip": True}]})


def test_cycle_retry_the_normal_profiles_every_five_minutes():
    fb = lambda t, **k: S(t, ap=True, fb=True, **k)                                  # noqa: E731
    seq = [S(t) for t in range(0, 181, 10)]                                          # 180 s: AP comes up
    seq += [fb(t) for t in range(190, 481, 10)]                                      # AP up, nobody connected
    acts = run_decisions(seq)
    assert dict(acts)[180] == "ap_up"
    retry_at = next(t for t, a in acts if a == "retry")
    assert retry_at == 480                                                           # 300 s after it came up
    # during the 30 s window the AP is down; the old Wi-Fi does not come back -> AP again
    window = [S(t) for t in range(490, 521, 10)]
    out = run_decisions(seq + window)
    assert [a for t, a in out if t >= 490] == ["none", "none", "ap_up", "none"] and out[-2][0] == 510
    # ... or it does come back -> retry_ok, normal phase, no AP
    out = run_decisions(seq + [S(490), S(500, wifi=True)])
    assert [a for t, a in out if t >= 490] == ["none", "retry_ok"]


def test_no_retry_while_a_phone_is_on_the_emergency_ap():
    seq = [S(t) for t in range(0, 181, 10)] + [S(t, ap=True, fb=True, ap_clients=1) for t in range(190, 900, 10)]
    assert "retry" not in [a for _, a in run_decisions(seq)]


def test_emergency_ap_comes_down_when_the_network_returns():
    seq = [S(t) for t in range(0, 181, 10)] + [S(190, ap=True, fb=True), S(200, eth=True, ap=True, fb=True)]
    assert run_decisions(seq)[-1] == (200, "ap_down")


def test_disabled_fallback_never_acts_and_switches_off_a_running_one():
    assert all(a == "none" for _, a in run_decisions([S(t) for t in range(0, 600, 10)], enabled=False))
    d = netops.fallback_decision([S(0, ap=True, fb=True)], 0, {"phase": "fallback", "last_retry": 0}, enabled=False)
    assert d["action"] == "ap_down"


def test_watcher_adopts_a_running_emergency_ap_after_restart():
    d = netops.fallback_decision([S(0, ap=True, fb=True)], 0, None)
    assert d["action"] == "none" and d["state"]["phase"] == "fallback"


def test_watch_tick_acts_through_the_fake_network(env):
    os.makedirs(env.state, exist_ok=True)
    samples, state = [], {}
    probe = netops.FakeProbe(env.ctx)
    netops.write_json(os.path.join(env.state, "fake-probe.json"), {"links": [{"iface": "eth0", "kind": "eth", "carrier": False, "ip": False}]})
    env.nm.update(active=[])
    acts = []
    for t in range(0, 200, 10):
        d = netops.watch_tick(env.ctx, samples, state, probe, now=float(t))
        if d["action"] != "none":
            acts.append((t, d["action"], d.get("result", {}).get("code")))
    assert acts == [(180, "ap_up", "fallback_up")]
    assert ("tt-fallback-ap", "wlan0") in env.view()["active"] and "tt-fallback-ap" in env.names()
    fb = next(p for p in env.nm.profiles() if p["name"] == "tt-fallback-ap")["props"]
    assert fb["connection.autoconnect"] == "no" and fb["wifi-sec.psk"] == "password"
    # a saved profile is never replaced by it: only tt-fallback-ap was added
    assert env.names() == sorted(["HomeNet", "Wired connection 1", "tt-fallback-ap"])
    netops.write_json(os.path.join(env.state, "fake-probe.json"), {"links": [{"iface": "eth0", "kind": "eth", "carrier": True, "ip": True}]})
    d = netops.watch_tick(env.ctx, samples, state, probe, now=200.0)
    assert d["action"] == "ap_down" and ("tt-fallback-ap", "wlan0") not in env.view()["active"]
    assert env.files()[netops.F_STATE]["fallback"]["active"] is False


def test_watch_tick_leaves_a_capture_alone(env, monkeypatch, tmp_path):
    d = tmp_path / "wifi"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({"state": "monitor"}))
    monkeypatch.setenv("TT_WIFI_HELPER_DIR", str(d))
    samples = [S(0), S(100)]
    dec = netops.watch_tick(env.ctx, samples, {}, netops.FakeProbe(env.ctx), now=300.0)
    assert dec["action"] == "none" and samples == []


def test_fallback_can_be_switched_off(env):
    netops.op_set_fallback(env.ctx, {"enabled": False})
    assert netops.op_fallback_ap(env.ctx)["code"] == "disabled" and "tt-fallback-ap" not in env.names()


# ------------------------------------------------------------------ plan (dry run, plain words)
CFG = {"wifi": {"mode": "client", "joined": "HomeNet", "ssid": "HomeNet", "ap": {}}, "ports": [{"iface": "eth0", "saved": True, "method": "dhcp", "label": "both", "link": True, "dns": [], "address": None, "gateway": None, "vlan": None},
                                                                                                     {"iface": "eth1", "saved": False, "link": True}],
       "connected_through": {"iface": "eth0", "kind": "ethernet", "label": "both"}, "ap_url": "http://10.42.0.1:8080"}


def norm(env, changes):
    return netops.normalize_changes(env.ctx, changes)


def test_plan_marks_changes_to_the_port_you_use_as_affecting(env):
    ch = {"ports": {"eth0": {"method": "static", "address": "192.168.9.2/24"}}}
    p = planmod.build_plan(norm(env, ch), CFG, ch)
    assert p["affects_current_connection"] and p["needs_confirm"] and p["will_auto_revert"] and "eth0" in p["reasons"][0]
    assert "fixed address 192.168.9.2/24" in p["steps"][0]
    other = {"ports": {"eth1": {"method": "dhcp", "label": "test"}}}
    p = planmod.build_plan(norm(env, other), CFG, other)
    assert not p["affects_current_connection"] and not p["will_auto_revert"] and not p["needs_confirm"]


def test_plan_unchanged_port_is_not_a_change(env):
    ch = {"ports": {"eth0": {"method": "dhcp", "label": "both"}}}
    p = planmod.build_plan(norm(env, ch), CFG, ch)
    assert p["nothing_to_do"] and not p["affects_current_connection"]


def test_plan_wifi_change_while_connected_over_wifi(env):
    cfg = dict(CFG, connected_through={"iface": "wlan0", "kind": "wifi", "label": None}, wifi={"mode": "ap", "ssid": "pi1", "joined": None, "ap": {"ssid": "pi1", "band": "bg", "channel": 6}})
    ch = {"wifi": {"mode": "ap", "ssid": "pi1", "password": "newpassword"}}
    p = planmod.build_plan(norm(env, ch), cfg, ch)
    assert p["affects_current_connection"] and "Wi-Fi" in p["reasons"][0]
    ch = {"wifi": {"mode": "client", "ssid": "HomeNet", "password": "homepassword"}}
    p = planmod.build_plan(norm(env, ch), cfg, ch)
    assert p["affects_current_connection"] and "joins 'HomeNet'" in p["steps"][0]
    ch = {"wifi": {"mode": "off"}}
    p = planmod.build_plan(norm(env, ch), CFG, ch)                                          # on Ethernet: Wi-Fi off does not touch you
    assert not p["affects_current_connection"] and "switched off" in p["steps"][0]


def test_plan_warnings_default_password_unknown_client_no_internet_port(env):
    ch = {"wifi": {"mode": "ap", "ssid": "pi1", "password": "password"}}
    p = planmod.build_plan(norm(env, ch), dict(CFG, connected_through=None), ch)
    assert any("factory default" in w for w in p["warnings"]) and any("cannot tell which port" in w for w in p["warnings"]) and p["will_auto_revert"] and not p["affects_current_connection"]
    ch = {"ports": {"eth0": {"method": "dhcp", "label": "test"}}}
    cfg = dict(CFG, wifi={"mode": "ap", "ap": {}}, ports=[dict(CFG["ports"][0])])
    p = planmod.build_plan(norm(env, ch), cfg, ch)
    assert any("no port is labelled Internet" in w for w in p["warnings"])


# ------------------------------------------------------------------ path_warning
def test_path_warning_wifi_ap_wifi_client_internet_only_and_fine(env):
    st = {"wifi_mode": "ap", "wifi_iface": "wlan0", "labels": {"eth0": "test", "eth1": "internet"}, "fallback": {}}
    w = paths.path_warning("10.42.0.55", route_dev=lambda ip: "wlan0", state=st)
    assert "access point" in w and "10.42.0.55" in w
    w = paths.path_warning("192.168.1.9", route_dev=lambda ip: "wlan0", state=dict(st, wifi_mode="client"))
    assert "management and internet link" in w
    w = paths.path_warning("192.168.9.9", route_dev=lambda ip: "eth1", state=st)
    assert "labelled Internet only" in w
    assert paths.path_warning("192.168.9.9", route_dev=lambda ip: "eth0", state=st) is None
    assert paths.path_warning("192.168.9.9", route_dev=lambda ip: "eth0.20", state=st) is None
    assert paths.path_warning("192.168.9.9", route_dev=lambda ip: "eth1.20", state=st)                 # a VLAN on an Internet-only port
    assert paths.path_warning("127.0.0.1", route_dev=lambda ip: "lo", state=st) is None
    assert paths.path_warning("/dev/ttyUSB0", route_dev=lambda ip: "wlan0", state=st) is None
    assert paths.path_warning(None, state=st) is None and paths.path_warning("", state=st) is None
    assert paths.path_warning("1.2.3.4", route_dev=lambda ip: None, state=st) is None
    assert paths.path_warning("192.168.9.9", route_dev=lambda ip: "eth5", state={"labels": {}}) is None


def test_path_warning_resolves_names_with_a_timeout():
    assert paths.path_warning("relay.example", route_dev=lambda ip: "wlan0", state={"wifi_mode": "client"}, resolve=lambda h: "192.168.1.50")
    assert paths.path_warning("relay.example", route_dev=lambda ip: "wlan0", state={}, resolve=lambda h: None) is None


# ------------------------------------------------------------------ the app-side watchdog thread
def test_watchdog_tick_acts_once_and_defers_to_the_root_service(env, monkeypatch):
    monkeypatch.setenv("TT_NET_FAKE", "1")
    monkeypatch.setenv("TT_NET_STATE_DIR", env.state)
    monkeypatch.setenv("TT_NET_SYSFS", env.sysfs)
    monkeypatch.setenv("TT_FAKE_NM_STATE", env.nm_path)
    acts, samples, state = [], [], {}
    probe = type("P", (), {"sample": lambda self: S(0)})()
    for t in range(0, 200, 10):
        watchdog.tick(samples, state, probe, lambda: {"fallback": {"enabled": True}}, acts.append, now=float(t), service_alive=lambda: False)
    assert acts == ["ap_up"]
    acts2, samples, state = [], [], {}
    for t in range(0, 200, 10):
        watchdog.tick(samples, state, probe, lambda: {"fallback": {"enabled": True}}, acts2.append, now=float(t), service_alive=lambda: True)
    assert acts2 == []                                                                          # netwatch is running: the app only watches
    assert watchdog.ACTION_OP["ap_up"] == "fallback_ap"


# ------------------------------------------------------------------ HTTP API
@pytest.fixture
def web(env, monkeypatch):
    for k, v in (("TT_NET_FAKE", "1"), ("TT_NET_STATE_DIR", env.state), ("TT_NET_DATA_DIR", env.data), ("TT_NET_SYSFS", env.sysfs), ("TT_FAKE_NM_STATE", env.nm_path)):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(statemod, "_live_ports", lambda: [{"name": "eth0", "mac": "aa", "wired": True, "usb": False, "driver": "bcmgenet", "carrier": True, "speed_mbps": 1000, "ip": "192.168.1.50", "is_bridge": False},
                                                         {"name": "eth1", "mac": "bb", "wired": True, "usb": True, "driver": "r8152", "carrier": False, "speed_mbps": None, "ip": None, "is_bridge": False}])
    routes = {"ip": "eth0"}
    monkeypatch.setattr(statemod, "_route_dev", lambda ip: routes["ip"] if ip else None)
    store = {}

    class Store:
        def get_setting(self, k, d=None):
            return store.get(k, d)

        def set_setting(self, k, v):
            store[k] = v
    app = FastAPI()
    netapi.register(app, type("St", (), {"store": Store()})())
    c = TestClient(app)
    c.store, c.routes, c.env = store, routes, env
    return c


def test_api_config_shows_ports_link_speed_ip_and_where_you_connect(web):
    j = web.get("/api/net/config").json()
    assert j["available"] and j["connected_through"]["iface"] == "eth0" and j["connected_through"]["text"] == "eth0 (Ethernet, Internet + Test)"
    e0 = next(p for p in j["ports"] if p["iface"] == "eth0")
    assert e0["link"] and e0["speed_mbps"] == 1000 and e0["ip"] == "192.168.1.50" and e0["you"] and e0["label"] == "both"
    e1 = next(p for p in j["ports"] if p["iface"] == "eth1")
    assert e1["usb"] and not e1["link"] and not e1["you"]
    assert j["wifi"]["mode"] == "client" and j["wifi"]["joined"] == "HomeNet" and j["wifi"]["radio"] == "on"
    assert j["pi_name"]


def test_api_pi_name_setting_is_read(web):
    web.store["pi_name"] = "Lab Pi 3"
    assert web.get("/api/net/config").json()["pi_name"] == "Lab Pi 3"


def test_api_plan_then_409_without_confirm_then_apply_keep(web):
    ch = {"ports": {"eth0": {"method": "static", "address": "192.168.5.10/24", "gateway": "192.168.5.1", "label": "test"}}}
    p = web.post("/api/net/plan", json={"changes": ch}).json()
    assert p["affects_current_connection"] and p["revert_seconds"] == 90 and p["steps"]
    r = web.post("/api/net/apply", json={"changes": ch})
    assert r.status_code == 409 and "affects the connection you are using" in r.json()["detail"]
    r = web.post("/api/net/apply", json={"changes": ch, "confirm": True})
    assert r.status_code == 200 and r.json()["state"] == "applied" and r.json()["pending"]["seconds"] == 90
    pend = web.get("/api/net/pending").json()
    assert pend["pending"]["state"] == "waiting" and 0 < pend["pending"]["seconds_left"] <= 90 and pend["pending"]["summary"]
    assert web.store["net_labels"] == {"eth0": "test"} and web.store["net_wifi_mode"] is None
    assert web.post("/api/net/keep").json()["ok"] and web.get("/api/net/pending").json()["pending"] is None
    assert web.get("/api/net/pending").json()["last_event"]["type"] == "kept"


def test_api_apply_unaffected_change_needs_no_confirm_and_no_timer(web):
    r = web.post("/api/net/apply", json={"changes": {"ports": {"eth1": {"method": "dhcp", "label": "internet"}}}})
    assert r.status_code == 200 and r.json()["state"] == "applied" and "pending" not in r.json() or r.json().get("pending") is None
    assert web.get("/api/net/pending").json()["pending"] is None


def test_api_revert_now_restores(web):
    before = web.env.view()
    ch = {"ports": {"eth0": {"method": "dhcp", "label": "test"}}}
    assert web.post("/api/net/apply", json={"changes": ch, "confirm": True}).status_code == 200
    assert web.env.view() != before
    r = web.post("/api/net/revert")
    assert r.status_code == 200 and web.env.view() == before and web.get("/api/net/pending").json()["pending"] is None
    assert web.get("/api/net/pending").json()["last_event"]["type"] == "reverted"


def test_api_plain_errors(web):
    r = web.post("/api/net/apply", json={"changes": {"wifi": {"mode": "ap", "ssid": "x" * 40, "password": "12345678"}}})
    assert r.status_code == 422 and "32 bytes" in r.json()["detail"]
    r = web.post("/api/net/plan", json={"changes": {"ports": {"eth0": {"method": "static", "address": "1.2.3.4; reboot", "mask": "255.255.255.0"}}}})
    assert r.status_code == 422 and "not a valid IPv4" in r.json()["detail"]
    r = web.post("/api/net/apply", json={"changes": {"ports": {"eth0": {"method": "dhcp", "vlan": 9999}}}, "confirm": True})
    assert r.status_code == 422 and "1 to 4094" in r.json()["detail"]
    assert web.post("/api/net/apply", json={"changes": {}}).json()["message"] == "Nothing needed to change."
    r = web.post("/api/net/fallback", json={"enabled": "yes"})
    assert r.status_code == 422


def test_api_apply_failure_is_422_with_the_reason_and_state_restored(web):
    before = web.env.view()
    web.env.nm.update(fail={"tt-wifi-1": "(7) Secrets were required, but not provided."})
    r = web.post("/api/net/apply", json={"changes": {"wifi": {"mode": "client", "ssid": "HomeNet", "password": "wrongpassword"}}, "confirm": True})
    assert r.status_code == 422 and "password" in r.json()["detail"] and web.env.view() == before


def test_api_scan_and_fallback_toggle(web):
    j = web.post("/api/net/wifi/scan").json()
    assert [n["ssid"] for n in j["networks"]] == ["HomeNet", "Cafe"]
    assert web.post("/api/net/fallback", json={"enabled": False}).json()["enabled"] is False and web.store["net_fallback"] is False
    assert web.get("/api/net/config").json()["fallback"]["enabled"] is False


def test_api_when_the_service_is_not_installed(web, monkeypatch):
    monkeypatch.delenv("TT_NET_FAKE")
    monkeypatch.setenv("TT_UPDATE_MODE", "off")
    j = web.get("/api/net/config").json()
    assert j["available"] is False and "install" in j["reason"]
    assert web.post("/api/net/apply", json={"changes": {"wifi": {"mode": "off"}}, "confirm": True}).status_code == 503
