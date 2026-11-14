"""Modbus RTU: CRC-16, framing (gap + CRC validation + resync), decoding."""
from __future__ import annotations

import struct
from typing import List, Optional, Tuple

FUNCS = {1: "Read Coils", 2: "Read Discrete Inputs", 3: "Read Holding Registers", 4: "Read Input Registers", 5: "Write Single Coil",
         6: "Write Single Register", 7: "Read Exception Status", 8: "Diagnostics", 11: "Get Comm Event Counter", 12: "Get Comm Event Log",
         15: "Write Multiple Coils", 16: "Write Multiple Registers", 17: "Report Server ID", 20: "Read File Record", 21: "Write File Record",
         22: "Mask Write Register", 23: "Read/Write Multiple Registers", 24: "Read FIFO Queue", 43: "Encapsulated Interface (MEI)"}
EXC = {1: "Illegal function", 2: "Illegal data address", 3: "Illegal data value", 4: "Server device failure", 5: "Acknowledge", 6: "Server busy",
       8: "Memory parity error", 10: "Gateway path unavailable", 11: "Gateway target failed to respond"}


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def add_crc(frame: bytes) -> bytes:
    return frame + struct.pack("<H", crc16(frame))


def crc_ok(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16(frame[:-2]) == struct.unpack("<H", frame[-2:])[0]


def char_time_s(baud: int, bits_per_char: int = 11) -> float:
    return bits_per_char / float(baud)


def default_gap_ms(baud: int) -> float:
    """t3.5 (1.75 ms fixed above 19200 baud), never below what a USB-serial adapter can resolve (4 ms)."""
    t35 = 1.75e-3 if baud > 19200 else 3.5 * char_time_s(baud)
    return max(t35 * 1000.0, 4.0)


def possible_lengths(buf: bytes) -> List[int]:
    """Frame lengths (with CRC) a valid frame starting here could have, judging by its function code. [] = unknown."""
    if len(buf) < 3:
        return []
    f = buf[1]
    if f & 0x80:
        return [5]                                              # exception response
    out = []
    if f in (1, 2, 3, 4, 5, 6):
        out.append(8)                                           # request / write response
        if f in (1, 2, 3, 4):
            out.append(5 + buf[2])                              # read response: byte count
    elif f in (15, 16):
        out.append(8)                                           # response
        if len(buf) >= 7:
            out.append(9 + buf[6])                              # request with byte count
    elif f in (7, 11):
        out += [4, 5, 6]
    elif f == 22:
        out.append(10)
    elif f == 23:
        out.append(5 + buf[2]) if len(buf) > 2 else None
        if len(buf) >= 11:
            out.append(13 + buf[10])
    elif f == 17 and len(buf) > 2:
        out.append(5 + buf[2])
    return [x for x in out if x >= 4]


def split_valid_prefix(buf: bytes) -> Optional[int]:
    """Length of a plausible complete frame at the start of buf (CRC ok, address 1-247/0, plausible length), else None."""
    if len(buf) < 4 or buf[0] > 247:
        return None
    for ln in possible_lengths(buf):
        if ln <= len(buf) and crc_ok(buf[:ln]):
            return ln
    return None


class ModbusFramer:
    """Feed byte chunks with host timestamps; get (frame, t_first_ns, t_last_ns, crc_ok) tuples."""

    def __init__(self, gap_ms: float = 4.0):
        self.gap_ns = int(gap_ms * 1e6)
        self.buf = bytearray()
        self.t_first = self.t_last = 0
        self.resyncs = 0

    def feed(self, data: bytes, t_ns: int) -> List[Tuple[bytes, int, int, bool]]:
        out: List[Tuple[bytes, int, int, bool]] = []
        if self.buf and t_ns - self.t_last > self.gap_ns:
            out += self._flush()
        if not self.buf:
            self.t_first = t_ns
        self.buf += data
        self.t_last = t_ns
        while True:                                          # complete valid frames are emitted without waiting for the gap
            n = split_valid_prefix(bytes(self.buf))
            if n is None:
                break
            out.append((bytes(self.buf[:n]), self.t_first, self.t_last, True))
            del self.buf[:n]
            self.t_first = t_ns
        return out

    def tick(self, t_ns: int) -> List[Tuple[bytes, int, int, bool]]:
        if self.buf and t_ns - self.t_last > self.gap_ns:
            return self._flush()
        return []

    def flush(self) -> List[Tuple[bytes, int, int, bool]]:
        return self._flush()

    def _flush(self) -> List[Tuple[bytes, int, int, bool]]:
        if not self.buf:
            return []
        b = bytes(self.buf)
        self.buf.clear()
        ok = crc_ok(b)
        if not ok:
            self.resyncs += 1
        return [(b, self.t_first, self.t_last, ok)]


def decode_rtu(frame: bytes, is_request: Optional[bool] = None) -> dict:
    """Decode one RTU frame. is_request: True/False when the direction is known, None = guess from the layout."""
    d = {"proto": "Modbus RTU", "len": len(frame), "crc_ok": crc_ok(frame), "error": None}
    if len(frame) < 4:
        d.update(summary="Short frame", error="too short", fields={})
        return d
    addr, fc = frame[0], frame[1]
    d["addr"], d["func"] = addr, fc & 0x7F
    pdu = frame[2:-2]
    fields = {"Unit address": addr, "Function": f"{fc & 0x7F} ({FUNCS.get(fc & 0x7F, 'unknown')})", "CRC": "ok" if d["crc_ok"] else "FAILED"}
    if fc & 0x80:
        code = pdu[0] if pdu else None
        fields["Exception"] = f"{code} ({EXC.get(code, 'unknown')})"
        d.update(kind="exception", summary=f"Unit {addr}: {FUNCS.get(fc & 0x7F, 'function ' + str(fc & 0x7F))} - EXCEPTION {EXC.get(code, code)}", fields=fields)
        return d
    name = FUNCS.get(fc, f"function {fc}")
    kind = "?"
    summ = f"Unit {addr}: {name}"
    try:
        if fc in (1, 2, 3, 4):
            req = (len(pdu) == 4) if is_request is None else is_request
            if req and len(pdu) == 4:
                a, q = struct.unpack(">HH", pdu)
                kind, summ = "request", f"Unit {addr}: {name} - start {a}, quantity {q}"
                fields.update({"Start address": a, "Quantity": q})
            else:
                bc = pdu[0]
                data = pdu[1:1 + bc]
                kind = "response"
                if fc in (3, 4):
                    regs = list(struct.unpack(f">{len(data) // 2}H", data[:len(data) // 2 * 2]))
                    fields.update({"Byte count": bc, "Registers": regs[:32]})
                    summ = f"Unit {addr}: {name} response - {len(regs)} register(s): {regs[:8]}{' ...' if len(regs) > 8 else ''}"
                else:
                    bits = "".join(f"{b:08b}"[::-1] for b in data)
                    fields.update({"Byte count": bc, "Bits (first 32)": bits[:32]})
                    summ = f"Unit {addr}: {name} response - {bc} byte(s)"
        elif fc in (5, 6):
            a, v = struct.unpack(">HH", pdu[:4])
            kind = "request/response (echo)"
            fields.update({"Address": a, "Value": f"0x{v:04X}" if fc == 5 else v})
            summ = f"Unit {addr}: {name} - address {a} = " + (("ON" if v == 0xFF00 else "OFF" if v == 0 else f"0x{v:04X}") if fc == 5 else str(v))
        elif fc in (15, 16):
            a, q = struct.unpack(">HH", pdu[:4])
            if len(pdu) > 5 and pdu[4] == len(pdu) - 5:
                kind = "request"
                fields.update({"Start address": a, "Quantity": q, "Byte count": pdu[4]})
                summ = f"Unit {addr}: {name} - start {a}, quantity {q}"
            else:
                kind = "response"
                fields.update({"Start address": a, "Quantity": q})
                summ = f"Unit {addr}: {name} response - start {a}, quantity {q}"
        elif fc == 22:
            a, am, om = struct.unpack(">HHH", pdu[:6])
            kind, summ = "request/response", f"Unit {addr}: {name} - address {a}"
            fields.update({"Address": a, "AND mask": f"0x{am:04X}", "OR mask": f"0x{om:04X}"})
        else:
            kind = "request" if is_request else "response" if is_request is False else "?"
            fields["Data"] = pdu.hex()
    except (struct.error, IndexError):
        d["error"] = "malformed"
        summ += " (malformed)"
    if is_request is True:
        kind = "request"
    elif is_request is False and kind == "request":
        kind = "response"
    d.update(kind=kind, summary=summ + ("" if d["crc_ok"] else "  [CRC FAILED]"), fields=fields)
    return d


def decode_mbap(payload: bytes) -> Optional[dict]:
    """Modbus/TCP: MBAP header + PDU (no CRC). Returns None if it does not look like Modbus/TCP."""
    if len(payload) < 8:
        return None
    tid, pid, ln, unit = struct.unpack(">HHHB", payload[:7])
    if pid != 0 or ln < 2 or ln != len(payload) - 6:
        return None
    d = decode_rtu(add_crc(bytes([unit]) + payload[7:]), None)
    d["proto"] = "Modbus/TCP"
    d["fields"] = {"Transaction id": tid, **{k: v for k, v in d["fields"].items() if k != "CRC"}}
    d["crc_ok"] = True
    d["summary"] = d["summary"].replace("  [CRC FAILED]", "")
    return d
