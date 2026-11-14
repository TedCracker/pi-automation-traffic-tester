"""Build a transport from a profile."""
from __future__ import annotations

from . import system
from .transports import SerialTransport, TcpTransport, Transport, UdpTransport


class ConfigError(Exception):
    pass


def resolve_serial_port(port: str) -> str:
    if port and port != "auto":
        return port
    # list_ports() is ordered: USB adapter, GPIO UART (ttyAMA0-4), ACM, others, Pi 5 debug connector (ttyAMA10), ttyS
    pick = [p for p in system.list_ports() if p["openable"]]
    if not pick:
        raise ConfigError("No serial port found. Plug in the USB-serial adapter (or pick a port manually).")
    return pick[0]["path"]


def make_transport(p: dict, role: str) -> Transport:
    t = p["transport"]
    if t == "serial":
        s = p["serial"]
        return SerialTransport(resolve_serial_port(s["port"]), s["baud"], s["bytesize"], s["parity"], s["stopbits"])
    n = p["net"]
    if t == "udp":
        if role == "tester" and not n["peer_host"]:
            raise ConfigError("Pick the Pi to send to, or type its IP address.")
        return UdpTransport(role, n["peer_host"], n["port"], n["dscp"], iface=(n.get("iface") or "") if role == "tester" else "")
    if t == "tcp":
        if role == "tester" and not n["peer_host"]:
            raise ConfigError("Pick the Pi to send to, or type its IP address.")
        # connect timeout follows the test's own "give up after" time (0.3-3 s): a fixed 300 ms could never connect over a slow link
        return TcpTransport(role, n["peer_host"], n["port"], n["dscp"], n["keepalive"], n["keepalive_idle_s"],
                            connect_timeout_s=min(3.0, max(0.3, p.get("timeout_ms", 300) / 1000.0)), iface=(n.get("iface") or "") if role == "tester" else "")
    if t in ("goose_l2", "goose_l3"):
        from .goose import GooseTransport
        return GooseTransport(role, t, p["goose"])
    raise ConfigError(f"Unknown transport {t}")


def port_label(p: dict) -> str:
    t = p["transport"]
    if t == "serial":
        return p["serial"]["port"]
    if t in ("udp", "tcp"):
        return f"{p['net']['peer_host'] or '*'}:{p['net']['port']}"
    return p["goose"]["interface"]
