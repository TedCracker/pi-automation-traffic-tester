"""The card-image tooling: shell syntax, the installer's --image mode (static), START-HERE.pdf, and the rules the build promises (checked as text; the build itself runs in Docker)."""
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(p):
    return open(os.path.join(ROOT, p)).read()


@pytest.mark.parametrize("p", ["scripts/install-pi.sh", "scripts/pkg/build-image.sh", "scripts/pkg/build-image-inner.sh", "scripts/pkg/qemu-bench.sh", "scripts/e2e-image.sh"])
def test_shell_syntax(p):
    assert subprocess.run(["bash", "-n", os.path.join(ROOT, p)], capture_output=True).returncode == 0


def test_installer_has_the_image_mode_and_documents_it():
    s = read("scripts/install-pi.sh")
    for flag in ("--image", "--ap-ssid", "--nexmon"):
        assert flag in s
    assert "AP_SSID_OPT=pi-setup" in s                                   # a card starts as the setup network
    assert ': > "$DATA/first-boot"' in s                                  # the marker that turns on the Name-this-Pi screen
    assert "tt-captive.conf" in s and "traffic-tester-firstboot.service" in s
    assert "--keyfile-only" in s                                          # no NetworkManager is running in the chroot
    out = subprocess.run(["bash", os.path.join(ROOT, "scripts/install-pi.sh"), "--help"], capture_output=True, text=True).stdout
    assert "--image" in out and "pi-setup" in out


def test_firstboot_unit_cannot_create_an_ordering_cycle_with_networkmanager():
    s = read("scripts/install-pi.sh")
    unit = s[s.index("[Unit]\nDescription=Pi Automation Traffic Tester - first boot"):s.index("FBUNIT\n  systemctl enable")]
    assert "Before=NetworkManager" not in unit and "After=cloud-init-network.service cloud-config.service NetworkManager.service" in unit
    assert "ConditionPathExists=!/var/lib/traffic-tester-update/firstboot-done" in unit      # runs once per card


def test_a_normal_install_never_gets_the_first_boot_marker():
    s = read("scripts/install-pi.sh")
    i = s.index('if [ "$IMAGE" -eq 1 ]; then\n  say "Card image: first-boot')
    assert s.count(': > "$DATA/first-boot"') == 1 and i < s.index(': > "$DATA/first-boot"')


def test_build_verifies_the_base_checksum_and_cleans_identity():
    b = read("scripts/pkg/build-image.sh")
    assert "sha256sum -c" in b and "downloads.raspberrypi.com/raspios_lite_arm64_latest" in b and "xz -T0" in b and "resize2fs" in read("scripts/pkg/build-image-inner.sh")
    i = read("scripts/pkg/build-image-inner.sh")
    for must in ("traffic-tester/*.db", "firstboot-done", "echo uninitialized > $R/etc/machine-id", "ssh_host_*", "resize2fs -M", "IMAGE CHECKS FAILED"):
        assert must in i, must
    assert 'ck "SSH is off' in i and 'ck "no account has a usable password' in i and 'ck "no app database on the card"' in i


def test_the_card_keeps_the_stock_first_boot_files():
    i = read("scripts/pkg/build-image-inner.sh")
    assert "cmdline.txt" in i                                              # checked, never edited
    for line in i.splitlines():
        if "cmdline.txt" in line and not line.lstrip().startswith("#"):
            assert "sed " not in line and "tee" not in line and not re.search(r">>?\s*\S*cmdline", line), line        # only ever read by a check


def test_start_here_pdf_is_one_page_with_the_three_steps(tmp_path):
    out = tmp_path / "s.pdf"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts/pkg/make-start-here.py"), str(out), "pi-traffic-tester-9.9.9.img.xz"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and len(re.findall(rb"/Type\s*/Page\b", data)) == 1
    txt = subprocess.run(["pdftotext", str(out), "-"], capture_output=True, text=True).stdout if subprocess.run(["which", "pdftotext"], capture_output=True).returncode == 0 else None
    if txt is not None:
        for s in ("pi-setup", "password", "10.42.0.1:8080", "Raspberry Pi Imager", "pi-traffic-tester-9.9.9.img.xz", "Name this Pi"):
            assert s in txt, s


def test_installer_keeps_ssh_off_at_the_first_boot_with_a_preset():
    s = read("scripts/install-pi.sh")
    assert "00-traffic-tester-ssh-off.preset" in s and "disable ssh.service" in s and "disable ssh.socket" in s
    assert s.index("00-traffic-tester-ssh-off.preset") > s.index('if [ "$IMAGE" -eq 1 ]; then\n  say "Card image: first-boot')      # only card images
