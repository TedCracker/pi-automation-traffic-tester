"""Job info of a run (SPEC 7.4, competitive review M5): Site, Link / radio, By, Note: stored with the run, optional, in every export."""
import json
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient

from traffic_tester import compare as C
from traffic_tester.report import build_report
from traffic_tester.server import create_app
from traffic_tester.store import Store

from _cmpfix import gen_run, pdf_text, trip_run

JOB = {"site": "Utility", "link": "Site A · 5G router", "by": "Alex", "note": "RSRP -95 dBm, light rain"}


@pytest.fixture()
def st():
    return Store(tempfile.mkdtemp())


@pytest.fixture()
def client():
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        yield c


def test_clean_job_trims_drops_empty_and_refuses_nonsense():
    assert C.clean_job(None) == {} and C.clean_job({}) == {} and C.clean_job("") == {}
    assert C.clean_job({"site": "  Center   Point ", "link": "", "by": None, "note": " x ", "other": "ignored"}) == {"site": "Center Point", "note": "x"}
    assert C.clean_job({"site": 4}) == {"site": "4"}
    with pytest.raises(ValueError, match="at most 80"):
        C.clean_job({"site": "x" * 81})
    with pytest.raises(ValueError, match="Note can be at most 300"):
        C.clean_job({"note": "x" * 301})
    with pytest.raises(ValueError, match="must be text"):
        C.clean_job({"by": ["a"]})
    with pytest.raises(ValueError, match="four optional texts"):
        C.clean_job([1, 2])
    assert C.job_line(JOB) == "Utility · Site A · 5G router · by Alex" and C.job_line({}) == "" and C.job_line({"note": "only"}) == ""


def test_job_is_stored_with_the_run_and_every_link_of_the_test(st):
    a = trip_run(st, gid=3)
    b = trip_run(st, gid=3, refl="pi3")
    solo = trip_run(st)
    assert st.get_run(a)["job"] == {} and st.last_job() == {}
    assert st.set_job(a, JOB) == [a, b]
    assert st.get_run(b)["job"] == JOB and st.get_run(b)["note"] == JOB["note"] and st.get_run(solo)["job"] == {}
    assert st.last_job() == JOB
    lst = {r["id"]: r for r in st.list_runs()}
    assert lst[a]["job"] == JOB and lst[a]["job_line"] == "Utility · Site A · 5G router · by Alex" and lst[solo]["job_line"] == ""
    st.set_job(solo, {"site": "Later site"})
    assert st.last_job() == {"site": "Later site"}
    st.set_job(solo, {})                                                # cleared: the one before it is "the last job" again
    assert st.get_run(solo)["job"] == {} and st.last_job() == JOB


def test_exports_carry_the_job(st):
    t, g = trip_run(st), gen_run(st)
    st.set_job(t, JOB)
    st.set_job(g, {"site": "Lab", "note": "a, b"})
    head, row = st.messages_csv(t).splitlines()[:2]
    assert head == "seq,phase,idle_gap_s,t_send_ms,rtt_ms,up_ms,down_ms,status,site,link_radio,by,note,group_id,leg,sender,answering_pi,basis,radio_recorded,radio_char_timeout_ms,radio_max_packet_bytes,radio_serial_mode,radio_note,started_local,ended_local,time_tag,mark"
    assert 'Utility,Site A · 5G router,Alex,"RSRP -95 dBm, light rain",' in row and ",pi1 → pi2,pi1,pi2,rtt,no (radio packetization not recorded)," in row
    assert st.intervals_csv(t).splitlines()[0].endswith("status,site,link_radio,by,note,group_id,leg,sender,answering_pi,basis") is False   # intervals have no status column
    assert st.intervals_csv(t).splitlines()[0].endswith("max_ms,site,link_radio,by,note,group_id,leg,sender,answering_pi,basis,radio_recorded,radio_char_timeout_ms,radio_max_packet_bytes,radio_serial_mode,radio_note,started_local,ended_local,time_tag,mark")
    gh, gr = st.gen_csv(g).splitlines()[:2]
    assert gh.endswith("rtt_avg_ms,site,link_radio,by,note,from_pi,to_pi,mark") and 'Lab,,,"a, b",pi1,pi2,' in gr
    assert st.run_json(t)["job"] == JOB
    import io, zipfile
    z = zipfile.ZipFile(io.BytesIO(st.session_zip(st.current_session()["id"])))
    assert json.loads(z.read(f"run-{t}/run.json"))["job"] == JOB


def test_pdf_cover_has_the_job_once_or_a_line_per_test(st):
    a, b = trip_run(st, name="Trip one"), trip_run(st, name="Trip two")
    st.set_job(a, JOB), st.set_job(b, JOB)
    txt = pdf_text(build_report(st, [a, b], "Report", pi_name="pi1"))
    assert txt.count("Utility") == 1 and "Link / radio" in txt and "RSRP -95 dBm, light rain" in txt and "Alex" in txt
    st.set_job(b, {"site": "Other site", "note": "second note"})
    txt = pdf_text(build_report(st, [a, b], "Report", pi_name="pi1"))
    assert "Link / radio" not in txt and "Job:" in txt and "Utility" in txt and "Other site" in txt and "second note" in txt
    st.set_job(a, {}), st.set_job(b, {})
    assert "Job" not in pdf_text(build_report(st, [a, b], "Report", pi_name="pi1")).replace("Pi Automation Traffic Tester", "")


def test_old_database_without_the_column_upgrades_and_keeps_its_note():
    d = tempfile.mkdtemp()
    st = Store(d)
    rid = trip_run(st)
    st.db.close()
    db = sqlite3.connect(f"{d}/traffic-tester.db")
    db.execute("UPDATE runs SET note='from before' WHERE id=?", (rid,))
    db.execute("ALTER TABLE runs DROP COLUMN job")                      # the table as 1.3.2 made it
    db.commit()
    assert "job" not in {r[1] for r in db.execute("PRAGMA table_info(runs)")}
    db.close()
    st2 = Store(d)
    assert "job" in {r["name"] for r in st2.db.execute("PRAGMA table_info(runs)")}
    assert st2.get_run(rid)["job"] == {"note": "from before"} and st2.last_job() == {}
    st2.set_job(rid, JOB)
    assert Store(d).get_run(rid)["job"] == JOB


def test_api_job_on_a_result_and_prefill(client):
    st = client.app.state.tt.store
    a = trip_run(st)
    assert client.get("/api/job/last").json()["job"] == {}
    assert [f["key"] for f in client.get("/api/job/last").json()["fields"]] == ["site", "link", "by", "note"]
    r = client.put(f"/api/runs/{a}/job", json={"job": JOB})
    assert r.status_code == 200 and r.json()["job"] == JOB and r.json()["job_line"].startswith("Utility · ") and r.json()["run_ids"] == [a]
    assert client.get(f"/api/runs/{a}").json()["job"] == JOB and client.get("/api/runs").json()[0]["job_line"].startswith("Utility")
    assert client.get("/api/job/last").json()["job"] == {k: v for k, v in JOB.items() if k != "note"}       # the note is about one run
    assert client.put(f"/api/runs/{a}/job", json={"site": "Plain body"}).json()["job"] == {"site": "Plain body"}
    assert client.put(f"/api/runs/{a}/job", json={"job": {"site": "x" * 200}}).status_code == 422
    assert client.put("/api/runs/999/job", json={"job": JOB}).status_code == 404
    assert f'"site": "Plain body"' in client.get(f"/api/runs/{a}/export.json").text


def test_api_start_takes_job_and_bad_job_never_starts_a_test(client):
    st = client.app.state.tt.store
    prof = {"name": "Job test", "transport": "udp", "net": {"peer_host": "127.0.0.1", "port": 47911}, "duration_s": 1, "interval_ms": 100, "timeout_ms": 200, "dest": {"host": "127.0.0.1"}}
    r = client.post("/api/runs", json={"profile": prof, "job": {"site": "x" * 99}})
    assert r.status_code == 422 and "Site can be at most 80" in r.json()["detail"] and not st.list_runs()
    r = client.post("/api/runs", json={"profile": prof, "job": JOB})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    assert st.get_run(rid)["job"] == JOB
    client.post(f"/api/runs/{rid}/stop")
    import time
    for _ in range(100):
        if st.get_run(rid)["status"] != "running":
            break
        time.sleep(0.1)
    assert st.get_run(rid)["job"] == JOB                                 # finishing the run does not lose it
    r2 = client.post("/api/runs", json={"profile": prof})               # no job: fine
    assert r2.status_code == 200 and st.get_run(r2.json()["run_id"])["job"] == {}
    client.post(f"/api/runs/{r2.json()['run_id']}/stop")
