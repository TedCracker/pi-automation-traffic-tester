"""App-side client for the privileged network helper. The web app is NOT root: it writes a request file, asks systemd to start the one fixed root unit
(traffic-tester-update.service, the same one the software updater uses; polkit allows exactly that) and polls the root-written result.

Test/dev mode: TT_NET_FAKE=1 runs the same operations IN-PROCESS against a fake NetworkManager (see update/netops.py), so unit tests and container
tests need no NetworkManager and no systemd. TT_NET_RUNNER=<path> points nmcli at a shim program instead."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from typing import Dict, Optional, Tuple

from ..update import netops

UNIT = "traffic-tester-update.service"
_lock = threading.Lock()                                  # one helper request at a time from this process


class NetClientError(Exception):
    def __init__(self, message: str, status: int = 500, code: str = "error"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def fake() -> bool:
    return os.environ.get("TT_NET_FAKE") == "1"


def state_dir() -> str:
    return os.environ.get("TT_NET_STATE_DIR") or os.environ.get("TT_UPDATE_STATE_DIR") or netops.DEFAULT_STATE_DIR


def data_dir() -> str:
    return os.environ.get("TT_NET_DATA_DIR") or os.environ.get("TT_DATA_DIR") or "./data"


def available() -> Tuple[bool, str]:
    """(can the app change the network here?, plain reason when not)"""
    if fake():
        return True, ""
    if os.environ.get("TT_UPDATE_MODE", "").lower() != "system":
        return False, "Network changes need the installed service (run install-pi.sh on the Pi). This copy of the app runs without it, so it can only show the network."
    return True, ""


def ctx() -> "netops.Ctx":
    return netops.make_ctx(state_dir(), data_dir=data_dir())


# ------------------------------------------------------------------ root-written, world-readable state
def read_state() -> dict:
    d = netops.blank_public()
    d.update(netops.read_json(os.path.join(state_dir(), netops.F_STATE), {}) or {})
    fb = netops.blank_public()["fallback"]
    fb.update(d.get("fallback") or {})
    d["fallback"] = fb
    return d


def read_pending() -> Optional[dict]:
    p = netops.read_json(os.path.join(state_dir(), netops.F_PENDING), None)
    if not p:
        return None
    p = dict(p)
    p["seconds_left"] = max(0, int(round(float(p.get("deadline") or 0) - time.time()))) if p.get("state") == "waiting" else None
    return p


def read_result() -> Optional[dict]:
    return netops.read_json(os.path.join(state_dir(), netops.F_RESULT), None)


def watcher_alive(max_age: float = 60.0) -> bool:
    """Is the root dead-man service (traffic-tester-netwatch) running? Then the app's own thread leaves the fallback to it."""
    b = netops.read_json(os.path.join(state_dir(), netops.F_BEAT), None)
    return bool(b and time.time() - float(b.get("t") or 0) < max_age)


# ------------------------------------------------------------------ asking the root helper
def _write_request(req: dict) -> None:
    d = os.path.join(data_dir(), "update")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "request.json")
    tmp = f"{path}.{os.getpid()}.net.tmp"
    with open(tmp, "w") as f:
        json.dump(req, f)
    os.replace(tmp, path)


def _start_unit() -> None:
    r = subprocess.run(["systemctl", "start", "--no-block", UNIT], capture_output=True, text=True, timeout=30)
    if r.returncode:
        err = (r.stderr or "").strip()
        if "authentication" in err.lower() or "access denied" in err.lower() or "not permitted" in err.lower():
            raise NetClientError("The service is not allowed to start the network helper. Run the installer again (it installs the permission rule).", 503, "not_permitted")
        raise NetClientError("The network helper could not be started: " + (err[:160] or "systemctl failed"), 503, "helper_failed")


def call(op: str, params: Optional[dict] = None, wait: float = 60.0, action: str = "net") -> dict:
    """Run one helper operation and return its result dict ({"ok","code","message",...}). Returns {"ok": None, "code": "working", "id": ...} when it
    did not finish within `wait` seconds (poll read_result()/read_pending() afterwards)."""
    ok, why = available()
    if not ok:
        raise NetClientError(why, 503, "unavailable")
    rid = uuid.uuid4().hex[:16]
    req: Dict[str, object] = {"action": action, "id": rid, **(params or {})}
    if action == "net":
        req["op"] = op
    if fake():
        return netops.handle(ctx(), req)
    with _lock:
        _write_request(req)
        _start_unit()
    end = time.monotonic() + wait
    while True:
        res = read_result()
        if res and res.get("id") == rid:
            return res
        if time.monotonic() > end:
            return {"ok": None, "code": "working", "message": "Still working.", "id": rid}
        time.sleep(0.3)


def clock(mode: str, server: Optional[str] = None, wait: float = 60.0) -> dict:
    """The peer-clock feature: mode 'serve' (this Pi is the time source), 'client' (take time from `server`), 'none', or 'status'."""
    p: Dict[str, object] = {"mode": mode}
    if server:
        p["server"] = server
    return call("clock", p, wait, action="clock")
