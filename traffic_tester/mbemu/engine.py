"""The Mirrored Bits emulator: one instance per end of the channel (spec 1-9).

Loop (one thread):  compose the next message from the 8 transmit bits -> codec -> impairment injector -> link   (every send cycle, whether
or not a bit changed)   and   link -> framer -> codec -> receive checks -> channel status (ROK / RBAD / CBAD) -> per-bit security counts -> RMB1..8.

All the logic that decides things is in status.py / codec.py / impair.py (pure, unit-tested); this file wires it to a clock and a link.
"""
from __future__ import annotations

import bisect
import csv
import io as _io
import sys
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

from . import codec as C
from .impair import Impairer
from .io import IO
from .links import Link, InternalLink
from .status import Channel, RxBits, Settings, REASONS, proto_warning

NS = 1_000_000_000
CODE_OK = ""
HIST_EDGES_MS = [0.5, 1, 1.5, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 50, 100, 200, 500, 1000]
EVENTS_MAX = 100_000
RTT_KEEP = 200_000
FLIPS_KEEP = 50_000


def _pctl(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def stats_ms(xs: List[float]) -> dict:
    if not xs:
        return {"n": 0, "min": None, "p50": None, "p95": None, "p99": None, "max": None, "avg": None}
    r = lambda v: None if v is None else round(v, 3)
    return {"n": len(xs), "min": r(min(xs)), "p50": r(_pctl(xs, 50)), "p95": r(_pctl(xs, 95)), "p99": r(_pctl(xs, 99)), "max": r(max(xs)), "avg": r(sum(xs) / len(xs))}


class Emulator:
    def __init__(self, settings: Settings, link: Link, name: str = "", emit: Callable[[dict], None] = lambda e: None,
                 mono: Callable[[], int] = time.monotonic_ns, real: Callable[[], int] = time.time_ns,
                 io: Optional[IO] = None, rng=None, duration_s: Optional[float] = None):
        settings.validate()
        self.s, self.link, self.name = settings, link, name or f"PROTO {settings.proto}"
        self.codec = C.get_codec(settings.codec)
        self._emit_cb = emit
        self.mono, self.real = mono, real
        self.io = io or IO()
        self.io.on_in_change = self._on_in_change
        self.imp = Impairer(rng)
        self.duration_s = duration_s
        self.stop_evt = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self.error: Optional[str] = None
        self.status_text = "idle"
        self.lock = threading.RLock()
        self.framer = C.Framer(self.codec)
        self._reset(0)

    # ================================================================ state
    def _reset(self, t0: int) -> None:
        s = self.s
        self.t0, self.t0_real = t0, self.real()
        self.nominal_ns = s.nominal_ns()
        self.ch = Channel(s, t0, self._on_chan_event)
        self.rx = RxBits(s.rmb_pu, s.rmb_do, s.rmb_default)
        self.events: Deque[tuple] = deque(maxlen=EVENTS_MAX)
        self.events_total = 0
        self.tx_count = 0
        self.rx_count = 0                      # messages that arrived (good or rejected)
        self.tx_skipped = 0
        self.tx_errors = 0
        self.tx_error: Optional[str] = None
        self.tx_late_max_ns = 0
        self.tx_bits = 0
        self.rx_bits_raw = 0                   # bits of the last good message
        self._next_tx = t0
        self._next_hk = t0
        self._next_emit = t0 + int(0.2 * NS)
        self._next_trip = t0 + int(s.trip_every_s * NS) if s.trip_every_s > 0 else None
        self._prev_tmb = 0
        self._overrun_pending = 0
        self.errors_seen = {"overrun": 0, "frame": 0, "parity": 0}
        self.hist = [0] * (len(HIST_EDGES_MS) + 1)
        self.gap_n = 0
        self.gap_sum = 0.0
        self.gap_max = 0.0
        self.gap_dev_sum = 0.0
        self.chips_tx: Deque[tuple] = deque(maxlen=600)
        self.chips_rx: Deque[tuple] = deque(maxlen=600)
        self.rx_chip_id = 0
        self.ids_seen: Dict[int, int] = {}
        # echo / round trip (spec 8)
        self.echo_tx = 0                        # value we currently send on the echo bit
        self.t_echo_tx: Optional[int] = None    # when we last changed it
        self.echo_changes = 0
        self.rtt_ms: List[float] = []           # every sample (thinned when huge)
        self.rtt_t: List[float] = []            # ... and when (seconds since start), same thinning
        self._rtt_stride = 1
        self._rtt_skip = 0
        self.rtt_chart: Deque[Tuple[float, float]] = deque(maxlen=600)
        self.rtt_n = 0
        self.rtt_alarm = 0
        self.rtt_alarm_events = 0
        self.rtt_last: Optional[float] = None
        self._rtt_prev: Optional[float] = None
        self._jit_sum = 0.0
        self._jit_n = 0
        # trips (bit transfer time probe)
        self.flips: List[dict] = []
        self.rmb_changes: Deque[dict] = deque(maxlen=20000)
        self._trip_count = 0
        self.rtt_per_trip: List[dict] = []
        self.trip_rows: List[dict] = []
        self._gen = getattr(self.link, "generation", 0)
        self._aligned = False
        self._user_mask = 0xFF & ~((1 << self.echo_idx) if self.echo_on else 0)       # the bits that are the user's (not the echo bit)
        self._logged_mask = sum(1 << i for i in range(8) if self.s.rmb_default[i]) & self._user_mask
        self.startup_junk = 0
        self._lag = 0
        self.host_stalls = 0
        self.started = False
        self.t_end: Optional[int] = None
        self.t_stop: Optional[int] = None
        self._rates = (0.0, 0.0)
        self._emit_prev = (t0, 0, 0)
        self._cpu0 = 0.0
        self.cpu_s = 0.0
        self._rok_before = 0

    @property
    def echo_idx(self) -> int:
        return self.s.echo_bit - 1

    @property
    def echo_on(self) -> bool:
        return self.s.echo_role != "off"

    def apply_settings(self, d: dict) -> None:
        """Settings that can change while running (counts, thresholds, labels, defaults): PU/DO, defaults, RBAD/CBAD, K, N, RTT_MAX, labels."""
        live_ok = ("rmb_pu", "rmb_do", "rmb_default", "rbad_pu_s", "cbad_pu_pct", "cbad_window_s", "k_timing", "late_floor_ms", "rok_n",
                   "rtt_max_ms", "labels", "trip_every_s", "trip_bit")
        new = Settings.from_dict({**self.s.to_dict(), **{k: v for k, v in d.items() if k in live_ok}})
        with self.lock:
            for k in live_ok:
                setattr(self.s, k, getattr(new, k))
            self.ch.late_ns, self.ch.lost_ns = self.s.late_limit_ns(), max(3 * self.nominal_ns, 2 * self.s.late_limit_ns())
            self.rx.pu, self.rx.do = list(self.s.rmb_pu), list(self.s.rmb_do)
            self.rx.default = [1 if x else 0 for x in self.s.rmb_default]
            self._next_trip = self.mono() + int(self.s.trip_every_s * NS) if self.s.trip_every_s > 0 else None

    # ================================================================ events
    def _t_s(self, t: int) -> float:
        return round((t - self.t0) / NS, 4)

    def log(self, t: int, kind: str, name: str, value, note: str = "") -> None:
        self.events.append((t, kind, name, value, note))
        self.events_total += 1

    def _on_chan_event(self, name: str, t: int, d: dict) -> None:
        up = {"rok": "ROK", "rbad": "RBAD", "cbad": "CBAD", "lbok": "LBOK", "lost": "LOST", "loopback": "LOOPBACK"}[name]
        note = d.get("reason") or (f"{d['pct']} % unavailable" if "pct" in d else "")
        self.log(t, "STATUS", up, d.get("value", 1), note)
        self.emit({"ev": "chan", "name": up, "value": d.get("value", 1), "t_s": self._t_s(t)})

    def emit(self, ev: dict) -> None:
        try:
            self._emit_cb(ev)
        except Exception:
            pass

    def _on_in_change(self, idx: int, v: int) -> None:
        """A contact input changed (UI switch, API, GPIO): remember when, for the bit transfer time."""
        t, tr = self.mono(), self.real()
        if len(self.flips) < FLIPS_KEEP:
            self.flips.append({"bit": idx + 1, "value": v, "t_mono_ns": t, "t_real_ns": tr})

    # ================================================================ lifecycle
    def begin(self, t_ns: Optional[int] = None) -> None:
        now = self.mono() if t_ns is None else t_ns
        with self.lock:
            self._reset(now)
            self.framer.reset()
            self.started = True
            if self.duration_s:
                self.t_end = now + int(self.duration_s * NS)
        self.log(now, "STATUS", "START", 1, f"{self.name} {self.link.describe()}")

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name=f"mb-{self.name}", daemon=True)
        self.thread.start()

    def stop(self, join: bool = True) -> None:
        self.stop_evt.set()
        if join and self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def run(self) -> None:
        sys.setswitchinterval(min(sys.getswitchinterval(), 0.001))     # the web server's threads must not hold the GIL for 5 ms in the middle of a 1 ms message cycle
        self._cpu0 = time.process_time()
        th0 = time.thread_time()
        self.running, self.error, self.status_text = True, None, "running"
        try:
            self.link.open()
            self.begin()
            while not self.stop_evt.is_set() and (self.t_end is None or self.mono() < self.t_end):
                self._cycle(True)
            self.status_text = "stopped" if self.stop_evt.is_set() else "finished"
        except Exception as e:  # noqa
            self.error, self.status_text = f"{type(e).__name__}: {e}", "failed"
        finally:
            self.t_stop = self.mono()
            self.cpu_s = time.thread_time() - th0
            self.running = False
            try:
                self.link.close()
            except Exception:
                pass

    def step(self) -> None:
        """One non-blocking pass (for tests with an injected clock)."""
        if not self.started:
            self.begin()
        self._cycle(False)

    # ================================================================ one pass
    def _cycle(self, block: bool) -> None:
        now = self.mono()
        self._tx_step(now)
        self._release(now)
        wait = max(0, self._next_wake(now) - now) / NS if block else 0.0
        chunks = self.link.recv(wait)
        if chunks:
            with self.lock:
                self._rx(chunks)
        self._housekeeping(self.mono())

    def _next_wake(self, now: int) -> int:
        w = [self._next_tx, self._next_hk]
        d = self.imp.next_due()
        if d is not None:
            w.append(d)
        if self.ch.rok and self.ch.last_arrival is not None:
            w.append(self.ch.last_arrival + self.ch.late_ns + 1)
        if self.t_end is not None:
            w.append(self.t_end)
        return min(w)

    # ---------------------------------------------------------------- TX
    def _compose(self, now: int) -> int:
        bits = self.io.mask
        if self.echo_on:
            e = self.echo_idx
            role = self.s.echo_role
            rmb = self.rx.state[e]
            v = (1 - rmb) if role == "near" else rmb
            bits = (bits & ~(1 << e)) | (v << e)
            if v != self.echo_tx:
                self.echo_tx = v
                self.t_echo_tx = now
                self.echo_changes += 1
        return bits

    def _tx_step(self, now: int) -> None:
        if now < self._next_tx:
            self._lag = 0
            return
        sched = self._next_tx
        lag = now - sched
        self._lag = lag                      # how late THIS pass of our own loop is: a stall of the host, not of the link (see _candidate / _housekeeping)
        if lag > self.tx_late_max_ns:
            self.tx_late_max_ns = lag
        if lag > self.nominal_ns:
            self.host_stalls += 1
        self._next_tx = sched + self.nominal_ns if lag < 3 * self.nominal_ns else now + self.nominal_ns   # a long stall: do not burst to catch up
        if self._next_trip is not None and now >= self._next_trip:
            idx = self.s.trip_bit - 1
            if not (self.echo_on and idx == self.echo_idx):
                self.io.set_in(idx, 1 - self.io.ins[idx])
                self._trip_count += 1
            self._next_trip = now + int(self.s.trip_every_s * NS)
        bits = self._compose(now)
        chg = bits ^ self._prev_tmb
        if chg:
            for i in range(8):
                if (chg >> i) & 1 and not (self.echo_on and i == self.echo_idx):
                    self.log(now, "TMB", f"TMB{i + 1}", (bits >> i) & 1)
            self._prev_tmb = bits
        self.tx_bits = bits
        msg = self.codec.encode(bits, self.s.tx_id)
        self.tx_count += 1
        self.chips_tx.append((self.tx_count, now, bits))
        if self.imp.active() or self.imp.pending():
            self.imp.submit(msg, now)
            self._release(now)
        else:
            self._send(msg)

    def _release(self, now: int) -> None:
        if self.imp.pending():
            for m in self.imp.due(now):
                self._send(m)

    def _send(self, msg: bytes) -> None:
        try:
            n = self.link.send(msg)
        except OSError as e:
            self.tx_errors += 1
            self.tx_error = f"{type(e).__name__}: {e}"
            return
        if not n:
            self.tx_skipped += 1

    # ---------------------------------------------------------------- RX
    def _rx(self, chunks) -> None:
        link = self.link
        gen = getattr(link, "generation", 0)
        if gen != getattr(self, "_gen", 0):
            self._gen = gen
            self.framer.reset()
        errs = link.take_errors()
        if errs:
            for k, v in errs.items():
                self.errors_seen[k] = self.errors_seen.get(k, 0) + v
            self._overrun_pending += errs.get("overrun", 0)
        dg = getattr(link, "datagram", False)
        parmrk = getattr(link, "char_mode", "plain") == "parmrk"
        err_name = getattr(link, "err_name", "framing")
        n = self.codec.chars_per_msg
        for data, t_m, t_r in chunks:
            if dg:
                if len(data) != n:
                    self._reject(t_m, t_r, "framing", None)
                else:
                    self._candidate(C.Framer.plain(data), t_m, t_r)
                continue
            chars = self.framer.parse_parmrk(data, err_name) if parmrk else C.Framer.plain(data)
            for cand in self.framer.feed(chars):
                if isinstance(cand, tuple):          # characters that could not start a message: one framing reject per run
                    if not self._aligned:            # ... except the very first: joining a stream in the middle of a message is alignment, not an error
                        self._aligned = True
                        self.startup_junk += 1
                        continue
                    self._reject(t_m, t_r, "framing", None)
                else:
                    self._aligned = True
                    self._candidate(cand, t_m, t_r)

    def _candidate(self, chars: List[C.RxChar], t: int, t_r: int) -> None:
        self.rx_count += 1
        d = self.codec.decode(chars)
        ch = self.ch
        reason: Optional[str] = None
        loop_ok = False
        if self._overrun_pending:
            self._overrun_pending -= 1
            reason = "overrun"
        elif d.char_err:
            reason = d.char_err
        elif not d.ok_structure:
            reason = "framing"
        elif d.copies[0] != d.copies[1] or d.copies[0] != d.copies[2]:
            reason = "consistency"
        else:
            if self.s.loopback_test:
                if d.msg_id == self.s.tx_id:
                    loop_ok = True
                else:
                    reason = "address"
            elif d.msg_id == self.s.tx_id and d.msg_id != self.s.rx_id:
                reason = "loopback"                    # we hear ourselves
            elif d.msg_id != self.s.rx_id:
                reason = "address"
        # inter-arrival check (spec 3.4).  Only while ROK is 1: once a gap has already dropped ROK (a "missing" event) the message that ends the gap is
        # the link coming back and counts toward re-asserting ROK; rejecting it for the very gap that was already counted would punish it twice.
        # (the time our own loop was late in this pass is not the link's fault: data that waited in the port buffer while the host stalled is not "late")
        if reason is None and ch.rok and ch.last_arrival is not None and t - ch.last_arrival - self._lag > ch.late_ns:
            reason = "timing"
        if d.msg_id is not None:
            self.ids_seen[d.msg_id] = self.ids_seen.get(d.msg_id, 0) + 1
        if reason is not None:
            self._reject(t, t_r, reason, d.copies[0] if d.copies else None)
            return
        bits = d.copies[0]
        # histogram of the gap between consecutive VALID messages
        lg = ch.last_good
        if lg is not None:
            g = (t - lg) / 1e6
            self.hist[bisect.bisect_left(HIST_EDGES_MS, g)] += 1
            self.gap_n += 1
            self.gap_sum += g
            self.gap_dev_sum += abs(g - self.nominal_ns / 1e6)
            if g > self.gap_max:
                self.gap_max = g
        before = ch.rok
        rose = ch.good(t, loop_ok)
        self.rx_bits_raw = bits
        self.rx_chip_id += 1
        self.chips_rx.append((self.rx_chip_id, t, bits, CODE_OK))
        if ch.rok:
            changed = self.rx.update(bits)
            self._rmb_changed(changed, t, t_r, via_msg=True, rok_before=before)
            if rose:
                pass
        self.io.write_outs(self.rx.value)

    def _reject(self, t: int, t_r: int, reason: str, bits: Optional[int]) -> None:
        was = self.ch.rok
        self.ch.bad(t, reason)
        self.rx_chip_id += 1
        self.chips_rx.append((self.rx_chip_id, t, bits, reason))
        if was and not self.ch.rok:
            self._to_defaults(t, t_r)

    def _to_defaults(self, t: int, t_r: int) -> None:
        changed = self.rx.to_defaults()
        self._rmb_changed(changed, t, t_r, via_msg=False, rok_before=0)
        self.io.write_outs(self.rx.value)

    def _rmb_changed(self, changed: List[int], t: int, t_r: int, via_msg: bool, rok_before: int) -> None:
        for i in changed:
            echo = self.echo_on and i == self.echo_idx
            if echo:
                if via_msg and rok_before:
                    self._rtt_sample(t)
                continue
            if not via_msg:                          # ROK went 0: the bit falls back to its default (logged, but not a delivered change)
                self.log(t, "RMB", f"RMB{i + 1}", self.rx.state[i], "default (ROK=0)")
        if via_msg:
            self._sync_logged(t, t_r)

    def _sync_logged(self, t: int, t_r: int) -> None:
        """Log what a good message DELIVERED: a bit whose value differs from the last delivered value.  This is not the same as 'the output changed': after
        an outage the bits sit at their defaults, so a bit that was flipped back to its default while the channel was down never changes at the output, yet
        the new value did arrive.  Comparing with the last delivered value (not with the output) keeps bit transfer times honest through ROK chatter."""
        diff = (self.rx.value ^ self._logged_mask) & self._user_mask
        if not diff:
            return
        for i in range(8):
            if (diff >> i) & 1:
                v = self.rx.state[i]
                self.log(t, "RMB", f"RMB{i + 1}", v, "")
                self.rmb_changes.append({"bit": i + 1, "value": v, "t_mono_ns": t, "t_real_ns": t_r, "via_msg": True})
        self._logged_mask = (self._logged_mask & ~diff) | (self.rx.value & diff)

    def _rtt_sample(self, t: int) -> None:
        if self.s.echo_role != "near" or self.t_echo_tx is None:
            return
        if self.ch.rok_since is not None and self.t_echo_tx < self.ch.rok_since:
            return                                      # the TMB change was made before the link came back: not a round trip
        rtt = (t - self.t_echo_tx) / 1e6
        self.rtt_last = rtt
        self.rtt_n += 1
        if self._rtt_prev is not None:
            self._jit_sum += abs(rtt - self._rtt_prev)
            self._jit_n += 1
        self._rtt_prev = rtt
        self._rtt_skip += 1
        if self._rtt_skip >= self._rtt_stride:
            self._rtt_skip = 0
            self.rtt_ms.append(rtt)
            self.rtt_t.append(self._t_s(t))
            if len(self.rtt_ms) > RTT_KEEP:
                self.rtt_ms = self.rtt_ms[::2]
                self.rtt_t = self.rtt_t[::2]
                self._rtt_stride *= 2
        self.rtt_chart.append((self._t_s(t), round(rtt, 3)))
        if rtt > self.s.rtt_max_ms:
            self._set_alarm(t, rtt)
        elif self.rtt_alarm:
            self.rtt_alarm = 0
            self.log(t, "STATUS", "RTT_ALARM", 0, f"RTT {rtt:.1f} ms")

    def _set_alarm(self, t: int, rtt: Optional[float]) -> None:
        if not self.rtt_alarm:
            self.rtt_alarm = 1
            self.rtt_alarm_events += 1
            self.log(t, "STATUS", "RTT_ALARM", 1, (f"RTT {rtt:.1f} ms" if rtt is not None else "no echo transition") + f" > RTT_MAX {self.s.rtt_max_ms:g} ms")

    # ---------------------------------------------------------------- timers
    def _housekeeping(self, now: int) -> None:
        if now < self._next_hk:
            return
        self._next_hk = now + 10_000_000                     # 10 ms
        with self.lock:
            was = self.ch.rok
            self.ch.tick(now - self._lag)
            if was and not self.ch.rok:
                self.rx_chip_id += 1
                self.chips_rx.append((self.rx_chip_id, now, None, "missing"))
                self._to_defaults(now, self.real())
            if self.s.echo_role == "near" and self.ch.rok and self.t_echo_tx is not None and not self.rtt_alarm:
                ref = max(self.t_echo_tx, self.ch.rok_since or 0)
                if now - ref > int(self.s.rtt_max_ms * 1e6):
                    self._set_alarm(now, None)
        self.io.poll_gpio()
        if now >= self._next_emit:
            self._emit_state(now)

    def _emit_state(self, now: int) -> None:
        t_prev, tx_prev, rx_prev = self._emit_prev
        dt = max(1e-9, (now - t_prev) / NS)
        self._rates = ((self.tx_count - tx_prev) / dt, (self.ch.msgs_good - rx_prev) / dt)
        self._emit_prev = (now, self.tx_count, self.ch.msgs_good)
        self._next_emit = now + int(0.2 * NS)
        self.emit({"ev": "mb", "t_s": self._t_s(now), "tx_bits": self.tx_bits, "rx_bits": self.rx.value, "tx_rate": round(self._rates[0], 1),
                   "rx_rate": round(self._rates[1], 1), "rok": self.ch.rok, "rbad": self.ch.rbad, "cbad": self.ch.cbad,
                   "rtt_ms": self.rtt_last, "chan": "ok" if self.ch.rok else "failed" if self.ch.first_rok is not None else "waiting"})

    # ================================================================ controls
    def set_tmb(self, idx: int, value: int) -> None:
        """Flip contact input IN<idx+1> (it drives TMB<idx+1>).  The echo bit is reserved."""
        if self.echo_on and idx == self.echo_idx:
            raise ValueError(f"TMB{idx + 1} is reserved for the round-trip echo")
        self.io.set_in(idx, value)

    def set_impairment(self, cfg: dict) -> dict:
        r = self.imp.set(cfg, self.mono())
        c = self.imp.cfg
        desc = ", ".join(f"{k}={v:g}" if not isinstance(v, bool) else f"{k}={'on' if v else 'off'}" for k, v in c.items() if v) or "off"
        self.log(self.mono(), "IMPAIR", "IMPAIR", 1 if self.imp.active() else 0, desc)
        return r

    # ================================================================ views
    def _now(self) -> int:
        return self.t_stop if (self.t_stop is not None and not self.running) else self.mono()

    def health(self) -> str:
        if not self.started:
            return "waiting"
        if self.ch.first_rok is None:
            return "waiting"
        return "ok" if self.ch.rok else "failed"

    def histogram(self) -> dict:
        labels = [f"<{e:g}" for e in HIST_EDGES_MS] + [f">{HIST_EDGES_MS[-1]:g}"]
        return {"edges_ms": HIST_EDGES_MS, "labels": labels, "counts": list(self.hist), "n": self.gap_n,
                "avg_ms": round(self.gap_sum / self.gap_n, 3) if self.gap_n else None, "max_ms": round(self.gap_max, 3),
                "nominal_ms": round(self.nominal_ns / 1e6, 3),
                "mean_dev_ms": round(self.gap_dev_sum / self.gap_n, 3) if self.gap_n else None}

    def rtt_stats(self, full: bool = False) -> dict:
        """Live screens use the newest 5000 samples and a result cached for a second (sorting 200k floats on every poll stalled the message loop);
        the test summary uses all of them."""
        now = time.monotonic()
        if not full:
            c = getattr(self, "_rtt_cache", None)
            if c is not None and now - c[0] < 1.0:
                st = dict(c[1])
                st.update(last=None if self.rtt_last is None else round(self.rtt_last, 3), alarm=self.rtt_alarm, alarm_events=self.rtt_alarm_events, n=self.rtt_n)
                return st
        st = stats_ms(self.rtt_ms if full else self.rtt_ms[-5000:])
        if not full:
            self._rtt_cache = (now, dict(st))
        st["n"] = self.rtt_n
        st["jitter_ms"] = round(self._jit_sum / self._jit_n, 3) if self._jit_n else None
        st["one_way_est_ms"] = None if st["p50"] is None else round(st["p50"] / 2, 3)
        st["alarm"] = self.rtt_alarm
        st["alarm_events"] = self.rtt_alarm_events
        st["rtt_max_ms"] = self.s.rtt_max_ms
        st["last"] = None if self.rtt_last is None else round(self.rtt_last, 3)
        return st

    def counters(self) -> dict:
        ch = self.ch
        rej = dict(ch.rejects)
        return {"tx_msgs": self.tx_count, "rx_msgs": self.rx_count, "rx_good": ch.msgs_good, "rx_rejected": ch.msgs_rejected, "rejects": rej,
                "rok_drops": ch.rok_drops, "rbad_events": ch.rbad_events, "cbad_events": ch.cbad_events, "lost_events": ch.lost_events,
                "tx_skipped": self.tx_skipped, "tx_errors": self.tx_errors, "driver_errors": dict(self.errors_seen), "startup_junk": self.startup_junk}

    def status(self) -> dict:
        now = self._now()
        with self.lock:
            st = self.ch.stats(now)
        link = self.link
        ov = None
        if getattr(link, "char_mode", "plain") == "parmrk" and not link.overrun_available:
            ov = "not available on this port"
        d = {
            "name": self.name, "proto": self.s.proto, "running": self.running, "status": self.status_text, "health": self.health(),
            "link": link.describe(), "codec": self.codec.label, "tx_id": self.s.tx_id, "rx_id": self.s.rx_id, "baud": self.s.baud,
            "nominal_ms": round(self.nominal_ns / 1e6, 3), "late_limit_ms": round(self.ch.late_ns / 1e6, 3),
            "tx_bits": self.tx_bits, "tmb": self.io.mask, "rmb": self.rx.value, "outs": self.io.state()["outs"],
            "rok": st["rok"], "rbad": st["rbad"], "cbad": st["cbad"], "lbok": st["lbok"], "lost": st["lost"], "loopback": st["loopback"],
            "rtt_alarm": self.rtt_alarm, "echo_role": self.s.echo_role, "echo_bit": self.s.echo_bit if self.echo_on else None,
            "tx_rate": round(self._rates[0], 1), "rx_rate": round(self._rates[1], 1),
            "channel": st, "counters": self.counters(), "rtt": self.rtt_stats(), "overrun_note": ov,
            "impair": {**self.imp.cfg, "active": self.imp.active(), **self.imp.stats()},
            "settings": self.s.to_dict(), "io": self.io.state(), "error": self.error, "tx_error": self.tx_error,
            "tx_late_max_ms": round(self.tx_late_max_ns / 1e6, 3), "host_stalls": self.host_stalls, "elapsed_s": round((now - self.t0) / NS, 2) if self.started else 0,
            "echo_changes": self.echo_changes, "trips": self._trip_count,
        }
        return d

    def live(self, since_tx: int = 0, since_rx: int = 0, limit: int = 40) -> dict:
        """What the screen needs ~4x a second: lamps, numbers and a sample of the newest messages (id > since_*)."""
        d = self.status()
        def pick(dq, since, tx):
            rows = [r for r in list(dq) if r[0] > since]
            last = rows[-1][0] if rows else since
            if len(rows) > limit:
                step = len(rows) / float(limit)
                rows = [rows[int(i * step)] for i in range(limit)]
            if tx:
                return last, [{"id": r[0], "t_s": self._t_s(r[1]), "bits": r[2]} for r in rows]
            return last, [{"id": r[0], "t_s": self._t_s(r[1]), "bits": r[2], "bad": r[3] or None} for r in rows]
        d["tx_last"], d["tx_chips"] = pick(self.chips_tx, since_tx, True)
        d["rx_last"], d["rx_chips"] = pick(self.chips_rx, since_rx, False)
        d["rtt_chart"] = list(self.rtt_chart)[-200:]
        d["histogram"] = self.histogram()
        d["events"] = self.events_view(25)
        return d

    def events_view(self, n: int = 25) -> List[dict]:
        out = []
        for t, kind, name, value, note in list(self.events)[-n:]:
            out.append({"t_s": self._t_s(t), "kind": kind, "name": name, "value": value, "note": note})
        return out

    def events_csv(self) -> str:
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time_utc", "elapsed_s", "element", "value", "note"])
        for t, kind, name, value, note in list(self.events):
            wall = self.t0_real + (t - self.t0)
            sec, ns = divmod(wall, NS)
            iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(sec)) + f".{ns // 1_000_000:03d}Z"
            w.writerow([iso, f"{(t - self.t0) / NS:.3f}", name, value, note])
        return buf.getvalue()

    def reflect_events(self) -> dict:
        """What this end saw change at its outputs (for the far end to time bit transfers; needs synced clocks)."""
        return {"rmb_changes": list(self.rmb_changes)[-5000:], "flips": self.flips[-5000:], "t0_real_ns": self.t0_real}


def events_to_csv(rows: List[dict], started_at: str = "") -> str:
    """The saved event log of a finished run as CSV (elapsed seconds, since the run started)."""
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["elapsed_s", "element", "value", "note"])
    for r in rows:
        w.writerow([f"{r['t_s']:.3f}", r["name"], r["value"], r.get("note", "")])
    return buf.getvalue()


# ======================================================================= bit transfer time (flip IN -> OUT at the other end)
def transfer_times(flips: List[dict], far_changes: List[dict], tol_ms: float = 2.0, window_s: float = 5.0) -> List[dict]:
    """For every IN flip at one end, the first matching OUT change (same bit, same value) at the other end within `window_s`.
    Valid to the accuracy of the two clocks (the caller checks they are synced).  Returns [{"bit","t_ms" (transfer time),"lost"}]."""
    out = []
    by_bit: Dict[int, List[dict]] = {}
    for c in far_changes:
        if c.get("via_msg", True):
            by_bit.setdefault(c["bit"], []).append(c)
    tol = int(tol_ms * 1e6)
    win = int(window_s * 1e9)
    pos: Dict[int, int] = {}
    for f in flips:
        lst = by_bit.get(f["bit"], [])
        j = pos.get(f["bit"], 0)
        found = None
        while j < len(lst):
            c = lst[j]
            dt = c["t_real_ns"] - f["t_real_ns"]
            if dt < -tol:
                j += 1
                continue
            if dt > win:
                break
            if c["value"] == f["value"]:
                found = dt
                j += 1
                break
            j += 1
        if found is None:
            out.append({"bit": f["bit"], "t_ms": None, "lost": True})
        else:
            pos[f["bit"]] = j
            out.append({"bit": f["bit"], "t_ms": round(max(0, found) / 1e6, 3), "lost": False})
    return out


# ======================================================================= results
NOTE = "SimCodec: our own framing for emulator-to-emulator tests. Not SEL wire-compatible: it will not talk to a real SEL relay or SEL-2505."


def verdict(m: dict, threshold_ms: float, percentile: int, max_loss_pct: float, max_down_pct: float) -> dict:
    """Pass/fail: bit transfer time percentile vs threshold, ROK unavailability, no RBAD (plus lost trips)."""
    reasons: List[str] = []
    tr = m["transfer"]
    key = "max" if percentile in (100, "max") else f"p{int(percentile)}"
    val = tr.get(key)
    label = "The slowest bit" if key == "max" else f"{int(percentile)} % of bit changes"
    basis = "from one Pi to the other" if tr["basis"] == "one_way" else "(estimated as half the round trip of the echo bit)"
    if not tr["n"]:
        reasons.append("No bit change made it across - check the link, the settings at both ends (PROTO A and B, TX_ID / RX_ID, baud) and that the other Pi is running.")
    elif val is None:
        reasons.append(f"Percentile {percentile} is not available for this run.")
    elif val > threshold_ms:
        reasons.append(f"{label} took {val:g} ms {basis} - the limit is {threshold_ms:g} ms.")
    if tr.get("loss_pct", 0) > max_loss_pct:
        reasons.append(f"{tr['loss_pct']:g} % of the bit changes ({tr['lost']} of {tr['sent']}) did not show up at the far end - the limit is {max_loss_pct:g} %.")
    if m["unavail_pct"] > max_down_pct:
        reasons.append(f"ROK was 0 for {m['unavail_pct']:g} % of the time ({m['rok_drops']} drops, longest {m['longest_outage_ms']:g} ms) - the limit is {max_down_pct:g} %.")
    if m["rbad_events"]:
        reasons.append(f"RBAD asserted {m['rbad_events']} time(s): the channel was down for longer than RBAD_PU.")
    return {"pass": not reasons, "reasons": reasons}


def _summary(self: "Emulator", threshold_ms: float = 60.0, percentile: int = 99, max_loss_pct: float = 0.1, max_down_pct: float = 0.1,
             far: Optional[dict] = None, one_way_ok: bool = False, tol_ms: float = 2.0, far_status: Optional[dict] = None) -> dict:
    """The test result: top-level keys follow the other tests' summaries (rtt_ms, sent, received, lost, verdict ...); summary['mirrored'] has the rest."""
    now = self._now()
    with self.lock:
        st = self.ch.stats(now)
    flips_n = len(self.flips)
    note = None
    if far is not None and one_way_ok:
        rows = transfer_times(self.flips, far.get("rmb_changes") or [], tol_ms)
        tt = [r["t_ms"] for r in rows if not r["lost"]]
        lost, sent, basis = sum(1 for r in rows if r["lost"]), len(rows), "one_way"
    else:
        tt = [r / 2.0 for r in self.rtt_ms]
        lost, sent, basis = self.rtt_alarm_events, self.rtt_n, "rtt_half"
        if far is None:
            note = "The other Pi's outputs could not be read, so bit transfer time is estimated as half the round trip of the echo bit (TMB%d)." % self.s.echo_bit
        elif not one_way_ok:
            note = "The two Pis' clocks are not synced closely enough to time a bit from one Pi to the other, so bit transfer time is estimated as half the round trip of the echo bit (TMB%d)." % self.s.echo_bit
    tstats = stats_ms(tt)
    tstats.update({"basis": basis, "sent": sent, "lost": lost, "loss_pct": round(100.0 * lost / sent, 4) if sent else 0.0, "threshold_ms": threshold_ms,
                   "late": sum(1 for x in tt if x > threshold_ms), "flips": flips_n})
    elapsed = max(1e-9, (now - self.t0) / NS)
    outs = [{"start_s": self._t_s(a), "dur_ms": round((b - a) / 1e6, 3)} for a, b in self.ch.outages[-200:]]
    if self.ch._outage_start is not None and self.ch.first_rok is not None:
        outs.append({"start_s": self._t_s(self.ch._outage_start), "dur_ms": round((now - self.ch._outage_start) / 1e6, 3), "open": True})
    m = {
        "mode": "mirrored_bits", "emulator": 2, "codec": self.codec.label, "sel_compatible": self.codec.sel_compatible, "note": NOTE,
        "proto": self.s.proto, "name": self.name, "link": self.link.describe(), "baud": self.s.baud, "settings": self.s.to_dict(),
        "wire_period_ms": round(self.nominal_ns / 1e6, 4), "tx_msgs": self.tx_count, "tx_rate": round(self.tx_count / elapsed, 2),
        "rx_msgs": self.rx_count, "rx_good": st["msgs_good"], "rx_rate": round(self.rx_count / elapsed, 2), "rejects": st["rejects"],
        "rok_drops": st["rok_drops"], "rbad_events": st["rbad_events"], "cbad_events": st["cbad_events"], "lost_events": st["lost_events"],
        "unavail_pct": st["unavail_pct"], "availability_pct": st["availability_pct"], "down_pct": st["unavail_pct"], "longest_outage_ms": st["longest_outage_ms"],
        "outage_count": st["outage_count"], "rbad_total_s": st["rbad_total_s"], "first_rok_s": st["first_rok_s"], "outages": outs,
        "histogram": self.histogram(), "rtt": self.rtt_stats(True), "transfer": tstats, "echo": {"bit": self.s.echo_bit if self.echo_on else None, "role": self.s.echo_role, "cycles": self.echo_changes},
        "impair": {**self.imp.cfg, **self.imp.stats()}, "events_total": self.events_total, "one_way_note": note,
        "overrun_available": bool(self.link.overrun_available) if getattr(self.link, "char_mode", "plain") == "parmrk" else None,
        "driver_errors": dict(self.errors_seen), "tx_late_max_ms": round(self.tx_late_max_ns / 1e6, 3), "host_stalls": self.host_stalls, "tx_skipped": self.tx_skipped,
        "loopback_seen": st["loopback"], "ids_seen": dict(self.ids_seen), "events_log": self.events_view(5000), "echo_changes": self.echo_changes,
        "startup_junk": self.startup_junk,
    }
    if far_status:
        m["far"] = {k: far_status.get(k) for k in ("name", "proto", "rok", "rbad", "cbad", "tx_id", "rx_id", "baud", "counters", "channel", "link")}
    v = verdict(m, threshold_ms, percentile, max_loss_pct, max_down_pct)
    s = {"sent": sent, "received": tstats["n"], "lost": lost, "loss_pct": tstats["loss_pct"], "late": tstats["late"], "over_threshold": tstats["late"],
         "over_threshold_pct": round(100.0 * tstats["late"] / sent, 4) if sent else 0.0, "duplicates": 0, "out_of_order": 0, "stale": 0, "corrupted": 0,
         "rtt_ms": {k: tstats[k] for k in ("min", "p50", "p95", "p99", "max", "avg")}, "jitter_ms": m["rtt"].get("jitter_ms"), "stddev_ms": None,
         "threshold_ms": threshold_ms, "one_way": False, "basis": "rtt",      # the one-way figure lives in summary["mirrored"]["transfer"]
         
         "idle": {"gaps": [], "count": 0, "lost": 0, "max_ms": None, "over_threshold": 0}, "mirrored": m, "verdict": v}
    return s


Emulator.summary = _summary


# ======================================================================= internal back-to-back (two instances in one process)
def default_pair(a: Optional[dict] = None, b: Optional[dict] = None) -> Tuple[Settings, Settings]:
    sa = Settings.from_dict({"proto": "A", "tx_id": 1, "rx_id": 2, **(a or {})})
    sb = Settings.from_dict({"proto": "B", "tx_id": 2, "rx_id": 1, "trip_every_s": 0, **(b or {})})
    return sa, sb


def selftest(duration_s: float = 5.0, a: Optional[dict] = None, b: Optional[dict] = None, impair_a: Optional[dict] = None,
             impair_b: Optional[dict] = None, script: Optional[List[Tuple[float, Callable[["Emulator", "Emulator"], None]]]] = None,
             links: Optional[Tuple[Link, Link]] = None, emit_a: Callable[[dict], None] = lambda e: None) -> dict:
    """Two emulators in one process joined by an in-process wire (or any pair of links): PROTO A <-> PROTO B.
    `script` = [(seconds, fn(a, b))] actions at given times (flip a bit, break the link, ...).  Returns both summaries."""
    sa, sb = default_pair(a, b)
    la, lb = links or InternalLink.pair()
    ea, eb = Emulator(sa, la, "A (internal)", emit_a, duration_s=duration_s), Emulator(sb, lb, "B (internal)", duration_s=duration_s)
    if impair_a:
        ea.imp.set(impair_a, 0)
    if impair_b:
        eb.imp.set(impair_b, 0)
    eb.start()
    ea.start()
    t0 = time.monotonic()
    for at, fn in sorted(script or [], key=lambda x: x[0]):
        while time.monotonic() - t0 < at:
            time.sleep(0.005)
        fn(ea, eb)
    ea.thread.join(duration_s + 10)
    eb.stop()
    far = eb.reflect_events()
    sum_a = ea.summary(far=far, one_way_ok=True, far_status=eb.status())
    return {"a": ea, "b": eb, "summary": sum_a, "status_a": ea.status(), "status_b": eb.status()}
