"""pcapng writer (with rotation) and reader. Timestamps are nanoseconds (if_tsresol = 9)."""
from __future__ import annotations

import datetime as dt
import os
import struct
import time
from typing import Callable, Dict, Iterator, List, Optional, Tuple

LINKTYPE_ETHERNET = 1
LINKTYPE_IEEE802_11_RADIOTAP = 127
LINKTYPE_USER0 = 147
SHB, IDB, SPB, EPB = 0x0A0D0D0A, 1, 3, 6


def _pad(b: bytes) -> bytes:
    return b + b"\x00" * (-len(b) % 4)


def _opt(code: int, val: bytes) -> bytes:
    return struct.pack("<HH", code, len(val)) + _pad(val)


def _block(btype: int, body: bytes) -> bytes:
    total = 12 + len(body)
    return struct.pack("<II", btype, total) + body + struct.pack("<I", total)


def shb(app: str = "Pi Automation Traffic Tester") -> bytes:
    body = struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1) + _opt(4, app.encode()) + _opt(0, b"")
    return _block(SHB, body)


def idb(linktype: int, snaplen: int = 0, name: str = "", descr: str = "") -> bytes:
    opts = b""
    if name:
        opts += _opt(2, name.encode())
    if descr:
        opts += _opt(3, descr.encode())
    opts += _opt(9, bytes([9])) + _opt(0, b"")             # if_tsresol = 10^-9 s
    return _block(IDB, struct.pack("<HHI", linktype, 0, snaplen) + opts)


def epb(iface: int, ts_ns: int, data: bytes, orig_len: Optional[int] = None, comment: str = "") -> bytes:
    body = struct.pack("<IIIII", iface, ts_ns >> 32, ts_ns & 0xFFFFFFFF, len(data), orig_len if orig_len is not None else len(data)) + _pad(data)
    if comment:
        body += _opt(1, comment.encode("utf-8", "replace")) + _opt(0, b"")
    return _block(EPB, body)


class PcapngWriter:
    """One pcapng file with one or more interfaces. Flushes every `flush_every` seconds so a live reader / crash sees the data."""

    def __init__(self, path: str, interfaces: List[Tuple[int, str, str]], flush_every: float = 1.0):
        self.path = path
        self.f = open(path, "wb")
        self.f.write(shb())
        for lt, name, descr in interfaces:
            self.f.write(idb(lt, 0, name, descr))
        self.bytes = self.f.tell()
        self.packets = 0
        self.t_open = time.monotonic()
        self._flush_every, self._last_flush = flush_every, time.monotonic()
        self.ifaces = interfaces

    def write(self, iface: int, ts_ns: int, data: bytes, comment: str = "") -> int:
        b = epb(iface, ts_ns, data, None, comment)
        self.f.write(b)
        self.bytes += len(b)
        self.packets += 1
        now = time.monotonic()
        if now - self._last_flush >= self._flush_every:
            self.f.flush()
            self._last_flush = now
        return len(b)

    def close(self) -> None:
        if not self.f.closed:
            self.f.flush()
            self.f.close()


class RingWriter:
    """Rotating pcapng files: new file when it reaches `max_bytes` or `max_seconds` (0 = off); keeps the newest `keep` files (0 = all)."""

    def __init__(self, directory: str, base: str, interfaces: List[Tuple[int, str, str]], max_bytes: int = 0, max_seconds: float = 0, keep: int = 0,
                 on_rotate: Optional[Callable[[str], None]] = None, suffix: str = ".pcapng"):
        os.makedirs(directory, exist_ok=True)
        self.dir, self.base, self.ifaces = directory, base, interfaces
        self.max_bytes, self.max_seconds, self.keep, self.on_rotate, self.suffix = max_bytes, max_seconds, keep, on_rotate, suffix
        self.n = 0
        self.files: List[str] = []
        self.deleted = 0
        self.total_packets = 0
        self.w: Optional[PcapngWriter] = None
        self._open()

    def _open(self) -> None:
        self.n += 1
        name = f"{self.base}_{self.n:05d}_{dt.datetime.now():%Y%m%d%H%M%S}{self.suffix}"
        self.w = PcapngWriter(os.path.join(self.dir, name), self.ifaces)
        self.files.append(self.w.path)

    def write(self, iface: int, ts_ns: int, data: bytes, comment: str = "") -> None:
        w = self.w
        if (self.max_bytes and w.bytes >= self.max_bytes) or (self.max_seconds and time.monotonic() - w.t_open >= self.max_seconds):
            self._rotate()
        self.w.write(iface, ts_ns, data, comment)
        self.total_packets += 1

    def _rotate(self) -> None:
        old = self.w.path
        self.w.close()
        if self.on_rotate:
            self.on_rotate(old)
        self._open()
        if self.keep and len(self.files) > self.keep:
            while len(self.files) > self.keep:
                p = self.files.pop(0)
                try:
                    os.remove(p)
                    self.deleted += 1
                except OSError:
                    pass

    def close(self) -> None:
        if self.w:
            self.w.close()


# ------------------------------------------------------------------ reader
def read_capture(path: str) -> Iterator[dict]:
    """Yield {iface, linktype, ts_ns, data, orig_len, comment, name} for pcapng and classic pcap files (tolerates a truncated tail)."""
    with open(path, "rb") as f:
        head = f.read(4)
        f.seek(0)
        if head in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
            yield from _read_pcap(f)
        else:
            yield from _read_pcapng(f)


def _read_pcap(f) -> Iterator[dict]:
    h = f.read(24)
    magic = h[:4]
    le = magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1")
    nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    e = "<" if le else ">"
    linktype = struct.unpack(e + "I", h[20:24])[0]
    while True:
        r = f.read(16)
        if len(r) < 16:
            return
        s, us, cl, ol = struct.unpack(e + "IIII", r)
        d = f.read(cl)
        if len(d) < cl:
            return
        yield {"iface": 0, "linktype": linktype, "ts_ns": s * 1_000_000_000 + (us if nano else us * 1000), "data": d, "orig_len": ol, "comment": "", "name": ""}


def _opts(b: bytes, e: str) -> Dict[int, bytes]:
    out, i = {}, 0
    while i + 4 <= len(b):
        code, ln = struct.unpack(e + "HH", b[i:i + 4])
        if code == 0:
            break
        out.setdefault(code, b[i + 4:i + 4 + ln])
        i += 4 + ln + (-ln % 4)
    return out


def _read_pcapng(f) -> Iterator[dict]:
    e = "<"
    ifs: List[dict] = []
    while True:
        h = f.read(8)
        if len(h) < 8:
            return
        bt = struct.unpack(e + "I", h[:4])[0]
        if bt == SHB:                                    # byte order from the magic that follows
            bom = f.read(4)
            e = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            total = struct.unpack(e + "I", h[4:8])[0]
            rest = f.read(total - 12)
            if len(rest) < total - 12:
                return
            ifs = []
            continue
        total = struct.unpack(e + "I", h[4:8])[0]
        body = f.read(total - 8)
        if len(body) < total - 8:
            return
        body = body[:-4]
        if bt == IDB:
            lt, _r, snap = struct.unpack(e + "HHI", body[:8])
            o = _opts(body[8:], e)
            res = o.get(9, b"\x06")[0] if 9 in o else 6
            ifs.append({"linktype": lt, "tsresol": res, "name": o.get(2, b"").decode(errors="replace")})
        elif bt == EPB:
            ifid, th, tl, cl, ol = struct.unpack(e + "IIIII", body[:20])
            data = body[20:20 + cl]
            o = _opts(body[20 + cl + (-cl % 4):], e)
            info = ifs[ifid] if ifid < len(ifs) else {"linktype": 1, "tsresol": 6, "name": ""}
            res = info["tsresol"]
            ts = (th << 32) | tl
            ts_ns = ts * (10 ** (9 - res)) if res <= 9 else ts // (10 ** (res - 9))
            yield {"iface": ifid, "linktype": info["linktype"], "ts_ns": ts_ns, "data": data, "orig_len": ol, "comment": o.get(1, b"").decode(errors="replace"), "name": info["name"]}
        elif bt == SPB and ifs:
            ol = struct.unpack(e + "I", body[:4])[0]
            yield {"iface": 0, "linktype": ifs[0]["linktype"], "ts_ns": 0, "data": body[4:4 + min(ol, len(body) - 4)], "orig_len": ol, "comment": "", "name": ifs[0]["name"]}


class StreamParser:
    """Incremental pcapng parser for a byte stream (dumpcap -w -): feed() returns complete packet dicts."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.e = "<"
        self.ifs: List[dict] = []

    def feed(self, data: bytes) -> List[dict]:
        self.buf += data
        out: List[dict] = []
        b = self.buf
        pos = 0
        while len(b) - pos >= 12:
            bt = struct.unpack_from("<I", b, pos)[0]
            if bt == SHB:
                if len(b) - pos < 12:
                    break
                self.e = "<" if bytes(b[pos + 8:pos + 12]) == b"\x4d\x3c\x2b\x1a" else ">"
            total = struct.unpack_from(self.e + "I", b, pos + 4)[0]
            if total < 12 or total > 1 << 24:
                pos = len(b)                              # corrupt: drop everything buffered
                break
            if len(b) - pos < total:
                break
            body = bytes(b[pos + 8:pos + total - 4])
            e = self.e
            if bt == SHB:
                self.ifs = []
            elif bt == IDB:
                lt, _r, _snap = struct.unpack(e + "HHI", body[:8])
                o = _opts(body[8:], e)
                self.ifs.append({"linktype": lt, "tsresol": o[9][0] if 9 in o else 6, "name": o.get(2, b"").decode(errors="replace")})
            elif bt == EPB:
                ifid, th, tl, cl, ol = struct.unpack(e + "IIIII", body[:20])
                info = self.ifs[ifid] if ifid < len(self.ifs) else {"linktype": 1, "tsresol": 6, "name": ""}
                res = info["tsresol"]
                ts = (th << 32) | tl
                out.append({"iface": ifid, "linktype": info["linktype"], "ts_ns": ts * (10 ** (9 - res)) if res <= 9 else ts // (10 ** (res - 9)),
                            "data": body[20:20 + cl], "orig_len": ol, "name": info["name"]})
            pos += total
        del b[:pos]
        return out
