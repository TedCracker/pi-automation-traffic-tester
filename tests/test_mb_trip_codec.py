"""The "Trip frame (seq + timestamp + CRC)" codec of the DER transfer-trip design: CRC, sequence wrap and gaps, echo round-trip maths,
resync after a lost character, PU/DO = 2, the p99.9 pass rule and the new built-in preset."""
import random

from traffic_tester import profiles as P
from traffic_tester.mbemu import Settings, Emulator, get_codec, Framer
from traffic_tester.mbemu import codec as C
from traffic_tester.mbemu.engine import LatHist, selftest, verdict
from tests.test_mbemu import Clock, FakeLink

MS = 1_000_000


def trip(**kw):
    kw.setdefault("baud", 19200)
    kw.setdefault("trip_every_s", 0)
    kw.setdefault("codec", "trip")
    s = Settings.from_dict({"proto": "A", "tx_id": 1, "rx_id": 2, **kw})
    clk, link = Clock(), FakeLink()
    e = Emulator(s, link, "t", mono=clk, real=lambda: clk.t)
    e.begin(0)
    return e, link, clk


def frame(bits=0, seq=0, ts=0, echo=None, echo_ts=0, hold=0, tx_id=2):
    return get_codec("trip").encode_frame(bits, tx_id, seq, ts, echo, echo_ts, hold)


def feed(e, link, clk, data, t):
    clk.t = t
    link.inbox = [(data, t, t)]
    e._rx(link.recv(0))


def test_codec_is_registered_labelled_and_not_sel():
    c = get_codec("trip")
    assert not c.sel_compatible and "not SEL wire-compatible" in c.label and c.trip_frame and c.chars_per_msg == 19
    assert len(frame()) == 19
    # one frame at 19200 8N1 = 19 x 10 / 19200 s = 9.9 ms
    assert abs(Settings.from_dict({"codec": "trip", "baud": 19200}).nominal_ns() / MS - 9.896) < 0.01


def test_round_trip_all_fields():
    c = get_codec("trip")
    d = c.decode(Framer.plain(c.encode_frame(0xA5, 7, 65535, 0xFFFFFFFF, 123, 4_000_000_000, 777)))
    assert (d.ok_structure, d.crc_ok, d.copies, d.msg_id, d.seq, d.ts_us, d.echo_valid, d.echo_seq, d.echo_ts_us, d.hold_us) == \
        (True, True, [0xA5] * 3, 7, 65535, 0xFFFFFFFF, True, 123, 4_000_000_000, 777)
    assert not c.decode(Framer.plain(c.encode_frame(0, 1, 0, 0))).echo_valid
    assert c.decode(Framer.plain(c.encode_frame(0, 1, 0, 0, 5, 0, 10 ** 9))).hold_us == 0xFFFF       # a hold time too long is clamped, not wrapped


def test_crc_catches_every_single_bit_error():
    c = get_codec("trip")
    m = frame(0x5A, 1234, 987654, 4321, 55555, 321)
    for byte in range(len(m)):
        for bit in range(8):
            b = bytearray(m)
            b[byte] ^= 1 << bit
            d = c.decode(Framer.plain(bytes(b)))
            assert not (d.ok_structure and d.crc_ok), (byte, bit)
            assert not c.frame_valid(Framer.plain(bytes(b)))
    assert c.frame_valid(Framer.plain(m))


def test_crc_catches_random_multi_bit_damage():
    c = get_codec("trip")
    rnd = random.Random(7)
    m = frame(0x33, 9, 1, 8, 2, 3)
    miss = 0
    for _ in range(3000):
        b = bytearray(m)
        for _ in range(rnd.randint(2, 6)):
            b[rnd.randrange(2, 19)] ^= 1 << rnd.randrange(8)
        if bytes(b) != m and c.frame_valid(Framer.plain(bytes(b))):
            miss += 1
    assert miss <= 1                      # 2^-16 per damaged frame


def test_crc_reject_is_counted_and_rok_drops():
    e, link, clk = trip()
    feed(e, link, clk, frame(seq=0) + frame(seq=1), 10 * MS)
    assert e.ch.msgs_good == 2 and e.ch.rok
    bad = bytearray(frame(seq=2))
    bad[4] ^= 0x10
    feed(e, link, clk, bytes(bad), 20 * MS)
    assert e.ch.rejects["crc"] == 1 and not e.ch.rok and e.frames()["crc_errors"] == 1


def test_sequence_gaps_are_counted_exactly_and_wrap_at_65535():
    e, link, clk = trip()
    seqs = [65533, 65534, 65535, 0, 1, 5, 6]            # 2, 3, 4 missing
    for i, q in enumerate(seqs):
        feed(e, link, clk, frame(seq=q), (i + 1) * 10 * MS)
    f = e.frames()
    assert f["lost"] == 3 and f["lost_events"] == 1 and f["longest_gap"] == 3 and f["good"] == 7
    assert f["duplicates"] == 0 and f["out_of_order"] == 0


def test_duplicates_and_out_of_order_are_counted_and_ignored():
    e, link, clk = trip()
    for i, chunk in enumerate([[10], [11, 11], [12, 10], [13]]):      # a repeat or an old frame rides along with a real one: it is not a sign of life by itself
        feed(e, link, clk, b"".join(frame(seq=q) for q in chunk), (i + 1) * 10 * MS)
    f = e.frames()
    assert f["duplicates"] == 1 and f["out_of_order"] == 1 and f["lost"] == 0 and f["good"] == 4


def test_echo_round_trip_math_has_no_clock_sync():
    """We sent seq 3 at our clock 1_000_000 us; the far end held it 2.5 ms and its frame arrives here at our clock 1_020_000 us -> rtt = 20 - 2.5 = 17.5 ms."""
    e, link, clk = trip()
    feed(e, link, clk, frame(seq=0, ts=555, echo=3, echo_ts=1_000_000, hold=2500), 1_020 * MS)
    assert e.rtt_n == 1 and abs(e.rtt_last - 17.5) < 1e-6 and abs(e.rtt_hist.pct(50) - 17.5) < 0.06
    # the same echo repeated in the next frame (our frame was not heard in between) is not a second sample
    feed(e, link, clk, frame(seq=1, ts=999, echo=3, echo_ts=1_000_000, hold=12500), 1_030 * MS)
    assert e.rtt_n == 1
    feed(e, link, clk, frame(seq=2, ts=999, echo=4, echo_ts=1_010_000, hold=0), 1_040 * MS)
    assert e.rtt_n == 2 and abs(e.rtt_last - 30.0) < 1e-6


def test_echo_timestamp_wraps_at_32_bits():
    e, link, clk = trip()
    send_us = 0xFFFFFFFF - 5000
    feed(e, link, clk, frame(seq=0, echo=1, echo_ts=send_us, hold=0), (send_us + 12_000) * 1000)        # our clock passed 2**32 us in between
    assert e.rtt_n == 1 and abs(e.rtt_last - 12.0) < 1e-6


def test_our_frames_carry_sequence_timestamp_and_the_echo_of_what_we_heard():
    e, link, clk = trip()
    feed(e, link, clk, frame(seq=41, ts=7000, echo=None), 100 * MS)
    clk.t = 103 * MS
    e._next_tx = 0
    e._tx_step(clk.t)
    d = get_codec("trip").decode(Framer.plain(link.sent[-1]))
    assert d.crc_ok and d.seq == 0 and d.ts_us == 103_000 and d.echo_valid and d.echo_seq == 41 and d.echo_ts_us == 7000 and d.hold_us == 3000
    e._next_tx = 0
    e._tx_step(clk.t + 10 * MS)
    assert get_codec("trip").decode(Framer.plain(link.sent[-1])).seq == 1


def test_a_lost_character_resyncs_without_a_storm_of_rejects():
    e, link, clk = trip()
    stream = b"".join(frame(seq=i) for i in range(30))
    cut = stream[:19 * 5 + 7] + stream[19 * 5 + 8:]                  # one character vanishes inside frame 5
    feed(e, link, clk, cut, 300 * MS)
    f = e.frames()
    assert f["good"] >= 27 and f["crc_errors"] <= 2 and f["lost"] == 30 - f["good"]            # the sequence numbers account for every missing frame exactly


def test_security_two_good_frames_before_a_bit_changes():
    e, link, clk = trip(rmb_pu=[2] * 8, rmb_do=[2] * 8)
    feed(e, link, clk, frame(bits=0, seq=0) + frame(bits=0, seq=1), 20 * MS)
    assert e.rx.value & 1 == 0
    feed(e, link, clk, frame(bits=1, seq=2), 30 * MS)
    assert e.rx.value & 1 == 0                                         # one frame is not enough
    feed(e, link, clk, frame(bits=1, seq=3), 40 * MS)
    assert e.rx.value & 1 == 1
    feed(e, link, clk, frame(bits=0, seq=4), 50 * MS)
    assert e.rx.value & 1 == 1                                         # a single glitch frame does not clear it either
    feed(e, link, clk, frame(bits=0, seq=5), 60 * MS)
    assert e.rx.value & 1 == 0


def test_frame_codec_frees_bit_8():
    e, link, clk = trip()
    assert not e.echo_on and e._user_mask == 0xFF
    e.set_tmb(7, 1)                                                    # TMB8 belongs to the user now


def test_lathist_percentiles_and_bounded_memory():
    h = LatHist()
    for i in range(1, 1001):
        h.add(i / 10.0)                                                # 0.1 .. 100.0 ms
    assert abs(h.pct(50) - 50.0) <= 0.06 and abs(h.pct(99) - 99.0) <= 0.06 and abs(h.pct(99.9) - 99.9) <= 0.06 and h.max == 100.0
    assert h.count_over(90.0) == 100 or h.count_over(90.0) == 99
    n = len(h.c)
    for _ in range(100_000):
        h.add(5000.0)                                                  # beyond the top bucket: still counted, max exact
    assert len(h.c) == n and h.max == 5000.0 and h.n == 100_000 + 1000


def test_p999_rule_passes_and_fails_on_the_999th_percentile():
    def m(p999, p99=10.0):
        rt = {"n": 10000, "p50": 5.0, "p95": 8.0, "p99": p99, "p999": p999, "max": p999 + 1}
        return {"frame_mode": True, "rtt": rt, "frames": {"good": 10000, "lost": 0, "lost_pct": 0.0}, "unavail_pct": 0.0, "rok_drops": 0, "longest_outage_ms": 0, "rbad_events": 0}
    assert verdict(m(150.0), 200, 99.9, 0.1, 0.1)["pass"]
    v = verdict(m(250.0, p99=20.0), 200, 99.9, 0.1, 0.1)
    assert not v["pass"] and "250" in v["reasons"][0] and "99.9" in v["reasons"][0]
    assert verdict(m(250.0, p99=20.0), 200, 99, 0.1, 0.1)["pass"]       # the same run passes at p99: the rule really is p99.9
    lost = m(10.0)
    lost["frames"] = {"good": 9000, "lost": 1000, "lost_pct": 10.0}
    assert not verdict(lost, 200, 99.9, 0.1, 0.1)["pass"]


def test_rule_accepts_p99_9_and_the_preset_is_listed():
    assert P.validate_profile({"name": "x", "mode": "mirrored", "transport": "serial", "threshold_ms": 200, "rule": {"percentile": 99.9}})["rule"]["percentile"] == 99.9
    pre = [p for p in P.builtin_presets() if p["name"] == "DER transfer trip — Orbit radio (2 s budget)"]
    assert len(pre) == 1
    p = pre[0]
    assert p["mode"] == "mirrored" and p["transport"] == "serial" and p["serial"]["baud"] == 19200 and p["mirrored"]["codec"] == "trip"
    assert p["mirrored"]["settings"]["rmb_pu"] == [2] * 8 and p["mirrored"]["settings"]["rmb_do"] == [2] * 8
    assert p["mirrored"]["trip_every_s"] == 2.0 and p["threshold_ms"] == 200 and p["rule"]["percentile"] == 99.9 and p["duration_s"] == 24 * 3600
    assert "Substation relay" in p["mirrored"]["settings"]["role_names"]["A"] and "DER relay" in p["mirrored"]["settings"]["role_names"]["B"]
    assert p["info"] and all(p["info"].get(k) for k in ("what", "how", "need", "pass_means", "typical"))
    assert any(x["name"] == "DER trip — Mirrored Bits mode" and x["mirrored"]["codec"] == "sim" for x in P.builtin_presets())    # the old one is unchanged


def test_whole_run_back_to_back_over_the_internal_wire():
    r = selftest(duration_s=3, a={"codec": "trip", "baud": 19200, "trip_every_s": 0.5, "rmb_pu": [2] * 8, "rmb_do": [2] * 8, "rtt_max_ms": 200},
                 b={"codec": "trip", "baud": 19200, "rmb_pu": [2] * 8, "rmb_do": [2] * 8, "rtt_max_ms": 200})
    s = r["summary"]
    m = s["mirrored"]
    assert s["verdict"]["pass"], s["verdict"]
    assert m["frame_mode"] and m["frames"]["lost"] == 0 and m["frames"]["crc_errors"] == 0 and m["rtt"]["n"] > 100
    assert s["rtt_ms"]["p999"] is not None and m["transfer"]["n"] > 0 and m["one_way_gps"]
    assert r["status_b"]["rtt"]["n"] > 100                                 # both ends measure the round trip
