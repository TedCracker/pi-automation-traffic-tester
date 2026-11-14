"""Blocking transports used by the engine threads (docs/SPEC.md 3, 5).

Every transport exposes:
  open() / close()
  write(frame)                      -> None   (raises OSError when the link is down)
  read(timeout_s)                   -> list[(Frame, rx_mono_ns, rx_real_ns)]
  corrupted (int), events (list of dict, drained by the engine)
"""
from __future__ import annotations

import select
import socket
import threading
import time
from typing import List, Optional, Tuple

from . import frames as F

Rx = Tuple[F.Frame, int, int]
mono = time.monotonic_ns
real = time.time_ns


class Transport:
    corrupted = 0
    role = "tester"          # or 'responder'

    def __init__(self) -> None:
        self.events: List[dict] = []

    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, frame: F.Frame) -> None: ...
    def read(self, timeout: float) -> List[Rx]: ...
    def describe(self) -> str: return self.__class__.__name__


# ---------------------------------------------------------------- serial
class SerialTransport(Transport):
    def __init__(self, port: str, baud=19200, bytesize=8, parity="N", stopbits=1):
        super().__init__()
        self.port, self.baud, self.bytesize, self.parity, self.stopbits = port, baud, bytesize, parity, stopbits
        self.ser = None
        self.parser = F.StreamParser()

    def open(self):
        import serial
        self.ser = serial.Serial(self.port, self.baud, bytesize=self.bytesize, parity=self.parity,
                                 stopbits=self.stopbits, timeout=0, write_timeout=2)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def write(self, frame):
        data = F.encode(frame)
        self.ser.write(data)
        try:
            self.ser.flush()
        except Exception:
            pass

    def read(self, timeout):
        r, _, _ = select.select([self.ser.fileno()], [], [], max(0.0, timeout))
        if not r:
            return []
        t_m, t_r = mono(), real()
        data = self.ser.read(4096)
        if not data:
            return []
        out = [(f, t_m, t_r) for f in self.parser.feed(data)]
        self.corrupted = self.parser.corrupted
        return out

    def describe(self):
        return f"serial {self.port} @ {self.baud} {self.bytesize}{self.parity}{self.stopbits}"


# ---------------------------------------------------------------- sending through one chosen network port
def bind_to_iface(s: socket.socket, iface: str) -> None:
    """Make a sending socket leave through one network port: bound to the device when the service may (CAP_NET_RAW), and to the port's own
    address either way, so the far end sees the test coming from that port. A port without an address raises OSError (plain message)."""
    from . import system
    ip = system.iface_ip(iface)
    if not ip:
        raise OSError(f"{iface} has no IP address. Give it one in Settings \u2192 Network, or pick another port.")
    try:
        s.setsockopt(socket.SOL_SOCKET, 25, iface.encode() + b"\0")      # SO_BINDTODEVICE
    except OSError:
        pass
    s.bind((ip, 0))


# ---------------------------------------------------------------- UDP
class UdpTransport(Transport):
    def __init__(self, role: str, peer_host: str, port: int, dscp: int = 46, bind_host: str = "0.0.0.0", iface: str = ""):
        super().__init__()
        self.role, self.peer_host, self.port, self.dscp, self.bind_host = role, peer_host, port, dscp, bind_host
        self.iface = iface if iface not in ("", "auto") else ""
        self.sock: Optional[socket.socket] = None
        self.peer: Optional[Tuple[str, int]] = (peer_host, port) if peer_host else None

    def open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, (self.dscp & 0x3F) << 2)
        except OSError:
            pass
        if self.role == "responder":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.bind_host, self.port))
        elif self.iface:
            try:
                bind_to_iface(s, self.iface)
            except OSError:
                s.close()
                raise
        s.setblocking(True)
        self.sock = s

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def write(self, frame):
        if not self.peer:
            return
        self.sock.sendto(F.encode(frame), self.peer)

    def read(self, timeout):
        r, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
        if not r:
            return []
        out: List[Rx] = []
        for _ in range(64):
            try:
                self.sock.setblocking(False)
                data, addr = self.sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:      # ICMP port unreachable etc.
                break
            finally:
                self.sock.setblocking(True)
            t_m, t_r = mono(), real()
            f = F.decode_datagram(data)
            if f is None:
                self.corrupted += 1
                continue
            if self.role == "responder":
                self.peer = addr
            out.append((f, t_m, t_r))
        return out

    def describe(self):
        return f"UDP {self.peer_host or '*'}:{self.port}"


# ---------------------------------------------------------------- TCP
class TcpTransport(Transport):
    """Tester: connects (and reconnects every 200 ms). Responder: listens, serves one connection at a time."""

    def __init__(self, role: str, peer_host: str, port: int, dscp: int = 46, keepalive=True,
                 keepalive_idle_s=5, bind_host="0.0.0.0", connect_timeout_s=0.3, iface: str = ""):
        super().__init__()
        self.iface = iface if iface not in ("", "auto") else ""
        self.connect_timeout_s = connect_timeout_s
        self.role, self.peer_host, self.port, self.dscp = role, peer_host, port, dscp
        self.keepalive, self.keepalive_idle_s, self.bind_host = keepalive, keepalive_idle_s, bind_host
        self.listen: Optional[socket.socket] = None
        self.conn: Optional[socket.socket] = None
        self.parser = F.StreamParser()
        self.connected = False
        self.last_attempt = 0.0
        self.down_since: Optional[int] = None     # mono ns when the session was lost
        self.connect_ms: List[float] = []
        self.sessions = 0

    def _tune(self, s: socket.socket):
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, (self.dscp & 0x3F) << 2)
        except OSError:
            pass
        if self.keepalive:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", self.keepalive_idle_s), ("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 3)):
                if hasattr(socket, opt):
                    try:
                        s.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
                    except OSError:
                        pass

    def open(self):
        if self.role == "responder":
            ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls.bind((self.bind_host, self.port))
            ls.listen(4)
            self.listen = ls
        else:
            self._connect(first=True)

    def _connect(self, first=False) -> bool:
        self.last_attempt = time.monotonic()
        t0 = mono()
        try:
            if self.iface:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    bind_to_iface(s, self.iface)
                    s.settimeout(self.connect_timeout_s)
                    s.connect((self.peer_host, self.port))
                except OSError:
                    s.close()
                    raise
            else:
                s = socket.create_connection((self.peer_host, self.port), timeout=self.connect_timeout_s)
        except OSError:
            return False
        ms = (mono() - t0) / 1e6
        self._tune(s)
        s.settimeout(None)
        self.conn, self.connected, self.parser = s, True, F.StreamParser()
        self.sessions += 1
        self.connect_ms.append(ms)
        self.events.append({"event": "connected", "connect_ms": round(ms, 3), "first": first, "t_mono_ns": mono()})
        return True

    def _drop(self):
        if self.conn:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn = None
        if self.connected:
            self.connected = False
            self.down_since = mono()
            self.events.append({"event": "disconnected", "t_mono_ns": self.down_since})

    def force_reconnect(self):
        self._drop()

    def close(self):
        self._drop()
        if self.listen:
            self.listen.close()
            self.listen = None

    def write(self, frame):
        if not self.connected:
            raise OSError("tcp session down")
        try:
            self.conn.sendall(F.encode(frame))
        except OSError:
            self._drop()
            raise

    def read(self, timeout):
        if self.role == "tester" and not self.connected:
            # reconnect attempts every 200 ms, never blocking longer than `timeout`
            wait = max(0.0, 0.2 - (time.monotonic() - self.last_attempt))
            if wait <= timeout:
                time.sleep(wait)
                self._connect()
                return []
            time.sleep(max(0.0, timeout))
            return []
        socks = [self.conn] if self.conn else []
        if self.role == "responder":
            if self.listen:
                socks.append(self.listen)
            if not socks:
                time.sleep(max(0.0, timeout))
                return []
        r, _, _ = select.select(socks, [], [], max(0.0, timeout))
        out: List[Rx] = []
        for s in r:
            if s is self.listen:
                c, _ = self.listen.accept()
                self._tune(c)
                if self.conn:                  # newest connection wins (old one is probably dead)
                    try:
                        self.conn.close()
                    except OSError:
                        pass
                self.conn, self.connected, self.parser = c, True, F.StreamParser()
                self.sessions += 1
                self.events.append({"event": "accepted", "t_mono_ns": mono()})
                continue
            try:
                data = s.recv(4096)
            except OSError:
                data = b""
            t_m, t_r = mono(), real()
            if not data:
                self._drop()
                continue
            out += [(f, t_m, t_r) for f in self.parser.feed(data)]
            self.corrupted = self.parser.corrupted
        return out

    def describe(self):
        return f"TCP {self.peer_host or '*'}:{self.port}"
