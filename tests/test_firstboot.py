"""First boot of the card image: the unnamed state, the name-this-Pi API, the root helper op (hostname + Wi-Fi name), the Imager Wi-Fi hand-over,
discovery of the next free name and the hotspot/captive decision logic. No NetworkManager is touched (fake nmcli, fake scheduler)."""
import json
import os
import socket
import threading
import urllib.request

import pytest
from fastapi.testclient import TestClient

from netcfgfix import Env
from traffic_tester import firstboot as FB
from traffic_tester.server import create_app
from traffic_tester.update import netops
from traffic_tester.update.netops import NetError


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def mkapp(tmp_path, monkeypatch, marker=True, peers=()):
    d = str(tmp_path / "appdata")
    os.makedirs(d, exist_ok=True)
    if marker:
        open(os.path.join(d, FB.MARKER), "w").close()
    e = Env(tmp_path / "net")
    monkeypatch.setenv("TT_NET_FAKE", "1")
    monkeypatch.setenv("TT_NET_STATE_DIR", e.state)
    monkeypatch.setenv("TT_NET_DATA_DIR", d)
    monkeypatch.setenv("TT_NET_SYSFS", e.sysfs)
    monkeypatch.setenv("TT_NET_ETC", os.path.join(e.tmp, "etc"))
    monkeypatch.setenv("TT_FAKE_NM_STATE", e.nm_path)
    monkeypatch.setenv("TT_NM_KEYFILE_DIR", os.path.join(e.tmp, "nmkeys"))
    monkeypatch.setenv("TT_NO_FIRSTBOOT_REDIRECT", "1")
    app = create_app(d)
    return app, e, d


def seed_setup_ap(e, ssid="pi-setup"):
    """What the card image has before naming: the tt-ap profile with the setup name and password 'password'."""
    d = e.nm.load()
    d["profiles"] = [p for p in d["profiles"] if p["type"] != "wifi"]
    d["active"] = [a for a in d["active"] if a["type"] != "wifi"]
    d["profiles"].append({"name": "tt-ap", "uuid": "22222222-0000-0000-0000-000000000009", "type": "wifi",
                          "props": {"connection.interface-name": "wlan0", "connection.autoconnect": "yes", "802-11-wireless.ssid": ssid, "802-11-wireless.mode": "ap"}})
    e.nm.save(d)
    netops.setup_default_ap(e.ctx, ssid=ssid, keyfile_only=False)


# ------------------------------------------------------------------ the unnamed state
def test_unnamed_state_is_marker_without_a_name(tmp_path, monkeypatch):
    app, e, d = mkapp(tmp_path, monkeypatch)
    c = TestClient(app)
    assert c.get("/api/status").json()["first_boot"] is True
    app.state.tt.store.set_setting("pi_name", "x1")
    assert c.get("/api/status").json()["first_boot"] is False        # a name was chosen some other way: the screen never comes back


def test_no_marker_means_a_normal_install_never_sees_the_screen(tmp_path, monkeypatch):
    app, e, d = mkapp(tmp_path, monkeypatch, marker=False)
    c = TestClient(app)
    assert c.get("/api/status").json()["first_boot"] is False
    assert c.get("/api/setup/info").json()["first_boot"] is False
    r = c.post("/api/setup/name", json={"name": "pi1"})
    assert r.status_code == 409 and "already" in r.json()["detail"]


def test_status_flag_is_open_even_when_a_password_is_set(tmp_path, monkeypatch):
    app, e, d = mkapp(tmp_path, monkeypatch)
    app.state.tt.store.set_setting("password_hash", app.state.tt.hash_pw("secret"))
    assert TestClient(app).get("/api/status").json()["first_boot"] is True


# ------------------------------------------------------------------ naming
def test_naming_sets_hostname_renames_the_ap_and_clears_the_marker(tmp_path, monkeypatch):
    app, e, d = mkapp(tmp_path, monkeypatch)
    seed_setup_ap(e)
    c = TestClient(app)
    info = c.get("/api/setup/info").json()
    assert info["suggested"] == "pi1" and info["first_boot"] and info["password"] == "password"
    r = c.post("/api/setup/name", json={"name": "Pi1"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["name"] == "pi1" and j["ssid"] == "pi1" and j["password"] == "password" and j["ap_renamed"] is True
    assert isinstance(j["ips"], list)
    assert j["urls"]["ap"] == "http://10.42.0.1:8080" and j["urls"]["local"] == "http://pi1.local:8080"
    assert open(os.path.join(e.tmp, "etc", "hostname")).read().strip() == "pi1"
    assert "127.0.1.1\tpi1" in open(os.path.join(e.tmp, "etc", "hosts")).read()
    assert not os.path.exists(os.path.join(d, FB.MARKER))
    assert c.get("/api/status").json()["first_boot"] is False and c.get("/api/status").json()["pi"]["name"] == "pi1"
    assert netops.get_private(e.ctx)["ap"]["ssid"] == "pi1" and netops.get_private(e.ctx)["ap"]["psk"] == "password"
    prof = next(p for p in e.nm.load()["profiles"] if p["name"] == "tt-ap")
    assert prof["props"]["802-11-wireless.ssid"] == "pi1"
    calls = [" ".join(c) for c in e.nm.load()["calls"]]
    assert any(c.startswith("systemd-run") and "tt-ap-restart" in c and c.endswith("nmcli connection up id tt-ap") for c in calls) and any(c == "hostnamectl set-hostname pi1" for c in calls)        # the AP restarts later, not under the answer


def test_naming_twice_is_refused(tmp_path, monkeypatch):
    app, e, d = mkapp(tmp_path, monkeypatch)
    seed_setup_ap(e)
    c = TestClient(app)
    assert c.post("/api/setup/name", json={"name": "pi1"}).status_code == 200
    assert c.post("/api/setup/name", json={"name": "pi9"}).status_code == 409


@pytest.mark.parametrize("bad", ["", " ", "has space", "-pi", "pi-", "a" * 40, "pi_1", "pi.1", "ünï", "pi1;rm"])
def test_bad_names_are_refused_before_anything_changes(tmp_path, monkeypatch, bad):
    app, e, d = mkapp(tmp_path, monkeypatch)
    seed_setup_ap(e)
    r = TestClient(app).post("/api/setup/name", json={"name": bad})
    assert r.status_code == 422
    assert os.path.exists(os.path.join(d, FB.MARKER)) and not os.path.exists(os.path.join(e.tmp, "etc", "hostname"))


def test_a_name_used_by_another_pi_on_the_network_is_refused(tmp_path, monkeypatch):
    app, e, d = mkapp(tmp_path, monkeypatch)
    seed_setup_ap(e)
    st = app.state.tt
    monkeypatch.setattr(st.peers, "list", lambda: [{"id": "abcd", "name": "pi1", "hostname": "pi1"}, {"id": "ef01", "name": "Pi2", "hostname": "pi2"}])
    c = TestClient(app)
    assert c.get("/api/setup/info").json()["suggested"] == "pi3"
    r = c.post("/api/setup/name", json={"name": "PI2"})
    assert r.status_code == 409 and "pi2" in r.json()["detail"]
    assert c.post("/api/setup/name", json={"name": "pi3"}).status_code == 200


def test_the_second_pi_is_offered_the_next_free_name_found_over_mdns(tmp_path, monkeypatch):
    """The 'second instance discovers the first' rule at unit level: the discovery registry feeds suggest_name."""
    assert FB.suggest_name([]) == "pi1"
    assert FB.suggest_name(["pi1"]) == "pi2"
    assert FB.suggest_name(["PI1", "pi3", "pi2"]) == "pi4"
    assert FB.suggest_name(["pi2"]) == "pi1"                       # first FREE one, not max+1
    assert FB.taken_names([{"name": "Lab Pi", "hostname": "pi5.local"}, {"name": "", "hostname": None}]) == ["Lab Pi", "pi5"]


def test_helper_missing_gives_a_plain_message_and_keeps_the_screen(tmp_path, monkeypatch):
    app, e, d = mkapp(tmp_path, monkeypatch)
    monkeypatch.delenv("TT_NET_FAKE")
    monkeypatch.delenv("TT_UPDATE_MODE", raising=False)
    r = TestClient(app).post("/api/setup/name", json={"name": "pi1"})
    assert r.status_code == 503 and "installed service" in r.json()["detail"]
    assert os.path.exists(os.path.join(d, FB.MARKER))


# ------------------------------------------------------------------ the root helper op
def test_op_set_name_keeps_a_user_chosen_ap_name_but_still_names_the_host(env):
    seed_setup_ap(env, "MyOwnWifi")
    res = netops.handle(env.ctx, {"action": "net", "op": "set_name", "name": "pi2", "id": "r1"})
    assert res["ok"] and res["ap_renamed"] is False and res["ssid"] == "MyOwnWifi"
    assert open(os.path.join(env.tmp, "etc", "hostname")).read().strip() == "pi2"


def test_op_set_name_renames_an_ap_that_still_has_the_old_default_name(env):
    seed_setup_ap(env, "raspberrypi")
    res = netops.handle(env.ctx, {"action": "net", "op": "set_name", "name": "pi2", "old_name": "raspberrypi", "id": "r2"})
    assert res["ok"] and res["ap_renamed"] is True and res["ssid"] == "pi2"


def test_op_set_name_removes_the_setup_captive_dns_and_schedules_the_ap_restart(env):
    seed_setup_ap(env)
    cp = os.path.join(env.ctx.etc_dir, netops.CAPTIVE_REL)
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    open(cp, "w").write(netops.CAPTIVE_TEXT)
    netops.handle(env.ctx, {"action": "net", "op": "set_name", "name": "pi1", "id": "r3"})
    assert not os.path.exists(cp)
    assert any("tt-ap-restart" in " ".join(c) for c in env.nm.load()["calls"])


@pytest.mark.parametrize("bad", ["", "UPPER space", "x" * 33, "-a", "a--", "a b"])
def test_op_set_name_validates(env, bad):
    seed_setup_ap(env)
    res = netops.handle(env.ctx, {"action": "net", "op": "set_name", "name": bad, "id": "r4"})
    assert res["ok"] is False and res["code"] == "bad_name"
    assert not os.path.exists(os.path.join(env.tmp, "etc", "hostname"))


def test_captive_dns_text_answers_every_name_with_the_pi():
    assert "address=/#/10.42.0.1" in netops.CAPTIVE_TEXT


# ------------------------------------------------------------------ Imager Wi-Fi: honour it
def test_firstboot_without_imager_wifi_keeps_the_pi_network_on(env):
    seed_setup_ap(env)
    res = netops.firstboot(env.ctx)
    assert res["own_wifi"] is False and "pi-setup" in res["message"]
    assert netops.get_private(env.ctx)["ap"]["autoconnect"] is True


def test_firstboot_sets_a_persistent_wifi_country_only_when_none_is_set(env):
    calls = []

    class Runner:
        def __init__(self, reg):
            self.reg = reg
            self.inner = env.runner

        def run(self, argv, timeout=60):
            calls.append(list(argv))
            if argv[:3] == ["iw", "reg", "get"]:
                return netops.R(0, self.reg, "")
            if argv and argv[0] in ("rfkill", "raspi-config", "iw"):
                return netops.R(0, "", "")
            return self.inner.run(argv, timeout)
    seed_setup_ap(env)
    env.ctx.runner = Runner("global\ncountry 00: DFS-UNSET\n")
    netops.firstboot(env.ctx)
    assert ["rfkill", "unblock", "wifi"] in calls and ["raspi-config", "nonint", "do_wifi_country", "US"] in calls
    calls.clear()
    env.ctx.runner = Runner("global\ncountry DE: DFS-ETSI\n")                 # Imager set a country: left alone
    netops.firstboot(env.ctx)
    assert ["rfkill", "unblock", "wifi"] in calls and not any(c[:2] == ["raspi-config", "nonint"] or c[:3] == ["iw", "reg", "set"] for c in calls)


def test_firstboot_with_imager_wifi_profile_turns_the_pi_network_off(env):
    seed_setup_ap(env)
    d = env.nm.load()      # what netplan leaves behind for Imager's Wi-Fi: a non-tt Wi-Fi client profile
    d["profiles"].append({"name": "netplan-wlan0-Site", "uuid": "33333333-0000-0000-0000-000000000001", "type": "wifi",
                          "props": {"connection.interface-name": "wlan0", "connection.autoconnect": "yes", "802-11-wireless.ssid": "Site", "802-11-wireless.mode": "infrastructure"}})
    env.nm.save(d)
    res = netops.firstboot(env.ctx)
    assert res["own_wifi"] is True and "stays off" in res["message"]
    assert netops.get_private(env.ctx)["ap"]["autoconnect"] is False
    prof = next(p for p in env.nm.load()["profiles"] if p["name"] == "tt-ap")
    assert prof["props"]["connection.autoconnect"] == "no"


def test_firstboot_reads_a_cloud_init_netplan_file_too(env, tmp_path):
    seed_setup_ap(env)
    npdir = os.path.join(env.ctx.etc_dir, "netplan"); os.makedirs(npdir)
    open(os.path.join(npdir, "50-cloud-init.yaml"), "w").write("network:\n  version: 2\n  wifis:\n    wlan0:\n      dhcp4: true\n      access-points:\n        \"Site\":\n          password: \"x\"\n")
    assert netops.firstboot(env.ctx)["own_wifi"] is True


def test_firstboot_edits_the_keyfile_when_networkmanager_is_not_running(env):
    """Image chroot / early boot: no NM yet -> the keyfile is what changes."""
    os.makedirs(env.ctx.keyfile_dir, exist_ok=True)
    netops.setup_default_ap(env.ctx, ssid="pi-setup", keyfile_only=True)
    p = os.path.join(env.ctx.keyfile_dir, "tt-ap.nmconnection")
    assert "autoconnect=true" in open(p).read()
    d = env.nm.load(); d["profiles"] = []; env.nm.save(d)
    npdir = os.path.join(env.ctx.etc_dir, "netplan"); os.makedirs(npdir)
    open(os.path.join(npdir, "50-cloud-init.yaml"), "w").write("network:\n  wifis:\n    wlan0:\n      access-points:\n        S: {}\n")
    env.ctx.runner = type("Dead", (), {"run": lambda self, argv, timeout=60: netops.R(1, "", "NetworkManager is not running")})()
    netops.firstboot(env.ctx)
    assert "autoconnect=false" in open(p).read() and "ssid=pi-setup" in open(p).read()


# ------------------------------------------------------------------ port 80 redirect (captive page)
def test_redirect_server_sends_every_request_to_the_app():
    srv = FB.start_redirect(8080, listen_port=0, host="127.0.0.1")
    assert srv is not None
    try:
        port = srv.server_address[1]
        class NoFollow(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(NoFollow)
        for path in ("/", "/generate_204", "/hotspot-detect.html", "/ncsi.txt"):
            with pytest.raises(urllib.error.HTTPError) as ei:
                op.open(f"http://127.0.0.1:{port}{path}", timeout=3)
            assert ei.value.code == 302 and ei.value.headers["Location"] == "http://127.0.0.1:8080/"
    finally:
        srv.shutdown()


def test_redirect_server_gives_up_quietly_when_the_port_is_taken():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
    try:
        assert FB.start_redirect(8080, listen_port=s.getsockname()[1], host="127.0.0.1") is None
    finally:
        s.close()
