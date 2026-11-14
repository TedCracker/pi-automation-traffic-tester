"""Packaging scripts: syntax, options and the generated-unit contract (the real install is tested in an arm64 container, see checks.md)."""
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = ["scripts/install-pi.sh", "scripts/install-wifi-nexmon.sh", "scripts/pkg/install-offline.sh", "scripts/pkg/build-offline-package.sh",
           "scripts/pkg/build-offline-apt.sh", "scripts/pkg/build-nexmon-modules.sh", "scripts/pkg/nexmon-inner-build.sh", "scripts/pkg/fetch-nexmon.sh",
           "scripts/pkg/build-on-pi.sh", "scripts/pkg/nexmon-build-test.sh", "scripts/build-usb-package.sh", "scripts/e2e-netcfg.sh"]


def test_scripts_have_valid_bash_syntax():
    for s in SCRIPTS:
        r = subprocess.run(["bash", "-n", os.path.join(ROOT, s)], capture_output=True, text=True)
        assert r.returncode == 0, f"{s}: {r.stderr}"


def test_installer_documents_offline_and_nexmon_options():
    r = subprocess.run(["bash", os.path.join(ROOT, "scripts/install-pi.sh"), "--help"], capture_output=True, text=True)
    assert "--offline" in r.stdout and "--no-wifi-nexmon" in r.stdout
    assert "--wifi-mode" in r.stdout and "--role A|B" in r.stdout and "ignored" in r.stdout          # roles are gone: accepted, ignored, documented


def test_nexmon_installer_refuses_without_root_or_dir():
    r = subprocess.run(["bash", os.path.join(ROOT, "scripts/install-wifi-nexmon.sh")], capture_output=True, text=True)
    assert r.returncode != 0


def test_polkit_rule_in_installer_names_only_helper_commands():
    s = open(os.path.join(ROOT, "scripts/install-wifi-nexmon.sh")).read()
    from traffic_tester.sniff import wifi_helper
    cmds = {"status", "monitor", "channel", "restore", "inject-test"}
    assert cmds <= set(wifi_helper.COMMANDS)
    assert "(status|monitor|channel|restore|inject-test)" in s
    rule = s.split("polkit.addRule")[1].split("RULE")[0]            # the rule text only
    assert "watchdog" not in rule and "boot" not in rule             # watchdog and boot-restore run from systemd, never on the app's request


def test_offline_apt_list_covers_the_installer_packages():
    inst = open(os.path.join(ROOT, "scripts/install-pi.sh")).read()
    listed = open(os.path.join(ROOT, "scripts/pkg/apt-packages.txt")).read().split()
    for p in ["python3", "python3-venv", "python3-pip", "chrony", "iproute2", "socat", "ca-certificates", "tshark", "tcpdump", "iw", "ethtool", "kmod", "git", "polkitd", "aircrack-ng",
              "network-manager", "dnsmasq-base", "avahi-daemon"]:                       # the Wi-Fi access point needs NetworkManager + its DHCP helper; mDNS gives <hostname>.local
        assert p in listed, p
        assert p in inst, p


def test_nexmon_patch_is_present_and_minimal():
    p = open(os.path.join(ROOT, "scripts/pkg/nexmon-patches/0001-pi-os-6.12-build-fixes.patch")).read()
    assert "NEXMON_SETCHAN_3ARG" in p and "SDIO_DEVICE_ID_BROADCOM_CYPRESS_43752" in p
    assert p.count("\ndiff -ru") + p.startswith("diff -ru") == 3          # cfg80211.c, Makefile, include/brcm_hw_ids.h


def test_usb_package_scripts_one_installer_and_aliases(tmp_path):
    """build-usb-package.sh writes install.sh (the one installer) plus install-A.sh / install-B.sh as aliases that call it."""
    out = tmp_path / "usb"
    out.mkdir()
    env = dict(os.environ, TT_DEPLOY_KEY=str(tmp_path / "no-key"))
    r = subprocess.run(["bash", os.path.join(ROOT, "scripts/build-usb-package.sh"), str(out)], capture_output=True, text=True, cwd=ROOT, env=env)
    assert r.returncode == 0, r.stderr
    for f in ("pi-traffic-tester.tar.gz", "install.sh", "install-A.sh", "install-B.sh"):
        assert (out / f).exists(), f
    for f in ("install.sh", "install-A.sh", "install-B.sh"):
        assert subprocess.run(["bash", "-n", str(out / f)]).returncode == 0
        assert os.access(out / f, os.X_OK)
    inst = (out / "install.sh").read_text()
    assert "scripts/install-pi.sh" in inst and "--role" not in inst and not re.search(r"\bPi [AB]\b", inst) and '"$@"' in inst
    for alias in ("install-A.sh", "install-B.sh"):
        a = (out / alias).read_text()
        assert "install.sh" in a and "--role" not in a
    # behaviour: the aliases call install.sh next to them; an address becomes --ntp-server; other options pass through
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "install.sh").write_text('#!/bin/bash\necho "ARGS:$*"\n')
    for alias in ("install-A.sh", "install-B.sh"):
        (stub / alias).write_text((out / alias).read_text())
        run = lambda *a: subprocess.run(["bash", str(stub / alias), *a], capture_output=True, text=True).stdout.strip()      # noqa: E731
        assert run() == "ARGS:"
        assert run("192.168.4.20") == "ARGS:--ntp-server 192.168.4.20"
        assert run("192.168.4.20", "--port", "8081") == "ARGS:--ntp-server 192.168.4.20 --port 8081"
        assert run("--wifi-mode", "keep") == "ARGS:--wifi-mode keep"
        assert run("--role", "B") == "ARGS:--role B"                                    # still passed on; the installer ignores it with a note
