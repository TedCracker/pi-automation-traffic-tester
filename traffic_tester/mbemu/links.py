"""Byte-level links for the Mirrored Bits emulator: serial (parity / framing markers, overrun counter), UDP and TCP serial-over-IP
(each message is sent the moment it is built: TCP_NODELAY, no batching), and an in-process pair for the internal back-to-back self-test."""
from __future__ import annotations

import collections
import fcntl
import os
import select
import socket
import struct
import termios
import threading
import time
from typing import Dict, List, Optional, Tuple

NS = 1_000_000_000

Rx = Tuple[bytes, int, int]
mono = time.monotonic_ns
real = time.time_ns


class Link:
    """Byte-level link.  send() returns the bytes accepted (0 = skipped, nothing sent) and raises OSError when
    the link is down; recv() returns [(bytes, t_mono_ns, t_real_ns)] per received chunk/datagram."""
    datagram = False
    char_mode = "plain"      # "parmrk": the stream carries termios PARMRK markers (0xFF 0x00 c = bad character, 0xFF 0xFF = data 0xFF)
    overrun_available = False
    generation = 0           # bumped whenever the byte stream restarts (TCP reconnect) -> parser reset

    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, data: bytes) -> int: return 0
    def recv(self, timeout_s: float) -> List[Rx]: return []
    def take_errors(self) -> Dict[str, int]: return {}
    def describe(self) -> str: return self.__class__.__name__



# ============================================================ serial
TIOCGICOUNT = 0x545D


class MBSerialLink(Link):
    """Real serial port (or a pty).  The line discipline is switched to PARMRK + INPCK (IGNPAR off), so a character with a parity or framing
    error arrives as 0xFF 0x00 <char> (and a real 0xFF as 0xFF 0xFF); the engine's framer undoes that.  Overruns come from the driver's
    TIOCGICOUNT counters; where a driver has none (pty, some USB adapters) `overrun_available` is False and the screen says "not available".
    Linux cannot tell a parity error from a framing error in this form: with parity N the marker is counted as framing, otherwise as parity."""
    char_mode = "parmrk"

    def __init__(self, port: str, baud=19200, bytesize=8, parity="N", stopbits=1, rtscts=False, max_out_bytes=16):
        self.port, self.baud, self.bytesize, self.parity, self.stopbits, self.rtscts = port, baud, bytesize, parity, stopbits, rtscts
        self.max_out_bytes = max_out_bytes
        self.ser = None
        self.skipped = 0
        self.overrun_available: Optional[bool] = None
        self.parmrk_on = False
        self._ic0: Optional[Tuple[int, ...]] = None
        self.err_name = "framing" if parity == "N" else "parity"

    def open(self):
        import serial
        self.ser = serial.Serial(self.port, self.baud, bytesize=self.bytesize, parity=self.parity, stopbits=self.stopbits,
                                 rtscts=bool(self.rtscts), timeout=0, write_timeout=0.05)
        fd = self.ser.fileno()
        try:
            a = termios.tcgetattr(fd)
            a[0] = (a[0] | termios.PARMRK | termios.INPCK) & ~(termios.IGNPAR | termios.ISTRIP | termios.IGNBRK | termios.BRKINT)
            termios.tcsetattr(fd, termios.TCSANOW, a)
            self.parmrk_on = True
        except (termios.error, OSError):
            self.parmrk_on = False
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self._ic0 = self._icount()
        self.overrun_available = self._ic0 is not None
        if not self.parmrk_on:
            self.char_mode = "plain"

    def _icount(self) -> Optional[Tuple[int, ...]]:
        try:
            buf = fcntl.ioctl(self.ser.fileno(), TIOCGICOUNT, bytes(80))
        except (OSError, AttributeError, TypeError):
            return None
        return struct.unpack("20i", buf)

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def send(self, data):
        if self.ser is None:
            raise OSError("serial port closed")
        try:
            if self.ser.out_waiting > self.max_out_bytes:          # never let latency creep up: skip, do not queue
                self.skipped += 1
                return 0
        except Exception:
            pass
        try:
            self.ser.write(data)                        # no flush(): tcdrain would block on the whole queue
        except Exception as e:  # noqa
            if type(e).__name__ == "SerialTimeoutException":
                self.skipped += 1
                return 0
            raise OSError(str(e)) from e
        return len(data)

    def recv(self, timeout_s):
        if self.ser is None:
            raise OSError("serial port closed")
        fd = self.ser.fileno()
        r, _, _ = select.select([fd], [], [], max(0.0, timeout_s))
        if not r:
            return []
        t_m, t_r = mono(), real()
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return []
        if not data:
            raise OSError("serial port hung up")
        return [(data, t_m, t_r)]

    def take_errors(self) -> Dict[str, int]:
        """Driver counters that moved since the last call: {'overrun': n, 'frame': n, 'parity': n} ({} = nothing or not available)."""
        if not self.overrun_available:
            return {}
        c = self._icount()
        if c is None or self._ic0 is None:
            return {}
        d = {"frame": c[6] - self._ic0[6], "overrun": (c[7] - self._ic0[7]) + (c[10] - self._ic0[10]), "parity": c[8] - self._ic0[8]}
        self._ic0 = c
        return {k: v for k, v in d.items() if v > 0}

    def describe(self):
        return f"serial {self.port} @ {self.baud} {self.bytesize}{self.parity}{self.stopbits}" + (" RTS/CTS" if self.rtscts else "")


# ============================================================ in-process pair (internal back-to-back self-test)
class InternalLink(Link):
    """One end of an in-process wire.  `InternalLink.pair()` returns the two ends.  Plain bytes by default; in char_mode="parmrk" the wire
    behaves like a tty with PARMRK (0xFF doubled) and `send_marked` can deliver characters as 'bad' (0xFF 0x00 c)."""

    def __init__(self, char_mode: str = "plain"):
        self.char_mode = char_mode
        self.peer: Optional["InternalLink"] = None
        self._q: "collections.deque[Tuple[bytes, int, int]]" = collections.deque()
        self._cv = threading.Condition()
        self.closed = False
        self.sent = 0
        self.overrun_available = False
        self.name = "internal"

    @classmethod
    def pair(cls, char_mode: str = "plain") -> Tuple["InternalLink", "InternalLink"]:
        a, b = cls(char_mode), cls(char_mode)
        a.peer, b.peer = b, a
        a.name, b.name = "internal A", "internal B"
        return a, b

    def open(self):
        self.closed = False

    def close(self):
        self.closed = True
        with self._cv:
            self._cv.notify_all()

    def _deliver(self, data: bytes):
        p = self.peer
        if p is None or p.closed:
            return
        with p._cv:
            p._q.append((data, mono(), real()))
            p._cv.notify()

    def send(self, data):
        if self.closed:
            raise OSError("internal link closed")
        if self.char_mode == "parmrk":
            data = data.replace(b"\xff", b"\xff\xff")
        self.sent += len(data)
        self._deliver(bytes(data))
        return len(data)

    def send_marked(self, data: bytes, bad: List[int]) -> int:
        """Deliver `data` with the characters at the indexes in `bad` marked as received with a parity/framing error."""
        out = bytearray()
        for i, b in enumerate(data):
            if i in bad:
                out += b"\xff\x00" + bytes([b])
            elif b == 0xFF:
                out += b"\xff\xff"
            else:
                out.append(b)
        self._deliver(bytes(out))
        return len(data)

    def recv(self, timeout_s):
        if self.closed:
            raise OSError("internal link closed")
        with self._cv:
            if not self._q and timeout_s > 0:
                self._cv.wait(timeout_s)
            out = list(self._q)
            self._q.clear()
        return out

    def describe(self):
        return f"in-process ({self.name})"


# ============================================================ UDP / TCP serial-over-IP (from the first Mirrored Bits mode: unchanged behaviour)
class UdpLink(Link):
    """Role A sends to peer_host:port from an ephemeral port.  Role B binds `port`; it transmits only once it
    knows the peer (peer_host given, or learned from the last datagram it received)."""
    datagram = True

    def __init__(self, role: str, peer_host: str, port: int, dscp: int = 46, bind_host: str = "0.0.0.0"):
        self.role, self.peer_host, self.port, self.dscp, self.bind_host = role.upper(), peer_host, port, dscp, bind_host
        self.sock: Optional[socket.socket] = None
        self.peer: Optional[Tuple[str, int]] = None

    def open(self):
        if self.role == "A" and not self.peer_host:
            raise ValueError("UDP role A needs the peer host")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, (self.dscp & 0x3F) << 2)
        except OSError:
            pass
        if self.role == "B":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.bind_host, self.port))
        s.setblocking(False)
        self.sock = s
        self.peer = (self.peer_host, self.port) if self.peer_host else None

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def send(self, data):
        if self.sock is None:
            raise OSError("udp socket closed")
        if not self.peer:
            return 0
        try:
            self.sock.sendto(data, self.peer)
        except ConnectionRefusedError:      # ICMP port unreachable from an earlier datagram: just a lost message
            return 0
        except BlockingIOError:
            return 0
        return len(data)

    def recv(self, timeout_s):
        if self.sock is None:
            raise OSError("udp socket closed")
        r, _, _ = select.select([self.sock], [], [], max(0.0, timeout_s))
        if not r:
            return []
        out: List[Rx] = []
        for _ in range(64):
            try:
                data, addr = self.sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:                 # ICMP errors
                break
            out.append((data, mono(), real()))
            if self.role == "B":
                self.peer = addr
        return out

    def describe(self):
        return f"UDP {self.role} {self.peer_host or '*'}:{self.port}"


class TcpLink(Link):
    """Role A connects (retry every 200 ms while down), role B listens (newest connection wins)."""

    def __init__(self, role: str, peer_host: str, port: int, dscp: int = 46, bind_host: str = "0.0.0.0",
                 keepalive_idle_s: int = 5, connect_timeout_s: float = 0.3):
        self.role, self.peer_host, self.port, self.dscp = role.upper(), peer_host, port, dscp
        self.bind_host, self.keepalive_idle_s, self.connect_timeout_s = bind_host, keepalive_idle_s, connect_timeout_s
        self.listen: Optional[socket.socket] = None
        self.conn: Optional[socket.socket] = None
        self.generation = 0
        self.sessions = 0
        self.last_attempt = 0.0
        self.skipped = 0
        self._rest = b""

    @property
    def connected(self) -> bool:
        return self.conn is not None

    def _tune(self, s: socket.socket):
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, (self.dscp & 0x3F) << 2)
        except OSError:
            pass
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, val in (("TCP_KEEPIDLE", self.keepalive_idle_s), ("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, opt):
                try:
                    s.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
                except OSError:
                    pass

    def _new_conn(self, s: socket.socket):
        self._tune(s)
        s.setblocking(False)
        if self.conn:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn, self._rest = s, b""
        self.generation += 1
        self.sessions += 1

    def open(self):
        if self.role == "B":
            ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls.bind((self.bind_host, self.port))
            ls.listen(4)
            self.listen = ls
        else:
            self._connect()

    def _connect(self) -> bool:
        self.last_attempt = time.monotonic()
        try:
            s = socket.create_connection((self.peer_host, self.port), timeout=self.connect_timeout_s)
        except OSError:
            return False
        self._new_conn(s)
        return True

    def _drop(self):
        if self.conn:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn, self._rest = None, b""

    def close(self):
        self._drop()
        if self.listen:
            self.listen.close()
            self.listen = None

    def send(self, data):
        if self.conn is None:
            raise OSError("tcp session down")
        try:
            if self._rest:                          # finish a partially sent message first
                n = self.conn.send(self._rest)
                self._rest = self._rest[n:]
                if self._rest:
                    self.skipped += 1
                    return 0
            n = self.conn.send(data)
        except (BlockingIOError, InterruptedError):
            self.skipped += 1
            return 0
        except OSError:
            self._drop()
            raise
        if n < len(data):
            self._rest = data[n:]
        return len(data)

    def recv(self, timeout_s):
        if self.role == "A" and self.conn is None:
            wait = max(0.0, 0.2 - (time.monotonic() - self.last_attempt))
            if wait <= timeout_s:
                time.sleep(wait)
                self._connect()
                return []
            time.sleep(max(0.0, timeout_s))
            return []
        socks = [self.conn] if self.conn else []
        if self.role == "B" and self.listen:
            socks.append(self.listen)
        if not socks:
            time.sleep(max(0.0, timeout_s))
            return []
        r, _, _ = select.select(socks, [], [], max(0.0, timeout_s))
        out: List[Rx] = []
        for s in r:
            if s is self.listen:
                try:
                    c, _ = self.listen.accept()
                except OSError:
                    continue
                self._new_conn(c)                   # newest connection wins
                continue
            try:
                data = s.recv(4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                data = b""
            t_m, t_r = mono(), real()
            if not data:
                self._drop()
                continue
            out.append((data, t_m, t_r))
        return out

    def describe(self):
        return f"TCP {self.role} {self.peer_host or '*'}:{self.port}"


