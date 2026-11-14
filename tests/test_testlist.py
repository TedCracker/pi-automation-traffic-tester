"""The Setup list is one list the user controls: edit / rename / hide / reorder / create / delete (SPEC §6.5)."""
import copy, json, tempfile

import pytest
from fastapi.testclient import TestClient

from traffic_tester import profiles as P
from traffic_tester.server import create_app
from traffic_tester.store import BuiltinError, Store


@pytest.fixture
def d():
    return tempfile.mkdtemp()


def trip(store):
    return [p for p in store.list_profiles() if p.get("kind", "trip") == "trip"]


def by_name(store, name):
    return next(p for p in store.list_profiles() if p["name"] == name)


def edit_body(p, **kw):
    q = {k: v for k, v in p.items() if k not in ("id", "position", "hidden", "edited", "preset_key", "default_name")}
    q.update(kw)
    return q


def test_builtin_edit_is_an_override_and_reset_restores_everything(d):
    s = Store(d)
    p = by_name(s, "DER trip test")
    assert p["builtin"] and not p["edited"] and p["preset_key"] == "der-trip-test" and p["default_name"] == "DER trip test"
    e = s.update_profile(p["id"], edit_body(p, name="Feeder 12 trip", interval_ms=25, serial={**p["serial"], "baud": 9600}))
    assert e["edited"] and e["builtin"] and e["name"] == "Feeder 12 trip" and e["interval_ms"] == 25 and e["serial"]["baud"] == 9600
    assert e["default_name"] == "DER trip test"
    row = s.db.execute("SELECT override_json, json FROM profiles WHERE id=?", (p["id"],)).fetchone()
    assert json.loads(row["override_json"]) == {"name": "Feeder 12 trip", "interval_ms": 25, "serial": {"baud": 9600}}     # only what changed
    r = s.reset_profile(p["id"])
    assert r["name"] == "DER trip test" and r["interval_ms"] == 50 and r["serial"]["baud"] == 19200 and not r["edited"]
    assert s.db.execute("SELECT override_json FROM profiles WHERE id=?", (p["id"],)).fetchone()["override_json"] is None


def test_saving_the_defaults_again_is_not_an_edit(d):
    s = Store(d)
    p = by_name(s, "Quick link check")
    assert not s.update_profile(p["id"], edit_body(p))["edited"]


def test_edits_survive_restart_and_an_app_update_and_reset_still_works(d, monkeypatch):
    s = Store(d)
    p = by_name(s, "DER trip test")
    s.update_profile(p["id"], edit_body(p, name="My DER", threshold_ms=45, description="mine"))
    s.set_hidden(by_name(s, "Quick link check")["id"], True)
    s.db.close()
    s = Store(d)                                                       # restart
    m = by_name(s, "My DER")
    assert m["threshold_ms"] == 45 and m["edited"] and m["description"] == "mine" and by_name(s, "Quick link check")["hidden"]
    # an app update: the default changes (interval 50 -> 40, new description) and a new built-in appears
    real = P.builtin_presets

    def newer():
        out = copy.deepcopy(real())
        for x in out:
            if x["name"] == "DER trip test":
                x["interval_ms"] = 40
                x["description"] = "shipped text v2"
        extra = copy.deepcopy(out[0])
        extra["name"] = "Brand new built-in"
        return out[:1] + [extra] + out[1:]
    monkeypatch.setattr(P, "builtin_presets", newer)
    s.db.close()
    s = Store(d)
    m = by_name(s, "My DER")
    assert m["interval_ms"] == 40                                      # untouched field follows the new default
    assert m["threshold_ms"] == 45 and m["name"] == "My DER" and m["description"] == "mine"     # his edits are kept
    names = [p["name"] for p in trip(s)]
    assert names.index("Brand new built-in") == names.index("My DER") + 1          # new built-in lands after its predecessor in the default order
    r = s.reset_profile(m["id"])
    assert r["name"] == "DER trip test" and r["interval_ms"] == 40 and r["description"] == "shipped text v2"
    assert s.db.execute("SELECT COUNT(*) AS n FROM profiles WHERE name='DER trip test'").fetchone()["n"] == 1      # never a duplicate row after a rename + restart


def test_an_override_that_no_longer_validates_is_kept_but_not_applied(d):
    s = Store(d)
    p = by_name(s, "Quick link check")
    s.db.execute("UPDATE profiles SET override_json=? WHERE id=?", (json.dumps({"interval_ms": -5}), p["id"]))
    s.db.close()
    s = Store(d)
    q = s.get_profile(p["id"])
    assert q["interval_ms"] == 20 and q["edited"] and "override_error" in q


def test_names_are_unique_and_a_rename_is_checked(d):
    s = Store(d)
    a, b = by_name(s, "DER trip test"), by_name(s, "Quick link check")
    with pytest.raises(ValueError, match="already exists"):
        s.update_profile(a["id"], edit_body(a, name="Quick link check"))
    mine = s.create_profile({"name": "Mine", "transport": "udp"})
    with pytest.raises(ValueError, match="already exists"):
        s.update_profile(mine["id"], edit_body(mine, name="DER trip test"))
    s.update_profile(a["id"], edit_body(a, name="Renamed"))
    # the old default name is free for another test, but then Reset must say why it cannot go back
    taken = s.update_profile(mine["id"], edit_body(mine, name="DER trip test"))
    assert taken["name"] == "DER trip test"
    with pytest.raises(ValueError, match="Rename that one first"):
        s.reset_profile(a["id"])
    s.update_profile(mine["id"], edit_body(mine, name="Mine again"))
    assert s.reset_profile(a["id"])["name"] == "DER trip test"
    assert b["id"] != a["id"]


def test_a_user_test_with_the_name_of_a_new_built_in_is_renamed_not_lost(d, monkeypatch):
    s = Store(d)
    s.create_profile({"name": "Brand new built-in", "transport": "udp", "interval_ms": 7})
    real = P.builtin_presets

    def newer():
        out = copy.deepcopy(real())
        extra = copy.deepcopy(out[0])
        extra["name"] = "Brand new built-in"
        return out + [extra]
    monkeypatch.setattr(P, "builtin_presets", newer)
    s.db.close()
    s = Store(d)
    assert by_name(s, "Brand new built-in")["builtin"]
    mine = by_name(s, "Brand new built-in (mine)")
    assert not mine["builtin"] and mine["interval_ms"] == 7


def test_hide_restore_and_builtins_cannot_be_deleted(d):
    s = Store(d)
    p = by_name(s, "TCP session test")
    with pytest.raises(BuiltinError):
        s.delete_profile(p["id"])
    assert s.set_hidden(p["id"], True)["hidden"]
    assert [x["name"] for x in s.list_profiles(include_hidden=False) if x["id"] == p["id"]] == []
    s.db.close()
    s = Store(d)
    assert by_name(s, "TCP session test")["hidden"]
    assert s.restore_hidden() == 1 and not by_name(s, "TCP session test")["hidden"]
    assert s.restore_hidden() == 0


def test_generator_and_sniffer_presets_stay_read_only(d):
    s = Store(d)
    g = next(p for p in s.list_profiles() if p.get("kind") == "generator")
    with pytest.raises(BuiltinError):
        s.update_profile(g["id"], edit_body(g, name="x"))


def test_own_test_delete_returns_it_and_undo_puts_it_back_in_place(d):
    s = Store(d)
    a = s.create_profile({"name": "A", "transport": "udp"})
    b = s.create_profile({"name": "B", "transport": "udp", "interval_ms": 33})
    c = s.create_profile({"name": "C", "transport": "udp"})
    gone = s.delete_profile(b["id"])
    assert gone["name"] == "B" and by_name(s, "C")
    back = s.undelete_profile(gone)
    names = [p["name"] for p in s.list_profiles()]
    assert back["interval_ms"] == 33 and names.index("A") < names.index("B") < names.index("C")
    assert a["id"] and c["id"]
    g2 = s.delete_profile(back["id"])
    s.create_profile({"name": "B", "transport": "udp"})                 # the name was taken meanwhile
    again = s.undelete_profile(g2)
    assert again["name"] == "B (2)" and again["interval_ms"] == 33        # comes back under a free name, nothing is overwritten


def test_order_persists_across_restart_and_keeps_other_kinds_in_place(d):
    s = Store(d)
    t = trip(s)
    ids = [p["id"] for p in t]
    new = list(reversed(ids))
    gens_before = [p["id"] for p in s.list_profiles() if p.get("kind") != "trip"]
    s.set_order(new)
    assert [p["id"] for p in trip(s)] == new
    s.db.close()
    s = Store(d)
    assert [p["id"] for p in trip(s)] == new
    assert [p["id"] for p in s.list_profiles() if p.get("kind") != "trip"] == gens_before
    mine = s.create_profile({"name": "Last one", "transport": "udp"})
    assert s.list_profiles()[-1]["id"] == mine["id"]                      # new tests go to the end of the list


def test_history_keeps_the_name_used_at_run_time(d):
    s = Store(d)
    p = by_name(s, "DER trip test")
    rid = s.create_run(s.current_session()["id"], {**p, "name": p["name"]}, "A", "port")
    s.update_profile(p["id"], edit_body(p, name="Renamed later"))
    assert json.loads(s.db.execute("SELECT profile_json FROM runs WHERE id=?", (rid,)).fetchone()["profile_json"])["name"] == "DER trip test"


def test_export_import_carries_overrides_hidden_and_order_and_old_files_still_import(d):
    s = Store(d)
    p = by_name(s, "DER trip test")
    s.update_profile(p["id"], edit_body(p, name="Feeder 12", interval_ms=25))
    s.set_hidden(by_name(s, "Overnight soak")["id"], True)
    s.create_profile({"name": "Mine", "transport": "udp"})
    t = trip(s)
    s.set_order([x["id"] for x in reversed(t)])
    doc = json.loads(json.dumps(s.export_profiles()))
    assert doc["test_state"]["hidden"] == ["overnight-soak"] and doc["test_state"]["order"][0] == "Mine"
    s2 = Store(tempfile.mkdtemp())
    out = s2.import_profiles(doc)
    assert [o["name"] for o in out] == ["Mine"]                                   # built-in tests are NOT imported as copies
    f = by_name(s2, "Feeder 12")
    assert f["builtin"] and f["edited"] and f["interval_ms"] == 25 and f["preset_key"] == "der-trip-test"
    assert by_name(s2, "Overnight soak")["hidden"]
    assert [x["name"] for x in trip(s2)] == [x["name"] for x in trip(s)]
    # a 1.0/1.1-style export (no preset_key, no test_state) still imports: built-ins are skipped, own tests are added
    old = {"format": P.FORMAT, "schema_version": 1, "app_version": "1.1.1",
           "profiles": [{**P.builtin_presets()[0]}, {**P.validate_profile({"name": "Old own", "transport": "udp"}), "builtin": False}]}
    s3 = Store(tempfile.mkdtemp())
    assert [o["name"] for o in s3.import_profiles(old)] == ["Old own"]
    assert len([x for x in s3.list_profiles() if x["name"].startswith("DER trip test")]) == 2    # DER trip test + (IP) only, no "(imported)" copy


def test_info_panels_roundtrip_through_validation():
    p = P.validate_profile({"name": "x", "info": {"what": "a", "how": "b", "need": "c", "pass_means": "d", "typical": "e"}})
    assert p["info"]["pass_means"] == "d"
    assert P.validate_profile({"name": "x"})["info"] is None


def test_old_database_without_the_new_columns_is_migrated(d):
    import sqlite3
    db = sqlite3.connect(d + "/traffic-tester.db")
    db.executescript("CREATE TABLE profiles(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, builtin INTEGER NOT NULL DEFAULT 0, json TEXT NOT NULL, created_at TEXT, updated_at TEXT);")
    old = P.validate_profile({"name": "DER trip test", "builtin": True, "transport": "serial"})
    db.execute("INSERT INTO profiles(name,builtin,json) VALUES(?,1,?)", ("DER trip test", json.dumps(old)))
    db.execute("INSERT INTO profiles(name,builtin,json) VALUES(?,0,?)", ("Saved earlier", json.dumps(P.validate_profile({"name": "Saved earlier", "transport": "udp"}))))
    db.commit(); db.close()
    s = Store(d)
    names = [p["name"] for p in s.list_profiles()]
    assert names[0] == "DER trip test" and names[-1] == "Saved earlier" and names.count("DER trip test") == 1
    assert by_name(s, "DER trip test")["preset_key"] == "der-trip-test"


def test_http_endpoints_edit_reset_hide_order_delete_undo():
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        ps = c.get("/api/profiles").json()
        der = next(p for p in ps if p["name"] == "DER trip test")
        r = c.put(f"/api/profiles/{der['id']}", json={**der, "name": "Edited DER", "threshold_ms": 50})
        assert r.status_code == 200 and r.json()["edited"] and r.json()["name"] == "Edited DER"
        assert c.put(f"/api/profiles/{der['id']}", json={**der, "name": "Quick link check"}).status_code == 422
        assert c.delete(f"/api/profiles/{der['id']}").status_code == 403
        assert c.post(f"/api/profiles/{der['id']}/reset").json()["name"] == "DER trip test"
        assert c.post(f"/api/profiles/{der['id']}/hide").json()["hidden"] is True
        assert c.post("/api/profiles/restore-hidden").json()["restored"] == 1
        ids = [p["id"] for p in c.get("/api/profiles").json() if p["kind"] == "trip"]
        o = c.post("/api/profiles/order", json={"ids": list(reversed(ids))}).json()["profiles"]
        assert [p["id"] for p in o if p["kind"] == "trip"] == list(reversed(ids))
        assert c.post("/api/profiles/order", json={"ids": "x"}).status_code == 422
        mine = c.post("/api/profiles", json={"name": "Mine", "transport": "udp"}).json()
        gone = c.delete(f"/api/profiles/{mine['id']}").json()["deleted"]
        assert c.get(f"/api/profiles/{mine['id']}").status_code == 404
        back = c.post("/api/profiles/undelete", json=gone).json()
        assert back["name"] == "Mine" and c.get(f"/api/profiles/{back['id']}").status_code == 200
        assert c.post("/api/profiles/9999/hide").status_code == 404


def test_every_builtin_trip_test_has_all_five_plain_english_sections():
    trips = [p for p in P.builtin_presets() if p["kind"] == "trip"]
    assert len(trips) >= 9
    for p in trips:
        for key, title in P.INFO_SECTIONS:
            t = (p["info"] or {}).get(key, "")
            assert len(t) >= 60, f"{p['name']}: section '{title}' is missing or too short"
    # the Mirrored Bits test must say plainly that it is not compatible with SEL relays
    mb = next(p for p in trips if p["mode"] == "mirrored")
    assert "not wire-compatible with sel" in mb["info"]["what"].lower() and "not compatible with sel" in mb["description"].lower()
    # one source: the AI guide gets the same text
    from traffic_tester.aiguide import build as B
    assert any(p["info"]["what"] == x["info"]["what"] for p in trips for x in B.presets() if x["name"] == p["name"])
