"""Host helpers: serial port discovery, FTDI latency timer, clock sync, USB drives, netem (docs/SPEC.md 5, 7)."""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional

UDEV_RULE = 'ACTION=="add", SUBSYSTEM=="usb-serial", DRIVER=="ftdi_sio", ATTR{latency_timer}="1"'
_PATTERNS = ["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA*", "/dev/serial0", "/dev/ttyS*"]


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _usb_info(tty: str) -> Dict[str, Optional[str]]:
    """Walk sysfs from /sys/class/tty/<tty>/device up to the USB device for vendor/product/serial."""
    name = os.path.basename(tty)
    dev = f"/sys/class/tty/{name}/device"
    if not os.path.exists(dev):
        return {}
    d = os.path.realpath(dev)
    for _ in range(6):
        if os.path.exists(os.path.join(d, "idVendor")):
            return {"vendor": _read(os.path.join(d, "manufacturer")) or _read(os.path.join(d, "idVendor")),
                    "product": _read(os.path.join(d, "product")),
                    "serial": _read(os.path.join(d, "serial")),
                    "vid": _read(os.path.join(d, "idVendor")), "pid": _read(os.path.join(d, "idProduct"))}
        d = os.path.dirname(d)
    return {}


def ftdi_latency(tty: str) -> Optional[int]:
    name = os.path.basename(tty)
    for base in (f"/sys/bus/usb-serial/devices/{name}/latency_timer",
                 f"/sys/class/tty/{name}/device/latency_timer"):
        v = _read(base)
        if v is not None and v.isdigit():
            return int(v)
    return None


def _openable(path: str) -> bool:
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        os.close(fd)
        return True
    except OSError:
        return False


# Raspberry Pi 5 serial ports (RP1 chip). Verify on the real Pi: ls -l /dev/serial* /dev/ttyAMA*; pinctrl get 14,15
PI5_PORTS = {
    "ttyAMA10": ("debug", "Pi 5 debug connector (3-pin JST)"),
    "ttyAMA0": ("gpio", "GPIO 14/15 UART0, pins 8/10 (needs dtparam=uart0=on)"),
    "ttyAMA1": ("gpio", "GPIO UART1 (needs overlay uart1-pi5)"),
    "ttyAMA2": ("gpio", "GPIO UART2 (needs overlay uart2-pi5)"),
    "ttyAMA3": ("gpio", "GPIO UART3 (needs overlay uart3-pi5)"),
    "ttyAMA4": ("gpio", "GPIO UART4 (needs overlay uart4-pi5)"),
}
_KIND_RANK = {"usb": 0, "gpio": 1, "acm": 2, "other": 3, "debug": 4, "ttyS": 5}


def pi_model() -> Dict[str, object]:
    """Which Raspberry Pi this is (from the device tree). TT_PI_MODEL overrides it for tests."""
    model = os.environ.get("TT_PI_MODEL")
    if model is None:
        model = _read(os.environ.get("TT_MODEL_FILE", "/proc/device-tree/model"))
    model = (model or "").replace("\x00", "").strip() or None
    return {"model": model, "is_pi": bool(model and "Raspberry Pi" in model), "is_pi5": bool(model and "Raspberry Pi 5" in model)}


def _port_kind(path: str, usb: bool) -> str:
    name = os.path.basename(path)
    if usb:
        return "usb"
    if name in PI5_PORTS:
        return PI5_PORTS[name][0]
    if name.startswith("ttyACM"):
        return "acm"
    if name.startswith("ttyS"):
        return "ttyS"
    return "other"


def list_ports() -> List[dict]:
    seen, out = set(), []
    for pat in _PATTERNS:
        for p in sorted(glob.glob(pat)):
            rp = os.path.realpath(p)
            if rp in seen:
                continue
            if "ttyS" in p and not _openable(p):      # many ttyS* are phantom UARTs
                continue
            seen.add(rp)
            info = _usb_info(p)
            lt = ftdi_latency(p)
            name = os.path.basename(rp)
            kind = _port_kind(rp, bool(info))
            label = " ".join(x for x in (info.get("vendor"), info.get("product")) if x)
            if not label:
                label = PI5_PORTS[name][1] if name in PI5_PORTS else "Built-in serial port"
            if info.get("serial"):
                label += f" ({info['serial']})"
            if os.path.exists("/dev/serial0") and os.path.realpath("/dev/serial0") == rp:
                label += " · also /dev/serial0"
            out.append({"path": p, "label": label, "usb": bool(info), "kind": kind, "debug_connector": kind == "debug",
                        "latency_timer_ms": lt, "adapter_slow": lt is not None and lt > 1, "openable": _openable(p)})
    for extra in filter(None, os.environ.get("TT_EXTRA_SERIAL_PORTS", "").split(":")):       # test hook: virtual ports (pty pairs) in screenshots/tests
        if extra not in [o["path"] for o in out]:
            out.append({"path": extra, "label": "Test serial port", "usb": False, "kind": "other", "debug_connector": False, "latency_timer_ms": None, "adapter_slow": False, "openable": True})
    out.sort(key=lambda x: (_KIND_RANK.get(x["kind"], 3), x["path"]))
    return out


def selfcheck(port: str, fix: bool = False) -> dict:
    """FTDI latency_timer check (+fix). Non-FTDI / pty ports report 'not applicable'."""
    lt = ftdi_latency(port)
    res = {"port": port, "ftdi": lt is not None, "latency_timer_ms": lt, "fixed": False,
           "udev_rule": UDEV_RULE, "message": ""}
    if lt is None:
        res["message"] = "Not an FTDI USB adapter (or not a USB port): no latency timer to check."
        res["ok"] = True
        return res
    if lt <= 1:
        res["message"] = f"Adapter latency timer is {lt} ms - good."
        res["ok"] = True
        return res
    res["ok"] = False
    res["message"] = f"Adapter holds data up to {lt} ms before passing it on (adds up to {lt} ms per side)."
    if fix:
        name = os.path.basename(port)
        try:
            with open(f"/sys/bus/usb-serial/devices/{name}/latency_timer", "w") as f:
                f.write("1")
            res.update(fixed=True, ok=True, latency_timer_ms=ftdi_latency(port),
                       message="Latency timer set to 1 ms. It resets on unplug - install the udev rule (install-pi.sh does this) to keep it.")
        except OSError as e:
            res["message"] += f" Could not change it automatically ({e.strerror}). Run install-pi.sh or add this udev rule: {UDEV_RULE}"
    return res


# ---- clock sync -------------------------------------------------------------
_clock_cache = (0.0, None)
_clock_refreshing = threading.Lock()
CLOCK_TRUST_S = 30.0       # a reading older than this is never used to decide anything: the caller waits for a fresh one


def ref_ip_from_id(refid: str) -> str:
    """chrony's reference ID of an IPv4 time server is the address itself in hex (C00002DA = 192.0.2.218); other kinds (local clock, GPS) give ""."""
    if not re.fullmatch(r"[0-9A-Fa-f]{8}", refid or "") or refid.upper() == "7F7F0101":
        return ""
    b = bytes.fromhex(refid)
    return "" if b[0] in (0, 0x7F) else ".".join(str(x) for x in b)


def assume_synced() -> bool:
    """Same-host tests only (TT_ASSUME_SYNCED=1): every clock is the same clock."""
    return os.environ.get("TT_ASSUME_SYNCED") == "1"


def _clock_probe() -> dict:
    """Ask chrony (or timedatectl) once. Starts a child process: several milliseconds on a Pi, so never call it from a timing thread."""
    st = {"synced": False, "offset_ms": None, "source": "none"}
    if os.environ.get("TT_ASSUME_SYNCED") == "1":
        return {"synced": True, "offset_ms": 0.0, "source": "same-host test (TT_ASSUME_SYNCED)", "ref_ip": "", "ref_local": False}
    if shutil.which("chronyc"):
        try:
            out = subprocess.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"System time\s*:\s*([\d.eE+-]+) seconds (fast|slow)", out)
            leap = re.search(r"Leap status\s*:\s*(.+)", out)
            if m:
                off = float(m.group(1)) * 1000.0 * (1 if m.group(2) == "fast" else -1)
                ok = bool(leap) and "Not synchronised" not in leap.group(1)
                st = {"synced": ok, "offset_ms": off, "source": "chrony"}
                ref = re.search(r"Reference ID\s*:\s*([0-9A-Fa-f]+)\s*\(([^)]*)\)", out)
                strat = re.search(r"Stratum\s*:\s*(\d+)", out)
                if ref:             # which time source this clock follows (two Pis that each follow their own local clock are NOT in step)
                    ip = ref.group(2).strip()
                    st["ref_local"] = ref.group(1).upper() == "7F7F0101"
                    st["ref_ip"] = ip if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) else ref_ip_from_id(ref.group(1))        # chrony may print the server's NAME in the brackets
                if strat:
                    st["stratum"] = int(strat.group(1))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    if st["source"] == "none" and shutil.which("timedatectl"):
        try:
            out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
            st = {"synced": out == "yes", "offset_ms": None, "source": "timedatectl (offset unknown)"}
        except (OSError, subprocess.SubprocessError):
            pass
    return st


def _clock_refresh() -> None:
    global _clock_cache
    try:
        _clock_cache = (time.monotonic(), _clock_probe())
    finally:
        _clock_refreshing.release()


def clock_state(max_age: float = 5.0) -> dict:
    """{'synced': bool, 'offset_ms': float|None, 'source': str}. TT_ASSUME_SYNCED=1 is for same-host tests only.

    The Tester and Responder call this for every message. A reading younger than `max_age` is returned as is; an older one
    is still returned at once while a background thread fetches a new one, so the probe never delays a message (it used to
    add its own run time to one echo every 5 s). Only a missing or very old reading (CLOCK_TRUST_S), or max_age=0, waits."""
    global _clock_cache
    t, st = _clock_cache
    age = time.monotonic() - t
    if st and age < max_age:
        return st
    if st and max_age > 0 and age < CLOCK_TRUST_S:
        if _clock_refreshing.acquire(blocking=False):
            threading.Thread(target=_clock_refresh, name="clock-probe", daemon=True).start()
        return st
    st = _clock_probe()
    _clock_cache = (time.monotonic(), st)
    return st


def clock_offset_us() -> int:
    st = clock_state()
    return int(round((st["offset_ms"] or 0.0) * 1000)) if st["offset_ms"] is not None else 0


def clock_ok(max_offset_ms: float) -> bool:
    st = clock_state()
    return bool(st["synced"]) and st["offset_ms"] is not None and abs(st["offset_ms"]) <= max_offset_ms


# ---- USB flash drives ------------------------------------------------------
def usb_drives() -> List[dict]:
    out = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                dev, mnt, fs = line.split()[:3]
                mnt = mnt.replace("\\040", " ")
                if (mnt.startswith("/media/") or mnt.startswith("/run/media/")) and os.access(mnt, os.W_OK):
                    st = os.statvfs(mnt)
                    out.append({"path": mnt, "device": dev, "fs": fs, "free_mb": int(st.f_bavail * st.f_frsize / 1e6)})
    except OSError:
        pass
    extra = os.environ.get("TT_USB_DIR")       # test hook: pretend this directory is a flash drive
    if extra and os.path.isdir(extra):
        out.append({"path": extra, "device": "test", "fs": "test", "free_mb": 0})
    return out


# ---- netem -----------------------------------------------------------------
def can_netem() -> dict:
    if not shutil.which("tc"):
        return {"ok": False, "reason": "tc (iproute2) is not installed."}
    if os.geteuid() == 0:
        return {"ok": True, "reason": ""}
    try:
        import ctypes
        # CAP_NET_ADMIN effective?
        for line in open("/proc/self/status"):
            if line.startswith("CapEff:"):
                if int(line.split()[1], 16) & (1 << 12):
                    return {"ok": True, "reason": ""}
    except Exception:
        pass
    return {"ok": False, "reason": "Needs root or CAP_NET_ADMIN (the installed service has it)."}


def netem_apply(iface: str, args: str, seed: Optional[int] = None) -> bool:
    """Apply netem. Returns True when a fixed seed was accepted (repeatable run);
    older iproute2 has no `seed` keyword, in which case it is applied unseeded (False)."""
    base = ["tc", "qdisc", "replace", "dev", iface, "root", "netem"] + args.split()
    if seed is not None:
        r = subprocess.run(base + ["seed", str(seed)], capture_output=True, text=True)
        if r.returncode == 0:
            return True
    subprocess.run(base, check=True, capture_output=True, text=True)
    return False


def netem_clear(iface: str) -> None:
    subprocess.run(["tc", "qdisc", "del", "dev", iface, "root"], capture_output=True, text=True)


NET_ROOT = "/sys/class/net"                                # (a test points this at a made-up tree)


def _is_ethernet(p: str) -> bool:
    """An Ethernet-type port (ARPHRD_ETHER = 1). A VPN / tunnel device such as tail0 or tun0 has no MAC and no Layer 2, so raw frames,
    GOOSE and a capture "port" make no sense on it. (An unreadable type counts as Ethernet, as before.)"""
    return _read(os.path.join(p, "type")) in ("1", None)


def interfaces() -> List[dict]:
    out = []
    for p in sorted(glob.glob(NET_ROOT + "/*")):
        n = os.path.basename(p)
        if n == "lo":
            continue
        out.append({"name": n, "wired": not os.path.exists(os.path.join(p, "wireless")) and not n.startswith(("wl", "docker", "br-", "veth")) and _is_ethernet(p),
                    "up": _read(os.path.join(p, "operstate")) in ("up", "unknown"), "mac": _read(os.path.join(p, "address"))})
    return out


# ---------------------------------------------------------------- the test ports of this Pi (New test, row 1)
def iface_ip(iface: str) -> str:
    """The first IPv4 address of a network port ("" = none / unknown)."""
    if not iface or iface == "auto":
        return ""
    forced = os.environ.get("TT_TEST_IFACE_IPS", "")          # test hook: "eth0=10.0.0.1,wlan0=10.0.1.1"
    for part in filter(None, forced.split(",")):
        k, _, v = part.partition("=")
        if k == iface:
            return v
    try:
        import fcntl
        import socket
        import struct
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", iface[:15].encode()))[20:24])   # SIOCGIFADDR
        finally:
            s.close()
    except (OSError, ValueError, ImportError):
        return ""


def _net_kind(name: str) -> str:
    base = f"/sys/class/net/{name}"
    if os.path.isdir(base + "/wireless") or name.startswith("wl"):
        return "wifi"
    try:
        dev = os.path.realpath(base + "/device")
    except OSError:
        dev = ""
    return "usb_eth" if "/usb" in dev else "eth"


def _wifi_ap(name: str) -> str:
    """The network name this Wi-Fi port serves as an access point ("" = it is not one / cannot tell)."""
    try:
        r = subprocess.run(["iw", "dev", name, "info"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    if "type AP" not in r.stdout:
        return ""
    m = re.search(r"^\s*ssid (.+)$", r.stdout, re.M)
    return m.group(1).strip() if m else "on"


PORT_KIND_NAMES = {"serial": "Serial", "eth": "Ethernet", "usb_eth": "USB Ethernet", "wifi": "Wi-Fi"}


def test_ports(client_ip: Optional[str] = None) -> List[dict]:
    """Every physical port a test can use, as a test set lists its ports: serial adapters, Ethernet, USB Ethernet, Wi-Fi, with live state.
    -> [{"id", "kind": serial|eth|usb_eth|wifi, "name", "label", "link", "state", "ip", "speed_mbps", "you_are_here", "warn"}]"""
    out: List[dict] = []
    here = route_dev(client_ip) if client_ip else None
    for i in interfaces():
        n = i["name"]
        if n.startswith(("docker", "br-", "veth", "virbr", "tail", "tun", "wg", "dummy", "bond", "sit", "ip6", "mon")) or n.endswith("mon"):
            continue
        kind = _net_kind(n)
        ip = iface_ip(n)
        speed = None
        if kind != "wifi":
            try:
                v = int(_read(f"/sys/class/net/{n}/speed") or -1)
                speed = v if v > 0 else None
            except ValueError:
                speed = None
        carrier = _read(f"/sys/class/net/{n}/carrier")
        link = (carrier == "1") if carrier is not None else bool(i["up"])
        ap = _wifi_ap(n) if kind == "wifi" else ""
        bits = []
        if kind == "wifi":
            bits.append(f"access point \u201c{ap}\u201d" if ap and ap != "on" else "access point" if ap else ("connected" if link else "not connected"))
        else:
            bits.append("link up" if link else "no link")
            if link and speed:
                bits.append(f"{speed / 1000:g} Gb/s" if speed >= 1000 else f"{speed} Mb/s")
        if ip:
            bits.append(ip)
        elif link:
            bits.append("no IP address")
        out.append({"id": n, "kind": kind, "name": n, "label": PORT_KIND_NAMES[kind], "link": link, "ip": ip, "speed_mbps": speed, "mac": i.get("mac"),
                    "state": " \u00b7 ".join(bits), "you_are_here": bool(here and here == n), "ap": bool(ap), "warn": ""})
    for sp in list_ports():
        if sp.get("debug_connector") or not sp.get("openable"):
            continue
        name = os.path.basename(sp["path"])
        what = "USB serial" if sp.get("usb") else "GPIO serial" if sp.get("kind") in ("gpio", "uart") or name.startswith("ttyAMA") or name == "serial0" else "Serial"
        out.append({"id": sp["path"], "kind": "serial", "name": name, "label": what, "link": True, "ip": "", "speed_mbps": None,
                    "state": sp["label"] + (" \u00b7 adds up to %d ms" % sp["latency_timer_ms"] if sp.get("adapter_slow") else " \u00b7 ready"),
                    "you_are_here": False, "ap": False, "adapter_slow": bool(sp.get("adapter_slow")), "warn": ""})
    rank = {"eth": 0, "usb_eth": 1, "wifi": 2, "serial": 3}
    out.sort(key=lambda p: (rank[p["kind"]], not p["link"], p["name"]))
    return out


# ---------------------------------------------------------------- Wi-Fi power saving (adds wake-up delay that spoils idle-gap results)
def route_dev(host: Optional[str]) -> Optional[str]:
    """Which interface the traffic to `host` leaves through (None when unknown / loopback)."""
    if not host or host.startswith("127.") or host in ("localhost", "::1"):
        return None
    try:
        r = subprocess.run(["ip", "route", "get", host], capture_output=True, text=True, timeout=3)
        parts = r.stdout.split()
        return parts[parts.index("dev") + 1] if "dev" in parts else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def is_wifi(iface: Optional[str]) -> bool:
    return bool(iface) and os.path.isdir(f"/sys/class/net/{iface}/wireless")


def wifi_power_save(iface: str) -> Optional[bool]:
    """True = Wi-Fi power saving is ON for this interface, False = off, None = cannot tell (no `iw`, not Wi-Fi)."""
    if not iface or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", iface) or not shutil.which("iw"):
        return None
    try:
        r = subprocess.run(["iw", "dev", iface, "get", "power_save"], capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"power save:\s*(on|off)", r.stdout, re.I)
    return None if not m else m.group(1).lower() == "on"


def wifi_path(host: Optional[str]) -> Optional[dict]:
    """Description of the Wi-Fi path to `host`, or None when the traffic does not go over Wi-Fi."""
    dev = route_dev(host)
    if not dev or not is_wifi(dev):
        return None
    return {"interface": dev, "power_save": wifi_power_save(dev)}


def wifi_powersave_off(iface: str) -> dict:
    """Turn Wi-Fi power saving off now (`iw`, needs the service's network permission) and, when NetworkManager allows it, for good
    (`802-11-wireless.powersave 2` on the active connection). {"ok", "persistent", "message"}"""
    if not is_wifi(iface) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", iface or ""):
        return {"ok": False, "persistent": False, "message": f"{iface or 'That port'} is not a Wi-Fi interface."}
    persistent, now_ok, why = False, False, ""
    if shutil.which("iw"):
        r = subprocess.run(["iw", "dev", iface, "set", "power_save", "off"], capture_output=True, text=True, timeout=6)
        now_ok = r.returncode == 0 and wifi_power_save(iface) is False
        why = (r.stderr or r.stdout).strip()
    if shutil.which("nmcli"):
        try:
            r = subprocess.run(["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"], capture_output=True, text=True, timeout=6)
            name = next((ln.rsplit(":", 1)[0] for ln in r.stdout.splitlines() if ln.rsplit(":", 1)[-1] == iface), "")
            if name:
                m = subprocess.run(["nmcli", "connection", "modify", name, "802-11-wireless.powersave", "2"], capture_output=True, text=True, timeout=10)
                persistent = m.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    if now_ok or persistent:
        msg = (f"Wi-Fi power saving is off on {iface}." + (" It stays off after a reboot." if persistent else
               " The app turns it off again every time it starts and before each test; for a permanent change run: sudo nmcli connection modify \"<your Wi-Fi name>\" 802-11-wireless.powersave 2"))
        return {"ok": True, "persistent": persistent, "message": msg}
    return {"ok": False, "persistent": False, "message": "Could not turn Wi-Fi power saving off" + (f": {why}" if why else ".") + " This needs the app's network permission (the full install has it)."}
