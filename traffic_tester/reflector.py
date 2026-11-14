"""The always-on reflector of every Pi (docs/SPEC.md section 2), like an iperf3 server or a Cisco IP SLA responder.

Every Pi answers UDP and TCP trip messages on the default test port (20500) from the moment the app starts, so a test that names this Pi as its
destination needs nothing set up here. Things that cannot be shared are claimed per test, by whichever Pi controls the test:

  serial port, raw Layer-2 GOOSE, GOOSE L3 mirror, Mirrored Bits mode, a non-default port, test hooks (delay / drop)

A claim has an owner (the test) and is released when the test ends. A claim on something already in use fails with a plain "busy" message."""
from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Set

from . import engine, factory, mbrun, profiles as P

log = logging.getLogger("tt")
DEFAULT_PORT = 20500


class Busy(Exception):
    """The serial port / interface / port is in use by something else."""


class PortClaims:
    """Serial ports and raw interfaces: one user at a time (a port cannot listen and send at once)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.held: Dict[str, str] = {}

    def claim(self, key: str, owner: str, what: str = "") -> None:
        with self.lock:
            cur = self.held.get(key)
            if cur and cur != owner:
                raise Busy(f"{what or key} is busy: it is being used by {cur}. Wait for that test to finish or pick another port.")
            self.held[key] = owner

    def release(self, key: str, owner: str) -> None:
        with self.lock:
            if self.held.get(key) == owner:
                del self.held[key]

    def release_owner(self, owner: str) -> None:
        with self.lock:
            for k in [k for k, v in self.held.items() if v == owner]:
                del self.held[k]


def reflect_cfg(profile: dict) -> dict:
    """The part of a test the reflecting end needs (the same shape /api/responder/start always took)."""
    return {k: profile[k] for k in ("transport", "serial", "net", "goose", "frame", "mode", "mirrored", "timeout_ms", "threshold_ms", "rule") if k in profile}


def key_of(prof: dict) -> str:
    t, mode = prof["transport"], prof.get("mode", "echo")
    if t == "serial":
        return f"{'mb-' if mode == 'mirrored' else ''}serial:{factory.resolve_serial_port(prof['serial']['port'])}"
    if t in ("udp", "tcp"):
        return f"{'mb-' if mode == 'mirrored' else ''}{t}:{prof['net']['port']}"
    return f"{t}:{prof['goose']['interface']}"


def controller_of(owner: str) -> str:
    """The controlling Pi in an owner string "<name>:<id>:<token>[-probe]" ("" for owners that do not say, e.g. "legacy")."""
    head, sep, _ = (owner or "").rpartition(":")
    return head if sep and ":" in head else ""


def _nice(key: str) -> str:
    """A reflector key in words: "serial:/dev/ttyUSB0" -> "Serial port /dev/ttyUSB0"."""
    k = key.split("#")[0].replace("mb-", "", 1)
    kind, _, what = k.partition(":")
    return {"serial": f"Serial port {what}", "goose_l2": f"Network port {what}", "goose_l3": f"Network port {what}", "udp": f"UDP port {what}", "tcp": f"TCP port {what}"}.get(kind, k)


class Entry:
    def __init__(self, key: str, prof: dict, resp, default: bool, shared: bool):
        self.key, self.prof, self.resp, self.default, self.shared = key, prof, resp, default, shared
        self.owners: Set[str] = set()
        self.paused: List["Entry"] = []          # defaults stopped to make room for this one; started again when this one goes

    def describe(self) -> dict:
        s = self.resp.status() if self.resp else {}
        return {"key": self.key, "transport": self.prof["transport"], "mode": self.prof.get("mode", "echo"), "default": self.default,
                "owners": sorted(self.owners), "running": bool(self.resp and self.resp.running), "echoed": s.get("echoed", 0),
                "last_rx_ago_s": s.get("last_rx_ago_s"), "error": s.get("error"), "detail": s.get("transport")}


class ReflectorService:
    def __init__(self, claims: Optional[PortClaims] = None, hooks: bool = False, ports: Optional[List[int]] = None):
        self.claims = claims or PortClaims()
        self.hooks = hooks
        self.ports = list(ports or [DEFAULT_PORT])
        self.lock = threading.RLock()
        self.entries: Dict[str, Entry] = {}
        self.default_errors: Dict[str, str] = {}
        self.started = False
        self.mb = None                           # the Mirrored Bits registry of this Pi (set by the app)
        self.me_name = lambda: "this Pi"

    # ------------------------------------------------------------ building one
    def _build(self, prof: dict, delay_ms: float = 0.0, drop_pct: float = 0.0):
        if prof["kind"] != "trip":
            raise factory.ConfigError("Only trip tests are reflected (serial, UDP, TCP or GOOSE).")
        if mbrun.is_mirrored(prof):
            return mbrun.MirroredResponder(prof, mbrun.make_link(prof, "B"), registry=self.mb, name=self.me_name())
        return engine.Responder(factory.make_transport(prof, "responder"), delay_ms, drop_pct, self.hooks)

    def _prof(self, cfg: dict) -> dict:
        return P.validate_profile({"name": "reflector", **cfg})

    # ------------------------------------------------------------ always-on
    def start_defaults(self) -> None:
        """UDP and TCP echo on the default test port(s). Best effort: a port that is taken is reported, never fatal."""
        with self.lock:
            self.started = True
            for port in self.ports:
                for t in ("udp", "tcp"):
                    try:
                        self._start_default(t, port)
                    except Exception as e:  # noqa
                        self.default_errors[f"{t}:{port}"] = f"{type(e).__name__}: {e}"
                        log.warning("reflector %s:%s not started: %s", t, port, e)

    def apply_default_ports(self, old: List[int], new: List[int]) -> None:
        """The list of always-on ports was edited: stop the ones taken out, start the new ones (if the app is running them at all)."""
        with self.lock:
            if not self.started:
                return
            for port in set(old) - set(new):
                for t in ("udp", "tcp"):
                    e = self.entries.get(f"{t}:{port}")
                    if e and e.default and not (e.owners):
                        e.resp.stop()
                        del self.entries[f"{t}:{port}"]
            for port in set(new) - set(old):
                for t in ("udp", "tcp"):
                    try:
                        self._start_default(t, port)
                    except Exception as ex:  # noqa
                        self.default_errors[f"{t}:{port}"] = f"{type(ex).__name__}: {ex}"

    def _start_default(self, t: str, port: int) -> Entry:
        key = f"{t}:{port}"
        prof = self._prof({"transport": t, "net": {"port": port}})
        r = self._build(prof)
        r.start()
        e = Entry(key, prof, r, default=True, shared=True)
        self.entries[key] = e
        self.default_errors.pop(key, None)
        return e

    def _pause(self, key: str, holder: Entry) -> None:
        d = self.entries.get(key)
        if d and d.default:
            d.resp.stop()
            del self.entries[key]
            holder.paused.append(d)

    def _resume(self, e: Entry) -> None:
        for d in e.paused:
            try:
                self._start_default(d.prof["transport"], d.prof["net"]["port"])
            except Exception as ex:  # noqa
                self.default_errors[d.key] = f"{type(ex).__name__}: {ex}"
        e.paused = []

    # ------------------------------------------------------------ per test
    def ensure(self, cfg: dict, owner: str, delay_ms: float = 0.0, drop_pct: float = 0.0) -> dict:
        prof = self._prof(cfg)
        shared = prof["transport"] in ("udp", "tcp") and prof.get("mode", "echo") == "echo" and not (self.hooks and (delay_ms or drop_pct))
        key = key_of(prof) + ("" if shared else f"#{owner}" if prof["transport"] in ("udp", "tcp") else "")
        with self.lock:
            self._drop_left_behind(owner)
            e = self.entries.get(key)
            if e is not None and e.resp and e.resp.running and shared:
                e.owners.add(owner)
                return e.describe()
            if e is not None and not shared and owner in e.owners and e.resp and e.resp.running:
                return e.describe()                              # the same test asking again
            if e is not None and not shared:
                raise Busy(f"{_nice(key)} is busy: it is already in use by {', '.join(sorted(e.owners)) or 'another test'}. Wait for that test to finish or pick another port.")
            holder = None
            claim = None
            if prof["transport"] == "serial":
                claim = key_of(prof).replace("mb-", "")
                self.claims.claim(claim, f"reflector for {owner}", f"Serial port {claim.split(':', 1)[1]}")
            elif prof["transport"] == "goose_l2":
                claim = f"iface:{prof['goose']['interface']}"
                self.claims.claim(claim, f"reflector for {owner}", f"Network port {prof['goose']['interface']} (raw frames)")
            try:
                if not shared and prof["transport"] in ("udp", "tcp"):
                    pk = f"{prof['transport']}:{prof['net']['port']}"
                    holder = Entry(key, prof, None, default=False, shared=False)
                    self._pause(pk, holder)
                    held = next((x for k, x in self.entries.items() if k.split("#")[0].replace("mb-", "", 1) == pk), None)
                    if held is not None:                         # another test holds this port (its own echo reflector, or one with the port to itself)
                        raise Busy(f"{_nice(pk)} is busy: it is already in use by {', '.join(sorted(held.owners)) or 'another test'}. Wait for that test to finish or pick another port.")
                if e is not None and shared:                      # a dead one with the same key: start over
                    e.resp.stop()
                    self.entries.pop(key, None)
                r = self._build(prof, delay_ms, drop_pct)
                r.start()
            except Exception:
                if holder:
                    self._resume(holder)
                if claim:
                    self.claims.release(claim, f"reflector for {owner}")
                raise
            ne = holder or Entry(key, prof, None, default=False, shared=shared)
            ne.resp = r
            ne.claim = claim                                     # type: ignore[attr-defined]
            ne.owners.add(owner)
            self.entries[key] = ne
            return ne.describe()

    def _drop_left_behind(self, owner: str) -> None:
        """A Pi controls ONE test at a time. So when a controller asks for something under a new test token, whatever an earlier test of the
        SAME controller still holds here was left behind (it lost contact, crashed or was switched off before it could let go): free it now,
        or the port would stay "busy" until this app is restarted."""
        ctl = controller_of(owner)
        if not ctl:
            return
        stale = {o for e in self.entries.values() for o in e.owners if o != owner and controller_of(o) == ctl}
        for o in stale:
            log.info("reflector: freeing what %s left behind (new request from the same Pi: %s)", o, owner)
            self.release(o)

    def release(self, owner: str) -> int:
        n = 0
        with self.lock:
            for key, e in list(self.entries.items()):
                if owner not in e.owners:
                    continue
                e.owners.discard(owner)
                if e.owners or e.default:
                    continue
                try:
                    e.resp.stop()
                except Exception:
                    pass
                self.entries.pop(key, None)
                if getattr(e, "claim", None):
                    self.claims.release(e.claim, f"reflector for {owner}")
                self._resume(e)
                n += 1
        return n

    def stop_all(self) -> None:
        with self.lock:
            for e in list(self.entries.values()):
                try:
                    e.resp.stop()
                except Exception:
                    pass
            self.entries.clear()

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        with self.lock:
            items = [e.describe() for e in self.entries.values()]
        running = [i for i in items if i["running"]]
        return {"running": bool(running), "items": items, "echoed": sum(i["echoed"] for i in items),
                "ports": self.ports, "default_errors": dict(self.default_errors),
                "claims": dict(self.claims.held)}

    def legacy_status(self) -> dict:
        """The shape the single "Responder" had (kept for older callers): the busiest running reflector."""
        s = self.status()
        run = sorted((i for i in s["items"] if i["running"]), key=lambda i: (i["default"], -i["echoed"]))
        if not run:
            return {"running": False}
        i = run[0]
        return {"running": True, "echoed": s["echoed"], "hellos": 0, "last_rx_ago_s": min([x["last_rx_ago_s"] for x in run if x["last_rx_ago_s"] is not None] or [None]) if any(x["last_rx_ago_s"] is not None for x in run) else None,
                "transport": i["detail"], "error": i["error"], "recoveries": 0, "tcp_sessions": None, "mode": i["mode"]}

    def mirrored_events(self) -> list:
        with self.lock:
            for e in self.entries.values():
                if e.resp and hasattr(e.resp, "reflect_events") and e.resp.running:
                    return e.resp.reflect_events()
        return []
