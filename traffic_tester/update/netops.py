"""Privileged network + clock operations for the Pi. STANDARD LIBRARY ONLY (it runs as root from the update helper's root-owned copy).

The web app is not root. It writes a request ({"action": "net" | "clock", ...}) and asks systemd to start traffic-tester-update.service; helper.run() hands
the request to handle() here. Everything in the request is validated strictly and turned into nmcli argument LISTS by this module (no shell, no free-form
arguments). Results go to <state_dir>/net-result.json (world readable, never holds a password).

What it does (all with NetworkManager; every profile it creates is named tt-*):
  apply     change Wi-Fi (access point / join a network / off) and Ethernet ports (DHCP or static, VLAN, label Internet/Test/both). A snapshot of the previous
            state is taken first and a revert is armed (systemd-run timer, 90 s) so a change that cuts the user off undoes itself unless they press Keep.
  keep / revert / scan / fallback_ap / fallback_off / fallback_retry / ensure_wifi / set_fallback / setup_default_ap
  clock     serve | client | status for chrony (the peer clock feature)
  watch     (python3 netops.py watch) the dead-man loop: no network for 3 minutes -> bring up the fallback AP (tt-fallback-ap); also fires an expired revert.

Test hooks (only honoured when the variable is in THIS process' environment; the root unit has none): TT_NET_FAKE=1 (in-process fake NetworkManager, fake scheduler,
fake probe), TT_NET_RUNNER=<path> (use that program instead of nmcli), TT_NET_STATE_DIR, TT_NET_SYSFS, TT_CHRONY_CONF, TT_CHRONY_MAIN_CONF, TT_NM_KEYFILE_DIR."""
from __future__ import annotations

import copy
import fcntl
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

if __package__ in (None, ""):          # run as a script (python3 -I netops.py watch): make `traffic_tester.update` importable like helper.py does
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    __package__ = "traffic_tester.update"

SYSTEM_CONF = "/etc/traffic-tester/update.conf"
DEFAULT_STATE_DIR = "/var/lib/traffic-tester-update"
REVERT_UNIT = "tt-net-revert"
REVERT_DEFAULT, REVERT_MIN, REVERT_MAX = 90, 15, 600
PROFILE_AP, PROFILE_FALLBACK = "tt-ap", "tt-fallback-ap"
AP_ADDR, AP_GATEWAY = "10.42.0.1/24", "10.42.0.1"
AP_NET = ipaddress.ip_network("10.42.0.0/24")
DEFAULT_PSK = "password"
PRIVATE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
GRACE_S, RETRY_EVERY_S, RETRY_WINDOW_S = 180, 300, 30          # fallback timing: 3 min without network, retry the normal profiles every 5 min for 30 s
SAMPLE_EVERY_S = 10
CH_BG = tuple(range(1, 14))
CH_A = (36, 40, 44, 48, 149, 153, 157, 161, 165)                # no DFS channels: an access point cannot start on them reliably
LABELS = ("internet", "test", "both")
WIFI_MODES = ("ap", "client", "off")
SECURITIES = ("wpa-psk", "sae", "none")
IFACE_RE = re.compile(r"^[a-z0-9_.-]{1,15}\Z")
TT_NAME_RE = re.compile(r"^tt-[a-z0-9][a-z0-9._-]{0,40}\Z")
IPV4_RE = re.compile(r"^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}\Z")
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}\Z")
F_STATE, F_PRIVATE, F_PENDING, F_SNAP, F_RESULT = "net-state.json", "net-private.json", "net-pending.json", "net-snapshot.json", "net-result.json"
F_LOCK, F_WATCH, F_BEAT = ".net.lock", "net-watch.json", "net-watch.beat"
SKIP_IFACE_PREFIX = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "tail", "wg", "zt", "ttbr", "ifb", "dummy", "sit", "bond")


class NetError(Exception):
    """A refusal or failure with a message a network engineer can act on. `code` is stable for the UI/tests."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ====================================================================== validation (strict; the helper is the only authority)
def v_ssid(s) -> str:
    if not isinstance(s, str) or not s:
        raise NetError("bad_ssid", "Give the network a name (SSID).")
    if len(s.encode("utf-8")) > 32:
        raise NetError("bad_ssid", "A Wi-Fi network name can be at most 32 bytes long (accented letters and emoji count as more than one).")
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        raise NetError("bad_ssid", "The Wi-Fi network name contains a control character; remove it.")
    return s


def v_psk(p) -> str:
    if not isinstance(p, str) or not 8 <= len(p) <= 63:
        raise NetError("bad_password", "The Wi-Fi password must be 8 to 63 characters (WPA2).")
    if any(ord(c) < 32 or ord(c) > 126 for c in p):
        raise NetError("bad_password", "The Wi-Fi password can only use letters, digits, spaces and the usual punctuation (plain ASCII).")
    return p


def v_ipv4(s, what: str = "address") -> ipaddress.IPv4Address:
    if not isinstance(s, str) or not IPV4_RE.match(s):
        raise NetError("bad_ip", f"The {what} '{str(s)[:40]}' is not a valid IPv4 address (like 192.168.1.20).")
    return ipaddress.IPv4Address(s)


def _unicast(ip: ipaddress.IPv4Address, what: str) -> None:
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local or ip.is_reserved or int(ip) == 0xFFFFFFFF:
        raise NetError("bad_ip", f"The {what} {ip} cannot be used (it is not a normal host address).")


def v_prefix(mask) -> int:
    if isinstance(mask, int) and not isinstance(mask, bool):
        n = mask
    elif isinstance(mask, str) and re.fullmatch(r"/?\d{1,2}", mask.strip()):
        n = int(mask.strip().lstrip("/"))
    elif isinstance(mask, str) and IPV4_RE.match(mask.strip()):
        m = int(ipaddress.IPv4Address(mask.strip()))
        inv = (~m) & 0xFFFFFFFF
        if inv & (inv + 1):
            raise NetError("bad_mask", f"The subnet mask '{mask}' is not valid (its 1-bits must be contiguous, like 255.255.255.0).")
        n = bin(m).count("1")
    else:
        raise NetError("bad_mask", f"The subnet mask '{str(mask)[:40]}' is not valid (use 255.255.255.0 or /24).")
    if not 8 <= n <= 30:
        raise NetError("bad_mask", "The subnet mask must be between /8 and /30.")
    return n


def v_address(addr, mask=None) -> Tuple[str, int]:
    """'192.168.1.20/24' or ('192.168.1.20', '255.255.255.0') -> ('192.168.1.20', 24)."""
    if isinstance(addr, str) and "/" in addr:
        a, m = addr.split("/", 1)
        if mask is not None and str(mask).strip() not in ("", m, "/" + m):
            raise NetError("bad_mask", "The address already contains a /prefix; leave the subnet mask empty or make it match.")
        mask = m
    else:
        a = addr
    ip = v_ipv4(a, "IP address")
    if mask is None or mask == "":
        raise NetError("bad_mask", "Enter the subnet mask too (for example 255.255.255.0).")
    n = v_prefix(mask)
    _unicast(ip, "IP address")
    net = ipaddress.ip_network(f"{ip}/{n}", strict=False)
    if ip == net.network_address or ip == net.broadcast_address:
        raise NetError("bad_ip", f"{ip} is the network or broadcast address of {net}; choose a host address inside it.")
    return str(ip), n


def v_gateway(gw, ip: str, prefix: int) -> str:
    g = v_ipv4(gw, "gateway")
    _unicast(g, "gateway")
    net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    if g not in net:
        raise NetError("bad_gateway", f"The gateway {g} is not inside {net}: it must be reachable directly from this port.")
    if str(g) == ip:
        raise NetError("bad_gateway", "The gateway cannot be the Pi's own address.")
    return str(g)


def v_dns(dns) -> List[str]:
    if dns in (None, "", []):
        return []
    if isinstance(dns, str):
        dns = [x for x in re.split(r"[,\s]+", dns.strip()) if x]
    if not isinstance(dns, list) or len(dns) > 3:
        raise NetError("bad_dns", "Give up to three DNS servers (IPv4 addresses).")
    out = []
    for d in dns:
        ip = v_ipv4(d, "DNS server")
        _unicast(ip, "DNS server")
        out.append(str(ip))
    return out


def v_vlan(v) -> Optional[int]:
    if v in (None, "", 0) and not isinstance(v, bool) or v is False:
        return None
    if isinstance(v, bool):
        raise NetError("bad_vlan", "The VLAN id must be a number from 1 to 4094.")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise NetError("bad_vlan", "The VLAN id must be a number from 1 to 4094.")
    if isinstance(v, float) or not 1 <= n <= 4094:
        raise NetError("bad_vlan", "The VLAN id must be a number from 1 to 4094.")
    return n


def v_iface_name(name, what: str = "interface") -> str:
    if not isinstance(name, str) or not IFACE_RE.match(name):
        raise NetError("bad_iface", f"The {what} name '{str(name)[:20]}' is not allowed (lower-case letters, digits, . _ - only, at most 15 characters).")
    return name


def v_label(label) -> str:
    if label not in LABELS:
        raise NetError("bad_label", "The port label must be Internet, Test or Both.")
    return label


def v_channel(band, channel) -> Tuple[str, Optional[int]]:
    band = "bg" if band in (None, "", "2.4", "2.4GHz") else ("a" if band in ("5", "5GHz") else band)
    if band not in ("bg", "a", "auto"):
        raise NetError("bad_band", "The Wi-Fi band must be 2.4 GHz, 5 GHz or automatic.")
    if channel in (None, "", "auto", 0):
        return band, None
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        raise NetError("bad_channel", "The Wi-Fi channel must be a number or 'automatic'.")
    if band == "auto":
        band = "bg" if ch in CH_BG else "a"
    if band == "bg" and ch not in CH_BG:
        raise NetError("bad_channel", "On 2.4 GHz the channel must be 1 to 13.")
    if band == "a" and ch not in CH_A:
        raise NetError("bad_channel", "On 5 GHz an access point can use channels " + ", ".join(map(str, CH_A)) + " (the others need radar detection, which this Wi-Fi chip does not offer for hosting).")
    return band, ch


def is_tt_name(n) -> bool:
    return isinstance(n, str) and bool(TT_NAME_RE.match(n))


def clean_text(s, n: int = 200) -> str:
    return "".join(c for c in str(s) if c >= " " and c != "\x7f")[:n]


# ====================================================================== runners, scheduler, probe, context
class R:
    def __init__(self, rc: int, out: str = "", err: str = ""):
        self.rc, self.out, self.err = rc, out, err

    @property
    def ok(self) -> bool:
        return self.rc == 0


class SubprocessRunner:
    """Runs fixed argument lists (never a shell). nmcli can be replaced by a shim (TT_NET_RUNNER) for container tests."""

    def __init__(self, nmcli: Optional[str] = None):
        self.nmcli = nmcli

    def run(self, argv: List[str], timeout: float = 60) -> R:
        argv = list(argv)
        if argv and argv[0] == "nmcli" and self.nmcli:
            argv[0] = self.nmcli
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            return R(p.returncode, p.stdout, p.stderr)
        except FileNotFoundError:
            return R(127, "", f"{argv[0]}: not found")
        except subprocess.TimeoutExpired:
            return R(124, "", "timed out")


class FakeRunner:
    """In-process fake NetworkManager (netfake) + a recorder for every other command."""

    def __init__(self, path: str):
        from . import netfake
        self.path, self.nf = path, netfake
        self.nm = netfake.FakeNM(path)

    def run(self, argv: List[str], timeout: float = 60) -> R:
        if argv and argv[0] == "nmcli":
            rc, out, err = self.nm.run(argv[1:])
            return R(rc, out, err)
        self.nf.record_call(self.path, argv)
        return R(0, "", "")


class SystemdScheduler:
    """Arms/cancels the one-shot revert timer with systemd-run, so it survives the app, the browser and a service restart."""

    def __init__(self, runner, python: Optional[str] = None, helper_py: Optional[str] = None):
        self.runner = runner
        self.python = python or ("/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable)
        self.helper_py = helper_py or os.path.join(os.path.dirname(os.path.abspath(__file__)), "helper.py")

    def schedule(self, seconds: int, unit: str, args: List[str]) -> None:
        self.cancel(unit)
        r = self.runner.run(["systemd-run", f"--on-active={int(seconds)}s", f"--unit={unit}", "--collect", "--timer-property=AccuracySec=1s",
                             "--description=Pi Automation Traffic Tester: undo the network change", self.python, "-I", self.helper_py] + list(args), 30)
        if r.rc:
            raise NetError("timer_failed", "The safety timer could not be started, so nothing was changed. " + clean_text(r.err or r.out, 120))

    def cancel(self, unit: str) -> None:
        self.runner.run(["systemctl", "stop", unit + ".timer"], 20)
        self.runner.run(["systemctl", "reset-failed", unit + ".service"], 20)


class FakeScheduler:
    """Records what would be scheduled; tests call fire() (or read .scheduled)."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.scheduled: Optional[dict] = None
        self.cancelled = 0

    def schedule(self, seconds: int, unit: str, args: List[str]) -> None:
        self.scheduled = {"seconds": seconds, "unit": unit, "args": list(args)}
        if self.path:
            write_json(self.path, self.scheduled, 0o644)

    def cancel(self, unit: str) -> None:
        self.scheduled = None
        self.cancelled += 1
        if self.path:
            try:
                os.unlink(self.path)
            except OSError:
                pass


class Ctx:
    def __init__(self, state_dir: str, runner=None, scheduler=None, now: Callable[[], float] = time.time, sysfs: str = "/sys/class/net", chrony_conf: str = "/etc/chrony/conf.d/traffic-tester.conf",
                 chrony_main: str = "/etc/chrony/chrony.conf", keyfile_dir: str = "/etc/NetworkManager/system-connections", data_dir: Optional[str] = None, fake: bool = False,
                 probe=None, lock_timeout: float = 90.0, etc_dir: str = "/etc"):
        self.state_dir, self.now, self.sysfs, self.etc_dir = state_dir, now, sysfs, etc_dir
        self.chrony_conf, self.chrony_main, self.keyfile_dir, self.fake, self.data_dir = chrony_conf, chrony_main, keyfile_dir, fake, data_dir
        self.runner = runner or SubprocessRunner()
        self.scheduler = scheduler or SystemdScheduler(self.runner)
        self.probe = probe
        self.lock_timeout = lock_timeout

    def path(self, name: str) -> str:
        return os.path.join(self.state_dir, name)

    def nm(self, *args: str, timeout: float = 60) -> R:
        return self.runner.run(["nmcli", *args], timeout)


def read_conf(path: str = SYSTEM_CONF) -> Dict[str, str]:
    conf: Dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    conf[k.strip()] = v.strip()
    except OSError:
        pass
    return conf


def make_ctx(state_dir: Optional[str] = None, **over) -> Ctx:
    env = os.environ
    conf = read_conf(env.get("TT_NET_CONF", SYSTEM_CONF))
    sd = state_dir or env.get("TT_NET_STATE_DIR") or conf.get("STATE_DIR") or DEFAULT_STATE_DIR
    fake = env.get("TT_NET_FAKE") == "1"
    kw = dict(sysfs=env.get("TT_NET_SYSFS", "/sys/class/net"), chrony_conf=env.get("TT_CHRONY_CONF", "/etc/chrony/conf.d/traffic-tester.conf"),
              chrony_main=env.get("TT_CHRONY_MAIN_CONF", "/etc/chrony/chrony.conf"), keyfile_dir=env.get("TT_NM_KEYFILE_DIR", "/etc/NetworkManager/system-connections"),
              data_dir=env.get("TT_NET_DATA_DIR") or conf.get("DATA_DIR") or env.get("TT_DATA_DIR"), fake=fake)
    kw["etc_dir"] = env.get("TT_NET_ETC") or (os.path.join(sd, "etc") if fake else "/etc")
    kw.update(over)
    if "runner" not in kw:
        if fake:
            kw["runner"] = FakeRunner(env.get("TT_FAKE_NM_STATE") or os.path.join(sd, "fake-nm.json"))
        else:
            kw["runner"] = SubprocessRunner(env.get("TT_NET_RUNNER") or None)
    if "scheduler" not in kw and fake:
        kw["scheduler"] = FakeScheduler(os.path.join(sd, "fake-scheduler.json"))
    ctx = Ctx(sd, **kw)
    if fake and ctx.probe is None:
        ctx.probe = FakeProbe(ctx)
    return ctx


# ====================================================================== state files
def write_json(path: str, data, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.unlink(tmp)
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path: str, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class _Lock:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.f = None

    def __enter__(self):
        os.makedirs(self.ctx.state_dir, mode=0o755, exist_ok=True)
        self.f = open(self.ctx.path(F_LOCK), "w")
        end = time.monotonic() + self.ctx.lock_timeout
        while True:
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > end:
                    raise NetError("busy", "Another network change is still running. Wait a moment and try again.")
                time.sleep(0.2)

    def __exit__(self, *a):
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        finally:
            self.f.close()


def blank_public() -> dict:
    return {"version": 1, "wifi_mode": None, "wifi_iface": None, "profiles": {}, "labels": {}, "fallback": {"enabled": True, "active": False, "since": None, "radio_was_off": False},
            "last_event": None, "updated": 0}


def blank_private() -> dict:
    return {"ap": None, "clients": {}, "ports": {}, "fallback": None}


def get_public(ctx: Ctx) -> dict:
    d = blank_public()
    d.update(read_json(ctx.path(F_STATE), {}) or {})
    fb = blank_public()["fallback"]
    fb.update(d.get("fallback") or {})
    d["fallback"] = fb
    return d


def get_private(ctx: Ctx) -> dict:
    d = blank_private()
    d.update(read_json(ctx.path(F_PRIVATE), {}) or {})
    return d


def public_from_private(priv: dict, old: dict) -> dict:
    """The world-readable summary: same facts as the private specs, without any password."""
    prof: Dict[str, dict] = {}
    labels: Dict[str, str] = {}
    ap = priv.get("ap")
    if ap:
        prof[ap["name"]] = {"kind": "ap", "iface": ap["iface"], "ssid": ap["ssid"], "band": ap["band"], "channel": ap["channel"], "autoconnect": ap["autoconnect"], "has_password": True}
    for n, c in sorted((priv.get("clients") or {}).items()):
        prof[n] = {"kind": "client", "iface": c["iface"], "ssid": c["ssid"], "secured": c["security"] != "none", "autoconnect": c["autoconnect"]}
    for iface, p in sorted((priv.get("ports") or {}).items()):
        prof[f"port:{iface}"] = {"kind": "eth", "iface": iface, "method": p["method"], "address": p.get("address"), "gateway": p.get("gateway"), "dns": p.get("dns") or [],
                                 "vlan": p.get("vlan"), "label": p["label"]}
        labels[iface] = p["label"]
    out = dict(old)
    out["profiles"], out["labels"] = prof, labels
    return out


def save_state(ctx: Ctx, priv: dict, public: dict) -> None:
    public = public_from_private(priv, public)
    public["updated"] = ctx.now()
    write_json(ctx.path(F_PRIVATE), priv, 0o600)
    write_json(ctx.path(F_STATE), public, 0o644)


def set_last_event(ctx: Ctx, kind: str, message: str, **extra) -> None:
    pub = get_public(ctx)
    pub["last_event"] = {"type": kind, "message": message, "at": ctx.now(), **extra}
    pub["updated"] = ctx.now()
    write_json(ctx.path(F_STATE), pub, 0o644)


# ====================================================================== what NetworkManager and the kernel say
def split_terse(line: str) -> List[str]:
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    out.append(cur)
    return out


def norm_type(t: str) -> str:
    t = (t or "").lower()
    if "wireless" in t or t == "wifi":
        return "wifi"
    if "ethernet" in t:
        return "ethernet"
    return t


def nm_running(ctx: Ctx) -> bool:
    r = ctx.nm("-t", "-f", "RUNNING", "general", timeout=10)
    return r.ok and "running" in r.out


def nm_profiles(ctx: Ctx) -> List[dict]:
    r = ctx.nm("-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show", timeout=20)
    if not r.ok:
        raise NetError("nm_unavailable", "NetworkManager did not answer, so the network cannot be changed from here. " + clean_text(r.err, 120))
    out = []
    for line in r.out.splitlines():
        p = split_terse(line)
        if len(p) >= 4 and p[0]:
            out.append({"name": p[0], "uuid": p[1], "type": norm_type(p[2]), "device": p[3]})
    return out


def nm_active(ctx: Ctx) -> List[dict]:
    r = ctx.nm("-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show", "--active", timeout=20)
    out = []
    for line in r.out.splitlines() if r.ok else []:
        p = split_terse(line)
        if len(p) >= 4 and p[0] and p[3] and not p[3].startswith(SKIP_IFACE_PREFIX):
            out.append({"name": p[0], "uuid": p[1], "type": norm_type(p[2]), "device": p[3]})
    return out


def nm_radio(ctx: Ctx) -> str:
    r = ctx.nm("radio", "wifi", timeout=10)
    return "disabled" if r.ok and "disabled" in r.out else "enabled"


def tt_present(ctx: Ctx) -> List[str]:
    return sorted({p["name"] for p in nm_profiles(ctx) if is_tt_name(p["name"]) and p["name"] != PROFILE_FALLBACK})


def sys_ifaces(ctx: Ctx) -> List[str]:
    try:
        return sorted(os.listdir(ctx.sysfs))
    except OSError:
        return []


def iface_exists(ctx: Ctx, name: str) -> bool:
    return os.path.isdir(os.path.join(ctx.sysfs, name))


def is_wifi_iface(ctx: Ctx, name: str) -> bool:
    return os.path.isdir(os.path.join(ctx.sysfs, name, "wireless")) or os.path.isdir(os.path.join(ctx.sysfs, name, "phy80211")) or name.startswith(("wl", "wlan"))


def wifi_iface(ctx: Ctx, required: bool = True) -> Optional[str]:
    for n in sys_ifaces(ctx):
        if is_wifi_iface(ctx, n):
            return n
    if required:
        raise NetError("no_wifi", "This Pi has no Wi-Fi adapter (or it is switched off in the hardware), so Wi-Fi cannot be set up.")
    return None


def is_ethernet_port(ctx: Ctx, name: str) -> bool:
    if name.startswith(SKIP_IFACE_PREFIX) or is_wifi_iface(ctx, name) or "." in name:
        return False
    try:
        with open(os.path.join(ctx.sysfs, name, "type")) as f:
            return f.read().strip() == "1"
    except OSError:
        return os.path.isdir(os.path.join(ctx.sysfs, name))          # test sandboxes without a type file


def carrier(ctx: Ctx, name: str) -> bool:
    try:
        with open(os.path.join(ctx.sysfs, name, "carrier")) as f:
            return f.read().strip() != "0"
    except OSError:
        return True


def default_ssid(ctx: Optional[Ctx] = None, data_dir: Optional[str] = None, use_db: bool = True) -> str:
    """The Pi's name: the app's `pi_name` setting when present, else the hostname. use_db=False never opens the app's database (it belongs to the unprivileged service user:
    root only reads it at install time, never while handling a request from the app)."""
    name = ""
    dd = (data_dir or (ctx.data_dir if ctx else None) or read_conf().get("DATA_DIR") or os.environ.get("TT_DATA_DIR")) if use_db else None
    if dd:
        db = os.path.join(dd, "traffic-tester.db")
        if os.path.isfile(db):
            try:
                import sqlite3
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
                try:
                    row = con.execute("SELECT value FROM settings WHERE key='pi_name'").fetchone()
                finally:
                    con.close()
                v = json.loads(row[0]) if row else ""
                name = v if isinstance(v, str) else ""
            except Exception:
                name = ""
    if not name.strip():
        try:
            name = socket.gethostname()
        except OSError:
            name = ""
    name = clean_text(name, 64).strip()
    while len(name.encode("utf-8")) > 32:
        name = name[:-1]
    return name or "pitester"


# ====================================================================== normalising a change request (the UI/API shape -> specs)
def _keys(d: dict, allowed: Tuple[str, ...], what: str) -> None:
    extra = [k for k in d if k not in allowed]
    if extra:
        raise NetError("bad_request", f"Unknown {what} setting '{clean_text(extra[0], 30)}'.")


def normalize_changes(ctx: Ctx, changes, priv: Optional[dict] = None) -> dict:
    """Validate everything and fill in what was left out from what is saved. Returns {"wifi": spec|None, "ports": {iface: spec}, "fallback": bool|None}."""
    priv = priv or get_private(ctx)
    if not isinstance(changes, dict):
        raise NetError("bad_request", "The change request is not valid.")
    _keys(changes, ("wifi", "ports", "fallback"), "network")
    out: dict = {"wifi": None, "ports": {}, "fallback": None}
    w = changes.get("wifi")
    if w:
        if not isinstance(w, dict):
            raise NetError("bad_request", "The Wi-Fi settings are not valid.")
        _keys(w, ("mode", "ssid", "password", "channel", "band", "hidden", "security", "iface"), "Wi-Fi")
        mode = w.get("mode")
        if mode not in WIFI_MODES:
            raise NetError("bad_mode", "Wi-Fi must be an access point, join a network, or off.")
        iface = v_iface_name(w["iface"], "Wi-Fi interface") if w.get("iface") else wifi_iface(ctx, required=mode != "off")
        if iface and mode != "off" and not iface_exists(ctx, iface):
            raise NetError("no_wifi", f"The Wi-Fi interface {iface} does not exist on this Pi.")
        if mode == "off":
            out["wifi"] = {"mode": "off", "iface": iface}
        elif mode == "ap":
            old = priv.get("ap") or {}
            ssid = v_ssid(w["ssid"]) if w.get("ssid") else (old.get("ssid") or default_ssid(ctx, use_db=False))
            psk = v_psk(w["password"]) if w.get("password") else (old.get("psk") or "")
            if not psk:
                raise NetError("bad_password", "Choose a Wi-Fi password of 8 to 63 characters for the access point.")
            band, ch = v_channel(w.get("band", old.get("band", "bg")), w["channel"] if "channel" in w else old.get("channel", 6))
            out["wifi"] = {"mode": "ap", "iface": iface, "ssid": ssid, "psk": psk, "band": band, "channel": ch}
        else:
            ssid = v_ssid(w.get("ssid"))
            sec = w.get("security") or ("wpa-psk" if w.get("password") else "none")
            if sec not in SECURITIES:
                raise NetError("bad_request", "Unknown Wi-Fi security type.")
            psk = None
            if sec != "none":
                psk = v_psk(w.get("password")) if w.get("password") else next((c.get("psk") for c in (priv.get("clients") or {}).values() if c.get("ssid") == ssid and c.get("psk")), None)
                if not psk:
                    raise NetError("bad_password", f"'{ssid}' needs a password (8 to 63 characters).")
            out["wifi"] = {"mode": "client", "iface": iface, "ssid": ssid, "psk": psk, "security": sec, "hidden": bool(w.get("hidden"))}
    ports = changes.get("ports")
    if ports:
        if not isinstance(ports, dict) or len(ports) > 8:
            raise NetError("bad_request", "The Ethernet port settings are not valid.")
        for iface, p in ports.items():
            v_iface_name(iface, "port")
            if not iface_exists(ctx, iface) or not is_ethernet_port(ctx, iface):
                raise NetError("bad_iface", f"'{iface}' is not an Ethernet port on this Pi.")
            if not isinstance(p, dict):
                raise NetError("bad_request", f"The settings for {iface} are not valid.")
            _keys(p, ("method", "address", "mask", "gateway", "dns", "vlan", "label"), f"port {iface}")
            old = (priv.get("ports") or {}).get(iface) or {}
            method = p.get("method", old.get("method", "dhcp"))
            if method not in ("dhcp", "static"):
                raise NetError("bad_method", "An Ethernet port is either DHCP (automatic) or static.")
            spec = {"iface": iface, "method": method, "address": None, "gateway": None, "dns": [], "vlan": v_vlan(p["vlan"] if "vlan" in p else old.get("vlan")),
                    "label": v_label(p.get("label", old.get("label", "both")))}
            if method == "static":
                addr = p["address"] if "address" in p else (f"{old.get('address')}" if old.get("address") else None)
                if not addr:
                    raise NetError("bad_ip", f"{iface}: enter the IP address for a static setup.")
                ip, n = v_address(addr, p.get("mask") if p.get("mask") not in (None, "") else None)
                spec["address"] = f"{ip}/{n}"
                gw = p["gateway"] if "gateway" in p else old.get("gateway")
                spec["gateway"] = v_gateway(gw, ip, n) if gw else None
                spec["dns"] = v_dns(p["dns"] if "dns" in p else old.get("dns"))
            out["ports"][iface] = spec
        _check_overlaps(out["ports"], priv, out["wifi"])
    elif out["wifi"] and out["wifi"]["mode"] == "ap":
        _check_overlaps({}, priv, out["wifi"])
    if "fallback" in changes and changes["fallback"] is not None:
        fb = changes["fallback"]
        if isinstance(fb, dict):
            fb = fb.get("enabled")
        if not isinstance(fb, bool):
            raise NetError("bad_request", "The fallback setting must be on or off.")
        out["fallback"] = fb
    return out


def _check_overlaps(new_ports: Dict[str, dict], priv: dict, wifi: Optional[dict]) -> None:
    nets: Dict[str, ipaddress.IPv4Network] = {}
    for iface, p in {**(priv.get("ports") or {}), **new_ports}.items():
        if p.get("method") == "static" and p.get("address"):
            nets[iface] = ipaddress.ip_network(p["address"], strict=False)
    items = sorted(nets.items())
    for i, (ia, na) in enumerate(items):
        for ib, nb in items[i + 1:]:
            if na.overlaps(nb) and (ia in new_ports or ib in new_ports):
                raise NetError("overlap", f"{ia} ({na}) and {ib} ({nb}) would be in overlapping networks. Give each port its own subnet.")
    ap_on = (wifi and wifi["mode"] == "ap") or (not wifi and (priv.get("ap") or {}).get("autoconnect"))
    if ap_on:
        for iface, n in nets.items():
            if iface in new_ports or wifi:
                if n.overlaps(AP_NET):
                    raise NetError("overlap", f"{iface} ({n}) overlaps the access point's own network {AP_NET}. Use a different subnet for the port.")


# ====================================================================== specs -> nmcli argument lists
def port_profile_names(spec: Optional[dict]) -> List[str]:
    if not spec:
        return []
    base = f"tt-eth-{spec['iface']}"
    return [base] + ([f"{base}-v{spec['vlan']}"] if spec.get("vlan") else [])


def _ip_props(spec: dict) -> List[str]:
    never = "yes" if spec["label"] == "test" else "no"
    a = ["ipv4.method", "manual" if spec["method"] == "static" else "auto"]
    if spec["method"] == "static":
        a += ["ipv4.addresses", spec["address"]]
        if spec.get("gateway"):
            a += ["ipv4.gateway", spec["gateway"]]
        if spec.get("dns"):
            a += ["ipv4.dns", ",".join(spec["dns"])]
    a += ["ipv4.never-default", never, "ipv6.method", "link-local", "ipv6.never-default", never]
    return a


def add_commands(spec_kind: str, spec: dict, name: Optional[str] = None) -> List[Tuple[List[str], str]]:
    """The `nmcli connection add` argument lists for one saved spec -> [(argv, profile name)]."""
    if spec_kind in ("ap", "fallback"):
        name = name or (PROFILE_AP if spec_kind == "ap" else PROFILE_FALLBACK)
        a = ["nmcli", "connection", "add", "type", "wifi", "ifname", spec["iface"], "con-name", name,
             "connection.autoconnect", "yes" if spec.get("autoconnect", True) else "no", "connection.autoconnect-priority", "100" if spec_kind == "ap" else "10",
             "802-11-wireless.ssid", spec["ssid"], "802-11-wireless.mode", "ap", "802-11-wireless.band", "a" if spec["band"] == "a" else "bg"]
        if spec["band"] == "auto":
            a[-1] = "bg"
        if spec.get("channel"):
            a += ["802-11-wireless.channel", str(spec["channel"])]
        a += ["ipv4.method", "shared", "ipv4.addresses", AP_ADDR, "ipv6.method", "ignore", "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn",
              "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.psk", spec["psk"]]
        return [(a, name)]
    if spec_kind == "client":
        name = spec["name"]
        a = ["nmcli", "connection", "add", "type", "wifi", "ifname", spec["iface"], "con-name", name, "connection.autoconnect", "yes" if spec.get("autoconnect", True) else "no",
             "connection.autoconnect-priority", "50", "802-11-wireless.ssid", spec["ssid"]]
        if spec.get("hidden"):
            a += ["802-11-wireless.hidden", "yes"]
        if spec["security"] != "none":
            a += ["wifi-sec.key-mgmt", spec["security"], "wifi-sec.psk", spec["psk"]]
        a += ["ipv4.method", "auto", "ipv6.method", "auto"]
        return [(a, name)]
    if spec_kind == "port":
        base = f"tt-eth-{spec['iface']}"
        if spec.get("vlan"):
            child = f"{base}-v{spec['vlan']}"
            parent = ["nmcli", "connection", "add", "type", "ethernet", "ifname", spec["iface"], "con-name", base, "connection.autoconnect", "yes", "connection.autoconnect-priority", "100",
                      "ipv4.method", "disabled", "ipv6.method", "ignore"]
            vl = ["nmcli", "connection", "add", "type", "vlan", "con-name", child, "ifname", f"{spec['iface']}.{spec['vlan']}", "dev", spec["iface"], "id", str(spec["vlan"]),
                  "connection.autoconnect", "yes", "connection.autoconnect-priority", "100"] + _ip_props(spec)
            return [(parent, base), (vl, child)]
        a = ["nmcli", "connection", "add", "type", "ethernet", "ifname", spec["iface"], "con-name", base, "connection.autoconnect", "yes", "connection.autoconnect-priority", "100"] + _ip_props(spec)
        return [(a, base)]
    raise NetError("internal", "unknown profile kind")


def specs_with_names(priv: dict) -> List[Tuple[str, dict, List[str]]]:
    out: List[Tuple[str, dict, List[str]]] = []
    if priv.get("ap"):
        out.append(("ap", priv["ap"], [PROFILE_AP]))
    for n, c in sorted((priv.get("clients") or {}).items()):
        out.append(("client", c, [n]))
    for iface, p in sorted((priv.get("ports") or {}).items()):
        out.append(("port", p, port_profile_names(p)))
    return out


# ====================================================================== apply / snapshot / revert
def friendly_nm_error(err: str, ssid: str = "") -> str:
    e = clean_text(err, 300).strip()
    low = e.lower()
    if "secrets were required" in low or "802-1x supplicant failed" in low or "(7)" in low:
        return f"The password for '{ssid}' was not accepted." if ssid else "The password was not accepted."
    if "no network with ssid" in low:
        return f"The Wi-Fi network '{ssid}' was not found (out of range, or hidden: tick 'Hidden network')."
    if "ip configuration could not be reserved" in low or "dhcp" in low and "time" in low:
        return "No DHCP server answered on that port."
    if "radio is switched off" in low or "wi-fi is disabled" in low:
        return "The Wi-Fi radio is switched off."
    if "timed out" in low or "timeout" in low:
        return "NetworkManager did not finish in time."
    return e.replace("Error: ", "")[:200] or "NetworkManager refused the change."


def take_snapshot(ctx: Ctx, touched: List[str]) -> dict:
    return {"taken": ctx.now(), "private": copy.deepcopy(get_private(ctx)), "public": copy.deepcopy(get_public(ctx)), "had_files": os.path.exists(ctx.path(F_PRIVATE)), "present": tt_present(ctx),
            "active": [a for a in nm_active(ctx) if a["type"] in ("wifi", "ethernet", "vlan")], "radio": nm_radio(ctx), "touched": sorted(touched)}


def get_pending(ctx: Ctx) -> Optional[dict]:
    return read_json(ctx.path(F_PENDING), None)


def clear_pending(ctx: Ctx) -> None:
    for f in (F_PENDING, F_SNAP):
        try:
            os.unlink(ctx.path(f))
        except OSError:
            pass


def _add_profile(ctx: Ctx, kind: str, spec: dict, name: Optional[str] = None) -> None:
    for argv, n in add_commands(kind, spec, name):
        r = ctx.runner.run(argv, 30)
        if r.rc:
            raise NetError("nm_failed", f"NetworkManager would not create '{n}': {friendly_nm_error(r.err, spec.get('ssid', ''))}")


def _delete_if_present(ctx: Ctx, names: List[str], present: List[str]) -> None:
    for n in names:
        if is_tt_name(n) and n in present:
            ctx.nm("connection", "delete", "id", n, timeout=30)


def restore_snapshot(ctx: Ctx, snap: dict, reason: str, message: str) -> List[str]:
    """Put the saved profiles, the radio and the active connections back as the snapshot recorded them."""
    notes: List[str] = []
    touched = set(snap.get("touched") or [])
    priv = snap.get("private") or blank_private()
    present_now = [p["name"] for p in nm_profiles(ctx) if is_tt_name(p["name"])]
    recreate = [(k, s, names) for k, s, names in specs_with_names(priv) if set(names) & touched]
    for n in sorted(touched | {x for _, _, names in recreate for x in names}):
        if n in present_now and n != PROFILE_FALLBACK:
            ctx.nm("connection", "delete", "id", n, timeout=30)
    if (snap.get("radio") == "enabled"):
        ctx.nm("radio", "wifi", "on", timeout=15)
    for k, s, names in recreate:
        try:
            _add_profile(ctx, k, s)
        except NetError as e:
            notes.append(e.message)
    if snap.get("radio") == "disabled":
        ctx.nm("radio", "wifi", "off", timeout=15)
    else:
        now_active = {(a["name"], a["device"]) for a in nm_active(ctx)}
        have = {p["name"] for p in nm_profiles(ctx)}
        for a in snap.get("active") or []:
            if (a["name"], a["device"]) not in now_active and a["name"] in have:
                r = ctx.nm("--wait", "30", "connection", "up", "id", a["name"], "ifname", a["device"], timeout=60)
                if r.rc:
                    notes.append(f"could not bring '{a['name']}' back up: {friendly_nm_error(r.err)}")
    pub = snap.get("public") or blank_public()
    if snap.get("had_files", True):
        write_json(ctx.path(F_PRIVATE), priv, 0o600)
    else:                                                       # nothing had been saved before this change: leave it that way
        try:
            os.unlink(ctx.path(F_PRIVATE))
        except OSError:
            pass
    if reason == "apply_failed" and not snap.get("had_files", True):        # the caller reports the failure itself; there was no saved state to keep
        try:
            os.unlink(ctx.path(F_STATE))
        except OSError:
            pass
        return notes
    pub["last_event"] = {"type": "reverted", "reason": reason, "message": message, "at": ctx.now()}
    pub["updated"] = ctx.now()
    write_json(ctx.path(F_STATE), pub, 0o644)
    return notes


def _clamp_seconds(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = REVERT_DEFAULT
    return max(REVERT_MIN, min(REVERT_MAX, n))


def _summary_lines(v) -> List[str]:
    if not isinstance(v, list):
        return []
    return [clean_text(x, 240) for x in v[:10] if isinstance(x, str)]


def op_apply(ctx: Ctx, req: dict) -> dict:
    priv0 = get_private(ctx)
    norm = normalize_changes(ctx, req.get("changes"), priv0)
    if norm["wifi"] is None and not norm["ports"] and norm["fallback"] is None:
        return {"ok": True, "code": "nothing", "message": "There was nothing to change."}
    pend = get_pending(ctx)
    if pend and pend.get("state") in ("applying", "waiting"):
        raise NetError("pending", "A previous change is still waiting for you to press Keep or Revert. Finish that first.")
    auto = req.get("auto_revert", True) is not False
    seconds = _clamp_seconds(req.get("revert_seconds"))
    if norm["wifi"] is None and not norm["ports"]:
        return op_set_fallback(ctx, {"enabled": norm["fallback"]})
    if not nm_running(ctx):
        raise NetError("nm_unavailable", "NetworkManager is not running on this Pi, so the network cannot be changed from the app.")
    priv = copy.deepcopy(priv0)
    pub = get_public(ctx)
    present = tt_present(ctx)
    touched: set = set()
    adds: List[Tuple[str, dict]] = []
    activate: List[Tuple[str, bool, str]] = []                  # (profile name, soft?, device)
    radio_after: Optional[str] = None
    radio_before: Optional[str] = None
    mode = pub.get("wifi_mode")
    w = norm["wifi"]
    if w:
        mode = w["mode"]
        pub["wifi_iface"] = w.get("iface") or pub.get("wifi_iface")
        if w["mode"] == "ap":
            priv["ap"] = {"name": PROFILE_AP, "iface": w["iface"], "ssid": w["ssid"], "psk": w["psk"], "band": w["band"], "channel": w["channel"], "autoconnect": True}
            touched.add(PROFILE_AP)
            adds.append(("ap", priv["ap"]))
            radio_before = "on"
            activate.append((PROFILE_AP, False, w["iface"]))
        elif w["mode"] == "client":
            clients = priv.setdefault("clients", {})
            name = next((n for n, c in clients.items() if c.get("ssid") == w["ssid"]), None)
            if not name:
                i = 1
                while f"tt-wifi-{i}" in clients or f"tt-wifi-{i}" in present:
                    i += 1
                name = f"tt-wifi-{i}"
            clients[name] = {"name": name, "iface": w["iface"], "ssid": w["ssid"], "psk": w["psk"], "security": w["security"], "hidden": w["hidden"], "autoconnect": True}
            touched.add(name)
            adds.append(("client", clients[name]))
            if priv.get("ap"):
                priv["ap"]["autoconnect"] = False
                touched.add(PROFILE_AP)
                adds.append(("ap", priv["ap"]))
            radio_before = "on"
            activate.append((name, False, w["iface"]))
        else:
            if priv.get("ap"):
                priv["ap"]["autoconnect"] = False
                touched.add(PROFILE_AP)
                adds.append(("ap", priv["ap"]))
            radio_after = "off"
    for iface, spec in norm["ports"].items():
        old = (priv.get("ports") or {}).get(iface)
        touched |= set(port_profile_names(old)) | set(port_profile_names(spec))
        priv.setdefault("ports", {})[iface] = spec
        adds.append(("port", spec))
        if carrier(ctx, iface):
            activate += [(n, True, iface if n == f"tt-eth-{iface}" else f"{iface}.{spec['vlan']}") for n in port_profile_names(spec)]
    snap = take_snapshot(ctx, sorted(touched))
    write_json(ctx.path(F_SNAP), snap, 0o600)
    started = ctx.now()
    if auto:
        write_json(ctx.path(F_PENDING), {"id": clean_text(req.get("id", ""), 64), "state": "applying", "started": started, "deadline": started + seconds + 120, "seconds": seconds,
                                         "unit": REVERT_UNIT, "summary": _summary_lines(req.get("summary"))}, 0o644)
        try:
            ctx.scheduler.schedule(seconds + 120, REVERT_UNIT, ["net-revert"])          # generous while applying (a Wi-Fi join can take a minute); tightened below
        except NetError:
            clear_pending(ctx)
            raise
    warnings: List[str] = []
    try:
        if radio_before == "on":
            ctx.nm("radio", "wifi", "on", timeout=15)
        _delete_if_present(ctx, sorted(touched), present)
        for kind, spec in adds:
            _add_profile(ctx, kind, spec)
        for name, soft, dev in activate:
            r = ctx.nm("--wait", "45", "connection", "up", "id", name, "ifname", dev, timeout=90)
            if r.rc:
                ssid = (priv.get("clients", {}).get(name) or priv.get("ap") or {}).get("ssid", "") if name.startswith(("tt-wifi", "tt-ap")) else ""
                msg = friendly_nm_error(r.err, ssid)
                if not soft:
                    what = "start the access point" if name == PROFILE_AP else f"join '{ssid}'"
                    raise NetError("activate_failed", f"The Pi could not {what}: {msg}")
                warnings.append(f"{dev}: the settings are saved but the port did not come up yet ({msg}). It keeps trying and comes up when the cable and network are ready.")
        if radio_after == "off":
            ctx.nm("radio", "wifi", "off", timeout=15)
        if norm["fallback"] is not None:
            pub["fallback"]["enabled"] = norm["fallback"]
        pub["wifi_mode"] = mode
        save_state(ctx, priv, pub)
    except NetError as e:
        try:
            if auto:
                ctx.scheduler.cancel(REVERT_UNIT)
            notes = restore_snapshot(ctx, snap, "apply_failed", e.message)
        finally:
            clear_pending(ctx)
        raise NetError(e.code, e.message + " The previous settings were put back." + (" (" + "; ".join(notes) + ")" if notes else ""))
    except Exception as e:                                                                     # noqa: BLE001 - never leave a half-applied network behind
        try:
            if auto:
                ctx.scheduler.cancel(REVERT_UNIT)
            restore_snapshot(ctx, snap, "apply_failed", str(e))
        finally:
            clear_pending(ctx)
        raise NetError("internal", f"The network change failed unexpectedly ({type(e).__name__}); the previous settings were put back.")
    if auto:
        deadline = ctx.now() + seconds
        write_json(ctx.path(F_PENDING), {"id": clean_text(req.get("id", ""), 64), "state": "waiting", "started": started, "deadline": deadline, "seconds": seconds, "unit": REVERT_UNIT,
                                         "summary": _summary_lines(req.get("summary"))}, 0o644)
        ctx.scheduler.schedule(seconds, REVERT_UNIT, ["net-revert"])                          # the real 90 s, counted from "applied"
        set_last_event(ctx, "applied", f"New network settings are active. They are undone automatically in {seconds} s unless you press Keep.")
        return {"ok": True, "code": "applied", "message": f"Applied. Press Keep within {seconds} seconds or the Pi goes back to the previous settings.", "pending": get_pending(ctx), "warnings": warnings}
    clear_pending(ctx)
    set_last_event(ctx, "applied", "New network settings are active.")
    return {"ok": True, "code": "applied", "message": "Applied.", "warnings": warnings}


def op_keep(ctx: Ctx, req: dict) -> dict:
    pend = get_pending(ctx)
    if not pend:
        return {"ok": True, "code": "nothing", "message": "There is nothing waiting to be kept."}
    ctx.scheduler.cancel(REVERT_UNIT)
    clear_pending(ctx)
    set_last_event(ctx, "kept", "The new network settings were kept.")
    return {"ok": True, "code": "kept", "message": "The new settings are now permanent."}


def op_revert(ctx: Ctx, req: dict, reason: str = "manual", force: bool = True) -> dict:
    pend = get_pending(ctx)
    snap = read_json(ctx.path(F_SNAP), None)
    if not pend or not snap:
        clear_pending(ctx)
        return {"ok": True, "code": "nothing", "message": "There is nothing to undo."}
    if not force and ctx.now() < float(pend.get("deadline") or 0) - 10:
        return {"ok": True, "code": "too_early", "message": "The countdown was extended; not reverting yet."}
    ctx.scheduler.cancel(REVERT_UNIT)
    msg = "The previous network settings were restored automatically (nobody pressed Keep in time)." if reason == "timeout" else "The previous network settings were restored."
    notes = restore_snapshot(ctx, snap, reason, msg)
    clear_pending(ctx)
    return {"ok": True, "code": "reverted", "message": msg, "notes": notes}


# ====================================================================== scan / fallback / ensure / setup
def op_scan(ctx: Ctx, req: dict) -> dict:
    iface = wifi_iface(ctx)
    if any(a["type"] == "wifi" and a["device"] == iface and (a["name"] in (PROFILE_AP, PROFILE_FALLBACK)) for a in nm_active(ctx)):
        raise NetError("radio_busy", "The Wi-Fi radio is busy being the access point, so it cannot look for other networks at the same time. Type the network name instead, or switch the access point off first.")
    if nm_radio(ctx) == "disabled":
        raise NetError("radio_off", "Wi-Fi is switched off. Turn it on first to scan for networks.")
    ctx.nm("device", "wifi", "rescan", "ifname", iface, timeout=20)
    r = ctx.nm("-t", "-f", "SSID,SIGNAL,SECURITY,CHAN", "device", "wifi", "list", "ifname", iface, timeout=30)
    if r.rc:
        raise NetError("scan_failed", "The Wi-Fi scan did not work: " + friendly_nm_error(r.err))
    best: Dict[str, dict] = {}
    for line in r.out.splitlines():
        p = split_terse(line)
        if len(p) < 4 or not p[0]:
            continue
        try:
            sig = int(p[1] or 0)
        except ValueError:
            sig = 0
        sec = p[2].strip()
        row = {"ssid": p[0], "signal": sig, "security": sec or "none", "secure": bool(sec and sec != "--"), "chan": p[3],
               "sae": "SAE" in sec and "WPA2" not in sec}
        if p[0] not in best or sig > best[p[0]]["signal"]:
            best[p[0]] = row
    nets = sorted(best.values(), key=lambda x: -x["signal"])[:40]
    return {"ok": True, "code": "scan", "message": f"{len(nets)} networks found.", "networks": nets}


def _fallback_spec(ctx: Ctx, priv: dict) -> dict:
    fb = priv.get("fallback")
    if not fb:
        fb = {"iface": wifi_iface(ctx), "ssid": (priv.get("ap") or {}).get("ssid") or default_ssid(ctx, use_db=False), "psk": DEFAULT_PSK, "band": "bg", "channel": 6, "autoconnect": False}
    fb = dict(fb)
    fb["autoconnect"] = False                                    # never comes up by itself: only the watchdog starts it
    return fb


def op_fallback_ap(ctx: Ctx, req: Optional[dict] = None) -> dict:
    pub = get_public(ctx)
    if not pub["fallback"]["enabled"]:
        return {"ok": True, "code": "disabled", "message": "The emergency access point is switched off in Settings."}
    priv = get_private(ctx)
    spec = _fallback_spec(ctx, priv)
    radio_off = nm_radio(ctx) == "disabled"
    if radio_off:
        ctx.nm("radio", "wifi", "on", timeout=15)
    if PROFILE_FALLBACK in [p["name"] for p in nm_profiles(ctx)]:
        ctx.nm("connection", "delete", "id", PROFILE_FALLBACK, timeout=30)
    _add_profile(ctx, "fallback", spec)
    r = ctx.nm("--wait", "30", "connection", "up", "id", PROFILE_FALLBACK, "ifname", spec["iface"], timeout=60)
    if r.rc:
        raise NetError("activate_failed", "The emergency access point could not start: " + friendly_nm_error(r.err))
    priv["fallback"] = spec
    write_json(ctx.path(F_PRIVATE), priv, 0o600)
    pub = get_public(ctx)
    pub["fallback"].update(active=True, since=ctx.now(), radio_was_off=radio_off or pub["fallback"].get("radio_was_off", False) and pub["fallback"].get("active", False))
    pub["last_event"] = {"type": "fallback", "message": f"No network for 3 minutes: the Pi started its own Wi-Fi '{spec['ssid']}' (password: {DEFAULT_PSK}). Open http://{AP_GATEWAY}:8080.", "at": ctx.now()}
    pub["updated"] = ctx.now()
    write_json(ctx.path(F_STATE), pub, 0o644)
    return {"ok": True, "code": "fallback_up", "message": pub["last_event"]["message"]}


def _fallback_down(ctx: Ctx, mark_inactive: bool) -> None:
    ctx.nm("connection", "down", "id", PROFILE_FALLBACK, timeout=30)
    pub = get_public(ctx)
    if mark_inactive:
        radio_was_off = pub["fallback"].get("radio_was_off")
        pub["fallback"].update(active=False, since=None, radio_was_off=False)
        pub["updated"] = ctx.now()
        write_json(ctx.path(F_STATE), pub, 0o644)
        if radio_was_off:
            ctx.nm("radio", "wifi", "off", timeout=15)


def _reconnect_normal(ctx: Ctx, wait: int = 25) -> R:
    iface = wifi_iface(ctx, required=False) or "wlan0"
    ctx.nm("radio", "wifi", "on", timeout=15)
    return ctx.nm("--wait", str(wait), "device", "connect", iface, timeout=wait + 20)


def op_fallback_off(ctx: Ctx, req: Optional[dict] = None) -> dict:
    _fallback_down(ctx, True)
    pub = get_public(ctx)
    if pub.get("wifi_mode") != "off":
        _reconnect_normal(ctx)
    return {"ok": True, "code": "fallback_down", "message": "The emergency access point was switched off; the normal network settings are back."}


def op_fallback_retry(ctx: Ctx, req: Optional[dict] = None) -> dict:
    _fallback_down(ctx, False)
    r = _reconnect_normal(ctx, RETRY_WINDOW_S)
    return {"ok": True, "code": "fallback_retry", "message": "Trying the normal Wi-Fi settings again.", "joined": r.ok}


def op_fallback_done(ctx: Ctx, req: Optional[dict] = None) -> dict:
    pub = get_public(ctx)
    pub["fallback"].update(active=False, since=None, radio_was_off=False)
    pub["updated"] = ctx.now()
    write_json(ctx.path(F_STATE), pub, 0o644)
    return {"ok": True, "code": "fallback_done", "message": "ok"}


def op_ensure_wifi(ctx: Ctx, req: Optional[dict] = None) -> dict:
    """Bring the saved Wi-Fi mode back (after Nexmon monitor mode, a reboot of the radio ...). Idempotent."""
    pub = get_public(ctx)
    mode = pub.get("wifi_mode")
    if not nm_running(ctx):
        return {"ok": False, "code": "nm_unavailable", "message": "NetworkManager is not running."}
    act = nm_active(ctx)
    if any(a["name"] == PROFILE_FALLBACK for a in act):
        return {"ok": True, "code": "fallback", "message": "The emergency access point is up."}
    if mode == "off":
        return {"ok": True, "code": "off", "message": "Wi-Fi is switched off in Settings."}
    ctx.nm("radio", "wifi", "on", timeout=15)
    iface = (pub.get("wifi_iface") or wifi_iface(ctx, required=False) or "wlan0")
    if mode == "ap":
        if any(a["name"] == PROFILE_AP for a in act):
            return {"ok": True, "code": "ap_up", "message": "The access point is already up."}
        r = ctx.nm("--wait", "30", "connection", "up", "id", PROFILE_AP, "ifname", iface, timeout=60)
        return {"ok": r.ok, "code": "ap_up" if r.ok else "ap_failed", "message": "The access point is back." if r.ok else "The access point did not come back: " + friendly_nm_error(r.err)}
    r = _reconnect_normal(ctx, 30)
    return {"ok": r.ok, "code": "wifi", "message": "Wi-Fi reconnected." if r.ok else "Wi-Fi did not reconnect yet: " + friendly_nm_error(r.err)}


def op_set_fallback(ctx: Ctx, req: dict) -> dict:
    en = req.get("enabled")
    if not isinstance(en, bool):
        raise NetError("bad_request", "The fallback setting must be on or off.")
    pub = get_public(ctx)
    pub["fallback"]["enabled"] = en
    pub["updated"] = ctx.now()
    write_json(ctx.path(F_STATE), pub, 0o644)
    return {"ok": True, "code": "set", "message": "Saved."}


def keyfile_text(spec: dict, uid: str) -> str:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\")
    if ";" in spec["ssid"]:
        raise NetError("bad_ssid", "A network name containing ';' cannot be written during install. Set it in the app instead.")
    lines = ["[connection]", f"id={PROFILE_AP}", f"uuid={uid}", "type=wifi", f"interface-name={spec['iface']}", "autoconnect=true", "autoconnect-priority=100", "",
             "[wifi]", "mode=ap", f"ssid={esc(spec['ssid'])}", f"band={'a' if spec['band'] == 'a' else 'bg'}"]
    if spec.get("channel"):
        lines.append(f"channel={spec['channel']}")
    lines += ["", "[wifi-security]", "key-mgmt=wpa-psk", "proto=rsn;", "pairwise=ccmp;", "group=ccmp;", f"psk={esc(spec['psk'])}", "",
              "[ipv4]", f"address1={AP_ADDR}", "method=shared", "", "[ipv6]", "addr-gen-mode=default", "method=ignore", ""]
    return "\n".join(lines)


def setup_default_ap(ctx: Ctx, ssid: Optional[str] = None, psk: str = DEFAULT_PSK, activate: bool = False, keyfile_only: bool = False, iface: str = "wlan0") -> dict:
    """The fresh-install default: Wi-Fi = access point named after the Pi, password 'password', 10.42.0.1 (NetworkManager 'shared')."""
    ssid = v_ssid(ssid) if ssid else default_ssid(ctx)
    psk = v_psk(psk)
    v_iface_name(iface, "Wi-Fi interface")
    spec = {"name": PROFILE_AP, "iface": iface, "ssid": ssid, "psk": psk, "band": "bg", "channel": 6, "autoconnect": True}
    priv = get_private(ctx)
    priv["ap"] = spec
    pub = get_public(ctx)
    pub["wifi_mode"], pub["wifi_iface"] = "ap", iface
    via = "nmcli"
    if not keyfile_only and nm_running(ctx):
        present = [p["name"] for p in nm_profiles(ctx)]
        if PROFILE_AP in present:
            ctx.nm("connection", "delete", "id", PROFILE_AP, timeout=30)
        _add_profile(ctx, "ap", spec)
        if activate:
            ctx.nm("radio", "wifi", "on", timeout=15)
            ctx.nm("--wait", "30", "connection", "up", "id", PROFILE_AP, "ifname", iface, timeout=60)
    else:
        via = "keyfile"
        text = keyfile_text(spec, str(uuid.uuid4()))
        os.makedirs(ctx.keyfile_dir, mode=0o755, exist_ok=True)
        path = os.path.join(ctx.keyfile_dir, PROFILE_AP + ".nmconnection")
        try:
            os.unlink(path)
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(path, 0o600)
    save_state(ctx, priv, pub)
    return {"ok": True, "code": "default_ap", "message": f"Wi-Fi access point '{ssid}' is the default (password {psk}, http://{AP_GATEWAY}:8080).", "via": via, "ssid": ssid}


# ====================================================================== the clock (chrony)
CHRONY_HEADER = "# Written by Pi Automation Traffic Tester (Settings -> Time source). Changes here are overwritten."


def clock_text(mode: str, server: Optional[str] = None) -> str:
    lines = [CHRONY_HEADER, f"# mode: {mode}"]
    if mode == "serve":
        lines += ["# This Pi is the clock source for the other Pis and the laptop (never the 5G link under test).", "local stratum 10"]
    elif mode == "client":
        lines += [f"server {server} iburst prefer minpoll 0 maxpoll 4"]
    lines += [f"allow {r}" for r in PRIVATE_RANGES]
    return "\n".join(lines) + "\n"


def clock_status(ctx: Ctx) -> dict:
    try:
        with open(ctx.chrony_conf) as f:
            txt = f.read()
    except OSError:
        return {"mode": "none", "server": None, "configured": False}
    server = None
    mode = "none"
    for line in txt.splitlines():
        t = line.strip()
        if re.match(r"^local\s+stratum\b", t):
            mode = "serve"
        m = re.match(r"^server\s+(\S+)\b", t)
        if m:
            mode, server = "client", m.group(1)
    return {"mode": mode, "server": server, "configured": True}


def _restart_chrony(ctx: Ctx) -> bool:
    for unit in ("chrony", "chronyd"):
        if ctx.runner.run(["systemctl", "restart", unit], 40).rc == 0:
            return True
    return False


def op_clock(ctx: Ctx, req: dict) -> dict:
    mode = req.get("mode")
    if mode == "status":
        return {"ok": True, "code": "status", "message": "ok", **clock_status(ctx)}
    if mode not in ("serve", "client", "none"):
        raise NetError("bad_request", "The time source must be 'serve' (this Pi is the clock), 'client' (use another Pi's clock) or 'status'.")
    server = None
    if mode == "client":
        ip = v_ipv4(req.get("server"), "time server")
        _unicast(ip, "time server")
        server = str(ip)
    elif req.get("server") not in (None, ""):
        raise NetError("bad_request", "A server address only makes sense for 'client'.")
    conf_dir = os.path.dirname(ctx.chrony_conf)
    os.makedirs(conf_dir, mode=0o755, exist_ok=True)
    try:                                                       # chrony only reads conf.d when chrony.conf asks for it (Debian's does; keep older ones working)
        with open(ctx.chrony_main) as f:
            main_txt = f.read()
        if not re.search(r"^\s*confdir\s+" + re.escape(conf_dir) + r"\s*$", main_txt, re.M):
            with open(ctx.chrony_main, "a") as f:
                f.write(f"\nconfdir {conf_dir}\n")
    except OSError:
        pass
    tmp = f"{ctx.chrony_conf}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(clock_text(mode, server))
    os.chmod(tmp, 0o644)
    os.replace(tmp, ctx.chrony_conf)
    restarted = _restart_chrony(ctx)
    return {"ok": True, "code": "clock", "message": {"serve": "This Pi is now the time source for the others.", "client": f"This Pi now takes its time from {server}.", "none": "This Pi uses the default time servers."}[mode],
            "restarted": restarted, **clock_status(ctx)}


# ====================================================================== the dead-man fallback: decision + sampling + loop
def sample_has_uplink(s: dict) -> bool:
    """A non-AP interface with a link and an address (Ethernet, a USB adapter, or Wi-Fi joined to a network)."""
    return any(l.get("kind") != "ap" and l.get("carrier") and l.get("ip") for l in s.get("links", []))


def sample_has_network(s: dict) -> bool:
    return sample_has_uplink(s) or int(s.get("ap_clients") or 0) > 0


def fallback_decision(samples: List[dict], now: float, state: Optional[dict] = None, *, enabled: bool = True, grace: float = GRACE_S,
                      retry_every: float = RETRY_EVERY_S, retry_window: float = RETRY_WINDOW_S) -> dict:
    """Pure: what should the dead-man loop do now? samples = [{"t","links":[{"iface","kind","carrier","ip"}],"ap_clients","ap_active","fallback_active"}], oldest first.
    Returns {"action": none|ap_up|ap_down|retry|retry_ok, "reason", "state"}; feed `state` back in next time (phase normal|fallback|retry)."""
    st = dict(state or {})
    st.setdefault("phase", "normal")

    def out(action: str, reason: str) -> dict:
        return {"action": action, "reason": reason, "state": st}
    if not samples:
        return out("none", "no samples yet")
    cur = samples[-1]
    if st["phase"] == "normal" and cur.get("fallback_active"):                              # the watcher was restarted while the emergency AP was up
        st.update(phase="fallback", since=now, last_retry=now)
    if not enabled:
        if st["phase"] != "normal" or cur.get("fallback_active"):
            st.update(phase="normal", since=None)
            return out("ap_down", "the emergency access point was switched off in Settings")
        return out("none", "fallback is switched off")
    if st["phase"] == "normal":
        if sample_has_network(cur):
            return out("none", "network is up")
        since = None
        for s in reversed(samples):
            if sample_has_network(s):
                break
            since = s["t"]
        if since is None or now - since < grace:
            return out("none", f"no network for {int(now - (since or now))} s (waiting {int(grace)} s)")
        if cur.get("ap_active"):
            return out("none", "an access point is already up")
        st.update(phase="fallback", since=now, last_retry=now)
        return out("ap_up", f"no network for {int(now - since)} s")
    if st["phase"] == "fallback":
        if int(cur.get("ap_clients") or 0) > 0:
            st["last_retry"] = now                                                          # somebody is using it: do not kick them off to look for the old Wi-Fi
            return out("none", "a phone or laptop is connected to the emergency access point")
        if sample_has_uplink(cur):
            st.update(phase="normal", since=None)
            return out("ap_down", "the network came back (cable or Wi-Fi)")
        if now - float(st.get("last_retry") or now) >= retry_every:
            st.update(phase="retry", retry_until=now + retry_window)
            return out("retry", "trying the normal network settings again")
        return out("none", "emergency access point is up")
    # phase == "retry": the AP is down while the normal profiles are tried
    if sample_has_uplink(cur):
        st.update(phase="normal", since=None)
        return out("retry_ok", "the normal network is back")
    if now >= float(st.get("retry_until") or now):
        st.update(phase="fallback", last_retry=now)
        return out("ap_up", "the normal network did not come back")
    return out("none", "retrying")


class SysProbe:
    """Samples the real system: interfaces with a link and an IPv4 address, whether an access point is up and how many stations it has."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def sample(self) -> dict:
        ctx = self.ctx
        ips: Dict[str, bool] = {}
        r = ctx.runner.run(["ip", "-4", "-o", "addr", "show"], 10)
        for line in r.out.splitlines() if r.ok else []:
            m = re.match(r"\d+:\s+(\S+)\s+inet\s+(\S+)", line)
            if m and not m.group(2).startswith(("169.254.", "127.")):
                ips[m.group(1).split("@")[0]] = True
        active = {a["device"]: a["name"] for a in nm_active(ctx)} if nm_running(ctx) else {}
        links = []
        for n in sys_ifaces(ctx):
            if n.startswith(SKIP_IFACE_PREFIX):
                continue
            kind = "wifi" if is_wifi_iface(ctx, n) else "eth"
            if kind == "wifi" and active.get(n) in (PROFILE_AP, PROFILE_FALLBACK):
                kind = "ap"
            try:
                with open(os.path.join(ctx.sysfs, n, "carrier")) as f:
                    car = f.read().strip() == "1"
            except OSError:
                car = False
            links.append({"iface": n, "kind": kind, "carrier": car or kind == "ap", "ip": bool(ips.get(n))})
        clients = 0
        for l in links:
            if l["kind"] == "ap":
                rr = ctx.runner.run(["iw", "dev", l["iface"], "station", "dump"], 10)
                clients += len(re.findall(r"^Station ", rr.out, re.M)) if rr.ok else 0
        return {"t": ctx.now(), "links": links, "ap_clients": clients, "ap_active": any(l["kind"] == "ap" for l in links),
                "fallback_active": PROFILE_FALLBACK in active.values()}


class FakeProbe:
    """Container/unit tests: <state_dir>/fake-probe.json = {"links": [...], "ap_clients": n}; ap state comes from the fake NetworkManager."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def sample(self) -> dict:
        d = read_json(self.ctx.path("fake-probe.json"), {}) or {}
        act = nm_active(self.ctx)
        names = {a["name"] for a in act}
        links = list(d.get("links", []))
        return {"t": self.ctx.now(), "links": links, "ap_clients": int(d.get("ap_clients", 0)), "ap_active": bool(names & {PROFILE_AP, PROFILE_FALLBACK}) or any(l.get("kind") == "ap" for l in links),
                "fallback_active": PROFILE_FALLBACK in names}


def act_on(ctx: Ctx, action: str) -> Optional[dict]:
    """Do what the decision says (root, in-process). Returns the op result or None."""
    if action == "ap_up":
        return op_fallback_ap(ctx)
    if action == "ap_down":
        return op_fallback_off(ctx)
    if action == "retry":
        return op_fallback_retry(ctx)
    if action == "retry_ok":
        return op_fallback_done(ctx)
    return None


def capture_active(ctx: Ctx) -> bool:
    """wifi_helper.py keeps its state in <HELPER_DIR>/state.json; 'monitor'/'switching' means the radio is borrowed by a capture."""
    d = os.environ.get("TT_WIFI_HELPER_DIR", "/run/tt-wifi")
    st = read_json(os.path.join(d, "state.json"), {}) or {}
    return st.get("state") in ("monitor", "switching")


def check_expired_revert(ctx: Ctx) -> Optional[dict]:
    """The revert timer is a transient systemd unit; if the Pi rebooted (or the unit was lost) while a change was pending, undo it when the deadline has passed."""
    p = get_pending(ctx)
    if p and ctx.now() > float(p.get("deadline") or 0) + 15:
        return op_revert(ctx, {}, reason="timeout", force=True)
    return None


def watch_tick(ctx: Ctx, samples: List[dict], state: dict, probe=None, now: Optional[float] = None) -> dict:
    """One iteration of the dead-man loop (also used by `watch --once`). Mutates `samples`; returns the decision."""
    if capture_active(ctx):                                    # built-in Wi-Fi is in monitor mode for a sniffer capture: its missing address is deliberate
        samples.clear()
        return {"action": "none", "reason": "built-in Wi-Fi is in monitor mode for a capture", "state": state}
    probe = probe or ctx.probe or SysProbe(ctx)
    s = probe.sample()
    if now is not None:
        s["t"] = now
    samples.append(s)
    t = s["t"]
    while samples and samples[0]["t"] < t - (GRACE_S + 3 * SAMPLE_EVERY_S + 60):
        samples.pop(0)
    pub = get_public(ctx)
    dec = fallback_decision(samples, t, state, enabled=bool(pub["fallback"]["enabled"]))
    state.clear()
    state.update(dec["state"])
    if dec["action"] != "none":
        try:
            with _Lock(ctx):
                dec["result"] = act_on(ctx, dec["action"])
        except NetError as e:
            dec["error"] = e.message
    try:
        with _Lock(ctx):
            check_expired_revert(ctx)
    except NetError:
        pass
    return dec


def watch_loop(ctx: Ctx, once: bool = False, now_fn: Optional[Callable[[], float]] = None, sleep: Callable[[float], None] = time.sleep, max_ticks: Optional[int] = None) -> int:
    samples: List[dict] = read_json(ctx.path(F_WATCH), {}).get("samples", []) if once else []
    state: dict = (read_json(ctx.path(F_WATCH), {}) or {}).get("state", {}) if once else {}
    n = 0
    while True:
        now = now_fn() if now_fn else None
        dec = watch_tick(ctx, samples, state, now=now)
        if dec["action"] != "none":
            print(json.dumps({"action": dec["action"], "reason": dec["reason"]}), flush=True)
        try:
            write_json(ctx.path(F_BEAT), {"t": ctx.now()}, 0o644)
            if once:
                write_json(ctx.path(F_WATCH), {"samples": samples, "state": state}, 0o644)
        except OSError:
            pass
        n += 1
        if once or (max_ticks and n >= max_ticks):
            return 0
        sleep(SAMPLE_EVERY_S)


# ====================================================================== dispatch
def _ensure_ctx_dir(ctx: Ctx) -> None:
    os.makedirs(ctx.state_dir, mode=0o755, exist_ok=True)


# ====================================================================== first boot: name this Pi (card image) + Imager Wi-Fi
SETUP_SSID = "pi-setup"                                          # the card image's access point until the Pi is named
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?\Z")
CAPTIVE_REL = "NetworkManager/dnsmasq-shared.d/tt-captive.conf"  # "every name = this Pi": makes a phone open the setup page by itself; removed when the Pi is named
CAPTIVE_TEXT = f"# Pi Automation Traffic Tester first boot: every name resolves to this Pi so a phone opens the setup page. Removed when the Pi is named.\naddress=/#/{AP_GATEWAY}\n"
RESTART_AP_UNIT = "tt-ap-restart"


def v_hostname(name) -> str:
    n = str(name or "").strip().lower()
    if not HOST_RE.match(n):
        raise NetError("bad_name", "Use 1 to 32 letters, digits or dashes (no spaces); it becomes the Pi's address name (pi1.local) and its Wi-Fi name.")
    return n


def _write_hostname_files(ctx: Ctx, host: str) -> None:
    os.makedirs(ctx.etc_dir, exist_ok=True)
    with open(os.path.join(ctx.etc_dir, "hostname"), "w") as f:
        f.write(host + "\n")
    hp = os.path.join(ctx.etc_dir, "hosts")
    try:
        with open(hp) as f:
            lines = f.read().splitlines()
    except OSError:
        lines = ["127.0.0.1\tlocalhost"]
    lines = [ln for ln in lines if not re.match(r"^127\.0\.1\.1\s", ln)]
    lines.insert(1 if lines else 0, f"127.0.1.1\t{host}")
    with open(hp, "w") as f:
        f.write("\n".join(lines) + "\n")


def op_set_name(ctx: Ctx, req: dict) -> dict:
    """Name this Pi: hostname (so <name>.local works) + the access point's Wi-Fi name (when it still carries the setup / old default name) + drop the setup-time
    captive DNS. The access point restarts a few seconds LATER (systemd-run timer) so the answer reaches the phone that is joined to it first."""
    n = clean_text(req.get("name") or "", 40).strip()
    host = v_hostname(n)
    old = clean_text(req.get("old_name") or "", 40)
    try:
        old_host = socket.gethostname().split(".")[0]
    except OSError:
        old_host = ""
    _write_hostname_files(ctx, host)
    ctx.runner.run(["hostnamectl", "set-hostname", host], 20)
    priv = get_private(ctx)
    ap = priv.get("ap") or {}
    old_ssid = ap.get("ssid") or ""
    renamed = False
    if ap and (old_ssid in (SETUP_SSID, old_host, old) or req.get("rename_ap") is True):
        ssid = v_ssid(host)
        ap["ssid"] = ssid
        priv["ap"] = ap
        present = []
        if nm_running(ctx):
            try:
                present = [p["name"] for p in nm_profiles(ctx)]
            except NetError:
                present = []
        if PROFILE_AP in present:
            r = ctx.nm("connection", "modify", "id", PROFILE_AP, "802-11-wireless.ssid", ssid, timeout=20)
            if r.rc:
                raise NetError("modify_failed", "The Wi-Fi name could not be changed: " + friendly_nm_error(r.err))
        else:
            path = os.path.join(ctx.keyfile_dir, PROFILE_AP + ".nmconnection")
            if os.path.isfile(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(keyfile_text({**ap, "iface": ap.get("iface") or "wlan0", "band": ap.get("band") or "bg"}, str(uuid.uuid4())))
        save_state(ctx, priv, get_public(ctx))
        renamed = True
    cp = os.path.join(ctx.etc_dir, CAPTIVE_REL)
    had_captive = os.path.isfile(cp)
    try:
        os.unlink(cp)
    except OSError:
        pass
    if renamed or had_captive:
        ctx.runner.run(["systemd-run", "--on-active=6s", f"--unit={RESTART_AP_UNIT}", "--collect", "--description=Pi Automation Traffic Tester: restart the Wi-Fi access point under its new name",
                        "nmcli", "connection", "up", "id", PROFILE_AP], 20)
    return {"ok": True, "code": "named", "message": f"This Pi is now called {host}.", "hostname": host, "ssid": (priv.get("ap") or {}).get("ssid"), "ap_renamed": renamed}


def _foreign_wifi(ctx: Ctx) -> List[str]:
    """Wi-Fi client networks that were NOT made by this app: what Raspberry Pi Imager's 'Edit settings' (cloud-init / netplan) leaves behind."""
    out: List[str] = []
    try:
        for p in nm_profiles(ctx):
            if p["type"] == "wifi" and not is_tt_name(p["name"]):
                out.append(p["name"])
    except NetError:
        pass
    npdir = os.path.join(ctx.etc_dir, "netplan")
    try:
        for fn in sorted(os.listdir(npdir)):
            if fn.endswith((".yaml", ".yml")):
                with open(os.path.join(npdir, fn)) as f:
                    txt = f.read()
                if re.search(r"^\s*wifis:\s*$", txt, re.M) and re.search(r"access-points:", txt):
                    out.append("netplan:" + fn)
    except OSError:
        pass
    return out


def firstboot(ctx: Ctx, country: str = "US") -> dict:
    """Run once at the first boot of a card image (traffic-tester-firstboot.service): switch the radio on (Raspberry Pi OS keeps Wi-Fi blocked until a country is set), and if
    the person gave their own Wi-Fi in Raspberry Pi Imager, let the Pi JOIN it instead of starting its own network (the emergency access point still starts after 3 minutes
    without any network, so a typo in the Wi-Fi password never locks the Pi out)."""
    notes: List[str] = []
    ctx.runner.run(["rfkill", "unblock", "wifi"], 10)
    reg = ctx.runner.run(["iw", "reg", "get"], 10)
    if reg.rc == 0 and re.search(r"^country 00", reg.out, re.M):
        if ctx.runner.run(["raspi-config", "nonint", "do_wifi_country", country], 30).rc != 0:        # persistent (survives a reboot) when raspi-config exists
            ctx.runner.run(["iw", "reg", "set", country], 10)
        notes.append(f"Wi-Fi country was not set: using {country} (change it in raspi-config if you are elsewhere)")
    foreign = _foreign_wifi(ctx)
    priv = get_private(ctx)
    ap = priv.get("ap")
    if foreign and ap and ap.get("autoconnect", True):
        ap["autoconnect"] = False
        priv["ap"] = ap
        if nm_running(ctx):
            ctx.nm("connection", "modify", "id", PROFILE_AP, "connection.autoconnect", "no", timeout=20)
            ctx.nm("connection", "down", "id", PROFILE_AP, timeout=30)                  # it may already be up (NetworkManager starts before this runs): hand the radio to the user's Wi-Fi
            _reconnect_normal(ctx, 25)
        else:
            path = os.path.join(ctx.keyfile_dir, PROFILE_AP + ".nmconnection")
            try:
                with open(path) as f:
                    txt = f.read()
                with open(path, "w") as f:
                    f.write(txt.replace("autoconnect=true", "autoconnect=false", 1))
            except OSError:
                pass
        save_state(ctx, priv, get_public(ctx))
        notes.append("your own Wi-Fi from Imager is used; the Pi's own network stays off")
    else:
        notes.append("no Wi-Fi from Imager: the Pi's own network '%s' is on" % ((ap or {}).get("ssid") or SETUP_SSID))
    return {"ok": True, "code": "firstboot", "message": "; ".join(notes), "own_wifi": bool(foreign)}


def op_claude_cleanup(ctx: Ctx, req: dict) -> dict:
    """Remove what the old 'Ask Claude' feature left on this Pi (see claude_cleanup.py). Safe to repeat."""
    from . import claude_cleanup as cc
    conf = read_conf(os.environ.get("TT_NET_CONF", SYSTEM_CONF))
    app = conf.get("APP_DIR") or os.environ.get("TT_APP_DIR")
    lines: List[str] = []
    removed = cc.cleanup(ctx.data_dir or "", app, ctx.etc_dir, log=lines.append)
    for ln in lines:
        print(ln, file=sys.stderr)                                    # journal: journalctl -u traffic-tester-update
    return {"ok": True, "removed": removed, "message": f"Removed {len(removed)} leftover(s) of the old Ask Claude feature." if removed else "Nothing to remove."}


OPS: Dict[str, Callable[[Ctx, dict], dict]] = {
    "claude_cleanup": op_claude_cleanup,
    "apply": op_apply, "keep": op_keep, "revert": lambda c, r: op_revert(c, r, "manual", True), "scan": op_scan, "fallback_ap": op_fallback_ap, "fallback_off": op_fallback_off,
    "set_name": op_set_name, "fallback_retry": op_fallback_retry, "fallback_done": op_fallback_done, "ensure_wifi": op_ensure_wifi, "set_fallback": op_set_fallback,
}


def handle(ctx: Ctx, req: dict) -> dict:
    """Run one request ({"action": "net", "op": ..., "id": ...} or {"action": "clock", "mode": ...}); always returns a result dict and writes net-result.json."""
    rid = clean_text(req.get("id", ""), 64) if isinstance(req, dict) else ""
    if rid and not ID_RE.match(rid):
        rid = ""
    try:
        _ensure_ctx_dir(ctx)
        if not isinstance(req, dict):
            raise NetError("bad_request", "The request is not valid.")
        action = req.get("action")
        if action == "clock":
            with _Lock(ctx):
                res = op_clock(ctx, req)
            op = "clock"
        elif action == "net":
            op = req.get("op")
            if op not in OPS:
                raise NetError("bad_request", "Unknown network operation.")
            with _Lock(ctx):
                res = OPS[op](ctx, req)
        else:
            raise NetError("bad_request", "Unknown request.")
    except NetError as e:
        res, op = {"ok": False, "code": e.code, "message": e.message}, (req.get("op") if isinstance(req, dict) else None)
    except Exception as e:                                                                      # noqa: BLE001
        res, op = {"ok": False, "code": "internal", "message": f"Unexpected problem ({type(e).__name__}). Nothing was left half-done; see the journal: journalctl -u traffic-tester-update."}, None
    res = {**res, "id": rid, "op": op, "time": ctx.now()}
    try:
        write_json(ctx.path(F_RESULT), res, 0o644)
    except OSError:
        pass
    return res


def revert_main(state_dir: Optional[str] = None, force: bool = False) -> int:
    """`helper.py net-revert` (fired by the systemd timer)."""
    ctx = make_ctx(state_dir)
    try:
        with _Lock(ctx):
            res = op_revert(ctx, {}, reason="timeout" if not force else "manual", force=force)
    except NetError as e:
        res = {"ok": False, "code": e.code, "message": e.message}
    print(res.get("code"), res.get("message"))
    return 0 if res.get("ok") else 1


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: netops.py watch [--once] | ap-ssid | set-name NAME | firstboot [--country US] | setup-default-ap [--ssid S] [--password P] [--activate] [--keyfile-only] | clock serve|client|none|status [--server IP] | net-revert [--force]", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]

    def opt(name: str, default=None):
        return rest[rest.index(name) + 1] if name in rest and rest.index(name) + 1 < len(rest) else default
    ctx = make_ctx()
    try:
        if cmd == "watch":
            return watch_loop(ctx, once="--once" in rest, now_fn=(lambda: float(opt("--now"))) if opt("--now") and (ctx.fake or os.environ.get("TT_NET_TEST_CLOCK") == "1") else None)
        if cmd == "net-revert":
            return revert_main(ctx.state_dir, "--force" in rest)
        if cmd == "set-name":
            with _Lock(ctx):
                res = op_set_name(ctx, {"name": rest[0] if rest else ""})
            print(res["message"])
            return 0
        if cmd == "firstboot":
            with _Lock(ctx):
                res = firstboot(ctx, opt("--country") or "US")
            print(res["message"])
            return 0
        if cmd == "ap-ssid":
            print(((get_public(ctx).get("profiles") or {}).get(PROFILE_AP) or {}).get("ssid") or "")
            return 0
        if cmd == "setup-default-ap":
            with _Lock(ctx):
                res = setup_default_ap(ctx, opt("--ssid"), opt("--password") or DEFAULT_PSK, "--activate" in rest, "--keyfile-only" in rest)
            print(res["message"], f"[{res['via']}]")
            return 0
        if cmd == "clock":
            with _Lock(ctx):
                res = op_clock(ctx, {"mode": rest[0] if rest else "status", "server": opt("--server")})
            print(json.dumps(res))
            return 0
    except NetError as e:
        print(f"ERROR: {e.message}", file=sys.stderr)
        return 1
    print("unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
