"""Turning a test's Source / Destination / Direction into legs (docs/SPEC.md section 2). Pure functions: no network, no threads, easy to unit-test.

A trip test is made of one or two LEGS. A leg is "this Pi sends trip messages, that Pi reflects them":
  Round trip, One way from -> to   one leg   (tester = source, reflector = destination)
  One way to -> from               one leg   (tester = destination, reflector = source)
  Both ways at once                two legs at the same time (UDP / TCP only)
The Pi whose page you are on is the CONTROLLER: it prepares the reflecting ends, starts every tester (itself, or another Pi through that Pi's
API), collects the results and stores the run(s)."""
from __future__ import annotations

import copy
import socket
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import peers as PE
from . import factory


class PlanError(factory.ConfigError):
    """The test cannot be set up as asked (plain message for the user)."""


@dataclass
class Node:
    kind: str                     # "self" (the controller) | "peer" (a Pi from the list) | "addr" (a typed address, not controllable) | "none" (not named)
    id: str = ""
    name: str = ""
    host: str = ""                # the address the controller reaches it at
    port: int = PE.DEFAULT_PORT   # its web port
    ips: List[str] = field(default_factory=list)
    peer: Optional[dict] = None
    data_host: str = ""           # an address typed by hand that belongs to this Pi: the test traffic goes there (the controller still talks to `host`)

    @property
    def is_self(self) -> bool:
        return self.kind == "self"

    @property
    def controllable(self) -> bool:
        return self.kind in ("self", "peer")

    def label(self) -> str:
        return self.name or self.host or "?"


@dataclass
class Leg:
    index: int
    label: str                    # "pi1 -> pi2"
    kind: str                     # "round_trip" (judged on the round trip) | "one_way" (judged on the delay tester -> reflector)
    tester: Node
    reflector: Node
    profile: dict                 # what the tester runs: source = itself, destination = a plain address
    reflect: Optional[dict]       # what the reflecting end must be running (None = nothing to set up: an address we cannot control)
    basis: str = "rtt"            # what pass/fail is judged on: "rtt" or "up" (one-way delay tester -> reflector)
    data_host: str = ""

    def meta(self) -> dict:
        return {"label": self.label, "kind": self.kind, "index": self.index, "source": self.tester.label(), "dest": self.reflector.label(),
                "source_id": self.tester.id, "dest_id": self.reflector.id, "basis": self.basis, "data_host": self.data_host}


@dataclass
class Context:
    me: Dict[str, object]                                        # {"id","name","ips"}
    resolve: Callable[[str], Optional[dict]]                     # id / name / address -> peer dict (PeerRegistry.resolve)
    local_ip_for: Callable[[str], str] = lambda host: ""         # this Pi's address on the path to `host`
    iface_ip: Callable[[str], str] = lambda iface: ""            # this Pi's address on one of its own network ports


def local_ip_for(host: str) -> str:
    """The address this Pi would use as the source of a packet to `host` (no packet is sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


# ---------------------------------------------------------------- nodes
def node_for(ep: dict, ctx: Context, what: str) -> Node:
    ep = ep or {}
    pi, host = (ep.get("pi") or "").strip(), (ep.get("host") or "").strip()
    me = ctx.me
    if (not pi and not host) or pi == "self" or (pi and pi == me["id"]):
        return Node("self", id=str(me["id"]), name=str(me["name"]), host="127.0.0.1", ips=list(me.get("ips") or []))
    ref = pi or host
    p = ctx.resolve(ref)
    if p is None and pi and host:
        p = ctx.resolve(host)
    if p is not None:
        if p["id"] == me["id"]:
            return Node("self", id=str(me["id"]), name=str(me["name"]), host="127.0.0.1", ips=list(me.get("ips") or []))
        typed = host if (host and not pi and host != p["host"]) else ""
        return Node("peer", id=p["id"], name=p["name"], host=p["host"], port=p.get("port", PE.DEFAULT_PORT), ips=list(p.get("ips") or [p["host"]]), peer=p, data_host=typed)
    if pi and not host:
        raise PlanError(f"{what} '{ep.get('name') or pi}' is not in your list of Pis. Open Settings → Pis on this network to add it, or pick another.")
    return Node("addr", name=host, host=host, ips=[host])


def data_address(tester: Node, reflector: Node, ctx: Context) -> str:
    """The address the TESTER must send to in order to reach the REFLECTOR."""
    if reflector.data_host:                                  # the user typed the address to send to: it is used as typed
        return reflector.data_host
    if reflector.kind in ("addr",):
        return reflector.host
    if reflector.kind == "none":
        return ""
    if tester.is_self:
        return "127.0.0.1" if reflector.is_self else reflector.host
    # the tester is another Pi: it must reach the reflector on an address that Pi can use
    if reflector.is_self:
        mine = ctx.local_ip_for(tester.host) or ""
        if mine and tester.host.startswith("127."):          # that Pi runs on this very machine (tests): loopback is the only address that always works
            return mine
        return PE.choose_address(tester.ips, [mine] + [i for i in (ctx.me.get("ips") or []) if i != mine] if mine else list(ctx.me.get("ips") or []), mine)
    if tester.host.startswith("127.") and reflector.host.startswith("127."):      # both Pis run on this machine (tests)
        return reflector.host
    return PE.choose_address(tester.ips, reflector.ips, reflector.host)


# ---------------------------------------------------------------- trip tests
def _same(a: Node, b: Node) -> bool:
    if a.kind == "self" or b.kind == "self":
        return a.kind == b.kind
    if a.kind == "none" or b.kind == "none":
        return False
    return (a.id and a.id == b.id) or (not a.id and not b.id and a.host == b.host)


def _ends(profile: dict, ctx: Context):
    """The sources and destinations of a test as [(Node, endpoint dict)], duplicates dropped. More than one of either = more links."""
    t = profile["transport"]
    src_eps = [dict(profile.get("source") or {})] + [dict(e) for e in (profile.get("sources") or [])]
    first = dict(profile.get("dest") or {})
    legacy_host = (profile["net"].get("peer_host") or "").strip() if t in ("udp", "tcp") else ""
    if not first.get("pi") and not first.get("host") and legacy_host:
        first["host"] = legacy_host
    dst_eps = [first] + [dict(e) for e in (profile.get("dests") or [])]
    srcs: list = []
    for ep in src_eps:
        n = node_for(ep, ctx, "The Pi to send from")
        if not any(_same(n, m) for m, _ in srcs):
            srcs.append((n, ep))
    dsts: list = []
    for i, ep in enumerate(dst_eps):
        if ep.get("pi") or ep.get("host"):
            n = node_for(ep, ctx, "The Pi to send to")
        elif i == 0 and len(dst_eps) == 1:
            n = Node("none", name="the other end")
            if t in ("udp", "tcp"):
                raise PlanError("Choose where to send: tick a Pi under To or type its IP address.")
        else:
            continue                                         # an empty extra row
        if not any(_same(n, m) for m, _ in dsts):
            dsts.append((n, ep))
    if not dsts:
        raise PlanError("Choose where to send: tick a Pi under To or type its IP address.")
    return srcs, dsts


def plan_trip(profile: dict, ctx: Context) -> List[Leg]:
    direction = profile.get("direction", "round_trip")
    srcs, dsts = _ends(profile, ctx)
    reverses = {"both": [False, True], "reverse": [True]}.get(direction, [False])
    kind = "one_way" if direction in ("forward", "reverse") else "round_trip"
    legs: List[Leg] = []
    for src, src_ep in srcs:
        for dst, dst_ep in dsts:
            if _same(src, dst):
                if len(srcs) * len(dsts) == 1:
                    raise PlanError(f"From and To are the same Pi ({src.label()}). Pick another Pi to send to.")
                continue                                     # pi1, pi2 -> pi1, pi2: a Pi does not test itself
            for rev in reverses:
                tester, refl = (dst, src) if rev else (src, dst)
                if not tester.controllable:
                    raise PlanError(f"{tester.label()} is not in your list of Pis, so this Pi cannot start the test there. Add it in Settings → Pis on this network, or run the test the other way round.")
                legs.append(_leg(profile, len(legs), kind, rev, tester, refl, src_ep, dst_ep, ctx))
    if not legs:
        raise PlanError("Nothing to test: every source is also the destination. Pick different Pis for From and To.")
    # A Pi other than this one runs ONE test at a time: it can be the sender of one link only.
    seen: Dict[str, str] = {}
    for l in legs:
        if l.tester.is_self:
            continue
        if l.tester.id in seen:
            raise PlanError(f"{l.tester.label()} would have to send on two links at once ({seen[l.tester.id]} and {l.label}). A Pi you are not on can send on one link per test: "
                            f"start this test from {l.tester.label()}'s own page, or pick fewer Pis.")
        seen[l.tester.id] = l.label
    inherited = profile.get("leg_info")
    if inherited and len(legs) == 1:                    # a test handed over by the controlling Pi: it already decided who sends, who reflects and what is judged
        leg = legs[0]
        leg.profile["leg_info"] = {**leg.meta(), **inherited}
        leg.basis = inherited.get("basis", leg.basis)
        leg.label = inherited.get("label", leg.label)
        leg.kind = inherited.get("kind", leg.kind)
    return legs


def _leg(profile: dict, index: int, kind: str, rev: bool, tester: Node, refl: Node, src_ep: dict, dst_ep: dict, ctx: Context) -> Leg:
    t = profile["transport"]
    # the Endpoint that describes each end, relative to the leg
    t_ep, r_ep = (dst_ep, src_ep) if rev else (src_ep, dst_ep)
    p = copy.deepcopy(profile)
    host = data_address(tester, refl, ctx)
    port = r_ep.get("port") or p["net"]["port"]
    # what the reflecting end must run
    r = copy.deepcopy(profile)
    p["source"], p["dest"] = {}, {}
    p["sources"], p["dests"] = [], []                # a leg is one link between two Pis
    p["direction"] = "round_trip"                  # a leg is always "tester sends, reflector answers"; one-way judging is `basis`
    if t in ("udp", "tcp"):
        # the port picked for the sending Pi: the test leaves through it, and goes to the far Pi's address on that port's network
        iface = (t_ep.get("iface") or "").strip()
        iface = "" if iface == "auto" else iface
        if iface and tester.is_self and refl.kind == "peer" and not refl.data_host:
            mine = ctx.iface_ip(iface)
            if mine:
                host = PE.choose_address([mine], refl.ips, host)
        p["net"]["iface"] = iface
        r["net"]["iface"] = ""
        p["net"]["peer_host"] = host
        p["net"]["port"] = int(port)
        r["net"]["port"] = int(port)
    elif t == "serial":
        tp = (t_ep.get("serial_port") or "").strip() or (profile["serial"]["port"] if not rev else "auto")
        rp = (r_ep.get("serial_port") or "").strip() or (profile["serial"]["port"] if rev else "auto")
        p["serial"]["port"], r["serial"]["port"] = tp, rp
    elif t in ("goose_l2", "goose_l3"):
        ti = (t_ep.get("iface") or "auto")
        ri = (r_ep.get("iface") or "auto")
        if not rev and profile["goose"]["interface"] not in ("", "auto") and ti in ("", "auto"):
            ti = profile["goose"]["interface"]
        p["goose"]["interface"], r["goose"]["interface"] = ti, ri
    r["source"], r["dest"] = {}, {}
    r["sources"], r["dests"] = [], []
    if r.get("mode") == "mirrored":              # the answering end is the mirror image: PROTO B, TX_ID <-> RX_ID, no scheduled flips, no injected impairment
        from .mbemu.pairing import far_settings
        r["mirrored"]["settings"] = far_settings(p["mirrored"].get("settings") or {})
        r["mirrored"]["impair"] = {}
    basis = "up" if kind == "one_way" else "rtt"
    label = f"{tester.label()} → {refl.label()}"
    reflect = None
    if refl.controllable:
        reflect = {k: r[k] for k in ("transport", "serial", "net", "goose", "frame", "mode", "mirrored", "timeout_ms", "threshold_ms", "rule") if k in r}
    leg = Leg(index, label, kind, tester, refl, p, reflect, basis, host)
    p["leg_info"] = leg.meta()
    return leg


def describe_plan(legs: List[Leg]) -> List[str]:
    out = []
    for l in legs:
        how = {"round_trip": "round trip", "one_way": "one way"}[l.kind]
        out.append(f"{l.label} ({how})")
    return out
