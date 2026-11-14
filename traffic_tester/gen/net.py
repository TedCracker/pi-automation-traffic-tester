"""ctypes fast path: sendmmsg / recvmmsg batches, ethertype BPF for raw receive (no C compiler needed on the Pi)."""
from __future__ import annotations

import ctypes
import ctypes.util
import socket
import struct
from typing import List, Optional

libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


class Iovec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]


class Msghdr(ctypes.Structure):
    _fields_ = [("name", ctypes.c_void_p), ("namelen", ctypes.c_uint32), ("iov", ctypes.c_void_p), ("iovlen", ctypes.c_size_t),
                ("control", ctypes.c_void_p), ("controllen", ctypes.c_size_t), ("flags", ctypes.c_int)]


class Mmsghdr(ctypes.Structure):
    _fields_ = [("hdr", Msghdr), ("len", ctypes.c_uint)]


libc.sendmmsg.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
libc.sendmmsg.restype = ctypes.c_int
libc.recvmmsg.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_void_p]
libc.recvmmsg.restype = ctypes.c_int
MSG_DONTWAIT = 0x40
MSG_TRUNC = 0x20
MMSG_SIZE = ctypes.sizeof(Mmsghdr)


class BatchTx:
    """Up to `n` pre-allocated packet slots sent with one sendmmsg call."""

    def __init__(self, n: int, slot_bytes: int):
        self.n = n
        self.bufs = [bytearray(slot_bytes) for _ in range(n)]
        self._c = [(ctypes.c_char * slot_bytes).from_buffer(b) for b in self.bufs]
        self.iov = (Iovec * n)()
        self.msgs = (Mmsghdr * n)()
        for i in range(n):
            self.iov[i].base = ctypes.addressof(self._c[i])
            self.iov[i].len = slot_bytes
            h = self.msgs[i].hdr
            h.iov = ctypes.addressof(self.iov[i])
            h.iovlen = 1
        self.base_addr = ctypes.addressof(self.msgs)

    def set_len(self, i: int, ln: int) -> None:
        self.iov[i].len = ln

    def send(self, fd: int, count: int) -> int:
        """Send slots [0:count]; returns packets sent (raises OSError on a hard error before anything was sent)."""
        done = 0
        while done < count:
            r = libc.sendmmsg(fd, self.base_addr + done * MMSG_SIZE, count - done, 0)
            if r < 0:
                e = ctypes.get_errno()
                if done:
                    return done
                raise OSError(e, "sendmmsg failed")
            done += r
        return done


class BatchRx:
    """recvmmsg into one big buffer; optional per-message source address buffers (for echo replies)."""

    def __init__(self, n: int = 64, slot_bytes: int = 2048, want_addr: bool = False):
        self.n, self.slot = n, slot_bytes
        self.buf = bytearray(n * slot_bytes)
        self._c = (ctypes.c_char * len(self.buf)).from_buffer(self.buf)
        self.mv = memoryview(self.buf)
        self.iov = (Iovec * n)()
        self.msgs = (Mmsghdr * n)()
        self.addr = bytearray(n * 128) if want_addr else None
        self._ac = (ctypes.c_char * len(self.addr)).from_buffer(self.addr) if want_addr else None
        base = ctypes.addressof(self._c)
        for i in range(n):
            self.iov[i].base = base + i * slot_bytes
            self.iov[i].len = slot_bytes
            h = self.msgs[i].hdr
            h.iov = ctypes.addressof(self.iov[i])
            h.iovlen = 1
        self.base_addr = ctypes.addressof(self.msgs)

    def recv(self, fd: int) -> int:
        if self.addr is not None:
            ab = ctypes.addressof(self._ac)
            for i in range(self.n):
                h = self.msgs[i].hdr
                h.name = ab + i * 128
                h.namelen = 128
        r = libc.recvmmsg(fd, self.base_addr, self.n, MSG_DONTWAIT | MSG_TRUNC, None)
        if r < 0:
            e = ctypes.get_errno()
            if e in (11, 4):            # EAGAIN / EINTR
                return 0
            raise OSError(e, "recvmmsg failed")
        return r

    def length(self, i: int) -> int:
        """Real length of message i (MSG_TRUNC): can be more than the slot, which then holds only the first slot bytes."""
        return self.msgs[i].len

    def src_ipv4(self, i: int) -> Optional[str]:
        """Source IPv4 address of message i (AF_INET sockaddr)."""
        if self.addr is None:
            return None
        o = i * 128
        if struct.unpack_from("<H", self.addr, o)[0] != socket.AF_INET:
            return None
        return socket.inet_ntoa(bytes(self.addr[o + 4:o + 8]))


def attach_ethertype_bpf(sock: socket.socket, ethertype: int) -> None:
    """Classic BPF: accept EtherType `ethertype`, or 0x8100 followed by it (VLAN tag still inline)."""
    prog = [(0x28, 0, 0, 12), (0x15, 3, 0, ethertype), (0x15, 0, 3, 0x8100), (0x28, 0, 0, 16),
            (0x15, 0, 1, ethertype), (0x06, 0, 0, 0xFFFF), (0x06, 0, 0, 0)]
    buf = ctypes.create_string_buffer(b"".join(struct.pack("HBBI", *i) for i in prog))
    try:
        sock.setsockopt(socket.SOL_SOCKET, 26, struct.pack("@HP", len(prog), ctypes.addressof(buf)))
    except OSError:
        pass


def iface_mac(name: str) -> bytes:
    with open(f"/sys/class/net/{name}/address") as f:
        return bytes(int(x, 16) for x in f.read().strip().split(":"))
