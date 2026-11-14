"""Synthetic Ethernet/IPv4/UDP|TCP frames so Wireshark decodes serial DNP3 / Modbus RTU captures with no set-up."""
from __future__ import annotations

import struct
from typing import Dict, Tuple


def csum(b: bytes) -> int:
    if len(b) % 2:
        b += b"\x00"
    s = sum(struct.unpack(f">{len(b) // 2}H", b))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def ip4(src: str, dst: str, proto: int, payload: bytes, ident: int = 0) -> bytes:
    sa, da = bytes(int(x) for x in src.split(".")), bytes(int(x) for x in dst.split("."))
    h = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident & 0xFFFF, 0x4000, 64, proto, 0, sa, da)
    h = h[:10] + struct.pack(">H", csum(h)) + h[12:]
    return h + payload


def eth(dst: bytes, src: bytes, payload: bytes) -> bytes:
    return dst + src + b"\x08\x00" + payload


def udp_frame(smac: bytes, dmac: bytes, sip: str, dip: str, sport: int, dport: int, data: bytes, ident: int = 0) -> bytes:
    u = struct.pack(">HHHH", sport, dport, 8 + len(data), 0) + data               # UDP checksum 0 = none (valid for IPv4)
    return eth(dmac, smac, ip4(sip, dip, 17, u, ident))


def tcp_frame(smac: bytes, dmac: bytes, sip: str, dip: str, sport: int, dport: int, seq: int, ack: int, data: bytes, ident: int = 0) -> bytes:
    t = struct.pack(">HHIIBBHHH", sport, dport, seq & 0xFFFFFFFF, ack & 0xFFFFFFFF, 5 << 4, 0x18, 65535, 0, 0) + data     # PSH|ACK
    pseudo = bytes(int(x) for x in sip.split(".")) + bytes(int(x) for x in dip.split(".")) + struct.pack(">BBH", 0, 6, len(t))
    t = t[:16] + struct.pack(">H", csum(pseudo + t)) + t[18:]
    return eth(dmac, smac, ip4(sip, dip, 6, t, ident))


class Wrapper:
    """Per-capture state: stable MAC/IP/ports per direction, TCP sequence numbers, MBAP transaction ids."""
    A_MAC, B_MAC = b"\x02\x54\x54\x00\x00\x01", b"\x02\x54\x54\x00\x00\x02"       # "A" = channel 0 / master side, "B" = channel 1 / slave side
    A_IP, B_IP = "10.54.0.1", "10.54.0.2"

    def __init__(self) -> None:
        self.seq: Dict[str, int] = {"a": 1000, "b": 5000}
        self.ident = 0
        self.tid = 0

    def dnp3(self, frame: bytes, from_a: bool) -> bytes:
        """DNP3 link frame as TCP/20000: Wireshark decodes that by default (its DNP3-over-UDP dissector is switched off by default)."""
        self.ident += 1
        k, o = ("a", "b") if from_a else ("b", "a")
        seq, ack = self.seq[k], self.seq[o]
        self.seq[k] += len(frame)
        if from_a:
            return tcp_frame(self.A_MAC, self.B_MAC, self.A_IP, self.B_IP, 40000, 20000, seq, ack, frame, self.ident)
        return tcp_frame(self.B_MAC, self.A_MAC, self.B_IP, self.A_IP, 20000, 40000, seq, ack, frame, self.ident)

    def modbus(self, rtu: bytes, from_a: bool) -> bytes:
        """RTU frame (addr, pdu, crc) -> Modbus/TCP segment (MBAP + pdu) on port 502."""
        self.ident += 1
        unit, pdu = rtu[0], rtu[1:-2] if len(rtu) >= 4 else rtu[1:]
        if from_a:
            self.tid = (self.tid + 1) & 0xFFFF
        mbap = struct.pack(">HHHB", self.tid, 0, 1 + len(pdu), unit) + pdu
        k, o = ("a", "b") if from_a else ("b", "a")
        seq, ack = self.seq[k], self.seq[o]
        self.seq[k] += len(mbap)
        if from_a:
            return tcp_frame(self.A_MAC, self.B_MAC, self.A_IP, self.B_IP, 40502, 502, seq, ack, mbap, self.ident)
        return tcp_frame(self.B_MAC, self.A_MAC, self.B_IP, self.A_IP, 502, 40502, seq, ack, mbap, self.ident)

    def raw(self, data: bytes, from_a: bool) -> bytes:
        """Unknown protocol: raw serial bytes as UDP payload on port 20001 (Wireshark shows them as 'Data')."""
        self.ident += 1
        if from_a:
            return udp_frame(self.A_MAC, self.B_MAC, self.A_IP, self.B_IP, 20001, 20001, data, self.ident)
        return udp_frame(self.B_MAC, self.A_MAC, self.B_IP, self.A_IP, 20001, 20001, data, self.ident)
