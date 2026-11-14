"""Wi-Fi power saving: pre-flight warning data, one-click off, kept off, and the result says the path was Wi-Fi."""
import subprocess, tempfile, types

import pytest
from fastapi.testclient import TestClient

from traffic_tester import system
from traffic_tester.server import create_app


class FakeIw:
    def __init__(self, on=True, nm=True):
        self.on, self.nm, self.calls = on, nm, []

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        out, rc = "", 0
        if cmd[:2] == ["iw", "dev"] and cmd[3:5] == ["get", "power_save"]:
            out = f"Power save: {'on' if self.on else 'off'}\n"
        elif cmd[:2] == ["iw", "dev"] and cmd[3:] == ["set", "power_save", "off"]:
            self.on = False
        elif cmd[:2] == ["ip", "route"]:
            out = "192.0.2.155 dev wlan0 src 192.0.2.215 uid 1000\n" if cmd[3].startswith("192.") else "8.8.8.8 dev eth0 src 1.2.3.4\n"
        elif cmd[0] == "nmcli" and "show" in cmd:
            out = "Home Wi-Fi:wlan0\nWired:eth0\n"
        elif cmd[0] == "nmcli" and "modify" in cmd:
            rc = 0 if self.nm else 1
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="" if rc == 0 else "denied")


@pytest.fixture
def iw(monkeypatch):
    f = FakeIw()
    monkeypatch.setattr(subprocess, "run", f.run)
    monkeypatch.setattr(system.shutil, "which", lambda n: "/usr/sbin/" + n)
    monkeypatch.setattr(system, "is_wifi", lambda i: i == "wlan0")
    return f


def test_path_detection_and_power_save_state(iw):
    assert system.wifi_path("192.0.2.155") == {"interface": "wlan0", "power_save": True}
    assert system.wifi_path("8.8.8.8") is None and system.wifi_path("127.0.0.1") is None and system.wifi_path("") is None


def test_turn_off_now_and_for_good(iw):
    r = system.wifi_powersave_off("wlan0")
    assert r["ok"] and r["persistent"] and system.wifi_power_save("wlan0") is False
    assert ["nmcli", "connection", "modify", "Home Wi-Fi", "802-11-wireless.powersave", "2"] in iw.calls


def test_turn_off_without_networkmanager_permission_still_works_until_reboot_and_says_so(iw):
    iw.nm = False
    r = system.wifi_powersave_off("wlan0")
    assert r["ok"] and not r["persistent"] and "every time it starts" in r["message"] and "nmcli connection modify" in r["message"]


def test_refuses_a_non_wifi_or_odd_interface(iw):
    assert not system.wifi_powersave_off("eth0")["ok"]
    assert not system.wifi_powersave_off("wlan0; rm -rf /")["ok"]


def test_endpoints_and_keep_off_flag(iw):
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        d = c.get("/api/wifi/powersave", params={"host": "192.0.2.155"}).json()
        assert d["wifi"] and d["interface"] == "wlan0" and d["power_save"] is True and d["keep_off"] is False
        assert c.get("/api/wifi/powersave", params={"host": "8.8.8.8"}).json()["wifi"] is False
        assert c.post("/api/wifi/powersave/off", json={"interface": "wlan0"}).json()["ok"]
        d = c.get("/api/wifi/powersave", params={"host": "192.0.2.155"}).json()
        assert d["power_save"] is False and d["keep_off"] is True


def test_run_result_says_the_path_was_wifi(iw, monkeypatch):
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        c.post("/api/responder/start", json={"transport": "udp", "net": {"port": 20991}, "frame": "extended"})
        p = {"name": "wifi path", "transport": "udp", "frame": "extended", "net": {"peer_host": "127.0.0.1", "port": 20991}, "duration_s": 2, "interval_ms": 50}
        # the traffic goes to loopback, but the app's view of the route says Wi-Fi: only the summary wiring is under test
        monkeypatch.setattr(system, "route_dev", lambda h: "wlan0")
        rid = c.post("/api/runs", json={"profile": p}).json()["run_id"]
        import time
        for _ in range(60):
            if not c.get("/api/status").json()["run"]["running"]:
                break
            time.sleep(0.2)
        s = c.get(f"/api/runs/{rid}").json()["summary"]
        assert s["path"] == {"kind": "wifi", "interface": "wlan0", "power_save": True}
