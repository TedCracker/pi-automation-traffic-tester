"""Decoders added in the sniffer workbench: SNMP, IEC 60870-5-104, PTP (IEEE 1588), GOOSE and R-GOOSE (IEC 61850).

Every decoder takes the payload bytes and returns {"proto", "crc_ok", "summary", "fields", "kind"} (the same shape the DNP3 and Modbus decoders use)
or None when the bytes are not that protocol. They never raise on short or damaged input: a damaged frame comes back with crc_ok False.
"""
from __future__ import annotations

import struct
from typing import List, Optional, Tuple


def _hexs(b: bytes, n: int = 16) -> str:
    return b[:n].hex(" ") + (" …" if len(b) > n else "")


# ------------------------------------------------------------------ BER helpers (SNMP, GOOSE)
def _tlv(b: bytes, i: int) -> Tuple[int, bytes, int]:
    if i + 2 > len(b):
        raise ValueError("short")
    tag, ln = b[i], b[i + 1]
    j = i + 2
    if ln & 0x80:
        n = ln & 0x7F
        if n == 0 or n > 4 or j + n > len(b):
            raise ValueError("bad length")
        ln, j = int.from_bytes(b[j:j + n], "big"), j + n
    if j + ln > len(b):
        raise ValueError("truncated")
    return tag, b[j:j + ln], j + ln


def _int(v: bytes, signed: bool = True) -> int:
    return int.from_bytes(v, "big", signed=signed) if v else 0


# ------------------------------------------------------------------ SNMP
SNMP_PDUS = {0xA0: "get-request", 0xA1: "get-next-request", 0xA2: "get-response", 0xA3: "set-request", 0xA4: "trap (v1)", 0xA5: "get-bulk-request", 0xA6: "inform-request", 0xA7: "trap (v2)", 0xA8: "report"}
SNMP_ERR = {0: "noError", 1: "tooBig", 2: "noSuchName", 3: "badValue", 4: "readOnly", 5: "genErr", 6: "noAccess", 7: "wrongType", 8: "wrongLength", 9: "wrongEncoding", 10: "wrongValue", 11: "noCreation",
            12: "inconsistentValue", 13: "resourceUnavailable", 14: "commitFailed", 15: "undoFailed", 16: "authorizationError", 17: "notWritable", 18: "inconsistentName"}
OID_NAMES = {"1.3.6.1.2.1.1.1": "sysDescr", "1.3.6.1.2.1.1.2": "sysObjectID", "1.3.6.1.2.1.1.3": "sysUpTime", "1.3.6.1.2.1.1.4": "sysContact", "1.3.6.1.2.1.1.5": "sysName", "1.3.6.1.2.1.1.6": "sysLocation",
             "1.3.6.1.2.1.2.1": "ifNumber", "1.3.6.1.2.1.2.2": "ifTable", "1.3.6.1.2.1.2.2.1.2": "ifDescr", "1.3.6.1.2.1.2.2.1.8": "ifOperStatus", "1.3.6.1.2.1.2.2.1.10": "ifInOctets",
             "1.3.6.1.2.1.2.2.1.16": "ifOutOctets", "1.3.6.1.2.1.31.1.1.1.6": "ifHCInOctets", "1.3.6.1.2.1.31.1.1.1.10": "ifHCOutOctets", "1.3.6.1.6.3.1.1.4.1.0": "snmpTrapOID",
             "1.3.6.1.6.3.1.1.5.3": "linkDown", "1.3.6.1.6.3.1.1.5.4": "linkUp", "1.3.6.1.6.3.1.1.5.1": "coldStart", "1.3.6.1.2.1.25.3.3.1.2": "hrProcessorLoad"}


def _oid(v: bytes) -> str:
    if not v:
        return ""
    a = [str(v[0] // 40), str(v[0] % 40)]
    n = 0
    for x in v[1:]:
        n = (n << 7) | (x & 0x7F)
        if not x & 0x80:
            a.append(str(n))
            n = 0
    return ".".join(a)


def _oid_name(o: str) -> str:
    for k in sorted(OID_NAMES, key=len, reverse=True):
        if o == k or o.startswith(k + "."):
            return f"{OID_NAMES[k]}{o[len(k):]}"
    return o


def _snmp_value(tag: int, v: bytes) -> str:
    if tag == 0x02:
        return str(_int(v))
    if tag == 0x04:
        try:
            s = v.decode("ascii")
            if s.isprintable():
                return f'"{s}"'
        except UnicodeDecodeError:
            pass
        return "0x" + v.hex()
    if tag == 0x05:
        return "NULL"
    if tag == 0x06:
        return _oid_name(_oid(v))
    if tag == 0x40 and len(v) == 4:
        return ".".join(str(x) for x in v)
    if tag in (0x41, 0x42, 0x46):
        return f"{_int(v, False)}" + (" (counter)" if tag != 0x42 else " (gauge)")
    if tag == 0x43:
        return f"{_int(v, False) / 100:.2f} s (timeticks)"
    if tag == 0x80:
        return "noSuchObject"
    if tag == 0x81:
        return "noSuchInstance"
    if tag == 0x82:
        return "endOfMibView"
    return "0x" + v.hex()


def decode_snmp(p: bytes) -> Optional[dict]:
    try:
        tag, body, _ = _tlv(p, 0)
        if tag != 0x30:
            return None
        t, v, i = _tlv(body, 0)
        if t != 0x02:
            return None
        ver = _int(v)
        if ver not in (0, 1, 3):
            return None
        f: dict = {"Version": {0: "SNMPv1", 1: "SNMPv2c", 3: "SNMPv3"}[ver]}
        if ver == 3:
            t, hdr, i = _tlv(body, i)
            mid = _int(_tlv(hdr, 0)[1]) if t == 0x30 else None
            f["Message id"] = mid
            f["Note"] = "SNMPv3: the scoped PDU is usually encrypted, so only the header is shown"
            return {"proto": "SNMP", "crc_ok": True, "kind": "v3", "summary": f"SNMPv3 message id {mid}", "fields": f}
        t, comm, i = _tlv(body, i)
        f["Community"] = comm.decode("ascii", "replace")
        t, pdu, i = _tlv(body, i)
        kind = SNMP_PDUS.get(t)
        if not kind:
            return None
        f["PDU type"] = kind
        j, vbs, extra = 0, [], ""
        if t == 0xA4:                                           # SNMPv1 trap: enterprise, agent address, generic, specific, time-stamp
            _, ent, j = _tlv(pdu, 0)
            _, ag, j = _tlv(pdu, j)
            _, gen, j = _tlv(pdu, j)
            _, spec, j = _tlv(pdu, j)
            _, tm, j = _tlv(pdu, j)
            f.update({"Enterprise": _oid(ent), "Agent address": ".".join(str(x) for x in ag), "Generic trap": _int(gen), "Specific trap": _int(spec)})
            extra = f"enterprise {_oid(ent)} generic {_int(gen)}"
        else:
            _, rid, j = _tlv(pdu, 0)
            _, es, j = _tlv(pdu, j)
            _, ei, j = _tlv(pdu, j)
            if t == 0xA5:
                f.update({"Request id": _int(rid), "Non-repeaters": _int(es), "Max repetitions": _int(ei)})
            else:
                f.update({"Request id": _int(rid), "Error status": f"{_int(es)} ({SNMP_ERR.get(_int(es), '?')})", "Error index": _int(ei)})
            extra = f"id {_int(rid)}" + (f" · {SNMP_ERR.get(_int(es), _int(es))}" if _int(es) and t != 0xA5 else "")
        _, vl, _ = _tlv(pdu, j)
        k = 0
        while k < len(vl):
            _, vb, k = _tlv(vl, k)
            _, o, m = _tlv(vb, 0)
            vt, vv, _ = _tlv(vb, m)
            vbs.append((_oid(o), _snmp_value(vt, vv)))
        f["Variable bindings"] = [f"{_oid_name(o)} = {val}" if val != "NULL" else _oid_name(o) for o, val in vbs]
        first = f["Variable bindings"][0] if vbs else "no bindings"
        more = f" +{len(vbs) - 1} more" if len(vbs) > 1 else ""
        return {"proto": "SNMP", "crc_ok": True, "kind": kind, "summary": f"{kind} {extra} · {first}{more}", "fields": f}
    except (ValueError, IndexError, struct.error):
        return None


# ------------------------------------------------------------------ IEC 60870-5-104
IEC_TYPES = {1: "M_SP_NA_1 single-point", 3: "M_DP_NA_1 double-point", 5: "M_ST_NA_1 step position", 7: "M_BO_NA_1 bitstring", 9: "M_ME_NA_1 measured (normalised)", 11: "M_ME_NB_1 measured (scaled)",
             13: "M_ME_NC_1 measured (float)", 15: "M_IT_NA_1 integrated totals", 30: "M_SP_TB_1 single-point + time", 31: "M_DP_TB_1 double-point + time", 34: "M_ME_TD_1 measured (normalised) + time",
             35: "M_ME_TE_1 measured (scaled) + time", 36: "M_ME_TF_1 measured (float) + time", 45: "C_SC_NA_1 single command", 46: "C_DC_NA_1 double command", 47: "C_RC_NA_1 regulating step",
             48: "C_SE_NA_1 set-point (normalised)", 49: "C_SE_NB_1 set-point (scaled)", 50: "C_SE_NC_1 set-point (float)", 58: "C_SC_TA_1 single command + time", 59: "C_DC_TA_1 double command + time",
             70: "M_EI_NA_1 end of initialisation", 100: "C_IC_NA_1 interrogation", 101: "C_CI_NA_1 counter interrogation", 102: "C_RD_NA_1 read", 103: "C_CS_NA_1 clock synchronisation",
             104: "C_TS_NA_1 test", 105: "C_RP_NA_1 reset process", 107: "C_TS_TA_1 test + time"}
IEC_COT = {1: "periodic", 2: "background", 3: "spontaneous", 4: "initialised", 5: "request", 6: "activation", 7: "activation confirm", 8: "deactivation", 9: "deactivation confirm",
           10: "activation termination", 20: "interrogated by station", 37: "counter interrogation", 44: "unknown type", 45: "unknown cause", 46: "unknown address", 47: "unknown object"}
IEC_U = {0x07: "STARTDT act", 0x0B: "STARTDT con", 0x13: "STOPDT act", 0x23: "STOPDT con", 0x43: "TESTFR act", 0x83: "TESTFR con"}
IEC_SIZE = {1: 1, 3: 1, 5: 2, 7: 5, 9: 3, 11: 3, 13: 5, 15: 5, 30: 8, 31: 8, 34: 10, 35: 10, 36: 12, 45: 1, 46: 1, 47: 1, 48: 3, 49: 3, 50: 5, 58: 8, 59: 8, 70: 1, 100: 1, 101: 1, 102: 0, 103: 7, 104: 2, 105: 1, 107: 9}


def _iec_value(t: int, e: bytes) -> str:
    try:
        if t in (1, 30):
            return f"{'ON' if e[0] & 1 else 'OFF'}" + ("" if not e[0] & 0xF0 else " (invalid/not topical)")
        if t in (3, 31):
            return {0: "intermediate", 1: "OFF", 2: "ON", 3: "faulty"}[e[0] & 3]
        if t in (9, 34):
            return f"{struct.unpack('<h', e[:2])[0] / 32768:.4f} (normalised)"
        if t in (11, 35):
            return f"{struct.unpack('<h', e[:2])[0]}"
        if t in (13, 36):
            return f"{struct.unpack('<f', e[:4])[0]:.4g}"
        if t in (45, 58):
            return f"{'ON' if e[0] & 1 else 'OFF'} · {'select' if e[0] & 0x80 else 'execute'}"
        if t in (46, 59):
            return f"{ {1: 'OFF', 2: 'ON'}.get(e[0] & 3, '?') } · {'select' if e[0] & 0x80 else 'execute'}"
        if t in (48, 49):
            return f"{struct.unpack('<h', e[:2])[0]}"
        if t == 50:
            return f"{struct.unpack('<f', e[:4])[0]:.4g}"
        if t == 100:
            return {20: "station interrogation"}.get(e[0], f"group {e[0] - 20}" if 21 <= e[0] <= 36 else f"qualifier {e[0]}")
        if t == 15:
            return f"{struct.unpack('<i', e[:4])[0]}"
    except (struct.error, IndexError, KeyError):
        pass
    return e.hex(" ")


def _iec_asdu(a: bytes) -> Tuple[str, dict]:
    t, vsq, cot, org, ca = a[0], a[1], a[2] & 0x3F, a[3], struct.unpack("<H", a[4:6])[0]
    sq, n = bool(vsq & 0x80), vsq & 0x7F
    f = {"Type": f"{t} ({IEC_TYPES.get(t, 'unknown type')})", "Cause of transmission": f"{cot} ({IEC_COT.get(cot, 'other')})" + (" · negative" if a[2] & 0x40 else "") + (" · test" if a[2] & 0x80 else ""),
         "Originator": org, "Common address": ca, "Objects": f"{n}" + (" (sequence)" if sq else "")}
    size, body, objs = IEC_SIZE.get(t), a[6:], []
    if size is not None and n:
        i = 0
        ioa = None
        for k in range(min(n, 40)):
            if sq and k:
                ioa += 1
            else:
                if i + 3 > len(body):
                    break
                ioa, i = int.from_bytes(body[i:i + 3], "little"), i + 3
            if i + size > len(body):
                break
            objs.append(f"IOA {ioa}: {_iec_value(t, body[i:i + size])}")
            i += size
    if objs:
        f["Values"] = objs + ([f"… {n - len(objs)} more"] if n > len(objs) else [])
    nm = IEC_TYPES.get(t, f"type {t}").split(" ", 1)
    first = objs[0] if objs else ""
    return f"{nm[1] if len(nm) > 1 else nm[0]} · {IEC_COT.get(cot, cot)} · CA {ca} · {first}" + (f" +{n - 1}" if n > 1 else ""), f


def decode_iec104(p: bytes) -> Optional[dict]:
    if len(p) < 6 or p[0] != 0x68:
        return None
    out, i, fields, sums, ok = [], 0, {}, [], True
    while i + 6 <= len(p) and p[i] == 0x68 and len(out) < 8:
        ln = p[i + 1]
        if ln < 4 or i + 2 + ln > len(p):
            ok = False
            break
        c = p[i + 2:i + 6]
        k = len(out) + 1
        pre = "" if k == 1 and i + 2 + ln >= len(p) else f"APDU {k}: "
        if c[0] & 3 == 3:
            nm = IEC_U.get(c[0] & 0xFF, f"U-frame 0x{c[0]:02X}")
            fields[pre + "Frame"] = f"U-format · {nm}"
            sums.append(nm)
        elif c[0] & 3 == 1:
            nr = (c[2] | c[3] << 8) >> 1
            fields[pre + "Frame"] = f"S-format · receive seq {nr}"
            sums.append(f"S · ack {nr}")
        else:
            ns, nr = (c[0] | c[1] << 8) >> 1, (c[2] | c[3] << 8) >> 1
            fields[pre + "Frame"] = f"I-format · send seq {ns} · receive seq {nr}"
            if ln >= 10:
                s, af = _iec_asdu(p[i + 6:i + 2 + ln])
                sums.append(f"I {ns}/{nr} · {s}")
                for k2, v2 in af.items():
                    fields[pre + k2] = v2
            else:
                sums.append(f"I {ns}/{nr}")
        out.append(1)
        i += 2 + ln
    if not out:
        return None
    if i < len(p) and p[i] == 0x68:
        ok = False
    return {"proto": "IEC 104", "crc_ok": ok, "kind": sums[0].split(" · ")[0].split(" ")[0], "summary": " | ".join(sums) if len(sums) > 1 else sums[0], "fields": fields}


# ------------------------------------------------------------------ PTP
PTP_TYPES = {0: "Sync", 1: "Delay_Req", 2: "Pdelay_Req", 3: "Pdelay_Resp", 8: "Follow_Up", 9: "Delay_Resp", 10: "Pdelay_Resp_Follow_Up", 11: "Announce", 12: "Signaling", 13: "Management"}
PTP_SRC = {0x10: "atomic clock", 0x20: "GPS", 0x30: "terrestrial radio", 0x40: "PTP", 0x50: "NTP", 0x60: "hand set", 0x90: "other", 0xA0: "internal oscillator"}


def _ptp_ts(b: bytes) -> Tuple[int, int]:
    return int.from_bytes(b[:6], "big"), struct.unpack(">I", b[6:10])[0]


def decode_ptp(p: bytes) -> Optional[dict]:
    if len(p) < 34:
        return None
    mt, ver = p[0] & 0x0F, p[1] & 0x0F
    if mt not in PTP_TYPES or ver not in (1, 2):
        return None
    dom, flags = p[4], struct.unpack(">H", p[6:8])[0]
    corr = struct.unpack(">q", p[8:16])[0] / 65536.0
    clk, port, seq = p[20:28].hex(), struct.unpack(">H", p[28:30])[0], struct.unpack(">H", p[30:32])[0]
    f = {"Message": f"{mt} ({PTP_TYPES[mt]})", "Version": f"PTPv{ver}", "Domain": dom, "Flags": f"0x{flags:04X}" + (" · two-step" if flags & 0x200 else "") + (" · unicast" if flags & 0x400 else ""),
         "Correction": f"{corr:.1f} ns", "Clock identity": clk, "Port": port, "Sequence": seq, "Log message interval": struct.unpack(">b", p[33:34])[0]}
    extra, b = "", p[34:]
    try:
        if mt in (0, 1, 8, 2) and len(b) >= 10:
            s, ns = _ptp_ts(b)
            f["Origin timestamp"] = f"{s}.{ns:09d} s"
            extra = f"t = {s}.{ns:09d}" if mt != 2 else ""
        elif mt in (9, 3, 10) and len(b) >= 20:
            s, ns = _ptp_ts(b)
            f["Receive timestamp" if mt != 10 else "Request receipt timestamp"] = f"{s}.{ns:09d} s"
            f["Requesting port"] = f"{b[10:18].hex()}:{struct.unpack('>H', b[18:20])[0]}"
        elif mt == 11 and len(b) >= 30:
            f.update({"Current UTC offset": f"{struct.unpack('>h', b[10:12])[0]} s", "Grandmaster priority 1": b[13], "Grandmaster class": b[14], "Grandmaster accuracy": f"0x{b[15]:02X}",
                      "Grandmaster priority 2": b[18], "Grandmaster identity": b[19:27].hex(), "Steps removed": struct.unpack(">H", b[27:29])[0], "Time source": PTP_SRC.get(b[29], f"0x{b[29]:02X}")})
            extra = f"GM {b[19:27].hex()} class {b[14]} prio {b[13]}/{b[18]} steps {struct.unpack('>H', b[27:29])[0]}"
    except (struct.error, IndexError):
        pass
    return {"proto": "PTP", "crc_ok": True, "kind": PTP_TYPES[mt], "summary": f"{PTP_TYPES[mt]} · seq {seq} · domain {dom}" + (f" · {extra}" if extra else ""), "fields": f}


# ------------------------------------------------------------------ GOOSE and R-GOOSE
def _goose_value(tag: int, v: bytes) -> str:
    try:
        if tag == 0x83:
            return "true" if v != b"\x00" else "false"
        if tag == 0x85:
            return str(_int(v))
        if tag == 0x86:
            return str(_int(v, False))
        if tag == 0x87 and len(v) in (5, 9):                       # floating-point: format byte, then IEEE 754
            return f"{struct.unpack('>f' if len(v) == 5 else '>d', v[1:])[0]:.6g}"
        if tag == 0x84:
            return f"bitstring {v[1:].hex()} ({(len(v) - 1) * 8 - v[0]} bits)" if v else "bitstring"
        if tag in (0x8A, 0x91):
            return '"' + v.decode("ascii", "replace") + '"'
        if tag == 0x89:
            return "0x" + v.hex()
        if tag == 0x90 and len(v) >= 4:
            return f"UTC {_int(v[:4], False)} s"
        if tag in (0xA1, 0xA2):
            items, i = [], 0
            while i < len(v):
                t2, v2, i = _tlv(v, i)
                items.append(_goose_value(t2, v2))
            return "{" + ", ".join(items) + "}"
    except (ValueError, struct.error):
        pass
    return "0x" + v.hex()


def decode_goose_pdu(b: bytes) -> Optional[dict]:
    """The goosePdu itself (tag 0x61), used for L2 GOOSE and R-GOOSE."""
    try:
        tag, body, _ = _tlv(b, 0)
        if tag != 0x61:
            return None
        f: dict = {}
        data: List[str] = []
        i = 0
        st = sq = None
        gocb = ""
        while i < len(body):
            t, v, i = _tlv(body, i)
            if t == 0x80:
                gocb = v.decode("ascii", "replace")
                f["gocbRef"] = gocb
            elif t == 0x81:
                f["Time allowed to live"] = f"{_int(v, False)} ms"
            elif t == 0x82:
                f["Data set"] = v.decode("ascii", "replace")
            elif t == 0x83:
                f["goID"] = v.decode("ascii", "replace")
            elif t == 0x84 and len(v) >= 8:
                s = _int(v[:4], False)
                frac = int.from_bytes(v[4:7], "big") * 1_000_000_000 >> 24
                f["Event time (UTC)"] = f"{s} s + {frac / 1e6:.3f} ms" + (f" · quality 0x{v[7]:02X}" if len(v) > 7 else "")
            elif t == 0x85:
                st = _int(v, False)
                f["stNum"] = st
            elif t == 0x86:
                sq = _int(v, False)
                f["sqNum"] = sq
            elif t == 0x87:
                f["Simulation"] = "true" if v != b"\x00" else "false"
            elif t == 0x88:
                f["confRev"] = _int(v, False)
            elif t == 0x89:
                f["Needs commissioning"] = "true" if v != b"\x00" else "false"
            elif t == 0x8A:
                f["Number of data set entries"] = _int(v, False)
            elif t == 0xAB:
                j = 0
                while j < len(v) and len(data) < 60:
                    t2, v2, j = _tlv(v, j)
                    data.append(_goose_value(t2, v2))
        if st is None or sq is None:
            return None
        if data:
            f["Data"] = [f"[{k}] {x}" for k, x in enumerate(data)]
        return {"stNum": st, "sqNum": sq, "gocb": gocb, "fields": f, "data": data}
    except (ValueError, IndexError, struct.error):
        return None


def decode_goose(b: bytes, appid: int, r_goose: bool = False, extra: Optional[dict] = None) -> Optional[dict]:
    g = decode_goose_pdu(b)
    if not g:
        return None
    f = {"APPID": f"0x{appid:04X}", **(extra or {}), **g["fields"]}
    flag = " · state change" if g["sqNum"] == 0 else ""
    vals = ", ".join(g["data"][:4]) + (" …" if len(g["data"]) > 4 else "")
    return {"proto": "R-GOOSE" if r_goose else "GOOSE", "crc_ok": True, "kind": "event" if g["sqNum"] == 0 else "repeat",
            "summary": f"{g['gocb'] or 'GOOSE'} · stNum {g['stNum']} sqNum {g['sqNum']}{flag}" + (f" · {vals}" if vals else ""), "fields": f}


def decode_rgoose(p: bytes) -> Optional[dict]:
    """R-GOOSE on UDP 102 (IEC 61850-90-5): optional CLTP header, session header, one GOOSE APDU. Falls back to locating the goosePdu tag when the session header is unusual."""
    try:
        i = 2 if p[:2] == b"\x01\x40" else 0
        if i >= len(p) or p[i] not in (0xA1, 0xA2, 0xA0):
            return None
        spdu = p[i]
        extra = {"Session": {0xA1: "GOOSE", 0xA2: "Sampled values", 0xA0: "Tunnelled"}[spdu]}
        hdr = p[i + 1]
        if p[i + 2] == 0x80 and len(p) >= i + 12:
            spdu_len, spdu_no = struct.unpack(">II", p[i + 4:i + 12])
            extra.update({"SPDU length": spdu_len, "SPDU number": spdu_no})
        q = i + 2 + hdr
        plen = struct.unpack(">I", p[q:q + 4])[0] if len(p) >= q + 4 else 0
        if len(p) > q + 10 and p[q + 4] == 0x81 and plen >= 6:
            appid = struct.unpack(">H", p[q + 6:q + 8])[0]
            if p[q + 5] & 1:
                extra["Simulation"] = "true"
            return decode_goose(p[q + 10:q + 4 + plen], appid, True, extra)
        k = p.find(b"\x61", i)                                      # unusual layout: find the goosePdu
        while k != -1:
            d = decode_goose_pdu(p[k:])
            if d:
                return decode_goose(p[k:], 0, True, extra)
            k = p.find(b"\x61", k + 1)
    except (ValueError, IndexError, struct.error):
        return None
    return None
