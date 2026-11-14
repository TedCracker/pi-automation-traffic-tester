"""In-app impairment injector, between the codec and the transport (works for serial AND IP; separate from the Linux netem bad-link
simulator and usable without it).  Pure scheduling: submit() a message with the current time, due() what may go out now."""
from __future__ import annotations

import heapq
import random
from typing import List, Optional, Tuple

NS = 1_000_000_000
DEFAULTS = {"latency_ms": 0.0, "jitter_ms": 0.0, "drop_pct": 0.0, "burst_len_ms": 0.0, "burst_every_s": 0.0, "bit_error_pct": 0.0, "break_link": False}


class Impairer:
    def __init__(self, rng: Optional[random.Random] = None):
        self.cfg = dict(DEFAULTS)
        self.rng = rng or random.Random()
        self._q: List[Tuple[int, int, bytes]] = []
        self._n = 0
        self._last_release = 0
        self._burst_t0: Optional[int] = None
        self.dropped = 0
        self.corrupted = 0
        self.delayed = 0
        self.passed = 0
        self.burst_drops = 0

    # ------------------------------------------------------------ config
    def set(self, cfg: dict, now_ns: int = 0) -> dict:
        new = dict(self.cfg)
        for k, v in (cfg or {}).items():
            if k in DEFAULTS:
                new[k] = bool(v) if k == "break_link" else float(v)
        for k in ("latency_ms", "jitter_ms", "burst_len_ms", "burst_every_s"):
            if new[k] < 0:
                raise ValueError(f"{k} cannot be negative")
        for k in ("drop_pct", "bit_error_pct"):
            if not 0 <= new[k] <= 100:
                raise ValueError(f"{k} must be 0..100")
        if (new["burst_len_ms"] > 0) != (new["burst_every_s"] > 0):
            raise ValueError("Set both the burst length and how often it happens (or neither)")
        if new["burst_every_s"] > 0 and new["burst_every_s"] * 1000 <= new["burst_len_ms"]:
            raise ValueError("The burst interval must be longer than the burst itself")
        was = self.active()
        self.cfg = new
        if new["burst_every_s"] > 0 and (self._burst_t0 is None or not was):
            self._burst_t0 = now_ns
        if new["burst_every_s"] <= 0:
            self._burst_t0 = None
        return dict(self.cfg)

    def active(self) -> bool:
        c = self.cfg
        return bool(c["latency_ms"] or c["jitter_ms"] or c["drop_pct"] or c["bit_error_pct"] or c["break_link"] or (c["burst_len_ms"] and c["burst_every_s"]))

    def in_burst(self, now: int) -> bool:
        c = self.cfg
        if not (c["burst_len_ms"] > 0 and c["burst_every_s"] > 0) or self._burst_t0 is None:
            return False
        per = int(c["burst_every_s"] * NS)
        # the burst is the FIRST burst_len of every interval after the injector was armed, so "every 5 s, 1 s long" starts an outage right away
        return ((now - self._burst_t0) % per) < int(c["burst_len_ms"] * 1e6)

    # ------------------------------------------------------------ messages
    def submit(self, msg: bytes, now: int) -> None:
        c = self.cfg
        if c["break_link"]:
            self.dropped += 1
            return
        if self.in_burst(now):
            self.dropped += 1
            self.burst_drops += 1
            return
        if c["drop_pct"] and self.rng.random() * 100.0 < c["drop_pct"]:
            self.dropped += 1
            return
        if c["bit_error_pct"] and self.rng.random() * 100.0 < c["bit_error_pct"]:
            b = bytearray(msg)
            i = self.rng.randrange(len(b))
            b[i] ^= 1 << self.rng.randrange(8)
            msg = bytes(b)
            self.corrupted += 1
        delay = c["latency_ms"] + (self.rng.uniform(-c["jitter_ms"], c["jitter_ms"]) if c["jitter_ms"] else 0.0)
        rel = now + max(0, int(delay * 1e6))
        if rel < self._last_release:            # a link does not reorder: bunching, not overtaking
            rel = self._last_release
        self._last_release = rel
        if rel > now:
            self.delayed += 1
        self._n += 1
        heapq.heappush(self._q, (rel, self._n, msg))

    def due(self, now: int) -> List[bytes]:
        out = []
        q = self._q
        while q and q[0][0] <= now:
            out.append(heapq.heappop(q)[2])
            self.passed += 1
        return out

    def next_due(self) -> Optional[int]:
        return self._q[0][0] if self._q else None

    def pending(self) -> int:
        return len(self._q)

    def clear(self) -> None:
        self._q.clear()

    def stats(self) -> dict:
        return {"dropped": self.dropped, "burst_drops": self.burst_drops, "corrupted": self.corrupted, "delayed": self.delayed, "passed": self.passed}
