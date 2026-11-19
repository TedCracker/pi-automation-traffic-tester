"""Sniffer workbench engine: one stateful Analyzer turns packets into list rows (with Δ time, request-to-response time, retransmission flags and a
conversation key), a flow table summarises conversations, and OpenCapture loads a saved capture file so it can be filtered, decoded and saved again."""
from __future__ import annotations

import os
import threading
import time
from collections import Counter, OrderedDict
from typing import Dict, Iterable, List, Optional, Tuple

from . import analyze, display, pcapng

DATA_KEEP = 512                      # bytes kept per packet for the bytes pane
MAX_FLOWS = 3000


def _ep(r: dict, which: str) -> str:
    ip = r.get("sip" if which == "s" else "dip")
    pt = r.get("sport" if which == "s" else "dport")
    if ip:
        return f"{ip}:{pt}" if pt is not None else ip
    return r.get("src" if which == "s" else "dst") or ""


class Analyzer:
    """Feed packets in capture order with add(); each returns a row dict. Not thread safe: callers lock."""

    def __init__(self) -> None:
        self.n = 0
        self.flows: "OrderedDict[str, dict]" = OrderedDict()

    def add(self, ts_ns: int, data: bytes, linktype: int, comment: str = "", iface: str = "", orig_len: Optional[int] = None) -> dict:
        try:
            r = analyze.decode_packet(data, linktype, comment)
        except Exception:  # noqa - one undecodable packet must not stop the view
            r = analyze.malformed(data)
        self.n += 1
        r.pop("rng", None)
        chan = comment.split(";")[0][8:] if comment.startswith("channel ") else None
        row = {k: r.get(k) for k in ("proto", "src", "dst", "info", "errors", "sip", "dip", "sport", "dport", "smac", "dmac", "l4", "vlan")}
        row.update(n=self.n, t_ns=ts_ns, resp_ms=None, retrans=False, iface=iface, len=orig_len or len(data), linktype=linktype, errors=r.get("errors", 0) or 0,
                   crc_bad=("crc=FAIL" in comment) or bool(r.get("app") and not r["app"].get("crc_ok", True)))
        if chan:                                                     # a serial tap capture (frames wrapped as Ethernet / IP / TCP): the list shows the channel, like the live view
            row["channel_name"] = chan
            row["src"], row["dst"] = chan, ""
            m = [x.strip() for x in comment.split(";")]
            if len(m) > 1 and m[1]:
                row["proto"] = m[1]
        a, b = _ep(r, "s"), _ep(r, "d")
        if not (a or b):
            row["flow"] = f"{r.get('proto')}"
            fl = self._flow(row["flow"], row, a, b, r)
            row["delta_ms"], row["dir"] = self._delta(fl, ts_ns, 0), ""
            return row
        lo, hi = (a, b) if a <= b else (b, a)
        key = f"{r.get('l4') or r.get('proto')}|{lo}|{hi}"
        row["flow"] = key
        fl = self._flow(key, row, a, b, r)
        d = 0 if a == fl["a"] else 1
        row["dir"] = "→" if d == 0 else "←"
        row["delta_ms"] = self._delta(fl, ts_ns, d)
        fl["pk"] += 1
        fl["by"] += row["len"]
        fl["err"] += row["errors"]
        if r.get("app"):
            fl["app"][r["proto"]] += 1
        tcp = r.get("tcp")
        has_data = False
        if tcp:
            seq, plen, flags = tcp["seq"], tcp["plen"], tcp["flags"]
            has_data = plen > 0
            nxt = fl["next"][d]
            if plen > 0 and nxt is not None and ((seq - nxt) & 0xFFFFFFFF) > 0x7FFFFFFF and seq != nxt:
                row["retrans"] = True
                fl["retr"] += 1
                row["info"] = "Retransmission · " + row["info"]
            end = (seq + plen + (1 if flags & 0x03 else 0)) & 0xFFFFFFFF            # SYN and FIN take one sequence number
            if nxt is None or ((end - nxt) & 0xFFFFFFFF) < 0x7FFFFFFF:
                fl["next"][d] = end
        elif r.get("l4") == "udp" or r.get("app"):
            has_data = True
        if has_data and not row.get("retrans"):
            pend = fl["pend"]
            if pend and pend[0] != d:                                                # first data the other way after a request = its response
                row["resp_ms"] = round((ts_ns - pend[1]) / 1e6, 3)
                row["delta_ms"] = row["resp_ms"]
                fl["resp_n"] += 1
                fl["resp_sum"] += row["resp_ms"]
                fl["resp_max"] = max(fl["resp_max"], row["resp_ms"])
                fl["resp_last"] = row["resp_ms"]
                fl["resp_list"].append(row["resp_ms"])
                if len(fl["resp_list"]) > 500:
                    del fl["resp_list"][:100]
                fl["pend"] = None
            elif not pend:
                fl["pend"] = (d, ts_ns)
        return row

    def _delta(self, fl: dict, ts: int, d: int) -> Optional[float]:
        prev, fl["last"] = fl["last"], ts
        return round((ts - prev) / 1e6, 3) if prev else 0.0

    def _flow(self, key: str, row: dict, a: str, b: str, r: dict) -> dict:
        fl = self.flows.get(key)
        if fl is None:
            if len(self.flows) >= MAX_FLOWS:
                self.flows.popitem(last=False)
            fl = self.flows[key] = {"key": key, "a": a, "b": b, "l4": r.get("l4") or "", "proto": r.get("proto"), "pk": 0, "by": 0, "err": 0, "retr": 0, "app": Counter(), "last": 0,
                                    "next": [None, None], "pend": None, "resp_n": 0, "resp_sum": 0.0, "resp_max": 0.0, "resp_last": None, "resp_list": [], "t0": row["t_ns"], "dirs": set()}
            if not (a or b):
                fl["pk"] = 0
        fl["dirs"].add(0 if a == fl["a"] else 1)
        fl["t1"] = row["t_ns"]
        if not (a or b):
            fl["pk"] += 1
            fl["by"] += row["len"]
            fl["err"] += row["errors"]
        return fl

    # ------------------------------------------------------------ summaries
    def flow_rows(self, limit: int = 200) -> List[dict]:
        out = []
        for fl in self.flows.values():
            app = fl["app"].most_common(1)[0][0] if fl["app"] else (fl["proto"] or fl["l4"].upper())
            two = len(fl["dirs"]) > 1
            lbl = f"{_short(fl['a'], _eph(fl['a'], fl['b']))} {'⇄' if two else '→'} {_short(fl['b'], _eph(fl['b'], fl['a']))}" if fl["b"] else fl["proto"] or fl["a"]
            out.append({"key": fl["key"], "label": lbl, "a": fl["a"], "b": fl["b"], "proto": app, "packets": fl["pk"], "bytes": fl["by"], "errors": fl["err"], "retrans": fl["retr"],
                        "responses": fl["resp_n"], "resp_avg_ms": round(fl["resp_sum"] / fl["resp_n"], 1) if fl["resp_n"] else None,
                        "resp_max_ms": round(fl["resp_max"], 1) if fl["resp_n"] else None, "resp_last_ms": fl["resp_last"],
                        "resp_p95_ms": _pct(fl["resp_list"], 0.95), "t0": fl["t0"], "t1": fl.get("t1", fl["t0"])})
        out.sort(key=lambda x: (-x["errors"] - x["retrans"], -x["bytes"]))
        return out[:limit]


def _port(ep: str) -> Optional[int]:
    ip, _, p = ep.rpartition(":") if ep.count(":") == 1 else (ep, "", "")
    return int(p) if p.isdigit() else None


def _eph(ep: str, other: str) -> bool:
    """True when this end's port is a throw-away client port (high) and the other end's is a service port (low): the label leaves it out."""
    a, b = _port(ep), _port(other)
    return a is not None and b is not None and a >= 32768 and b < 32768


def _short(ep: str, drop_port: bool = False) -> str:
    """192.0.2.90:20000 -> .90:20000 (the last byte is enough to tell two devices on one subnet apart)."""
    ip, _, port = ep.rpartition(":") if ep.count(":") == 1 else (ep, "", "")
    if drop_port:
        port = ""
    base = ip if ip else ep
    if base.count(".") == 3:
        base = "." + base.rsplit(".", 1)[1]
    return base + (f":{port}" if port else "")


def _pct(xs: List[float], q: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(q * len(s)))], 1)


def protocol_counts(rows: Iterable[dict]) -> List[dict]:
    c: Counter = Counter()
    by: Counter = Counter()
    for r in rows:
        c[r["proto"]] += 1
        by[r["proto"]] += r["len"]
    return [{"proto": k, "packets": v, "bytes": by[k]} for k, v in c.most_common(20)]


def timing(rows: List[dict]) -> dict:
    """Quiet gaps and response-time spread over the rows given (already filtered)."""
    resp = [r["resp_ms"] for r in rows if r.get("resp_ms") is not None]
    gaps = sorted(rows, key=lambda r: -(r.get("delta_ms") or 0))[:5]
    t = [r["t_ns"] for r in rows if r.get("t_ns")]
    return {"responses": len(resp), "resp_min_ms": round(min(resp), 1) if resp else None, "resp_avg_ms": round(sum(resp) / len(resp), 1) if resp else None,
            "resp_p95_ms": _pct(resp, 0.95), "resp_max_ms": round(max(resp), 1) if resp else None,
            "longest_gaps": [{"n": r["n"], "delta_ms": r["delta_ms"], "flow": r["flow"]} for r in gaps if (r.get("delta_ms") or 0) > 0],
            "span_s": round((max(t) - min(t)) / 1e9, 3) if len(t) > 1 else 0.0,
            "buckets": _rate_buckets(t)}


def _rate_buckets(ts: List[int], n: int = 40) -> List[int]:
    if len(ts) < 2:
        return []
    lo, hi = min(ts), max(ts)
    span = max(1, hi - lo)
    b = [0] * n
    for x in ts:
        b[min(n - 1, int((x - lo) * n / span))] += 1
    return b


# ------------------------------------------------------------------ an opened capture file
class OpenCapture:
    """A saved capture loaded into rows, so it can be filtered and decoded exactly like the live view."""

    MAX_PACKETS = 200_000

    def __init__(self, cid: str, paths: List[str], title: str = ""):
        self.id, self.paths, self.title = cid, paths, title
        self.an = Analyzer()
        self.rows: List[dict] = []
        self.data: Dict[int, bytes] = {}
        self.raw: Dict[int, Tuple[int, int, bytes, str, str]] = {}    # n -> (ts, linktype, full data, comment, iface name) for saving
        self.truncated = False
        self.mtimes = tuple(os.path.getmtime(p) for p in paths if os.path.exists(p))
        self.lock = threading.Lock()
        self.load()

    def load(self) -> None:
        pk: List[dict] = []
        for path in self.paths:
            try:
                for p in pcapng.read_capture(path):
                    if len(pk) >= self.MAX_PACKETS:
                        self.truncated = True
                        break
                    pk.append(p)
            except OSError:
                continue
            if self.truncated:
                break
        pk.sort(key=lambda p: p["ts_ns"])                       # files of a ring buffer are in order already; this keeps merged files honest
        for p in pk:
            row = self.an.add(p["ts_ns"], p["data"], p["linktype"], p.get("comment", ""), p.get("name", ""), p.get("orig_len"))
            self.rows.append(row)
            self.data[row["n"]] = p["data"][:DATA_KEEP]
            self.raw[row["n"]] = (p["ts_ns"], p["linktype"], p["data"], p.get("comment", ""), p.get("name", ""))

    def view(self, filt: str = "", after: int = 0, limit: int = 400) -> dict:
        pred = display.compile_filter(filt)
        shown = [r for r in self.rows if pred(r)]
        page = [r for r in shown if r["n"] > after][:limit]
        return {"rows": page, "total": len(self.rows), "shown": len(shown), "errors": sum(1 for r in shown if r["errors"]), "truncated": self.truncated}

    def detail(self, n: int) -> Optional[dict]:
        row = next((r for r in self.rows if r["n"] == n), None) if n <= len(self.rows) else None
        if not row or n not in self.data:
            return None
        return packet_detail(row, self.data[n])

    def save_shown(self, filt: str, dest: str) -> int:
        """Write the packets the filter shows into a new pcapng file; returns how many."""
        pred = display.compile_filter(filt)
        ifs: List[Tuple[int, str, str]] = []
        idx: Dict[Tuple[int, str], int] = {}
        keep = [r for r in self.rows if pred(r)]
        for r in keep:
            ts, lt, _d, _c, nm = self.raw[r["n"]]
            if (lt, nm) not in idx:
                idx[(lt, nm)] = len(ifs)
                ifs.append((lt, nm or "capture", "opened capture"))
        if not keep:
            return 0
        w = pcapng.PcapngWriter(dest, ifs)
        for r in keep:
            ts, lt, d, c, nm = self.raw[r["n"]]
            w.write(idx[(lt, nm)], ts, d, c)
        w.close()
        return len(keep)


def packet_detail(row: dict, data: bytes) -> dict:
    out = {**row, "hex": data.hex(), "layers": analyze.layers(data, row["linktype"])}
    if row.get("channel_name"):
        out["layers"].insert(0, {"name": "Serial frame", "fields": {"Channel": row["channel_name"], "CRC": "FAILED" if row.get("crc_bad") else "ok",
                                                                    "Note": "The saved file wraps each serial frame as Ethernet / IP / TCP so Wireshark can open it. The bytes below are that wrapper."}})
    if row.get("resp_ms") is not None:
        out["layers"].insert(0, {"name": "Frame", "fields": {"Number": row["n"], "Length": f"{row['len']} bytes", "Response time": f"{row['resp_ms']} ms after the request"}, "range": [0, len(data)]})
    else:
        out["layers"].insert(0, {"name": "Frame", "fields": {"Number": row["n"], "Length": f"{row['len']} bytes", "Since previous in conversation": f"{row.get('delta_ms') or 0} ms"}, "range": [0, len(data)]})
    return out


_OPEN: "OrderedDict[str, OpenCapture]" = OrderedDict()
_OPEN_LOCK = threading.Lock()


def open_capture(key: str, paths: List[str], title: str = "") -> OpenCapture:
    """Cached: the same files (unchanged) are not read twice. At most two captures are kept in memory."""
    with _OPEN_LOCK:
        c = _OPEN.get(key)
        mt = tuple(os.path.getmtime(p) for p in paths if os.path.exists(p))
        if c and c.mtimes == mt:
            _OPEN.move_to_end(key)
            return c
    c = OpenCapture(key, paths, title)                               # read outside the lock: a large file takes a while
    with _OPEN_LOCK:
        _OPEN[key] = c
        while len(_OPEN) > 2:
            _OPEN.popitem(last=False)
    return c


def get_open(key: str) -> Optional[OpenCapture]:
    with _OPEN_LOCK:
        return _OPEN.get(key)


def save_filtered(paths: List[str], filt: str, dest: str, max_seconds: float = 120.0) -> Tuple[int, int]:
    """Stream the capture file(s) through the display filter and write only the packets it shows. Returns (packets read, packets written)."""
    pred = display.compile_filter(filt)
    an, ifs, idx, w = Analyzer(), [], {}, None
    seen = kept = 0
    t_end = time.monotonic() + max_seconds
    pk: List[dict] = []
    for path in paths:
        try:
            pk.extend(pcapng.read_capture(path))
        except OSError:
            continue
    pk.sort(key=lambda p: p["ts_ns"])
    # the interface list has to be known before the first write: collect it first (cheap, no decoding)
    for p in pk:
        k = (p["linktype"], p.get("name", ""))
        if k not in idx:
            idx[k] = len(ifs)
            ifs.append((p["linktype"], p.get("name") or "capture", "saved from the workbench"))
    if not pk:
        return 0, 0
    w = pcapng.PcapngWriter(dest, ifs)
    try:
        for p in pk:
            seen += 1
            row = an.add(p["ts_ns"], p["data"], p["linktype"], p.get("comment", ""), p.get("name", ""), p.get("orig_len"))
            if pred(row):
                w.write(idx[(p["linktype"], p.get("name", ""))], p["ts_ns"], p["data"], p.get("comment", ""))
                kept += 1
            if seen % 2000 == 0 and time.monotonic() > t_end:
                break
    finally:
        w.close()
    return seen, kept


def serial_flows(rows: List[dict]) -> List[dict]:
    """Flow table for a serial tap: one line per protocol on the line (there are no addresses to separate conversations)."""
    g: Dict[str, dict] = {}
    for r in rows:
        f = g.setdefault(r["proto"], {"key": r["proto"], "label": f"serial line · {r['proto']}", "a": "", "b": "", "proto": r["proto"], "packets": 0, "bytes": 0, "errors": 0, "retrans": 0, "resp": []})
        f["packets"] += 1
        f["bytes"] += r["len"]
        f["errors"] += r.get("errors", 0)
        if r.get("resp_ms") is not None:
            f["resp"].append(r["resp_ms"])
    out = []
    for f in g.values():
        rs = f.pop("resp")
        out.append({**f, "responses": len(rs), "resp_avg_ms": round(sum(rs) / len(rs), 1) if rs else None, "resp_max_ms": round(max(rs), 1) if rs else None, "resp_last_ms": rs[-1] if rs else None,
                    "resp_p95_ms": _pct(rs, 0.95)})
    return out


def serial_detail(row: dict) -> dict:
    """Detail of one frame from a live serial tap: Frame, the serial line, then the protocol layer (the raw serial bytes, not the Wireshark wrapper)."""
    raw = row.get("hex") or ""
    n = len(raw) // 2
    t = f"{row['resp_ms']} ms after the request" if row.get("resp_ms") is not None else f"{row.get('delta_ms') or 0} ms since the previous frame"
    layers = [{"name": "Frame", "fields": {"Number": row["n"], "Length": f"{row['len']} bytes", "Timing": t}, "range": [0, n]},
              {"name": "Serial frame", "fields": {"Channel": row.get("channel_name", ""), "CRC": "ok" if row.get("crc_ok", True) else "FAILED"}, "range": [0, n]},
              {"name": row["proto"], "fields": {"Summary": row["info"], **(row.get("fields") or {})}, "range": [0, n]}]
    return {**{k: v for k, v in row.items() if k != "fields"}, "layers": layers, "len": row["len"]}
