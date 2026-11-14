"""'Create custom test' (Setup): the form's ranges come from the schema, and a custom test is an ordinary saved profile
(save / edit / delete / export / import all through the profiles API, schema_version unchanged)."""
import json
import tempfile

import pytest
from fastapi.testclient import TestClient

from traffic_tester import profiles as P
from traffic_tester.server import create_app


@pytest.fixture()
def client():
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        yield c


def custom(**kw):
    d = {"name": "Site 4 trip", "description": "my own", "transport": "udp", "frame": "extended", "interval_ms": 50, "duration_s": 600,
         "threshold_ms": 60, "rule": {"percentile": 99, "max_loss_pct": 0.1}, "net": {"peer_host": "10.0.0.2", "port": 20500, "dscp": 46}}
    d.update(kw)
    return d


def test_schema_version_unchanged():
    assert P.SCHEMA_VERSION == 1 and P.form_limits()["schema_version"] == 1


def test_limits_endpoint_serves_the_schema_ranges(client):
    r = client.get("/api/profile-limits").json()
    f = r["fields"]
    assert f["interval_ms"] == {"ge": 1, "le": 60000} and f["duration_s"]["ge"] == 1 and f["rule.max_loss_pct"] == {"ge": 0, "le": 100}
    assert f["net.port"] == {"ge": 1, "le": 65535} and f["serial.baud"] == {"ge": P.BAUD_MIN, "le": P.BAUD_MAX} and f["goose.vlan_prio"] == {"ge": 0, "le": 7}
    assert f["idle_gaps_s"]["max_count"] == P.IDLE_GAPS_MAX_COUNT and f["name"]["max_length"] == 80
    d = r["defaults"]                       # a brand-new custom test is valid as it stands
    P.validate_profile(d)
    assert d["transport"] == "udp" and d["threshold_ms"] == 60 and d["rule"]["percentile"] == 99


def test_every_limit_is_enforced_by_the_server(client):
    """Each range the form shows is a real server rule: just inside is accepted, just outside is rejected."""
    f = P.form_limits()["fields"]
    cases = {"interval_ms": lambda v: custom(interval_ms=v), "duration_s": lambda v: custom(duration_s=v),
             "rule.max_loss_pct": lambda v: custom(rule={"max_loss_pct": v}), "net.port": lambda v: custom(net={"port": v}),
             "net.dscp": lambda v: custom(net={"dscp": v}), "idle_repeats": lambda v: custom(idle_repeats=v), "load_mbps": lambda v: custom(load_mbps=v),
             "serial.baud": lambda v: custom(transport="serial", serial={"baud": v}), "goose.vlan_prio": lambda v: custom(transport="goose_l2", goose={"vlan_prio": v}),
             "goose.vlan_id": lambda v: custom(transport="goose_l2", goose={"vlan_id": v}), "goose.ttl": lambda v: custom(transport="goose_l3", goose={"ttl": v}),
             "net.keepalive_idle_s": lambda v: custom(transport="tcp", net={"keepalive_idle_s": v})}
    for key, mk in cases.items():
        b = f[key]
        lo, hi = b.get("ge"), b.get("le")
        for v, ok in [(lo, True), (hi, True), (lo - 1, False), (hi + 1, False)]:
            assert (client.post("/api/profiles", json=mk(v) | {"name": f"{key}-{v}"}).status_code == 200) == ok, (key, v, ok)
    assert P.form_limits()["fields"]["threshold_ms"] == {"gt": 0}
    assert client.post("/api/profiles", json=custom(name="t0", threshold_ms=0)).status_code == 422


def test_name_rules(client):
    assert client.post("/api/profiles", json=custom(name="")).status_code == 422
    assert client.post("/api/profiles", json=custom(name="x" * 81)).status_code == 422
    assert client.post("/api/profiles", json=custom(name="ok")).status_code == 200
    assert client.post("/api/profiles", json=custom(name="ok")).status_code in (409, 422)      # duplicate name


def test_timeout_must_exceed_threshold(client):
    assert client.post("/api/profiles", json=custom(threshold_ms=100, timeout_ms=100)).status_code == 422


def test_save_edit_delete(client):
    r = client.post("/api/profiles", json=custom()).json()
    pid = r["id"]
    assert not r["builtin"] and r["transport"] == "udp" and r["name"] == "Site 4 trip"
    assert any(p["id"] == pid for p in client.get("/api/profiles").json())
    e = client.put(f"/api/profiles/{pid}", json={**{k: v for k, v in r.items() if k != "id"}, "name": "Site 4 trip v2", "threshold_ms": 40, "interval_ms": 20, "impairment": "Bad cell", "load_mbps": 5}).json()
    assert e["id"] == pid and e["name"] == "Site 4 trip v2" and e["threshold_ms"] == 40 and e["impairment"] == "Bad cell" and e["load_mbps"] == 5
    assert client.put(f"/api/profiles/{pid}", json={**e, "threshold_ms": -1}).status_code == 422
    assert client.get(f"/api/profiles/{pid}").json()["threshold_ms"] == 40          # a rejected edit changes nothing
    d = client.post(f"/api/profiles/{pid}/duplicate").json()
    assert d["name"] == "Site 4 trip v2 (copy)" and d["id"] != pid and not d["builtin"]
    assert client.delete(f"/api/profiles/{pid}").status_code == 200
    assert client.get(f"/api/profiles/{pid}").status_code == 404
    assert any(p["id"] == d["id"] for p in client.get("/api/profiles").json())


def test_all_five_link_types_can_be_saved(client):
    for t in ("serial", "udp", "tcp", "goose_l2", "goose_l3"):
        r = client.post("/api/profiles", json=custom(name=f"mine {t}", transport=t, frame="compact" if t == "serial" else "extended"))
        assert r.status_code == 200 and r.json()["transport"] == t


def test_export_import_round_trip_of_a_custom_test(client):
    made = client.post("/api/profiles", json=custom(name="Round trip me", transport="tcp", interval_ms=20, duration_s=90, idle_gaps_s=[5, 30], idle_repeats=2,
                                                      one_way=True, impairment="Handover", load_mbps=2.5, net={"peer_host": "10.1.2.3", "port": 21000, "dscp": 34, "keepalive": False, "keepalive_idle_s": 9},
                                                      rule={"percentile": 95, "max_loss_pct": 0.5})).json()
    doc = json.loads(client.get(f"/api/profiles/export?ids={made['id']}").text)
    assert doc["format"] == "traffic-tester-profiles" and doc["schema_version"] == 1 and len(doc["profiles"]) == 1
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as other:           # another Pi
        got = other.post("/api/profiles/import", json=doc).json()["imported"]
        assert len(got) == 1 and not got[0]["builtin"]
        a = {k: v for k, v in made.items() if k not in ("id", "builtin")}
        b = {k: v for k, v in got[0].items() if k not in ("id", "builtin")}
        assert a == b                                                    # every field survives
    again = client.post("/api/profiles/import", json=doc).json()["imported"]   # same Pi: name clash -> "(imported)"
    assert again[0]["name"].startswith("Round trip me (imported)")


def test_old_exports_still_import(client):
    """An export written before this feature (no load_mbps / impair_iface keys, older field set) still imports."""
    old = {"format": "traffic-tester-profiles", "schema_version": 1, "exported_at": "2026-11-03T00:00:00+00:00", "app_version": "1.0.0",
           "profiles": [{"name": "Old style", "transport": "udp", "frame": "extended", "interval_ms": 50, "duration_s": 60, "timeout_ms": 1000,
                         "threshold_ms": 60, "idle_gaps_s": [], "rule": {"percentile": 99, "max_loss_pct": 0.1}}]}
    r = client.post("/api/profiles/import", json=old)
    assert r.status_code == 200 and r.json()["imported"][0]["name"] == "Old style"
