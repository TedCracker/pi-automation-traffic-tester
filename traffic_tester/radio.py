"""Radio facts for serial-over-radio tests (the MDS Orbit and similar): what the radio's serial side was set to, how much data the test sends,
whether the radio bundles messages, and how the result changes by hour of day.  Pure functions and small accumulators: no network, no files.

The Pi never configures the radio.  It only records what the user says the radio was set to and looks at the data for signs of trouble."""
from __future__ import annotations

import datetime as dt
import time
from collections import Counter
from typing import Dict, List, Optional, Tuple

SERIAL_MODES = ["", "tcp_client", "tcp_server", "udp", "udp_multicast", "other"]
SERIAL_MODE_NAMES = {"": "not set", "tcp_client": "TCP client", "tcp_server": "TCP server", "udp": "UDP", "udp_multicast": "UDP multicast", "other": "other"}

HDR_UDP = 28          # IPv4 (20) + UDP (8)
HDR_TCP = 40          # IPv4 (20) + TCP (20); acknowledgements ride on the frames going the other way
CELL_ALLOW_PCT = 5.0  # allowance for cellular-side overhead (tunnelling, signalling) on top of the IP bytes: an assumption, said so on screen
NOT_RECORDED = "radio packetization not recorded"
FRAME_BYTES = 19      # the Trip frame (traffic_tester.mbemu.codec.TripCodec.LEN)
BIG = 1e9             # decimal GB / MB, as a carrier counts them


# ------------------------------------------------------------------ what the radio was set to
def radio_recorded(r: Optional[dict]) -> bool:
    r = r or {}
    return any(r.get(k) not in (None, "", 0) for k in ("char_timeout_ms", "max_packet_bytes", "serial_mode", "note"))


def radio_text(r: Optional[dict]) -> str:
    """One plain line for Results, the PDF and the exports."""
    if not radio_recorded(r):
        return NOT_RECORDED
    r = r or {}
    bits = []
    if r.get("char_timeout_ms") not in (None, ""):
        bits.append(f"character timeout {r['char_timeout_ms']:g} ms")
    if r.get("max_packet_bytes") not in (None, "", 0):
        bits.append(f"packet size {int(r['max_packet_bytes'])} bytes")
    if r.get("serial_mode"):
        bits.append(f"serial mode {SERIAL_MODE_NAMES.get(r['serial_mode'], r['serial_mode'])}")
    if r.get("note"):
        bits.append(f"note: {r['note']}")
    return "Radio serial packetization: " + ", ".join(bits)


def advice(baud: int = 19200, frame_bytes: int = FRAME_BYTES) -> dict:
    """What to set on the radio: one message per packet.  Character timeout about 2 character times, packet size = the frame."""
    char_ms = 10_000.0 / max(1, baud)                      # 8N1: ten bits per character
    return {"char_ms": round(char_ms, 3), "char_timeout_ms": round(2 * char_ms, 2), "max_packet_bytes": frame_bytes,
            "frame_ms": round(frame_bytes * char_ms, 2),
            "text": (f"Set the radio to send one message per packet: character timeout about 2 character times (about {2 * char_ms:.1f} ms at {baud} baud; "
                     f"one character is {char_ms:.2f} ms) and packet size = the frame size ({frame_bytes} bytes). The Pi does not change the radio's settings.")}


# ------------------------------------------------------------------ data usage
def usage_estimate(baud: int = 19200, frame_bytes: int = FRAME_BYTES, interval_ms: float = 10.0, serial_mode: str = "", frames_per_packet: int = 1) -> dict:
    """Data a nonstop stream uses, each way.  Payload = the serial bytes.  Over the air = payload + one IP/UDP (28 B) or IP/TCP (40 B) header per
    packet at `frames_per_packet` frames per packet, + CELL_ALLOW_PCT.  Both are shown: the payload figure is not what the carrier counts."""
    baud = max(300, int(baud))
    frame_bytes = max(1, int(frame_bytes))
    wire_fps = baud / (10.0 * frame_bytes)                     # the serial line cannot carry more frames than this
    fps = min(1000.0 / max(0.001, float(interval_ms)), wire_fps)
    fpp = max(1, int(frames_per_packet))
    pps = fps / fpp
    payload_bps = fps * frame_bytes
    day, mon = 86400.0, 30 * 86400.0

    def fig(bytes_per_s: float) -> dict:
        return {"bytes_per_s": round(bytes_per_s, 1), "mb_per_day": round(bytes_per_s * day / 1e6, 1), "gb_per_30_days": round(bytes_per_s * mon / BIG, 2),
                "both_ways_gb_per_30_days": round(2 * bytes_per_s * mon / BIG, 2)}

    over = {}
    for name, hdr in (("udp", HDR_UDP), ("tcp", HDR_TCP)):
        bps = (fps * frame_bytes + pps * hdr) * (1 + CELL_ALLOW_PCT / 100.0)
        over[name] = {**fig(bps), "header_bytes": hdr, "packets_per_s": round(pps, 1), "x_payload": round(bps / payload_bps, 2) if payload_bps else None}
    sel = "tcp" if serial_mode.startswith("tcp") else "udp"
    o = over[sel]
    pay = fig(payload_bps)
    text = (f"Payload (the serial bytes only): {payload_bps:,.0f} bytes/s each way = {pay['mb_per_day']:,.0f} MB/day = {pay['gb_per_30_days']:.1f} GB per 30 days each way. "
            f"Over the air at {fpp} frame per packet ({pps:.0f} packets/s), with {o['header_bytes']} bytes of {'IP/TCP' if sel == 'tcp' else 'IP/UDP'} header per packet and {CELL_ALLOW_PCT:g} % cellular overhead: "
            f"{o['bytes_per_s']:,.0f} bytes/s each way = {o['mb_per_day']:,.0f} MB/day = {o['gb_per_30_days']:.1f} GB per 30 days each way, {o['both_ways_gb_per_30_days']:.1f} GB for the radio's both directions "
            f"({o['x_payload']}x the payload figure). Check that the data plan covers the over-the-air figure; the payload figure alone is not what the carrier counts.")
    return {"baud": baud, "frame_bytes": frame_bytes, "frames_per_s": round(fps, 2), "frames_per_packet": fpp, "interval_ms": interval_ms, "selected": sel,
            "payload": pay, "over_the_air": over, "text": text,
            "assumptions": [f"{frame_bytes}-byte frame, 8N1 serial ({baud} baud = {wire_fps:.0f} frames/s at most)", f"one frame per packet" if fpp == 1 else f"{fpp} frames per packet",
                            f"IPv4 headers: UDP {HDR_UDP} B, TCP {HDR_TCP} B (acknowledgements ride on the frames going the other way)",
                            f"{CELL_ALLOW_PCT:g} % allowance for cellular overhead (an assumption)", "30 days; 1 GB = 1,000,000,000 bytes (as carriers count)"]}


def actual_usage(tx_frames: int, rx_frames: int, frame_bytes: int, elapsed_s: float, serial_mode: str = "", frames_per_packet: int = 1) -> dict:
    """Bytes this run really moved (frames x frame size) and the over-the-air figure for that many frames."""
    hdr = HDR_TCP if serial_mode.startswith("tcp") else HDR_UDP
    fpp = max(1, int(frames_per_packet))

    def air(n):
        return round((n * frame_bytes + (n / fpp) * hdr) * (1 + CELL_ALLOW_PCT / 100.0))

    return {"tx_bytes": int(tx_frames * frame_bytes), "rx_bytes": int(rx_frames * frame_bytes), "tx_frames": int(tx_frames), "rx_frames": int(rx_frames),
            "tx_over_air_bytes": air(tx_frames), "rx_over_air_bytes": air(rx_frames), "elapsed_s": round(elapsed_s, 1)}


# ------------------------------------------------------------------ does the radio bundle messages?
class GapStats:
    """Online record of how far apart good frames arrive (as a fraction of the sending period).  A radio that waits to fill a packet delivers frames in
    bursts: several arrive at the same instant, then a hole.  Bounded memory: a few counters."""
    NEAR = 0.25                                   # a gap under a quarter of the period counts as "at the same instant"

    def __init__(self):
        self.n = 0                                # gaps between consecutive good frames
        self.near = 0                             # of those, almost zero
        self.bursts: Counter = Counter()          # burst size -> how many (a burst = frames arriving together)
        self._cur = 1

    def add(self, gap_ns: float, nominal_ns: float) -> None:
        self.n += 1
        if gap_ns < self.NEAR * nominal_ns:
            self.near += 1
            self._cur += 1
        else:
            self._close()

    def _close(self) -> None:
        if self._cur >= 2:
            self.bursts[self._cur] += 1
        self._cur = 1

    def to_dict(self) -> dict:
        b = Counter(self.bursts)
        if self._cur >= 2:
            b[self._cur] += 1
        return {"n": self.n, "near_zero": self.near, "bursts": {str(k): v for k, v in sorted(b.items())}}


def rtt_step(rtts_ms: List[float], bin_ms: float = 1.0) -> Optional[dict]:
    """Do the round trips pile up at regular steps (e.g. 0, 10, 20, 30 ms above the fastest)?  Returns {step_ms, peaks} or None."""
    n = len(rtts_ms)
    if n < 300:
        return None
    lo = min(rtts_ms)
    c = Counter(int((x - lo) / bin_ms) for x in rtts_ms)
    top = max(c)
    cnt = [c.get(i, 0) for i in range(top + 1)]
    floor = max(5, 0.04 * n)
    peaks = [i for i in range(len(cnt)) if cnt[i] >= floor and cnt[i] >= (cnt[i - 1] if i else 0) and cnt[i] > (cnt[i + 1] if i + 1 < len(cnt) else 0)]
    merged: List[int] = []
    for i in peaks:                                            # a flat top counts once
        if merged and i - merged[-1] <= 1:
            continue
        merged.append(i)
    if len(merged) < 3:
        return None
    gaps = [b - a for a, b in zip(merged, merged[1:])]
    mean = sum(gaps) / len(gaps)
    if mean < 3 or max(gaps) - min(gaps) > max(1, 0.25 * mean):
        return None
    for a, b in zip(merged, merged[1:]):                       # a real valley between neighbours, not one smeared hump
        if min(cnt[a:b + 1]) > 0.5 * min(cnt[a], cnt[b]):
            return None
    if sum(cnt[p] for p in merged) < 0.4 * n:
        return None
    return {"step_ms": round(mean * bin_ms, 1), "peaks": len(merged)}


def detect_bundling(gaps: Optional[dict], rtts_ms: Optional[List[float]] = None, nominal_ms: float = 10.0) -> dict:
    """Plain verdict from GapStats.to_dict() (and the round-trip samples).  Suspected when many frames arrive together in bursts, or round trips sit
    at a fixed step.  A USB-serial adapter with a long latency timer can do the same, so the message names both."""
    g = gaps or {}
    n = int(g.get("n") or 0)
    out = {"suspected": False, "enough_data": n >= 300, "burst_pct": None, "mean_burst": None, "step_ms": None, "reasons": [], "message": ""}
    if n < 300:
        out["message"] = "Too few frames to tell whether the radio bundles messages."
        return out
    bursts = {int(k): v for k, v in (g.get("bursts") or {}).items()}
    nb = sum(bursts.values())
    in_burst = sum(k * v for k, v in bursts.items())
    out["burst_pct"] = round(100.0 * in_burst / (n + 1), 1)
    out["mean_burst"] = round(in_burst / nb, 1) if nb else None
    if out["burst_pct"] >= 30 and (out["mean_burst"] or 0) >= 2:
        out["reasons"].append(f"{out['burst_pct']:g} % of the frames arrived together in bursts of {out['mean_burst']:g} on average, with almost no gap between them")
    st = rtt_step(rtts_ms or [])
    if st:
        out["step_ms"] = st["step_ms"]
        out["reasons"].append(f"the round trips sit at steps of about {st['step_ms']:g} ms ({st['peaks']} steps)")
    out["suspected"] = bool(out["reasons"])
    out["message"] = (("The radio seems to be bundling messages — check its character timeout (and, on a USB-serial adapter, its latency timer). " + "; ".join(out["reasons"]).capitalize() + ".")
                      if out["suspected"] else "No sign of the radio bundling messages: frames arrived one at a time.")
    return out


# ------------------------------------------------------------------ time of day
def local_hour_start(t: float) -> float:
    lt = time.localtime(t)
    return t - (lt.tm_min * 60 + lt.tm_sec) - (t - int(t))


def pct_counts(c: Dict[int, int], n: int, p: float, res: float, vmax: float) -> Optional[float]:
    if not n:
        return None
    rank = max(1, -(-int(p * 1000) * n // 100000))
    acc = 0
    for i in sorted(c):
        acc += c[i]
        if acc >= rank:
            return min(vmax, (i + 1) * res)
    return vmax


class HourStats:
    """Round trips, lost frames and outages per clock hour (local time), kept as sparse 0.05 ms buckets: a week-long run stays a few hundred KB."""
    RES = 0.05

    def __init__(self):
        self.h: Dict[float, dict] = {}
        self._outs: set = set()
        self._cur: Optional[Tuple[float, float, dict]] = None

    def _bucket(self, wall: float) -> dict:
        cur = self._cur
        if cur is None or not (cur[0] <= wall < cur[1]):
            start = local_hour_start(wall)
            end = start + 3600
            b = self.h.setdefault(start, {"c": Counter(), "n": 0, "max": 0.0, "lost": 0, "outages": 0, "outage_ms": 0.0})
            self._cur = cur = (start, end, b)
        return cur[2]

    def add_rtt(self, wall: float, ms: float) -> None:
        b = self._bucket(wall)
        b["c"][int(ms / self.RES)] += 1
        b["n"] += 1
        if ms > b["max"]:
            b["max"] = ms

    def add_lost(self, wall: float, n: int) -> None:
        self._bucket(wall)["lost"] += n

    def add_outage(self, wall: float, dur_ms: float) -> None:
        if (round(wall, 3), round(dur_ms, 3)) in self._outs:          # asking twice (a live view, then the final summary) counts once
            return
        self._outs.add((round(wall, 3), round(dur_ms, 3)))
        b = self._bucket(wall)
        b["outages"] += 1
        b["outage_ms"] += dur_ms

    def report(self) -> dict:
        return hourly_report(self.h, self.RES)


def _row(c: Dict[int, int], n: int, mx: float, lost: int, outages: int, outage_ms: float, res: float) -> dict:
    r = lambda v: None if v is None else round(v, 2)
    sent = n + lost
    return {"n": n, "lost": lost, "lost_pct": round(100.0 * lost / sent, 4) if sent else 0.0, "p50": r(pct_counts(c, n, 50, res, mx)), "p99": r(pct_counts(c, n, 99, res, mx)),
            "p999": r(pct_counts(c, n, 99.9, res, mx)), "max": r(mx) if n else None, "outages": outages, "outage_ms": round(outage_ms, 1)}


def _merge(rows: List[dict]) -> Tuple[Counter, int, float, int, int, float]:
    c: Counter = Counter()
    n = lost = outages = 0
    mx = outage_ms = 0.0
    for b in rows:
        c.update(b["c"])
        n += b["n"]
        lost += b["lost"]
        outages += b["outages"]
        outage_ms += b["outage_ms"]
        mx = max(mx, b["max"])
    return c, n, mx, lost, outages, outage_ms


def hourly_report(h: Dict[float, dict], res: float = 0.05) -> dict:
    """{'hours': one row per clock hour of the run, 'by_hour_of_day': 24 rows (0-23, all days merged), 'weekday' / 'weekend': merged rows}."""
    hours = []
    by_hod: Dict[int, List[dict]] = {}
    wk: Dict[str, List[dict]] = {"weekday": [], "weekend": []}
    for start in sorted(h):
        b = h[start]
        lt = time.localtime(start)
        kind = "weekend" if lt.tm_wday >= 5 else "weekday"
        hours.append({"start": time.strftime("%Y-%m-%d %H:00", lt), "hour": lt.tm_hour, "day": time.strftime("%a", lt), "kind": kind, **_row(b["c"], b["n"], b["max"], b["lost"], b["outages"], b["outage_ms"], res)})
        by_hod.setdefault(lt.tm_hour, []).append(b)
        wk[kind].append(b)
    hod = [{"hour": k, **_row(*_merge(v), res)} for k, v in sorted(by_hod.items())]
    kinds = {k: _row(*_merge(v), res) for k, v in wk.items() if v}
    return {"hours": hours, "by_hour_of_day": hod, "kinds": kinds, "note": "The first and last hour of a run are usually partial: look at n (round trips) before comparing hours."}


def add_outages_wall(hs: HourStats, outages: List[Tuple[float, float]]) -> None:
    """outages = [(wall start seconds, duration ms)]."""
    for w, d in outages:
        hs.add_outage(w, d)


# ------------------------------------------------------------------ when it ran
BUSY = [("weekday", 7, 9, "7-9 am"), ("weekday", 16, 19, "4-7 pm")]          # busy hours on a cell network (an assumption, stated on screen)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def coverage(start: float, end: float) -> dict:
    """Start / end in local time, which busy windows the run covered, and whether it touched a weekend.  Covered = at least half of one window."""
    end = max(end, start)
    d0 = dt.datetime.fromtimestamp(start).date()
    d1 = dt.datetime.fromtimestamp(end).date()
    got = {"7-9 am": False, "4-7 pm": False}
    touch = {"7-9 am": False, "4-7 pm": False}
    weekend = False
    d = d0
    while d <= d1:
        if d.weekday() >= 5:
            if _overlap(start, end, time.mktime(d.timetuple()), time.mktime((d + dt.timedelta(days=1)).timetuple())) > 0:
                weekend = True
        else:
            for _, a, b, label in BUSY:
                w0 = time.mktime(dt.datetime(d.year, d.month, d.day, a).timetuple())
                w1 = time.mktime(dt.datetime(d.year, d.month, d.day, b).timetuple())
                ov = _overlap(start, end, w0, w1)
                if ov > 0:
                    touch[label] = True
                if ov >= 0.5 * (w1 - w0):
                    got[label] = True
        d += dt.timedelta(days=1)
    fmt = lambda t: time.strftime("%a %Y-%m-%d %H:%M", time.localtime(t))
    cov = [f"weekdays {k}" for k, v in got.items() if v] + (["weekend"] if weekend else [])
    missing = [f"weekdays {k}" for k, v in got.items() if not v] + ([] if weekend else ["weekend"])
    note = ("Busy hours covered: " + (", ".join(cov) if cov else "none")) + (" (not covered: " + ", ".join(missing) + ")" if missing else "") + ". Busy hours assumed: weekdays 7-9 am and 4-7 pm."
    h = time.localtime(start).tm_hour
    part = "night" if h < 6 else "morning" if h < 12 else "afternoon" if h < 18 else "evening"
    tag = f"{'weekend' if time.localtime(start).tm_wday >= 5 else 'weekday'} {part}"
    return {"start_local": fmt(start), "end_local": fmt(end), "start_epoch": round(start), "end_epoch": round(end), "tag": tag, "busy_7_9am": got["7-9 am"], "busy_4_7pm": got["4-7 pm"],
            "in_7_9am": touch["7-9 am"], "in_4_7pm": touch["4-7 pm"], "weekend": weekend, "note": note}
