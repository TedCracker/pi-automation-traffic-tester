"""What the Network card shows: the saved settings (root-written net-state.json) merged with the live link, speed, address and the port the browser uses."""
from __future__ import annotations

import socket
from typing import Callable, List, Optional

from ..update import netops
from . import client


LABEL_WORDS = {"internet": "Internet", "test": "Test", "both": "Internet + Test"}


def _live_ports() -> List[dict]:
    from ..sniff import preflight
    return preflight.eth_ports()


def _route_dev(ip: Optional[str]) -> Optional[str]:
    from ..sniff import preflight
    return preflight.route_dev(ip)


def base_iface(dev: Optional[str]) -> Optional[str]:
    return dev.split(".")[0] if dev else dev


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "raspberrypi"


def pi_name(get_setting: Optional[Callable] = None) -> str:
    v = None
    try:
        v = get_setting("pi_name") if get_setting else None
    except Exception:
        v = None
    return v if isinstance(v, str) and v.strip() else hostname()


def _nm_prop(c: "netops.Ctx", name: str, fields: str) -> List[str]:
    r = c.nm("-g", fields, "connection", "show", "id", name, timeout=10)
    return r.out.splitlines() if r.ok else []


def _wifi_live(c: "netops.Ctx", pub: dict) -> dict:
    live = {"radio": "unknown", "iface": pub.get("wifi_iface"), "active_name": None, "active_kind": None, "ssid": None, "present": False}
    try:
        live["iface"] = live["iface"] or netops.wifi_iface(c, required=False)
        live["present"] = bool(live["iface"])
        if not live["present"]:
            return live
        live["radio"] = "off" if netops.nm_radio(c) == "disabled" else "on"
        for a in netops.nm_active(c):
            if a["type"] == "wifi" and a["device"] == live["iface"]:
                live["active_name"] = a["name"]
                vals = _nm_prop(c, a["name"], "802-11-wireless.ssid,802-11-wireless.mode")
                live["ssid"] = vals[0] if vals else a["name"]
                live["active_kind"] = "ap" if (vals[1:2] == ["ap"] or a["name"] in (netops.PROFILE_AP, netops.PROFILE_FALLBACK)) else "client"
    except Exception:
        pass
    return live


def build_config(get_setting: Optional[Callable] = None, client_ip: Optional[str] = None, *, ports_fn: Optional[Callable] = None, route_dev_fn: Optional[Callable] = None) -> dict:
    avail, why = client.available()
    pub = client.read_state()
    c = client.ctx()
    live = _wifi_live(c, pub)
    ports = (ports_fn or _live_ports)()
    dev = (route_dev_fn or _route_dev)(client_ip)
    prof = pub.get("profiles") or {}
    ap = prof.get(netops.PROFILE_AP) or {}
    fallback_up = live["active_name"] == netops.PROFILE_FALLBACK or bool(pub["fallback"].get("active"))
    mode = pub.get("wifi_mode")
    if fallback_up:
        mode = "fallback"
    elif live["active_kind"] == "ap":
        mode = "ap"
    elif mode is None:                                                  # an install from before this feature: derive it from what NetworkManager does
        mode = "off" if live["radio"] == "off" else ("client" if live["active_kind"] == "client" else "unknown")
    wifi = {"present": live["present"], "iface": live["iface"], "mode": mode, "radio": live["radio"], "ssid": (ap.get("ssid") if mode == "ap" and ap else live["ssid"]),
            "ap": {"ssid": ap.get("ssid"), "band": ap.get("band", "bg"), "channel": ap.get("channel", 6), "has_password": bool(ap.get("has_password")), "url": f"http://{netops.AP_GATEWAY}:8080"},
            "joined": live["ssid"] if live["active_kind"] == "client" else None,
            "clients_saved": sorted({v["ssid"] for v in prof.values() if v.get("kind") == "client"})}
    out_ports = []
    for p in ports:
        if not p.get("wired") or p.get("is_bridge"):
            continue
        n = p["name"]
        sp = prof.get(f"port:{n}")
        out_ports.append({"iface": n, "usb": bool(p.get("usb")), "link": bool(p.get("carrier")), "speed_mbps": p.get("speed_mbps"), "ip": p.get("ip"), "driver": p.get("driver"),
                          "saved": bool(sp), "method": (sp or {}).get("method", "dhcp"), "address": (sp or {}).get("address"), "gateway": (sp or {}).get("gateway"),
                          "dns": (sp or {}).get("dns", []), "vlan": (sp or {}).get("vlan"), "label": (sp or {}).get("label", pub.get("labels", {}).get(n, "both")),
                          "mac": p.get("mac"), "you": base_iface(dev) == n})
    through = None
    if dev:
        b = base_iface(dev)
        kind = "wifi" if netops.is_wifi_iface(c, dev) or dev == live["iface"] else ("ethernet" if any(x["iface"] == b for x in out_ports) else "other")
        label = next((x["label"] for x in out_ports if x["iface"] == b), None)
        text = {"wifi": f"{dev} (Wi-Fi" + (", the Pi's own access point" if mode in ("ap", "fallback") else "") + ")",
                "ethernet": f"{dev} (Ethernet" + (f", {LABEL_WORDS[label]}" if label in LABEL_WORDS else "") + ")"}.get(kind, dev)
        through = {"iface": dev, "kind": kind, "label": label, "text": text}
    return {"available": avail, "reason": why, "pi_name": pi_name(get_setting), "hostname": hostname(), "wifi": wifi, "ports": out_ports, "connected_through": through,
            "client_ip": client_ip, "pending": client.read_pending(), "last_event": pub.get("last_event"), "fallback": pub["fallback"], "saved": bool(pub.get("wifi_mode") or prof),
            "ap_url": f"http://{netops.AP_GATEWAY}:8080"}
