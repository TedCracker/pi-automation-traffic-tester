"""path_warning(host): would a test's traffic to `host` go over a link that is wrong for testing (the Pi's own access point, the management Wi-Fi, or a
port labelled Internet only)? Plain words, or None when the path is fine."""
from __future__ import annotations

import ipaddress
import socket
import threading
from typing import Callable, Optional

from . import client


def _resolve(host: str, timeout: float = 1.5) -> Optional[str]:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not host or host.startswith("/") or " " in host or len(host) > 253:
        return None
    out: list = []

    def work() -> None:
        try:
            out.append(socket.gethostbyname(host))
        except OSError:
            pass
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return out[0] if out else None


def _route_dev(ip: str) -> Optional[str]:
    from ..sniff import preflight
    return preflight.route_dev(ip)


def path_warning(host: Optional[str], *, route_dev: Optional[Callable[[str], Optional[str]]] = None, state: Optional[dict] = None, resolve: Optional[Callable[[str], Optional[str]]] = None) -> Optional[str]:
    """None when the route to `host` leaves through a normal test port. `route_dev`, `state` and `resolve` are injectable for tests."""
    if not host or not isinstance(host, str):
        return None
    ip = (resolve or _resolve)(host.strip())
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.is_loopback or addr.is_unspecified:
        return None
    dev = (route_dev or _route_dev)(ip)
    if not dev or dev == "lo":
        return None
    st = state if state is not None else client.read_state()
    base = dev.split(".")[0]
    labels = st.get("labels") or {}
    mode = st.get("wifi_mode")
    wifi_dev = st.get("wifi_iface")
    is_wifi = base == wifi_dev or base.startswith(("wlan", "wlp", "wl"))
    if is_wifi:
        if mode in ("ap", "fallback") or (st.get("fallback") or {}).get("active"):
            return (f"Traffic to {host} would travel over the Pi's own Wi-Fi access point ({dev}). That radio is for you to reach the Pi, not for testing, and other phones on it "
                    "add delay and noise. Connect the device under test to an Ethernet port labelled Test instead.")
        return (f"Traffic to {host} would travel over the Pi's Wi-Fi ({dev}), which is its management and internet link. Test results over it include Wi-Fi delay and jitter. "
                "Use an Ethernet port labelled Test for the device under test.")
    if labels.get(base) == "internet":
        return (f"Traffic to {host} would leave through {dev}, which is labelled Internet only. Label it Test (or Internet + Test) in Settings → Network, or use another port, "
                "so management traffic does not mix with the test.")
    return None
