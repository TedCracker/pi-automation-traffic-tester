"""Which Pi is the time source (docs/SPEC.md section 2, "Clock"). One-way delay numbers are only meaningful when the Pis taking part follow the
same clock, so exactly one Pi serves time and the others follow it.

Setting `clock_source` (per Pi, Settings -> Time source):
  auto      the Pi that was set up first (earliest `setup_at`; ties by id) among the Pis this one can see serves time; the others follow it
  self      this Pi serves time
  <pi id>   follow that Pi
  keep      leave the time setup alone (installs from before there were no roles: Pi A serves, Pi B follows it, as the installer set up)
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, Dict, List, Optional

log = logging.getLogger("tt")


_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
HOLD_S = 600.0            # a Pi that follows another keeps following it this long after that Pi stopped answering (auto mode)


def _key(setup_at: Optional[str], pid: str):
    return (setup_at or "9999", pid)


def decide(me: dict, peers: List[dict], setting: str) -> dict:
    """What this Pi should do about time. `me` = {id, name, setup_at}; `peers` = registry entries (reachable ones count).
    -> {"mode": "serve" | "client" | "keep", "server": ip|None, "source_id", "source_name", "why"}"""
    setting = setting or "auto"
    if setting == "keep":
        return {"mode": "keep", "server": None, "source_id": None, "source_name": None, "why": "Left as the installer set it up."}
    if setting == "self":
        return {"mode": "serve", "server": None, "source_id": me["id"], "source_name": me["name"], "why": "You made this Pi the time source."}
    if setting != "auto":
        p = next((p for p in peers if p["id"] == setting), None)
        if p is None:
            return {"mode": "keep", "server": None, "source_id": setting, "source_name": None, "why": "The Pi you chose as time source is not in your list."}
        return {"mode": "client", "server": _addr(p), "source_id": p["id"], "source_name": p["name"], "why": f"You chose {p['name']} as the time source."}
    cands = [{"id": me["id"], "name": me["name"], "setup_at": me.get("setup_at"), "me": True}] + \
            [{"id": p["id"], "name": p["name"], "setup_at": p.get("setup_at"), "me": False, "peer": p} for p in peers
             if p.get("reachable") and isinstance(p.get("setup_at"), str) and _STAMP.match(p["setup_at"])]      # a set-up time that is not a timestamp (another device's say-so) never wins
    win = min(cands, key=lambda c: _key(c["setup_at"], c["id"]))
    if win["me"]:
        return {"mode": "serve", "server": None, "source_id": me["id"], "source_name": me["name"], "why": "This Pi was set up first, so it is the time source."}
    return {"mode": "client", "server": _addr(win["peer"]), "source_id": win["id"], "source_name": win["name"], "why": f"{win['name']} was set up first, so it is the time source."}


def _addr(p: dict) -> str:
    return p.get("host") or (p.get("ips") or [""])[0]


class ClockManager:
    """Applies the decision through the privileged helper (a function `apply(mode, server) -> {"ok", "message"}`)."""

    def __init__(self, store, identity, peers, apply: Optional[Callable[[str, Optional[str]], dict]] = None):
        self.store, self.me, self.peers = store, identity, peers
        self.apply_fn = apply
        self.last: dict = {}

    def setting(self) -> str:
        return self.store.get_setting("clock_source", "auto") or "auto"

    def set_setting(self, value: str) -> None:
        if value not in ("auto", "self", "keep") and not self.peers.get(value):
            raise ValueError("That Pi is not in your list.")
        self.store.set_setting("clock_source", value)

    def decision(self) -> dict:
        return decide({"id": self.me.id, "name": self.me.name, "setup_at": self.me.setup_at}, self.peers.list(), self.setting())

    def _hold(self, applied: dict, d: dict) -> bool:
        """Auto mode, this Pi follows another one, and the fresh decision is something else only because that Pi does not answer right now
        (one missed probe on Wi-Fi is enough). Changing the time source rewrites chrony's setup and restarts it, which makes every one-way
        number jump, so: keep following until the Pi has been away for HOLD_S."""
        if self.setting() != "auto" or applied.get("mode") != "client" or not applied.get("server"):
            return False
        src = next((p for p in self.peers.list() if applied["server"] == p.get("host") or applied["server"] in (p.get("ips") or [])), None)
        if src is None or src.get("reachable"):
            return False                                     # gone from the list for good, or it answers: the fresh decision stands
        seen = src.get("last_seen")
        return bool(seen) and time.time() - float(seen) < HOLD_S

    def reconcile(self, busy: bool = False) -> dict:
        """`busy` = a test is running on this Pi: the decision is worked out and shown, but nothing is changed until the test is over."""
        d = self.decision()
        applied = self.store.get_setting("clock_applied") or {}
        want = {"mode": d["mode"], "server": d["server"]}
        res = {"changed": False}
        if applied != want and d["mode"] != "keep" and (busy or self._hold(applied, d)):
            res = {"changed": False, "held": True, "message": "A test is running: the time source is left alone until it ends." if busy else
                   "The Pi this one follows does not answer right now: it keeps following it for a while before choosing another time source."}
        elif d["mode"] != "keep" and applied != want and self.apply_fn is not None:
            try:
                r = self.apply_fn(d["mode"], d["server"])
            except Exception as e:  # noqa
                r = {"ok": False, "message": f"{type(e).__name__}: {e}"}
            res = {"changed": bool(r.get("ok")), **r}
            if r.get("ok"):
                self.store.set_setting("clock_applied", want)
            else:
                log.info("clock: could not apply %s: %s", want, r.get("message"))
        self.last = {**d, "applied": self.store.get_setting("clock_applied"), "result": res}
        return self.last
