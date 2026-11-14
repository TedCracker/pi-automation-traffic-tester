"""Capture-filter helper: presets, builder and validation (compiled with tcpdump -d / dumpcap)."""
from __future__ import annotations

import re
import shutil
import subprocess
from typing import Dict, List, Optional

PRESETS: List[dict] = [
    {"id": "all", "name": "Everything", "expr": "", "about": "No filter: every frame."},
    {"id": "dnp3", "name": "Only DNP3", "expr": "tcp port 20000 or udp port 20000", "about": "DNP3 over TCP or UDP (port 20000)."},
    {"id": "modbus", "name": "Only Modbus TCP", "expr": "tcp port 502", "about": "Modbus/TCP (port 502)."},
    {"id": "goose", "name": "Only GOOSE", "expr": "ether proto 0x88b8", "about": "IEC 61850 GOOSE (Layer 2)."},
    {"id": "rgoose", "name": "Only R-GOOSE", "expr": "udp port 102", "about": "Routable GOOSE over UDP port 102."},
    {"id": "arp", "name": "Only ARP", "expr": "arp", "about": "ARP requests and replies."},
    {"id": "icmp", "name": "Only ping (ICMP)", "expr": "icmp or icmp6", "about": "ICMP / ICMPv6."},
    {"id": "no_web", "name": "Hide this Pi's web page", "expr": "not tcp port 8080", "about": "Leaves out the app's own web traffic (port 8080)."},
    {"id": "ptp", "name": "Only PTP / time", "expr": "udp port 319 or udp port 320 or ether proto 0x88f7 or udp port 123", "about": "PTP and NTP time protocols."},
]
IP_RE = re.compile(r"^[0-9a-fA-F:.]+$")
MAC_RE = re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")
LEADING_DASH = "A capture filter cannot start with '-'."


def build(kind: str, value: str = "") -> str:
    """Build an expression from a simple choice: host / mac / vlan / port / preset id."""
    v = value.strip()
    if kind == "host":
        if not IP_RE.match(v):
            raise ValueError("Enter an IP address, for example 192.168.1.50.")
        return f"host {v}"
    if kind == "mac":
        if not MAC_RE.match(v):
            raise ValueError("Enter a MAC address like 02:00:00:00:00:01.")
        return f"ether host {v.replace('-', ':')}"
    if kind == "vlan":
        if not v.isdigit() or not 0 <= int(v) <= 4094:
            raise ValueError("VLAN id must be a number from 0 to 4094.")
        return f"vlan {v}"
    if kind == "port":
        if not v.isdigit() or not 1 <= int(v) <= 65535:
            raise ValueError("Port must be a number from 1 to 65535.")
        return f"port {v}"
    for p in PRESETS:
        if p["id"] == kind:
            return p["expr"]
    raise ValueError(f"Unknown filter choice '{kind}'.")


def validate(expr: str, linktype: str = "EN10MB") -> Optional[str]:
    """None = ok (or cannot check); otherwise a plain error message."""
    expr = (expr or "").strip()
    if not expr:
        return None
    if expr.startswith("-"):                     # would be read as a tcpdump option (-V file, -z program ...), never a filter
        return LEADING_DASH
    if len(expr) > 600 or re.search(r"[;`$|&<>\n\r]", expr.replace("&&", "").replace("||", "")):
        return "That filter contains characters that are not allowed."
    tool = shutil.which("tcpdump")
    if not tool:
        return None
    try:
        r = subprocess.run([tool, "-d", "-y", linktype, "--", expr], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode:
        msg = (r.stderr or "").strip().splitlines()
        msg = msg[-1] if msg else "invalid filter"
        msg = msg.replace("tcpdump: ", "")
        return f"The capture filter is not valid: {msg}"
    return None
