"""Mirrored Bits echo round trip / bit transfer time over a SLOW link (cellular radios, 50-500 ms) with frame loss and ROK drops.
Found on the real Pis behind the Orbit radios: the echo read ~1 ms instead of >100 ms because every ROK drop made the near end flip the echo bit again, so several
edges circulated and each returning edge was paired with the newest flip.  Virtual time (tests/mbecho.py): deterministic, no sleeping."""
import pytest

from mbecho import run_echo
from traffic_tester.mbemu.engine import transfer_times

MS = 1_000_000
RADIO = dict(delay_ms=150.0, loss=0.10, seconds=30.0)


def _ok_samples(ea, lo, hi, n_min):
    r = ea.rtt_ms
    assert len(r) >= n_min, f"only {len(r)} samples"
    assert min(r) >= lo, f"a sample of {min(r)} ms is impossible on this link"
    assert max(r) <= hi, f"a sample of {max(r)} ms"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_round_trip_is_the_real_300ms_with_loss_and_periodic_rok_drops(seed):
    ea, eb = run_echo(blackout_every_s=3.0, blackout_ms=60, seed=seed, **RADIO)
    assert ea.ch.rok_drops >= 8                                  # the ROK drops are really there
    _ok_samples(ea, 300 - 15, 300 + 15, 30)
    assert abs(sorted(ea.rtt_ms)[len(ea.rtt_ms) // 2] - 300) < 15


def test_round_trip_survives_long_radio_dropouts_too():
    ea, eb = run_echo(blackout_every_s=3.0, blackout_ms=400, **RADIO)
    assert ea.ch.rok_drops >= 8 and eb.ch.rok_drops >= 8
    _ok_samples(ea, 285, 315, 12)
    assert ea.echo_discarded >= 1                                 # the interrupted edges were thrown away, not turned into numbers


def test_dropouts_at_the_far_end_only_do_not_fake_an_echo():
    ea, eb = run_echo(blackout_every_s=4.0, blackout_ms=80, direction="a2b", **RADIO)       # B loses ROK, A never does
    assert eb.ch.rok_drops >= 6 and ea.ch.rok_drops <= 4        # (A only has the random loss)
    _ok_samples(ea, 285, 400, 20)                                 # a late edge (the far end was down when it arrived) may be slower, never faster


def test_dropouts_at_the_near_end_only():
    ea, eb = run_echo(blackout_every_s=4.0, blackout_ms=80, direction="b2a", **RADIO)
    assert ea.ch.rok_drops >= 6 and eb.ch.rok_drops <= 4
    _ok_samples(ea, 285, 315, 20)


def test_fast_link_is_unchanged_about_4ms_round_trip():
    ea, eb = run_echo(delay_ms=2.0, loss=0.0, seconds=10.0)
    _ok_samples(ea, 3.5, 4.0 + 15, 1000)
    assert ea.rtt_alarm_events == 0
    ea, eb = run_echo(delay_ms=2.0, loss=0.10, seconds=20.0, blackout_every_s=2.0, blackout_ms=30)
    _ok_samples(ea, 3.5, 4.0 + 15, 200)


@pytest.mark.parametrize("delay", [50.0, 250.0])
def test_round_trip_follows_the_link_delay(delay):
    ea, eb = run_echo(delay_ms=delay, loss=0.10, seconds=30.0, blackout_every_s=4.0, blackout_ms=100)
    _ok_samples(ea, 2 * delay - 5, 2 * delay + 15, 15)


def test_only_one_echo_edge_is_in_flight():
    seen = []

    def trace(t, ea, eb):
        seen.append(ea.echo_pend is not None)
    ea, eb = run_echo(blackout_every_s=3.0, blackout_ms=200, trace=trace, **RADIO)
    # launches are at least one round trip apart unless the previous one was dropped (ROK) or timed out
    assert ea.echo_changes <= 1 + ea.rtt_n + ea.echo_discarded + ea.echo_timeouts + 1


def test_constant_rok_drops_say_so_instead_of_showing_a_number():
    # the link is up for 100 ms and down for 400 ms, over and over: no echo can complete in a 300 ms round trip
    ea, eb = run_echo(delay_ms=150.0, loss=0.0, seconds=30.0, blackout_every_s=0.5, blackout_ms=400, blackout_from_s=0.2)
    assert ea.rtt_n == 0 and ea.echo_discarded >= 10
    s = ea.summary(threshold_ms=60.0)
    m = s["mirrored"]
    assert m["echo"]["unreliable"] is True and "could not be measured reliably" in m["echo_note"] and str(ea.echo_discarded + ea.echo_timeouts) in m["echo_note"]
    assert s["verdict"]["pass"] is False and any("could not be measured reliably" in r for r in s["verdict"]["reasons"])
    assert s["mirrored"]["transfer"]["n"] == 0 and s["mirrored"]["transfer"]["p50"] is None


def test_clean_run_has_no_echo_note():
    ea, eb = run_echo(delay_ms=20.0, loss=0.0, seconds=10.0)
    m = ea.summary(threshold_ms=60.0)["mirrored"]
    assert m["echo_note"] is None and m["echo"]["unreliable"] is False and m["echo"]["discarded"] <= 1


def test_summary_half_round_trip_is_never_sub_ms_on_a_radio_link():
    ea, eb = run_echo(blackout_every_s=4.0, blackout_ms=60, **RADIO)
    s = ea.summary(threshold_ms=60.0, far=None)
    t = s["mirrored"]["transfer"]
    assert t["basis"] == "rtt_half" and t["min"] >= 135 and abs(t["p50"] - 150) < 10
    assert s["mirrored"]["echo_note"] is None or "discarded" in s["mirrored"]["echo_note"]


def test_one_way_bit_transfer_time_with_trips_over_a_slow_link():
    ea, eb = run_echo(blackout_every_s=4.0, blackout_ms=60, flip_every_s=1.0, **RADIO)
    s = ea.summary(threshold_ms=200.0, far=eb.reflect_events(), one_way_ok=True)
    t = s["mirrored"]["transfer"]
    assert t["basis"] == "one_way" and t["n"] >= 20
    assert t["min"] >= 140 and t["p50"] < 175          # one way = 150 ms + a few frames


# ---------------------------------------------------------------- transfer_times: edges that cannot be told apart get no figure
def fl(bit, v, ms):
    return {"bit": bit, "value": v, "t_real_ns": int(ms * MS), "t_mono_ns": int(ms * MS)}


def ch(bit, v, ms):
    return {"bit": bit, "value": v, "t_real_ns": int(ms * MS), "t_mono_ns": int(ms * MS), "via_msg": True}


def test_transfer_times_clean_flips_pair_normally():
    rows = transfer_times([fl(1, 1, 0), fl(1, 0, 1000)], [ch(1, 1, 150), ch(1, 0, 1150)])
    assert [r["t_ms"] for r in rows] == [150.0, 150.0] and not any(r["lost"] or r["ambiguous"] for r in rows)


def test_transfer_times_a_lost_edge_is_not_paired_with_a_later_flips_change():
    # flip 1 (to 1) never arrived; flip 3 (to 1) did, 2 s later: pairing flip 1 with that change would invent a 2.15 s transfer
    rows = transfer_times([fl(1, 1, 0), fl(1, 0, 1000), fl(1, 1, 2000)], [ch(1, 1, 2150)])
    assert rows[0]["t_ms"] is None and rows[0]["ambiguous"] is True and rows[0]["lost"] is False
    assert rows[2]["t_ms"] == 150.0 and not rows[2]["ambiguous"]          # ... that change is left for flip 3, which it belongs to


def test_transfer_times_flips_a_second_apart_on_a_slow_link_are_fine():
    flips = [fl(1, i % 2, i * 1000) for i in range(1, 9)]
    rows = transfer_times(flips, [ch(1, i % 2, i * 1000 + 180) for i in range(1, 9)])
    assert [r["t_ms"] for r in rows] == [180.0] * 8


def test_transfer_times_never_goes_below_the_real_delay_when_flips_overlap_on_a_slow_link():
    # flips every 100 ms on a 150 ms link: each change really took 150 ms; none may be reported shorter
    for gap in (100, 50, 20):
        flips = [fl(1, i % 2, i * gap) for i in range(1, 13)]
        far = [ch(1, i % 2, i * gap + 150) for i in range(1, 13)]
        rows = transfer_times(flips, far)
        assert all(r["t_ms"] is None or r["t_ms"] == 150.0 for r in rows), (gap, rows)
        assert sum(1 for r in rows if r["t_ms"] == 150.0) >= 0
