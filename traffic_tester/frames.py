"""Trip-message wire format (docs/SPEC.md section 3).

Compact frame  (7 bytes):  A5 | type<<4|flags | seq(2) | bits | crc16
Extended frame (40 bytes): A5 5B | ver | type | flags | bits | seq(4) | t1 t2 t3 (8 each) | offset_us(4) | crc16
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional

SYNC = 0xA5
MAGIC = b"\xA5\x5B"
T_TRIP, T_ECHO, T_HELLO, T_HELLO_ACK = 1, 2, 3, 4
F_IDLE = 0x01           # idle-probe
F_SYNCED = 0x02         # sender clock synced (extended only)
COMPACT_LEN = 7
EXT_LEN = 40
VERSION = 1


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass
class Frame:
    type: int
    seq: int
    bits: int = 0
    flags: int = 0
    t1: int = 0
    t2: int = 0
    t3: int = 0
    offset_us: int = 0
    extended: bool = False


def encode_compact(f: Frame) -> bytes:
    body = struct.pack(">BHB", ((f.type & 0xF) << 4) | (f.flags & 0xF), f.seq & 0xFFFF, f.bits & 0xFF)
    return bytes([SYNC]) + body + struct.pack(">H", crc16(body))


def encode_extended(f: Frame) -> bytes:
    body = (struct.pack(">BBBB", VERSION, f.type & 0xFF, f.flags & 0xFF, f.bits & 0xFF)
            + struct.pack(">I", f.seq & 0xFFFFFFFF)
            + struct.pack(">QQQ", f.t1, f.t2, f.t3)
            + struct.pack(">i", max(-2**31, min(2**31 - 1, f.offset_us))))
    return MAGIC + body + struct.pack(">H", crc16(body))


def encode(f: Frame) -> bytes:
    return encode_extended(f) if f.extended else encode_compact(f)


def _decode_compact(b: bytes) -> Optional[Frame]:
    if len(b) != COMPACT_LEN or b[0] != SYNC:
        return None
    body = b[1:5]
    if crc16(body) != struct.unpack(">H", b[5:7])[0]:
        return None
    tf, seq, bits = struct.unpack(">BHB", body)
    return Frame(type=tf >> 4, flags=tf & 0xF, seq=seq, bits=bits)


def _decode_extended(b: bytes) -> Optional[Frame]:
    if len(b) != EXT_LEN or b[:2] != MAGIC:
        return None
    body = b[2:38]
    if crc16(body) != struct.unpack(">H", b[38:40])[0]:
        return None
    ver, typ, flags, bits = struct.unpack(">BBBB", body[:4])
    if ver != VERSION:
        return None
    seq = struct.unpack(">I", body[4:8])[0]
    t1, t2, t3 = struct.unpack(">QQQ", body[8:32])
    off = struct.unpack(">i", body[32:36])[0]
    return Frame(type=typ, flags=flags, bits=bits, seq=seq, t1=t1, t2=t2, t3=t3,
                 offset_us=off, extended=True)


def decode_datagram(b: bytes) -> Optional[Frame]:
    """Decode exactly one frame from a datagram (UDP). Returns None if invalid."""
    if len(b) >= EXT_LEN and b[:2] == MAGIC:
        return _decode_extended(b[:EXT_LEN])
    if len(b) >= COMPACT_LEN and b[0] == SYNC:
        return _decode_compact(b[:COMPACT_LEN])
    return None


class StreamParser:
    """Resyncing parser for byte streams (serial / TCP). Counts CRC failures."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.corrupted = 0

    def feed(self, data: bytes) -> List[Frame]:
        self.buf += data
        out: List[Frame] = []
        buf = self.buf
        while buf:
            if buf[0] != SYNC:
                del buf[0]
                continue
            if len(buf) >= 2 and buf[1] == MAGIC[1]:
                # candidate extended frame (type nibble 5 / flag 0xB is never used by compact)
                if len(buf) < EXT_LEN:
                    break
                f = _decode_extended(bytes(buf[:EXT_LEN]))
                if f is not None:
                    out.append(f)
                    del buf[:EXT_LEN]
                    continue
                # maybe a compact frame whose type byte happens to be 0x5B (type 5, unused)
            if len(buf) < COMPACT_LEN:
                break
            f = _decode_compact(bytes(buf[:COMPACT_LEN]))
            if f is not None:
                out.append(f)
                del buf[:COMPACT_LEN]
            else:
                self.corrupted += 1
                del buf[0]
        return out


def unwrap_seq(seq: int, expected: int, bits: int) -> int:
    """Map a wrapped sequence number to the 64-bit value nearest `expected`."""
    mod = 1 << bits
    base = expected - (expected % mod)
    cand = base + (seq % mod)
    if cand - expected > mod // 2:
        cand -= mod
    elif expected - cand > mod // 2:
        cand += mod
    return cand
