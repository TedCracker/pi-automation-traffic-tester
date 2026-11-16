"""Latency statistics, message classification and pass/fail (docs/SPEC.md section 4)."""
from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence

OK, LATE, LOST = "ok", "late", "lost"


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """Nearest-rank percentile (p in 0..100)."""
    if not values:
        return None
    s = sorted(values)
    if p >= 100:
        return s[-1]
    k = max(1, math.ceil(p / 100.0 * len(s)))
    return s[k - 1]


def jitter(values_in_order: Sequence[float]) -> Optional[float]:
    """Mean absolute difference of consecutive values (RFC 3550-style IPDV)."""
    if len(values_in_order) < 2:
        return None
    d = [abs(b - a) for a, b in zip(values_in_order, values_in_order[1:])]
    return sum(d) / len(d)


def classify(rtt_us: Optional[int], threshold_us: int, timeout_us: int) -> str:
    if rtt_us is None or rtt_us > timeout_us:
        return LOST
    return LATE if rtt_us > threshold_us else OK


def _stats_ms(values_us: List[int]) -> Dict[str, Optional[float]]:
    if not values_us:
        return {"min": None, "mean": None, "p50": None, "p95": None, "p99": None, "p999": None, "max": None}
    f = lambda v: None if v is None else round(v / 1000.0, 3)
    return {"min": f(min(values_us)), "mean": f(sum(values_us) / len(values_us)),
            "p50": f(percentile(values_us, 50)), "p95": f(percentile(values_us, 95)),
            "p99": f(percentile(values_us, 99)), "p999": f(percentile(values_us, 99.9)), "max": f(max(values_us))}


class Accumulator:
    """Streaming result accumulator: keeps compact arrays, not per-message dicts
    (a 12 h soak at 100 ms is ~430k messages)."""

    def __init__(self, threshold_ms: float, basis: str = "rtt"):
        from array import array
        self.basis = basis                      # "rtt" = judged on the round trip, "up" = on the one-way delay from the sender to the reflector
        self.thr_us = int(threshold_ms * 1000)
        self.rtts = array("q")
        self.ups = array("q")
        self.downs = array("q")
        self.sent = self.lost = self.late = 0
        self._prev: Optional[int] = None
        self._jsum = 0
        self._jn = 0
        self.idle: List[dict] = []

    def add(self, m: dict) -> None:
        """m: seq, phase ('steady'|'idle'), idle_gap_s, rtt_us (None=lost), status, up_us, down_us."""
        if m["phase"] == "idle":
            self.idle.append({"gap_s": m.get("idle_gap_s"), "rtt_us": m.get("rtt_us"), "status": m["status"]})
            return
        self.sent += 1
        if m["status"] == LOST or m.get("rtt_us") is None:
            self.lost += 1
            return
        r = m["rtt_us"]
        if m["status"] == LATE:
            self.late += 1
        self.rtts.append(r)
        if self._prev is not None:
            self._jsum += abs(r - self._prev)
            self._jn += 1
        self._prev = r
        if m.get("up_us") is not None:
            self.ups.append(m["up_us"])
        if m.get("down_us") is not None:
            self.downs.append(m["down_us"])

    def summary(self, profile: dict, counters: Optional[dict] = None, one_way_ok: bool = False) -> dict:
        counters = counters or {}
        thr_us = self.thr_us
        rtts = list(self.rtts)
        sent, lost, late = self.sent, self.lost, self.late
        sd = statistics.pstdev(rtts) if len(rtts) > 1 else None
        j = (self._jsum / self._jn) if self._jn else None
        summary = {
            "sent": sent, "received": len(rtts), "lost": lost,
            "loss_pct": round(100.0 * lost / sent, 4) if sent else 0.0,
            "late": late, "over_threshold": late,
            "over_threshold_pct": round(100.0 * late / sent, 4) if sent else 0.0,
            "duplicates": counters.get("duplicates", 0),
            "out_of_order": counters.get("out_of_order", 0),
            "stale": counters.get("stale", 0),
            "corrupted": counters.get("corrupted", 0),
            "rtt_ms": _stats_ms(rtts),
            "jitter_ms": None if j is None else round(j / 1000.0, 3),
            "stddev_ms": None if sd is None else round(sd / 1000.0, 3),
            "threshold_ms": profile["threshold_ms"],
            "one_way": bool(one_way_ok),
            "basis": self.basis,
        }
        for k in ("extra",):
            if counters.get(k):
                summary[k] = counters[k]
        gaps: Dict[str, dict] = {}
        for m in self.idle:
            g = gaps.setdefault(str(m["gap_s"]), {"gap_s": m["gap_s"], "rtts": [], "lost": 0, "count": 0})
            g["count"] += 1
            if m["status"] == LOST or m["rtt_us"] is None:
                g["lost"] += 1
            else:
                g["rtts"].append(m["rtt_us"])
        idle_out = []
        for g in sorted(gaps.values(), key=lambda g: g["gap_s"] or 0):
            r = g.pop("rtts")
            idle_out.append({**g, "min_ms": None if not r else round(min(r) / 1000, 3),
                             "median_ms": None if not r else round(percentile(r, 50) / 1000, 3),
                             "max_ms": None if not r else round(max(r) / 1000, 3),
                             "over_threshold": sum(1 for x in r if x > thr_us)})
        idle_rtts = [m["rtt_us"] for m in self.idle if m["rtt_us"] is not None and m["status"] != LOST]
        summary["idle"] = {
            "gaps": idle_out, "count": len(self.idle),
            "lost": sum(1 for m in self.idle if m["status"] == LOST or m["rtt_us"] is None),
            "max_ms": None if not idle_rtts else round(max(idle_rtts) / 1000, 3),
            "over_threshold": sum(1 for x in idle_rtts if x > thr_us),
        }
        if one_way_ok or self.basis == "up":
            summary["up_ms"] = _stats_ms(list(self.ups))
            summary["down_ms"] = _stats_ms(list(self.downs))
        summary["verdict"] = verdict(summary, profile)
        return summary


def summarize(messages: List[dict], profile: dict, counters: Optional[dict] = None,
              one_way_ok: bool = False) -> dict:
    acc = Accumulator(profile["threshold_ms"])
    for m in sorted(messages, key=lambda m: m["seq"]):
        acc.add(m)
    return acc.summary(profile, counters, one_way_ok)


def verdict(summary: dict, profile: dict) -> dict:
    """Pass/fail per profile rule. Returns {pass: bool, reasons: [plain sentences]}."""
    rule = profile.get("rule", {})
    pct = rule.get("percentile", 99)
    thr = profile["threshold_ms"]
    reasons: List[str] = []
    one_way = summary.get("basis") == "up"
    leg = summary.get("leg") or profile.get("leg_info") or {}
    rt = summary["up_ms"] if one_way and summary.get("up_ms") else summary["rtt_ms"]
    key = "max" if pct in (100, "max") else "p999" if pct == 99.9 else f"p{int(pct)}"
    val = rt.get(key)
    label = "The slowest message" if key == "max" else f"{pct:g} % of messages"
    if summary["sent"] == 0 or summary["received"] == 0:
        reasons.append("No replies were received - check the cable, port or address, and that the other Pi is on.")
    elif one_way and not summary.get("one_way"):
        reasons.append("The two Pis' clocks did not stay in step during the test, so the one-way delay cannot be trusted.")
    elif val is None:
        reasons.append(f"Percentile {pct} is not available for this run.")
    elif val > thr:
        if one_way:
            reasons.append(f"{label} took up to {val:g} ms to get from {leg.get('source') or 'the sender'} to {leg.get('dest') or 'the receiver'} - the limit is {thr:g} ms.")
        else:
            reasons.append(f"{label} arrived within {val:g} ms - the limit is {thr:g} ms.")
    max_loss = rule.get("max_loss_pct", 0.1)
    if summary["loss_pct"] > max_loss:
        reasons.append(f"{summary['loss_pct']:g} % of messages were lost - the limit is {max_loss:g} %.")
    if rule.get("include_idle", True) and summary["idle"]["count"]:
        ithr = rule.get("idle_threshold_ms") or thr
        im = summary["idle"]["max_ms"]
        if summary["idle"]["lost"]:
            reasons.append(f"{summary['idle']['lost']} message(s) sent after a quiet period were lost.")
        if im is not None and im > ithr:
            reasons.append(f"The first message after a quiet period took {im:g} ms - the limit is {ithr:g} ms.")
    return {"pass": not reasons, "reasons": reasons}


def interval_summary(rows: List[dict]) -> dict:
    """rows: finished steady messages in one interval."""
    rtts = [r["rtt_us"] for r in rows if r["status"] != LOST and r["rtt_us"] is not None]
    return {"sent": len(rows), "received": len(rtts),
            "lost": sum(1 for r in rows if r["status"] == LOST),
            "late": sum(1 for r in rows if r["status"] == LATE),
            "p50_us": percentile(rtts, 50), "p95_us": percentile(rtts, 95),
            "p99_us": percentile(rtts, 99), "max_us": max(rtts) if rtts else None}
