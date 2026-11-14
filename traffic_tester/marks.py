"""Mark and "since mark" (docs/SPEC.md 7.5): a flag dropped during a test ("antenna moved"), and the numbers counted again from that flag.
Nothing is deleted and the verdict is always for the whole test: this is the safe form of a "clear statistics" button."""
from __future__ import annotations

from typing import List, Optional

from . import metrics as M

KEEP = 20000                 # delays kept for the since-mark percentiles (the newest)
MAX_MARKS = 50
NAME_MAX = 40


def clean_name(name) -> str:
    return " ".join(str(name or "").split())[:NAME_MAX]


class Since:
    """A trip test's numbers since the last mark, fed with the live `msg` events of one link."""

    def __init__(self, t_s: float):
        self.t_s = round(float(t_s), 1)
        self.sent = self.lost = self.late = 0
        self.vals: List[float] = []
        self.max: Optional[float] = None
        self.last: Optional[float] = None

    def add(self, e: dict) -> None:
        if e.get("phase") not in (None, "steady"):
            return
        self.sent += 1
        if e.get("status") == "lost":
            self.lost += 1
            return
        v = e.get("rtt_ms")
        if v is None:
            return
        if e.get("status") == "late":
            self.late += 1
        self.last = v
        self.max = v if self.max is None else max(self.max, v)
        self.vals.append(v)
        if len(self.vals) > KEEP:
            del self.vals[:len(self.vals) - KEEP]

    def view(self) -> dict:
        v = self.vals
        return {"t_s": self.t_s, "sent": self.sent, "lost": self.lost, "late": self.late, "now_ms": self.last,
                "p50_ms": round(M.percentile(v, 50), 2) if v else None, "p99_ms": round(M.percentile(v, 99), 2) if v else None,
                "max_ms": None if self.max is None else round(self.max, 2)}


class SinceCounters:
    """The same for tests that report running totals (generator, Mirrored Bits): the totals at the mark are subtracted."""

    KEYS = ("sent", "received", "lost", "late", "tx_pkts", "rx_pkts")

    def __init__(self, t_s: float, base: Optional[dict]):
        self.t_s = round(float(t_s), 1)
        self.base = {k: (base or {}).get(k) or 0 for k in self.KEYS}
        self.n = 0
        self.rx_sum = self.tx_sum = 0.0
        self.lat_max: Optional[float] = None

    def view(self, e: dict) -> dict:
        out = {"t_s": self.t_s}
        for k in self.KEYS:
            if e.get(k) is not None:
                out[k] = max(0, e[k] - self.base[k])
        if e.get("ev") == "gstate":
            self.n += 1
            self.rx_sum += e.get("rx_mbps") or 0.0
            self.tx_sum += e.get("tx_mbps") or 0.0
            if e.get("lat_ms") is not None:
                self.lat_max = e["lat_ms"] if self.lat_max is None else max(self.lat_max, e["lat_ms"])
            out.update(rx_mbps=round(self.rx_sum / self.n, 2), tx_mbps=round(self.tx_sum / self.n, 2), lat_max_ms=self.lat_max)
        return out


def mark_at(marks: List[dict], t_s: Optional[float]) -> str:
    """The name of the mark in force at `t_s` seconds into the test ("" before the first one): the CSV column."""
    if t_s is None:
        return ""
    cur = ""
    for i, m in enumerate(marks or []):
        if t_s >= m.get("t_s", 0):
            cur = m.get("name") or f"Mark {i + 1}"
    return cur
