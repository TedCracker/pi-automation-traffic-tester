"""Spec section 10: the six acceptance tests, each automated, each on the in-process back-to-back pair AND on a socat pty pair (the real
serial code path: termios PARMRK markers, select/read, write).  #6 uses the in-app injector at +50 ms fixed latency, 0 jitter."""
import time

import pytest

from mbfix import Chan, Socat, run_pair, wait_for
from traffic_tester.mbemu import get_codec
from traffic_tester.mbemu.links import InternalLink, MBSerialLink

KINDS = ["internal", "socat"]
NOM_MS = 4.1667            # 9600 baud, 4 characters
SLACK_MS = {"internal": 15, "socat": 40}


@pytest.fixture(params=KINDS)
def chan(request):
    c = Chan(request.param, 9600)
    yield c
    c.close()


def ev(e, name, value):
    return [x for x in e.events if x[2] == name and x[3] == value]


def test_1_toggle_tmb_at_a_shows_up_as_rmb_at_b_within_message_time_plus_pu(chan):
    pu = 3
    res = {}

    def flip(a, b):
        t0 = time.monotonic()
        a.set_tmb(2, 1)                                    # TMB3 at A
        res["dt"] = wait_for(lambda: b.rx.state[2] == 1, 2.0)
        res["t_flip"] = t0
        res["rmb"] = b.rx.value
    ea, eb = run_pair(chan, 2.5, b={"rmb_pu": [1, 1, pu, 1, 1, 1, 1, 1]}, script=[(1.0, flip)])
    assert res["dt"] is not None, "RMB3 never changed at B"
    # message time (1 message) + transport latency + PU messages + one more cycle for the sender to pick the change up
    limit_ms = (pu + 2) * NOM_MS + SLACK_MS[chan.kind]
    assert res["dt"] * 1000 <= limit_ms, (res["dt"] * 1000, limit_ms)
    assert res["dt"] * 1000 >= (pu - 1) * NOM_MS                    # but not faster than PU consecutive messages can arrive
    assert eb.ch.rok == 1 and ea.ch.rok == 1 and eb.ch.rok_drops == 0
    assert ea.ch.msgs_rejected == 0 and eb.ch.msgs_rejected == 0
    assert any(x[2] == "RMB3" and x[3] == 1 for x in eb.events) and any(x[2] == "TMB3" and x[3] == 1 for x in ea.events)


def test_2_kill_the_link_rok_drops_at_once_rmbs_go_to_defaults_rbad_after_rbad_pu(chan):
    res = {}

    def cut(a, b):
        a.set_tmb(1, 1)                                   # TMB2 = 1 at A (default at B is 0)
        wait_for(lambda: b.rx.state[1] == 1)
        res["before"] = b.rx.value
        res["t_cut"] = b.mono()
        a.set_impairment({"break_link": True})
        b.set_impairment({"break_link": True})
        res["dt_rok"] = wait_for(lambda: b.ch.rok == 0, 1.0)
        res["after"] = b.rx.value
    ea, eb = run_pair(chan, 4.0, a={"rbad_pu_s": 1.0}, b={"rbad_pu_s": 1.0, "rmb_default": [0, 0, 0, 0, 1, 0, 0, 0]}, script=[(1.0, cut)])
    assert res["before"] & 0b10 and not res["before"] & 0b10000
    assert res["dt_rok"] is not None and res["dt_rok"] * 1000 <= eb.ch.late_ns / 1e6 + 20       # "instantly": within the timing limit
    assert res["after"] == 0b10000                                                              # RMBs at their defaults (RMB5 default 1)
    drop = ev(eb, "ROK", 0)[-1]
    rbad = ev(eb, "RBAD", 1)
    assert rbad, "RBAD never asserted"
    assert abs((rbad[0][0] - drop[0]) / 1e9 - 1.0) < 0.05                                         # exactly RBAD_PU after ROK went 0
    assert eb.ch.rbad == 1 and ea.ch.rbad == 1


def test_3a_mismatched_ids_are_rejected(chan):
    ea, eb = run_pair(chan, 1.5, b={"rx_id": 9})
    assert eb.ch.msgs_good == 0 and eb.ch.rejects["address"] > 50 and eb.ch.rok == 0 and eb.rx.value == 0
    assert ea.ch.msgs_good > 50                                        # B's messages (ID 2) are fine for A


def test_3b_loopback_own_tx_id_is_rejected_and_flagged():
    for kind in KINDS:
        if kind == "socat":
            sc = Socat(loop=True)
            la = MBSerialLink(sc.a, 9600)
        else:
            sc = None
            la = InternalLink()
            la.peer = la                                              # A's TX wired straight back to A's RX
        from traffic_tester.mbemu import Emulator, Settings
        e = Emulator(Settings.from_dict({"proto": "A", "tx_id": 1, "rx_id": 2, "baud": 9600, "trip_every_s": 0}), la, "loop", duration_s=1.5)
        try:
            e.run()
        finally:
            if sc:
                sc.close()
        assert e.ch.msgs_good == 0 and e.ch.rejects["loopback"] > 50, (kind, e.ch.rejects)
        assert e.ch.loopback_seen == 1 and e.ch.rok == 0 and any(x[2] == "LOOPBACK" for x in e.events)


def test_3c_loopback_test_mode_lbok():
    sc = Socat(loop=True)
    try:
        from traffic_tester.mbemu import Emulator, Settings
        e = Emulator(Settings.from_dict({"proto": "A", "tx_id": 1, "rx_id": 2, "baud": 9600, "trip_every_s": 0, "loopback_test": True, "echo": "off"}),
                     MBSerialLink(sc.a, 9600), "loop", duration_s=1.5)
        e.run()
    finally:
        sc.close()
    assert e.ch.lbok == 1 and e.ch.rok == 1 and e.ch.msgs_rejected == 0


def test_4_corrupt_one_copy_of_one_bit_rejects_that_message(chan):
    la, lb = chan.links()
    codec = get_codec()
    state = {"armed": False, "done": 0}
    real_send = la.send

    def send(data):
        if state["armed"]:
            state["armed"] = False
            state["done"] += 1
            data = codec.corrupt_copy(data, 1, 3)                      # copy 2 of bit 4
        return real_send(data)
    la.send = send

    def arm(a, b):
        wait_for(lambda: b.ch.rok == 1)
        state["armed"] = True
    ea, eb = run_pair(chan, 2.0, links=(la, lb), script=[(0.8, arm)])
    assert state["done"] == 1
    assert eb.ch.rejects["consistency"] == 1 and eb.ch.msgs_rejected == 1, eb.ch.rejects
    assert eb.ch.rok_drops == 1                                          # ROK went 0 on that message ...
    assert eb.ch.rok == 1                                                 # ... and came back after N good ones
    assert ea.ch.msgs_rejected == 0


def test_5_ten_percent_random_drop_makes_rok_chatter_and_cbad_asserts_per_its_threshold(chan):
    # strict timing (floor 3 ms): at 9600 baud one dropped message is a 8.3 ms gap, which the default 5 ms host-jitter floor would see only at K x nominal = 6.25 ms
    ea, eb = run_pair(chan, 6.0, b={"cbad_pu_pct": 5.0, "cbad_window_s": 4.0, "late_floor_ms": 3.0}, impair_a={"drop_pct": 10})
    assert eb.imp.stats()["dropped"] == 0 and ea.imp.stats()["dropped"] > 60
    assert eb.ch.rok_drops >= 40, eb.ch.rok_drops                         # ROK chatter
    assert eb.ch.cbad_events >= 1 and ev(eb, "CBAD", 1)                  # unavailability in the window exceeded 5 %
    assert eb.ch.stats(eb.mono())["unavail_pct"] > 5
    # and a threshold above what the channel really suffers does NOT assert CBAD
    ea2, eb2 = run_pair(chan, 6.0, b={"cbad_pu_pct": 90.0, "cbad_window_s": 4.0, "late_floor_ms": 3.0}, impair_a={"drop_pct": 10})
    assert eb2.ch.rok_drops >= 40 and eb2.ch.cbad_events == 0


def test_6_steady_50ms_latency_zero_jitter_keeps_rok_up_and_the_echo_rtt_alarm_fires(chan):
    ea, eb = run_pair(chan, 4.0, a={"rtt_max_ms": 40.0}, impair_a={"latency_ms": 50.0, "jitter_ms": 0.0})
    assert ea.ch.rok == 1 and eb.ch.rok == 1
    assert eb.ch.rok_drops == 0 and ea.ch.rok_drops == 0                  # ROK alone does not see a steady delay ...
    assert eb.ch.msgs_rejected == 0 and ea.ch.msgs_rejected == 0
    assert ea.rtt_alarm == 1 and ea.rtt_alarm_events >= 1                 # ... the echo does
    assert any(x[2] == "RTT_ALARM" and x[3] == 1 for x in ea.events)
    st = ea.rtt_stats()
    assert 50.0 <= st["p50"] <= 50.0 + 25 and st["one_way_est_ms"] >= 25.0
