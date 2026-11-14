"""Compare runs + baseline (SPEC 7.5, competitive review M1): logic, API routes and the one-page PDF."""
import tempfile

import pytest
from fastapi.testclient import TestClient

from traffic_tester import compare as C
from traffic_tester.report import build_comparison, build_report
from traffic_tester.server import create_app
from traffic_tester.store import Store

from _cmpfix import gen_run, mb_run, pdf_text, trip_run


@pytest.fixture()
def st():
    return Store(tempfile.mkdtemp())


@pytest.fixture()
def client():
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        yield c


def test_two_trip_results_table_change_sentence_and_chart(st):
    a = trip_run(st, p50=25.0, p99=41.0)
    b = trip_run(st, p50=41.0, p99=79.0, lost=3)
    c = C.compare(st, [a, b])
    assert c["kind"] == "trip" and [r["letter"] for r in c["runs"]] == ["A", "B"] and c["runs"][0]["id"] == a
    rows = {r["key"]: r for r in c["rows"]}
    assert rows["p99"]["values"] == [41.0, 79.0] and rows["p99"]["change"][0] is None
    ch = rows["p99"]["change"][1]
    assert ch["abs"] == 38.0 and ch["state"] == "more" and ch["judged"] == "worse" and ch["pct"] == pytest.approx(92.7)
    assert rows["loss"]["change"][1]["judged"] == "worse" and rows["sent"]["change"][1]["state"] == "same" and rows["sent"]["change"][1]["judged"] is None
    assert "typical message took 16 ms longer in B than in A" in c["sentence"] and "38 ms longer" in c["sentence"] and "B lost more" in c["sentence"]
    assert c["setup_diff"] == [] and c["setup_line"] == "The setup was the same in every result."
    h = c["chart"]
    assert h["type"] == "hist" and h["limit_ms"] == 60 and len(h["edges"]) == C.BINS + 1 and [s["letter"] for s in h["series"]] == ["A", "B"]
    assert all(abs(sum(s["pct"]) - 100.0) < 0.5 and len(s["cum"]) == len(h["edges"]) and s["cum"][0] == 0 and s["cum"] == sorted(s["cum"]) and s["cum"][-1] >= 99 for s in h["series"]) and h["series"][0]["n"] == 200
    assert h["edges"][-1] >= 60                                        # the limit is on the picture


def test_same_numbers_read_as_the_same(st):
    a, b = trip_run(st, p50=25.0, p99=41.0), trip_run(st, p50=25.2, p99=41.3)
    c = C.compare(st, [a, b])
    assert {r["key"]: r for r in c["rows"]}["p99"]["change"][1]["state"] == "same"
    assert "about the same" in c["sentence"] and "loss was the same" in c["sentence"]


def test_setup_differences_in_one_line(st):
    a = trip_run(st, thr=60)
    b = trip_run(st, thr=40, extra_prof={"interval_ms": 100})
    c = C.compare(st, [a, b])
    labels = [d["label"] for d in c["setup_diff"]]
    assert "Delay limit" in labels and "Message every" in labels and "Link" not in labels
    assert "Delay limit: A 60 ms, B 40 ms" in c["setup_line"]
    assert c["chart"]["limit_ms"] is None                              # two limits: no single line to draw


def test_three_and_four_results_and_the_limits_on_the_count(st):
    ids = [trip_run(st, p99=40.0 + 10 * i) for i in range(5)]
    c = C.compare(st, ids[:3])
    assert len(c["runs"]) == 3 and len(c["rows"][0]["change"]) == 3 and "A was the fastest" in c["sentence"]
    assert len(C.compare(st, ids[:4])["runs"]) == 4
    with pytest.raises(C.CompareError, match="at most 4"):
        C.compare(st, ids)
    with pytest.raises(C.CompareError, match="at least 2"):
        C.compare(st, [ids[0], ids[0]])
    with pytest.raises(C.CompareError, match="does not exist"):
        C.compare(st, [ids[0], 9999])


def test_mixed_kinds_running_and_empty_results_are_refused(st):
    t, g, m = trip_run(st), gen_run(st), mb_run(st)
    for pair in ([t, g], [t, m], [g, m]):
        with pytest.raises(C.CompareError, match="Pick results of the same kind"):
            C.compare(st, pair)
    with pytest.raises(C.CompareError, match="still running"):
        C.compare(st, [t, trip_run(st, status="running")])
    bad = trip_run(st)
    st.finish_run(bad, "failed", {"error": "no answer"})
    with pytest.raises(C.CompareError, match="no numbers"):
        C.compare(st, [t, bad])


def test_generator_results_throughput_lines_from_zero(st):
    a, b = gen_run(st, rx=5.0, p99=20.0), gen_run(st, rx=8.0, loss=0.5, p99=30.0)
    c = C.compare(st, [a, b])
    assert c["kind"] == "generator" and c["chart"]["type"] == "lines"
    s0, s1 = c["chart"]["series"]
    assert s0["t"][0] == 1.0 and s0["rx_mbps"][0] == pytest.approx(5.0, abs=0.01) and s1["rx_mbps"][0] == pytest.approx(8.0, abs=0.01) and s1["lat_ms"][0] == 15.0
    rows = {r["key"]: r for r in c["rows"]}
    assert rows["rx_mbps"]["change"][1]["judged"] == "better" and rows["loss"]["change"][1]["judged"] == "worse"
    assert "B carried 3 Mb/s more than A" in c["sentence"]


def test_mirrored_bits_results(st):
    a, b = mb_run(st, p99=6.0, avail=99.9), mb_run(st, p99=9.0, avail=99.5)
    c = C.compare(st, [a, b])
    assert c["kind"] == "mirrored" and c["kind_name"] == "Mirrored Bits test"
    rows = {r["key"]: r for r in c["rows"]}
    assert rows["avail"]["change"][1]["judged"] == "worse" and rows["p99"]["label"].startswith("Bit transfer time") and rows["rejected"]["values"] == [1, 1]
    assert "bit change" in c["sentence"] and "ROK availability was lower" in c["sentence"] and c["chart"]["type"] == "hist"


# ---------------------------------------------------------------- baseline
def test_baseline_is_per_saved_test_and_later_runs_show_the_change(st):
    old = trip_run(st, name="DER trip", p99=30.0)
    base = trip_run(st, name="DER trip", p99=41.0)
    other = trip_run(st, name="Other test", p99=10.0)
    assert C.vs_baseline(st, st.get_run(base)) is None
    assert C.set_baseline(st, base) == {"ok": True, "run_id": base, "test": "DER trip"}
    v = C.vs_baseline(st, st.get_run(base))
    assert v["is_baseline"] and "is the baseline" in v["text"]
    later = trip_run(st, name="DER trip", p99=79.0, lost=2)
    v = C.vs_baseline(st, st.get_run(later))
    assert v["text"] == "vs baseline: p99 +38 ms · loss +0.2 %" and v["run_id"] == base and not v["is_baseline"]
    assert [i["judged"] for i in v["items"]] == ["worse", "worse"]
    better = trip_run(st, name="DER trip", p99=35.0)
    assert C.vs_baseline(st, st.get_run(better))["text"] == "vs baseline: p99 −6 ms · loss same"
    assert C.vs_baseline(st, st.get_run(old)) is None                   # only LATER runs are measured against it
    assert C.vs_baseline(st, st.get_run(other)) is None                 # another saved test has no baseline
    lst = {r["id"]: r for r in st.list_runs()}
    assert lst[later]["vs_baseline"]["text"].startswith("vs baseline: p99 +38 ms") and lst[base]["vs_baseline"]["is_baseline"] and lst[other]["vs_baseline"] is None
    assert lst[later]["kind"] == "trip"
    # a newer baseline replaces the old one; clearing removes it; deleting the baseline run forgets it
    C.set_baseline(st, later)
    assert C.vs_baseline(st, st.get_run(base)) is None and C.vs_baseline(st, st.get_run(later))["is_baseline"]
    assert C.clear_baseline(st, later)["cleared"] and C.vs_baseline(st, st.get_run(later)) is None
    C.set_baseline(st, base)
    st.delete_run(base)
    assert C.vs_baseline(st, st.get_run(later)) is None and C.baselines(st) == {}


def test_baseline_of_a_test_with_several_links_matches_link_by_link(st):
    b1 = trip_run(st, tester="pi1", refl="pi2", p99=20.0, gid=5)
    b2 = trip_run(st, tester="pi1", refl="pi3", p99=50.0, gid=5)
    C.set_baseline(st, b1)
    assert C.vs_baseline(st, st.get_run(b2))["is_baseline"]             # both links of the baseline test are "the baseline"
    n1 = trip_run(st, tester="pi1", refl="pi2", p99=25.0, gid=9)
    n2 = trip_run(st, tester="pi1", refl="pi3", p99=45.0, gid=9)
    assert C.vs_baseline(st, st.get_run(n1))["text"].startswith("vs baseline: p99 +5 ms")
    assert C.vs_baseline(st, st.get_run(n2))["text"].startswith("vs baseline: p99 −5 ms")
    assert st.group_run_ids(n1) == [n1, n2] and st.group_run_ids(b2) == [b1, b2] and st.group_run_ids(9999) == []


def test_baseline_refuses_what_cannot_be_one(st):
    bad = trip_run(st)
    st.finish_run(bad, "failed", {"error": "no answer"})
    with pytest.raises(ValueError, match="finished result with numbers"):
        C.set_baseline(st, bad)
    with pytest.raises(KeyError):
        C.set_baseline(st, 4242)


# ---------------------------------------------------------------- API + PDF
def test_api_compare_baseline_and_pdf(client):
    st = client.app.state.tt.store
    a, b, g = trip_run(st, p99=41.0), trip_run(st, p99=79.0), gen_run(st)
    r = client.get(f"/api/compare?run_ids={a},{b}")
    assert r.status_code == 200 and r.json()["kind"] == "trip" and len(r.json()["rows"]) >= 6
    r = client.get(f"/api/compare?run_ids={a},{g}")
    assert r.status_code == 422 and r.json()["detail"] == "Pick results of the same kind."
    assert client.get(f"/api/compare?run_ids={a}").status_code == 422 and client.get("/api/compare?run_ids=x,y").status_code == 422
    assert client.post(f"/api/runs/{a}/baseline").json()["run_id"] == a
    assert client.post("/api/runs/999/baseline").status_code == 404
    one = client.get(f"/api/runs/{b}").json()
    assert one["vs_baseline"]["text"].startswith("vs baseline: p99 +38 ms") and one["kind"] == "trip"
    lst = {x["id"]: x for x in client.get("/api/runs").json()}
    assert lst[a]["vs_baseline"]["is_baseline"] and lst[b]["vs_baseline"]["run_id"] == a
    assert client.get(f"/api/compare?run_ids={a},{b}").json()["runs"][0]["baseline"] is True
    pdf = client.get(f"/api/compare.pdf?run_ids={a},{b}")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content[:5] == b"%PDF-" and "comparison-" in pdf.headers["content-disposition"]
    assert client.get(f"/api/compare.pdf?run_ids={a},{g}").status_code == 422
    assert client.delete(f"/api/runs/{a}/baseline").json() == {"ok": True, "cleared": True}
    assert client.get(f"/api/runs/{b}").json()["vs_baseline"] is None
    assert client.delete("/api/runs/999/baseline").status_code == 404


def test_comparison_pdf_is_one_page_with_table_sentence_and_setup(st):
    a = trip_run(st, p50=25.0, p99=41.0)
    b = trip_run(st, p50=41.0, p99=79.0, thr=40)
    st.set_job(a, {"site": "Utility", "link": "Site A"})
    C.set_baseline(st, a)
    pdf = build_comparison(st, [a, b], pi_name="pi1")
    txt = pdf_text(pdf)
    import re
    assert len(re.findall(rb"/Type\s*/Page(?!s)", pdf)) == 1                 # one page
    for frag in ("Comparison", "Change \\(B against A\\)", "Slowest 1 % \\(p99\\)", "+38 ms", "Utility", "baseline", "Setup differences", "Delay limit: A 60 ms, B 40 ms", "How delays were spread"):
        assert frag in txt, frag
    for ids in ([gen_run(st, p99=20.0), gen_run(st, rx=8.0, p99=30.0)], [mb_run(st), mb_run(st, p99=9.0)]):
        assert build_comparison(st, ids)[:5] == b"%PDF-"
    with pytest.raises(C.CompareError):
        build_comparison(st, [a])


def test_report_shows_vs_baseline_under_the_verdict(st):
    a, b = trip_run(st, p99=41.0), trip_run(st, p99=79.0)
    C.set_baseline(st, a)
    txt = pdf_text(build_report(st, [a, b], "Report", pi_name="pi1"))
    assert "vs baseline: p99 +38 ms" in txt and "This result is the baseline for this test." in txt
