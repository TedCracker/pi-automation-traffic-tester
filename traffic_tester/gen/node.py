"""Generator node: the senders and receivers that run on ONE Pi (docs/SPEC.md section 13).

A run on Pi A drives its own node directly and Pi B's node over HTTP (see run.py / server.py)."""
from __future__ import annotations

import os
import select
import socket
import struct
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import net as N
from . import proto as P

PACKET_IGNORE_OUTGOING = 23
SOL_PACKET = 263


class GenError(Exception):
    pass


def _first_wired() -> str:
    from .. import system
    for i in system.interfaces():
        if i["wired"] and i["up"]:
            return i["name"]
    raise GenError("No wired network port found. Pick one in the settings.")


def resolve_iface(name: str) -> str:
    return _first_wired() if not name or name == "auto" else name


def iface_ipv4(name: str) -> Optional[str]:
    import fcntl
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))[20:24])
    except OSError:
        return None
    finally:
        s.close()


def big_rcvbuf(s: socket.socket, size: int = 16 << 20) -> None:
    """Large receive buffer so a short scheduling stall does not drop packets. SO_RCVBUFFORCE (needs CAP_NET_ADMIN, which the
    service has) ignores net.core.rmem_max; otherwise the normal option is capped by it (installer raises the cap)."""
    try:
        s.setsockopt(socket.SOL_SOCKET, 33, size)
    except OSError:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, size)
        except OSError:
            pass


def _l2_socket(iface: str, ethertype: Optional[int], recv: bool) -> socket.socket:
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3) if recv else 0)
        s.bind((iface, 3 if recv else 0))
    except PermissionError:
        raise GenError("Raw Ethernet streams need extra network permission (CAP_NET_RAW). This copy of the app was started without it; the full install (sudo scripts/install-pi.sh) grants it to the service.")
    except OSError as e:
        raise GenError(f"Cannot open port {iface}: {e.strerror}")
    if recv:
        try:
            s.setsockopt(SOL_PACKET, PACKET_IGNORE_OUTGOING, 1)
        except OSError:
            pass
        N.attach_ethertype_bpf(s, ethertype)
        big_rcvbuf(s)
    else:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 << 20)
    return s


def stream_pps(cfg: dict, mean: Optional[float] = None) -> Tuple[float, float]:
    """(configured rate in packets/s, packets/s really sent). They differ only when a burst gap is set: the sender then
    sends burst_packets back to back every gap and the rate setting is not used."""
    if mean is None:
        sz = cfg["size"]
        mean = P.mean_size(P.size_table(sz["mode"], sz.get("size", 64), sz.get("min", 64), sz.get("max", 1518), cfg.get("seed", 1),
                                        cfg["type"], bool(cfg.get("vlan")) and cfg["type"] == "l2"))
    r = cfg["rate"]
    pps = max(0.1, float(r["pps"]) if r.get("pps") else float(r["mbps"]) * 1e6 / 8.0 / mean)
    burst = max(1, int(cfg.get("burst_packets", 1)))
    gap = float(cfg.get("burst_gap_ms", 0)) / 1000.0
    return pps, (burst / (gap + (burst / pps if pps > 1e5 else 0)) if gap > 0 else pps)


def _test_drop(stream_id: int) -> int:
    """Test hook, only with TT_TEST_HOOKS=1: TT_GEN_TEST_DROP="<first stream id>:<N>" makes this Pi's receivers ignore every Nth packet
    of the streams with that id or a higher one (a lossy link for the automatic tests and the screenshots)."""
    spec = os.environ.get("TT_GEN_TEST_DROP", "") if os.environ.get("TT_TEST_HOOKS") == "1" else ""
    if not spec:
        return 0
    try:
        first, every = (int(x) for x in spec.split(":"))
    except ValueError:
        return 0
    return every if stream_id >= first and every > 0 else 0


# ------------------------------------------------------------------ statistics
class TxStat:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.pkts = self.bytes = self.err = 0
        self.rtt = P.Hist()
        self.rtt_sec = P.Hist()
        self.t_start: Optional[float] = None
        self.t_end: Optional[float] = None
        self.target_pps = 0.0
        self._last = (0, 0)

    def result(self) -> dict:
        dur = max(1e-3, ((self.t_end or time.monotonic()) - (self.t_start or time.monotonic())))
        return {"pkts": self.pkts, "bytes": self.bytes, "err": self.err, "duration_s": round(dur, 3),
                "target_pps": round(self.target_pps, 1), "achieved_pps": round(self.pkts / dur, 1),
                "achieved_mbps": round(self.bytes * 8 / dur / 1e6, 3), "rtt": self.rtt.to_dict()}


class RxStat:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tr = P.SeqTracker()
        self.pkts = self.bytes = 0
        self.hist = P.Hist()
        self.sec = P.Hist()
        self.J = 0.0
        self.Jmax = 0.0
        self.Jsum = 0.0
        self.Jn = 0
        self.prev_transit: Optional[int] = None
        self.t_first: Optional[float] = None
        self.t_last: Optional[float] = None
        self._last = (0, 0)
        self._pcts: Tuple[int, dict] = (-1, {})
        self.drop_every = 0            # test hook (TT_TEST_HOOKS): pretend every Nth packet of this stream never arrived
        self._seen = 0

    def live_pcts(self) -> dict:
        """Delay percentiles so far (microseconds), for a controller that judges a step while the run goes on. Worked out again only when packets arrived."""
        n = self.hist.n
        if self._pcts[0] != n:
            h = self.hist
            self._pcts = (n, {"p50_us": h.pct(50), "p95_us": h.pct(95), "p99_us": h.pct(99), "max_us": h.max} if n else {})
        return self._pcts[1]

    def on_packet(self, seq: int, t_tx: int, t_rx: int, nbytes: int, now: float) -> None:
        if self.drop_every:
            self._seen += 1
            if self._seen % self.drop_every == 0:
                return
        self.pkts += 1
        self.bytes += nbytes
        if self.t_first is None:
            self.t_first = now
        self.t_last = now
        if self.tr.see(seq):                           # not a duplicate: counts for delay and (in arrival order) jitter
            transit = t_rx - t_tx
            lat = transit // 1000
            if lat < 0:
                lat = 0
            self.hist.add(lat)
            self.sec.add(lat)
            if self.prev_transit is not None:           # RFC 3550 interarrival jitter, arrival order
                d = transit - self.prev_transit
                self.J += ((d if d >= 0 else -d) - self.J) / 16.0
                if self.J > self.Jmax:
                    self.Jmax = self.J
                self.Jsum += self.J
                self.Jn += 1
            self.prev_transit = transit

    def result(self) -> dict:
        t = self.tr
        dur = max(1e-3, (self.t_last or 0) - (self.t_first or 0)) if self.t_first else 0.0
        return {"pkts": self.pkts, "bytes": self.bytes, "unique": t.count, "dup": t.dup, "ooo": t.ooo,
                "lost_gap": t.lost_so_far, "duration_s": round(dur, 3),
                "lat": self.hist.to_dict(),
                "jitter_us": round(self.Jsum / self.Jn / 1000.0, 1) if self.Jn else None,
                "jitter_max_us": round(self.Jmax / 1000.0, 1)}


# ------------------------------------------------------------------ sender
class Sender(threading.Thread):
    def __init__(self, cfg: dict, iface: str, stat: TxStat, t0: float):
        super().__init__(name=f"gen-tx-{cfg['id']}", daemon=True)
        self.cfg, self.iface, self.stat, self.t0 = cfg, iface, stat, t0
        self.stop_evt = threading.Event()
        self.sock: Optional[socket.socket] = None
        self.error: Optional[str] = None
        self.done = threading.Event()
        self.paused = threading.Event()                  # a stream switched off during the run: nothing is sent while this is set
        self.vlan = bool(cfg.get("vlan")) and cfg["type"] == "l2"
        sz = cfg["size"]
        self.sizes = P.size_table(sz["mode"], sz.get("size", 64), sz.get("min", 64), sz.get("max", 1518), cfg.get("seed", 1), cfg["type"], self.vlan)
        self.mean = P.mean_size(self.sizes)
        self.pps, stat.target_pps = stream_pps(cfg, self.mean)   # with a burst gap the target is burst / gap, not the rate
        self.burst = max(1, int(cfg.get("burst_packets", 1)))
        self.gap = float(cfg.get("burst_gap_ms", 0)) / 1000.0
        self.echo_every = 0
        if cfg.get("rtt_probe_pps", 0) and cfg["type"] != "tcp":
            self.echo_every = max(1, int(round(self.pps / float(cfg["rtt_probe_pps"]))))
        self.open()

    # -- socket / frame setup (raises GenError synchronously)
    def open(self) -> None:
        c, t = self.cfg, self.cfg["type"]
        try:
            if t == "udp":
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 << 20)
                s.connect((c["dst_ip"], int(c["dst_port"])))
            elif t == "ip":
                s = socket.socket(socket.AF_INET, socket.SOCK_RAW, int(c.get("ip_proto", 253)))
                s.connect((c["dst_ip"], 0))
            elif t == "tcp":
                s = socket.create_connection((c["dst_ip"], int(c["dst_port"])), timeout=5)
                s.settimeout(None)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            else:
                s = _l2_socket(self.iface, None, False)
        except PermissionError:
            raise GenError("Raw IP streams need extra network permission (CAP_NET_RAW). This copy of the app was started without it; the full install (sudo scripts/install-pi.sh) grants it to the service.")
        except GenError:
            raise
        except OSError as e:
            raise GenError(f"Stream '{c['name']}': cannot connect to {c.get('dst_ip', '')}: {e.strerror or e}")
        if t in ("udp", "ip", "tcp"):
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, (int(c.get("dscp", 0)) & 0x3F) << 2)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, int(c.get("ttl", 64)))
            except OSError:
                pass
        self.sock = s
        # frame layout: which bytes of the chosen size are ours
        self.ov = P.overhead(t, self.vlan)
        self.prefix = b""
        if t == "l2":
            src = P.parse_mac(c["src_mac"]) if c.get("src_mac") else N.iface_mac(self.iface)
            self.prefix = P.l2_prefix(P.parse_mac(c["dst_mac"]), src, int(c.get("ethertype", 0x88B5)), self.vlan, int(c.get("vlan_id", 0)), int(c.get("vlan_prio", 0)))
        self.hdr_off = len(self.prefix)
        mx = max(self.sizes)
        self.batch_n = min(64, self.burst if (self.burst > 1) else max(1, int(self.pps / 1000.0)))
        if t == "tcp":
            self.batch_n = min(self.batch_n, 32)
            self.tcpbuf = bytearray(self.batch_n * mx)
        else:
            self.tx = N.BatchTx(self.batch_n, mx)
            for b in self.tx.bufs:
                b[:len(self.prefix)] = self.prefix
            self.fixed = len(set(self.sizes)) == 1

    # -- the pacing loop
    def run(self) -> None:
        c, st = self.cfg, self.stat
        try:
            off = float(c.get("start_offset_s", 0))
            while not self.stop_evt.is_set() and time.monotonic() < self.t0 + off:
                time.sleep(0.01)
            st.t_start = time.monotonic()
            end = st.t_start + float(c["duration_s"]) if c.get("duration_s") else None
            limit = int(c["packets"]) if c.get("packets") else None
            if self.cfg["type"] != "tcp" and self.echo_every:
                threading.Thread(target=self._reply_reader, daemon=True).start() if self.cfg["type"] == "udp" else None
            seq, ti = 0, 0
            nxt = time.perf_counter()
            round_n = self.burst if self.burst > 1 else self.batch_n    # a burst larger than one batch goes out as several batches back to back
            per_round = round_n / self.pps
            while not self.stop_evt.is_set():
                if end and time.monotonic() >= end:
                    break
                if limit is not None and seq >= limit:
                    break
                if self.paused.is_set():                   # switched off: stay quiet, and do not burst to catch up when it is switched on again
                    time.sleep(0.05)
                    nxt = time.perf_counter()
                    continue
                todo = round_n if limit is None else min(round_n, limit - seq)
                while todo > 0:
                    n = min(self.batch_n, todo)
                    seq, ti = self._send_batch(seq, ti, n)
                    todo -= n
                if self.gap > 0:                           # burst mode: N back to back, then the gap
                    nxt += self.gap + (self.burst / self.pps if self.pps > 1e5 else 0)
                else:
                    nxt += per_round
                d = nxt - time.perf_counter()
                if d > 0:                                  # sleep (releases the GIL); never spin: it starves the receiver threads
                    time.sleep(d)
                if d < -0.05:                              # fell behind: never burst to catch up
                    nxt = time.perf_counter()
        except OSError as e:
            self.error = f"{type(e).__name__}: {e.strerror or e}"
        except Exception as e:  # noqa
            self.error = f"{type(e).__name__}: {e}"
        finally:
            st.t_end = time.monotonic()
            self.done.set()
            if self.cfg["type"] == "tcp":
                try:
                    self.sock.close()
                except OSError:
                    pass

    def _send_batch(self, seq: int, ti: int, n: int) -> Tuple[int, int]:
        c, t, sid = self.cfg, self.cfg["type"], self.cfg["id"]
        t_tx = time.time_ns()
        sizes, ov = self.sizes, self.ov
        ns = len(sizes)
        total = 0
        if t == "tcp":
            buf, off = self.tcpbuf, 0
            for i in range(n):
                size = sizes[ti % ns]
                ti += 1
                P.pack_header(buf, off, 0, sid, size, seq + i, t_tx)
                off += size
            try:
                self.sock.sendall(memoryview(buf)[:off])
            except OSError:
                self.stat.err += n
                raise
            self.stat.pkts += n
            self.stat.bytes += off
            return seq + n, ti
        bufs = self.tx.bufs
        every = self.echo_every
        for i in range(n):
            size = sizes[ti % ns]
            ti += 1
            s = seq + i
            flags = P.FLAG_ECHO_REQ if (every and s % every == 0) else 0
            P.pack_header(bufs[i], self.hdr_off, flags, sid, size - ov, s, t_tx)
            if not self.fixed:
                self.tx.set_len(i, size if t == "l2" else size - ov)
            total += size
        if self.fixed and self.tx.iov[0].len != (sizes[0] if t == "l2" else sizes[0] - ov):
            for i in range(self.batch_n):
                self.tx.set_len(i, sizes[0] if t == "l2" else sizes[0] - ov)
        try:
            sent = self.tx.send(self.sock.fileno(), n)
        except OSError as e:
            if e.errno in (105, 11, 90):                  # ENOBUFS / EAGAIN / EMSGSIZE: count and back off
                self.stat.err += n
                time.sleep(0.001)
                return seq + n, ti
            raise
        self.stat.pkts += sent
        self.stat.bytes += total if sent == n else total * sent // n
        if sent < n:
            self.stat.err += n - sent
        return seq + n, ti

    def _reply_reader(self) -> None:                      # UDP echo replies come back on the connected sender socket
        s = self.sock
        while not self.stop_evt.is_set():
            r, _, _ = select.select([s], [], [], 0.2)
            if not r:
                continue
            try:
                d = s.recv(2048)
            except OSError:
                continue
            t_rx = time.time_ns()
            h = P.parse_header(d, 0) if len(d) >= P.HDR_LEN else None
            if h and h[0] & P.FLAG_ECHO_REPLY and h[1] == self.cfg["id"]:
                v = max(0, (t_rx - h[4]) // 1000)
                self.stat.rtt.add(v)
                self.stat.rtt_sec.add(v)

    def stop(self) -> None:
        self.stop_evt.set()


# ------------------------------------------------------------------ receiver
class RxSocket(threading.Thread):
    """One receive socket (udp port / ip protocol / ethertype / tcp listener) serving all streams that use it."""

    def __init__(self, node: "Node", kind: str, key: int, iface: str):
        super().__init__(name=f"gen-rx-{kind}-{key}", daemon=True)
        self.node, self.kind, self.key, self.iface = node, kind, key, iface
        self.stop_evt = threading.Event()
        self.sock: Optional[socket.socket] = None
        self.error: Optional[str] = None
        self.own_mac = b""
        self.open()

    def open(self) -> None:
        k = self.kind
        try:
            if k == "udp":
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                big_rcvbuf(s)
                s.bind(("", self.key))
            elif k == "ip":
                s = socket.socket(socket.AF_INET, socket.SOCK_RAW, self.key)
                big_rcvbuf(s)
            elif k == "l2":
                s = _l2_socket(self.iface, self.key, True)
                self.own_mac = N.iface_mac(self.iface)
            else:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("", self.key))
                s.listen(8)
        except PermissionError:
            raise GenError("Raw streams need extra network permission (CAP_NET_RAW / port permission). This copy of the app was started without it; the full install (sudo scripts/install-pi.sh) grants it to the service.")
        except GenError:
            raise
        except OSError as e:
            raise GenError(f"Cannot listen for {k} {self.key}: {e.strerror}")
        self.sock = s
        self.rx = N.BatchRx(64, 2048 if k != "l2" else 2048, want_addr=(k in ("udp", "ip")))

    def run(self) -> None:
        try:
            if self.kind == "tcp":
                self._tcp_loop()
            else:
                self._dgram_loop()
        except Exception as e:  # noqa
            if not self.stop_evt.is_set():             # closing the socket at the end of a run is expected
                self.error = f"{type(e).__name__}: {e}"

    def _dgram_loop(self) -> None:
        fd, rx, node = self.sock.fileno(), self.rx, self.node
        mv = rx.mv
        slot = rx.slot
        unpack = P.HDR.unpack_from
        sinks, txs = node.rxs, node.txs
        kind = self.kind
        extra = {"udp": P.ETH_HDR + P.IP_HDR + P.UDP_HDR, "ip": P.ETH_HDR, "l2": 0}[kind]    # count whole Ethernet frames, like the sender
        while not self.stop_evt.is_set():
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                continue
            n = rx.recv(fd)
            if not n:
                continue
            t_rx = time.time_ns()
            now = time.monotonic()
            for i in range(n):
                ln = rx.length(i)                           # real length, also for a jumbo frame cut to the slot (header still fits)
                base = i * slot
                if kind == "ip":
                    off = (mv[base] & 15) * 4
                elif kind == "l2":
                    off = 18 if (mv[base + 12] == 0x81 and mv[base + 13] == 0x00) else 14
                else:
                    off = 0
                if ln < off + P.HDR_LEN:
                    continue
                magic, ver, flags, sid, _p, length, seq, t_tx = unpack(mv, base + off)
                if magic != P.MAGIC or ver != P.VER:
                    continue
                if flags & P.FLAG_ECHO_REPLY:
                    tx = txs.get(sid)
                    if tx is not None:
                        v = max(0, (t_rx - t_tx) // 1000)
                        tx.rtt.add(v)
                        tx.rtt_sec.add(v)
                    continue
                st = sinks.get(sid)
                if st is None:
                    continue
                nb = ln + extra
                if kind == "l2" and off == 14 and st.cfg.get("vlan"):
                    nb += 4                                 # the kernel already took the VLAN tag off the frame
                st.on_packet(seq, t_tx, t_rx, nb, now)
                if flags & P.FLAG_ECHO_REQ:
                    self._echo(i, base, off, sid, seq, t_tx)

    def _echo(self, i: int, base: int, off: int, sid: int, seq: int, t_tx: int) -> None:
        try:
            hdr = bytearray(P.HDR_LEN)
            P.pack_header(hdr, 0, P.FLAG_ECHO_REPLY, sid, P.HDR_LEN, seq, t_tx)
            if self.kind == "udp":
                self.sock.sendto(bytes(hdr), (self.rx.src_ipv4(i), struct.unpack_from(">H", self.rx.addr, i * 128 + 2)[0]))
            elif self.kind == "ip":
                self.sock.sendto(bytes(hdr), (self.rx.src_ipv4(i), 0))
            elif self.kind == "l2":
                f = bytes(self.rx.mv[base + 6:base + 12]) + self.own_mac + bytes(self.rx.mv[base + 12:base + off]) + bytes(hdr)
                self.sock.send(f.ljust(60, b"\0"))
        except OSError:
            pass

    def _tcp_loop(self) -> None:
        ls = self.sock
        while not self.stop_evt.is_set():
            r, _, _ = select.select([ls], [], [], 0.2)
            if r:
                try:
                    c, _ = ls.accept()
                except OSError:
                    continue
                c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                threading.Thread(target=self._tcp_conn, args=(c,), daemon=True).start()

    def _tcp_conn(self, c: socket.socket) -> None:
        buf = bytearray()
        sinks = self.node.rxs
        c.settimeout(0.2)
        while not self.stop_evt.is_set():
            try:
                d = c.recv(262144)
            except socket.timeout:
                continue
            except OSError:
                break
            if not d:
                break
            t_rx = time.time_ns()
            now = time.monotonic()
            buf += d
            pos = 0
            while len(buf) - pos >= P.HDR_LEN:
                magic, ver, flags, sid, _p, length, seq, t_tx = P.HDR.unpack_from(buf, pos)
                if magic != P.MAGIC or length < P.HDR_LEN:
                    buf.clear()
                    pos = 0
                    break
                if len(buf) - pos < length:
                    break
                st = sinks.get(sid)
                if st is not None:
                    st.on_packet(seq, t_tx, t_rx, length, now)
                pos += length
            if pos:
                del buf[:pos]
        c.close()

    def stop(self) -> None:
        self.stop_evt.set()
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------ node
class Node:
    """Everything one Pi does for a generator run: senders, receivers, per-second rows."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        old_stop, old_tick = getattr(self, "tick_stop", None), getattr(self, "tick_thread", None)
        if old_stop is not None:                     # a ticker left from a failed start must not live on into the next run
            old_stop.set()
        if old_tick is not None and old_tick is not threading.current_thread():
            old_tick.join(timeout=2)
        self.senders: List[Sender] = []
        self.rxsockets: Dict[Tuple[str, int], RxSocket] = {}
        self.rxs: Dict[int, RxStat] = {}
        self.txs: Dict[int, TxStat] = {}
        self.rows: List[dict] = []
        self.t0: Optional[float] = None
        self.tick_thread: Optional[threading.Thread] = None
        self.tick_stop = threading.Event()
        self.active = False
        self.iface = ""

    def busy(self) -> bool:
        return self.active

    # -- start / stop
    def _begin(self, iface: str) -> str:
        """First call of a run: reset state. Later calls (rx_start then tx_start) join the same run."""
        if not self.active and not self.rxsockets and not self.senders:
            self.reset()
        sys.setswitchinterval(0.0005)                # let receiver threads in quickly (latency accuracy)
        if self.t0 is None:
            self.t0 = time.monotonic()
        if not self.iface or self.iface == "auto":   # "auto" from a UDP-only rx_start must not stick when a raw stream names the port later
            self.iface = iface
        return self.iface

    def _need_iface(self, streams: List[dict], iface: str) -> str:
        return resolve_iface(iface) if (any(s["type"] == "l2" for s in streams) or (iface and iface != "auto")) else (iface or "auto")

    def _activate(self) -> None:
        if not self.active:
            self.active = True
            self.tick_stop.clear()
            self.tick_thread = threading.Thread(target=self._tick, args=(self.tick_stop,), name="gen-tick", daemon=True)
            self.tick_thread.start()

    def rx_start(self, rx_streams: List[dict], iface: str = "auto") -> None:
        with self.lock:
            if self.active and self.senders:
                raise GenError("This Pi is already running a generator test.")
            self._begin(self._need_iface(rx_streams, iface))
            new = []
            try:
                for s in rx_streams:
                    self.rxs[s["id"]] = RxStat(s)
                    self.rxs[s["id"]].drop_every = _test_drop(s["id"])
                    key = ("l2", int(s.get("ethertype", 0x88B5))) if s["type"] == "l2" else ("ip", int(s.get("ip_proto", 253))) if s["type"] == "ip" else (s["type"], int(s["dst_port"]))
                    if key not in self.rxsockets:
                        self.rxsockets[key] = r = RxSocket(self, key[0], key[1], self.iface)
                        new.append(r)
            except Exception:
                self._close_all()
                self.reset()
                raise
            for r in new:
                r.start()
            self._activate()

    def tx_start(self, tx_streams: List[dict], iface: str = "auto", start_in_s: float = 0.0) -> None:
        with self.lock:
            more = bool(self.senders) and self.senders_done()      # Throughput in steps: the next step of the SAME run starts when the senders of the last one are done
            if self.senders and not more:
                raise GenError("This Pi is already sending a generator test.")
            if more and any(s["id"] in self.txs for s in tx_streams):
                raise GenError("This Pi already sent these streams in this test.")
            self._begin(self._need_iface(tx_streams, iface))
            new_rx, new_tx = [], []
            try:
                for s in tx_streams:
                    self.txs[s["id"]] = TxStat(s)
                    if s.get("rtt_probe_pps") and s["type"] in ("l2", "ip"):      # raw echo replies come back as raw frames
                        key = ("l2", int(s.get("ethertype", 0x88B5))) if s["type"] == "l2" else ("ip", int(s.get("ip_proto", 253)))
                        if key not in self.rxsockets:
                            self.rxsockets[key] = r = RxSocket(self, key[0], key[1], self.iface)
                            new_rx.append(r)
                t_start = time.monotonic() + start_in_s
                for s in tx_streams:
                    new_tx.append(Sender(s, self.iface, self.txs[s["id"]], t_start))
            except Exception:
                for snd in new_tx:
                    snd.stop()
                    if snd.sock:
                        try:
                            snd.sock.close()
                        except OSError:
                            pass
                self._close_all()
                self.reset()
                raise
            for snd in self.senders:                 # the senders of the step before are done: their reply readers end, their numbers stay in self.txs
                snd.stop()
            self.senders = self.senders + new_tx
            for r in new_rx:
                r.start()
            for snd in new_tx:
                snd.start()
            self._activate()

    def start(self, rx_streams: List[dict], tx_streams: List[dict], iface: str = "auto", start_in_s: float = 0.0) -> None:
        """Convenience (tests): receivers first, then senders."""
        self.rx_start(rx_streams, iface)
        self.tx_start(tx_streams, iface, start_in_s)

    def _close_all(self) -> None:
        for snd in self.senders:
            snd.stop()
            if snd.sock:
                try:
                    snd.sock.close()
                except OSError:
                    pass
        for r in self.rxsockets.values():
            r.stop()

    def senders_done(self) -> bool:
        return bool(self.senders) and all(s.done.is_set() for s in self.senders)

    def errors(self) -> List[str]:
        return [e for e in [s.error for s in self.senders] + [r.error for r in self.rxsockets.values()] if e]

    def set_stream_on(self, stream_id: int, on: bool) -> bool:
        """Switch one stream of this Pi's senders off or on during the run (False = this Pi does not send that stream)."""
        with self.lock:
            for s in self.senders:
                if s.cfg["id"] == stream_id:
                    (s.paused.clear if on else s.paused.set)()
                    return True
        return False

    def stop_senders(self) -> None:
        """Stop only the senders; the receivers keep listening until stop()."""
        with self.lock:
            for s in self.senders:
                s.stop()
            for s in self.senders:
                s.join(timeout=3)

    def stop(self) -> dict:
        """Stop everything; return the final per-stream statistics."""
        with self.lock:
            for s in self.senders:
                s.stop()
            for s in self.senders:
                s.join(timeout=3)
            self._snapshot()
            for r in self.rxsockets.values():
                r.stop()
            for r in self.rxsockets.values():
                r.join(timeout=2)
            self.tick_stop.set()
            if self.tick_thread:
                self.tick_thread.join(timeout=2)
            for s in self.senders:
                if s.sock:
                    try:
                        s.sock.close()
                    except OSError:
                        pass
            res = {"tx": {str(k): v.result() for k, v in self.txs.items()}, "rx": {str(k): v.result() for k, v in self.rxs.items()},
                   "errors": self.errors(), "rows": list(self.rows)}
            self.reset()
            return res

    # -- per-second rows
    def _tick(self, stop_evt: threading.Event) -> None:     # its own event: reset() swaps self.tick_stop for the next run
        while not stop_evt.wait(1.0):
            self._snapshot()

    def _snapshot(self) -> None:
        if self.t0 is None:
            return
        t = int(max(0.0, time.monotonic() - self.t0) + 0.5)
        for sid, tx in self.txs.items():
            lp, lb = tx._last
            row = {"t": t, "stream": sid, "dir": "tx", "pkts": tx.pkts - lp, "bytes": tx.bytes - lb, "rtt_avg_us": (tx.rtt_sec.sum // tx.rtt_sec.n) if tx.rtt_sec.n else None,
                   "lost_cum": None, "lat_avg_us": None, "lat_p95_us": None, "lat_max_us": None, "jitter_us": None}
            tx._last = (tx.pkts, tx.bytes)
            tx.rtt_sec = P.Hist()
            self.rows.append(row)
        for sid, rx in self.rxs.items():
            lp, lb = rx._last
            sec, rx.sec = rx.sec, P.Hist()
            row = {"t": t, "stream": sid, "dir": "rx", "pkts": rx.pkts - lp, "bytes": rx.bytes - lb, "rtt_avg_us": None, "lost_cum": rx.tr.lost_so_far,
                   "lat_avg_us": (sec.sum // sec.n) if sec.n else None, "lat_p95_us": sec.pct(95), "lat_max_us": sec.max, "jitter_us": round(rx.J / 1000.0, 1) if rx.prev_transit is not None else None}
            rx._last = (rx.pkts, rx.bytes)
            self.rows.append(row)

    def live(self, since: int = 0) -> dict:
        return {"active": self.active, "rows": self.rows[since:], "n_rows": len(self.rows), "done": self.senders_done(), "errors": self.errors(),
                "tx": {str(k): {"pkts": v.pkts, "bytes": v.bytes, "err": v.err} for k, v in self.txs.items()},
                "rx": {str(k): {"pkts": v.pkts, "bytes": v.bytes, "unique": v.tr.count, "lost": v.tr.lost_so_far, **v.live_pcts()} for k, v in self.rxs.items()}}
