"""Generator packet format, size plans, latency histogram, sequence tracking (docs/SPEC.md section 13)."""
from __future__ import annotations

import math
import random
import struct
from typing import Dict, List, Optional, Tuple

MAGIC = 0x5447                       # 'TG'
VER = 1
FLAG_ECHO_REQ = 1
FLAG_ECHO_REPLY = 2
HDR = struct.Struct("<HBBBBHQQ")      # magic, ver, flags, stream, pad, length, seq, t_tx_ns  = 24 bytes
HDR_LEN = HDR.size
assert HDR_LEN == 24

ETH_HDR = 14
VLAN_LEN = 4
IP_HDR = 20
UDP_HDR = 8
IMIX = [64] * 7 + [594] * 4 + [1514] * 1      # "simple IMIX" 7 x 64, 4 x 594, 1 x 1518, counted WITHOUT the 4-byte frame check (1514 = largest frame for a 1500-byte MTU)
STD_MTU_FRAME = 1514
MAX_FRAME = 9000


def overhead(stype: str, vlan: bool = False) -> int:
    """Bytes of the chosen frame size that are NOT our payload."""
    return {"l2": ETH_HDR + (VLAN_LEN if vlan else 0), "ip": ETH_HDR + IP_HDR,
            "udp": ETH_HDR + IP_HDR + UDP_HDR, "tcp": 0}[stype]


def min_frame(stype: str, vlan: bool = False) -> int:
    """Smallest frame size (Ethernet bytes without FCS; for tcp = record bytes) that still holds our header."""
    m = overhead(stype, vlan) + HDR_LEN
    return max(m, 60) if stype != "tcp" else HDR_LEN


def size_table(mode: str, size: int, lo: int, hi: int, seed: int, stype: str, vlan: bool = False, n: int = 4096) -> List[int]:
    """Deterministic repeating table of frame sizes for a stream (clamped to what the stream type can carry)."""
    mn = min_frame(stype, vlan)
    if mode == "fixed":
        t = [size]
    elif mode == "range":
        rnd = random.Random(seed)
        t = [rnd.randint(min(lo, hi), max(lo, hi)) for _ in range(n)]
    elif mode == "imix":
        t = IMIX[:]
    else:
        raise ValueError(f"unknown size mode {mode}")
    return [min(MAX_FRAME, max(mn, x)) for x in t]


def mean_size(table: List[int]) -> float:
    return sum(table) / len(table)


def pack_header(buf, off: int, flags: int, stream: int, length: int, seq: int, t_tx: int) -> None:
    HDR.pack_into(buf, off, MAGIC, VER, flags, stream, 0, length, seq, t_tx)


def parse_header(buf, off: int) -> Optional[Tuple[int, int, int, int, int, int]]:
    """(flags, stream, length, seq, t_tx) or None."""
    magic, ver, flags, stream, _pad, length, seq, t_tx = HDR.unpack_from(buf, off)
    if magic != MAGIC or ver != VER:
        return None
    return flags, stream, length, seq, t_tx


def parse_mac(s: str) -> bytes:
    p = s.replace("-", ":").split(":")
    if len(p) != 6:
        raise ValueError(f"bad MAC address '{s}'")
    return bytes(int(x, 16) for x in p)


def l2_prefix(dst: bytes, src: bytes, ethertype: int, vlan: bool, vid: int, prio: int) -> bytes:
    h = dst + src
    if vlan:
        h += struct.pack(">HH", 0x8100, (prio << 13) | vid)
    return h + struct.pack(">H", ethertype)


# ------------------------------------------------------------------ latency histogram
class Hist:
    """Log histogram of microsecond values, 32 sub-buckets per power of two (~3 % resolution), 1 us .. > 10 s."""
    N = 704
    __slots__ = ("c", "n", "sum", "min", "max")

    def __init__(self) -> None:
        self.c = [0] * self.N
        self.n = 0
        self.sum = 0
        self.min = None
        self.max = None

    @staticmethod
    def idx(v: int) -> int:
        if v < 32:
            return 0 if v < 0 else v
        e = v.bit_length() - 6
        i = 32 * e + (v >> e)
        return i if i < Hist.N else Hist.N - 1

    @staticmethod
    def lower(i: int) -> int:
        if i < 32:
            return i
        e = (i >> 5) - 1
        return (32 + (i & 31)) << e

    def add(self, v_us: int) -> None:
        self.c[self.idx(v_us)] += 1
        self.n += 1
        self.sum += v_us
        if self.min is None or v_us < self.min:
            self.min = v_us
        if self.max is None or v_us > self.max:
            self.max = v_us

    def pct(self, p: float) -> Optional[int]:
        if not self.n:
            return None
        if p >= 100:
            return self.max
        target = max(1, math.ceil(self.n * p / 100.0))          # nearest rank
        acc = 0
        for i, c in enumerate(self.c):
            acc += c
            if acc >= target:
                lo = self.lower(i)
                hi = self.lower(i + 1) if i + 1 < self.N else lo
                v = (lo + hi) // 2 if hi > lo else lo
                return int(min(max(v, self.min), self.max))        # never outside the observed range
        return self.max

    def merge(self, other: "Hist") -> None:
        for i, c in enumerate(other.c):
            self.c[i] += c
        self.n += other.n
        self.sum += other.sum
        if other.min is not None and (self.min is None or other.min < self.min):
            self.min = other.min
        if other.max is not None and (self.max is None or other.max > self.max):
            self.max = other.max

    def stats_ms(self) -> Dict[str, Optional[float]]:
        if not self.n:
            return {"min": None, "avg": None, "p50": None, "p95": None, "p99": None, "max": None, "n": 0}
        f = lambda v: round(v / 1000.0, 3)
        return {"min": f(self.min), "avg": f(self.sum / self.n), "p50": f(self.pct(50)), "p95": f(self.pct(95)),
                "p99": f(self.pct(99)), "max": f(self.max), "n": self.n}

    def to_dict(self) -> dict:
        return {"c": {i: c for i, c in enumerate(self.c) if c}, "n": self.n, "sum": self.sum, "min": self.min, "max": self.max}

    @classmethod
    def from_dict(cls, d: dict) -> "Hist":
        h = cls()
        for i, c in d["c"].items():
            h.c[int(i)] = c
        h.n, h.sum, h.min, h.max = d["n"], d["sum"], d["min"], d["max"]
        return h


# ------------------------------------------------------------------ sequence tracking
class SeqTracker:
    """Unique / duplicate / out-of-order / missing from sequence numbers (per stream, receiver side)."""
    CAP = 200_000

    def __init__(self) -> None:
        self.max_seq = -1
        self.count = 0
        self.dup = 0
        self.ooo = 0
        self.missing: set = set()
        self.overflow = 0          # gaps too large to remember (always counted as lost)

    def see(self, seq: int) -> int:
        """0 = duplicate, 1 = new highest sequence (in order), 2 = unique but late (reordered)."""
        if seq > self.max_seq:
            gap = seq - self.max_seq - 1
            if gap > 0:
                if len(self.missing) + gap <= self.CAP:
                    self.missing.update(range(self.max_seq + 1, seq))
                else:
                    self.overflow += gap
            self.max_seq = seq
            self.count += 1
            return 1
        if seq in self.missing:
            self.missing.discard(seq)
            self.count += 1
            self.ooo += 1
            return 2
        self.dup += 1
        return 0

    @property
    def lost_so_far(self) -> int:
        return len(self.missing) + self.overflow
