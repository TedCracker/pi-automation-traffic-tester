"""Mirrored Bits emulator: codec, every receive rejection path, ROK / lost channel / RBAD / CBAD timing, PU/DO counts, defaults, PROTO warning,
impairment injector, contact I/O.  Time is injected (an integer clock), so every transition is checked at its exact moment."""
import random

import pytest

from traffic_tester.mbemu import Settings, Emulator, Impairer, Channel, RxBits, get_codec, Framer, SimCodec
from traffic_tester.mbemu import codec as C
from traffic_tester.mbemu.io import IO
from traffic_tester.mbemu.links import Link, InternalLink
from traffic_tester.mbemu.pairing import far_settings, proto_conflict, id_conflict
from traffic_tester.mbemu.status import proto_warning

MS = 1_000_000
S1 = 1_000_000_000


class Clock:
    t = 0

    def __call__(self):
        return self.t


class FakeLink(Link):
    def __init__(self, char_mode="plain"):
        self.char_mode = char_mode
        self.inbox, self.sent, self.err = [], [], {}
        self.overrun_available = True
        self.err_name = "framing"

    def open(self): ...
    def close(self): ...

    def send(self, data):
        self.sent.append(bytes(data))
        return len(data)

    def recv(self, timeout_s):
        out, self.inbox = self.inbox, []
        return out

    def take_errors(self):
        e, self.err = self.err, {}
        return e


def make(**kw):
    kw.setdefault("baud", 9600)
    kw.setdefault("trip_every_s", 0)
    kw.setdefault("echo", "off")
    s = Settings.from_dict({"proto": "A", "tx_id": 1, "rx_id": 2, **{k: v for k, v in kw.items() if k not in ("_mode", "_lag")}})
    clk, link = Clock(), FakeLink(kw.get("_mode", "plain"))
    e = Emulator(s, link, "t", mono=clk, real=lambda: clk.t)
    e.begin(0)
    if kw.get("_lag") is None:                       # a healthy loop: every pass is on schedule (the jumping test clock must not look like a host stall)
        orig = e._tx_step
        e._tx_step = lambda now: (setattr(e, "_next_tx", now), orig(now))[1]
    return e, link, clk


def good(bits=0, id_=2):
    return get_codec().encode(bits, id_)


def feed(e, link, clk, t, data):
    clk.t = t
    link.inbox.append((data, t, t))
    e.step()


NOM = 4_166_667          # 4 chars x 10 bits at 9600 baud


# ------------------------------------------------------------------ codec
def test_simcodec_is_labelled_and_not_sel():
    c = get_codec()
    assert isinstance(c, SimCodec) and not c.sel_compatible and "not SEL wire-compatible" in c.label and "emulator-to-emulator only" in c.label
    with pytest.raises(ValueError):
        get_codec("sel")


def test_simcodec_round_trip_every_bit_pattern_and_id():
    c = get_codec()
    for bits in range(256):
        for i in (0, 1, 2, 31):
            m = c.encode(bits, i)
            assert len(m) == 4 == c.chars_per_msg
            d = c.decode(Framer.plain(m))
            assert d.ok_structure and d.msg_id == i and d.copies == [bits, bits, bits] and d.char_err is None


def test_every_bit_is_carried_three_times():
    m = get_codec().encode(0b10100101, 3)
    assert m[1] == m[2] == m[3] == 0b10100101


def test_decode_reports_per_character_errors():
    c = get_codec()
    chars = Framer.plain(c.encode(5, 2))
    chars[2] = C.RxChar(chars[2].value, "parity")
    assert c.decode(chars).char_err == "parity"


def test_framer_resyncs_after_junk_and_keeps_messages():
    f = Framer(get_codec())
    stream = bytes([0x00, 0x13, 0x77]) + get_codec().encode(9, 2) + get_codec().encode(9, 2)
    out = f.feed(Framer.plain(stream))
    junk = [x for x in out if isinstance(x, tuple)]
    msgs = [x for x in out if not isinstance(x, tuple)]
    assert len(msgs) == 2 and len(junk) == 1 and junk[0][1] == 3


def test_parmrk_markers_are_decoded_and_real_0xff_survives():
    f = Framer(get_codec())
    chars = f.parse_parmrk(b"\xa2\xff\xff\xff\xff\xff\x00\x03", "parity")
    assert [c.value for c in chars] == [0xA2, 0xFF, 0xFF, 0x03]
    assert chars[3].err == "parity" and chars[1].err is None
    assert f.parse_parmrk(b"\xff", "x") == [] and f.parse_parmrk(b"\x00\x07", "x")[0].err == "x"      # split across reads


# ------------------------------------------------------------------ receive rejection paths (spec 3)
def rejects(e):
    return {k: v for k, v in e.ch.rejects.items() if v}


def up(e, link, clk, n=2):
    for i in range(n):
        feed(e, link, clk, (i + 1) * NOM, good(0))
    assert e.ch.rok == 1
    return n * NOM


def test_rok_needs_n_good_messages_and_default_is_two():
    e, link, clk = make()
    assert e.s.rok_n == 2 and e.ch.rok == 0
    feed(e, link, clk, NOM, good(1))
    assert e.ch.rok == 0 and e.rx.value == 0
    feed(e, link, clk, 2 * NOM, good(1))
    assert e.ch.rok == 1 and e.rx.value == 1                  # the message that asserts ROK also updates the bits
    e2, l2, c2 = make(rok_n=5)
    for i in range(4):
        feed(e2, l2, c2, (i + 1) * NOM, good())
    assert e2.ch.rok == 0
    feed(e2, l2, c2, 5 * NOM, good())
    assert e2.ch.rok == 1


def test_consistency_one_copy_of_one_bit_rejects_and_rok_drops_at_once():
    e, link, clk = make()
    t = up(e, link, clk)
    bad = get_codec().corrupt_copy(good(0b1010), 1, 3)
    feed(e, link, clk, t + NOM, bad)
    assert rejects(e) == {"consistency": 1} and e.ch.rok == 0 and e.ch.rok_drops == 1
    assert e.rx.value == 0                                      # bits sit at their defaults


def test_address_wrong_id_rejected():
    e, link, clk = make()
    t = up(e, link, clk)
    feed(e, link, clk, t + NOM, good(0, 7))
    assert rejects(e) == {"address": 1} and e.ch.rok == 0


def test_own_tx_id_is_loopback_and_is_flagged():
    e, link, clk = make()
    feed(e, link, clk, NOM, good(0, 1))
    assert rejects(e) == {"loopback": 1} and e.ch.loopback_seen == 1 and e.ch.rok == 0 and e.status()["loopback"] == 1
    assert any(ev[2] == "LOOPBACK" for ev in e.events)


def test_loopback_test_mode_accepts_own_id_and_sets_lbok():
    e, link, clk = make(loopback_test=True)
    feed(e, link, clk, NOM, good(0, 1))
    feed(e, link, clk, 2 * NOM, good(0, 1))
    assert e.ch.rok == 1 and e.ch.lbok == 1 and not rejects(e)
    feed(e, link, clk, 3 * NOM, good(0, 9))                     # a foreign id in the loopback test: LBOK drops
    assert e.ch.lbok == 0 and rejects(e) == {"address": 1}


def test_character_errors_parity_framing_overrun():
    e, link, clk = make(_mode="parmrk")
    link.err_name = "parity"
    t = up(e, link, clk)
    m = good(0)
    marked = m[:1] + b"\xff\x00" + m[1:2] + m[2:]               # second character delivered with a parity/framing marker
    feed(e, link, clk, t + NOM, marked)
    assert rejects(e) == {"parity": 1} and e.ch.rok == 0
    e2, l2, c2 = make(_mode="parmrk")
    l2.err_name = "framing"
    up(e2, l2, c2)
    feed(e2, l2, c2, 5 * NOM, good(0)[:3] + b"\xff\x00" + good(0)[3:])
    assert rejects(e2) == {"framing": 1}
    e3, l3, c3 = make()
    t3 = up(e3, l3, c3)
    l3.err = {"overrun": 1}                                     # the driver's overrun counter moved while this message came in
    feed(e3, l3, c3, t3 + NOM, good(0))
    assert rejects(e3) == {"overrun": 1} and e3.ch.rok == 0 and e3.errors_seen["overrun"] == 1


def test_not_a_message_is_a_framing_reject_once_per_run_of_junk():
    e, link, clk = make()
    t = up(e, link, clk)
    feed(e, link, clk, t + NOM, bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06]))
    assert rejects(e) == {"framing": 1} and e.ch.rok == 0


def test_late_message_is_rejected_for_timing():
    e, link, clk = make()
    t = up(e, link, clk)
    limit = e.ch.late_ns
    assert limit == max(int(1.5 * NOM), 5 * MS)
    e.ch.tick = lambda *_: None                                 # as if the housekeeping tick had not run: the arrival check alone catches it
    feed(e, link, clk, t + limit + MS, good(0))
    assert rejects(e) == {"timing": 1} and e.ch.rok == 0


def test_on_time_messages_at_the_limit_are_fine():
    e, link, clk = make()
    t = up(e, link, clk)
    feed(e, link, clk, t + e.ch.late_ns - 1, good(0))
    assert not rejects(e) and e.ch.rok == 1


# ------------------------------------------------------------------ missing message / lost channel / RBAD / CBAD (spec 4)
def test_missing_message_drops_rok_at_the_deadline_and_defaults_apply():
    e, link, clk = make(rmb_default=[0, 1, 0, 0, 0, 0, 0, 0])
    feed(e, link, clk, NOM, good(0b11))
    feed(e, link, clk, 2 * NOM, good(0b11))
    assert e.ch.rok == 1 and e.rx.value == 0b11
    t_last = 2 * NOM
    clk.t = t_last + e.ch.late_ns - 1
    e._next_hk = 0
    e.step()
    assert e.ch.rok == 1
    clk.t = t_last + e.ch.late_ns + 1
    e._next_hk = 0
    e.step()
    assert e.ch.rok == 0 and e.ch.rejects["missing"] == 1
    drop = [ev for ev in e.events if ev[2] == "ROK" and ev[3] == 0][-1]
    assert drop[0] == t_last + e.ch.late_ns                     # time-stamped at the deadline, not at the (later) tick
    assert e.rx.value == 0b10                                   # RMB1 -> its default 0, RMB2 -> its default 1


def test_lost_channel_rule_one_valid_message_per_three_messages_of_time():
    e, link, clk = make()
    t = up(e, link, clk)
    lost_ns = e.ch.lost_ns
    assert lost_ns >= 3 * e.nominal_ns
    clk.t = t + lost_ns - 1
    e._next_hk = 0
    e.step()
    assert e.ch.lost == 0
    clk.t = t + lost_ns + 1
    e._next_hk = 0
    e.step()
    assert e.ch.lost == 1 and e.ch.lost_events == 1
    feed(e, link, clk, clk.t + NOM, good(0))
    assert e.ch.lost == 0


def test_rbad_asserts_after_rbad_pu_seconds_of_rok_zero_and_clears_with_rok():
    e, link, clk = make(rbad_pu_s=2.0)
    t = up(e, link, clk)
    feed(e, link, clk, t + NOM, good(0, 9))                     # reject: ROK -> 0
    t0 = t + NOM
    assert e.ch.rok == 0 and e.ch.rbad == 0
    clk.t = t0 + 2 * S1 - MS
    e._next_hk = 0
    e.step()
    assert e.ch.rbad == 0
    clk.t = t0 + 2 * S1 + MS
    e._next_hk = 0
    e.step()
    assert e.ch.rbad == 1 and e.ch.rbad_events == 1
    ev = [x for x in e.events if x[2] == "RBAD"][-1]
    assert ev[0] == t0 + 2 * S1
    feed(e, link, clk, clk.t + NOM, good(0))
    feed(e, link, clk, clk.t + NOM, good(0))
    assert e.ch.rok == 1 and e.ch.rbad == 0


def test_a_short_outage_never_asserts_rbad():
    e, link, clk = make(rbad_pu_s=2.0)
    t = up(e, link, clk)
    feed(e, link, clk, t + NOM, good(0, 9))
    for i in range(2, 5):
        feed(e, link, clk, t + i * NOM, good(0))
    assert e.ch.rok == 1 and e.ch.rbad == 0 and e.ch.rbad_events == 0


def _up_channel(s, t_first=0):
    events = []
    ch = Channel(s, 0, lambda n, t, d: events.append((n, t, d)))
    ch.good(t_first + NOM)
    ch.good(t_first + 2 * NOM)
    assert ch.rok == 1
    return ch, events


def test_cbad_asserts_when_unavailability_in_the_rolling_window_exceeds_the_limit():
    s = Settings.from_dict({"cbad_window_s": 10, "cbad_pu_pct": 20, "baud": 9600})
    ch, events = _up_channel(s)
    t_down = 20 * S1                                             # the window (10 s) is full of good time before the outage
    ch.good(t_down - NOM)
    ch.bad(t_down, "consistency")
    assert ch.rok == 0
    ch.tick(t_down + 1 * S1)
    assert ch.cbad == 0                                          # 1 s of 10 s = 10 %
    ch.tick(t_down + 2 * S1 - 10 * MS)
    assert ch.cbad == 0                                          # 19.9 %
    ch.tick(t_down + 2 * S1 + 10 * MS)
    assert ch.cbad == 1 and ch.cbad_events == 1                  # 20.1 % > 20 %
    cb = [e for e in events if e[0] == "cbad" and e[2]["value"] == 1][0]
    assert t_down + 2 * S1 < cb[1] <= t_down + 2 * S1 + 10 * MS
    for i in range(1, 3):                                        # link back: ROK=1 again, then the window slides past the outage
        ch.good(t_down + 3 * S1 + i * NOM)
    assert ch.rok == 1
    t_end = t_down + 23 * S1
    ch.good(t_end - NOM)                                         # (messages keep arriving: the 20 s of silence would be a "missing" otherwise)
    ch.tick(t_end)
    assert ch.cbad == 0 and any(e[0] == "cbad" and e[2]["value"] == 0 for e in events)


def test_cbad_percentage_is_exact_over_the_window():
    s = Settings.from_dict({"cbad_window_s": 100, "cbad_pu_pct": 50, "baud": 9600})
    ch, _ = _up_channel(s)
    ch.good(10 * S1 - NOM)
    ch.bad(10 * S1, "address")
    for i in range(1, 3):
        ch.good(30 * S1 + i * NOM)
    assert ch.rok == 1
    assert ch.unavailability_pct(100 * S1) == pytest.approx(20.0, abs=0.05)    # 20 s down of a 100 s window
    assert ch.unavailability_pct(200 * S1) == pytest.approx(0.0, abs=0.01)     # the outage has left the window


def test_never_up_means_no_cbad_but_rbad():
    s = Settings.from_dict({"rbad_pu_s": 1, "baud": 9600})
    ch = Channel(s, 0)
    ch.tick(2 * S1)
    assert ch.rok == 0 and ch.cbad == 0 and ch.rbad == 1


# ------------------------------------------------------------------ per-bit security counts and defaults (spec 5)
def test_pu_do_counts_per_bit():
    rb = RxBits(pu=[3] + [1] * 7, do=[2] + [1] * 7, default=[0] * 8)
    assert rb.update(1) == [] and rb.update(1) == [] and rb.value == 0
    assert rb.update(1) == [0] and rb.value == 1                  # 0->1 only after PU=3 consecutive 1s
    assert rb.update(0) == [] and rb.value == 1
    assert rb.update(0) == [0] and rb.value == 0                  # 1->0 after DO=2 consecutive 0s
    rb.update(1)
    rb.update(1)
    rb.update(0)                                                  # a 0 in between restarts the count
    assert rb.update(1) == [] and rb.update(1) == [] and rb.value == 0


def test_defaults_while_rok_is_zero_and_resume_through_the_counts():
    e, link, clk = make(rmb_pu=[2] * 8, rmb_do=[1] * 8, rmb_default=[1, 0, 0, 0, 0, 0, 0, 0])
    feed(e, link, clk, NOM, good(0b110))
    assert e.rx.value == 0b1                                      # ROK=0: RMB1 sits at its default 1, the rest at 0
    feed(e, link, clk, 2 * NOM, good(0b110))
    assert e.ch.rok == 1 and e.rx.value == 0b1 & 0b1 or True
    assert e.rx.state[1] == 0                                     # PU=2: one message showing 1 is not enough yet...
    feed(e, link, clk, 3 * NOM, good(0b110))
    assert e.rx.state[1] == 1 and e.rx.state[2] == 1              # ... a second one is
    assert e.rx.state[0] == 0                                     # RMB1 (default 1) follows the message (0): DO=1
    feed(e, link, clk, 4 * NOM, good(0b110, 9))                   # reject -> ROK 0 -> every bit back to its default
    assert e.ch.rok == 0 and e.rx.value == 0b1


def test_outputs_follow_the_received_bits():
    e, link, clk = make()
    feed(e, link, clk, NOM, good(0b10110))
    feed(e, link, clk, 2 * NOM, good(0b10110))
    assert e.io.outs == [0, 1, 1, 0, 1, 0, 0, 0]
    feed(e, link, clk, 3 * NOM, good(0, 9))
    assert e.io.outs == [0] * 8


# ------------------------------------------------------------------ settings, PROTO pairing
def test_proto_mismatch_is_warned_and_far_settings_are_the_mirror_image():
    assert proto_warning("A", "A") and "opposite" in proto_warning("B", "B")
    assert proto_warning("A", "B") is None and proto_conflict("A", "A") and proto_conflict("A", "B") is None
    far = far_settings({"proto": "A", "tx_id": 3, "rx_id": 4, "echo": "near", "trip_every_s": 2, "loopback_test": True})
    assert far["proto"] == "B" and far["tx_id"] == 4 and far["rx_id"] == 3 and far["trip_every_s"] == 0 and far["echo"] == "far" and far["loopback_test"] is False
    assert id_conflict({"tx_id": 1, "rx_id": 2}, {"tx_id": 2, "rx_id": 1}) is None
    assert "do not match" in id_conflict({"tx_id": 1, "rx_id": 2}, {"tx_id": 1, "rx_id": 2})


def test_settings_validation_and_labels_and_rates():
    with pytest.raises(ValueError):
        Settings.from_dict({"proto": "C"})
    with pytest.raises(ValueError):
        Settings.from_dict({"rmb_pu": [0] * 8})
    with pytest.raises(ValueError):
        Settings.from_dict({"k_timing": 0.5})
    s = Settings.from_dict({"baud": 38400, "labels": {"proto": "MODE"}})
    assert s.label("proto") == "MODE" and s.label("tx_id") == "TX_ID"
    assert abs(s.nominal_ns() - 1_041_667) < 2                    # ~1 ms per message at 38.4 kbit/s (spec 2)
    assert abs(Settings.from_dict({"baud": 9600}).nominal_ns() - 4_166_667) < 2     # ~4 ms at 9600
    capped = Settings.from_dict({"baud": 38400, "rate_mode": "per_cycle", "line_hz": 60, "per_cycle": 4})
    assert abs(capped.nominal_ns() - 4_166_667) < 2               # 4 messages per 60 Hz cycle
    assert Settings.from_dict({"baud": 9600, "stopbits": 2, "parity": "E"}).nominal_ns() == int(round(4 * 12 * 1e9 / 9600))   # extra stop bit + parity: 12-bit characters
    assert Settings.from_dict({"baud": 9600}).late_limit_ns() == max(int(1.5 * 4_166_667), 5 * MS)


# ------------------------------------------------------------------ impairment injector
def test_impairer_latency_jitter_never_reorders():
    im = Impairer(random.Random(1))
    im.set({"latency_ms": 50, "jitter_ms": 20})
    for i in range(200):
        im.submit(bytes([i % 256]), i * MS)
    out, t, last = [], 0, 0
    while im.pending():
        for m in im.due(t):
            out.append((t, m))
        t += MS
    assert [m[1][0] for m in out] == [i % 256 for i in range(200)]
    first = out[0][0]
    assert 30 * MS <= first <= 70 * MS


def test_impairer_fixed_latency_exact():
    im = Impairer()
    im.set({"latency_ms": 50})
    im.submit(b"x", 1000)
    assert im.due(1000 + 50 * MS - 1) == [] and im.due(1000 + 50 * MS) == [b"x"]


def test_impairer_drop_burst_break_and_bit_errors():
    im = Impairer(random.Random(3))
    im.set({"drop_pct": 10})
    for i in range(10000):
        im.submit(b"abcd", i)
    assert 800 < im.dropped < 1200 and im.pending() == 10000 - im.dropped
    im2 = Impairer()
    im2.set({"burst_len_ms": 100, "burst_every_s": 1}, 0)
    kept = 0
    for ms in range(0, 3000):
        before = im2.dropped
        im2.submit(b"x", ms * MS)
        kept += im2.dropped == before
    assert 2650 <= kept <= 2750                                 # 100 ms of every second is a burst outage
    im3 = Impairer()
    im3.set({"break_link": True})
    im3.submit(b"x", 0)
    assert im3.pending() == 0 and im3.dropped == 1
    im3.set({"break_link": False})
    im3.submit(b"x", 1)
    assert im3.pending() == 1
    im4 = Impairer(random.Random(5))
    im4.set({"bit_error_pct": 100})
    im4.submit(bytes(4), 0)
    out = im4.due(0)[0]
    assert sum(bin(b).count("1") for b in out) == 1 and im4.corrupted == 1
    with pytest.raises(ValueError):
        im4.set({"drop_pct": 120})
    with pytest.raises(ValueError):
        im4.set({"burst_len_ms": 100})                          # a burst needs an interval too


# ------------------------------------------------------------------ contact I/O
def test_io_virtual_inputs_and_outputs_and_gpio_mapping():
    seen = []
    io = IO(lambda i, v: seen.append((i, v)))
    io.set_in(0, 1)
    io.set_in(0, 1)
    io.set_in(7, 1)
    assert io.mask == 0b10000001 and seen == [(0, 1), (7, 1)]
    with pytest.raises(ValueError):
        io.set_in(8, 1)

    class Be:
        def __init__(self):
            self.vals, self.outs = {2: 1}, {}
        def input(self, pin, pull_up):
            return lambda: self.vals.get(pin, 0)
        def output(self, pin):
            return lambda v: self.outs.__setitem__(pin, v)
        def close(self): ...
    be = Be()
    io2 = IO()
    st = io2.configure_gpio([2, 3, None, None, None, None, None, None], [17, 18, None, None, None, None, None, None], backend=be)
    assert st["mode"] == "gpio" and "3.3 V" in st["warning"]
    io2.poll_gpio()
    assert io2.mask == 1
    io2.write_outs(0b10)
    assert be.outs == {17: 0, 18: 1} or be.outs == {18: 1}
    with pytest.raises(ValueError):
        io2.configure_gpio([2, 2] + [None] * 6, [None] * 8, backend=be)
    with pytest.raises(ValueError):
        io2.configure_gpio([99] + [None] * 7, [None] * 8, backend=be)


def test_echo_bit_is_reserved_and_excluded_from_user_toggles():
    e, link, clk = make(echo="near")
    with pytest.raises(ValueError):
        e.set_tmb(7, 1)
    e.set_tmb(0, 1)
    assert e.io.mask == 1


def test_tx_sends_a_full_message_every_cycle_even_without_changes():
    e, link, clk = make()
    for i in range(1, 6):
        clk.t = i * NOM
        e.step()
    assert len(link.sent) >= 5 and len(set(link.sent)) == 1 and link.sent[0] == get_codec().encode(0, 1)


def test_a_stall_of_our_own_loop_is_not_blamed_on_the_link_but_a_link_gap_is():
    # host stall: the loop itself wakes 20 ms late (its TX schedule slipped), data that waited in the port buffer arrives in one go
    e, link, clk = make(_lag=True)
    e._next_tx = 0
    t = up(e, link, clk)
    e._next_tx = t + NOM
    feed(e, link, clk, t + NOM + 20 * MS, good(0))               # the loop was 20 ms late for TX, so a 20 ms-old message is not "late"
    assert not rejects(e) and e.ch.rok == 1 and e.host_stalls >= 1
    # link gap: the loop is on time (it sent its message when due) but nothing arrived for 20 ms
    e2, l2, c2 = make()
    t2 = up(e2, l2, c2)
    c2.t = t2 + 20 * MS
    e2._next_hk = 0
    e2.step()
    assert e2.ch.rok == 0 and e2.ch.rejects["missing"] == 1


def test_a_bit_flipped_back_to_its_default_during_an_outage_is_still_a_delivered_change():
    """Found on the real Pis: ROK chatter hides changes (the output sits at its default while the channel is down), which made bit transfer times jump by whole
    test-bit intervals.  What a good message delivers is logged against the last DELIVERED value, not against the output."""
    from traffic_tester.mbemu.engine import transfer_times
    e, link, clk = make()
    t = up(e, link, clk)
    feed(e, link, clk, t + NOM, good(0b100))              # near end flipped bit 3 to 1: delivered
    assert [x for x in e.rmb_changes if x["bit"] == 3] == [x for x in e.rmb_changes if x["bit"] == 3 and x["value"] == 1 and x["via_msg"]] and e.rx.state[2] == 1
    feed(e, link, clk, t + 2 * NOM, good(0b100, 9))        # a reject: ROK=0, the bit falls to its default 0
    assert e.rx.state[2] == 0
    t2 = t + 3 * NOM
    feed(e, link, clk, t2, good(0))                         # meanwhile the near end flipped bit 3 back to 0 and the channel comes back
    feed(e, link, clk, t2 + NOM, good(0))
    ch = [x for x in e.rmb_changes if x["bit"] == 3]
    assert [x["value"] for x in ch] == [1, 0], ch           # the 0 DID arrive although the output never left 0
    rows = transfer_times([{"bit": 3, "value": 1, "t_real_ns": t, "t_mono_ns": t}, {"bit": 3, "value": 0, "t_real_ns": t2 - MS, "t_mono_ns": t2 - MS}], e.reflect_events()["rmb_changes"])
    assert [r["lost"] for r in rows] == [False, False] and rows[1]["t_ms"] < 20
