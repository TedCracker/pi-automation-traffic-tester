"""Software update API: upload, online check (a local bare git repo stands in for GitHub), install request, password gate, rollback request."""
import json
import os
import shutil
import subprocess
import tempfile

import pytest
from fastapi.testclient import TestClient

from traffic_tester.server import create_app
from traffic_tester.update import bundle as B, ed25519, manager as M
from tests.test_update import PUB, SEED, OTHER, app_files, make, rewrite

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def build_app(monkeypatch, mode="user", repo=None, version="1.0.0", role="A", key=True):
    root = tempfile.mkdtemp()
    appdir = os.path.join(root, "app"); os.makedirs(f"{appdir}/traffic_tester")
    open(f"{appdir}/traffic_tester/__init__.py", "w").write(f'__version__ = "{version}"\n')
    data = os.path.join(root, "data")
    monkeypatch.setenv("TT_APP_DIR", appdir); monkeypatch.setenv("TT_UPDATE_MODE", mode); monkeypatch.setenv("TT_UPDATE_PUBKEY", PUB.hex())
    monkeypatch.setenv("TT_UPDATE_STATE_DIR", os.path.join(root, "state")); monkeypatch.setenv("TT_UPDATE_SYSTEM_CONF", "1")
    if repo:
        monkeypatch.setenv("TT_UPDATE_REPO", repo)
    app = create_app(data, role)
    app.state.tt.updates.version = version
    started = []
    monkeypatch.setattr(M.UpdateManager, "_start", lambda self, req: started.append(req))
    return app, root, started


def bare_repo(version="1.1.0", seed=SEED, branch="stable", **kw):
    root = tempfile.mkdtemp()
    bare, work = f"{root}/releases.git", f"{root}/w"
    subprocess.run(["git", "init", "-q", "--bare", "-b", branch, bare], check=True)
    os.makedirs(work)
    path, m = make(work, version, seed=seed, **kw)
    json.dump({"version": version, "file": os.path.basename(path)}, open(f"{work}/latest.json", "w"))
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=work, check=True)
    subprocess.run(g + ["add", "-A"], cwd=work, check=True); subprocess.run(g + ["commit", "-q", "-m", "r"], cwd=work, check=True)
    subprocess.run(["git", "push", "-q", bare, branch], cwd=work, check=True)
    return bare, work, path


def test_status_reports_version_and_what_works(monkeypatch):
    app, _, _ = build_app(monkeypatch)
    with TestClient(app) as c:
        j = c.get("/api/update/status").json()
        assert j["current"]["version"] == "1.0.0" and j["current"]["date"] and j["can_install"] is True and j["mode"] == "user"
        assert j["online"]["can_check"] is False and "access key" in j["online"]["reason"]          # no deploy key installed here


def test_not_an_installed_copy_cannot_update(monkeypatch):
    app, _, _ = build_app(monkeypatch, mode="off")
    with TestClient(app) as c:
        j = c.get("/api/update/status").json()
        assert j["can_install"] is False and "install-pi.sh" in j["reason"]
        assert c.post("/api/update/install", json={}).status_code == 409


def test_upload_install_roundtrip_in_the_api(monkeypatch):
    app, root, started = build_app(monkeypatch)
    path, _ = make(tempfile.mkdtemp(), "1.1.0")
    with TestClient(app) as c:
        r = c.post("/api/update/upload", content=open(path, "rb").read(), headers={"content-type": "application/octet-stream"})
        assert r.status_code == 200 and r.json()["version"] == "1.1.0" and r.json()["newer"] is True and r.json()["notes"] == "test notes"
        assert c.get("/api/update/status").json()["staged"]["version"] == "1.1.0"
        r = c.post("/api/update/install", json={"source": "upload"})
        assert r.status_code == 202 and r.json()["to_version"] == "1.1.0"
        assert started == [{"action": "apply", "allow_older": False, "to": "1.1.0"}]


def test_upload_of_bad_files_is_refused_with_a_reason(monkeypatch):
    app, root, _ = build_app(monkeypatch)
    good, _ = make(tempfile.mkdtemp(), "1.1.0")
    wrong, _ = make(tempfile.mkdtemp(), "1.1.0", seed=OTHER)
    bad = rewrite(good, good + ".bad", lambda n, d: (b"X" + d[1:] if n == "app/traffic_tester/main.txt" else d) if n else None)
    with TestClient(app) as c:
        for p, word in ((wrong, "not signed by the release key"), (bad, "checksum"), (__file__, "not an update bundle")):
            r = c.post("/api/update/upload", content=open(p, "rb").read())
            assert r.status_code == 422 and word in r.json()["detail"], (p, r.json())
        assert c.post("/api/update/upload", content=b"").status_code == 422
        assert c.get("/api/update/status").json()["staged"] is None
        assert c.post("/api/update/install", json={"source": "upload"}).status_code == 409


def test_downgrade_needs_the_tick(monkeypatch):
    app, root, started = build_app(monkeypatch, version="1.1.0")
    path, _ = make(tempfile.mkdtemp(), "1.0.0")
    with TestClient(app) as c:
        j = c.post("/api/update/upload", content=open(path, "rb").read()).json()
        assert j["older"] is True
        r = c.post("/api/update/install", json={"source": "upload"})
        assert r.status_code == 422 and "older" in r.json()["detail"] and started == []
        assert c.post("/api/update/install", json={"source": "upload", "allow_older": True}).status_code == 202 and started[0]["allow_older"] is True


def test_no_install_while_a_test_runs(monkeypatch):
    app, root, started = build_app(monkeypatch)
    path, _ = make(tempfile.mkdtemp(), "1.1.0")
    with TestClient(app) as c:
        c.post("/api/update/upload", content=open(path, "rb").read())
        app.state.tt.updates.busy = lambda: True
        r = c.post("/api/update/install", json={"source": "upload"})
        assert r.status_code == 409 and "test is running" in r.json()["detail"] and started == []


def test_password_gate(monkeypatch):
    app, root, started = build_app(monkeypatch)
    path, _ = make(tempfile.mkdtemp(), "1.1.0")
    with TestClient(app) as c:
        assert c.post("/api/auth/set", json={"password": "secret1"}).status_code == 200
        for method, url, kw in (("get", "/api/update/status", {}), ("post", "/api/update/check", {}), ("post", "/api/update/upload", {"content": open(path, "rb").read()}),
                                ("post", "/api/update/install", {"json": {}}), ("post", "/api/update/rollback", {}), ("post", "/api/update/settings", {"json": {"daily_check": True}}),
                                ("post", "/api/update/partner", {"json": {"host": "10.0.0.2"}}), ("post", "/api/update/discard", {})):
            r = getattr(c, method)(url, **kw)
            assert r.status_code == 401, (url, r.status_code)
        assert started == []
        assert c.post("/api/auth/login", json={"password": "secret1"}).status_code == 200
        assert c.get("/api/update/status").status_code == 200
        assert c.post("/api/update/upload", content=open(path, "rb").read()).status_code == 200


def test_online_check_stage_and_install_from_a_local_repo(monkeypatch):
    bare, _, _ = bare_repo("1.2.0")
    app, root, started = build_app(monkeypatch, repo=bare)
    with TestClient(app) as c:
        assert c.get("/api/update/status").json()["online"]["can_check"] is True
        r = c.post("/api/update/check")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["version"] == "1.2.0" and j["newer"] is True and j["signature"] == "ok" and j["notes"] == "test notes"
        assert c.get("/api/update/status").json()["online"]["last"]["version"] == "1.2.0"
        r = c.post("/api/update/install", json={"source": "online"})
        assert r.status_code == 202 and started[0]["to"] == "1.2.0"
        assert os.path.isfile(app.state.tt.updates.staged)


def test_online_release_signed_by_someone_else_is_refused(monkeypatch):
    bare, _, _ = bare_repo("1.2.0", seed=OTHER)
    app, root, started = build_app(monkeypatch, repo=bare)
    with TestClient(app) as c:
        r = c.post("/api/update/check")
        assert r.status_code == 422 and "not signed by the release key" in r.json()["detail"]
        assert c.post("/api/update/install", json={"source": "online"}).status_code == 409 and started == []


def test_online_channel_unreachable_gives_a_plain_message(monkeypatch):
    app, root, _ = build_app(monkeypatch, repo=os.path.join(tempfile.mkdtemp(), "nope.git"))
    with TestClient(app) as c:
        r = c.post("/api/update/check")
        assert r.status_code == 502 and r.json()["detail"]


def test_online_is_greyed_out_without_internet_or_key(monkeypatch):
    app, _, _ = build_app(monkeypatch)
    app.state.tt.updates.key_path = lambda: "/some/key"
    monkeypatch.setenv("TT_UPDATE_FAKE_ONLINE", "0")
    with TestClient(app) as c:
        o = c.get("/api/update/status").json()["online"]
        assert o["can_check"] is False and "No internet" in o["reason"]
        assert c.post("/api/update/check").status_code == 409


def test_rollback_request_and_daily_toggle(monkeypatch):
    app, root, started = build_app(monkeypatch)
    with TestClient(app) as c:
        assert c.post("/api/update/rollback").status_code == 409           # nothing to go back to
        os.makedirs(f"{root}/app.prev/traffic_tester")
        open(f"{root}/app.prev/traffic_tester/__init__.py", "w").write('__version__ = "0.9.0"\n')
        assert c.get("/api/update/status").json()["current"]["previous_version"] == "0.9.0"
        assert c.post("/api/update/rollback").status_code == 202 and started == [{"action": "rollback"}]
        assert c.post("/api/update/settings", json={"daily_check": True}).json()["daily_check"] is True
        assert c.get("/api/update/status").json()["daily_check"] is True


def test_daily_check_only_notes_never_installs(monkeypatch):
    bare, _, _ = bare_repo("1.3.0")
    app, root, started = build_app(monkeypatch, repo=bare)
    with TestClient(app) as c:
        m = app.state.tt.updates
        assert m.daily_due() is False
        c.post("/api/update/settings", json={"daily_check": True})
        assert m.daily_due() is True
        m.check_online()
        assert m.daily_due() is False and started == [] and m.last_online()["version"] == "1.3.0"


def test_partner_push_validates_input(monkeypatch):
    app, root, _ = build_app(monkeypatch)
    with TestClient(app) as c:
        assert c.post("/api/update/partner", json={"host": ""}).status_code == 422
        assert c.post("/api/update/partner", json={"host": "a/b"}).status_code == 422
        assert c.post("/api/update/partner", json={"host": "10.0.0.9"}).status_code == 409      # nothing staged yet
