"""Tester lamp (docs/SPEC.md 7.6): is a Pi, not the link, limiting or spoiling the measurement?

One line under the Run banner, three states:
  ok     "Pi OK"                nothing on the Pis themselves stands in the way of a trustworthy number
  warn   "Check the Pi"         something on a Pi may colour the numbers (busy processor, Wi-Fi power saving, slow USB adapter, clocks apart ...)
  limit  "The Pi is the limit"  the Pi could not do what the test asked (could not send at the asked rate, dropped packets it received)
Every reason names the Pi. Pure functions and one small timeline class: no threads, no network."""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional

LEVELS = ("ok", "warn", "limit")
TITLES = {"ok": "Pi OK", "warn": "Check the Pi", "limit": "The Pi is the limit"}
SENT_SHARE_LIMIT = 0.93          # sent less than 93 % of the asked rate -> the Pi is the limit (same figure as the generator's own warning)
CPU_WARN_PCT = 85.0
LATE_MIN_MS = 2.0                # sending that ran late by less than this is never reported
LATE_SHARE = 0.2                 # ... or by less than 20 % of the sending interval


def _rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


# ---------------------------------------------------------------- processor load (whole Pi, all cores)
_cpu_lock = threading.Lock()
_cpu_last: Optional[tuple] = None
_cpu_val: Optional[float] = None
_cpu_at = 0.0


def _read_stat() -> Optional[tuple]:
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        v = [int(x) for x in parts[1:9]]
        idle = v[3] + v[4]
        return sum(v), idle
    except (OSError, ValueError, IndexError):
        return None


def cpu_percent(min_gap_s: float = 0.5) -> Optional[float]:
    """Share of processor time in use since the previous call (None the first time, or when it cannot be read). Calls closer together than
    `min_gap_s` return the last figure, so several tests asking at once do not shorten each other's window."""
    global _cpu_last, _cpu_val, _cpu_at
    with _cpu_lock:
        now = time.monotonic()
        if _cpu_last is not None and now - _cpu_at < min_gap_s:
            return _cpu_val
        cur = _read_stat()
        if cur is None:
            return None
        prev, _cpu_last, _cpu_at = _cpu_last, cur, now
        if prev is None:
            return _cpu_val
        dt, di = cur[0] - prev[0], cur[1] - prev[1]
        if dt <= 0:
            return _cpu_val
        _cpu_val = round(max(0.0, min(100.0, 100.0 * (dt - di) / dt)), 1)
        return _cpu_val


def cores() -> int:
    return os.cpu_count() or 1


# ---------------------------------------------------------------- reasons
def _num(v) -> str:
    return f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}"


def assess(i: Dict) -> List[dict]:
    """What is wrong with ONE Pi right now. `i` (every key optional):
      pi                       its name
      asked_pps, sent_pps      rate the test asked for / what the Pi really sent (any unit, same for both); unit = "messages/s" | "packets/s"
      late_ms, interval_ms     how late the sending ran against its schedule (worst in the last second) and the sending interval
      cpu                      processor in use, %
      rx_drops                 packets this Pi received and dropped (receive buffer full)
      one_way, clock_offset_ms, max_clock_offset_ms
      wifi_power_save, wifi_iface
      ftdi_latency_ms          the USB-serial adapter's hold time
    -> [{"level", "pi", "code", "text"}], worst first."""
    pi = str(i.get("pi") or "this Pi")
    out: List[dict] = []

    def add(level: str, code: str, text: str) -> None:
        out.append({"level": level, "pi": pi, "code": code, "text": text})

    asked, sent = i.get("asked_pps"), i.get("sent_pps")
    if asked and sent is not None and asked > 0 and sent < SENT_SHARE_LIMIT * asked:
        unit = i.get("unit") or "messages/s"
        add("limit", "rate", f"{pi} sent only {100.0 * sent / asked:.0f} % of the asked rate ({_num(sent)} of {_num(asked)} {unit})")
    drops = i.get("rx_drops")
    if drops:
        add("limit", "rx_drops", f"{pi}'s receive buffer dropped {int(drops):,} packets")
    late, interval = i.get("late_ms"), i.get("interval_ms")
    if late is not None and late > max(LATE_MIN_MS, LATE_SHARE * float(interval or 0)):
        add("warn", "late", f"{pi}: sending ran late by {_num(late)} ms")
    cpu = i.get("cpu")
    if cpu is not None and cpu > CPU_WARN_PCT:
        add("warn", "cpu", f"{pi}: processor at {cpu:.0f} %")
    off, maxo = i.get("clock_offset_ms"), i.get("max_clock_offset_ms")
    if i.get("one_way") and off is not None and maxo and abs(off) > maxo:
        add("warn", "clock", f"{pi}: clock {_num(abs(off))} ms apart (one-way timing needs {maxo:g} ms or less)")
    if i.get("wifi_power_save"):
        add("warn", "wifi_ps", f"{pi}: Wi-Fi power saving is on{(' (' + str(i['wifi_iface']) + ')') if i.get('wifi_iface') else ''}; it adds wake-up delay")
    fl = i.get("ftdi_latency_ms")
    if fl is not None and fl > 1:
        add("warn", "ftdi", f"{pi}: the USB adapter holds data for up to {int(fl)} ms")
    out.sort(key=lambda r: -_rank(r["level"]))
    return out


def merge(groups: List[List[dict]]) -> List[dict]:
    """Reasons of several Pis / links as one list: one entry per (Pi, kind of problem), worst first."""
    seen: Dict[tuple, dict] = {}
    for g in groups:
        for r in g or []:
            k = (r.get("pi"), r.get("code"))
            if k not in seen or _rank(r["level"]) > _rank(seen[k]["level"]):
                seen[k] = r
    return sorted(seen.values(), key=lambda r: -_rank(r["level"]))


def level_of(reasons: List[dict]) -> str:
    return max((r["level"] for r in reasons), key=_rank, default="ok")


def line(reasons: List[dict], ok_text: str = "") -> dict:
    """The lamp as shown: {"level", "title", "text", "reasons"}."""
    lv = level_of(reasons)
    if lv == "ok":
        return {"level": "ok", "title": TITLES["ok"], "text": ok_text, "reasons": []}
    top = [r["text"] for r in reasons[:2]]
    more = len(reasons) - len(top)
    return {"level": lv, "title": TITLES[lv], "text": "; ".join(top) + (f" (+{more} more)" if more > 0 else ""), "reasons": reasons}


def _clock(s: float) -> str:
    s = int(max(0, round(s)))
    return f"{s // 60}:{s % 60:02d}"


class Lamp:
    """The lamp over the length of one test: the state now, and the periods in which it was not OK (kept with the result)."""

    HOLD_S = 3.0                 # a reason stays on this long after it was last seen, so the line does not flicker

    def __init__(self, max_periods: int = 40):
        self.max_periods = max_periods
        self.periods: List[dict] = []                  # {"from_s", "to_s", "level", "pi", "code", "text"}
        self._open: Dict[tuple, dict] = {}
        self._last_seen: Dict[tuple, float] = {}
        self.now: dict = line([])
        self._sig = None
        self.lock = threading.Lock()

    def update(self, t_s: float, reasons: List[dict], ok_text: str = "") -> Optional[dict]:
        """Feed the reasons seen at `t_s` seconds into the test. Returns the lamp line when it CHANGED (to send to the screen), else None."""
        with self.lock:
            live = {}
            for r in reasons or []:
                k = (r.get("pi"), r.get("code"))
                live[k] = r
                self._last_seen[k] = t_s
                if k not in self._open:
                    self._open[k] = {"from_s": round(t_s, 1), "to_s": round(t_s, 1), "level": r["level"], "pi": r.get("pi"), "code": r.get("code"), "text": r["text"]}
                o = self._open[k]
                o["to_s"] = round(t_s, 1)
                if _rank(r["level"]) >= _rank(o["level"]):
                    o["level"], o["text"] = r["level"], r["text"]
            for k in list(self._open):
                if k not in live and t_s - self._last_seen.get(k, t_s) > self.HOLD_S:
                    self._close(k)
            shown = sorted((live.get(k) or {"level": o["level"], "pi": o["pi"], "code": o["code"], "text": o["text"]} for k, o in self._open.items()),
                           key=lambda r: -_rank(r["level"]))
            cur = line(shown, ok_text)
            sig = (cur["level"], cur["text"])
            self.now = cur
            if sig != self._sig:
                self._sig = sig
                return cur
            return None

    def _close(self, k: tuple) -> None:
        o = self._open.pop(k, None)
        if o is not None and len(self.periods) < self.max_periods:
            self.periods.append(o)

    def summary(self, total_s: Optional[float] = None) -> dict:
        """For the stored result: {"level", "title", "text", "periods"}; text = "Pi OK the whole run" or "pi2: sending ran late by 12 ms from 2:10 to 2:25"."""
        with self.lock:
            periods = sorted(self.periods + list(self._open.values()), key=lambda p: (-_rank(p["level"]), p["from_s"]))
        if not periods:
            return {"level": "ok", "title": TITLES["ok"], "text": "Pi OK the whole run", "periods": []}
        lv = max((p["level"] for p in periods), key=_rank)
        parts = []
        for p in periods[:3]:
            whole = total_s is not None and p["from_s"] <= 2.0 and p["to_s"] >= total_s - 3.0
            parts.append(p["text"] + (" the whole run" if whole else f" from {_clock(p['from_s'])} to {_clock(p['to_s'])}"))
        more = len(periods) - len(parts)
        return {"level": lv, "title": TITLES[lv], "text": "; ".join(parts) + (f" (+{more} more)" if more > 0 else ""),
                "periods": [dict(p) for p in sorted(periods, key=lambda p: p["from_s"])]}
