import tempfile
import pytest
from fastapi.testclient import TestClient
from traffic_tester.server import create_app


@pytest.fixture()
def client():
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        yield c


def test_status_and_static_ui(client):
    s = client.get("/api/status").json()
    assert "role" not in s and s["pi"]["name"] and s["pi"]["id"] and s["version"] and s["run"]["running"] is False
    html = client.get("/").text
    assert "Traffic Tester" in html and "vendor/uPlot.min.js" in html and "http" not in html.split("<body>")[1].split("src=")[1][:5]   # no CDN


def test_no_cdn_references_in_frontend():
    import glob, re
    for f in glob.glob("traffic_tester/static/*.html") + glob.glob("traffic_tester/static/*.js") + glob.glob("traffic_tester/static/*.css"):
        txt = open(f).read()
        assert not re.search(r"https?://(?!www\.w3\.org)", txt), f


def test_builtin_tests_editable_not_deletable_over_http(client):
    ps = client.get("/api/profiles").json()
    b = [p for p in ps if p["builtin"] and p["kind"] == "trip"][0]
    assert client.put(f"/api/profiles/{b['id']}", json=b).status_code == 200              # an edit (here: none) of a built-in trip test is allowed (SPEC 6.5)
    g = [p for p in ps if p["builtin"] and p["kind"] == "generator"][0]
    assert client.put(f"/api/profiles/{g['id']}", json=g).status_code == 403              # generator / sniffer presets stay read-only
    assert client.delete(f"/api/profiles/{b['id']}").status_code == 403                    # built-in tests are hidden, never deleted
    d = client.post(f"/api/profiles/{b['id']}/duplicate").json()
    assert client.put(f"/api/profiles/{d['id']}", json={**d, "threshold_ms": 40}).status_code == 200


def test_import_validation_message(client):
    r = client.post("/api/profiles/import", json={"format": "other"})
    assert r.status_code == 422 and "not a Pi Automation Traffic Tester profile" in r.json()["detail"]


def test_run_without_peer_address_is_422(client):
    r = client.post("/api/runs", json={"profile": {"name": "x", "transport": "udp"}})
    assert r.status_code == 422 and "IP address" in r.json()["detail"]


def test_password_protection(client):
    assert client.post("/api/auth/set", json={"password": "ab"}).status_code == 422
    assert client.post("/api/auth/set", json={"password": "secret1"}).json()["enabled"]
    assert client.get("/api/profiles").status_code == 401                    # locked
    assert client.get("/api/status").json()["login_required"] is True        # status stays open for the login page
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "secret1"}).status_code == 200
    assert client.get("/api/profiles").status_code == 200                    # cookie set
    assert client.post("/api/auth/set", json={"password": ""}).json()["enabled"] is False
    assert client.get("/api/profiles").status_code == 200


def test_sign_out_clears_the_cookie_and_the_token(client):
    assert client.post("/api/auth/logout").status_code == 200                # nothing to sign out of: harmless
    client.post("/api/auth/set", json={"password": "secret1"})
    assert client.post("/api/auth/login", json={"password": "secret1"}).status_code == 200
    assert client.get("/api/profiles").status_code == 200
    tok = client.cookies.get("tt_token")
    r = client.post("/api/auth/logout")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "tt_token" in r.headers.get("set-cookie", "")                     # the cookie is cleared in the browser ...
    client.cookies.set("tt_token", tok)                                      # ... and the old token no longer works even if a client keeps it
    assert client.get("/api/profiles").status_code == 401
    assert client.get("/api/status").json()["password_enabled"] is True      # the password is still set


def test_session_clear_and_new(client):
    s0 = client.get("/api/status").json()["session"]["id"]
    s1 = client.post("/api/sessions", json={"name": "Site 9"}).json()
    assert s1["id"] != s0 and client.get("/api/status").json()["session"]["id"] == s1["id"]
    assert client.delete(f"/api/sessions/{s1['id']}/runs").status_code == 200


def test_websocket_connects(client):
    with client.websocket_connect("/ws/live") as ws:
        assert "snapshot" in ws.receive_json()
