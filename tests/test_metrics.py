from traffic_tester import metrics as M, profiles as P


def prof(**kw):
    return P.validate_profile({"name": "t", **kw})


def msgs(rtts, phase="steady", gap=None, start=0):
    out = []
    for i, r in enumerate(rtts):
        st = M.LOST if r is None else (M.LATE if r > 60000 else M.OK)
        out.append({"seq": start + i, "phase": phase, "idle_gap_s": gap, "rtt_us": r, "status": st})
    return out


def test_percentile_nearest_rank():
    v = list(range(1, 101))
    assert M.percentile(v, 50) == 50 and M.percentile(v, 95) == 95 and M.percentile(v, 99) == 99 and M.percentile(v, 100) == 100
    assert M.percentile([5], 99) == 5 and M.percentile([], 50) is None
    assert M.percentile([3, 1, 2], 50) == 2


def test_jitter_mean_abs_difference():
    assert M.jitter([10, 20, 10, 40]) == (10 + 10 + 30) / 3
    assert M.jitter([5]) is None


def test_classify():
    assert M.classify(59999, 60000, 1000000) == M.OK
    assert M.classify(60001, 60000, 1000000) == M.LATE
    assert M.classify(None, 60000, 1000000) == M.LOST
    assert M.classify(1000001, 60000, 1000000) == M.LOST


def test_summary_counts_and_stats():
    m = msgs([5000] * 95 + [70000, 80000, None, None, 5000])
    s = M.summarize(m, prof())
    assert s["sent"] == 100 and s["lost"] == 2 and s["late"] == 2 and s["over_threshold"] == 2 and s["received"] == 98
    assert s["loss_pct"] == 2.0 and s["rtt_ms"]["max"] == 80.0 and s["rtt_ms"]["min"] == 5.0
    assert s["jitter_ms"] is not None


def test_verdict_passes_clean_run():
    s = M.summarize(msgs([4000, 5000, 6000] * 100), prof())
    assert s["verdict"]["pass"] and s["verdict"]["reasons"] == []


def test_verdict_fails_on_percentile_not_average():
    # average is tiny (<10 ms) but the 99th percentile is over the limit -> must FAIL
    r = [1000] * 97 + [200000] * 3
    s = M.summarize(msgs(r), prof())
    assert s["rtt_ms"]["mean"] < 10          # average hides the problem; percentile rule must still fail
    assert not s["verdict"]["pass"] and "limit is 60 ms" in s["verdict"]["reasons"][0]


def test_verdict_rule_percentile_configurable():
    r = [1000] * 97 + [200000] * 3
    assert M.summarize(msgs(r), prof(rule={"percentile": 95}))["verdict"]["pass"]      # p95 is fine
    assert not M.summarize(msgs(r), prof(rule={"percentile": 100}))["verdict"]["pass"]


def test_verdict_fails_on_loss():
    r = [1000] * 990 + [None] * 10
    s = M.summarize(msgs(r), prof())
    assert not s["verdict"]["pass"] and any("lost" in x for x in s["verdict"]["reasons"])
    assert M.summarize(msgs(r), prof(rule={"max_loss_pct": 2}))["verdict"]["pass"]


def test_idle_first_stats_and_rule():
    m = msgs([2000] * 50) + msgs([3000], "idle", 5, 100) + msgs([400000], "idle", 30, 101) + msgs([None], "idle", 30, 102)
    s = M.summarize(m, prof())
    assert s["idle"]["count"] == 3 and s["idle"]["lost"] == 1 and s["idle"]["max_ms"] == 400.0
    assert [g["gap_s"] for g in s["idle"]["gaps"]] == [5, 30]
    assert not s["verdict"]["pass"]
    s2 = M.summarize(m, prof(rule={"include_idle": False}))
    assert s2["verdict"]["pass"]                         # idle ignored when the rule says so


def test_no_replies_is_a_plain_fail():
    s = M.summarize(msgs([None] * 10), prof())
    assert not s["verdict"]["pass"] and "No replies" in s["verdict"]["reasons"][0]


def test_duplicates_counters_passthrough():
    s = M.summarize(msgs([1000] * 10), prof(), {"duplicates": 2, "out_of_order": 1, "stale": 3, "corrupted": 4})
    assert (s["duplicates"], s["out_of_order"], s["stale"], s["corrupted"]) == (2, 1, 3, 4)


def test_one_way_only_when_ok():
    m = msgs([2000] * 10)
    for x in m: x["up_us"], x["down_us"] = 900, 1100
    assert "up_ms" not in M.summarize(m, prof(), one_way_ok=False)
    s = M.summarize(m, prof(), one_way_ok=True)
    assert s["up_ms"]["p50"] == 0.9 and s["down_ms"]["p50"] == 1.1
