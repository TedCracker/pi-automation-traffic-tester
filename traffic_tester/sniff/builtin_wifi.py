"""App-side client for the built-in Wi-Fi (Nexmon) helper. The web app never touches the radio itself: it writes a small request
file and asks systemd to run the root helper (tt-wifi-helper@<command>.service; polkit allows exactly those units), then reads the
helper's result. TT_WIFI_HELPER_DIRECT=<python3 path> runs the helper directly (tests / containers without systemd)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from typing import List, Optional

def run_dir() -> str:
    return os.environ.get("TT_WIFI_RUN_DIR", "/run/traffic-tester")


def request_path() -> str:
    return os.path.join(run_dir(), "wifi-request.json")


def heartbeat_path() -> str:
    return os.path.join(run_dir(), "wifi-heartbeat")


def result_path() -> str:
    return os.path.join(os.environ.get("TT_WIFI_HELPER_DIR", "/run/tt-wifi"), "result.json")


def helper_py() -> str:
    return os.environ.get("TT_WIFI_HELPER_PY", os.path.join(os.path.dirname(os.path.abspath(__file__)), "wifi_helper.py"))


WARNING = ("Internet over Wi-Fi will drop while the built-in Wi-Fi is sniffing. Use an Ethernet cable for the internet (and for this web page), "
           "or a USB Wi-Fi adapter for sniffing. Normal Wi-Fi comes back when you stop, press Restore, or reboot.")


AP_WARNING = ("The Pi's own Wi-Fi network (its access point) goes off while the built-in Wi-Fi sniffs: phones and laptops connected to it are dropped, and the access point "
              "comes back by itself when the capture stops (or after Restore). Open this page over Ethernet, or use a USB Wi-Fi adapter for sniffing to keep the access point up.")


def was_access_point() -> bool:
    """Was the Pi serving its own Wi-Fi (saved mode 'access point', or the emergency one) before the capture? (root-written state file; False when unknown)"""
    try:
        from ..netcfg import client as netclient
        st = netclient.read_state()
    except Exception:
        return False
    return st.get("wifi_mode") == "ap" or bool((st.get("fallback") or {}).get("active"))


def _bring_back_access_point(res: dict) -> None:
    """After the radio is normal again: if the Pi was an access point, make sure that profile is up (idempotent; the root helper also does it when the app is not there to ask)."""
    if not was_access_point():
        return
    try:
        from ..netcfg import client as netclient
        r = netclient.call("ensure_wifi", {}, wait=60)
        res["ap_restored"] = bool(r.get("ok"))
        if r.get("message"):
            res["ap_message"] = r["message"]
    except Exception as e:                                                         # noqa: BLE001 - never turn a successful radio restore into an error
        res["ap_restored"] = False
        res["ap_message"] = str(e)[:160]


class BuiltinWifiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _read(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _call(cmd: str, timeout: float = 150) -> dict:
    """Run helper command `cmd` (status | monitor | channel | restore | inject-test) and return its result dict."""
    t0 = time.time()
    direct = os.environ.get("TT_WIFI_HELPER_DIRECT")
    if direct:
        r = subprocess.run([direct, helper_py(), cmd], capture_output=True, text=True, timeout=timeout)
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise BuiltinWifiError("helper_failed", f"The Wi-Fi helper did not answer: {(r.stderr or r.stdout).strip()[:200]}")
    if not shutil.which("systemctl"):
        raise BuiltinWifiError("no_systemd", "Built-in Wi-Fi sniffing needs the installed service (systemd). Run install-pi.sh on the Pi.")
    r = subprocess.run(["systemctl", "start", f"tt-wifi-helper@{cmd}.service"], capture_output=True, text=True, timeout=timeout)
    res = _read(result_path())
    if r.returncode and (not res or res.get("time", 0) < t0 - 1):
        err = (r.stderr or "").strip()
        if "authentication" in err.lower() or "access denied" in err.lower() or "not permitted" in err.lower():
            raise BuiltinWifiError("not_permitted", "The service is not allowed to start the Wi-Fi helper. Run the installer again (it installs the permission rule).")
        raise BuiltinWifiError("helper_failed", f"The Wi-Fi helper could not be started: {err[:200]}")
    if not res or res.get("time", 0) < t0 - 1:
        raise BuiltinWifiError("helper_failed", "The Wi-Fi helper produced no result.")
    return res


def _request(iface: str, channel: int, width: str) -> None:
    os.makedirs(run_dir(), exist_ok=True)
    tmp = request_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"iface": iface, "channel": int(channel), "width": width}, f)
    os.replace(tmp, request_path())


def status() -> dict:
    """Helper status merged with a plain-words verdict on whether built-in sniffing is possible here."""
    try:
        res = _call("status", 40)
    except BuiltinWifiError as e:
        return {"available": False, "state": "unknown", "reason": e.message, "code": e.code}
    st = res.get("status", {})
    reasons = []
    if not st.get("nexmon_module"):
        reasons.append(f"The Nexmon driver is not installed for this kernel ({st.get('kernel')}). Run the installer's Wi-Fi step.")
    if not st.get("nexmon_firmware"):
        reasons.append("The Nexmon firmware is not installed.")
    if not st.get("interfaces"):
        reasons.append("No Wi-Fi interface found (is Wi-Fi switched off?).")
    return {"available": not reasons, "reason": " ".join(reasons), "state": st.get("state", "stock"), "kernel": st.get("kernel"), "interfaces": st.get("interfaces", []),
            "monitor_ifaces": st.get("monitor_ifaces", []), "nexmon_firmware_selected": st.get("nexmon_firmware_selected"), "other_kernels": st.get("other_kernels", []),
            "tools": st.get("tools", {}), "warning": WARNING, "ap_warning": AP_WARNING if was_access_point() else ""}


def monitor(iface: str, channel: int, width: str = "HT20") -> str:
    """Switch to Nexmon + monitor mode; returns the monitor interface name. Raises BuiltinWifiError (already restored) on failure."""
    _request(iface, channel, width)
    res = _call("monitor")
    if not res.get("ok"):
        raise BuiltinWifiError(res.get("code", "failed"), res.get("message", "Built-in Wi-Fi monitor mode failed."))
    heartbeat()
    return res["monitor_iface"]


def set_channel(iface: str, channel: int, width: str = "HT20") -> None:
    _request(iface, channel, width)
    res = _call("channel", 30)
    if not res.get("ok"):
        raise BuiltinWifiError(res.get("code", "failed"), res.get("message", "Could not change channel."))


def restore() -> dict:
    res = _call("restore", 150)
    try:
        os.remove(heartbeat_path())
    except OSError:
        pass
    _bring_back_access_point(res)
    return res


def inject_test() -> dict:
    return _call("inject-test", 80)


def heartbeat() -> None:
    os.makedirs(run_dir(), exist_ok=True)
    with open(heartbeat_path(), "w") as f:
        f.write(str(time.time()))


class Heartbeat(threading.Thread):
    """Touches the heartbeat file while a built-in Wi-Fi capture runs; the watchdog restores normal Wi-Fi when it stops."""

    def __init__(self, every: float = 5.0):
        super().__init__(name="wifi-heartbeat", daemon=True)
        self.stop_evt = threading.Event()
        self.every = every

    def run(self) -> None:
        while not self.stop_evt.wait(self.every):
            try:
                heartbeat()
            except OSError:
                pass

    def stop(self) -> None:
        self.stop_evt.set()


class NexmonMonitor:
    """Same interface as capture.WifiMonitor (up/down/set_channel/hop) for the built-in chip."""

    def __init__(self, iface: str = "wlan0", channel: int = 6, width: str = "HT20", hop: Optional[List[int]] = None, dwell_s: float = 2.0):
        self.iface = iface if iface and iface not in ("builtin", "auto") else "wlan0"
        self.channel, self.width, self.hop, self.dwell = channel, width, hop or [], dwell_s
        self.monitor_iface: Optional[str] = None
        self.current: Optional[int] = None
        self.visited: List[int] = []
        self.stop_evt = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.beat = Heartbeat()

    def up(self) -> None:
        first = self.hop[0] if self.hop else self.channel
        self.monitor_iface = monitor(self.iface, first, self.width)
        self.current = first
        self.visited.append(first)
        self.beat.start()
        if len(self.hop) > 1:
            self.thread = threading.Thread(target=self._hop, name="wifi-hop-builtin", daemon=True)
            self.thread.start()

    def _hop(self) -> None:
        i = 0
        while not self.stop_evt.wait(self.dwell):
            i = (i + 1) % len(self.hop)
            try:
                set_channel(self.iface, self.hop[i], self.width)
                self.current = self.hop[i]
                self.visited.append(self.hop[i])
            except BuiltinWifiError:
                pass

    def down(self) -> None:
        self.stop_evt.set()
        self.beat.stop()
        if self.thread:
            self.thread.join(timeout=5)
        try:
            restore()
        except BuiltinWifiError:
            pass
