"""Stored results for the compare / job / baseline tests: made straight in the database, no test is run."""
import base64, re, zlib
from traffic_tester import profiles as P


def pdf_text(pdf: bytes) -> str:
    out = b""
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        try:
            out += zlib.decompress(base64.a85decode(m.group(1).strip(), adobe=True))
        except Exception:
            pass
    return out.decode("latin-1")


def trip_run(st, name="DER trip", p50=25.0, p99=41.0, lost=0, sent=1000, thr=60, tester="pi1", refl="pi2", gid=None, msgs=True, extra_prof=None, status="finished"):
    prof = P.validate_profile({"name": name, "transport": "udp", "net": {"peer_host": "10.0.0.2"}, "threshold_ms": thr, **(extra_prof or {})})
    leg = {"label": f"{tester} → {refl}", "kind": "round_trip", "basis": "rtt", "tester": tester, "reflector": refl, "controller": "pi1"}
    rid = st.create_run(st.current_session()["id"], prof, tester, "udp:20500", group_id=gid, leg=leg)
    if msgs:
        rows = [{"seq": i, "phase": "steady", "idle_gap_s": None, "t_send_ms": i * 50.0, "rtt_us": int((p50 + (i % 10) * (p99 - p50) / 9.0) * 1000), "up_us": None, "down_us": None, "status": "ok"} for i in range(200)]
        st.add_messages(rid, rows)
    summary = {"sent": sent, "received": sent - lost, "lost": lost, "loss_pct": round(100.0 * lost / sent, 3), "over_threshold": 0, "over_threshold_pct": 0.0, "threshold_ms": thr,
               "rtt_ms": {"p50": p50, "p95": (p50 + p99) / 2, "p99": p99, "max": p99 * 1.2}, "jitter_ms": 1.5, "verdict": {"pass": p99 <= thr and not lost, "reasons": []},
               "endpoints": {"source": tester, "dest": refl, "kind": "round_trip", "basis": "rtt", "controller": "pi1", "label": leg["label"]}}
    if status == "running":
        return rid
    st.finish_run(rid, status, summary)
    return rid


def gen_run(st, name="Throughput", rx=5.0, loss=0.0, p99=None, secs=5):
    prof = P.validate_profile({"name": name, "kind": "generator", "gen": {"streams": [{"name": "s1"}], "duration_s": secs}})
    rid = st.create_run(st.current_session()["id"], prof, "pi1", "")
    st.add_gen_intervals(rid, [{"t_offset_s": float(t), "stream": 0, "tx_pkts": 100, "tx_bytes": int(rx * 1e6 / 8), "rx_pkts": 100, "rx_bytes": int(rx * 1e6 / 8), "lost_cum": 0,
                                "lat_avg_us": int(p99 * 500) if p99 else None, "lat_p95_us": None, "lat_max_us": None, "jitter_us": None, "rtt_avg_us": None} for t in range(1, secs + 1)])
    lat = {"p50": p99 / 2, "p95": p99 * 0.9, "p99": p99, "max": p99 * 1.5} if p99 else None
    streams = [{"id": 0, "name": "s1", "type": "udp", "src": "pi1", "dst": "pi2", "size": "1000 B", "tx": {"mbps": rx, "pkts": 500, "bytes": 1}, "rx": {"mbps": rx, "pps": 100, "unique": 500},
                "loss_pct": loss, "lost": 0, "lat_ms": lat, "jitter_ms": 0.4, "rtt_ms": None, "warnings": []}]
    st.finish_run(rid, "finished", {"kind": "generator", "streams": streams, "pairs": [], "nodes": ["pi1", "pi2"], "duration_s": secs,
                                    "totals": {"tx_pkts": 500, "rx_pkts": 500, "lost": 0, "loss_pct": loss, "tx_mbps": rx, "rx_mbps": rx, "lat_ms": lat}, "clocks": {"valid": bool(p99)},
                                    "verdict": {"pass": True, "reasons": []}})
    return rid


def mb_run(st, name="Mirrored Bits", p99=6.0, avail=99.9):
    prof = P.validate_profile({"name": name, "transport": "udp", "mode": "mirrored", "net": {"peer_host": "10.0.0.2"}})
    leg = {"label": "pi1 → pi2", "kind": "round_trip", "basis": "rtt", "tester": "pi1", "reflector": "pi2", "controller": "pi1"}
    rid = st.create_run(st.current_session()["id"], prof, "pi1", "udp:20500", leg=leg)
    st.add_messages(rid, [{"seq": i, "phase": "steady", "idle_gap_s": None, "t_send_ms": i * 2000.0, "rtt_us": int(p99 * 800), "up_us": None, "down_us": None, "status": "ok"} for i in range(20)])
    st.finish_run(rid, "finished", {"sent": 20, "received": 20, "lost": 0, "loss_pct": 0, "rtt_ms": {"p50": p99 / 2, "p95": p99, "p99": p99, "max": p99}, "threshold_ms": 60,
                                    "mirrored": {"emulator": True, "transfer": {"p50": p99 / 2, "p99": p99, "max": p99 * 1.1}, "rtt": {"p50": p99, "p99": p99 * 2, "alarm_events": 0},
                                                 "availability_pct": avail, "longest_outage_ms": 0, "rok_drops": 0, "rejects": {"parity": 1}, "rbad_events": 0, "cbad_events": 0},
                                    "verdict": {"pass": True, "reasons": []}, "endpoints": {"source": "pi1", "dest": "pi2", "kind": "round_trip", "basis": "rtt", "label": "pi1 → pi2"}})
    return rid
