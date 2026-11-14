"""Job info of a run, comparing results side by side, and the baseline of a saved test (docs/SPEC.md 7.4 and 7.5).

Pure functions over stored runs: nothing here talks to the network or to a running test.
  job       four optional words about the job a test belongs to (Site, Link / radio, By, Note), kept with every run of a test
  compare   2 to 4 results of the same kind next to each other: headline numbers, the change against the first one (A), one sentence, what
            differed in the setup, and the data for one overlaid chart
  baseline  one result per saved test (kind + test name) that later runs of the same test are measured against"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import metrics as M

# ---------------------------------------------------------------- job info
JOB_FIELDS = (("site", "Site", 80), ("link", "Link / radio", 80), ("by", "By", 60), ("note", "Note", 300))
JOB_KEYS = tuple(k for k, _, _ in JOB_FIELDS)


def clean_job(job: Any) -> dict:
    """{site, link, by, note} with only the filled-in fields, trimmed. Raises ValueError with a plain message."""
    if job is None or job == "" or job == {}:
        return {}
    if not isinstance(job, dict):
        raise ValueError("Job info is four optional texts: Site, Link / radio, By and Note.")
    out = {}
    for k, label, mx in JOB_FIELDS:
        v = job.get(k)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise ValueError(f"{label} must be text.")
        v = " ".join(str(v).split())
        if len(v) > mx:
            raise ValueError(f"{label} can be at most {mx} characters.")
        if v:
            out[k] = v
    return out


def job_line(job: Optional[dict]) -> str:
    """'Utility · Site A · by Alex' (the note is shown on its own)."""
    j = job or {}
    parts = [j.get("site"), j.get("link"), ("by " + j["by"]) if j.get("by") else None]
    return " · ".join(p for p in parts if p)


# ---------------------------------------------------------------- what kind of result, and its headline numbers
KIND_NAMES = {"trip": "trip test", "generator": "traffic generator test", "mirrored": "Mirrored Bits test", "sniffer": "capture"}
TRANSPORTS = {"serial": "Serial", "udp": "Ethernet · UDP", "tcp": "Ethernet · TCP", "goose_l2": "GOOSE · Layer 2", "goose_l3": "GOOSE · Layer 3 multicast"}


def kind_of(r: dict) -> str:
    s, p = r.get("summary") or {}, r.get("profile") or {}
    k = s.get("kind") or p.get("kind") or "trip"
    if k in ("generator", "sniffer"):
        return k
    if r.get("transport") in ("generator", "sniffer"):
        return r["transport"]
    if s.get("mirrored") or p.get("mode") == "mirrored":
        return "mirrored"
    return "trip"


def has_numbers(r: dict) -> bool:
    s = r.get("summary") or {}
    k = kind_of(r)
    if k == "generator":
        return bool((s.get("totals") or {}).get("tx_pkts") is not None and s.get("streams"))
    if k == "mirrored" and (s.get("mirrored") or {}).get("emulator"):
        return True
    return bool(s.get("rtt_ms")) and s.get("sent") is not None


def _trip_stats(s: dict) -> dict:
    return (s.get("up_ms") if s.get("basis") == "up" and s.get("up_ms") else s.get("rtt_ms")) or {}


def headline(r: dict) -> List[tuple]:
    """[(key, label, unit, value, better)] with better = 'lower' | 'higher' | None (a count that is neither good nor bad)."""
    s, k = r.get("summary") or {}, kind_of(r)
    if k == "generator":
        t = s.get("totals") or {}
        lat = t.get("lat_ms") or {}
        jit = [x.get("jitter_ms") for x in s.get("streams") or [] if x.get("jitter_ms") is not None]
        return [("rx_mbps", "Received", "Mb/s", t.get("rx_mbps"), "higher"), ("tx_mbps", "Sent", "Mb/s", t.get("tx_mbps"), None),
                ("loss", "Lost", "%", t.get("loss_pct"), "lower"), ("p50", "Delay, typical (p50)", "ms", lat.get("p50"), "lower"),
                ("p99", "Delay, slowest 1 % (p99)", "ms", lat.get("p99"), "lower"), ("jitter", "Jitter", "ms", max(jit) if jit else None, "lower"),
                ("sent", "Packets sent", "", t.get("tx_pkts"), None)]
    m = s.get("mirrored") or {}
    if k == "mirrored" and m.get("emulator"):
        t, e, rej = m.get("transfer") or {}, m.get("rtt") or {}, m.get("rejects") or {}
        return [("p50", "Bit transfer time, typical (p50)", "ms", t.get("p50"), "lower"), ("p99", "Bit transfer time, slowest 1 % (p99)", "ms", t.get("p99"), "lower"),
                ("max", "Bit transfer time, slowest", "ms", t.get("max"), "lower"), ("echo_p99", "Round trip of the echo bit (p99)", "ms", e.get("p99"), "lower"),
                ("avail", "ROK availability", "%", m.get("availability_pct"), "higher"), ("outage", "Longest outage", "ms", m.get("longest_outage_ms"), "lower"),
                ("rok_drops", "ROK drops", "", m.get("rok_drops"), "lower"), ("rejected", "Rejected messages", "", sum(int(v or 0) for v in rej.values()), "lower"),
                ("rbad", "RBAD events", "", m.get("rbad_events"), "lower"), ("cbad", "CBAD events", "", m.get("cbad_events"), "lower"),
                ("alarms", "Echo alarms", "", e.get("alarm_events"), "lower")]
    rt = _trip_stats(s)
    return [("p50", "Typical (p50)", "ms", rt.get("p50"), "lower"), ("p95", "p95", "ms", rt.get("p95"), "lower"),
            ("p99", "Slowest 1 % (p99)", "ms", rt.get("p99"), "lower"), ("max", "Slowest", "ms", rt.get("max"), "lower"),
            ("jitter", "Jitter", "ms", s.get("jitter_ms"), "lower"), ("loss", "Lost", "%", s.get("loss_pct"), "lower"),
            ("over", "Over the limit", "%", s.get("over_threshold_pct"), "lower"), ("sent", "Messages sent", "", s.get("sent"), None)]


def _tol(unit: str, a: float) -> float:
    """Differences smaller than this read as 'the same' (measurement noise, rounding)."""
    floor = {"ms": 0.05, "%": 0.005, "Mb/s": 0.01}.get(unit, 0.0)
    if unit == "%":                                      # 99.9 % against 99.5 % availability is a real difference: the share that is MISSING counts
        return max(floor, min(abs(a), abs(100.0 - abs(a))) * 0.02)
    return max(floor, abs(a) * 0.02) if unit else 0.0


def _change(a, v, unit: str, better: Optional[str]) -> Optional[dict]:
    if not isinstance(a, (int, float)) or not isinstance(v, (int, float)):
        return None
    d = v - a
    state = "same" if abs(d) <= _tol(unit, a) else ("more" if d > 0 else "less")
    judged = None
    if better and state != "same":
        judged = "better" if (d < 0) == (better == "lower") else "worse"
    return {"abs": round(d, 3), "pct": round(100.0 * d / a, 1) if a else None, "state": state, "judged": judged}


def fmt(v, unit: str = "") -> str:
    if v is None:
        return "–"
    if isinstance(v, float) and not v.is_integer():
        a = abs(v)
        t = f"{v:.0f}" if a >= 100 else f"{v:.1f}" if a >= 10 else f"{v:.2f}"
        t = t.rstrip("0").rstrip(".") if "." in t else t
    else:
        t = f"{int(v):,}"
    return f"{t} {unit}".strip()


def _route(r: dict) -> str:
    e, lg = (r.get("summary") or {}).get("endpoints") or {}, r.get("leg") or {}
    if e.get("label") or lg.get("label"):
        return e.get("label") or lg.get("label")
    a, b = e.get("source") or lg.get("tester"), e.get("dest") or lg.get("reflector")
    return f"{a} → {b}" if a and b else ""


# ---------------------------------------------------------------- the setup, as words (only what differs is shown)
def _dur(s) -> str:
    try:
        s = float(s)
    except (TypeError, ValueError):
        return ""
    if s >= 3600:
        return f"{s / 3600:g} h"
    return f"{s / 60:g} min" if s >= 120 else f"{s:g} s"


def _rate_words(rate: dict) -> str:
    for key, unit in (("mbps", "Mb/s"), ("kbps", "kb/s"), ("pps", "packets/s")):
        if rate.get(key):
            return f"{rate[key]:g} {unit}"
    return "as fast as possible" if rate.get("max") or rate.get("mode") == "max" else ""


def setup_of(r: dict) -> Dict[str, str]:
    p, s, k = r.get("profile") or {}, r.get("summary") or {}, kind_of(r)
    out: Dict[str, str] = {}
    if k == "generator":
        g = p.get("gen") or {}
        sts = g.get("streams") or []
        names = {x.get("id"): x for x in s.get("streams") or []}
        out["Streams"] = str(len(sts))
        desc = []
        for i, x in enumerate(sts):
            rate = x.get("rate") or {}
            sz = x.get("size") or {}
            size = {"fixed": f"{sz.get('size')} B", "range": f"{sz.get('min')}-{sz.get('max')} B", "imix": "IMIX"}.get(sz.get("mode"), "")
            ends = names.get(i) or {}
            route = f" {ends.get('src')} → {ends.get('dst')}" if ends.get("src") and ends.get("dst") else ""
            speed = _rate_words(rate)
            desc.append(f"{str(x.get('type', '')).upper()} {speed} {size}{route}".strip())
        out["Traffic"] = "; ".join(desc)
        out["Length"] = _dur(g.get("duration_s"))
        rule = g.get("rule") or {}
        lim = [f"loss ≤ {rule['max_loss_pct']:g} %"] if rule.get("max_loss_pct") is not None else []
        if rule.get("max_latency_ms"):
            lim.append(f"delay p{rule.get('latency_percentile', 99)} ≤ {rule['max_latency_ms']:g} ms")
        if rule.get("max_jitter_ms"):
            lim.append(f"jitter ≤ {rule['max_jitter_ms']:g} ms")
        if rule.get("min_throughput_mbps"):
            lim.append(f"at least {rule['min_throughput_mbps']:g} Mb/s")
        out["Pass rule"] = ", ".join(lim) or "none"
    else:
        t = p.get("transport") or r.get("transport") or ""
        out["Link"] = TRANSPORTS.get(t, t)
        out["From → to"] = _route(r)
        e, lg = s.get("endpoints") or {}, r.get("leg") or {}
        out["Direction"] = "One way" if (e.get("kind") or lg.get("kind")) == "one_way" else "Round trip"
        if t == "serial":
            out["Speed"] = f"{(p.get('serial') or {}).get('baud')} baud"
        elif t in ("udp", "tcp"):
            out["Port"] = str((p.get("net") or {}).get("port") or "")
        if k == "mirrored":
            mb = p.get("mirrored") or {}
            if t != "serial":
                out["Speed"] = f"{mb.get('rate_baud')} baud"
            out["Test bit every"] = f"{mb.get('trip_every_s'):g} s" if mb.get("trip_every_s") is not None else ""
        else:
            out["Message every"] = f"{p.get('interval_ms'):g} ms" if p.get("interval_ms") is not None else ""
            out["Message size"] = "40 bytes (with timestamps)" if p.get("frame") == "extended" else "7 bytes"
            out["Quiet gaps"] = ", ".join(_dur(g) for g in p.get("idle_gaps_s") or []) or "none"
            out["Background load"] = f"{p.get('load_mbps'):g} Mb/s" if p.get("load_mbps") else "none"
        out["Length"] = _dur(p.get("duration_s"))
        out["Delay limit"] = f"{p.get('threshold_ms'):g} ms" if p.get("threshold_ms") is not None else ""
        rule = p.get("rule") or {}
        out["Pass rule"] = f"p{rule.get('percentile', 99)}, loss ≤ {rule.get('max_loss_pct', 0):g} %"
        out["Bad-link simulation"] = p.get("impairment") or "none"
        pth = s.get("path") or {}
        out["Path"] = f"Wi-Fi ({pth.get('interface')})" if pth.get("kind") == "wifi" else "not Wi-Fi"
    return out


# ---------------------------------------------------------------- the one sentence
_WORDS = {  # key -> (subject, more, less, how the best one is called)
    "trip": ("p99", "slowest 1 % of messages took", "longer", "shorter", "fastest"),
    "mirrored": ("p99", "slowest 1 % of bit changes took", "longer to cross", "less time to cross", "fastest"),
    "generator": ("rx_mbps", "", "", "", "highest"),
}


def _loss_part(kind: str, vals: list, letters: list) -> str:
    key, word = ("avail", "ROK availability") if kind == "mirrored" else ("loss", "loss")
    xs = [v.get(key) for v in vals]
    if any(x is None for x in xs):
        return ""
    if len(xs) == 2:
        c = _change(xs[0], xs[1], "%", None)
        if c["state"] == "same":
            return f"{word} was the same ({fmt(xs[0], '%')})"
        if kind == "mirrored":
            return f"{letters[1]}'s ROK availability was {'higher' if c['state'] == 'more' else 'lower'} ({fmt(xs[1], '%')} against {fmt(xs[0], '%')})"
        return f"{letters[1]} lost {'more' if c['state'] == 'more' else 'less'} ({fmt(xs[1], '%')} against {fmt(xs[0], '%')})"
    if max(xs) - min(xs) <= _tol("%", xs[0]):
        return f"{word} was the same in all ({fmt(xs[0], '%')})"
    return f"{word}: " + ", ".join(f"{l} {fmt(x, '%')}" for l, x in zip(letters, xs))


def sentence(kind: str, vals: List[dict], letters: List[str]) -> str:
    key, subj, more, less, best = _WORDS[kind]
    xs = [v.get(key) for v in vals]
    unit = "Mb/s" if kind == "generator" else "ms"
    if any(x is None for x in xs):
        key2 = "loss"
        ys = [v.get(key2) for v in vals]
        if kind == "generator" or any(y is None for y in ys):
            return "These results do not all have the numbers needed for a comparison in words; see the table."
        return _loss_part(kind, vals, letters).capitalize() + "."
    A, B = letters[0], letters[1]
    if len(xs) == 2:
        c = _change(xs[0], xs[1], unit, None)
        if kind == "generator":
            first = (f"{A} and {B} carried about the same ({fmt(xs[0], unit)} and {fmt(xs[1], unit)} received)" if c["state"] == "same"
                     else f"{B} carried {fmt(abs(c['abs']), unit)} {'more' if c['state'] == 'more' else 'less'} than {A} ({fmt(xs[1], unit)} against {fmt(xs[0], unit)} received)")
        elif c["state"] == "same":
            first = f"{A} and {B} were about the same: the {subj.replace(' took', '')} took {fmt(xs[0], unit)} and {fmt(xs[1], unit)}"
        else:
            first = f"{B}'s {subj} {fmt(abs(c['abs']), unit)} {more if c['state'] == 'more' else less} than {A}'s ({fmt(xs[1], unit)} against {fmt(xs[0], unit)})"
        t0, t1 = vals[0].get("p50"), vals[1].get("p50")
        tc = _change(t0, t1, unit, None) if kind != "generator" else None
        if tc and tc["state"] != "same":                        # "the radio adds about 16 ms to a typical message"
            what = "bit change" if kind == "mirrored" else "message"
            first = (f"A typical {what} took {fmt(abs(tc['abs']), unit)} {'longer' if tc['state'] == 'more' else 'less'} in {B} than in {A} "
                     f"({fmt(t1, unit)} against {fmt(t0, unit)}); {first}")
    else:
        label = "Received" if kind == "generator" else ("Slowest 1 % of bit changes (p99)" if kind == "mirrored" else "Slowest 1 % of messages (p99)")
        pick = max if kind == "generator" else min
        bi = xs.index(pick(xs))
        spread = max(xs) - min(xs) <= _tol(unit, xs[0])
        first = f"{label}: " + ", ".join(f"{l} {fmt(x, unit)}" for l, x in zip(letters, xs)) + ("; all about the same" if spread else f"; {letters[bi]} was the {best}")
    tail = _loss_part(kind, vals, letters)
    return first + (f"; {tail}." if tail else ".")


# ---------------------------------------------------------------- the chart
BINS = 40


def _delays(store, r: dict) -> List[float]:
    basis_up = (r.get("summary") or {}).get("basis") == "up"
    out = []
    for m in store.run_messages(r["id"], 200000):
        if m["phase"] != "steady":
            continue
        v = m["up_us"] if basis_up and m["up_us"] is not None else m["rtt_us"]
        if v is not None and m["status"] != "lost":
            out.append(v / 1000.0)
    return out


def _hist_chart(store, runs: List[dict], letters: List[str]) -> Optional[dict]:
    data = [_delays(store, r) for r in runs]
    if not any(data):
        return None
    top = max(M.percentile(d, 99.5) for d in data if d) * 1.2
    thrs = {(r.get("summary") or {}).get("threshold_ms") or (r.get("profile") or {}).get("threshold_ms") for r in runs}
    limit = thrs.pop() if len(thrs) == 1 else None
    if limit and limit <= top * 3:                       # the limit is on the picture whenever it is anywhere near the data
        top = max(top, limit * 1.15)
    top = max(top, 0.01)
    bw = top / BINS
    series = []
    for l, d in zip(letters, data):
        cnt = [0] * BINS
        for v in d:
            cnt[min(BINS - 1, int(v / bw))] += 1
        pct = [round(100.0 * c / len(d), 3) if d else 0.0 for c in cnt]
        cum, run = [0.0], 0                                   # cum[i] = share of messages faster than edges[i]
        over = sum(1 for v in d if v >= top)                  # slower than the picture shows: the line ends below 100 %
        for i, c in enumerate(cnt):
            run += c - (over if i == BINS - 1 else 0)
            cum.append(round(100.0 * run / len(d), 3) if d else 0.0)
        series.append({"letter": l, "n": len(d), "pct": pct, "cum": cum})
    return {"type": "hist", "title": "How delays were spread", "unit": "ms", "edges": [round(i * bw, 4) for i in range(BINS + 1)], "series": series,
            "limit_ms": limit, "note": "Each line shows the share of messages that were at least that fast. A line that climbs early and steeply is a fast, steady link."}


def _lines_chart(store, runs: List[dict], letters: List[str]) -> Optional[dict]:
    series = []
    for l, r in zip(letters, runs):
        by: Dict[float, dict] = {}
        for i in store.run_gen_intervals(r["id"]):
            d = by.setdefault(i["t_offset_s"], {"rx": 0.0, "lat": []})
            d["rx"] += (i["rx_bytes"] or 0) * 8 / 1e6
            if i["lat_avg_us"] is not None:
                d["lat"].append(i["lat_avg_us"] / 1000.0)
        ts = sorted(by)
        series.append({"letter": l, "t": ts, "rx_mbps": [round(by[t]["rx"], 3) for t in ts],
                       "lat_ms": [round(sum(by[t]["lat"]) / len(by[t]["lat"]), 3) if by[t]["lat"] else None for t in ts]})
    if not any(s["t"] for s in series):
        return None
    return {"type": "lines", "title": "Throughput and delay per second", "series": series,
            "note": "Each test starts at 0 s. Top: received Mb/s per second. Below: average one-way delay per second (only when the clocks were synced)."}


# ---------------------------------------------------------------- compare
class CompareError(ValueError):
    pass


def compare(store, run_ids: List[int]) -> dict:
    ids = list(dict.fromkeys(run_ids))
    if len(ids) < 2:
        raise CompareError("Pick at least 2 results to compare.")
    if len(ids) > 4:
        raise CompareError("Pick at most 4 results to compare.")
    runs = []
    for i in ids:
        r = store.get_run(i)
        if not r:
            raise CompareError(f"Result {i} does not exist (it may have been deleted).")
        runs.append(r)
    kinds = {kind_of(r) for r in runs}
    if "sniffer" in kinds:
        raise CompareError("Captures cannot be compared. Pick trip, traffic generator or Mirrored Bits results.")
    if len(kinds) > 1:
        raise CompareError("Pick results of the same kind.")
    kind = kinds.pop()
    for r in runs:
        if r.get("status") == "running":
            raise CompareError(f"Result {r['id']} is still running. Compare it when it has finished.")
        if not has_numbers(r):
            raise CompareError(f"Result {r['id']} has no numbers to compare (the test could not run).")
    letters = [chr(ord("A") + i) for i in range(len(runs))]
    heads = [headline(r) for r in runs]
    base = baselines(store)
    rows = []
    for n, (key, label, unit, _, better) in enumerate(heads[0]):
        vals = [h[n][3] for h in heads]
        if all(v is None for v in vals):
            continue
        rows.append({"key": key, "label": label, "unit": unit, "better": better, "values": vals,
                     "change": [None] + [_change(vals[0], v, unit, better) for v in vals[1:]]})
    vals = [{h[n][0]: h[n][3] for n in range(len(h))} for h in heads]
    setups = [setup_of(r) for r in runs]
    diff = [{"label": k, "values": [s.get(k, "") for s in setups]} for k in setups[0] if len({s.get(k, "") for s in setups}) > 1]
    line = " · ".join(f"{d['label']}: " + ", ".join(f"{l} {v or 'none'}" for l, v in zip(letters, d["values"])) for d in diff) or "The setup was the same in every result."
    out_runs = []
    for l, r in zip(letters, runs):
        out_runs.append({"id": r["id"], "letter": l, "name": (r.get("profile") or {}).get("name") or "", "job": r.get("job") or {}, "job_line": job_line(r.get("job")),
                         "started_at": r.get("started_at"), "status": r.get("status"), "verdict": r.get("verdict"), "route": _route(r),
                         "kind": kind, "baseline": base.get(baseline_key(r)) in _group_ids(store, r)})
    chart = _lines_chart(store, runs, letters) if kind == "generator" else _hist_chart(store, runs, letters)
    return {"kind": kind, "kind_name": KIND_NAMES[kind], "runs": out_runs, "rows": rows, "sentence": sentence(kind, vals, letters),
            "setup_diff": diff, "setup_line": line, "chart": chart}


# ---------------------------------------------------------------- baseline (one per saved test)
def baseline_key(r: dict) -> str:
    return f"{kind_of(r)}|{(r.get('profile') or {}).get('name') or ''}"


def baselines(store) -> Dict[str, int]:
    b = store.get_setting("baselines", {})
    return b if isinstance(b, dict) else {}


def _group_ids(store, r: dict) -> List[int]:
    return store.group_run_ids(r["id"]) if r.get("group_id") else [r["id"]]


def set_baseline(store, rid: int) -> dict:
    r = store.get_run(rid)
    if not r:
        raise KeyError(rid)
    if kind_of(r) == "sniffer":
        raise ValueError("A capture cannot be a baseline.")
    if r.get("status") == "running" or not has_numbers(r):
        raise ValueError("Only a finished result with numbers can be the baseline.")
    b = baselines(store)
    b[baseline_key(r)] = rid
    store.set_setting("baselines", b)
    return {"ok": True, "run_id": rid, "test": (r.get("profile") or {}).get("name") or ""}


def clear_baseline(store, rid: int) -> dict:
    r = store.get_run(rid)
    if not r:
        raise KeyError(rid)
    b = baselines(store)
    had = b.pop(baseline_key(r), None)
    store.set_setting("baselines", b)
    return {"ok": True, "cleared": had is not None}


def baseline_for(store, r: dict, cache: Optional[dict] = None) -> Optional[dict]:
    """The baseline result this run is measured against: the same link / direction of the baseline test when that one had several."""
    b = cache["b"] if cache is not None else baselines(store)
    key = baseline_key(r)
    bid = b.get(key)
    if bid is None:
        return None
    get = (lambda i: cache["runs"].setdefault(i, store.get_run(i))) if cache is not None else store.get_run
    base = get(bid)
    if not base:                                              # the baseline was deleted: forget it
        b.pop(key, None)
        store.set_setting("baselines", b)
        return None
    if base.get("group_id"):
        mine = _route(r)
        ids = cache["groups"].setdefault(bid, store.group_run_ids(bid)) if cache is not None else store.group_run_ids(bid)
        for i in ids:
            x = get(i)
            if x and _route(x) == mine:
                return x
    return base


_VS = {"trip": (("p99", "p99", "ms"), ("loss", "loss", "%")),
       "mirrored": (("p99", "bit transfer p99", "ms"), ("avail", "ROK availability", "%")),
       "generator": (("rx_mbps", "received", "Mb/s"), ("loss", "loss", "%"), ("p99", "delay p99", "ms"))}


def vs_baseline(store, r: dict, cache: Optional[dict] = None) -> Optional[dict]:
    """{'run_id', 'started_at', 'is_baseline', 'text': 'vs baseline: p99 +38 ms · loss same', 'items': [...]} or None (no baseline for this
    test, or this run is older than the baseline: only LATER runs of the test are measured against it)."""
    if kind_of(r) == "sniffer":
        return None
    base = baseline_for(store, r, cache)
    if not base:
        return None
    name = (r.get("profile") or {}).get("name") or "this test"
    same_test = base["id"] == r["id"] or (base.get("group_id") is not None and base.get("group_id") == r.get("group_id"))
    if same_test:
        return {"run_id": base["id"], "started_at": base.get("started_at"), "is_baseline": True, "items": [],
                "text": f"This result is the baseline for “{name}”. Later runs of this test are compared with it."}
    if r["id"] < base["id"] or r.get("status") == "running" or not has_numbers(r) or not has_numbers(base):
        return None
    hv = {h[0]: h for h in headline(r)}
    hb = {h[0]: h for h in headline(base)}
    items = []
    for key, label, unit in _VS[kind_of(r)]:
        a, v = hb[key][3], hv[key][3]
        c = _change(a, v, unit, hv[key][4])
        if c is None:
            continue
        txt = f"{label} same" if c["state"] == "same" else f"{label} {'+' if c['abs'] > 0 else '−'}{fmt(abs(c['abs']), unit)}"
        items.append({"key": key, "label": label, "unit": unit, "baseline": a, "value": v, **c, "text": txt})
    if not items:
        return None
    return {"run_id": base["id"], "started_at": base.get("started_at"), "is_baseline": False, "items": items,
            "text": "vs baseline: " + " · ".join(i["text"] for i in items)}


class BaselineView:
    """vs_baseline() for a whole list of runs with the baselines read once (the results list)."""

    def __init__(self, store):
        self.store = store
        self.cache = {"b": baselines(store), "runs": {}, "groups": {}}

    def vs(self, r: dict) -> Optional[dict]:
        if baseline_key(r) not in self.cache["b"]:
            return None
        return vs_baseline(self.store, r, self.cache)
