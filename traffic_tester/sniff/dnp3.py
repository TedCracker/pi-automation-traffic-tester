"""DNP3 (IEEE 1815) link/transport/application decoding and stream framing with CRC-16/DNP checks."""
from __future__ import annotations

import struct
from typing import List, Optional, Tuple

START = b"\x05\x64"

APP_FUNCS = {0: "Confirm", 1: "Read", 2: "Write", 3: "Select", 4: "Operate", 5: "Direct Operate", 6: "Direct Operate (no ack)", 7: "Immediate Freeze",
             8: "Immediate Freeze (no ack)", 9: "Freeze and Clear", 10: "Freeze and Clear (no ack)", 11: "Freeze at Time", 12: "Freeze at Time (no ack)",
             13: "Cold Restart", 14: "Warm Restart", 15: "Initialize Data", 16: "Initialize Application", 17: "Start Application", 18: "Stop Application",
             19: "Save Configuration", 20: "Enable Unsolicited", 21: "Disable Unsolicited", 22: "Assign Class", 23: "Delay Measurement",
             24: "Record Current Time", 25: "Open File", 26: "Close File", 27: "Delete File", 28: "Get File Info", 29: "Authenticate File",
             30: "Abort File", 129: "Response", 130: "Unsolicited Response", 131: "Authentication Response"}
PRIMARY = {0: "Reset Link States", 1: "Reset User Process", 2: "Test Link States", 3: "Confirmed User Data", 4: "Unconfirmed User Data", 9: "Request Link Status"}
SECONDARY = {0: "ACK", 1: "NACK", 11: "Link Status", 15: "Not Supported"}
IIN1 = ["Broadcast", "Class 1 events", "Class 2 events", "Class 3 events", "Need time", "Local control", "Device trouble", "Device restart"]
IIN2 = ["Function not supported", "Object unknown", "Parameter error", "Event buffer overflow", "Already executing", "Configuration corrupt", "Reserved", "Reserved"]
GROUPS = {1: "Binary Input", 2: "Binary Input Event", 3: "Double-bit Input", 4: "Double-bit Input Event", 10: "Binary Output", 11: "Binary Output Event",
          12: "Control Relay Output Block", 20: "Counter", 21: "Frozen Counter", 22: "Counter Event", 23: "Frozen Counter Event", 30: "Analog Input",
          32: "Analog Input Event", 40: "Analog Output Status", 41: "Analog Output Block", 42: "Analog Output Event", 50: "Time and Date", 51: "Time and Date CTO",
          52: "Time Delay", 60: "Class Data", 80: "Internal Indications", 110: "Octet String", 120: "Authentication"}
# bytes per object for common (group, variation); None = bit-packed (1 bit per point); missing = unknown size
SIZES = {(1, 1): None, (1, 2): 1, (2, 1): 1, (2, 2): 7, (2, 3): 3, (3, 2): 1, (4, 1): 1, (4, 2): 7, (10, 1): None, (10, 2): 1, (11, 1): 1, (11, 2): 7, (12, 1): 11,
         (20, 1): 5, (20, 2): 3, (20, 5): 4, (20, 6): 2, (21, 1): 5, (21, 2): 3, (21, 5): 11, (21, 6): 9, (22, 1): 5, (22, 2): 3, (22, 5): 11, (22, 6): 9,
         (30, 1): 5, (30, 2): 3, (30, 3): 4, (30, 4): 2, (30, 5): 5, (30, 6): 9, (32, 1): 5, (32, 2): 3, (32, 3): 11, (32, 4): 9, (32, 5): 5, (32, 6): 9,
         (32, 7): 11, (32, 8): 15, (40, 1): 5, (40, 2): 3, (40, 3): 5, (40, 4): 9, (41, 1): 5, (41, 2): 3, (41, 3): 5, (41, 4): 9, (50, 1): 6, (50, 3): 6,
         (51, 1): 6, (51, 2): 6, (52, 1): 2, (52, 2): 2, (80, 1): None}


def crc16(data: bytes) -> int:
    """CRC-16/DNP (poly 0x3D65 reflected = 0xA6BC, init 0, result inverted)."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA6BC if crc & 1 else crc >> 1
    return (~crc) & 0xFFFF


def frame_len(length_field: int) -> int:
    """Total on-wire bytes of a link frame given its LENGTH octet (user data = length - 5)."""
    user = max(0, length_field - 5)
    return 10 + user + 2 * (-(-user // 16))


def build_frame(ctrl: int, dest: int, src: int, user: bytes = b"") -> bytes:
    """Build a complete link frame (header + CRCs + data blocks) - used by tests and the sample capture."""
    hdr = struct.pack("<BBBBHH", 0x05, 0x64, 5 + len(user), ctrl, dest, src)
    out = hdr + struct.pack("<H", crc16(hdr))                    # the header CRC covers all 8 header bytes INCLUDING the 05 64 start bytes
    for i in range(0, len(user), 16):
        blk = user[i:i + 16]
        out += blk + struct.pack("<H", crc16(blk))
    return out


class Dnp3Framer:
    """Stream framer: finds 05 64, validates the header CRC, collects the frame, validates every data-block CRC."""

    def __init__(self, stale_ns: int = 2_000_000_000):
        self.buf = bytearray()
        self.t_first = self.t_last = 0
        self.skipped = 0
        self.stale_ns = stale_ns

    def feed(self, data: bytes, t_ns: int) -> List[Tuple[bytes, int, int, bool]]:
        out = []
        if not self.buf:
            self.t_first = t_ns
        self.buf += data
        self.t_last = t_ns
        b = self.buf
        while True:
            i = b.find(START)
            if i < 0:
                self.skipped += max(0, len(b) - 1)
                del b[:max(0, len(b) - 1)]
                self.t_first = t_ns                                              # what is left (at most one byte) arrived now, not with the dropped bytes
                break
            if i:
                self.skipped += i
                del b[:i]
                self.t_first = t_ns
            if len(b) < 10:
                break
            if crc16(bytes(b[0:8])) != struct.unpack("<H", b[8:10])[0]:         # not a real header: skip this start and look for the next
                self.skipped += 1
                del b[:1]
                self.t_first = t_ns
                continue
            n = frame_len(b[2])
            if len(b) < n:
                break
            fr = bytes(b[:n])
            out.append((fr, self.t_first, self.t_last, blocks_ok(fr)))
            del b[:n]
            self.t_first = t_ns
        return out

    def tick(self, t_ns: int) -> List[Tuple[bytes, int, int, bool]]:
        if self.buf and t_ns - self.t_last > self.stale_ns:       # a half frame that never completes
            b = bytes(self.buf)
            self.buf.clear()
            self.skipped += len(b)
            return [(b, self.t_first, self.t_last, False)]
        return []


def blocks_ok(frame: bytes) -> bool:
    if len(frame) < 10 or crc16(frame[0:8]) != struct.unpack("<H", frame[8:10])[0]:
        return False
    pos = 10
    while pos < len(frame):
        blk = frame[pos:pos + 18]
        if len(blk) < 3:
            return False
        data, c = blk[:-2], struct.unpack("<H", blk[-2:])[0]
        if crc16(data) != c:
            return False
        pos += len(blk)
    return True


def user_data(frame: bytes) -> bytes:
    out, pos = b"", 10
    while pos < len(frame):
        blk = frame[pos:pos + 18]
        out += blk[:-2]
        pos += 18
    return out


def _objects(app: bytes, i: int, out: List[str], with_data: bool = True) -> None:
    """List object headers (group/variation/qualifier/range); stop politely at the first object whose size is unknown.
    with_data=False: a read request, which carries only the headers (and index prefixes), no object data."""
    n = 0
    while i + 3 <= len(app) and n < 40:
        g, v, q = app[i], app[i + 1], app[i + 2]
        i += 3
        n += 1
        prefix, rng = (q >> 4) & 7, q & 0x0F
        count = None
        desc = ""
        try:
            if rng == 0:
                a, b = app[i], app[i + 1]; i += 2; count = b - a + 1; desc = f"index {a}-{b}"
            elif rng == 1:
                a, b = struct.unpack("<HH", app[i:i + 4]); i += 4; count = b - a + 1; desc = f"index {a}-{b}"
            elif rng == 2:
                a, b = struct.unpack("<II", app[i:i + 8]); i += 8; count = b - a + 1; desc = f"index {a}-{b}"
            elif rng == 6:
                desc = "all objects"
            elif rng == 7:
                count = app[i]; i += 1; desc = f"count {count}"
            elif rng == 8:
                count = struct.unpack("<H", app[i:i + 2])[0]; i += 2; desc = f"count {count}"
            elif rng == 9:
                count = struct.unpack("<I", app[i:i + 4])[0]; i += 4; desc = f"count {count}"
            elif rng == 0x0B:
                count = app[i]; i += 1; desc = f"variable count {count}"
            else:
                out.append(f"g{g}v{v} ({GROUPS.get(g, 'object')}) qualifier 0x{q:02X}: unsupported range, rest not decoded")
                return
        except (IndexError, struct.error):
            out.append(f"g{g}v{v} truncated")
            return
        name = GROUPS.get(g, f"group {g}")
        out.append(f"g{g}v{v} {name}, {desc or 'no range'}")
        if count is None:
            continue
        pre = {0: 0, 1: 1, 2: 2, 3: 4, 4: 1, 5: 2, 6: 4}.get(prefix, 0) if rng not in (0, 1, 2) else 0
        if not with_data:
            i += count * pre
            continue
        if (g, v) in SIZES:
            sz = SIZES[(g, v)]
            if sz is None:
                i += (count + 7) // 8
            else:
                i += count * (sz + pre)
        elif count and rng not in (6,):
            out.append("(object data not decoded: size of this object type is not in the table)")
            return


def decode_frame(frame: bytes) -> dict:
    """Decode one DNP3 link frame (and the application header when the frame carries the first transport segment)."""
    d = {"proto": "DNP3", "len": len(frame), "crc_ok": blocks_ok(frame), "error": None}
    if len(frame) < 10 or frame[:2] != START:
        d.update(summary="Not a DNP3 link frame", error="no start bytes", fields={})
        return d
    length, ctrl, dest, src = frame[2], frame[3], struct.unpack("<H", frame[4:6])[0], struct.unpack("<H", frame[6:8])[0]
    dirbit, prm = bool(ctrl & 0x80), bool(ctrl & 0x40)
    fc = ctrl & 0x0F
    fname = (PRIMARY if prm else SECONDARY).get(fc, f"function {fc}")
    fields = {"Direction": "from master" if dirbit else "from outstation", "Link function": f"{fc} ({fname})", "Destination": dest, "Source": src, "Header/data CRC": "ok" if d["crc_ok"] else "FAILED"}
    if prm:
        fields["FCB / FCV"] = f"{int(bool(ctrl & 0x20))} / {int(bool(ctrl & 0x10))}"
    summ = f"{src} → {dest}  {'master' if dirbit else 'outstation'}: {fname}"
    data = user_data(frame)
    d["src"], d["dest"], d["from_master"] = src, dest, dirbit
    if data:
        tp = data[0]
        fir, fin, seq = bool(tp & 0x40), bool(tp & 0x80), tp & 0x3F
        fields["Transport"] = f"FIR={int(fir)} FIN={int(fin)} SEQ={seq}"
        if fir and len(data) >= 3:
            ac, afc = data[1], data[2]
            fields["Application control"] = f"FIR={int(bool(ac & 0x80))} FIN={int(bool(ac & 0x40))} CON={int(bool(ac & 0x20))} UNS={int(bool(ac & 0x10))} SEQ={ac & 0x0F}"
            fields["Application function"] = f"{afc} ({APP_FUNCS.get(afc, 'unknown')})"
            summ += f" | {APP_FUNCS.get(afc, 'func ' + str(afc))}"
            pos = 3
            if afc in (129, 130, 131) and len(data) >= 5:
                i1, i2 = data[3], data[4]
                active = [n for k, n in enumerate(IIN1) if i1 >> k & 1] + [n for k, n in enumerate(IIN2) if i2 >> k & 1]
                fields["IIN"] = f"0x{i1:02X}{i2:02X}" + (f" ({', '.join(active)})" if active else " (none)")
                if active:
                    summ += f" [IIN: {', '.join(active)}]"
                pos = 5
            objs: List[str] = []
            if fin:
                _objects(data, pos, objs, afc != 1)
            else:
                objs.append("(fragment continues in the next frame; objects not decoded)")
            if objs:
                fields["Objects"] = objs
                summ += " | " + "; ".join(o.split(",")[0] for o in objs[:3])
        elif not fir:
            summ += f" | continuation segment (SEQ {seq})"
    d.update(summary=summ + ("" if d["crc_ok"] else "  [CRC FAILED]"), fields=fields)
    return d
