import json, tempfile
import pytest
from traffic_tester import profiles as P
from traffic_tester.store import Store, BuiltinError


def test_builtins_valid_and_marked():
    ps = P.builtin_presets()
    names = [p["name"] for p in ps]
    assert "DER trip test" in names and "GOOSE trip test - L2" in names and "GOOSE trip test - L3 multicast" in names
    der = ps[names.index("DER trip test")]
    assert der["builtin"] and der["transport"] == "serial" and der["serial"]["baud"] == 19200
    assert der["threshold_ms"] == 60 and der["rule"]["percentile"] == 99 and der["idle_gaps_s"] == [5, 30, 120]


@pytest.mark.parametrize("bad", [
    {"name": ""}, {"name": "x", "interval_ms": 0}, {"name": "x", "threshold_ms": 100, "timeout_ms": 50},
    {"name": "x", "transport": "carrier-pigeon"}, {"name": "x", "idle_gaps_s": [-1]}, {"name": "x", "impairment": "Nuclear"},
    {"name": "x", "serial": {"baud": 5}}, {"name": "x", "goose": {"dest_mac": "nope"}}])
def test_validation_rejects(bad):
    with pytest.raises(ValueError):
        P.validate_profile(bad)


def test_one_way_forces_extended_frame():
    assert P.validate_profile({"name": "x", "transport": "udp", "one_way": True})["frame"] == "extended"


def test_export_import_roundtrip_and_never_builtin():
    ps = P.builtin_presets()
    doc = json.loads(json.dumps(P.export_profiles(ps[:3])))
    assert doc["format"] == P.FORMAT and doc["schema_version"] == 1 and len(doc["profiles"]) == 3
    back = P.parse_import(doc)
    assert [b["name"] for b in back] == [p["name"] for p in ps[:3]] and not any(b["builtin"] for b in back)
    assert back[0]["interval_ms"] == ps[0]["interval_ms"]


@pytest.mark.parametrize("doc", [None, [], {"format": "x"}, {"format": P.FORMAT, "schema_version": 99, "profiles": [{}]},
                                 {"format": P.FORMAT, "schema_version": 1, "profiles": []},
                                 {"format": P.FORMAT, "schema_version": 1, "profiles": [{"name": "x", "interval_ms": -5}]}])
def test_import_rejects_bad_files(doc):
    with pytest.raises(ValueError):
        P.parse_import(doc)


def test_store_builtin_readonly_duplicate_and_import():
    s = Store(tempfile.mkdtemp())
    b = [p for p in s.list_profiles() if p["builtin"] and p["kind"] == "trip"][0]
    g = [p for p in s.list_profiles() if p["builtin"] and p["kind"] == "generator"][0]
    with pytest.raises(BuiltinError): s.update_profile(g["id"], g)         # generator / sniffer presets stay read-only; trip tests are editable (tests/test_testlist.py)
    with pytest.raises(BuiltinError): s.delete_profile(b["id"])
    d = s.duplicate_profile(b["id"])
    assert not d["builtin"] and d["name"] == b["name"] + " (copy)"
    d2 = s.duplicate_profile(b["id"])
    assert d2["name"] != d["name"]
    s.update_profile(d["id"], {**d, "threshold_ms": 30})
    assert s.get_profile(d["id"])["threshold_ms"] == 30
    imp = s.import_profiles(s.export_profiles([d["id"]]))
    assert imp[0]["name"] != d["name"] and imp[0]["threshold_ms"] == 30
    s.delete_profile(d["id"])


def test_store_builtins_reseeded_on_reopen_and_not_duplicated():
    d = tempfile.mkdtemp()
    n1 = len(Store(d).list_profiles()); n2 = len(Store(d).list_profiles())
    assert n1 == n2 == len(P.builtin_presets())


def test_sessions_runs_clear_and_interrupted_recovery():
    d = tempfile.mkdtemp(); s = Store(d)
    prof = P.builtin_presets()[0]
    sess = s.new_session("Site 4")
    rid = s.create_run(sess["id"], prof, "A", "/dev/x")
    s.add_messages(rid, [{"seq": 1, "phase": "steady", "rtt_us": 1500, "status": "ok"}])
    s.add_intervals(rid, [{"t_offset_s": 0, "sent": 1, "received": 1, "lost": 0, "late": 0, "p50_us": 1500, "p95_us": 1500, "p99_us": 1500, "max_us": 1500}])
    assert "1.5" in s.messages_csv(rid) and "p99_ms" in s.intervals_csv(rid)
    s2 = Store(d)                                  # simulated power loss: run was 'running'
    assert s2.get_run(rid)["status"] == "interrupted"
    s2.clear_session(sess["id"])
    assert s2.list_runs(sess["id"]) == [] and s2.run_messages(rid) == []


def test_mirrored_bits_serial_default_is_38400():
    """Alex 2026-11-15: 38400 is the default for Mirrored Bits over serial (real Pis, docs/e2e-real-pis-serial.md); plain serial tests stay at 19200,
    and Mirrored Bits over UDP / TCP keeps its own rate."""
    from traffic_tester.mbemu.status import Settings
    ps = {p["name"]: p for p in P.builtin_presets()}
    mb = ps["DER trip — Mirrored Bits mode"]
    assert mb["mode"] == "mirrored" and mb["serial"]["baud"] == 38400
    assert ps["DER trip test"]["serial"]["baud"] == 19200 and ps["Overnight soak"]["serial"]["baud"] == 19200
    assert P.Profile(name="x").serial.baud == 19200 and P.Mirrored().rate_baud == 19200
    assert Settings().baud == 38400
