"""GOOSE publisher/subscriber transport: Layer-2 GOOSE (EtherType 0x88B8) and Layer-3 multicast (R-GOOSE style, UDP).

Docs/SPEC.md 5.5. No tunnelling: the cellular router builds GRE tunnels; the Pi just emits correct GOOSE.

Test model: a trip 'event' = a GOOSE state change (stNum = seq + 1) published with the IEC 61850-8-1 retransmission
profile (T1, doubling up to T0). Pi B's subscriber returns each new event to the Responder which republishes it
('mirror') in its own GOOSE, so Pi A can time the event round trip. Dataset layout:
    N booleans (the 8 payload bits) | control int (type<<4|flags) | B clock offset us | rx-info | B receive time ns
"""
from __future__ import annotations

import fcntl
import select
import socket
import struct
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import frames as F
from .transports import Transport, Rx, mono, real

ETH_GOOSE = 0x88B8
ETH_VLAN = 0x8100
SPDU_GOOSE = 0xA1                      # R-GOOSE session identifier used by libiec61850 for GOOSE payload
PACKET_OUTGOING = 4


# ------------------------------------------------------------------ BER
def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag: int, val: bytes) -> bytes:
    return bytes([tag]) + _len(len(val)) + val


def enc_int(v: int) -> bytes:
    n = max(1, (v.bit_length() + 8) // 8) if v >= 0 else max(1, ((-v - 1).bit_length() + 8) // 8)
    return v.to_bytes(n, "big", signed=True)


def enc_uint(v: int) -> bytes:                  # minimal unsigned encoded as INTEGER (leading 0 if high bit set)
    return enc_int(v)


def read_tlv(b: bytes, i: int) -> Tuple[int, bytes, int]:
    tag = b[i]
    ln = b[i + 1]
    j = i + 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(b[j:j + k], "big")
        j += k
    if j + ln > len(b):
        raise ValueError("truncated BER")
    return tag, b[j:j + ln], j + ln


def utc_enc(ns: int) -> bytes:
    sec, frac_ns = divmod(ns, 1_000_000_000)
    frac = (frac_ns << 24) // 1_000_000_000
    return struct.pack(">I", sec) + frac.to_bytes(3, "big") + b"\x8A"


def utc_dec(b: bytes) -> int:
    sec = struct.unpack(">I", b[:4])[0]
    frac = int.from_bytes(b[4:7], "big")
    return sec * 1_000_000_000 + (frac * 1_000_000_000 >> 24)


def pdu_encode(p: dict) -> bytes:
    data = b"".join(tlv(0x83, b"\xFF" if v else b"\x00") if k == "b" else tlv(0x85, enc_int(v)) for k, v in p["data"])
    body = (tlv(0x80, p["gocb"].encode()) + tlv(0x81, enc_uint(p["tal_ms"])) + tlv(0x82, p["dat_set"].encode()) +
            tlv(0x83, p["go_id"].encode()) + tlv(0x84, utc_enc(p["t_ns"])) + tlv(0x85, enc_uint(p["st"])) +
            tlv(0x86, enc_uint(p["sq"])) + tlv(0x87, b"\xFF" if p["sim"] else b"\x00") + tlv(0x88, enc_uint(p["conf_rev"])) +
            tlv(0x89, b"\xFF" if p["nds"] else b"\x00") + tlv(0x8A, enc_uint(len(p["data"]))) + tlv(0xAB, data))
    return tlv(0x61, body)


def pdu_decode(b: bytes) -> dict:
    tag, body, _ = read_tlv(b, 0)
    if tag != 0x61:
        raise ValueError("not a goosePdu")
    out: dict = {"data": []}
    i = 0
    names = {0x80: "gocb", 0x81: "tal_ms", 0x82: "dat_set", 0x83: "go_id", 0x85: "st", 0x86: "sq", 0x88: "conf_rev"}
    while i < len(body):
        t, v, i = read_tlv(body, i)
        if t in (0x80, 0x82, 0x83):
            out[names[t]] = v.decode(errors="replace")
        elif t in (0x81, 0x85, 0x86, 0x88, 0x8A):
            out[names.get(t, "n_entries")] = int.from_bytes(v, "big", signed=True)
        elif t == 0x84:
            out["t_ns"] = utc_dec(v)
        elif t == 0x87:
            out["sim"] = v != b"\x00"
        elif t == 0x89:
            out["nds"] = v != b"\x00"
        elif t == 0xAB:
            j = 0
            while j < len(v):
                dt, dv, j = read_tlv(v, j)
                out["data"].append(("b", dv != b"\x00") if dt == 0x83 else ("i", int.from_bytes(dv, "big", signed=True)))
    for k in ("gocb", "st", "sq", "tal_ms", "t_ns", "conf_rev"):
        if k not in out:
            raise ValueError(f"missing {k}")
    return out


# ------------------------------------------------------------------ L2 / L3 envelopes
def mac_b(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def l2_build(dst: str, src: bytes, vlan: bool, vid: int, prio: int, appid: int, sim: bool, pdu: bytes) -> bytes:
    h = mac_b(dst) + src
    if vlan:
        h += struct.pack(">HH", ETH_VLAN, (prio << 13) | vid)
    return h + struct.pack(">HHHHH", ETH_GOOSE, appid, 8 + len(pdu), 0x8000 if sim else 0, 0) + pdu


def l2_parse(frame: bytes, vlan_tci: Optional[int] = None) -> Optional[dict]:
    if len(frame) < 22:
        return None
    dst, src = frame[:6], frame[6:12]
    et, i, tci = struct.unpack(">H", frame[12:14])[0], 14, vlan_tci
    if et == ETH_VLAN:
        tci = struct.unpack(">H", frame[14:16])[0]
        et, i = struct.unpack(">H", frame[16:18])[0], 18
    if et != ETH_GOOSE or len(frame) < i + 8:
        return None
    appid, length, res1, _ = struct.unpack(">HHHH", frame[i:i + 8])
    pdu = frame[i + 8:i + length]
    return {"dst": dst, "src": src, "vlan": tci is not None, "prio": None if tci is None else tci >> 13,
            "vid": None if tci is None else tci & 0xFFF, "appid": appid, "sim": bool(res1 & 0x8000), "pdu": pdu, "size": len(frame)}


CLTP_UD = b"\x01\x40"                       # ISO 8602 connectionless transport header (length indicator 1, TPDU type UD): R-GOOSE rides in it on UDP port 102


def l3_build(spdu_num: int, appid: int, sim: bool, pdu: bytes) -> bytes:
    """R-GOOSE UDP payload (IEC 61850-90-5): CLTP header, session header (fixed binary layout, no security), payload = one GOOSE APDU.
    Layout as dissected by Wireshark's R-GOOSE dissector (checked against tshark 4.0 in scripts/e2e_sniff.py)."""
    apdu = bytes([0x81, 1 if sim else 0]) + struct.pack(">HH", appid, len(pdu)) + pdu          # tag, simulation, APPID, APDU length
    user = struct.pack(">I", len(apdu)) + apdu                                                  # payload length + APDU
    sec = struct.pack(">IHI", 0, 0, 0) + b"\x00"                                                # time of current key, of next key, key id, IV length 0 (no security)
    common = struct.pack(">II", 0, spdu_num & 0xFFFFFFFF) + b"\x00\x01"                         # SPDU length (filled below), SPDU number, version 1
    hdr_body = b"\x80" + bytes([len(common)]) + common + sec
    spdu_len = 2 + len(hdr_body) + len(user)
    common = struct.pack(">II", spdu_len, spdu_num & 0xFFFFFFFF) + b"\x00\x01"
    hdr_body = b"\x80" + bytes([len(common)]) + common + sec
    return CLTP_UD + bytes([SPDU_GOOSE, len(hdr_body)]) + hdr_body + user


def l3_parse(d: bytes) -> Optional[dict]:
    try:
        i = 2 if d[:2] == CLTP_UD else 0
        if d[i] != SPDU_GOOSE or d[i + 2] != 0x80:
            return None
        hdr_len = d[i + 1]
        spdu_len, spdu = struct.unpack(">II", d[i + 4:i + 12])
        iv_len = d[i + 24]                                    # IV length: last fixed byte of the security information
        if hdr_len != 23 + iv_len:
            return None
        p = i + 2 + hdr_len
        plen = struct.unpack(">I", d[p:p + 4])[0]
        if d[p + 4] != 0x81 or plen < 6 or p + 4 + plen > len(d):
            return None
        sim, appid, alen = d[p + 5], *struct.unpack(">HH", d[p + 6:p + 10])
        return {"appid": appid, "sim": bool(sim), "pdu": d[p + 10:p + 10 + alen], "spdu": spdu, "size": len(d)}
    except (ValueError, IndexError, struct.error):
        return None


# ------------------------------------------------------------------ system helpers
def raw_socket_ok() -> dict:
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_GOOSE))
        s.close()
        return {"ok": True, "reason": ""}
    except PermissionError:
        return {"ok": False, "reason": "Needs permission to use raw network sockets (CAP_NET_RAW). This copy of the app was started without it; the full install (sudo scripts/install-pi.sh) grants it to the service."}
    except OSError as e:
        return {"ok": False, "reason": f"Raw sockets unavailable: {e.strerror}"}


def iface_ipv4(name: str) -> Optional[str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))[20:24])
    except OSError:
        return None
    finally:
        s.close()


def iface_mac(name: str) -> bytes:
    with open(f"/sys/class/net/{name}/address") as f:
        return mac_b(f.read().strip())


def pick_iface(name: str) -> str:
    if name and name != "auto":
        return name
    from . import system
    for i in system.interfaces():
        if i["wired"] and i["up"]:
            return i["name"]
    raise OSError("No wired network port found.")


def _attach_goose_bpf(sock: socket.socket) -> None:
    """Classic BPF: accept EtherType 0x88B8, or 0x8100 followed by 0x88B8."""
    import ctypes
    prog = [(0x28, 0, 0, 12), (0x15, 3, 0, 0x88B8), (0x15, 0, 3, 0x8100), (0x28, 0, 0, 16),
            (0x15, 0, 1, 0x88B8), (0x06, 0, 0, 0xFFFF), (0x06, 0, 0, 0)]
    buf = ctypes.create_string_buffer(b"".join(struct.pack("HBBI", *i) for i in prog))
    try:
        sock.setsockopt(socket.SOL_SOCKET, 26, struct.pack("@HP", len(prog), ctypes.addressof(buf)))   # SO_ATTACH_FILTER
    except OSError:
        pass          # user-space filtering still applies


# ------------------------------------------------------------------ transport
class GooseTransport(Transport):
    def __init__(self, role: str, kind: str, cfg: dict):
        super().__init__()
        self.role, self.kind, self.cfg = role, kind, cfg
        self.l2 = kind == "goose_l2"
        me, peer = ("a", "b") if role == "tester" else ("b", "a")
        self.me, self.peer = me, peer
        self.my = {"appid": cfg[f"appid_{me}"], "gocb": cfg[f"gocb_ref_{me}"], "dat_set": cfg[f"dat_set_{me}"], "go_id": cfg[f"go_id_{me}"]}
        self.pr = {"appid": cfg[f"appid_{peer}"], "gocb": cfg[f"gocb_ref_{peer}"], "go_id": cfg[f"go_id_{peer}"]}
        self.iface = ""
        self.socks: List[socket.socket] = []
        self.tx_sock: Optional[socket.socket] = None
        self.cond = threading.Condition()
        self.cur: Optional[dict] = None          # current published state
        self.next_due = 0.0
        self.interval = 0.0
        self.stop_evt = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.spdu = 0
        self.dst_peer: Optional[Tuple[str, int]] = None   # L3 unicast fallback: learned/configured
        self.stats = {"events_tx": 0, "events_rx": 0, "retx_rx": 0, "sq_gaps": 0, "duplicates": 0, "tal_expiries": 0,
                      "tal_total_ms": 0.0, "frame_size": 0, "vlan_seen": None, "prio_seen": None, "foreign_frames": 0,
                      "st_jumps": 0, "conf_rev_mismatch": 0, "b_saw": None, "frames_tx": 0, "frames_rx": 0}
        self.last_st: Optional[int] = None
        self.last_sq = -1
        self.last_rx = 0.0
        self.last_tal_ms = 0
        self.expired = False
        self.exp_at = 0.0
        self.hello_n = 0
        self.auxdata_ok = True
        self.nonce = int.from_bytes(__import__("os").urandom(2), "big")
        self.rxinfo = 0                      # what we last received, reported back in our mirror
        self.t2_last = 0

    # -------------------------------------------------------------- setup
    def describe(self):
        return f"GOOSE {'L2' if self.l2 else 'L3 multicast'} on {self.iface or self.cfg['interface']}"

    def open(self):
        c = self.cfg
        self.iface = pick_iface(c["interface"])
        if self.l2:
            self.src = iface_mac(self.iface)
            ifindex = socket.if_nametoindex(self.iface)
            # One ETH_P_ALL socket (so the VLAN tag is still visible: the kernel strips priority-tagged
            # VID-0 tags before protocol-specific sockets see them) with a BPF filter for EtherType 0x88B8.
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
            s.bind((self.iface, 3))
            try:       # PACKET_AUXDATA -> VLAN tci when the kernel has taken the tag off the frame
                s.setsockopt(263, 8, 1)
                self.auxdata_ok = True
            except OSError:        # e.g. qemu-user emulation: VLAN presence then cannot be known
                self.auxdata_ok = False
            _attach_goose_bpf(s)
            self.socks.append(s)
            # join the destination multicast MAC so real NICs do not filter it out (PACKET_ADD_MEMBERSHIP)
            mreq = struct.pack("iHH8s", ifindex, 0, 6, mac_b(c["dest_mac"]) + b"\0\0")
            try:
                self.socks[0].setsockopt(263, 1, mreq)
            except OSError:
                pass
            self.tx_sock = self.socks[0]
            self.dst_mac = c["dest_mac"]
        else:
            ip = iface_ipv4(self.iface)
            if not ip:
                raise OSError(f"Port {self.iface} has no IPv4 address.")
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", c["udp_port"]))
            sub_group = c["group_b"] if self.role == "tester" else c["group_a"]
            self.pub_group = c["group_a"] if self.role == "tester" else c["group_b"]
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(sub_group) + socket.inet_aton(ip))
            t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            t.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            t.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, c["ttl"])
            t.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
            t.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, (c["dscp"] & 0x3F) << 2)
            self.socks.append(s)
            self.tx_sock = t
            if c.get("unicast_peer"):        # non-standard fallback: both Pis are given the other's address
                self.dst_peer = (c["unicast_peer"], c["udp_port"])
        self.thread = threading.Thread(target=self._publisher, name="goose-pub", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_evt.set()
        with self.cond:
            self.cond.notify_all()
        if self.thread:
            self.thread.join(timeout=2)
        for s in self.socks + ([self.tx_sock] if self.tx_sock and self.tx_sock not in self.socks else []):
            try:
                s.close()
            except OSError:
                pass
        self.socks, self.tx_sock = [], None

    # -------------------------------------------------------------- publish
    def _entries(self, f: F.Frame) -> List[tuple]:
        n = self.cfg["num_entries"]
        bits = [("b", bool((f.bits >> i) & 1)) for i in range(min(8, n))] + [("b", False)] * max(0, n - 8)
        ctl = (f.type << 4) | (f.flags & 0xF)
        return bits + [("i", ctl), ("i", f.offset_us), ("i", self.rxinfo), ("i", f.t2)]

    def write(self, f: F.Frame) -> None:
        c = self.cfg
        if f.type in (F.T_HELLO, F.T_HELLO_ACK):
            self.hello_n += 1          # unique per transport instance so a new run is never mistaken for a retransmission
            st = 0xFFFF0000 + ((self.nonce + self.hello_n) & 0xFFFF)
        else:
            st = (f.seq + 1) & 0xFFFFFFFF
        t_ns = f.t3 or f.t1 or real()
        with self.cond:
            self.cur = {"frame": f, "st": st, "sq": 0, "t_ns": t_ns}
            self.interval = c["t1_ms"] / 1000.0
            self.stats["events_tx"] += 1
            self._send_current()
            self.next_due = time.monotonic() + self.interval
            self.cond.notify()

    def _send_current(self) -> None:           # caller holds self.cond
        c, cur = self.cfg, self.cur
        tal = max(1, int(round(self.interval * 2000)))      # TAL = 2 x next retransmission interval
        pdu = pdu_encode({"gocb": self.my["gocb"], "tal_ms": tal, "dat_set": self.my["dat_set"], "go_id": self.my["go_id"],
                          "t_ns": cur["t_ns"], "st": cur["st"], "sq": cur["sq"], "sim": c["simulation"],
                          "conf_rev": c["conf_rev"], "nds": c["nds_com"], "data": self._entries(cur["frame"])})
        if self.l2:
            data = l2_build(self.dst_mac, self.src, c["vlan"], c["vlan_id"], c["vlan_prio"], self.my["appid"], c["simulation"], pdu)
            self.tx_sock.send(data)
        else:
            self.spdu += 1
            data = l3_build(self.spdu, self.my["appid"], c["simulation"], pdu)
            dst = self.dst_peer or (self.pub_group, c["udp_port"])
            self.tx_sock.sendto(data, dst)
        self.stats["frame_size"] = len(data)
        self.stats["frames_tx"] += 1
        cur["sq"] += 1

    def _publisher(self):
        c = self.cfg
        while not self.stop_evt.is_set():
            with self.cond:
                if self.cur is None:
                    self.cond.wait(0.2)
                    continue
                wait = self.next_due - time.monotonic()
                if wait > 0:
                    self.cond.wait(min(wait, 0.2))
                    continue
                try:
                    self._send_current()
                except OSError:
                    pass
                if c["fixed_interval"]:
                    self.interval = c["t1_ms"] / 1000.0
                else:
                    self.interval = min(self.interval * c["multiplier"], c["t0_ms"] / 1000.0)
                self.next_due = time.monotonic() + self.interval

    # -------------------------------------------------------------- subscribe
    def read(self, timeout: float) -> List[Rx]:
        out: List[Rx] = []
        self._tal_check()
        if not self.socks:
            time.sleep(max(0, timeout))
            return out
        r, _, _ = select.select(self.socks, [], [], max(0.0, min(timeout, 0.1)))
        for s in r:
            try:
                if self.l2:
                    data, anc, _fl, addr = s.recvmsg(2048, 256)
                    t_m, t_r = mono(), real()
                    if addr[2] == PACKET_OUTGOING:
                        continue
                    tci = None
                    for lvl, typ, cd in anc:
                        if lvl == 263 and typ == 8 and len(cd) >= 20:
                            status, = struct.unpack("I", cd[:4])
                            if status & 0x10:
                                tci = struct.unpack("H", cd[16:18])[0]
                    env = l2_parse(data, tci)
                    if env is None or env["src"] == self.src:
                        continue
                    if mac_b(self.cfg["dest_mac"]) != env["dst"]:
                        self.stats["foreign_frames"] += 1
                        continue
                    if self.cfg["check_vlan"] and not env["vlan"]:
                        self.stats["foreign_frames"] += 1
                        continue
                else:
                    data, addr = s.recvfrom(2048)
                    t_m, t_r = mono(), real()
                    env = l3_parse(data)
                    if env is None:
                        continue
                self.stats["frames_rx"] += 1
                fr = self._rx_pdu(env, t_m, t_r)
                if fr:
                    out.append(fr)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                continue
        self._tal_check()
        return out

    def _rx_pdu(self, env: dict, t_m: int, t_r: int) -> Optional[Rx]:
        try:
            p = pdu_decode(env["pdu"])
        except ValueError:
            self.corrupted += 1
            return None
        if env["appid"] != self.pr["appid"] or p["gocb"] != self.pr["gocb"] or p.get("go_id") != self.pr["go_id"]:
            self.stats["foreign_frames"] += 1
            return None
        if self.cfg["check_conf_rev"] and p["conf_rev"] != self.cfg["conf_rev"]:
            self.stats["conf_rev_mismatch"] += 1
            return None
        st = self.stats
        if self.l2 and env.get("vlan") is not None and (env["vlan"] or self.auxdata_ok):
            st["vlan_seen"], st["prio_seen"] = env["vlan"], env["prio"]       # else: unknown (reported as '–')
        # TAL bookkeeping: frame arrived
        if self.expired:
            st["tal_total_ms"] += (time.monotonic() - self.exp_at) * 1000
            self.expired = False
        self.last_rx, self.last_tal_ms = time.monotonic(), p["tal_ms"]
        if p["st"] == self.last_st:
            if p["sq"] <= self.last_sq:
                st["duplicates"] += 1
            else:
                if p["sq"] > self.last_sq + 1:
                    st["sq_gaps"] += p["sq"] - self.last_sq - 1
                st["retx_rx"] += 1
                self.last_sq = p["sq"]
            return None
        # a new event
        if self.last_st is not None and p["st"] != (self.last_st + 1) & 0xFFFFFFFF and p["st"] < 0xFFFF0000 and self.last_st < 0xFFFF0000:
            st["st_jumps"] += 1
        self.last_st, self.last_sq = p["st"], p["sq"]
        st["events_rx"] += 1
        d = p["data"]
        n = self.cfg["num_entries"]
        try:
            bits = sum(1 << i for i, (k, v) in enumerate(d[:min(8, n)]) if v)
            ints = [v for k, v in d[n:] if k == "i"]
            ctl, off, rxinfo, t2 = (ints + [0, 0, 0, 0])[:4]
        except Exception:
            return None
        typ, flags = (ctl >> 4) & 0xF, ctl & 0xF
        seq = 0 if p["st"] >= 0xFFFF0000 else p["st"] - 1
        # remember what arrived for the mirror's 'rx-info' (vlan, prio, size)
        self.rxinfo = ((1 if env.get("vlan") else 0) << 15) | ((env.get("prio") or 0) << 12) | min(env["size"], 0xFFF)
        if typ == F.T_ECHO and rxinfo:
            st["b_saw"] = {"vlan": bool(rxinfo >> 15 & 1), "prio": rxinfo >> 12 & 7, "size": rxinfo & 0xFFF}
        # the sender's event time rides in 't'; for TRIP it is A's send time, for ECHO it is B's send time
        f = F.Frame(type=typ, seq=seq & 0xFFFFFFFF, bits=bits, flags=flags, t1=p["t_ns"] if typ == F.T_TRIP else 0,
                    t2=t2, t3=p["t_ns"] if typ != F.T_TRIP else 0, offset_us=off, extended=True)
        if typ == F.T_TRIP:
            self.t2_last = t_r
        return (f, t_m, t_r)

    def _tal_check(self):
        if self.last_st is not None and not self.expired and time.monotonic() > self.last_rx + self.last_tal_ms / 1000.0:
            self.expired, self.exp_at = True, self.last_rx + self.last_tal_ms / 1000.0
            self.stats["tal_expiries"] += 1

    def goose_stats(self) -> dict:
        s = dict(self.stats)
        s["tal_total_ms"] = round(s["tal_total_ms"], 1)
        s["mode"] = "L2" if self.l2 else "L3 multicast"
        s["interface"] = self.iface
        s["retransmission"] = {"t1_ms": self.cfg["t1_ms"], "t0_ms": self.cfg["t0_ms"], "multiplier": self.cfg["multiplier"], "fixed": self.cfg["fixed_interval"]}
        s["too_big_for_gre"] = s["frame_size"] > 1400
        return s
