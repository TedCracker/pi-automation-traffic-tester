"""Sniffer pre-flight: what tools, ports, adapters and drives are available, and what would go wrong (plain words)."""
from __future__ import annotations

import os
import subprocess
from typing import List, Optional

from .. import system
from ..gen.node import iface_ipv4
from . import bpf as bpfmod, builtin_wifi, capture as cap


def eth_ports() -> List[dict]:
    out = []
    for i in system.interfaces():
        n = i["name"]
        base = f"/sys/class/net/{n}"
        real = os.path.realpath(base)
        try:
            carrier = open(f"{base}/carrier").read().strip() == "1"
        except OSError:
            carrier = False
        try:
            speed = int(open(f"{base}/speed").read().strip())
        except (OSError, ValueError):
            speed = None
        drv = ""
        try:
            drv = os.path.basename(os.path.realpath(f"{base}/device/driver"))
        except OSError:
            pass
        out.append({"name": n, "mac": i["mac"], "wired": i["wired"], "usb": "/usb" in real, "driver": drv, "carrier": carrier, "speed_mbps": speed,
                    "ip": iface_ipv4(n), "is_bridge": os.path.isdir(f"{base}/bridge")})
    return out


def route_dev(client_ip: Optional[str]) -> Optional[str]:
    """Which port the browser is connected through (so we never bridge away the user's own connection)."""
    if not client_ip or client_ip in ("127.0.0.1", "::1", "testclient"):
        return None
    try:
        r = subprocess.run(["ip", "route", "get", client_ip], capture_output=True, text=True, timeout=3)
        parts = r.stdout.split()
        return parts[parts.index("dev") + 1] if "dev" in parts else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def bridge_legs(profile: dict, ports: Optional[List[dict]] = None) -> tuple:
    """The two ports an inline-tap profile bridges (auto = first built-in wired port + first USB adapter, else the next wired port).
    The ONE resolver: pre-flight, the cut-off guard in the server and the run itself (session) all use it, so they cannot disagree."""
    ports = ports if ports is not None else eth_ports()
    e = profile["sniff"]["eth"]
    usb = [p["name"] for p in ports if p["usb"] and p["wired"]]
    a = e["leg_a"] if e["leg_a"] != "auto" else next((p["name"] for p in ports if p["wired"] and not p["usb"]), None)
    b = e["leg_b"] if e["leg_b"] != "auto" else (usb[0] if usb else next((p["name"] for p in ports if p["wired"] and p["name"] != a), None))
    return a, b


def span_iface(profile: dict, ports: Optional[List[dict]] = None) -> Optional[str]:
    """The port a mirror-port (SPAN) profile captures on (auto = first wired port). Shared by pre-flight and the run, like bridge_legs."""
    name = profile["sniff"]["eth"]["interface"]
    if name and name != "auto":
        return name
    ports = ports if ports is not None else eth_ports()
    return next((p["name"] for p in ports if p["wired"]), None)


def wifi_default_route_dev() -> Optional[str]:
    """Interface carrying the default route (the Pi's internet link)."""
    try:
        r = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=3)
        parts = r.stdout.split()
        return parts[parts.index("dev") + 1] if "dev" in parts else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _builtin_checks(res: dict, c: dict, add, client_ip) -> None:
    st = builtin_wifi.status()
    res["builtin_wifi"] = st
    if builtin_wifi.was_access_point():                                  # first line: this is the thing people trip over
        add("warn", "The Pi's own Wi-Fi network will go off", builtin_wifi.AP_WARNING)
    if st["available"]:
        add("ok", "Nexmon is installed", f"Driver and firmware are ready for kernel {st['kernel']}.")
    else:
        add("bad", "Built-in Wi-Fi cannot sniff yet", st["reason"] or "Nexmon is not available.")
    if st.get("state") == "monitor":
        add("warn", "Built-in Wi-Fi is already in monitor mode", "Use 'Restore normal Wi-Fi' first, or just start: the capture will use the current mode.")
    web = route_dev(client_ip)
    if web and web.startswith("wlan"):
        add("bad", "You are connected through Wi-Fi", f"This page reaches the Pi over {web}. Monitor mode would cut it off. Open the Pi over Ethernet first.")
    inet = wifi_default_route_dev()
    if inet and inet.startswith("wlan"):
        add("warn", "The Pi's internet is on Wi-Fi", builtin_wifi.WARNING)
    if not c["wifi"].get("builtin_ack"):
        add("bad", "Please confirm", "Tick 'I understand normal Wi-Fi goes off while sniffing'.")


def preflight(profile: Optional[dict], client_ip: Optional[str] = None) -> dict:
    tools = cap.find_tools()
    ports = eth_ports()
    wifi = cap.list_wifi()
    res = {"tools": {k: bool(v) for k, v in tools.items() if k != "engine"}, "engine": tools["engine"], "eth": ports, "wifi": wifi, "serial": system.list_ports(),
           "usb": system.usb_drives(), "presets": bpfmod.PRESETS, "client_port": route_dev(client_ip), "checks": [], "unprivileged": os.geteuid() != 0}
    chk: List[dict] = res["checks"]

    def add(state, title, text):
        chk.append({"s": state, "title": title, "text": text})
    if tools["engine"] == "dumpcap":
        add("ok", "Capture tool found", "dumpcap (Wireshark's capture engine) will write pcapng files.")
    elif tools["engine"] == "tcpdump":
        add("warn", "Only tcpdump found", "Captures work but are saved as plain pcap and only one port at a time. Install tshark for pcapng: sudo apt install tshark")
    else:
        add("bad", "No capture tool", "Install it with: sudo apt install tshark tcpdump (the installer does this).")
    if not profile:
        return res
    c = profile["sniff"]
    mode = c["mode"]
    if c["filter"]["bpf"] and mode != "serial":
        err = bpfmod.validate(c["filter"]["bpf"], "IEEE802_11_RADIO" if mode == "wifi" else "EN10MB")
        add("bad" if err else "ok", "Capture filter" if not err else "Capture filter is not valid", err or "The filter compiles.")
    if mode == "eth_bridge":
        e = c["eth"]
        a, b = bridge_legs(profile, ports)
        if not b:
            add("bad", "Second Ethernet port missing", "The inline tap needs a USB 3 gigabit Ethernet adapter next to the built-in port. None found.")
        elif a == b or not a:
            add("bad", "Two different ports needed", "Pick two different wired ports.")
        else:
            add("ok", "Inline ports", f"{a} (built-in) and {b} (USB) will be bridged.")
            if res["client_port"] in (a, b):
                add("bad", "You are connected through one of these ports", f"This page reaches the Pi through {res['client_port']}. Bridging it would cut your connection. Open the Pi over Wi-Fi (or the third port) first.")
            for pname in (a, b):
                pp = next((p for p in ports if p["name"] == pname), None)
                if pp and pp["ip"]:
                    add("warn", f"{pname} has an IP address", "The Pi will stop using that address while the port is part of the tap.")
                if pp and not pp["carrier"]:
                    add("warn", f"{pname} has no cable", "No link yet: plug both cables in.")
    elif mode == "eth_span":
        iface = span_iface(profile, ports)
        pp = next((p for p in ports if p["name"] == iface), None)
        if not pp:
            add("bad", "Network port not found", "Pick the port that is connected to the switch's mirror port.")
        else:
            add("ok", "Mirror port", f"Capturing on {iface}" + (f" ({pp['speed_mbps']} Mb/s link)" if pp["speed_mbps"] else "") + ".")
            if not pp["carrier"]:
                add("warn", f"{iface} has no cable", "No link yet: connect it to the switch's mirror port.")
            if pp["ip"] and iface == res["client_port"]:
                add("warn", "This is the port you are using", "Capturing here also shows your own browser traffic (hide it with the filter 'Hide this Pi's web page').")
    elif mode == "serial":
        chans = [ch for ch in c["serial"]["channels"] if ch["port"]]
        if not chans:
            add("bad", "No serial port chosen", "Choose the USB-serial adapter(s) wired to the line.")
        for ch in chans:
            sc = system.selfcheck(ch["port"]) if ch["port"] else None
            if sc and sc["ftdi"] and not sc["ok"]:
                add("warn", f"{ch['port']}: slow USB adapter", sc["message"] + " Timestamps will be off by up to that much.")
            elif sc:
                add("ok", f"{ch['port']}", sc["message"])
        if len({ch["port"] for ch in chans}) != len(chans):
            add("bad", "Same port twice", "Each direction needs its own adapter.")
    elif mode == "wifi":
        cands = [w for w in wifi if w.get("monitor_capable")]
        if not tools["iw"]:
            add("bad", "iw not installed", "sudo apt install iw")
        elif c["wifi"]["interface"] == "builtin":
            _builtin_checks(res, c, add, client_ip)
        elif c["wifi"]["interface"] == "auto" and not cands:
            add("bad", "No monitor-mode Wi-Fi adapter", "Plug in a USB adapter such as the Alfa AWUS036ACM (the reliable choice). The Pi's own Wi-Fi can be used with Nexmon instead (experimental): choose 'Built-in Wi-Fi' as the adapter.")
        else:
            add("ok", "Wi-Fi adapter", ", ".join(f"{w['name']} ({w['driver']})" for w in (cands or wifi)) or "found")
            if cands and any(x.get("built_in") for x in wifi):
                add("ok", "USB adapter preferred", "A USB monitor adapter is plugged in, so the built-in Wi-Fi (and your internet over it) stays on.")
    if c["storage"]["usb"] and not res["usb"]:
        add("bad", "No USB drive", "'Save to USB drive' is on but no drive is plugged in.")
    elif res["usb"] and c["storage"]["usb"]:
        add("ok", "USB drive", f"{res['usb'][0]['path']} ({res['usb'][0]['free_mb']} MB free)")
    return res
