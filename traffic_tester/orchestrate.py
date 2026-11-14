"""The controller's side of a test: check every Pi taking part, prepare the reflecting ends, and clean up afterwards (docs/SPEC.md section 2).

State machine of one test (`Orchestration.state`): planning -> checking -> preparing -> running -> collecting -> done | failed | stopped.
Every step that touches another Pi goes through that Pi's normal web API (honouring its password); a failure in any step releases what was
prepared before it, so a refused or failed start never leaves a reflector or a serial port claimed on some other Pi."""
from __future__ import annotations

import logging
import secrets
import socket
from typing import Callable, Dict, List, Optional, Tuple

from . import plan as PL, system
from .peers import PeerAuthError, PeerClient, PeerError
from .reflector import Busy as ReflectorBusy

log = logging.getLogger("tt")

STATES = ("planning", "checking", "preparing", "running", "collecting", "done", "failed", "stopped")


class Services:
    """What a controller needs besides the run manager: who I am, who the other Pis are, my own reflectors and port claims.
    Any of them can be None (a bare RunManager in a unit test): then only this Pi and typed addresses exist."""

    def __init__(self, identity=None, peers=None, reflectors=None, claims=None):
        self.identity, self.peers, self.reflectors, self.claims = identity, peers, reflectors, claims
        from .mbemu.registry import Registry
        self.mb = Registry()                      # what the web API shows of the Mirrored Bits emulator on this Pi
        if reflectors is not None:
            reflectors.mb = self.mb
            reflectors.me_name = lambda: self.me()["name"]

    def me(self) -> dict:
        if self.identity is not None:
            return {"id": self.identity.id, "name": self.identity.name, "ips": self.identity.ips()}
        return {"id": "self", "name": socket.gethostname().split(".")[0] or "this Pi", "ips": []}

    def context(self) -> PL.Context:
        resolve = self.peers.resolve if self.peers is not None else (lambda ref: None)
        return PL.Context(self.me(), resolve, PL.local_ip_for, system.iface_ip)

    def client(self, node: PL.Node) -> PeerClient:
        if self.peers is not None and node.peer:
            return self.peers.client(node.peer)
        return PeerClient(node.host, node.port, "", label=node.label())


# ---------------------------------------------------------------- clocks
def clock_agree(a: dict, b: dict, a_ips: List[str], b_ips: List[str]) -> Tuple[Optional[bool], str]:
    """Do two Pis follow the SAME time source? Both can say "synced" (each to its own local clock) while being seconds apart, so
    "synced" alone is not enough. None = cannot tell (an older app version that does not report its time source)."""
    if system.assume_synced():
        return True, ""
    ra, rb = a.get("ref_ip"), b.get("ref_ip")
    if "ref_ip" not in a or "ref_ip" not in b:
        return None, ""
    if ra and ra in b_ips or rb and rb in a_ips:
        return True, ""
    if ra and rb and ra == rb and not a.get("ref_local") and not b.get("ref_local"):
        return True, ""
    return False, "they are not following the same time source (set one Pi as the time source in Settings → Time source)"


def node_clock(svc: Services, node: PL.Node) -> Tuple[dict, List[str]]:
    if node.is_self:
        return system.clock_state(0), list(svc.me()["ips"])
    p = svc.peers.fresh(node.id) if svc.peers is not None else None
    return ((p or {}).get("clock") or {}), list(node.ips)


# ---------------------------------------------------------------- checks
def check_nodes(svc: Services, legs: List[PL.Leg]) -> None:
    """Every Pi that has to be controlled is on and answers (and its password, if any, is known)."""
    seen = set()
    for leg in legs:
        for n in (leg.tester, leg.reflector):
            if n.kind != "peer" or n.id in seen:
                continue
            seen.add(n.id)
            p = svc.peers.fresh(n.id, 0) if svc.peers is not None else None      # asked again right now: a Pi that went away a second ago must not pass
            if not p or not p.get("reachable"):
                raise PL.PlanError(f"{n.label()} does not answer at {n.host}. Check that it is on and on the same network, then try again.")
            if p.get("password_required") and p.get("auth") == "needs_password":
                raise PL.PlanError(f"{n.label()} is password-protected. Enter its password in Settings → Pis on this network.")


def check_clocks(svc: Services, legs: List[PL.Leg], profile: dict) -> None:
    """Fill in whether each leg's two clocks agree (the engine refuses a one-way test when they do not)."""
    if not (profile.get("one_way") or profile.get("mode") == "mirrored" or any(l.basis == "up" for l in legs)):
        return
    for leg in legs:
        if not leg.reflector.controllable:
            continue
        try:
            ca, ia = node_clock(svc, leg.tester)
            cb, ib = node_clock(svc, leg.reflector)
        except Exception:
            continue
        agree, note = clock_agree(ca, cb, ia, ib)
        info = leg.profile["leg_info"]
        info["clock_agree"] = agree
        info["clock_note"] = f"{leg.tester.label()} and {leg.reflector.label()}: {note}" if note else ""


# ---------------------------------------------------------------- reflectors
def new_owner(svc: Services) -> str:
    """Who holds what a test claims on another Pi: "<Pi name>:<Pi id>:<token of this test>". Everything before the last colon names the
    controlling Pi, so the other Pi can tell that a new request comes from the same controller as a claim left behind (reflector.py)."""
    me = svc.me()
    return f"{me['name']}:{me['id']}:{secrets.token_hex(3)}"


def prepare_reflectors(svc: Services, legs: List[PL.Leg], owner: str) -> List[Tuple[PL.Node, str]]:
    """Start (or claim) the reflecting end of every leg. Returns what was prepared, for `release_reflectors`. Raises PlanError and
    releases everything already prepared when one of them cannot be."""
    done: List[Tuple[PL.Node, str]] = []
    try:
        for leg in legs:
            if leg.reflect is None:
                continue
            n = leg.reflector
            key = (n.kind, n.id)
            try:
                if n.is_self:
                    if svc.reflectors is None:
                        raise PL.PlanError("This Pi has no reflector service.")
                    svc.reflectors.ensure(leg.reflect, owner)
                else:
                    svc.client(n).post("/api/peer/reflect/ensure", {"cfg": leg.reflect, "owner": owner}, timeout=10)
            except ReflectorBusy as e:
                raise PL.PlanError(f"{n.label()}: {e}")
            except PeerAuthError as e:
                raise PL.PlanError(str(e))
            except PeerError as e:
                detail = getattr(e, "detail", None)
                raise PL.PlanError(f"{n.label()} could not get ready to answer: {detail or e}")
            except (OSError, ValueError) as e:
                raise PL.PlanError(f"{n.label()} could not get ready to answer: {e}")
            done.append((n, owner))
    except Exception:
        release_reflectors(svc, done)
        raise
    return done


def release_reflectors(svc: Services, prepared: List[Tuple[PL.Node, str]]) -> None:
    for n, owner in {(n.id or "self", o): (n, o) for n, o in prepared}.values():
        try:
            if n.is_self:
                if svc.reflectors is not None:
                    svc.reflectors.release(owner)
            else:
                svc.client(n).post("/api/peer/reflect/release", {"owner": owner}, timeout=6)
        except Exception as e:  # noqa
            log.warning("could not release the reflector on %s: %s", n.label(), e)
