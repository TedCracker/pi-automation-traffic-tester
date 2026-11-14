"""Network split: Wi-Fi = internet/management, Ethernet test ports never the default route."""
import json
import os
import shutil
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "net-split.sh")


def _run(args, env=None, **kw):
    return subprocess.run(args, capture_output=True, text=True, env={**os.environ, **(env or {})}, cwd=ROOT, **kw)


def test_net_split_networkmanager_path_uses_never_default(tmp_path):
    log = tmp_path / "nm.log"
    stub = tmp_path / "nmcli"
    stub.write_text(f"""#!/bin/sh
echo "$@" >> {log}
case "$*" in
  *"-f NAME,TYPE connection show"*) printf 'Wired connection 1:802-3-ethernet\\nHome Wi-Fi:802-11-wireless\\nbr0:bridge\\n';;
esac
exit 0
""")
    stub.chmod(0o755)
    env = {"TT_NMCLI": str(stub), "TT_NM_ACTIVE": "1"}
    r = _run(["bash", SCRIPT, "--apply"], env)
    assert r.returncode == 0, r.stderr
    calls = log.read_text()
    assert "connection modify Wired connection 1 ipv4.never-default yes ipv6.never-default yes" in calls
    assert "Home Wi-Fi" not in calls.replace("connection show", "") and "br0" not in calls.replace("connection show", "")     # only Ethernet profiles are touched
    r = _run(["bash", SCRIPT, "--undo"], env)
    assert r.returncode == 0 and "ipv4.never-default no ipv6.never-default no" in log.read_text()


def test_net_split_dhcpcd_path_is_idempotent_and_undoable(tmp_path):
    conf = tmp_path / "dhcpcd.conf"
    conf.write_text("hostname\nclientid\n")
    env = {"TT_DHCPCD_CONF": str(conf), "TT_NM_ACTIVE": "0"}
    for _ in range(2):
        assert _run(["bash", SCRIPT, "--apply"], env).returncode == 0
    txt = conf.read_text()
    assert txt.count("# >>> traffic-tester net split") == 1 and "nogateway" in txt and txt.startswith("hostname\nclientid\n")
    assert _run(["bash", SCRIPT, "--undo"], env).returncode == 0
    assert conf.read_text() == "hostname\nclientid\n"


def test_net_split_usage_error():
    assert _run(["bash", SCRIPT, "--nope"]).returncode == 64


def _have_netns():
    if not shutil.which("ip"):
        return False
    return subprocess.run(["sudo", "-n", "ip", "netns", "list"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _have_netns(), reason="needs passwordless sudo and ip netns")
def test_routing_self_check_in_a_netns_with_two_fake_interfaces():
    ns = "tt" + uuid.uuid4().hex[:6]
    sh = lambda c: subprocess.run(["sudo", "-n", "bash", "-c", c], capture_output=True, text=True)
    def check(*extra):
        r = subprocess.run(["sudo", "-n", "ip", "netns", "exec", ns, sys.executable, "-m", "traffic_tester.netcheck", "--json", *extra], capture_output=True, text=True, cwd=ROOT)
        return r.returncode, json.loads(r.stdout or "{}")
    try:
        assert sh(f"ip netns add {ns} && ip netns exec {ns} bash -c 'ip link set lo up; ip link add wlan0 type dummy; ip link add eth0 type dummy; ip link add eth1 type dummy; "
                  f"for i in wlan0 eth0 eth1; do ip link set $i up; done; ip addr add 192.168.1.50/24 dev wlan0; ip addr add 192.168.50.1/24 dev eth0; ip addr add 192.168.60.1/24 dev eth1'").returncode == 0
        sh(f"ip netns exec {ns} ip route add default via 192.168.1.1 dev wlan0")
        rc, j = check("--peer", "192.168.50.2")
        assert rc in (0, 1) and j["status"] in ("ok", "warn") and all(c["s"] != "bad" for c in j["checks"]), j            # internet via Wi-Fi, test peer via Ethernet: fine
        sh(f"ip netns exec {ns} ip route add default via 192.168.50.254 dev eth0 metric 10")                              # the mistake we guard against
        rc, j = check("--peer", "192.168.50.2")
        assert rc == 2 and j["status"] == "bad" and any(c["s"] == "bad" and c["fix"] for c in j["checks"]), j
        sh(f"ip netns exec {ns} ip route del default dev eth0; ip netns exec {ns} ip route del default dev wlan0")
        rc, j = check("--peer", "192.168.50.2")
        assert rc in (0, 1) and j["status"] != "bad", j                                                                   # no internet at all: tester still fine (fully offline use)
    finally:
        sh(f"ip netns del {ns}")
