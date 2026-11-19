"""Display filter: choose which captured packets the list shows, without changing what is saved.

Plain words, joined with and / or / not and brackets:
  host 10.0.0.5 · src 10.0.0.5 · dst 10.0.0.5 · port 20000 · src port 502 · mac aa:bb:… · vlan 100
  tcp udp icmp arp dnp3 modbus goose rgoose snmp iec104 ptp · errors · retrans · crc · response
  len > 100 · delta > 500 (ms since the previous packet of the conversation) · resp > 100 (ms from request to response)
  channel Master · "any words" (looks in the info column) · Wireshark forms ip.addr == 10.0.0.5, tcp.port == 502 also work.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional

Pred = Callable[[dict], bool]

PROTOS: Dict[str, List[str]] = {                      # word -> row protocol names it matches
    "tcp": ["tcp", "dnp3", "modbus/tcp", "iec 104"], "udp": ["udp", "snmp", "ptp", "r-goose"], "icmp": ["icmp", "icmpv6"], "arp": ["arp"], "dnp3": ["dnp3"],
    "modbus": ["modbus/tcp", "modbus rtu"], "goose": ["goose", "r-goose"], "rgoose": ["r-goose"], "r-goose": ["r-goose"], "snmp": ["snmp"], "iec104": ["iec 104"], "104": ["iec 104"],
    "iec": ["iec 104"], "ptp": ["ptp"], "lldp": ["lldp"], "ipv4": ["ipv4"], "ipv6": ["ipv6", "icmpv6"], "sv": ["sv"], "wifi": ["802.11"], "802.11": ["802.11"], "beacon": ["802.11"],
}
_OPS = {">": lambda a, b: a > b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "=": lambda a, b: a == b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
_TOK = re.compile(r'\s*(?:(\()|(\))|(&&|\|\||!=|==|>=|<=|[><=!])|"([^"]*)"|([^\s()"&|!=<>]+))')


def _tokens(expr: str) -> List[tuple]:
    out, i = [], 0
    while i < len(expr):
        m = _TOK.match(expr, i)
        if not m or m.end() == i:
            if expr[i:].strip() == "":
                break
            raise ValueError(f"I cannot read the filter near “{expr[i:i + 12].strip()}”.")
        i = m.end()
        if m.group(1):
            out.append(("(", "("))
        elif m.group(2):
            out.append((")", ")"))
        elif m.group(3):
            out.append(("op", m.group(3)))
        elif m.group(4) is not None:
            out.append(("str", m.group(4)))
        else:
            w = m.group(5)
            out.append(("word", w))
    return out


class _P:
    def __init__(self, toks: List[tuple]):
        self.t, self.i = toks, 0

    def peek(self) -> Optional[tuple]:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> tuple:
        x = self.t[self.i]
        self.i += 1
        return x

    def word(self, *names: str) -> bool:
        x = self.peek()
        return bool(x and x[0] in ("word", "op") and x[1].lower() in names)

    def parse(self) -> Pred:
        p = self.or_()
        if self.peek():
            raise ValueError(f"Unexpected “{self.peek()[1]}” in the filter.")
        return p

    def or_(self) -> Pred:
        a = self.and_()
        while self.word("or", "||"):
            self.take()
            b = self.and_()
            a = (lambda a, b: lambda r: a(r) or b(r))(a, b)
        return a

    def and_(self) -> Pred:
        a = self.not_()
        while self.word("and", "&&"):
            self.take()
            b = self.not_()
            a = (lambda a, b: lambda r: a(r) and b(r))(a, b)
        return a

    def not_(self) -> Pred:
        if self.word("not", "!"):
            self.take()
            a = self.not_()
            return lambda r: not a(r)
        return self.atom()

    def value(self, what: str) -> str:
        x = self.peek()
        if not x or x[0] not in ("word", "str"):
            raise ValueError(f"“{what}” needs a value, for example “{what} 10.0.0.5”.")
        return self.take()[1]

    def num_cmp(self, name: str, key: str, scale: float = 1.0) -> Pred:
        op = ">="
        x = self.peek()
        if x and x[0] == "op":
            op = self.take()[1]
            if op not in _OPS:
                raise ValueError(f"“{op}” is not a comparison I know after “{name}”.")
        try:
            n = float(self.value(name))
        except ValueError:
            raise ValueError(f"“{name}” compares with a number, for example “{name} > 100”.")
        f = _OPS[op]
        return lambda r: r.get(key) is not None and f(r[key] * scale, n)

    def atom(self) -> Pred:
        x = self.peek()
        if x is None:
            raise ValueError("The filter ends too soon.")
        if x[0] == "(":
            self.take()
            p = self.or_()
            if not self.peek() or self.take()[0] != ")":
                raise ValueError("A bracket is not closed.")
            return p
        if x[0] == "str":
            s = self.take()[1].lower()
            return lambda r: s in (r.get("info") or "").lower() or s in (r.get("src") or "").lower() or s in (r.get("dst") or "").lower()
        if x[0] != "word":
            raise ValueError(f"Unexpected “{x[1]}” in the filter.")
        w = self.take()[1]
        lw = w.lower()
        if lw in ("host", "ip.addr", "ip.src", "ip.dst", "src", "dst", "ip"):
            if lw == "src" and self.word("port"):
                self.take()
                return self._port("sport", self.value("src port"))
            if lw == "dst" and self.word("port"):
                self.take()
                return self._port("dport", self.value("dst port"))
            if self.peek() and self.peek()[0] == "op" and self.peek()[1] in ("==", "="):
                self.take()
            if lw == "ip" and self.word("addr"):
                self.take()
            v = self.value(w).lower()
            keys = {"src": ("sip",), "ip.src": ("sip",), "dst": ("dip",), "ip.dst": ("dip",)}.get(lw, ("sip", "dip"))
            return lambda r: any(_ip_match(r.get(k), v) for k in keys)
        if lw in ("port", "tcp.port", "udp.port", "tcp.srcport", "udp.srcport", "tcp.dstport", "udp.dstport"):
            if self.peek() and self.peek()[0] == "op" and self.peek()[1] in ("==", "="):
                self.take()
            v = self.value(w)
            keys = ("sport",) if "src" in lw else ("dport",) if "dst" in lw else ("sport", "dport")
            if lw.startswith("tcp"):
                return (lambda p: lambda r: r.get("l4") == "tcp" and p(r))(self._port_keys(keys, v))
            if lw.startswith("udp"):
                return (lambda p: lambda r: r.get("l4") == "udp" and p(r))(self._port_keys(keys, v))
            return self._port_keys(keys, v)
        if lw in ("mac", "eth.addr", "eth.src", "eth.dst"):
            if self.peek() and self.peek()[0] == "op" and self.peek()[1] in ("==", "="):
                self.take()
            v = self.value(w).lower().replace("-", ":")
            return lambda r: v in ((r.get("smac") or "").lower(), (r.get("dmac") or "").lower()) or v in (r.get("src") or "").lower() and ":" in v
        if lw in ("vlan", "vlan.id"):
            if self.peek() and self.peek()[0] == "op" and self.peek()[1] in ("==", "="):
                self.take()
            v = self.value(w)
            if v.isdigit():
                return lambda r: bool(r.get("vlan")) and r["vlan"]["id"] == int(v)
            raise ValueError("“vlan” needs a number, for example “vlan 100”.")
        if lw in ("len", "length", "frame.len"):
            return self.num_cmp(w, "len")
        if lw in ("delta", "dt"):
            return self.num_cmp(w, "delta_ms")
        if lw in ("resp", "response", "rtt"):
            if not self.peek() or self.peek()[0] != "op":
                return lambda r: r.get("resp_ms") is not None
            return self.num_cmp(w, "resp_ms")
        if lw in ("errors", "error", "bad"):
            return lambda r: bool(r.get("errors"))
        if lw in ("retrans", "retransmission", "retransmissions"):
            return lambda r: bool(r.get("retrans"))
        if lw in ("crc", "crcfail", "crc-fail"):
            return lambda r: bool(r.get("crc_bad"))
        if lw in ("channel", "ch"):
            v = self.value(w).lower()
            return lambda r: v in (r.get("channel_name") or "").lower()
        if lw in ("info", "contains", "text"):
            if self.word("contains", "==", "="):
                self.take()
            s = self.value(w).lower()
            return lambda r: s in (r.get("info") or "").lower()
        if lw in PROTOS:
            names = PROTOS[lw]
            return lambda r: (r.get("proto") or "").lower() in names or (lw in ("tcp", "udp") and r.get("l4") == lw)
        # a bare word: an IP, a MAC, a port number, or a word to look for in the packet text
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", w):
            return lambda r: _ip_match(r.get("sip"), lw) or _ip_match(r.get("dip"), lw)
        if re.fullmatch(r"([0-9a-f]{2}[:-]){5}[0-9a-f]{2}", lw):
            v = lw.replace("-", ":")
            return lambda r: v in ((r.get("smac") or "").lower(), (r.get("dmac") or "").lower())
        if w.isdigit():
            return self._port_keys(("sport", "dport"), w)
        return lambda r: lw in (r.get("info") or "").lower() or (r.get("proto") or "").lower() == lw

    def _port(self, key: str, v: str) -> Pred:
        return self._port_keys((key,), v)

    def _port_keys(self, keys: tuple, v: str) -> Pred:
        if not v.isdigit():
            raise ValueError("A port is a number, for example “port 20000”.")
        n = int(v)
        return lambda r: any(r.get(k) == n for k in keys)


def _ip_match(have: Optional[str], want: str) -> bool:
    if not have:
        return False
    have = have.lower()
    if have == want:
        return True
    if want.endswith(".") or want.endswith("/"):               # a prefix: 10.10.10.
        return have.startswith(want.rstrip("/"))
    if "/" in want:                                            # 192.0.2.0/24
        try:
            import ipaddress
            return ipaddress.ip_address(have) in ipaddress.ip_network(want, strict=False)
        except ValueError:
            return False
    return False


def compile_filter(expr: str) -> Pred:
    """Return a predicate over analyzed rows. An empty filter shows everything. Raises ValueError with a plain message."""
    expr = (expr or "").strip()
    if not expr:
        return lambda r: True
    return _P(_tokens(expr)).parse()


def check(expr: str) -> Optional[str]:
    try:
        compile_filter(expr)
        return None
    except ValueError as e:
        return str(e)
    except (IndexError, TypeError):
        return "I cannot read that filter."
