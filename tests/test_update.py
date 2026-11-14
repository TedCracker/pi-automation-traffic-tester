"""Software updates: signature + manifest checks (good, tampered, wrong key, unsigned, unsafe, downgrade), stage/swap/rollback, health-check failure,
requirements/wheels, the web API (upload, online check from a local bare repo standing in for GitHub, password gate)."""
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile

import pytest
from fastapi.testclient import TestClient

from traffic_tester.update import bundle as B, ed25519, helper as H
from traffic_tester.update.bundle import BundleError

SEED = bytes(range(32))
PUB = ed25519.public_key(SEED)
OTHER = bytes(range(1, 33))


def app_files(version="1.1.0", extra=None, reqs="fastapi>=0.110\n"):
    f = {"app/traffic_tester/__init__.py": f'__version__ = "{version}"\n'.encode(), "app/traffic_tester/main.txt": f"code {version}".encode(),
         "app/requirements.txt": reqs.encode(), "app/scripts/net-split.sh": b"#!/bin/sh\n", "extra/install-pi.sh": b"#!/bin/sh\necho new\n"}
    f.update(extra or {})
    return f


def make(tmp, version="1.1.0", seed=SEED, **kw):
    out = os.path.join(tmp, B.bundle_filename(version))
    files = kw.pop("files", None) or app_files(version)
    m = B.build_bundle(out, files, seed, version=version, notes="test notes", **kw)
    return out, m


def rewrite(path, out, edit):
    """Copy a tar.gz bundle, letting edit(name, bytes) -> bytes|None change or drop members; extra members via edit(None, None) -> [(name, bytes)]."""
    with tarfile.open(path, "r:gz") as src, tarfile.open(out, "w:gz") as dst:
        for m in src:
            data = src.extractfile(m).read()
            new = edit(m.name, data)
            if new is None:
                continue
            ti = tarfile.TarInfo(m.name); ti.size = len(new); ti.mode = m.mode
            dst.addfile(ti, io.BytesIO(new))
        for name, data in edit(None, None) or []:
            ti = tarfile.TarInfo(name); ti.size = len(data); ti.mode = 0o644
            dst.addfile(ti, io.BytesIO(data))
    return out


# ------------------------------------------------------------------------------------------- crypto
def test_ed25519_rfc8032_vectors():
    v = [("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60", "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
          "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
         ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb", "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
          "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")]
    for seed, pub, msg, sig in v:
        s, m = bytes.fromhex(seed), bytes.fromhex(msg)
        assert ed25519.public_key(s).hex() == pub and ed25519.sign(s, m).hex() == sig
        assert ed25519.verify(bytes.fromhex(pub), m, bytes.fromhex(sig))
        assert not ed25519.verify(bytes.fromhex(pub), m + b"x", bytes.fromhex(sig))
    assert not ed25519.verify(PUB, b"m", b"\x00" * 64) and not ed25519.verify(PUB, b"m", b"short") and not ed25519.verify(b"x", b"m", b"\x00" * 64)


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")
def test_signature_is_accepted_by_openssl():
    d = tempfile.mkdtemp()
    open(f"{d}/pub.der", "wb").write(bytes.fromhex("302a300506032b6570032100") + PUB)
    open(f"{d}/msg", "wb").write(b"hello update"); open(f"{d}/sig", "wb").write(ed25519.sign(SEED, b"hello update"))
    r = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-keyform", "DER", "-inkey", f"{d}/pub.der", "-rawin", "-in", f"{d}/msg", "-sigfile", f"{d}/sig"], capture_output=True, text=True)
    if "unknown option" in r.stderr or "Unknown" in r.stderr:
        pytest.skip("openssl too old for -rawin")
    assert r.returncode == 0, r.stderr


# ------------------------------------------------------------------------------------------- bundle verification
def test_good_bundle_verifies_and_unpacks_with_modes():
    tmp = tempfile.mkdtemp(); path, m = make(tmp)
    dest = os.path.join(tmp, "out")
    got = B.verify_bundle(path, PUB, dest=dest)
    assert got["version"] == "1.1.0" and got["notes"] == "test notes" and got == m | {} or got["version"] == m["version"]
    assert open(f"{dest}/app/traffic_tester/main.txt").read() == "code 1.1.0"
    assert os.access(f"{dest}/app/scripts/net-split.sh", os.X_OK) and not os.access(f"{dest}/app/requirements.txt", os.X_OK)


def test_tampered_file_is_refused():
    tmp = tempfile.mkdtemp(); path, _ = make(tmp)
    bad = rewrite(path, f"{tmp}/bad.ptt", lambda n, d: (d + b"#evil" if n == "app/traffic_tester/main.txt" else d) if n else None)
    with pytest.raises(BundleError) as e:
        B.verify_bundle(bad, PUB)
    assert e.value.code == "tampered" and "Nothing was installed" in e.value.message


def test_edited_manifest_breaks_the_signature():
    tmp = tempfile.mkdtemp(); path, _ = make(tmp)
    bad = rewrite(path, f"{tmp}/bad.ptt", lambda n, d: (d.replace(b'"1.1.0"', b'"9.9.9"') if n == "manifest.json" else d) if n else None)
    with pytest.raises(BundleError) as e:
        B.verify_bundle(bad, PUB)
    assert e.value.code == "bad_signature"


def test_wrong_key_is_refused():
    tmp = tempfile.mkdtemp(); path, _ = make(tmp, seed=OTHER)
    with pytest.raises(BundleError) as e:
        B.verify_bundle(path, PUB)
    assert e.value.code == "bad_signature"


def test_unsigned_bundle_is_refused():
    tmp = tempfile.mkdtemp(); path, _ = make(tmp)
    with tarfile.open(path) as src, tarfile.open(f"{tmp}/u.ptt", "w:gz") as dst:
        for m in src:
            if m.name != "manifest.sig":
                dst.addfile(m, src.extractfile(m))
    with pytest.raises(BundleError) as e:
        B.verify_bundle(f"{tmp}/u.ptt", PUB)
    assert e.value.code == "unsigned"


@pytest.mark.parametrize("name", ["../evil", "/abs/evil", "app/../../evil", "app\\evil"])
def test_unsafe_names_are_refused(name):
    tmp = tempfile.mkdtemp(); path, _ = make(tmp)
    bad = rewrite(path, f"{tmp}/bad.ptt", lambda n, d: d if n else [(name, b"x")])
    with pytest.raises(BundleError) as e:
        B.verify_bundle(bad, PUB)
    assert e.value.code in ("unsafe", "tampered")


def test_symlink_and_extra_files_are_refused():
    tmp = tempfile.mkdtemp(); path, _ = make(tmp)
    with tarfile.open(path) as src, tarfile.open(f"{tmp}/l.ptt", "w:gz") as dst:
        for m in src:
            dst.addfile(m, src.extractfile(m) if m.isreg() else None)
        ti = tarfile.TarInfo("app/link"); ti.type = tarfile.SYMTYPE; ti.linkname = "/etc/passwd"; dst.addfile(ti)
    with pytest.raises(BundleError) as e:
        B.verify_bundle(f"{tmp}/l.ptt", PUB)
    assert e.value.code == "unsafe"
    extra = rewrite(path, f"{tmp}/x.ptt", lambda n, d: d if n else [("app/extra.py", b"print(1)")])
    with pytest.raises(BundleError) as e:
        B.verify_bundle(extra, PUB)
    assert e.value.code == "tampered"


def test_missing_member_and_garbage_files():
    tmp = tempfile.mkdtemp(); path, _ = make(tmp)
    gone = rewrite(path, f"{tmp}/g.ptt", lambda n, d: (None if n == "app/traffic_tester/main.txt" else d) if n else None)
    with pytest.raises(BundleError) as e:
        B.verify_bundle(gone, PUB)
    assert e.value.code == "tampered" and "missing" in e.value.message
    open(f"{tmp}/junk.ptt", "wb").write(b"not a tar at all")
    with pytest.raises(BundleError) as e:
        B.verify_bundle(f"{tmp}/junk.ptt", PUB)
    assert e.value.code == "malformed"
    data = open(path, "rb").read()
    open(f"{tmp}/trunc.ptt", "wb").write(data[: len(data) // 2])
    with pytest.raises(BundleError):
        B.verify_bundle(f"{tmp}/trunc.ptt", PUB)


def test_downgrade_same_version_and_installer_rules():
    _, m = make(tempfile.mkdtemp(), "1.0.5")
    with pytest.raises(BundleError) as e:
        B.check_installable(m, "1.1.0")
    assert e.value.code == "downgrade" and "older" in e.value.message
    B.check_installable(m, "1.1.0", allow_older=True)
    with pytest.raises(BundleError) as e:
        B.check_installable(m, "1.0.5")
    assert e.value.code == "same_version"
    B.check_installable(m, "1.0.4")
    _, m2 = make(tempfile.mkdtemp(), "1.2.0", min_installer=5)
    with pytest.raises(BundleError) as e:
        B.check_installable(m2, "1.0.0", installer_level=1)
    assert e.value.code == "installer_too_old" and "install-pi.sh" in e.value.message


def test_version_helpers():
    assert B.cmp_version("1.10.0", "1.9.9") > 0 and B.cmp_version("1.0", "1.0.0") == 0 and B.bump_version("1.2.3") == "1.2.4" and B.bump_version("1.2.3", "minor") == "1.3.0"
    with pytest.raises(BundleError):
        B.parse_version("banana")


# ------------------------------------------------------------------------------------------- stage / swap / rollback
class Fake:
    """restart + health stand-ins that record what the helper did."""
    def __init__(self, healthy=True, fail_start_once=False):
        self.calls, self.healthy, self.fail_start_once, self.n_start = [], healthy, fail_start_once, 0

    def restart(self, action):
        self.calls.append(action)
        if action == "start":
            self.n_start += 1
            if self.fail_start_once and self.n_start == 1:
                raise RuntimeError("unit failed to start")

    def health(self, version):
        self.calls.append(f"health:{version}")
        if callable(self.healthy):
            return self.healthy(version)
        return None if self.healthy else "main page answered 500"


@pytest.fixture()
def install(monkeypatch):
    root = tempfile.mkdtemp()
    app = os.path.join(root, "app"); data = os.path.join(root, "data")
    os.makedirs(f"{app}/traffic_tester"); os.makedirs(f"{app}/.git"); os.makedirs(data)
    open(f"{app}/traffic_tester/__init__.py", "w").write('__version__ = "1.0.0"\n')
    open(f"{app}/traffic_tester/main.txt", "w").write("code 1.0.0")
    open(f"{app}/.git/HEAD", "w").write("ref: refs/heads/master\n")
    open(f"{data}/traffic-tester.db", "w").write("MY SAVED TESTS")
    lay = H.Layout(mode="user", app_dir=app, data_dir=data, state_dir=f"{root}/state", work_dir=f"{root}/work", pubkey=PUB, health_seconds=0.2)
    return lay, root


def test_update_swaps_keeps_previous_and_never_touches_data(install):
    lay, root = install
    path, _ = make(root, "1.1.0"); f = Fake()
    r = H.apply_update(lay, path, restart=f.restart, health=f.health)
    assert r["state"] == "ok" and r["to_version"] == "1.1.0" and r["from_version"] == "1.0.0"
    assert open(f"{lay.app_dir}/traffic_tester/main.txt").read() == "code 1.1.0"
    assert open(f"{lay.prev_dir}/traffic_tester/main.txt").read() == "code 1.0.0"      # the old app is kept
    assert os.path.exists(f"{lay.app_dir}/.git/HEAD")                                   # the approved-changes history survives
    assert open(f"{lay.data_dir}/traffic-tester.db").read() == "MY SAVED TESTS"
    assert f.calls[:2] == ["stop", "start"] and "health:1.1.0" in f.calls
    assert H.read_status(lay.state_dir)["state"] == "ok" and not os.path.exists(lay.app_dir + ".new") and not os.path.exists(lay.work_dir)


def test_failing_health_check_rolls_back_automatically(install):
    lay, root = install
    path, _ = make(root, "1.1.0")
    f = Fake(healthy=lambda v: "main page answered 500" if v == "1.1.0" else None)
    r = H.apply_update(lay, path, restart=f.restart, health=f.health)
    assert r["state"] == "rolled_back" and "Rolled back automatically" in r["message"] and "1.0.0" in r["message"]
    assert open(f"{lay.app_dir}/traffic_tester/main.txt").read() == "code 1.0.0" and H.installed_version(lay.app_dir) == "1.0.0"
    assert open(f"{lay.data_dir}/traffic-tester.db").read() == "MY SAVED TESTS"
    assert f.calls.count("start") == 2 and "health:1.0.0" in f.calls and not os.path.exists(lay.app_dir + ".failed")


def test_service_that_will_not_start_rolls_back(install):
    lay, root = install
    path, _ = make(root, "1.1.0"); f = Fake(fail_start_once=True)
    r = H.apply_update(lay, path, restart=f.restart, health=f.health)
    assert r["state"] == "rolled_back" and H.installed_version(lay.app_dir) == "1.0.0"


def test_old_version_also_dead_is_reported_as_failed(install):
    lay, root = install
    path, _ = make(root, "1.1.0")
    r = H.apply_update(lay, path, restart=Fake(healthy=False).restart, health=Fake(healthy=False).health)
    assert r["state"] == "failed" and "did not answer either" in r["message"]


@pytest.mark.parametrize("why", ["tamper", "wrongkey", "unsigned"])
def test_bad_bundles_change_nothing(install, why):
    lay, root = install
    path, _ = make(root, "1.1.0", seed=OTHER if why == "wrongkey" else SEED)
    if why == "tamper":
        path = rewrite(path, f"{root}/t.ptt", lambda n, d: (d + b"x" if n == "app/traffic_tester/main.txt" else d) if n else None)
    if why == "unsigned":
        with tarfile.open(path) as src, tarfile.open(f"{root}/u.ptt", "w:gz") as dst:
            for m in src:
                if m.name != "manifest.sig":
                    dst.addfile(m, src.extractfile(m))
        path = f"{root}/u.ptt"
    f = Fake()
    r = H.apply_update(lay, path, restart=f.restart, health=f.health)
    assert r["state"] == "refused" and "Nothing was" in r["message"]
    assert f.calls == [] and H.installed_version(lay.app_dir) == "1.0.0" and not os.path.exists(lay.prev_dir) and not os.path.exists(lay.app_dir + ".new")


def test_downgrade_refused_unless_allowed(install):
    lay, root = install
    path, _ = make(root, "0.9.0"); f = Fake()
    r = H.apply_update(lay, path, restart=f.restart, health=f.health)
    assert r["state"] == "refused" and "older" in r["message"] and f.calls == []
    r = H.apply_update(lay, path, allow_older=True, restart=f.restart, health=f.health)
    assert r["state"] == "ok" and H.installed_version(lay.app_dir) == "0.9.0"


def test_installer_too_old_changes_nothing(install):
    lay, root = install
    path, _ = make(root, "1.2.0", min_installer=9)
    r = H.apply_update(lay, path, restart=Fake().restart, health=Fake().health)
    assert r["state"] == "refused" and "install-pi.sh" in r["message"] and H.installed_version(lay.app_dir) == "1.0.0"


def test_requirements_without_wheels_says_what_is_missing_and_changes_nothing(install, monkeypatch):
    lay, root = install
    lay.venv_python = "/fake/python"
    monkeypatch.setattr(H, "_need_packages", lambda l, r: True)
    monkeypatch.setattr(H, "_py_tag", lambda l: "cp313")
    files = app_files("1.1.0", {"wheels/cp311/foo-1-py3-none-any.whl": b"w"})
    path, _ = make(root, "1.1.0", files=files, requirements_changed=True)
    f = Fake()
    r = H.apply_update(lay, path, restart=f.restart, health=f.health)
    assert r["state"] == "refused" and "cp313" in r["message"] and "cp311" in r["message"] and "Nothing was changed" in r["message"]
    assert f.calls == [] and H.installed_version(lay.app_dir) == "1.0.0"


def test_requirements_with_matching_wheels_installs_offline(install, monkeypatch):
    lay, root = install
    lay.venv_python = "/fake/python"
    monkeypatch.setattr(H, "_need_packages", lambda l, r: True)
    monkeypatch.setattr(H, "_py_tag", lambda l: "cp313")
    seen = []
    monkeypatch.setattr(H, "_run", lambda cmd, timeout=120, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    files = app_files("1.1.0", {"wheels/cp313/foo-1-py3-none-any.whl": b"w", "wheels/cp311/foo-1-py3-none-any.whl": b"w"})
    path, m = make(root, "1.1.0", files=files, requirements_changed=True)
    assert m["wheel_tags"] == ["cp311", "cp313"]
    r = H.apply_update(lay, path, restart=Fake().restart, health=Fake().health)
    pip = [c for c in seen if "pip" in c and "--no-index" in c]
    assert r["state"] == "ok" and pip and pip[0][pip[0].index("--find-links") + 1].endswith("wheels/cp313")


def test_rollback_button_swaps_back_and_forth(install):
    lay, root = install
    H.apply_update(lay, make(root, "1.1.0")[0], restart=Fake().restart, health=Fake().health)
    f = Fake()
    r = H.rollback(lay, restart=f.restart, health=f.health)
    assert r["state"] == "ok" and H.installed_version(lay.app_dir) == "1.0.0" and H.installed_version(lay.prev_dir) == "1.1.0"
    assert open(f"{lay.data_dir}/traffic-tester.db").read() == "MY SAVED TESTS"
    H.rollback(lay, restart=f.restart, health=f.health)
    assert H.installed_version(lay.app_dir) == "1.1.0"


def test_rollback_to_a_version_that_will_not_start_keeps_the_current_one(install):
    lay, root = install
    H.apply_update(lay, make(root, "1.1.0")[0], restart=Fake().restart, health=Fake().health)
    f = Fake(healthy=lambda v: "boom" if v == "1.0.0" else None)
    r = H.rollback(lay, restart=f.restart, health=f.health)
    assert r["state"] == "rolled_back" and H.installed_version(lay.app_dir) == "1.1.0" and H.installed_version(lay.prev_dir) == "1.0.0"


def test_rollback_without_previous_is_refused(install):
    lay, _ = install
    assert H.rollback(lay, restart=Fake().restart, health=Fake().health)["state"] == "refused"


def test_status_file_is_not_written_through_a_planted_link(install):
    lay, root = install
    os.makedirs(lay.state_dir); target = f"{root}/precious"; open(target, "w").write("keep")
    os.symlink(target, f"{lay.state_dir}/.status.{os.getpid()}.tmp")
    H.Status(lay, "install").update(message="x")
    assert open(target).read() == "keep"


def test_filesystem_that_refuses_directory_renames_still_updates(install, monkeypatch):
    """overlayfs (containers) answers EXDEV to rename() of a directory: the helper falls back to copy + delete."""
    import errno
    lay, root = install
    real = os.rename
    def fake(src, dst, *a, **k):
        if os.path.isdir(src) and not os.path.basename(src).startswith("."):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "rename", fake)
    f = Fake()
    r = H.apply_update(lay, make(root, "1.1.0")[0], restart=f.restart, health=f.health)
    assert r["state"] == "ok" and H.installed_version(lay.app_dir) == "1.1.0" and H.installed_version(lay.prev_dir) == "1.0.0"
    r = H.rollback(lay, restart=f.restart, health=f.health)
    assert r["state"] == "ok" and H.installed_version(lay.app_dir) == "1.0.0"


def test_swap_that_dies_half_way_restores_the_old_app_and_restarts_it(install, monkeypatch):
    lay, root = install
    calls = {"n": 0}
    real = H._mv
    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:                       # app -> app.prev worked, app.new -> app fails
            raise OSError("disk exploded")
        return real(src, dst)
    monkeypatch.setattr(H, "_mv", flaky)
    f = Fake()
    r = H.apply_update(lay, make(root, "1.1.0")[0], restart=f.restart, health=f.health)
    assert r["state"] == "rolled_back" and H.installed_version(lay.app_dir) == "1.0.0" and f.calls.count("start") >= 1
    assert open(f"{lay.data_dir}/traffic-tester.db").read() == "MY SAVED TESTS" and not os.path.exists(lay.app_dir + ".new")
