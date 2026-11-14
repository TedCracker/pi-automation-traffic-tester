"""Packet decoding (live view) and capture-file summaries (top talkers, protocols, errors, timing, Wi-Fi networks)."""
from __future__ import annotations

import os
import struct
import time
from collections import Counter
from typing import Dict, Iterable, List, Optional

from . import dnp3, modbus, pcapng

ETHERTYPES = {0x0800: "IPv4", 0x0806: "ARP", 0x86DD: "IPv6", 0x8100: "VLAN", 0x88B8: "GOOSE", 0x88BA: "SV", 0x88CC: "LLDP", 0x88B5: "Generator", 0x8847: "MPLS", 0x0842: "WoL", 0x88F7: "PTP"}


def mac(b: bytes) -> str:
    return ":".join("%02x" % x for x in b)


def _tcp_flags(f: int) -> str:
    return "".join(n for bit, n in ((0x02, "SYN"), (0x10, "ACK"), (0x08, "PSH"), (0x01, "FIN"), (0x04, "RST")) if f & bit) or "-"


def decode_ethernet(d: bytes) -> dict:
    r = {"proto": "Ethernet", "src": "", "dst": "", "info": "", "len": len(d), "app": None, "vlan": None, "errors": 0}
    if len(d) < 14:
        r.update(proto="Malformed", info="frame shorter than an Ethernet header", errors=1)
        return r
    r["dst"], r["src"] = mac(d[0:6]), mac(d[6:12])
    et, off = struct.unpack(">H", d[12:14])[0], 14
    if et == 0x8100 and len(d) >= 18:
        tci = struct.unpack(">H", d[14:16])[0]
        r["vlan"] = {"id": tci & 0xFFF, "prio": tci >> 13}
        et, off = struct.unpack(">H", d[16:18])[0], 18
    r["ethertype"] = et
    r["proto"] = ETHERTYPES.get(et, f"0x{et:04X}")
    if et == 0x0806 and len(d) >= off + 28:
        op = struct.unpack(">H", d[off + 6:off + 8])[0]
        sip, tip = ".".join(str(x) for x in d[off + 14:off + 18]), ".".join(str(x) for x in d[off + 24:off + 28])
        r.update(src=sip, dst=tip, info=("who has %s? tell %s" % (tip, sip)) if op == 1 else f"{sip} is at {mac(d[off + 8:off + 14])}")
    elif et == 0x0800 and len(d) >= off + 20:
        ihl = (d[off] & 15) * 4
        proto, src, dst = d[off + 9], ".".join(str(x) for x in d[off + 12:off + 16]), ".".join(str(x) for x in d[off + 16:off + 20])
        r.update(src=src, dst=dst)
        p, tt = d[off + ihl:], d[off + 8]
        if proto == 6 and len(p) >= 20:
            sp, dp, seq, ack, doff, fl = struct.unpack(">HHIIBB", p[:14])
            payload = p[(doff >> 4) * 4:]
            r.update(proto="TCP", src=f"{src}:{sp}", dst=f"{dst}:{dp}", info=f"{sp} → {dp} [{_tcp_flags(fl)}] len {len(payload)}")
            _app(r, payload, sp, dp, True)
        elif proto == 17 and len(p) >= 8:
            sp, dp, ln = struct.unpack(">HHH", p[:6])
            r.update(proto="UDP", src=f"{src}:{sp}", dst=f"{dst}:{dp}", info=f"{sp} → {dp} len {len(p) - 8}")
            _app(r, p[8:], sp, dp, False)
        elif proto == 1:
            r.update(proto="ICMP", info=f"type {p[0] if p else '?'}")
        else:
            r.update(proto=f"IP proto {proto}")
    elif et == 0x0800:
        r.update(proto="Malformed", info="truncated IPv4 header", errors=1)
    elif et == 0x86DD and len(d) >= off + 40:
        nh = d[off + 6]
        r.update(src=":".join("%x" % x for x in struct.unpack(">8H", d[off + 8:off + 24])), dst=":".join("%x" % x for x in struct.unpack(">8H", d[off + 24:off + 40])),
                 proto={6: "TCP", 17: "UDP", 58: "ICMPv6"}.get(nh, "IPv6"))
    elif et == 0x88B8 and len(d) >= off + 8:
        appid = struct.unpack(">H", d[off:off + 2])[0]
        r.update(info=f"GOOSE APPID 0x{appid:04X}")
        try:
            from ..goose import pdu_decode
            p = pdu_decode(d[off + 8:])
            r["info"] = f"GOOSE 0x{appid:04X} {p['gocb']} stNum {p['st']} sqNum {p['sq']}"
        except Exception:
            pass
    if r["vlan"]:
        r["info"] = f"VLAN {r['vlan']['id']} prio {r['vlan']['prio']}  " + r["info"]
    return r


def _app(r: dict, payload: bytes, sp: int, dp: int, tcp: bool) -> None:
    if (20000 in (sp, dp)) and payload[:2] == dnp3.START:
        dd = dnp3.decode_frame(payload[:dnp3.frame_len(payload[2])] if len(payload) > 2 else payload)
        r.update(proto="DNP3", info=dd["summary"], app=dd)
        r["errors"] += 0 if dd["crc_ok"] else 1
    elif tcp and 502 in (sp, dp):
        dd = modbus.decode_mbap(payload)
        if dd:
            r.update(proto="Modbus/TCP", info=dd["summary"], app=dd)


def decode_radiotap(d: bytes) -> dict:
    r = {"proto": "802.11", "src": "", "dst": "", "info": "", "len": len(d), "app": None, "errors": 0, "signal": None, "channel": None}
    if len(d) < 8:
        r.update(proto="Malformed", errors=1)
        return r
    rlen = struct.unpack("<H", d[2:4])[0]
    present = struct.unpack("<I", d[4:8])[0]
    pos = 8
    hdr_end = min(rlen, len(d))                                     # a truncated frame can claim a longer header than it has
    while present & 0x80000000 and pos + 4 <= hdr_end:              # extended present words
        present2 = struct.unpack("<I", d[pos:pos + 4])[0]
        pos += 4
        if not present2 & 0x80000000:
            break
    off = pos
    # alignment/size of fields 0..5: TSFT(8,a8) Flags(1) Rate(1) Channel(4,a2) FHSS(2) dBm signal(1)
    for bit, size, align in ((0, 8, 8), (1, 1, 1), (2, 1, 1), (3, 4, 2), (4, 2, 2), (5, 1, 1)):
        if not present >> bit & 1:
            continue
        off += (-off) % align
        if bit == 3 and off + 4 <= hdr_end:
            freq, _fl = struct.unpack("<HH", d[off:off + 4])
            r["channel"] = _freq_to_channel(freq)
            r["freq"] = freq
        if bit == 5 and off + 1 <= hdr_end:
            r["signal"] = struct.unpack("<b", d[off:off + 1])[0]
        off += size
    f = d[rlen:]
    if len(f) < 10 or (len(f) < 24 and (f[0] >> 2) & 3 != 1):      # control frames (ACK, CTS, RTS ...) are shorter than a 24-byte header
        r["info"] = "radiotap only / short frame"
        return r
    fc = f[0]
    ftype, sub = (fc >> 2) & 3, fc >> 4
    names = {(0, 8): "Beacon", (0, 4): "Probe Request", (0, 5): "Probe Response", (0, 0): "Association Request", (0, 1): "Association Response", (0, 11): "Authentication",
             (0, 12): "Deauthentication", (0, 10): "Disassociation", (1, 11): "RTS", (1, 12): "CTS", (1, 13): "ACK", (2, 0): "Data", (2, 8): "QoS Data"}
    name = names.get((ftype, sub), f"type {ftype} subtype {sub}")
    has_ta = len(f) >= 16 and not (ftype == 1 and sub in (12, 13))      # CTS and ACK carry a receiver address only
    r.update(proto="802.11", dst=mac(f[4:10]), src=mac(f[10:16]) if has_ta else "", info=name)
    if ftype == 0 and sub in (8, 5) and len(f) >= 36:
        r["bssid"] = mac(f[16:22])
        i, ssid = 36, ""
        while i + 2 <= len(f):
            t, l = f[i], f[i + 1]
            v = f[i + 2:i + 2 + l]
            if t == 0:
                ssid = v.decode("utf-8", "replace")
            elif t == 3 and l == 1 and v:
                r["channel"] = v[0]
            i += 2 + l
        r["ssid"] = ssid
        r["info"] = f"{name} SSID '{ssid}' ch {r.get('channel')} {r['signal']} dBm"
    return r


def _freq_to_channel(f: int) -> Optional[int]:
    if 2412 <= f <= 2472:
        return (f - 2407) // 5
    if f == 2484:
        return 14
    if 5000 <= f <= 5900:
        return (f - 5000) // 5
    if 5955 <= f <= 7115:
        return (f - 5950) // 5
    return None


def malformed(data: bytes) -> dict:
    """Row for a packet whose decoding failed (the caller keeps going with the next packet)."""
    return {"proto": "Malformed", "src": "", "dst": "", "info": "packet could not be decoded", "len": len(data), "app": None, "errors": 1}


def decode_packet(data: bytes, linktype: int, comment: str = "") -> dict:
    if linktype == pcapng.LINKTYPE_IEEE802_11_RADIOTAP:
        return decode_radiotap(data)
    if linktype == pcapng.LINKTYPE_USER0:
        d = modbus.decode_rtu(data, None)
        return {"proto": "Modbus RTU", "src": "", "dst": "", "info": d["summary"], "len": len(data), "app": d, "errors": 0 if d["crc_ok"] else 1}
    r = decode_ethernet(data)
    if "crc=FAIL" in comment and (not r.get("app") or r["app"].get("crc_ok", True)):      # not twice when the DNP3 decode already counted it
        r["errors"] += 1
    return r


# ------------------------------------------------------------------ capture summary
def summarize_files(paths: Iterable[str], max_packets: int = 3_000_000, max_seconds: float = 60.0) -> dict:
    t_start = time.monotonic()
    proto, talkers = Counter(), Counter()
    talker_pk = Counter()
    nets: Dict[str, dict] = {}
    n = nbytes = errors = 0
    t_first = t_last = None
    prev = None
    max_gap = 0
    crc_fail = crc_ok_n = 0
    dnp3_n = mb_n = 0
    func_count: Counter = Counter()
    truncated = False
    vlans: Counter = Counter()
    for path in paths:
        try:
            for p in pcapng.read_capture(path):
                if n >= max_packets or time.monotonic() - t_start > max_seconds:
                    truncated = True
                    break
                try:
                    r = decode_packet(p["data"], p["linktype"], p["comment"])
                except Exception:  # noqa - one undecodable packet must not drop the rest of the file
                    r = malformed(p["data"])
                n += 1
                nbytes += p["orig_len"] or len(p["data"])
                ts = p["ts_ns"]
                if ts:
                    t_first = ts if t_first is None else min(t_first, ts)
                    t_last = ts if t_last is None else max(t_last, ts)
                    if prev is not None and ts > prev:
                        max_gap = max(max_gap, ts - prev)
                    prev = ts
                proto[r["proto"]] += 1
                errors += r.get("errors", 0)
                if r["src"] or r["dst"]:
                    key = f"{r['src']} → {r['dst']}"
                    talkers[key] += p["orig_len"] or len(p["data"])
                    talker_pk[key] += 1
                if r.get("vlan"):
                    vlans[r["vlan"]["id"]] += 1
                if "crc=FAIL" in p["comment"]:
                    crc_fail += 1
                app = r.get("app")
                if app:
                    if app["proto"].startswith("DNP3"):
                        dnp3_n += 1
                    else:
                        mb_n += 1
                    if app["crc_ok"]:
                        crc_ok_n += 1
                    elif "crc=FAIL" not in p["comment"]:
                        crc_fail += 1
                    func_count[f"{app['proto']}: {app['summary'].split('|')[1].strip().split(' ')[0] if '|' in app['summary'] else app.get('kind', '')}"] += 1
                if r["proto"] == "802.11" and r.get("ssid") is not None and r.get("bssid"):
                    k = r["bssid"]
                    e = nets.setdefault(k, {"bssid": k, "ssid": r["ssid"], "channel": r.get("channel"), "signal": r.get("signal"), "beacons": 0})
                    e["beacons"] += 1
                    if r.get("signal") is not None and (e["signal"] is None or r["signal"] > e["signal"]):
                        e["signal"] = r["signal"]
        except Exception as e:  # noqa
            errors += 1
        if truncated:
            break
    dur = ((t_last - t_first) / 1e9) if (t_first is not None and t_last is not None) else 0.0
    return {"packets": n, "bytes": nbytes, "duration_s": round(dur, 3), "pps": round(n / dur, 1) if dur > 0 else None, "mbps": round(nbytes * 8 / dur / 1e6, 4) if dur > 0 else None,
            "protocols": dict(proto.most_common(15)),
            "top_talkers": [{"pair": k, "bytes": v, "packets": talker_pk[k]} for k, v in talkers.most_common(8)],
            "errors": errors, "crc_failures": crc_fail, "crc_ok": crc_ok_n, "dnp3_frames": dnp3_n, "modbus_frames": mb_n,
            "functions": dict(func_count.most_common(12)), "vlans": dict(vlans),
            "max_gap_ms": round(max_gap / 1e6, 3), "first_packet_ns": t_first, "last_packet_ns": t_last,
            "wifi_networks": sorted(nets.values(), key=lambda x: (x["channel"] or 0, -(x["signal"] or -200)))[:60],
            "truncated": truncated}


def layers(data: bytes, linktype: int) -> List[dict]:
    """Layer-by-layer detail of one packet for the UI (on demand, not for every packet)."""
    out: List[dict] = []
    if linktype != pcapng.LINKTYPE_ETHERNET:
        r = decode_packet(data, linktype)
        out.append({"name": r["proto"], "fields": {"Summary": r["info"], "Source": r["src"], "Destination": r["dst"]}})
        if r.get("signal") is not None:
            out[0]["fields"]["Signal"] = f"{r['signal']} dBm"
        if r.get("channel"):
            out[0]["fields"]["Channel"] = r["channel"]
        if r.get("app"):
            out.append({"name": r["app"]["proto"], "fields": r["app"].get("fields", {})})
        return out
    r = decode_ethernet(data)
    out.append({"name": "Ethernet", "fields": {"Destination": r["dst"], "Source": r["src"], "Type": r["proto"] if r.get("ethertype") is None else f"0x{r.get('ethertype', 0):04X} ({ETHERTYPES.get(r.get('ethertype'), '?')})"}})
    if r.get("vlan"):
        out.append({"name": "802.1Q VLAN", "fields": {"VLAN id": r["vlan"]["id"], "Priority": r["vlan"]["prio"]}})
    ip4 = r.get("ethertype") == 0x0800
    off = 18 if r.get("vlan") else 14
    if ip4 and r["proto"] in ("TCP", "UDP", "ICMP", "IPv4", "DNP3", "Modbus/TCP") and len(data) > 34:
        out.append({"name": "IPv4", "fields": {"Source": r["src"].split(":")[0], "Destination": r["dst"].split(":")[0], "TTL": data[off + 8], "Protocol": data[off + 9], "Length": len(data) - off}})
    elif r.get("ethertype") == 0x86DD and r["proto"] in ("TCP", "UDP"):                      # decode_ethernet only names these with a full 40-byte IPv6 header
        out.append({"name": "IPv6", "fields": {"Source": r["src"], "Destination": r["dst"], "Hop limit": data[off + 7], "Next header": f"{data[off + 6]} ({r['proto']})", "Length": len(data) - off}})
    if ip4 and r["proto"] in ("TCP", "DNP3", "Modbus/TCP", "UDP"):
        out.append({"name": "Ports", "fields": {"Source": r["src"], "Destination": r["dst"], "Info": r["info"]}})
    if r.get("app"):
        out.append({"name": r["app"]["proto"], "fields": r["app"].get("fields", {})})
    elif r["proto"] not in ("TCP", "UDP"):
        out.append({"name": r["proto"], "fields": {"Info": r["info"]}})
    return out
