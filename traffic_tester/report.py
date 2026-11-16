"""PDF report builder (docs/SPEC.md 7 + 13): any mix of trip-test and generator runs -> one formatted PDF."""
from __future__ import annotations

import datetime as dt
import io
import socket
from typing import List, Optional
from xml.sax.saxutils import escape as _x

from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, Line, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import __version__
from . import compare as CMP

TEAL, GREEN, RED, GREY = colors.HexColor("#0f766e"), colors.HexColor("#15803d"), colors.HexColor("#b91c1c"), colors.HexColor("#5c6b7a")
SS = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=SS["Heading1"], textColor=TEAL, fontSize=20, spaceAfter=6)
H2 = ParagraphStyle("h2", parent=SS["Heading2"], textColor=colors.HexColor("#16202a"), fontSize=14, spaceBefore=10, spaceAfter=4)
H3 = ParagraphStyle("h3", parent=SS["Heading3"], textColor=GREY, fontSize=10, spaceBefore=6, spaceAfter=2)
BODY = ParagraphStyle("body", parent=SS["BodyText"], fontSize=9, leading=12)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8, textColor=GREY)


def _ms(v, d=2):
    return "-" if v is None else f"{v:.{d}f} ms"


def _tbl(rows, widths=None, head=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if head else 0)
    st = [("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d5dde5")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if head:
        st += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0f2f0")), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    t.setStyle(TableStyle(st))
    return t


def _chart(series, title, ylabel, width=170 * mm, height=55 * mm, hline=None):
    """series: [(name, [(x, y), ...], color)]"""
    d = Drawing(width, height + 12)
    pts = [p for _, s, _ in series for p in s if p[1] is not None]
    if not pts:
        d.add(String(10, height / 2, "no data", fontSize=9, fillColor=GREY))
        return d
    lp = LinePlot()
    lp.x, lp.y, lp.width, lp.height = 38, 20, width - 50, height - 14
    lp.data = [[(x, y) for x, y in s if y is not None] or [(0, 0)] for _, s, _ in series]
    for i, (_, _, c) in enumerate(series):
        lp.lines[i].strokeColor = c
        lp.lines[i].strokeWidth = 1.2
    ymax = max(y for _, y in pts)
    if hline:
        ymax = max(ymax, hline * 1.2)
    lp.yValueAxis.valueMin, lp.yValueAxis.valueMax = 0, ymax * 1.1 or 1
    lp.xValueAxis.labels.fontSize = lp.yValueAxis.labels.fontSize = 7
    lp.xValueAxis.valueMin = 0
    d.add(lp)
    if hline:
        y = lp.y + lp.height * (hline / (ymax * 1.1))
        d.add(Line(lp.x, y, lp.x + lp.width, y, strokeColor=RED, strokeDashArray=[4, 3], strokeWidth=1))
    d.add(String(38, height, title, fontSize=8, fillColor=colors.HexColor("#16202a")))
    d.add(String(2, height / 2, ylabel, fontSize=7, fillColor=GREY))
    x0 = 38 + 0
    for i, (name, _, c) in enumerate(series):
        d.add(String(x0 + i * 70, 4, "- " + name, fontSize=7, fillColor=c))
    return d


def _banner(verdict: Optional[str], extra: str = ""):
    if verdict is None:
        return Paragraph(f"<b>No verdict</b> {extra}", BODY)
    c = GREEN if verdict == "PASS" else RED
    return Paragraph(f'<font color="{c.hexval()}" size="16"><b>{"PASS" if verdict == "PASS" else "FAIL"}</b></font> {extra}', BODY)


def _reasons(summary) -> List:
    v = (summary or {}).get("verdict") or {}
    return [Paragraph("- " + r, BODY) for r in v.get("reasons", [])]


KIND_WORDS = {"round_trip": "round trip (the far end reflects each message back)", "one_way": "one way (timed by the receiving Pi's clock)"}


def _route(r) -> str:
    """'pi1 -> pi2' for a trip test: who sent, who answered (by name). Older results without it show nothing."""
    s_ = (r.get("summary") or {})
    e = s_.get("endpoints") or {}
    lg = r.get("leg") or {}
    a, b = e.get("source") or lg.get("tester"), e.get("dest") or lg.get("reflector")
    return f"{a} \u2192 {b}" if a and b else ""


def _leg_lines(r) -> List:
    """Which Pi sent, which one answered, what it was judged on and who ran it; for the other direction of a two-way test, the group it belongs to."""
    s_ = (r.get("summary") or {})
    e = s_.get("endpoints") or {}
    lg = r.get("leg") or {}
    a, b = e.get("source") or lg.get("tester"), e.get("dest") or lg.get("reflector")
    if not (a and b):
        return []
    kind = e.get("kind") or lg.get("kind") or "round_trip"
    basis = e.get("basis") or lg.get("basis") or "rtt"
    txt = f"<b>Sent by</b> {_x(str(a))} &nbsp; <b>answered by</b> {_x(str(b))}"
    if e.get("address"):
        txt += f" ({_x(str(e['address']))})"
    txt += f" &nbsp; - &nbsp; {KIND_WORDS.get(kind, _x(str(kind)))}"
    ctl = e.get("controller") or lg.get("controller")
    if ctl:
        txt += f" &nbsp; - &nbsp; started from {_x(str(ctl))}"
    out = [Paragraph(txt, BODY)]
    if r.get("group_id"):
        out.append(Paragraph(f"One of the directions of a test run in both directions at once (group {r['group_id']}). The pass/fail is judged on "
                             + ("the delay from the sender to the answering Pi." if basis == "up" else "the round trip."), SMALL))
    return out


def _mb_latency(mb: dict, t: dict, r: dict) -> List:
    """The latency block: the frame codec reports the round trip of every frame (p99.9, frames lost, CRC errors, outages); SimCodec the bit transfer time."""
    if not mb.get("frame_mode"):
        return [_tbl([["Bit transfer time", "p50", "p95", "p99", "max", "Limit", "Basis"],
                      ["", _ms(t.get("p50")), _ms(t.get("p95")), _ms(t.get("p99")), _ms(t.get("max")), _ms(t.get("threshold_ms")),
                       "one way, clocks synced" if t.get("basis") == "one_way" else "half the echo round trip"]])]
    f = mb.get("frames") or {}
    return [_tbl([["Round trip of every frame", "min", "p50", "p95", "p99", "p99.9", "max", "jitter", "Limit"],
                  [f"{r.get('n')} measured", _ms(r.get("min")), _ms(r.get("p50")), _ms(r.get("p95")), _ms(r.get("p99")), _ms(r.get("p999")), _ms(r.get("max")), _ms(r.get("jitter_ms")),
                   _ms(t.get("threshold_ms"))]]),
            Spacer(1, 3),
            _tbl([["Frames", "good", "lost", "lost %", "CRC errors", "repeated", "out of order", "outages", "longest outage"],
                  ["", f.get("good"), f.get("lost"), f.get("lost_pct"), f.get("crc_errors"), f.get("duplicates"), f.get("out_of_order"), mb.get("outage_count"),
                   f"{mb.get('longest_outage_ms')} ms"]]),
            Spacer(1, 2),
            Paragraph("Round trip = from the echo in each frame (frame number + send time returned by the other end, minus the time it held the frame): no clock sync. "
                      "One way is about half of it, an estimate; true one-way needs GPS time (not built in). Lost frames are counted exactly from the frame numbers.", BODY)]


def _mb_emulator(mb: dict) -> List:
    """Mirrored Bits emulator section: transfer time, availability, RBAD / CBAD, rejects by reason, inter-arrival histogram, impairment, event log."""
    t, r, rej, imp = mb.get("transfer") or {}, mb.get("rtt") or {}, mb.get("rejects") or {}, mb.get("impair") or {}
    out: List = [Paragraph("Mirrored Bits channel", H3),
                 Paragraph(f"Two Mirrored Bits emulators at {mb.get('baud')} baud (PROTO {mb.get('proto')} on this Pi), one full message every {mb.get('wire_period_ms')} ms in each direction. "
                           f"{mb.get('codec')}.", BODY),
                 *_mb_latency(mb, t, r),
                 Spacer(1, 3),
                 _tbl([["ROK availability", "ROK was 0", "Longest outage", "ROK drops", "RBAD events", "CBAD events", "Lost-channel", "Echo alarms"],
                       [f"{mb.get('availability_pct')} %", f"{mb.get('unavail_pct')} %", f"{mb.get('longest_outage_ms')} ms", mb.get("rok_drops"), mb.get("rbad_events"),
                        mb.get("cbad_events"), mb.get("lost_events"), r.get("alarm_events")]]),
                 *([] if mb.get("frame_mode") else [Spacer(1, 3), _tbl([["Round trip (echo bit)", "min", "p50", "p99", "max", "jitter", "RTT_MAX"],
                       ["", _ms(r.get("min")), _ms(r.get("p50")), _ms(r.get("p99")), _ms(r.get("max")), _ms(r.get("jitter_ms")), _ms(r.get("rtt_max_ms"))]])]),
                 Spacer(1, 3),
                 _tbl([["Messages", "sent", "received good"] + [k for k in ("parity", "framing", "overrun", "consistency", "crc", "address", "loopback", "timing", "missing")],
                       ["", mb.get("tx_msgs"), mb.get("rx_good")] + [rej.get(k, 0) for k in ("parity", "framing", "overrun", "consistency", "crc", "address", "loopback", "timing", "missing")]])]
    if mb.get("overrun_available") is False:
        out.append(Paragraph("Overrun counting is not available on this serial port (the driver has no counter).", BODY))
    h = mb.get("histogram") or {}
    if h.get("counts"):
        out.append(Spacer(1, 3))
        out.append(Paragraph(f"Gap between good messages (nominal {h.get('nominal_ms')} ms; exposes radio bunching)", BODY))
        out.append(_tbl([[l.replace("<", "") for l in h["labels"]], [c for c in h["counts"]]]))
    on = [f"{k.replace('_', ' ')} {v:g}" for k, v in imp.items() if k in ("latency_ms", "jitter_ms", "drop_pct", "burst_len_ms", "burst_every_s", "bit_error_pct") and v]
    if on or imp.get("break_link"):
        out.append(Paragraph("Impairment injector was on: " + ", ".join(on + (["link broken by hand"] if imp.get("break_link") else [])), BODY))
    if mb.get("one_way_note"):
        out.append(Paragraph(str(mb["one_way_note"]), BODY))
    far = mb.get("far")
    if far:
        c = far.get("counters") or {}
        out.append(Paragraph(f"The other end ({far.get('name')}, PROTO {far.get('proto')}): ROK drops {c.get('rok_drops')}, RBAD {c.get('rbad_events')}, CBAD {c.get('cbad_events')}, rejected {c.get('rx_rejected')}.", BODY))
    ev = (mb.get("events_log") or [])[-40:]
    if ev:
        out.append(Spacer(1, 3))
        out.append(Paragraph(f"Event log (last {len(ev)} of {mb.get('events_total')}; the full log is in the CSV export)", BODY))
        out.append(_tbl([["Time", "Element", "Value", "Note"]] + [[f"{e['t_s']:.3f} s", e["name"], e["value"], e.get("note", "")] for e in ev]))
    return out


def _radio_block(s: dict) -> List:
    """Radio packetization as recorded, when the run happened (busy hours), the bundling check, data used and the by-hour-of-day table."""
    rd, w, u = s.get("radio"), s.get("when"), s.get("usage")
    mb = s.get("mirrored") or {}
    if not (rd or w or mb.get("packetization")):
        return []
    out: List = [Spacer(1, 4), Paragraph("Radio, time of day and data use", H3)]
    if rd:
        out.append(Paragraph(_x(rd.get("text") or "radio packetization not recorded"), BODY))
    if w:
        out.append(Paragraph(f"Ran {_x(w.get('start_local', ''))} to {_x(w.get('end_local', ''))} ({_x(w.get('tag', ''))}). {_x(w.get('note', ''))}", BODY))
    pk = mb.get("packetization")
    if pk:
        out.append(Paragraph(("<font color='#b91c1c'><b>Warning:</b></font> " if pk.get("suspected") else "") + _x(pk.get("message", "")), BODY))
    if u:
        a, o = u.get("actual") or {}, (u.get("over_the_air") or {}).get(u.get("selected") or "udp") or {}
        out.append(Paragraph(_x(u.get("text", "")), SMALL))
        if a:
            out.append(Paragraph(f"This run actually sent {a.get('tx_bytes', 0):,} bytes and received {a.get('rx_bytes', 0):,} bytes of frames "
                                 f"(about {a.get('tx_over_air_bytes', 0):,} / {a.get('rx_over_air_bytes', 0):,} bytes over the air).", BODY))
    hr = mb.get("hourly")
    if hr:
        out += _hourly_block(hr)
    return out


def _hourly_block(hr: dict) -> List:
    f = lambda v: "-" if v is None else f"{v:g}"
    rows = [["Hour of day", "Round trips", "p50 ms", "p99 ms", "p99.9 ms", "Slowest ms", "Lost frames", "Outages"]]
    for h in hr.get("by_hour_of_day") or []:
        rows.append([f"{h['hour']:02d}:00", h["n"], f(h["p50"]), f(h["p99"]), f(h["p999"]), f(h["max"]), h["lost"], h["outages"]])
    for k, label in (("weekday", "Weekdays"), ("weekend", "Weekends")):
        h = (hr.get("kinds") or {}).get(k)
        if h:
            rows.append([label, h["n"], f(h["p50"]), f(h["p99"]), f(h["p999"]), f(h["max"]), h["lost"], h["outages"]])
    out: List = [Spacer(1, 3), Paragraph("Delay by hour of day (local time) and weekday vs weekend", BODY), _tbl(rows)]
    hod = hr.get("by_hour_of_day") or []
    if len(hod) > 1:
        ser = lambda k: [(h["hour"], h[k]) for h in hod]
        out += [Spacer(1, 3), _chart([("p50", ser("p50"), GREEN), ("p99", ser("p99"), TEAL), ("p99.9", ser("p999"), RED), ("slowest", ser("max"), GREY)], "Delay by hour of day (0-23)", "ms")]
    out.append(Paragraph(_x(hr.get("note", "")), SMALL))
    return out


def _combined_block(c: dict) -> List:
    t = c.get("totals") or {}
    f = lambda v: "-" if v is None else f"{v:g}"
    out: List = [Paragraph(f"Scheduled repeats: {_x(c.get('name', ''))}", H2), Paragraph(_x(c.get("schedule_text", "")) + ". " + _x(c.get("coverage_note", "")), BODY),
                 _tbl([["Runs", "Passed", "Failed", "Missed slots", "Frames sent", "Frames lost", "Worst p99 ms", "Worst p99.9 ms", "Worst slowest ms"],
                       [t.get("runs"), t.get("passed"), t.get("failed"), t.get("missed_slots"), t.get("frames_sent"), t.get("frames_lost"), f(t.get("worst_p99")), f(t.get("worst_p999")), f(t.get("worst_max"))]]),
                 Paragraph(_x(c.get("basis", "")), SMALL)]
    if t.get("bundling_runs"):
        out.append(Paragraph(f"<font color='#b91c1c'><b>Warning:</b></font> the radio seemed to be bundling messages in {t['bundling_runs']} run(s).", BODY))
    if c.get("by_tag"):
        out += [Spacer(1, 3), _tbl([["Time of day", "Runs", "Failed", "Frames lost", "Worst p99 ms", "Worst p99.9 ms", "Worst slowest ms"]] +
                                   [[x["tag"], x["runs"], x["fails"], x["lost"], f(x["worst_p99"]), f(x["worst_p999"]), f(x["worst_max"])] for x in c["by_tag"]])]
    out += [Spacer(1, 3), _tbl([["Run", "Started", "Tag", "Result", "Sent", "Lost", "p99 ms", "p99.9 ms", "Slowest ms"]] +
                               [[x["run_id"], x["start"], x["tag"], x["verdict"] or x["status"], x["sent"], x["lost"], f(x["p99"]), f(x["p999"]), f(x["max"])] for x in c.get("rows") or []])]
    return out


def _trip_section(store, r) -> List:
    s, p = r["summary"] or {}, r["profile"]
    out = [Paragraph(f"{p.get('name', 'Test')} &nbsp; <font size=9 color='#5c6b7a'>(run {r['id']}, {r['started_at']})</font>", H2),
           _banner(r["verdict"], f"&nbsp; {p.get('transport', '').upper()} - limit {p.get('threshold_ms')} ms on p{p.get('rule', {}).get('percentile', 99)}")]
    out += _leg_lines(r)
    out += _reasons(s)
    if s.get("error"):
        out.append(Paragraph("Could not run: " + str(s["error"]), BODY))
        return out
    rt = s.get("rtt_ms") or {}
    out.append(Spacer(1, 4))
    out.append(_tbl([["Typical (p50)", "p95", "p99", "Slowest", "Jitter", "Sent", "Lost", "Over limit"],
                     [_ms(rt.get("p50")), _ms(rt.get("p95")), _ms(rt.get("p99")), _ms(rt.get("max")), _ms(s.get("jitter_ms")), s.get("sent"),
                      f"{s.get('lost')} ({s.get('loss_pct')} %)", s.get("over_threshold")]]))
    pth = s.get("path") or {}
    if pth.get("kind") == "wifi":
        out.append(Spacer(1, 3))
        out.append(Paragraph(f"The test traffic went over Wi-Fi ({pth.get('interface')}); Wi-Fi power saving was "
                             + ("ON, which adds wake-up delay to the first message after a quiet period." if pth.get("power_save") else "off." if pth.get("power_save") is False else "not readable."), BODY))
    mb = s.get("mirrored")
    if mb and mb.get("emulator"):
        out += _mb_emulator(mb)
    elif mb:
        a = mb.get("a_rx") or {}
        out.append(Paragraph("Mirrored Bits channel", H3))
        out.append(Paragraph(f"Both Pis sent one {mb.get('wire_period_ms')} ms message after another at {mb.get('baud')} baud for the whole test. "
                             "Test made with the first Mirrored Bits mode: copies Mirrored Bits timing and load; not wire-compatible with SEL relays.", BODY))
        out.append(_tbl([["Trips", "Trips lost", "Messages received", "Bad", "Lost", "Channel failed", "Longest outage", "Failure detected in"],
                         [mb.get("trips"), mb.get("trips_lost"), a.get("msgs_good"), (a.get("msgs_bad") or 0) + (a.get("startup_bad") or 0), a.get("msgs_lost"),
                          f"{mb.get('down_pct')} %", f"{mb.get('longest_outage_ms')} ms", "-" if mb.get("detect_ms_avg") is None else f"{mb.get('detect_ms_avg')} ms"]]))
        if mb.get("one_way_note"):
            out.append(Paragraph(str(mb["one_way_note"]), BODY))
    out += _radio_block(s)
    idle = (s.get("idle") or {}).get("gaps") or []
    if idle:
        out.append(Paragraph("First message after a quiet period", H3))
        out.append(_tbl([["Quiet for", "Tests", "Fastest", "Median", "Slowest", "Lost"]] + [[f"{g['gap_s']:g} s", g["count"], _ms(g["min_ms"]), _ms(g["median_ms"]), _ms(g["max_ms"]), g["lost"]] for g in idle]))
    ints = store.run_intervals(r["id"])
    if ints:
        thr = p.get("threshold_ms", 60)
        f = lambda k: [(i["t_offset_s"], None if i[k] is None else i[k] / 1000.0) for i in ints]
        out.append(Spacer(1, 4))
        out.append(_chart([("p50", f("p50_us"), GREEN), ("p99", f("p99_us"), TEAL), ("slowest", f("max_us"), GREY)], "Delay per second", "ms", hline=thr))
    return out


def _stream_route(x) -> str:
    a, b = x.get("src"), x.get("dst")
    if a and b:
        return f"{a} \u2192 {b}"
    return "from \u2192 to" if x.get("direction") == "a_to_b" else "to \u2192 from"


def _steps_block(s) -> List:
    """Throughput in steps: the headline and one row per step (docs/SPEC.md 13.9)."""
    sp = s.get("steps") or {}
    if not sp:
        return []
    from .gen.run import fmt_mbps
    lim = sp.get("limit") or {}
    pc = lim.get("latency_percentile") or 99
    dl = "Slowest" if pc == 100 else f"p{pc} delay"
    parts = ([f"lost \u2264 {lim['max_loss_pct']:g} %"] if lim.get("max_loss_pct") is not None else []) + ([f"{dl.lower()} \u2264 {lim['max_latency_ms']:g} ms"] if lim.get("max_latency_ms") is not None else [])
    out = [Paragraph("Throughput in steps", H3),
           Paragraph(f"<b><font size=13 color='{'#15803d' if sp.get('clean_step') else '#b91c1c'}'>{_x(sp.get('headline', ''))}</font></b>", BODY)]
    f = sp.get("flood")
    top = f"Top rate {fmt_mbps(sp.get('top_mbps'))} Mb/s" + (f", measured by a {f['duration_s']:g} s flood ({fmt_mbps(f['sent_mbps'])} Mb/s sent, {fmt_mbps(f['got_mbps'])} Mb/s arrived)" if f else " (typed)")
    out.append(Paragraph(_x(f"{top}. {sp.get('step_s', 10):g} s per step. A step is clean when {' and '.join(parts) or 'something arrives'}."), SMALL))
    rows = [["Step", "Rate asked", "Sent Mb/s", "Got Mb/s", "Lost", dl, "Clean", "Why / note"]]
    for r in sp.get("rows") or []:
        ran = r.get("state") in ("done", "stopped")
        mark = "\u2713" if r.get("clean") is True else "\u2717" if r.get("clean") is False else "-"
        why = "not sent" if r.get("state") == "skipped" else "stopped before it ended" if r.get("state") == "stopped" else "; ".join(x for x in (r.get("why"), r.get("note")) if x)
        rows.append([f"{r['n']} of {r['of']}", f"{fmt_mbps(r.get('asked_mbps'))} Mb/s", fmt_mbps(r["sent_mbps"]) if ran else "-", fmt_mbps(r["got_mbps"]) if ran else "-",
                     f"{r['loss_pct']:g} % ({r['lost']:,})" if ran else "-", _ms(r.get("delay_ms")) if ran and r.get("delay_ms") is not None else "-", mark, Paragraph(_x(why), SMALL)])
    out.append(_tbl(rows, [16 * mm, 22 * mm, 18 * mm, 18 * mm, 28 * mm, 20 * mm, 12 * mm, 52 * mm]))
    for n in sp.get("notes") or []:
        out.append(Paragraph(_x(n), SMALL))
    done = [r for r in sp.get("rows") or [] if r.get("state") in ("done", "stopped")]
    if len(done) > 1:
        out.append(Spacer(1, 4))
        out.append(_chart([("everything arrives", [(r["sent_mbps"], r["sent_mbps"]) for r in done], GREY), ("got", [(r["sent_mbps"], r["got_mbps"]) for r in done], TEAL)],
                          "What arrived at each rate (x: sent, Mb/s)", "Mb/s"))
    return out


def _gen_section(store, r) -> List:
    s, p = r["summary"] or {}, r["profile"]
    out = [Paragraph(f"Traffic generator - {p.get('name', '')} &nbsp; <font size=9 color='#5c6b7a'>(run {r['id']}, {r['started_at']})</font>", H2),
           _banner(r["verdict"], f"&nbsp; {len(s.get('streams', []))} stream(s), {s.get('duration_s', '')} s")]
    if s.get("nodes"):
        out.append(Paragraph("<b>Pis taking part:</b> " + ", ".join(_x(str(n)) for n in s["nodes"]), BODY))
    out += _reasons(s)
    if s.get("error") or not s.get("streams"):
        out.append(Paragraph("Could not run: " + str(s.get("error") or "no results"), BODY))
        return out
    t = s["totals"]
    c = s.get("clocks") or {}
    out += _steps_block(s)
    out.append(Spacer(1, 4))
    out.append(_tbl([["Sent", "Received", "Lost", "Throughput sent", "Throughput received", "Delay p99 (one-way)"],
                     [f"{t['tx_pkts']:,}", f"{t['rx_pkts']:,}", f"{t['lost']:,} ({t['loss_pct']} %)", f"{t['tx_mbps']} Mb/s", f"{t['rx_mbps']} Mb/s",
                      _ms((t.get("lat_ms") or {}).get("p99")) if c.get("valid") else "clocks not synced"]]))
    rows = [["Stream", "Type / from \u2192 to", "Size", "Sent Mb/s", "Recv Mb/s", "pps", "Loss", "Delay p50 / p99 / max", "Jitter", "Round trip"]]
    for x in s["streams"]:
        lat = x.get("lat_ms") or {}
        rows.append([x["name"], Paragraph(f"{_x(x['type'])}<br/>{_x(_stream_route(x))}", SMALL), x["size"], x["tx"]["mbps"], x["rx"]["mbps"], f"{x['rx']['pps']:.0f}", f"{x['loss_pct']} %",
                     "-" if not lat else f"{_ms(lat['p50'], 2)} / {_ms(lat['p99'], 2)} / {_ms(lat['max'], 2)}", _ms(x.get("jitter_ms")), _ms((x.get("rtt_ms") or {}).get("avg"))])
    pr = s.get("pairs") or []
    if len(pr) > 1 or (pr and len(s["streams"]) > 1):
        out.append(Paragraph("Each pair of Pis", H3))
        out.append(_tbl([["From", "To", "Streams", "Sent", "Received", "Lost", "Sent Mb/s", "Recv Mb/s", "Jitter", "Delay p99"]] +
                        [[g["src"], g["dst"], g.get("streams"), f"{g.get('tx_pkts', 0):,}", f"{g.get('rx_pkts', 0):,}", f"{g.get('lost', 0):,} ({g.get('loss_pct')} %)", g.get("tx_mbps"),
                          g.get("rx_mbps"), _ms(g.get("jitter_ms")), _ms((g.get("lat_ms") or {}).get("p99")) if isinstance(g.get("lat_ms"), dict) else "-"] for g in pr]))
    out.append(Paragraph("Streams", H3))
    out.append(_tbl(rows, [32 * mm, 18 * mm, 24 * mm, 16 * mm, 16 * mm, 13 * mm, 14 * mm, 38 * mm, 14 * mm, 18 * mm]))
    ints = store.run_gen_intervals(r["id"])
    if ints:
        by = {}
        for i in ints:
            d = by.setdefault(i["t_offset_s"], {"tx": 0, "rx": 0, "lost": 0, "lat": [], "p95": [], "jit": []})
            d["tx"] += i["tx_bytes"] * 8 / 1e6
            d["rx"] += i["rx_bytes"] * 8 / 1e6
            d["lost"] += i["lost_cum"] or 0
            if i["lat_avg_us"] is not None:
                d["lat"].append(i["lat_avg_us"] / 1000.0)
            if i["lat_p95_us"] is not None:
                d["p95"].append(i["lat_p95_us"] / 1000.0)
            if i["jitter_us"] is not None:
                d["jit"].append(i["jitter_us"] / 1000.0)
        ts = sorted(by)
        out.append(Spacer(1, 4))
        out.append(_chart([("sent", [(t_, by[t_]["tx"]) for t_ in ts], GREY), ("received", [(t_, by[t_]["rx"]) for t_ in ts], TEAL)], "Throughput per second", "Mb/s"))
        if any(by[t_]["lat"] for t_ in ts):
            lim = (p.get("gen", {}).get("rule") or {}).get("max_latency_ms")
            out.append(_chart([("average", [(t_, (sum(by[t_]["lat"]) / len(by[t_]["lat"])) if by[t_]["lat"] else None) for t_ in ts], GREEN),
                               ("p95", [(t_, max(by[t_]["p95"]) if by[t_]["p95"] else None) for t_ in ts], TEAL),
                               ("jitter", [(t_, max(by[t_]["jit"]) if by[t_]["jit"] else None) for t_ in ts], GREY)], "One-way delay and jitter per second", "ms", hline=lim))
        out.append(_chart([("lost (cumulative)", [(t_, by[t_]["lost"]) for t_ in ts], RED)], "Packets lost so far", "packets"))
    rule = (p.get("gen") or {}).get("rule") or {}
    lim = [f"loss <= {rule['max_loss_pct']} %"] if rule.get("max_loss_pct") is not None else []
    if rule.get("max_latency_ms"):
        lim.append(f"delay p{rule.get('latency_percentile', 99)} <= {rule['max_latency_ms']} ms")
    if rule.get("max_jitter_ms"):
        lim.append(f"jitter <= {rule['max_jitter_ms']} ms")
    if rule.get("min_throughput_mbps"):
        lim.append(f"throughput >= {rule['min_throughput_mbps']} Mb/s")
    out.append(Paragraph("Pass rule: " + (", ".join(lim) or "none"), SMALL))
    for x in s["streams"]:
        for w in x.get("warnings", []):
            out.append(Paragraph(f"Note ({x['name']}): {w}", SMALL))
    return out


def _sniff_section(store, r) -> List:
    s, p = r["summary"] or {}, r["profile"]
    a = s.get("analysis") or {}
    modes = {"eth_bridge": "Inline Ethernet tap", "eth_span": "SPAN / mirror port", "serial": "Serial tap", "wifi": "Wi-Fi monitor"}
    out = [Paragraph(f"Traffic capture - {p.get('name', '')} &nbsp; <font size=9 color='#5c6b7a'>(run {r['id']}, {r['started_at']})</font>", H2),
           Paragraph(f"{modes.get(s.get('mode'), s.get('mode', ''))} - ran {s.get('elapsed_s', '?')} s - stopped: {s.get('stop_reason', '?')}", BODY)]
    if s.get("error"):
        out.append(Paragraph("Problem: " + str(s["error"]), BODY))
    if not a.get("packets"):
        out.append(Paragraph("No packets were captured.", BODY))
        return out
    out.append(Spacer(1, 4))
    out.append(_tbl([["Packets", "Bytes", "Duration", "Average rate", "Longest quiet gap", "Errors / CRC failures"],
                     [f"{a['packets']:,}", f"{a['bytes']:,}", f"{a['duration_s']} s", f"{a.get('pps') or '-'} pkt/s, {a.get('mbps') or '-'} Mb/s", f"{a.get('max_gap_ms', 0)} ms",
                      f"{a.get('errors', 0)} / {a.get('crc_failures', 0)}"]]))
    if a.get("protocols"):
        out.append(Paragraph("Protocols", H3))
        out.append(_tbl([["Protocol", "Packets", "Share"]] + [[k, f"{v:,}", f"{100.0 * v / a['packets']:.1f} %"] for k, v in a["protocols"].items()], [50 * mm, 30 * mm, 30 * mm]))
    if a.get("top_talkers"):
        out.append(Paragraph("Top talkers", H3))
        out.append(_tbl([["Source → destination", "Packets", "Bytes"]] + [[t["pair"], f"{t['packets']:,}", f"{t['bytes']:,}"] for t in a["top_talkers"]], [100 * mm, 25 * mm, 30 * mm]))
    if a.get("dnp3_frames") or a.get("modbus_frames"):
        out.append(Paragraph("Industrial protocols", H3))
        out.append(_tbl([["DNP3 frames", "Modbus frames", "CRC ok", "CRC failed"], [a.get("dnp3_frames", 0), a.get("modbus_frames", 0), a.get("crc_ok", 0), a.get("crc_failures", 0)]]))
        if a.get("functions"):
            out.append(Spacer(1, 3))
            out.append(_tbl([["Function", "Frames"]] + [[k, v] for k, v in a["functions"].items()], [90 * mm, 30 * mm]))
    if a.get("wifi_networks"):
        out.append(Paragraph("Wi-Fi networks heard", H3))
        out.append(_tbl([["Network (SSID)", "BSSID", "Channel", "Best signal"]] + [[n["ssid"] or "(hidden)", n["bssid"], n["channel"], f"{n['signal']} dBm" if n["signal"] is not None else "-"] for n in a["wifi_networks"][:25]],
                        [55 * mm, 45 * mm, 20 * mm, 25 * mm]))
    eng = s.get("engine") or {}
    if (eng.get("kernel_dropped") or 0) > 0:
        out.append(Paragraph(f"<b>The capture is not complete: the system dropped {int(eng['kernel_dropped']):,} packets.</b> The Pi could not save them fast enough. Capture with a filter, a short snap length (headers only), or to a USB drive.", SMALL))
    if eng.get("kernel_dropped") is not None:
        out.append(Paragraph(f"Capture engine: {eng.get('engine')}; packets the kernel dropped: {eng.get('kernel_dropped')}.", SMALL))
    if (s.get("serial") or {}).get("timestamp_note"):
        out.append(Paragraph("Timing: " + s["serial"]["timestamp_note"], SMALL))
    out.append(Paragraph("Files: " + ", ".join(f"{f['name']} ({f['bytes'] // 1024} KB)" for f in s.get("files", [])[:8]) + f" - stored in {s.get('location')}", SMALL))
    return out


def _job_block(job: dict) -> List:
    """Job info as a small two-column block (cover of the report, head of a comparison)."""
    rows = [[label, Paragraph(_x(str(job[k])), BODY)] for k, label, _ in CMP.JOB_FIELDS if job.get(k)]
    if not rows:
        return []
    t = Table(rows, colWidths=[26 * mm, 148 * mm])
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8), ("TEXTCOLOR", (0, 0), (0, -1), GREY), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor("#d5dde5")), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f6f9fb")),
                           ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return [Paragraph("Job", H3), t, Spacer(1, 6)]


def _job_para(job: dict) -> List:
    line = CMP.job_line(job)
    out = [Paragraph("<b>Job:</b> " + _x(line), BODY)] if line else []
    if job.get("note"):
        out.append(Paragraph("<b>Note:</b> " + _x(str(job["note"])), BODY))
    return out


def _vs_para(store, r) -> List:
    v = CMP.vs_baseline(store, r)
    if not v:
        return []
    if v["is_baseline"]:
        return [Paragraph("This result is the baseline for this test.", SMALL)]
    return [Paragraph(_x(v["text"]) + f" &nbsp; <font size=7 color='#5c6b7a'>(baseline: run {v['run_id']}, {v.get('started_at') or ''})</font>", BODY)]


CMP_COLORS = [colors.HexColor(c) for c in ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")]      # the same four series colours as the Compare screen


def _footer(canvas, d):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(GREY)
    canvas.drawString(18 * mm, 9 * mm, f"Pi Automation Traffic Tester {__version__}")
    canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"Page {d.page}")
    canvas.restoreState()


def build_comparison(store, run_ids: List[int], hostname: Optional[str] = None, pi_name: Optional[str] = None) -> bytes:
    """One page: 2 to 4 results of the same kind side by side (SPEC 7.5). Raises compare.CompareError like GET /api/compare."""
    c = CMP.compare(store, run_ids)
    runs, letters = c["runs"], [r["letter"] for r in c["runs"]]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title="Comparison", author="Pi Automation Traffic Tester")
    story: List = [Paragraph("Comparison", H1),
                   Paragraph(f"{len(runs)} results of the same kind ({_x(c['kind_name'])}) side by side. Generated {dt.datetime.now():%Y-%m-%d %H:%M} on "
                             f"{_x(pi_name or hostname or socket.gethostname())}. Change is measured against A.", BODY), Spacer(1, 6)]
    head = [["", "Test", "Job", "When", "Result"]]
    for r in runs:
        col = "#" + CMP_COLORS[letters.index(r["letter"])].hexval()[2:]
        name = _x(r["name"]) + (f"<br/><font size=7 color='#5c6b7a'>{_x(r['route'])}</font>" if r.get("route") else "") + ("<br/><font size=7 color='#0f766e'>baseline</font>" if r.get("baseline") else "")
        job = _x(r["job_line"]) + (f"<br/><font size=7 color='#5c6b7a'>{_x(str(r['job'].get('note')))}</font>" if (r.get("job") or {}).get("note") else "")
        head.append([Paragraph(f'<font color="{col}" size=11>\u25a0</font> <b>{r["letter"]}</b>', BODY), Paragraph(name, SMALL), Paragraph(job or "-", SMALL),
                     f"{r.get('started_at') or ''}\nrun {r['id']}", r.get("verdict") or r.get("status") or ""])
    story.append(_tbl(head, [12 * mm, 54 * mm, 54 * mm, 36 * mm, 18 * mm]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(_x(c["sentence"]), ParagraphStyle("sent", parent=BODY, fontSize=10.5, leading=14)))
    story.append(Spacer(1, 6))
    rows = [[""] + letters + [f"Change ({l} against A)" for l in letters[1:]]]
    marks = []
    for n, row in enumerate(c["rows"], start=1):
        line = [row["label"]] + [CMP.fmt(v, row["unit"]) for v in row["values"]]
        for k, ch in enumerate(row["change"][1:]):
            if ch is None:
                line.append("-")
                continue
            if ch["state"] == "same":
                line.append("same")
            else:
                line.append(("+" if ch["abs"] > 0 else "-") + CMP.fmt(abs(ch["abs"]), row["unit"]) + (f" ({'+' if ch['pct'] > 0 else '-'}{abs(ch['pct']):g} %)" if ch.get("pct") is not None else ""))
                if ch.get("judged"):
                    marks.append((1 + len(letters) + k, n, GREEN if ch["judged"] == "better" else RED))
        rows.append(line)
    nl = len(letters)
    w0 = 52 * mm
    wv = (174 * mm - w0) / (2 * nl - 1)
    t = _tbl(rows, [w0] + [wv] * (2 * nl - 1))
    t.setStyle(TableStyle([("TEXTCOLOR", (col, row), (col, row), clr) for col, row, clr in marks] + [("FONTNAME", (col, row), (col, row), "Helvetica-Bold") for col, row, _ in marks]))
    story.append(t)
    story.append(Spacer(1, 8))
    ch = c.get("chart")
    if ch and ch["type"] == "hist":
        story.append(_chart([(f"{s['letter']} ({s['n']:,} messages)", list(zip(ch["edges"], s["cum"])), CMP_COLORS[i]) for i, s in enumerate(ch["series"])],
                            ch["title"] + ": share of messages (%) at least this fast (ms)" + (f"; limit {ch['limit_ms']:g} ms" if ch.get("limit_ms") else ""), "%", height=62 * mm))
    elif ch:
        story.append(_chart([(f"{s['letter']} received", list(zip(s["t"], s["rx_mbps"])), CMP_COLORS[i]) for i, s in enumerate(ch["series"])],
                            "Received per second (Mb/s), each test from 0 s", "Mb/s", height=50 * mm))
        if any(v is not None for s in ch["series"] for v in s["lat_ms"]):
            story.append(_chart([(f"{s['letter']} delay", list(zip(s["t"], s["lat_ms"])), CMP_COLORS[i]) for i, s in enumerate(ch["series"])],
                                "Average one-way delay per second (ms)", "ms", height=42 * mm))
    if ch:
        story.append(Paragraph(_x(ch["note"]), SMALL))
    story.append(Spacer(1, 6))
    story.append(Paragraph("<b>Setup differences:</b> " + _x(c["setup_line"]), BODY))
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _tester_marks_para(r: dict) -> list:
    """One line for the tester lamp (was the Pi itself the limit?) and one for the ⚑ marks dropped during the test."""
    s, out = r.get("summary") or {}, []
    t = s.get("tester")
    if t and t.get("level"):
        clr = {"ok": GREEN, "warn": colors.HexColor("#b45309"), "limit": RED}.get(t["level"], GREEN)
        out.append(Paragraph(f'<font color="{clr.hexval()}"><b>{_x(t.get("title") or "")}</b></font> {_x(t.get("text") or "")}', BODY))
    mk = s.get("marks") or []
    if mk:
        out.append(Paragraph("<b>Marks:</b> " + "; ".join(f"Mark {m.get('n')} at {m.get('t_s')} s" + (f" ({_x(m.get('name'))})" if m.get("name") else "") for m in mk), BODY))
    return out


def build_report(store, run_ids: List[int], title: str = "Link test report", hostname: Optional[str] = None, pi_name: Optional[str] = None, repeat: Optional[dict] = None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=title, author="Pi Automation Traffic Tester")

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GREY)
        canvas.drawString(18 * mm, 9 * mm, f"Pi Automation Traffic Tester {__version__}")
        canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"Page {d.page}")
        canvas.restoreState()

    runs = [r for r in (store.get_run(i) for i in run_ids) if r]
    story: List = [Paragraph(title, H1), Paragraph(f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} on {_x(pi_name or hostname or socket.gethostname())} - {len(runs)} test(s)", BODY), Spacer(1, 6)]
    jobs = {tuple(sorted((r.get("job") or {}).items())) for r in runs}
    one_job = dict(next(iter(jobs))) if len(jobs) == 1 else None      # every test of the report belongs to the same job: it goes on the cover, once
    if one_job:
        story += _job_block(one_job)
    summ = [["#", "Test", "Kind", "When", "Result", "Key figure"]]
    for r in runs:
        s = r["summary"] or {}
        if s.get("kind") == "generator":
            t = s.get("totals") or {}
            key = f"{t.get('rx_mbps', '-')} Mb/s, loss {t.get('loss_pct', '-')} %"
            kind = "Traffic generator"
        elif s.get("kind") == "sniffer":
            a = s.get("analysis") or {}
            key = f"{a.get('packets', 0):,} packets, {a.get('bytes', 0):,} bytes"
            kind = "Traffic capture"
        else:
            key = f"p99 {_ms((s.get('rtt_ms') or {}).get('p99'))}, loss {s.get('loss_pct', '-')} %"
            kind = (r.get("transport") or "").upper()
        rt = _route(r) if s.get("kind") not in ("generator", "sniffer") else ""
        if s.get("kind") == "generator" and s.get("nodes"):
            rt = ", ".join(str(n) for n in s["nodes"])
        nm = (r["profile"] or {}).get("name", "")
        nm = Paragraph(_x(nm) + f"<br/><font size=7 color='#5c6b7a'>{_x(rt)}</font>", SMALL) if rt else nm
        summ.append([r["id"], nm, kind, r["started_at"], r["verdict"] or r["status"], key])
    story.append(_tbl(summ, [8 * mm, 52 * mm, 28 * mm, 34 * mm, 16 * mm, 40 * mm]))
    if repeat:
        story.append(PageBreak())
        story += _combined_block(repeat)
    for r in runs:
        story.append(PageBreak())
        kind = (r["summary"] or {}).get("kind")
        sec = (_gen_section if kind == "generator" else _sniff_section if kind == "sniffer" else _trip_section)(store, r)
        extra = ([] if one_job is not None else _job_para(r.get("job") or {})) + _vs_para(store, r) + _tester_marks_para(r)
        story += sec[:2] + extra + sec[2:]                              # under the test's name and verdict
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
