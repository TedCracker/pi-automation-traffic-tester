"""Settings, receive-side channel status (ROK / RBAD / CBAD / LBOK) and per-bit security counts.  Pure logic: every time is passed in
(monotonic ns), nothing reads a clock, so the unit tests drive it exactly."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Callable, Deque, Dict, List, Optional, Tuple

NS = 1_000_000_000
REASONS = ("parity", "framing", "overrun", "consistency", "crc", "address", "timing")   # spec 3 / 8; "loopback" is counted next to them

# Setting names are LABELS: SEL models call them differently, so the screen lets you rename them.  Keys never change.
DEFAULT_LABELS = {
    "proto": "PROTO", "baud": "BAUD", "tx_id": "TX_ID", "rx_id": "RX_ID", "rbad_pu_s": "RBAD_PU", "cbad_pu_pct": "CBAD_PU",
    "cbad_window_s": "CBAD window", "rok_n": "ROK after N good", "k_timing": "K (timing)", "rtt_max_ms": "RTT_MAX",
    "rtscts": "Hardware handshake", "stopbits": "Stop bits", "echo_bit": "Echo bit",
}


@dataclass
class Settings:
    proto: str = "A"                    # PROTO A / B: the two ends must be opposite
    codec: str = "sim"
    baud: int = 38400
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1                   # 2 = the extra stop bit some equipment needs (10-bit characters)
    rtscts: bool = False
    tx_id: int = 1
    rx_id: int = 2
    rate_mode: str = "max"              # "max" = back to back at the baud rate, "per_cycle" = capped: messages per power-system cycle
    line_hz: int = 60
    per_cycle: int = 4
    k_timing: float = 1.5               # a message later than K x the nominal interval is rejected (timing)
    late_floor_ms: float = 5.0          # ... but never tighter than this: a non-real-time Linux host cannot honour 1 ms
    rok_n: int = 2                      # ASSUMPTION (spec 4): ROK re-asserts after N consecutive good messages
    rbad_pu_s: float = 10.0
    cbad_pu_pct: float = 10.0
    cbad_window_s: float = 60.0
    rmb_pu: List[int] = field(default_factory=lambda: [1] * 8)
    rmb_do: List[int] = field(default_factory=lambda: [1] * 8)
    rmb_default: List[int] = field(default_factory=lambda: [0] * 8)
    loopback_test: bool = False         # loopback test mode: our own TX comes back to our RX and is expected (LBOK)
    echo: str = "auto"                  # "auto" (PROTO A = near, B = far) | "near" | "far" | "off"
    echo_bit: int = 8                   # 1..8, reserved for the round-trip echo
    rtt_max_ms: float = 40.0
    trip_bit: int = 1                   # the bit the test flips on a schedule (the "bit transfer time" probe)
    trip_every_s: float = 2.0           # 0 = no scheduled flips
    labels: Dict[str, str] = field(default_factory=dict)
    role_names: Dict[str, str] = field(default_factory=dict)      # optional display name of each end, by PROTO ("A": "Substation relay ...", "B": "DER relay ...")

    # ---- derived
    @property
    def echo_role(self) -> str:
        from .codec import get_codec
        if get_codec(self.codec).trip_frame:
            return "off"                # frame codec: the echo travels in the frame (sequence + timestamp), so no bit is reserved
        if self.echo == "auto":
            return "near" if self.proto == "A" else "far"
        return self.echo if self.echo in ("near", "far") else "off"

    @property
    def char_bits(self) -> int:
        return 1 + self.bytesize + (0 if self.parity == "N" else 1) + self.stopbits

    def label(self, key: str) -> str:
        return self.labels.get(key) or DEFAULT_LABELS.get(key, key)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["echo_role"] = self.echo_role
        return d

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Settings":
        base = cls()
        for k, v in (d or {}).items():
            if hasattr(base, k) and k not in ("echo_role", "char_bits"):
                setattr(base, k, v)
        base.validate()
        return base

    def validate(self) -> None:
        if self.proto not in ("A", "B"):
            raise ValueError("PROTO must be A or B")
        if not (self.baud > 0):
            raise ValueError("baud must be positive")
        if self.parity not in ("N", "E", "O"):
            raise ValueError("parity must be N, E or O")
        if self.stopbits not in (1, 2):
            raise ValueError("stop bits must be 1 or 2")
        if self.bytesize not in (7, 8):
            raise ValueError("data bits must be 7 or 8")
        for k in ("tx_id", "rx_id"):
            if not 0 <= getattr(self, k) <= 31:
                raise ValueError(f"{k} must be 0..31")
        if self.tx_id == self.rx_id and not self.loopback_test:
            raise ValueError("TX_ID and RX_ID must be different: a message that carries our own TX_ID is how a loop-back is recognised (spec 3.3)")
        if not (1 <= self.rok_n <= 1000):
            raise ValueError("ROK after N must be 1..1000")
        if self.k_timing < 1.0:
            raise ValueError("K must be at least 1")
        if not 1 <= self.echo_bit <= 8 or not 1 <= self.trip_bit <= 8:
            raise ValueError("bit numbers are 1..8")
        for k in ("rmb_pu", "rmb_do", "rmb_default"):
            v = getattr(self, k)
            if len(v) != 8:
                raise ValueError(f"{k} needs 8 values")
        if any(int(x) < 1 for x in self.rmb_pu + self.rmb_do):
            raise ValueError("PU / DO counts must be at least 1")
        if self.rate_mode not in ("max", "per_cycle"):
            raise ValueError("rate mode must be max or per_cycle")
        if self.line_hz not in (50, 60) or self.per_cycle < 1:
            raise ValueError("power-system frequency 50 or 60 Hz, at least 1 message per cycle")

    def nominal_ns(self) -> int:
        """Interval between messages we send (and expect to receive: both ends are assumed to use the same rate)."""
        from .codec import get_codec
        wire = int(round(get_codec(self.codec).chars_per_msg * self.char_bits * NS / float(self.baud)))
        if self.rate_mode == "per_cycle":
            return max(wire, int(NS / (self.line_hz * self.per_cycle)))
        return wire

    def late_limit_ns(self) -> int:
        return max(int(self.k_timing * self.nominal_ns()), int(self.late_floor_ms * 1e6))


def proto_warning(a_proto: str, b_proto: str) -> Optional[str]:
    if a_proto == b_proto:
        return f"Both ends are set to PROTO {a_proto}. The two ends of a Mirrored Bits channel must be opposite (one A, one B)."
    return None


class RxBits:
    """RMB1..8 with per-bit security counts (spec 5).  0->1 only after PU consecutive good messages showing 1, 1->0 only after DO showing 0.
    While ROK=0 every bit sits at its default and the counts restart; after recovery updates run through the counts again."""

    def __init__(self, pu: List[int], do: List[int], default: List[int]):
        self.pu, self.do, self.default = list(pu), list(do), [1 if x else 0 for x in default]
        self.state = list(self.default)
        self._cnt = [0] * 8

    def to_defaults(self) -> List[int]:
        """ROK went 0: every bit to its default.  Returns the indexes that changed."""
        ch = [i for i in range(8) if self.state[i] != self.default[i]]
        self.state = list(self.default)
        self._cnt = [0] * 8
        return ch

    def update(self, bits: int) -> List[int]:
        """A good message while ROK=1.  Returns indexes that changed."""
        ch = []
        for i in range(8):
            v = (bits >> i) & 1
            if v == self.state[i]:
                self._cnt[i] = 0
                continue
            self._cnt[i] += 1
            if self._cnt[i] >= (self.pu[i] if v else self.do[i]):
                self.state[i] = v
                self._cnt[i] = 0
                ch.append(i)
        return ch

    @property
    def value(self) -> int:
        return sum(1 << i for i in range(8) if self.state[i])


class Channel:
    """ROK / lost channel / RBAD / CBAD / LBOK for one receive direction.

    Events (name, t_ns, detail) go to `on_event`; names: rok, rbad, cbad, lost, loopback, lbok, echo_alarm (the engine adds the others)."""

    def __init__(self, s: Settings, t0_ns: int, on_event: Callable[[str, int, dict], None] = lambda *a: None):
        self.s, self.on_event = s, on_event
        self.nominal_ns = s.nominal_ns()
        self.late_ns = s.late_limit_ns()
        self.lost_ns = max(3 * self.nominal_ns, 2 * self.late_ns)   # spec 4: at least 1 valid message per 3 messages' worth of time
        self.t0 = t0_ns
        self.rok = 0
        self.rbad = 0
        self.cbad = 0
        self.lbok = 0
        self.lost = 0                    # no valid message for 3 messages' worth of time
        self.loopback_seen = 0           # latched: we heard our own TX_ID
        self.ok_run = 0
        self.last_arrival: Optional[int] = None      # last message of any kind (good or rejected)
        self.last_good: Optional[int] = None
        self.rok0_since: Optional[int] = t0_ns
        self.first_rok: Optional[int] = None
        self.rok_since: Optional[int] = None
        # counters
        self.rejects: Dict[str, int] = {r: 0 for r in REASONS}
        self.rejects["loopback"] = 0
        self.rejects["missing"] = 0      # time ran out with no message at all (not a received message)
        self.msgs_good = 0
        self.msgs_rejected = 0
        self.rok_drops = 0               # ROK 1 -> 0
        self.rbad_events = 0
        self.cbad_events = 0
        self.lost_events = 0
        self.rbad_total_ns = 0
        self.longest_outage_ns = 0
        self._outage_start: Optional[int] = None
        self.outages: List[Tuple[int, int]] = []     # closed ROK=0 periods after first ROK (kept: last 2000)
        self._down: Deque[Tuple[int, int]] = deque()  # closed ROK=0 intervals for the rolling window
        self._down_sum = 0
        self._rbad_since: Optional[int] = None

    # ------------------------------------------------------------ events in
    def good(self, t: int, loopback_ok: bool = False) -> bool:
        """A message that passed every check.  Returns True when ROK just went 1."""
        self.msgs_good += 1
        self.last_arrival = self.last_good = t
        if self.lost:
            self.lost = 0
        self.ok_run += 1
        if loopback_ok:
            if not self.lbok:
                self.lbok = 1
                self.on_event("lbok", t, {"value": 1})
        rose = False
        if not self.rok and self.ok_run >= self.s.rok_n:
            self._rok_up(t)
            rose = True
        return rose

    def bad(self, t: int, reason: str, arrived: bool = True) -> bool:
        """A rejected message.  Returns True when ROK just went 0."""
        self.rejects[reason] = self.rejects.get(reason, 0) + 1
        self.msgs_rejected += 1
        if arrived:
            self.last_arrival = t
        if reason == "loopback" and not self.loopback_seen:
            self.loopback_seen = 1
            self.on_event("loopback", t, {"value": 1})
        if reason != "loopback" and self.s.loopback_test and self.lbok:
            self.lbok = 0
            self.on_event("lbok", t, {"value": 0})
        self.ok_run = 0
        return self._rok_down(t, reason)

    def tick(self, t: int) -> None:
        """Time passing: missing messages, lost channel, RBAD, CBAD."""
        if self.last_arrival is None:
            ref = self.t0
        else:
            ref = self.last_arrival
        if self.rok and t - ref > self.late_ns:
            self.rejects["missing"] += 1
            self.ok_run = 0
            self._rok_down(ref + self.late_ns, "missing")
        elif not self.rok and self.ok_run and t - ref > self.late_ns:
            self.ok_run = 0                  # spec 4: N CONSECUTIVE good messages. A message went missing while ROK was 0: the count starts again
        lg = self.last_good if self.last_good is not None else self.t0
        if not self.lost and t - lg > self.lost_ns:
            self.lost = 1
            self.lost_events += 1
            self.on_event("lost", lg + self.lost_ns, {"value": 1})
        if not self.rok and self.rok0_since is not None and not self.rbad and (t - self.rok0_since) >= int(self.s.rbad_pu_s * NS):
            self.rbad = 1
            self.rbad_events += 1
            self._rbad_since = self.rok0_since + int(self.s.rbad_pu_s * NS)
            self.on_event("rbad", self._rbad_since, {"value": 1})
        self._cbad_eval(t)

    # ------------------------------------------------------------ transitions
    def _rok_up(self, t: int) -> None:
        self.rok = 1
        self.rok_since = t
        if self.first_rok is None:
            self.first_rok = t
        else:
            self._close_down(t)
        self.rok0_since = None
        if self.rbad:
            self.rbad = 0
            self.rbad_total_ns += max(0, t - (self._rbad_since or t))
            self.on_event("rbad", t, {"value": 0})
        self.on_event("rok", t, {"value": 1})
        self._cbad_eval(t)

    def _rok_down(self, t: int, reason: str) -> bool:
        if not self.rok:
            return False
        self.rok = 0
        self.rok0_since = t
        self.rok_drops += 1
        self._outage_start = t
        self.on_event("rok", t, {"value": 0, "reason": reason})
        self._cbad_eval(t)
        return True

    def _close_down(self, t: int) -> None:
        s = self._outage_start
        self._outage_start = None
        if s is None:
            return
        self.longest_outage_ns = max(self.longest_outage_ns, t - s)
        self.outages.append((s, t))
        if len(self.outages) > 2000:
            del self.outages[0]
        self._down.append((s, t))
        self._down_sum += t - s

    # ------------------------------------------------------------ CBAD: rolling unavailability
    def window_down_ns(self, t: int) -> int:
        w0 = t - int(self.s.cbad_window_s * NS)
        while self._down and self._down[0][1] <= w0:
            a, b = self._down.popleft()
            self._down_sum -= b - a
        tot = self._down_sum
        if self._down and self._down[0][0] < w0:
            tot -= w0 - self._down[0][0]
        if self._outage_start is not None and self.first_rok is not None:
            tot += t - max(self._outage_start, w0)
        return max(0, tot)

    def unavailability_pct(self, t: int) -> float:
        w = int(self.s.cbad_window_s * NS)
        if self.first_rok is None:           # never up. The start-up seconds do not count, but a whole window without ROK is 100 % unavailable (spec 4)
            return 100.0 if t - self.t0 >= w else 0.0
        span = max(min(w, t - self.first_rok), min(w, NS))
        return 100.0 * self.window_down_ns(t) / span if span > 0 else 0.0

    def _cbad_eval(self, t: int) -> None:
        pct = self.unavailability_pct(t)
        if not self.cbad and pct > self.s.cbad_pu_pct:
            self.cbad = 1
            self.cbad_events += 1
            self.on_event("cbad", t, {"value": 1, "pct": round(pct, 2)})
        elif self.cbad and pct <= self.s.cbad_pu_pct:
            self.cbad = 0
            self.on_event("cbad", t, {"value": 0, "pct": round(pct, 2)})

    # ------------------------------------------------------------ whole-run numbers
    def down_ns_total(self, t: int) -> int:
        """ROK=0 time since the first ROK."""
        if self.first_rok is None:
            return 0
        tot = sum(b - a for a, b in self.outages)
        if self._outage_start is not None:
            tot += t - self._outage_start
        return tot

    def stats(self, t: int) -> dict:
        up_since = self.first_rok
        span = (t - up_since) if up_since is not None else 0
        down = self.down_ns_total(t)
        longest = self.longest_outage_ns
        if self._outage_start is not None and up_since is not None:
            longest = max(longest, t - self._outage_start)
        return {
            "rok": self.rok, "rbad": self.rbad, "cbad": self.cbad, "lbok": self.lbok, "lost": self.lost, "loopback": self.loopback_seen,
            "msgs_good": self.msgs_good, "msgs_rejected": self.msgs_rejected, "rejects": dict(self.rejects),
            "rok_drops": self.rok_drops, "rbad_events": self.rbad_events, "cbad_events": self.cbad_events, "lost_events": self.lost_events,
            "unavail_pct": round(100.0 * down / span, 4) if span > 0 else 0.0,
            "availability_pct": round(100.0 - 100.0 * down / span, 4) if span > 0 else 0.0,
            "window_unavail_pct": round(self.unavailability_pct(t), 3),
            "longest_outage_ms": round(longest / 1e6, 3), "outage_count": len(self.outages) + (1 if self._outage_start is not None and up_since is not None else 0),
            "rbad_total_s": round((self.rbad_total_ns + ((t - self._rbad_since) if self.rbad and self._rbad_since else 0)) / NS, 3),
            "first_rok_s": None if up_since is None else round((up_since - self.t0) / NS, 3),
            "ok_time_s": round(max(0, span - down) / NS, 3), "down_time_s": round(down / NS, 3),
        }
