"""The privileged helper: helper.run() hands {"action": "net" | "clock"} requests to netops with a fake NetworkManager; the clock op writes chrony's config;
unknown or invalid requests are refused; the revert entry point; and the installer scripts' text (no roles, AP default, netwatch, chrony)."""
import json
import os
import subprocess

import pytest

from netcfgfix import Env
from traffic_tester.update import helper as H, netops

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def helper_env(env, monkeypatch):
    """A rootless-style layout pointing at the sandbox, with the in-process fake network switched on."""
    for k, v in (("TT_NET_FAKE", "1"), ("TT_NET_STATE_DIR", env.state), ("TT_NET_SYSFS", env.sysfs), ("TT_FAKE_NM_STATE", env.nm_path), ("TT_NET_CONF", os.path.join(env.tmp, "none.conf")),
                 ("TT_CHRONY_CONF", env.ctx.chrony_conf), ("TT_CHRONY_MAIN_CONF", env.ctx.chrony_main), ("TT_NM_KEYFILE_DIR", env.ctx.keyfile_dir), ("TT_NET_DATA_DIR", env.data)):
        monkeypatch.setenv(k, v)
    os.makedirs(env.state, exist_ok=True)
    os.makedirs(os.path.join(env.data, "update"), exist_ok=True)
    layout = os.path.join(env.tmp, "layout.json")
    json.dump({"mode": "user", "app_dir": os.path.join(env.tmp, "app"), "data_dir": env.data, "state_dir": env.state, "work_dir": os.path.join(env.tmp, "work"), "pubkey": "00" * 32}, open(layout, "w"))
    env.layout = layout
    env.request = os.path.join(env.data, "update", "request.json")
    return env


def send(env, req):
    json.dump(req, open(env.request, "w"))
    rc = H.run(["run", "--layout", env.layout])
    res = netops.read_json(os.path.join(env.state, netops.F_RESULT))
    return rc, res


def test_net_request_is_dispatched_and_the_result_is_world_readable(helper_env):
    rc, res = send(helper_env, {"action": "net", "id": "abc123", "op": "scan"})
    assert rc == 0 and res["id"] == "abc123" and res["ok"] and [n["ssid"] for n in res["networks"]] == ["HomeNet", "Cafe"]
    assert oct(os.stat(os.path.join(helper_env.state, netops.F_RESULT)).st_mode & 0o777) == "0o644"
    assert not os.path.exists(helper_env.request)                                                              # consumed


def test_apply_through_the_helper_then_keep(helper_env):
    ch = {"ports": {"eth1": {"method": "static", "address": "192.168.8.2/24", "gateway": "192.168.8.1", "label": "internet"}}}
    rc, res = send(helper_env, {"action": "net", "id": "a1", "op": "apply", "changes": ch, "auto_revert": True, "summary": ["eth1: static"]})
    assert res["ok"] and res["pending"]["state"] == "waiting" and "tt-eth-eth1" in helper_env.names()
    assert "psk" not in json.dumps(res)
    rc, res = send(helper_env, {"action": "net", "id": "a2", "op": "keep"})
    assert res["ok"] and res["code"] == "kept" and netops.get_pending(helper_env.ctx) is None


def test_refuses_unknown_actions_ops_and_bad_values(helper_env):
    rc, res = send(helper_env, {"action": "net", "id": "u1", "op": "rm -rf /"})
    assert not res["ok"] and res["code"] == "bad_request" and res["id"] == "u1"
    rc, res = send(helper_env, {"action": "net", "id": "u2", "op": "apply", "changes": {"wifi": {"mode": "ap", "ssid": "x" * 33, "password": "12345678"}}})
    assert not res["ok"] and res["code"] == "bad_ssid" and "32 bytes" in res["message"] and helper_env.names() == ["HomeNet", "Wired connection 1"]
    rc, res = send(helper_env, {"action": "net", "id": "u3", "op": "apply", "changes": {"ports": {"eth0; reboot": {"method": "dhcp"}}}})
    assert not res["ok"] and res["code"] == "bad_iface"
    rc, res = send(helper_env, {"action": "net", "id": "bad id!;", "op": "scan"})
    assert res["ok"] and res["id"] == ""                                                                       # an odd id is dropped, never echoed into a path
    json.dump({"action": "format-disk"}, open(helper_env.request, "w"))
    assert H.run(["run", "--layout", helper_env.layout]) == 2                                                  # the update path still refuses unknown actions


def test_a_second_request_arriving_meanwhile_is_not_lost(helper_env, monkeypatch):
    seen = []
    real = netops.handle

    def handle(ctx, req):
        seen.append(req["id"])
        if len(seen) == 1:                                                                                     # a newer request lands while the first is handled
            json.dump({"action": "net", "id": "second", "op": "scan"}, open(helper_env.request, "w"))
        return real(ctx, req)
    monkeypatch.setattr(netops, "handle", handle)
    json.dump({"action": "net", "id": "first", "op": "scan"}, open(helper_env.request, "w"))
    assert H.run(["run", "--layout", helper_env.layout]) == 0
    assert seen == ["first", "second"] and not os.path.exists(helper_env.request)


def test_rootless_layout_cannot_change_the_network(helper_env, monkeypatch):
    monkeypatch.delenv("TT_NET_FAKE")
    rc, res = send(helper_env, {"action": "net", "id": "r1", "op": "apply", "changes": {"wifi": {"mode": "off"}}})
    assert not res["ok"] and res["code"] == "needs_service" and helper_env.names() == ["HomeNet", "Wired connection 1"]


def test_net_revert_entry_point_restores_after_the_deadline(helper_env):
    before = helper_env.view()
    send(helper_env, {"action": "net", "id": "a1", "op": "apply", "changes": {"wifi": {"mode": "off"}}, "auto_revert": True})
    assert helper_env.view() != before
    pend = netops.get_pending(helper_env.ctx)
    assert H.run(["net-revert"]) == 0 and helper_env.view() != before                                          # the timer fired early: ignored
    pend["deadline"] = 1.0
    netops.write_json(os.path.join(helper_env.state, netops.F_PENDING), pend)
    assert H.run(["net-revert"]) == 0 and helper_env.view() == before and netops.get_pending(helper_env.ctx) is None
    assert netops.get_public(helper_env.ctx)["last_event"]["reason"] == "timeout"


def test_real_scheduler_arms_systemd_run_with_the_helper_and_net_revert(env):
    calls = []

    class Rec:
        def run(self, argv, timeout=60):
            calls.append(list(argv))
            return netops.R(0)
    s = netops.SystemdScheduler(Rec(), python="/usr/bin/python3", helper_py="/opt/x/helper.py")
    s.schedule(90, "tt-net-revert", ["net-revert"])
    cmd = next(c for c in calls if c[0] == "systemd-run")
    assert cmd[1] == "--on-active=90s" and "--unit=tt-net-revert" in cmd and "--collect" in cmd
    assert cmd[-4:] == ["/usr/bin/python3", "-I", "/opt/x/helper.py", "net-revert"]
    assert ["systemctl", "stop", "tt-net-revert.timer"] in calls
    s.cancel("tt-net-revert")
    assert calls[-2:] == [["systemctl", "stop", "tt-net-revert.timer"], ["systemctl", "reset-failed", "tt-net-revert.service"]]

    class Fail:
        def run(self, argv, timeout=60):
            return netops.R(1 if argv[0] == "systemd-run" else 0, "", "boom")
    with pytest.raises(netops.NetError) as e:
        netops.SystemdScheduler(Fail()).schedule(90, "tt-net-revert", ["net-revert"])
    assert e.value.code == "timer_failed"


# ------------------------------------------------------------------ clock op (peer clock feature)
def conf(env):
    return open(env.ctx.chrony_conf).read()


def test_clock_serve_writes_local_stratum_and_private_allow_lines_no_server(helper_env):
    rc, res = send(helper_env, {"action": "clock", "id": "c1", "mode": "serve"})
    txt = conf(helper_env)
    assert res["ok"] and res["mode"] == "serve" and res["restarted"]
    assert "local stratum 10" in txt and "server " not in txt.replace("# ", "")
    for r in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert f"allow {r}" in txt
    assert not [l for l in txt.splitlines() if l.startswith("server")]
    assert oct(os.stat(helper_env.ctx.chrony_conf).st_mode & 0o777) == "0o644" and not [f for f in os.listdir(os.path.dirname(helper_env.ctx.chrony_conf)) if f.endswith(".tmp")]
    assert ["systemctl", "restart", "chrony"] in helper_env.nm.load()["calls"]


def test_clock_client_writes_server_line_and_allow_no_local(helper_env):
    rc, res = send(helper_env, {"action": "clock", "id": "c2", "mode": "client", "server": "192.168.4.20"})
    txt = conf(helper_env)
    assert res["ok"] and res["mode"] == "client" and res["server"] == "192.168.4.20"
    assert "server 192.168.4.20 iburst prefer minpoll 0 maxpoll 4" in txt.splitlines() and not [l for l in txt.splitlines() if l.startswith("local")]
    assert "allow 192.168.0.0/16" in txt
    rc, res = send(helper_env, {"action": "clock", "id": "c3", "mode": "status"})
    assert res["mode"] == "client" and res["server"] == "192.168.4.20"
    rc, res = send(helper_env, {"action": "clock", "id": "c4", "mode": "serve"})                                  # switching back replaces the file
    assert res["mode"] == "serve" and "server 192.168.4.20" not in conf(helper_env)
    rc, res = send(helper_env, {"action": "clock", "id": "c5", "mode": "status"})
    assert res["mode"] == "serve" and res["server"] is None


def test_clock_status_when_nothing_is_configured_and_none_mode(helper_env):
    rc, res = send(helper_env, {"action": "clock", "id": "c0", "mode": "status"})
    assert res["ok"] and res["mode"] == "none" and res["configured"] is False
    rc, res = send(helper_env, {"action": "clock", "id": "c6", "mode": "none"})
    txt = conf(helper_env)
    assert res["ok"] and "allow 10.0.0.0/8" in txt and "local" not in txt.replace("# ", "") and "server" not in txt


def test_clock_refuses_invalid_requests_and_leaves_the_file_alone(helper_env):
    send(helper_env, {"action": "clock", "id": "ok", "mode": "serve"})
    good = conf(helper_env)
    for i, req in enumerate(({"mode": "client", "server": "192.168.4.20; reboot"}, {"mode": "client", "server": "time.example.com"}, {"mode": "client"}, {"mode": "client", "server": "127.0.0.1"},
                             {"mode": "client", "server": "0.0.0.0"}, {"mode": "client", "server": "224.0.0.1"}, {"mode": "client", "server": "1.2.3.4\nlocal stratum 1"}, {"mode": "evil"},
                             {"mode": "serve", "server": "10.0.0.1"}, {"mode": None}, {})):
        rc, res = send(helper_env, {"action": "clock", "id": f"bad{i}", **req})
        assert not res["ok"] and res["code"] in ("bad_ip", "bad_request"), req
        assert conf(helper_env) == good
    assert not [l for l in open(helper_env.ctx.chrony_conf).read().splitlines() if l.startswith("server")]


def test_clock_adds_confdir_once_when_chrony_conf_lacks_it(helper_env):
    os.makedirs(os.path.dirname(helper_env.ctx.chrony_main), exist_ok=True)
    open(helper_env.ctx.chrony_main, "w").write("pool 2.debian.pool.ntp.org iburst\n")
    send(helper_env, {"action": "clock", "id": "d1", "mode": "serve"})
    send(helper_env, {"action": "clock", "id": "d2", "mode": "serve"})
    assert open(helper_env.ctx.chrony_main).read().count("confdir ") == 1


# ------------------------------------------------------------------ installer text (no roles, AP default, netwatch, chrony)
def script(name):
    return open(os.path.join(ROOT, name)).read()


def test_installer_ignores_role_with_a_note_and_writes_no_role():
    s = script("scripts/install-pi.sh")
    note = "There are no fixed roles any more: every Pi is the same. --role is ignored."
    assert note in s
    r = subprocess.run(["bash", os.path.join(ROOT, "scripts/install-pi.sh"), "--role", "B", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and note in (r.stdout + r.stderr)
    r = subprocess.run(["bash", os.path.join(ROOT, "scripts/install-pi.sh"), "--role", "X", "--help"], capture_output=True, text=True)
    assert r.returncode == 0                                                                                     # any value is accepted and ignored
    assert "TT_ROLE=" not in s.split("cat > \"$CONF\"")[1].split("CFG\n", 2)[1]
    assert "Installed. Pi name:" in s and "Installed as Pi" not in s and "Start Responder" not in s


def test_installer_chrony_has_private_allow_lines_and_no_role_logic():
    s = script("scripts/install-pi.sh")
    block = s.split("# ------------------------------------------------------------------ clock")[1].split("# ------------------------------------------------------------------ built-in Wi-Fi")[0]
    assert "local stratum" not in block and "$ROLE" not in block
    for r in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert f"allow {r}" in block
    assert '[ -n "$NTP" ] && echo "server $NTP iburst prefer minpoll 0 maxpoll 4"' in block


def test_installer_wifi_default_is_ap_for_fresh_installs_keep_for_existing():
    s = script("scripts/install-pi.sh")
    r = subprocess.run(["bash", os.path.join(ROOT, "scripts/install-pi.sh"), "--help"], capture_output=True, text=True)
    assert "--wifi-mode ap|keep" in r.stdout and "FRESH" in r.stdout.upper() and "existing install" in r.stdout
    assert '[ -f "$CONF" ] && FRESH=0' in s                                                    # no config yet = a fresh install
    assert 'if [ "$FRESH" -eq 1 ]; then WIFI_MODE="ap"; else WIFI_MODE="keep"; fi' in s
    assert '--wifi-mode must be ap or keep' in s
    assert 'setup-default-ap' in s and "netops.py" in s and "--activate" in s
    assert "traffic-tester-netwatch.service" in s and "ExecStart=/usr/bin/python3 -I $NETOPS watch" in s and "netops.py" in s
    assert '[ "$WIFI_MODE" = ap ] && NET_SPLIT=0' in s                                         # Ethernet may be the default route on the new AP layout
    # activation (which drops a Wi-Fi client) never happens under a remote shell that runs over Wi-Fi
    assert "ssh_over_wifi" in s and "AP_NOW" in s
    r = subprocess.run(["bash", os.path.join(ROOT, "scripts/install-pi.sh"), "--wifi-mode", "dhcp", "--help"], capture_output=True, text=True)
    assert r.returncode == 0                                                                    # --help wins; the bad value is rejected on a real run (below)
    r = subprocess.run(["bash", os.path.join(ROOT, "scripts/install-pi.sh"), "--wifi-mode", "dhcp"], capture_output=True, text=True)
    assert r.returncode != 0 and "--wifi-mode must be ap or keep" in r.stderr


def test_installer_uninstall_removes_the_netwatch_unit_and_the_ap_profile():
    s = script("scripts/install-pi.sh")
    un = s.split("# ------------------------------------------------------------------ uninstall")[1].split("# ------------------------------------------------------------------ checks")[0]
    assert "traffic-tester-netwatch" in un and "tt-ap" in un


def test_offline_installer_and_docs_have_no_roles():
    import re
    for f in ("scripts/pkg/install-offline.sh", "docs/INSTALL-OFFLINE.md", "docs/INSTALL-PI.md"):
        t = script(f)
        assert not re.search(r"install(-pi|-offline)\.sh --role", t) and "Start Responder" not in t, f
        assert "ignored" in t or f.endswith("INSTALL-OFFLINE.md") and "ignored" in t
    assert "install-offline.sh" in script("docs/INSTALL-OFFLINE.md")


def test_installer_keeps_an_existing_role_line_and_survives_a_missing_config(tmp_path):
    """The line that reads the old TT_ROLE runs under `set -euo pipefail` on a FRESH install, where the config file does not exist yet: it must not abort the installer."""
    line = next(l for l in script("scripts/install-pi.sh").splitlines() if l.startswith("OLD_ROLE="))
    case = next(l for l in script("scripts/install-pi.sh").splitlines() if l.startswith('case "$OLD_ROLE" in'))
    conf = tmp_path / "config.env"
    prog = f'set -euo pipefail\nCONF="{conf}"\n{line}\necho "[$OLD_ROLE]"\n'
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "[]", r.stderr                                           # fresh: no file, no abort
    conf.write_text("TT_PORT=8080\nTT_ROLE=B\n")
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True)
    assert r.stdout.strip() == "[B]"
    conf.write_text("TT_PORT=8080\nTT_ROLE=B\n")
    subprocess.run(["bash", "-c", prog + "\n" + case + "\n"], capture_output=True, text=True)
    assert conf.read_text().count("TT_ROLE=B") == 2                                                            # A or B is written back after the file is recreated
    conf.write_text("TT_PORT=8080\nTT_ROLE=$(touch " + str(tmp_path / "pwned") + ")\n")
    before = conf.read_text()
    subprocess.run(["bash", "-c", prog + "\n" + case + "\n"], capture_output=True, text=True)
    assert conf.read_text() == before and not (tmp_path / "pwned").exists()                                    # anything but A or B is dropped, never executed
