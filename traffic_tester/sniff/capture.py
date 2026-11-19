"""Ethernet / Wi-Fi capture: dumpcap supervisor (tcpdump fallback), inline bridge, live feeder, Wi-Fi monitor set-up."""
from __future__ import annotations

import glob
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import Counter
from typing import Dict, List, Optional

from . import analyze, bpf as bpfmod, display, pcapng, workbench


class CaptureError(Exception):
    pass


def find_tools() -> Dict[str, Optional[str]]:
    t = {n: shutil.which(n) or next((p for p in (f"/usr/sbin/{n}", f"/sbin/{n}", f"/usr/bin/{n}") if os.path.exists(p)), None)
         for n in ("dumpcap", "tcpdump", "tshark", "iw", "ip", "ethtool")}
    t["engine"] = "dumpcap" if t["dumpcap"] else ("tcpdump" if t["tcpdump"] else None)
    return t


def _run(cmd: List[str], timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _plain_error(stderr: str, what: str) -> str:
    s = (stderr or "").strip()
    low = s.lower()
    if "permission" in low or "not permitted" in low or "operation not permitted" in low or "don't have permission" in low:
        return f"{what}: the app has no permission to capture. This copy of the app was started without it; the full install (sudo scripts/install-pi.sh) sets the capture permissions."
    if "no such device" in low or "no suitable device" in low or "does not exist" in low or "cannot find device" in low:
        return f"{what}: that network port does not exist. Check the name in the settings."
    if "syntax error" in low or "can't parse filter" in low:
        return f"{what}: the capture filter is not valid."
    last = s.splitlines()[-1] if s else "unknown error"
    return f"{what}: {last}"


# ------------------------------------------------------------------ inline bridge
class BridgeError(Exception):
    pass


class EthBridge:
    """Transparent L2 bridge between two ports: no IP address, STP off, link-local frames forwarded, offloads off."""

    def __init__(self, legs: List[str], name: str = "ttbr0", passthrough: bool = False):
        self.legs, self.name, self.passthrough = legs, name, passthrough
        self.tools = find_tools()
        self.up_done = False
        self.stop_evt = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.forced: Dict[str, bool] = {}
        self.events: List[str] = []
        self.up_grace_s = 3.0                    # a port that was just re-enabled has no carrier for ~1.5 s: that is not a new link loss
        self._quiet_until: Dict[str, float] = {}

    def _ip(self, *a: str, check: bool = True) -> str:
        if not self.tools["ip"]:
            raise BridgeError("The 'ip' tool (iproute2) is not installed.")
        r = _run([self.tools["ip"], *a])
        if r.returncode and check:
            err = (r.stderr or r.stdout).strip()
            if "not permitted" in err.lower() or "permission" in err.lower():
                raise BridgeError("The app has no permission to change network settings (needs CAP_NET_ADMIN). This copy of the app was started without it; the full install (sudo scripts/install-pi.sh) grants it to the service.")
            raise BridgeError(f"ip {' '.join(a)}: {err}")
        return r.stdout

    def up(self) -> None:
        for leg in self.legs:
            if not os.path.exists(f"/sys/class/net/{leg}"):
                raise BridgeError(f"Network port '{leg}' does not exist.")
        if len(set(self.legs)) != 2:
            raise BridgeError("The inline tap needs two different network ports.")
        self.down(quiet=True)
        self.stop_evt.clear()                    # down() set it: without this the link-passthrough thread would exit at once
        self._ip("link", "add", "name", self.name, "type", "bridge", "stp_state", "0", "forward_delay", "0", "vlan_filtering", "0")
        self._ip("link", "set", "dev", self.name, "type", "bridge", "group_fwd_mask", "0xfff8", check=False)      # forward LLDP and other link-local frames (not STP/pause/LACP)
        self._ip("link", "set", "dev", self.name, "addrgenmode", "none", check=False)
        for leg in self.legs:
            self._ip("addr", "flush", "dev", leg, check=False)
            self._ip("link", "set", "dev", leg, "addrgenmode", "none", check=False)
            self._ip("link", "set", "dev", leg, "promisc", "on", check=False)
            if self.tools["ethtool"]:                      # offloads would hand the capture jumbo "frames" and strip VLAN tags
                _run([self.tools["ethtool"], "-K", leg, "gro", "off", "lro", "off", "tso", "off", "gso", "off", "rxvlan", "off", "txvlan", "off"])
            self._ip("link", "set", "dev", leg, "master", self.name)
            self._ip("link", "set", "dev", leg, "up")
        self._ip("link", "set", "dev", self.name, "up")
        self.up_done = True
        if self.passthrough:
            self.thread = threading.Thread(target=self._watch, name="link-passthrough", daemon=True)
            self.thread.start()

    def down(self, quiet: bool = False) -> None:
        self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=3)
        for leg in self.legs:
            if os.path.exists(f"/sys/class/net/{leg}"):
                self._ip("link", "set", "dev", leg, "up", check=False)
                self._ip("link", "set", "dev", leg, "nomaster", check=False)
                self._ip("link", "set", "dev", leg, "promisc", "off", check=False)
                self._ip("link", "set", "dev", leg, "addrgenmode", "eui64", check=False)
        if os.path.exists(f"/sys/class/net/{self.name}"):
            self._ip("link", "del", self.name, check=False)
        self.up_done = False

    # link-state passthrough: if one side loses its cable partner, take the other leg down too so that its partner sees the loss
    def _carrier(self, leg: str) -> bool:
        try:
            return open(f"/sys/class/net/{leg}/carrier").read().strip() == "1"
        except OSError:
            return False

    def _watch(self) -> None:
        a, b = self.legs
        last_probe = time.monotonic()
        while not self.stop_evt.wait(0.4):
            ca, cb = self._carrier(a), self._carrier(b)
            now = time.monotonic()
            for leg, other, c_leg in ((a, b, ca), (b, a, cb)):
                if not c_leg and not self.forced.get(leg) and not self.forced.get(other) and now >= self._quiet_until.get(leg, 0.0):
                    self._ip("link", "set", "dev", other, "down", check=False)
                    self.forced[other] = True
                    self.events.append(f"{time.strftime('%H:%M:%S')} {leg} lost its link: {other} taken down (link-state passthrough)")
            if any(self.forced.values()) and time.monotonic() - last_probe > 2.0:     # probe: is the partner back?
                last_probe = time.monotonic()
                for leg in list(self.forced):
                    if self.forced.get(leg):
                        other = b if leg == a else a
                        if self._carrier(other):                                       # the leg we kept up has its partner again
                            self._ip("link", "set", "dev", leg, "up", check=False)
                            self.forced[leg] = False
                            self._quiet_until[leg] = time.monotonic() + self.up_grace_s      # let it renegotiate before its carrier counts again
                            self.events.append(f"{time.strftime('%H:%M:%S')} link restored: {leg} brought up")


# ------------------------------------------------------------------ dumpcap / tcpdump
class Capture:
    """One capture process writing rotating files. Supervises it, never touches packets itself."""

    def __init__(self, interfaces: List[str], out_dir: str, bpf: str = "", ring: Optional[dict] = None, snaplen: int = 0, duration_s: float = 0, base: str = "capture",
                 max_kb: int = 0):
        self.interfaces, self.out_dir, self.bpf, self.snaplen, self.duration_s, self.base = interfaces, out_dir, (bpf or "").strip(), snaplen, duration_s, base
        if self.bpf.startswith("-"):               # never let filter text be read as tcpdump / dumpcap options
            raise CaptureError(bpfmod.LEADING_DASH)
        self.max_kb = max_kb                       # hard size limit for an unbounded (non-ring) capture = free space minus the guard
        self.ring = ring or {}
        self.tools = find_tools()
        self.engine = self.tools["engine"]
        if not self.engine:
            raise CaptureError("No capture tool found. Install it with: sudo apt install tshark tcpdump (the installer does this).")
        self.proc: Optional[subprocess.Popen] = None
        self.stderr_lines: List[str] = []
        self.stats_text = ""
        self.cmd: List[str] = []                   # the command start() ran (stop() reports it; it must never raise from rebuilding it)
        os.makedirs(out_dir, exist_ok=True)
        self.suffix = ".pcapng" if self.engine == "dumpcap" else ".pcap"

    def command(self) -> List[str]:
        r = self.ring
        mb, mins, files = int(r.get("ring_mb", 0)), float(r.get("ring_minutes", 0)), int(r.get("ring_files", 0))
        out = os.path.join(self.out_dir, self.base + self.suffix)
        if self.engine == "dumpcap":
            cmd = [self.tools["dumpcap"], "-n", "-B", "16"]
            for i in self.interfaces:
                cmd += ["-i", i]
            if self.bpf:
                cmd += ["-f", self.bpf]
            if self.snaplen:
                cmd += ["-s", str(self.snaplen)]
            cmd += ["-w", out]
            if mb:
                cmd += ["-b", f"filesize:{mb * 1024}"]
            if mins:
                cmd += ["-b", f"duration:{int(mins * 60)}"]
            if files and (mb or mins):
                cmd += ["-b", f"files:{files}"]
            if self.duration_s:
                cmd += ["-a", f"duration:{int(self.duration_s)}"]
            if self.max_kb and not mb:
                cmd += ["-a", f"filesize:{int(self.max_kb)}"]
            elif self.max_kb and not files:                # with -b, "-a filesize" only switches files (dumpcap(1)) and would replace the ring file size:
                cmd += ["-a", f"files:{max(1, int(self.max_kb) // (mb * 1024))}"]      # stop after as many ring files as fit instead
            return cmd
        if len(self.interfaces) != 1:
            raise CaptureError("tcpdump can capture only one port at a time; install tshark (dumpcap) for several.")
        cmd = [self.tools["tcpdump"], "-i", self.interfaces[0], "-nn", "-U", "-w", out]
        if self.snaplen:
            cmd += ["-s", str(self.snaplen)]
        else:
            cmd += ["-s", "0"]
        if mb:
            cmd += ["-C", str(mb)]
            if files:
                cmd += ["-W", str(files)]
        if self.bpf:
            cmd += ["--"] + self.bpf.split()       # "--": everything after it is the filter, never an option
        return cmd

    def start(self) -> None:
        cmd = self.cmd = self.command()
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
        threading.Thread(target=self._drain, daemon=True).start()
        for _ in range(12):                                   # fail fast with a plain message if it dies at once
            time.sleep(0.15)
            if self.proc.poll() is not None:
                time.sleep(0.2)
                raise CaptureError(_plain_error("\n".join(self.stderr_lines), "Capture could not start"))

    def _drain(self) -> None:
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip())
            if len(self.stderr_lines) > 200:
                del self.stderr_lines[:100]

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def stop(self) -> dict:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
            except OSError:
                pass
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        time.sleep(0.2)
        text = "\n".join(self.stderr_lines)
        info = {"engine": self.engine, "cmd": " ".join(self.cmd), "returncode": self.proc.returncode if self.proc else None}
        m = re.search(r"Packets captured:\s*(\d+)", text)
        if m:
            info["packets_captured"] = int(m.group(1))
        recv = drop = 0
        for a, b in re.findall(r"Packets received/dropped on interface [^:]+:\s*(\d+)/(\d+)", text):
            recv, drop = recv + int(a), drop + int(b)
        m2 = re.search(r"(\d+) packets captured", text)
        m3 = re.search(r"(\d+) packets dropped by kernel", text)
        if m2:
            info["packets_captured"] = int(m2.group(1))
        info["kernel_received"], info["kernel_dropped"] = (recv, drop) if recv else (int(m2.group(1)) if m2 else None, int(m3.group(1)) if m3 else 0)
        info["stderr_tail"] = text.splitlines()[-6:]
        return info

    def files(self) -> List[str]:
        base = os.path.join(self.out_dir, self.base)
        found = []                                 # dumpcap: capture_00001_<time>.pcapng; tcpdump -C/-W: capture.pcap, capture.pcap0, capture.pcap1 ...
        for f in glob.glob(base + "*" + self.suffix + "*"):
            m = re.search(re.escape(self.suffix) + r"(\d*)$", f)
            if m:
                found.append((f[:m.start(1)], int(m.group(1) or -1), f))
        return [f for _stem, _num, f in sorted(found)]

    def bytes_written(self) -> int:
        return sum(os.path.getsize(f) for f in self.files() if os.path.exists(f))


class LiveFeeder(threading.Thread):
    """Best-effort live view: a second short-snaplen capture streamed as pcapng into a bounded decoder (never slows the file capture)."""

    MAX_DECODE_PER_SEC = 400
    KEEP_ROWS = 20000                       # newest packets the list can scroll through (the saved file always has everything)

    def __init__(self, interfaces: List[str], bpf: str = ""):
        super().__init__(name="live-feeder", daemon=True)
        self.tools = find_tools()
        self.interfaces, self.bpf = interfaces, bpf
        self.proc: Optional[subprocess.Popen] = None
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.rows: List[dict] = []
        self.an = workbench.Analyzer()
        self.n = 0
        self.seen = self.decoded = 0
        self.bytes = 0
        self.proto: Counter = Counter()
        self.error: Optional[str] = None
        self._sec, self._budget = 0, self.MAX_DECODE_PER_SEC
        self.parser = pcapng.StreamParser()
        self.data: Dict[int, bytes] = {}

    def start_proc(self) -> None:
        if not self.tools["dumpcap"]:
            self.error = "Live view needs dumpcap (apt install tshark)."
            return
        cmd = [self.tools["dumpcap"], "-q", "-n", "-B", "4", "-s", "512"]
        for i in self.interfaces:
            cmd += ["-i", i]
        if self.bpf:
            cmd += ["-f", self.bpf]
        cmd += ["-w", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.start()

    def run(self) -> None:
        fd = self.proc.stdout.fileno()
        while not self.stop_evt.is_set():
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            for p in self.parser.feed(chunk):
                self.seen += 1
                self.bytes += p["orig_len"]
                sec = int(time.monotonic())
                if sec != self._sec:
                    self._sec, self._budget = sec, self.MAX_DECODE_PER_SEC
                if self._budget <= 0:
                    continue
                self._budget -= 1
                with self.lock:
                    row = self.an.add(p["ts_ns"], p["data"], p["linktype"], "", p["name"], p["orig_len"])
                    self.decoded += 1
                    self.proto[row["proto"]] += 1
                    self.n = row["n"]
                    self.rows.append(row)
                    self.data[row["n"]] = p["data"][:workbench.DATA_KEEP]
                    if len(self.rows) > self.KEEP_ROWS:
                        for old in self.rows[:4000]:
                            self.data.pop(old["n"], None)
                        del self.rows[:4000]

    def live(self, since: int = 0) -> dict:
        with self.lock:
            rows = [r for r in self.rows[-400:] if r["n"] > since][-300:]
            protocols = dict(self.proto.most_common(10))
        return {"n": self.n, "rows": rows, "seen": self.seen, "decoded": self.decoded, "bytes": self.bytes, "protocols": protocols, "error": self.error,
                "sampling": self.seen > self.decoded}

    def view(self, filt: str = "", after: int = 0, limit: int = 400, tail: bool = True) -> dict:
        """The list for the workbench: rows matching the display filter. After a number = what is new; first call (after 0) = the newest rows."""
        pred = display.compile_filter(filt)
        with self.lock:
            snap = list(self.rows)
        shown = [r for r in snap if pred(r)]
        page = [r for r in shown if r["n"] > after]
        if tail and after == 0:
            page = page[-limit:]
        else:
            page = page[:limit]
        return {"rows": page, "total": self.n, "buffered": len(snap), "shown": len(shown), "errors": sum(1 for r in shown if r["errors"]), "sampling": self.seen > self.decoded,
                "seen": self.seen, "decoded": self.decoded, "first_n": snap[0]["n"] if snap else 0}

    def flows(self) -> List[dict]:
        with self.lock:
            return self.an.flow_rows()

    def snapshot_rows(self) -> List[dict]:
        with self.lock:
            return list(self.rows)

    def detail(self, n: int) -> Optional[dict]:
        with self.lock:
            row = next((r for r in self.rows if r["n"] == n), None)
            data = self.data.get(n)
        if not row or data is None:
            return None
        return workbench.packet_detail(row, data)

    def stop(self) -> None:
        self.stop_evt.set()
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
            except OSError:
                pass
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)


# ------------------------------------------------------------------ Wi-Fi
class WifiMonitor:
    """Put a USB Wi-Fi adapter into monitor mode, set/hop channels. Uses `iw` + `ip` (needs CAP_NET_ADMIN)."""

    def __init__(self, iface: str, channel: int = 6, width: str = "HT20", hop: Optional[List[int]] = None, dwell_s: float = 2.0):
        self.iface, self.channel, self.width, self.hop, self.dwell = iface, channel, width, hop or [], dwell_s
        self.tools = find_tools()
        self.stop_evt = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.current: Optional[int] = None
        self.visited: List[int] = []

    def _iw(self, *a: str, check: bool = True) -> str:
        if not self.tools["iw"]:
            raise CaptureError("The 'iw' tool is not installed (sudo apt install iw).")
        r = _run([self.tools["iw"], *a])
        if r.returncode and check:
            raise CaptureError(_plain_error(r.stderr or r.stdout, f"Wi-Fi setup ('iw {' '.join(a)}')"))
        return r.stdout

    def _ip(self, *a: str) -> None:
        r = _run([self.tools["ip"] or "ip", *a])
        if r.returncode:
            raise CaptureError(_plain_error(r.stderr, f"Wi-Fi setup ('ip {' '.join(a)}')"))

    def set_channel(self, ch: int) -> None:
        args = ["dev", self.iface, "set", "channel", str(ch)]
        if self.width and self.width != "HT20" and ch not in (14,):
            args.append(self.width)
        self._iw(*args)
        self.current = ch
        self.visited.append(ch)

    def up(self) -> None:
        self._ip("link", "set", "dev", self.iface, "down")
        self._iw("dev", self.iface, "set", "type", "monitor")
        self._ip("link", "set", "dev", self.iface, "up")
        self.set_channel(self.hop[0] if self.hop else self.channel)
        if len(self.hop) > 1:
            self.thread = threading.Thread(target=self._hop, name="wifi-hop", daemon=True)
            self.thread.start()

    def _hop(self) -> None:
        i = 0
        while not self.stop_evt.wait(self.dwell):
            i = (i + 1) % len(self.hop)
            try:
                self.set_channel(self.hop[i])
            except CaptureError:
                pass

    def down(self) -> None:
        self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=3)
        try:
            self._ip("link", "set", "dev", self.iface, "down")
            self._iw("dev", self.iface, "set", "type", "managed", check=False)
            self._ip("link", "set", "dev", self.iface, "up")
        except CaptureError:
            pass


def list_wifi() -> List[dict]:
    """Wi-Fi interfaces with driver + monitor-mode capability (built-in Pi chip: brcmfmac, cannot monitor)."""
    t = find_tools()
    out: List[dict] = []
    if not t["iw"]:
        return out
    try:
        txt = _run([t["iw"], "dev"]).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    cur = None
    for line in txt.splitlines():
        line = line.strip()
        m = re.match(r"phy#(\d+)", line)
        if m:
            phy = int(m.group(1))
        m = re.match(r"Interface (\S+)", line)
        if m:
            cur = {"name": m.group(1), "phy": phy, "type": "", "driver": "", "monitor_capable": None}
            out.append(cur)
        m = re.match(r"type (\S+)", line)
        if m and cur:
            cur["type"] = m.group(1)
    for w in out:
        try:
            w["driver"] = os.path.basename(os.path.realpath(f"/sys/class/net/{w['name']}/device/driver"))
            info = _run([t["iw"], "phy", f"phy{w['phy']}", "info"]).stdout
            w["monitor_capable"] = bool(re.search(r"\*\s*monitor", info))
        except (OSError, subprocess.SubprocessError):
            pass
        w["built_in"] = w["driver"] in ("brcmfmac", "brcmfmac_wcc")
        if w["built_in"]:
            w["monitor_capable"] = False          # the stock firmware cannot; the Nexmon option (builtin_wifi) can, experimentally
            w["nexmon_candidate"] = True
    return out
