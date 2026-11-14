"""Nexmon (built-in Wi-Fi monitor mode) and the access point: pre-flight warns first, restore() brings the AP profile back when the Pi was in AP mode before."""
import importlib.util
import json
import os
import subprocess

import pytest

from traffic_tester.netcfg import client
from traffic_tester.sniff import builtin_wifi, preflight

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_builtin_checks(monkeypatch, ap):
    monkeypatch.setattr(builtin_wifi, "status", lambda: {"available": True, "kernel": "6.12", "state": "stock", "reason": ""})
    monkeypatch.setattr(builtin_wifi, "was_access_point", lambda: ap)
    monkeypatch.setattr(preflight, "wifi_default_route_dev", lambda: "eth0")
    checks = []
    preflight._builtin_checks({}, {"wifi": {"builtin_ack": True}}, lambda s, t, x: checks.append({"s": s, "title": t, "text": x}), "192.168.1.5")
    return checks


def test_preflight_warns_first_that_the_access_point_goes_off(monkeypatch):
    checks = run_builtin_checks(monkeypatch, True)
    assert checks[0]["s"] == "warn" and "own Wi-Fi network will go off" in checks[0]["title"]
    assert "comes back by itself" in checks[0]["text"] and "access point" in checks[0]["text"]
    assert all(c["s"] != "bad" for c in checks)


def test_preflight_has_no_access_point_warning_otherwise(monkeypatch):
    assert not [c for c in run_builtin_checks(monkeypatch, False) if "own Wi-Fi network" in c["title"]]


def test_was_access_point_reads_the_saved_mode_and_the_emergency_ap(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_NET_STATE_DIR", str(tmp_path))
    assert builtin_wifi.was_access_point() is False                                   # nothing saved: an install from before the feature
    (tmp_path / "net-state.json").write_text(json.dumps({"wifi_mode": "ap"}))
    assert builtin_wifi.was_access_point() is True
    (tmp_path / "net-state.json").write_text(json.dumps({"wifi_mode": "client", "fallback": {"active": True}}))
    assert builtin_wifi.was_access_point() is True
    (tmp_path / "net-state.json").write_text(json.dumps({"wifi_mode": "client"}))
    assert builtin_wifi.was_access_point() is False


def test_status_carries_the_access_point_warning(monkeypatch):
    monkeypatch.setattr(builtin_wifi, "_call", lambda cmd, timeout=150: {"status": {"nexmon_module": True, "nexmon_firmware": True, "interfaces": ["wlan0"], "kernel": "6.12"}})
    monkeypatch.setattr(builtin_wifi, "was_access_point", lambda: True)
    assert "access point" in builtin_wifi.status()["ap_warning"]
    monkeypatch.setattr(builtin_wifi, "was_access_point", lambda: False)
    assert builtin_wifi.status()["ap_warning"] == ""


def test_restore_asks_the_network_helper_to_bring_the_access_point_back(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_WIFI_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(builtin_wifi, "_call", lambda cmd, timeout=150: {"ok": True, "code": "restored"})
    asked = []
    monkeypatch.setattr(client, "call", lambda op, params=None, wait=60, action="net": asked.append(op) or {"ok": True, "message": "The access point is back."})
    monkeypatch.setattr(builtin_wifi, "was_access_point", lambda: True)
    res = builtin_wifi.restore()
    assert asked == ["ensure_wifi"] and res["ap_restored"] is True and res["ap_message"] == "The access point is back."
    asked.clear()
    monkeypatch.setattr(builtin_wifi, "was_access_point", lambda: False)
    res = builtin_wifi.restore()
    assert asked == [] and "ap_restored" not in res                                  # a Pi that was not an access point is left alone


def test_restore_never_fails_because_the_access_point_could_not_be_asked(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_WIFI_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(builtin_wifi, "_call", lambda cmd, timeout=150: {"ok": True, "code": "restored"})
    monkeypatch.setattr(builtin_wifi, "was_access_point", lambda: True)

    def boom(*a, **k):
        raise client.NetClientError("not installed", 503)
    monkeypatch.setattr(client, "call", boom)
    res = builtin_wifi.restore()
    assert res["ok"] and res["ap_restored"] is False and "not installed" in res["ap_message"]


@pytest.fixture
def helper(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_WIFI_ROOT", str(tmp_path))
    monkeypatch.delenv("TT_NET_STATE_DIR", raising=False)
    spec = importlib.util.spec_from_file_location("wifi_helper_ap_test", os.path.join(ROOT, "traffic_tester", "sniff", "wifi_helper.py"))
    H = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(H)
    calls = []

    def fake_run(cmd, timeout=30, check=False):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(H, "run", fake_run)
    monkeypatch.setattr(H, "have", lambda t: True)
    monkeypatch.setattr(H, "_wait_iface", lambda name, timeout: True)
    monkeypatch.setattr(H, "_select_stock_firmware", lambda: None)
    H.calls = calls
    H.state_file = str(tmp_path / "var/lib/traffic-tester-update/net-state.json")
    os.makedirs(os.path.dirname(H.state_file))
    return H


def test_root_helper_restore_brings_tt_ap_back_when_the_pi_was_an_access_point(helper):
    open(helper.state_file, "w").write(json.dumps({"wifi_mode": "ap"}))
    res = helper.restore("test")
    assert res["ok"] and any("tt-ap" in n and "brought back" in n for n in res["notes"])
    up = [c for c in helper.calls if c[:2] == ["nmcli", "--wait"] and "up" in c]
    assert up and up[0][-4:] == ["id", "tt-ap", "ifname", "wlan0"]
    assert helper.calls.index(["nmcli", "device", "connect", "wlan0"]) < helper.calls.index(up[0])      # normal reconnect first, then the explicit profile


def test_root_helper_restore_uses_the_emergency_profile_and_leaves_clients_alone(helper):
    open(helper.state_file, "w").write(json.dumps({"wifi_mode": "client", "fallback": {"active": True}}))
    helper.restore("test")
    assert [c for c in helper.calls if "tt-fallback-ap" in c]
    helper.calls.clear()
    open(helper.state_file, "w").write(json.dumps({"wifi_mode": "client"}))
    helper.restore("test")
    assert not [c for c in helper.calls if c[:2] == ["nmcli", "--wait"]]
    helper.calls.clear()
    os.unlink(helper.state_file)                                                      # an install from before the feature: no saved state, nothing extra
    helper.restore("test")
    assert not [c for c in helper.calls if c[:2] == ["nmcli", "--wait"]]
