"""The app-side dead-man thread: no network for 3 minutes -> ask the root helper for the emergency access point; retry the normal profiles every 5 minutes.
The decision itself is the pure function netops.fallback_decision (shared with the root service traffic-tester-netwatch, which also runs while the app is down).
When that service is running (it writes a heartbeat) this thread only watches; it never acts twice."""
from __future__ import annotations

import threading
from typing import Callable, List, Optional

from ..update import netops
from . import client

ACTION_OP = {"ap_up": "fallback_ap", "ap_down": "fallback_off", "retry": "fallback_retry", "retry_ok": "fallback_done"}
fallback_decision = netops.fallback_decision


def tick(samples: List[dict], state: dict, probe, get_state: Callable[[], dict], act: Callable[[str], object], now: Optional[float] = None, *, service_alive: Callable[[], bool] = client.watcher_alive) -> dict:
    """One iteration. Returns the decision. Never raises."""
    try:
        if netops.capture_active(client.ctx()):                       # built-in Wi-Fi sniffing in monitor mode: no address on Wi-Fi is deliberate
            samples.clear()
            return {"action": "none", "reason": "capture running", "state": state}
        s = probe.sample()
        if now is not None:
            s["t"] = now
        samples.append(s)
        while samples and samples[0]["t"] < s["t"] - (netops.GRACE_S + 90):
            samples.pop(0)
        enabled = bool((get_state() or {}).get("fallback", {}).get("enabled", True))
        dec = netops.fallback_decision(samples, s["t"], state, enabled=enabled)
        state.clear()
        state.update(dec["state"])
        if dec["action"] != "none" and not service_alive():
            try:
                act(dec["action"])
            except Exception as e:                                  # noqa: BLE001
                dec["error"] = str(e)
        return dec
    except Exception as e:                                          # noqa: BLE001
        return {"action": "none", "reason": f"error: {e}", "state": state}


def _act(action: str) -> object:
    op = ACTION_OP.get(action)
    return client.call(op, {}, wait=90) if op else None


def start(get_state: Optional[Callable[[], dict]] = None, *, interval: float = netops.SAMPLE_EVERY_S, probe=None, act: Optional[Callable[[str], object]] = None) -> threading.Thread:
    """Start the daemon thread. `get_state()` returns the network state dict (default: the root-written net-state.json), whose fallback.enabled switches it off."""
    get_state = get_state or client.read_state
    samples: List[dict] = []
    state: dict = {}
    stop = threading.Event()

    def loop() -> None:
        pr = probe or netops.SysProbe(client.ctx())
        while not stop.wait(interval):
            tick(samples, state, pr, get_state, act or _act)
    t = threading.Thread(target=loop, name="net-watchdog", daemon=True)
    t.stop = stop.set                                               # type: ignore[attr-defined]
    t.start()
    return t
