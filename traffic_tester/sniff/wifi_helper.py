#!/usr/bin/env python3
"""Privileged helper for the Pi's built-in Wi-Fi (Broadcom/Cypress CYW43455) monitor mode with Nexmon.

Runs as ROOT, started by systemd (tt-wifi-helper@<command>.service) so the web app itself never needs root.
Standard library only (it runs with the system python3). Every input comes from a small JSON request file
written by the app and is validated strictly here.

Commands:  status | monitor | restore [--if-needed] | watchdog | inject-test

Design (docs/SPEC.md 14.x):
  * the stock brcmfmac module and stock firmware are NEVER replaced on disk. The Nexmon module lives in
    /opt/traffic-tester/nexmon/<kernel>/brcmfmac.ko and is loaded on demand with insmod; the Nexmon firmware is selected with
    update-alternatives (cyfmac43455-sdio.bin) only while monitoring. "restore" undoes both -> stock Wi-Fi again.
  * monitor mode kills the Wi-Fi link (it is also the Pi's internet link): restore runs automatically when the app
    stops sending its heartbeat (watchdog), on any failure, and at boot.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

ROOT = os.environ.get("TT_WIFI_ROOT", "")                       # test sandbox prefix; empty on a real Pi
RUN_DIR = ROOT + "/run/traffic-tester"                       # owned by the service user: the helper only READS the request + heartbeat from here
HELPER_DIR = ROOT + "/run/tt-wifi"                           # owned by root: state, results, backups (a service-user symlink can never redirect a root write)
REQUEST = RUN_DIR + "/wifi-request.json"
HEARTBEAT = RUN_DIR + "/wifi-heartbeat"
STATE = HELPER_DIR + "/state.json"
RESULT = HELPER_DIR + "/result.json"
FW_BACKUP = HELPER_DIR + "/fw-backup.json"
NEXMON_DIR = ROOT + "/opt/traffic-tester/nexmon"
FW_DIR = ROOT + "/lib/firmware/cypress"
FW_NAME = "cyfmac43455-sdio.bin"
FW_NEXMON = "cyfmac43455-sdio-nexmon.bin"
FW_STOCK = "cyfmac43455-sdio-standard.bin"
ALT_NAME = "cyfmac43455-sdio.bin"
STOCK_MODULES = ["brcmfmac_wcc", "brcmfmac_cyw", "brcmfmac_bca", "brcmfmac"]      # unload order (vendor modules first)
HEARTBEAT_TTL = int(os.environ.get("TT_WIFI_HEARTBEAT_TTL", "45"))
SWITCH_TIMEOUT = int(os.environ.get("TT_WIFI_SWITCH_TIMEOUT", "120"))
IFACE_RE = re.compile(r"^wlan[0-9]{1,2}$")
CHAN_RE = re.compile(r"^[0-9]{1,3}$")
WIDTHS = ("HT20", "HT40+", "HT40-", "80MHz")
FW_ERR_RE = re.compile(r"(firmware[^\n]*(crash|fail|timeout|error|trap)|brcmf_[a-z_]+:[^\n]*(-110|-22|-5)\b|dongle trap|TRAP\b|brcmf_sdio[^\n]*(error|timeout)|brcmf_c_preinit_dcmds[^\n]*fail)", re.I)


class HelperError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def log(msg: str) -> None:
    print(f"[tt-wifi] {msg}", file=sys.stderr, flush=True)


def run(cmd: List[str], timeout: float = 30, check: bool = False) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        r = subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        r = subprocess.CompletedProcess(cmd, 124, "", "timed out")
    if check and r.returncode:
        raise HelperError("command_failed", f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()[:200]}")
    return r


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def now() -> float:
    return time.time()


# ------------------------------------------------------------------ state files
def write_json(path: str, data: dict, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path: str, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def get_state() -> dict:
    return read_json(STATE, {"state": "stock"})


def set_state(**kw) -> dict:
    s = {**get_state(), **kw, "updated": now()}
    write_json(STATE, s)
    return s


def finish(ok: bool, code: str = "", message: str = "", **extra) -> dict:
    res = {"ok": ok, "code": code, "message": message, "time": now(), **extra}
    write_json(RESULT, res)
    return res


# ------------------------------------------------------------------ request validation
def load_request() -> dict:
    r = read_json(REQUEST, None)
    if not isinstance(r, dict):
        raise HelperError("bad_request", "No valid request file.")
    iface = str(r.get("iface", "wlan0"))
    if not IFACE_RE.match(iface):
        raise HelperError("bad_request", f"Interface name '{iface}' is not allowed.")
    chan = str(r.get("channel", "6"))
    if not CHAN_RE.match(chan) or not 1 <= int(chan) <= 233:
        raise HelperError("bad_request", f"Channel '{chan}' is not allowed.")
    width = str(r.get("width", "HT20"))
    if width not in WIDTHS:
        raise HelperError("bad_request", f"Width '{width}' is not allowed.")
    return {"iface": iface, "channel": int(chan), "width": width}


# ------------------------------------------------------------------ inspection
def kernel_release() -> str:
    return os.environ.get("TT_WIFI_KVER") or os.uname().release


def nexmon_module(kver: Optional[str] = None) -> str:
    return os.path.join(NEXMON_DIR, kver or kernel_release(), "brcmfmac.ko")


def nexmon_firmware() -> str:
    return os.path.join(FW_DIR, FW_NEXMON)


def current_alternative() -> Optional[str]:
    """Which file cyfmac43455-sdio.bin currently resolves to (None if unknown)."""
    link = os.path.join(FW_DIR, FW_NAME)
    try:
        return os.path.realpath(link)
    except OSError:
        return None


def alt_group_exists() -> bool:
    if not have("update-alternatives"):
        return False
    r = run(["update-alternatives", "--query", ALT_NAME])
    return r.returncode == 0


def wifi_ifaces() -> List[str]:
    out = []
    r = run(["iw", "dev"])
    for m in re.finditer(r"Interface\s+(\S+)", r.stdout):
        out.append(m.group(1))
    return out


def iface_types() -> Dict[str, str]:
    r = run(["iw", "dev"])
    res, cur = {}, None
    for line in r.stdout.splitlines():
        m = re.match(r"\s*Interface\s+(\S+)", line)
        if m:
            cur = m.group(1)
        m = re.match(r"\s*type\s+(\S+)", line)
        if m and cur:
            res[cur] = m.group(1)
    return res


def dmesg_errors(since_marker: Optional[str] = None) -> List[str]:
    r = run(["dmesg"], timeout=10)
    lines = [l for l in r.stdout.splitlines() if "brcmf" in l.lower() or "firmware" in l.lower()]
    if since_marker and since_marker in r.stdout:
        lines = [l for l in r.stdout.split(since_marker, 1)[1].splitlines() if "brcmf" in l.lower() or "firmware" in l.lower()]
    return [l for l in lines if FW_ERR_RE.search(l)][-5:]


def stock_nexmon_state() -> dict:
    ifs = wifi_ifaces()
    types = iface_types() if ifs else {}
    alt = current_alternative()
    nexmon_alt = bool(alt and alt.endswith(FW_NEXMON))
    return {"interfaces": ifs, "types": types, "alternative": alt, "nexmon_firmware_selected": nexmon_alt,
            "monitor_ifaces": [i for i, t in types.items() if t == "monitor"]}


# ------------------------------------------------------------------ status
def cmd_status() -> dict:
    kver = kernel_release()
    mod = nexmon_module()
    st = get_state()
    info = {"state": st.get("state", "stock"), "since": st.get("since"), "kernel": kver, "nexmon_module": os.path.exists(mod), "nexmon_module_path": mod,
            "nexmon_firmware": os.path.exists(nexmon_firmware()), "alt_group": alt_group_exists(), "other_kernels": sorted(
                d for d in (os.listdir(NEXMON_DIR) if os.path.isdir(NEXMON_DIR) else []) if os.path.exists(os.path.join(NEXMON_DIR, d, "brcmfmac.ko"))),
            **stock_nexmon_state(), "tools": {t: have(t) for t in ("iw", "ip", "nmcli", "modprobe", "insmod", "rmmod", "update-alternatives", "airmon-ng", "aireplay-ng", "dmesg")}}
    info["ready"] = info["nexmon_module"] and info["nexmon_firmware"] and have("iw") and have("insmod")
    return finish(True, "status", "ok", status=info)


# ------------------------------------------------------------------ restore
def _select_stock_firmware() -> None:
    if alt_group_exists():
        run(["update-alternatives", "--auto", ALT_NAME])
        return
    link = os.path.join(FW_DIR, FW_NAME)
    backup = read_json(FW_BACKUP, {}).get("target")
    target = backup or FW_STOCK
    try:
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        os.symlink(target, link)
    except OSError as e:
        log(f"cannot restore firmware link: {e}")


def _select_nexmon_firmware() -> None:
    link = os.path.join(FW_DIR, FW_NAME)
    if alt_group_exists():
        run(["update-alternatives", "--set", ALT_NAME, nexmon_firmware()], check=True)
        return
    try:
        target = os.readlink(link) if os.path.islink(link) else FW_STOCK
        write_json(FW_BACKUP, {"target": target})
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        os.symlink(FW_NEXMON, link)
    except OSError as e:
        raise HelperError("firmware_switch_failed", f"Cannot select the Nexmon firmware: {e}")


def _unload_wifi_modules() -> None:
    for m in STOCK_MODULES:
        run(["modprobe", "-r", m])
        run(["rmmod", m])                                                         # nexmon module (loaded with insmod) is not known to modprobe's dependency graph


def _wait_iface(name: str, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if name in wifi_ifaces():
            return True
        time.sleep(0.5)
    return False


NET_STATE_DIR = os.environ.get("TT_NET_STATE_DIR") or (ROOT + "/var/lib/traffic-tester-update")      # written by netops.py (root): what the Wi-Fi was before the capture


def _wifi_profile_to_bring_back() -> Optional[str]:
    """The Pi's own access point (tt-ap, or the emergency tt-fallback-ap) is not one of NetworkManager's 'reconnect' candidates for `device connect`: name it explicitly."""
    st = read_json(NET_STATE_DIR + "/net-state.json", {}) or {}
    if (st.get("fallback") or {}).get("active"):
        return "tt-fallback-ap"
    return "tt-ap" if st.get("wifi_mode") == "ap" else None


def restore(reason: str = "requested", base: Optional[str] = None) -> dict:
    """Idempotent: monitor interface removed, stock module + stock firmware loaded, NetworkManager and the Wi-Fi link brought back."""
    st = get_state()
    base = base or st.get("base") or "wlan0"
    notes: List[str] = []
    try:
        for i, t in iface_types().items():
            if t == "monitor":
                run(["iw", "dev", i, "del"])
                notes.append(f"removed monitor interface {i}")
        run(["ip", "link", "set", base, "down"])
        _unload_wifi_modules()
        _select_stock_firmware()
        notes.append("stock firmware selected")
        r = run(["modprobe", "brcmfmac"], timeout=60)
        if r.returncode:
            notes.append(f"modprobe brcmfmac: {(r.stderr or r.stdout).strip()[:120]}")
        ok_if = _wait_iface(base, 15)
        if have("nmcli"):
            run(["nmcli", "radio", "wifi", "on"])
            run(["nmcli", "device", "set", base, "managed", "yes"])
            run(["nmcli", "device", "connect", base], timeout=45)
            notes.append("NetworkManager told to manage and reconnect Wi-Fi")
            ap = _wifi_profile_to_bring_back()
            if ap and ok_if:                                                          # the Pi was its own access point before the capture: bring it back
                r_ap = run(["nmcli", "--wait", "30", "connection", "up", "id", ap, "ifname", base], timeout=60)
                notes.append(f"access point {ap} brought back" if r_ap.returncode == 0 else f"access point {ap} did not come back: {(r_ap.stderr or r_ap.stdout).strip()[:100]}")
        run(["ip", "link", "set", base, "up"])
        set_state(state="stock", base=base, restored_reason=reason, monitor_iface=None)
        msg = "Normal Wi-Fi restored." if ok_if else "Stock driver loaded, but the Wi-Fi interface did not come back yet. Wait 20 s, or reboot (a reboot always restores normal Wi-Fi)."
        return finish(ok_if, "restored" if ok_if else "restore_incomplete", msg, notes=notes, reason=reason)
    except Exception as e:  # noqa
        set_state(state="failed")
        return finish(False, "restore_failed", f"Restore hit a problem ({e}). A reboot restores normal Wi-Fi.", notes=notes)


def cmd_restore(if_needed: bool = False) -> dict:
    if if_needed:
        s = stock_nexmon_state()
        st = get_state()
        if st.get("state") in (None, "stock") and not s["nexmon_firmware_selected"] and not s["monitor_ifaces"]:
            return finish(True, "nothing_to_do", "Wi-Fi is already normal.")
    return restore("boot" if if_needed else "requested")


# ------------------------------------------------------------------ monitor
def cmd_monitor() -> dict:
    req = load_request()
    base, chan, width = req["iface"], req["channel"], req["width"]
    kver = kernel_release()
    mod = nexmon_module(kver)
    if not os.path.exists(mod):
        have_other = sorted(d for d in (os.listdir(NEXMON_DIR) if os.path.isdir(NEXMON_DIR) else []))
        raise HelperError("nexmon_missing", f"No Nexmon driver for this kernel ({kver}). " + (f"Built for: {', '.join(have_other)}. " if have_other else "") +
                          "Run the offline installer again (it rebuilds the driver) or use a USB Wi-Fi adapter.")
    if not os.path.exists(nexmon_firmware()):
        raise HelperError("nexmon_missing", "The Nexmon firmware file is not installed. Run the installer's Wi-Fi step again.")
    for t in ("iw", "insmod"):
        if not have(t):
            raise HelperError("tool_missing", f"'{t}' is not installed.")
    if base not in wifi_ifaces():
        raise HelperError("no_interface", f"Wi-Fi interface {base} not found. Is the Wi-Fi radio switched off (rfkill) or the module unloaded?")
    marker = f"tt-wifi-marker-{int(now())}"
    run(["sh", "-c", f"echo {marker} > /dev/kmsg"])
    set_state(state="switching", base=base, since=now(), channel=chan)
    try:
        if have("nmcli"):
            run(["nmcli", "device", "set", base, "managed", "no"])
        run(["ip", "link", "set", base, "down"])
        _select_nexmon_firmware()
        _unload_wifi_modules()
        for dep in ("cfg80211", "brcmutil"):
            run(["modprobe", dep])
        r = run(["insmod", mod], timeout=60)
        if r.returncode:
            raise HelperError("module_load_failed", f"The Nexmon driver would not load: {(r.stderr or r.stdout).strip()[:160]}")
        if not _wait_iface(base, 15):
            errs = dmesg_errors(marker)
            raise HelperError("firmware_crash" if errs else "interface_missing",
                              "The Wi-Fi chip did not come back with the Nexmon firmware." + (f" Kernel log: {errs[-1][:160]}" if errs else " (The firmware may have crashed; this chip is known to be flaky.)"))
        errs = dmesg_errors(marker)
        if errs:
            raise HelperError("firmware_crash", f"The Wi-Fi firmware reported a problem: {errs[-1][:180]}")
        mon = f"{base}mon"
        run(["ip", "link", "set", base, "up"])
        r = run(["iw", "dev", base, "interface", "add", mon, "type", "monitor", "flags", "none"])
        if r.returncode and have("airmon-ng"):                                        # fallback: airmon-ng (its "-524" message is expected)
            run(["airmon-ng", "start", base], timeout=45)
        if mon not in iface_types() and f"{base}mon" not in wifi_ifaces():
            raise HelperError("monitor_failed", f"Monitor mode did not start: {(r.stderr or r.stdout).strip()[:160] or 'no monitor interface appeared'}")
        run(["ip", "link", "set", mon, "up"])
        if iface_types().get(mon) != "monitor":
            raise HelperError("monitor_failed", f"{mon} exists but is not in monitor mode (check with: iw dev).")
        args = ["iw", "dev", mon, "set", "channel", str(chan)] + ([width] if width != "HT20" else [])
        r = run(args)
        if r.returncode:
            raise HelperError("channel_failed", f"Could not set channel {chan}: {(r.stderr or r.stdout).strip()[:120]}")
        set_state(state="monitor", monitor_iface=mon, base=base, since=now(), channel=chan)
        return finish(True, "monitor", f"{mon} is in monitor mode on channel {chan}. Normal Wi-Fi is OFF until you press Restore (or stop the capture).", monitor_iface=mon, channel=chan)
    except HelperError as e:
        log(f"monitor failed: {e.code}: {e.message}")
        res = restore(f"monitor failed ({e.code})", base)
        return finish(False, e.code, e.message + (" Normal Wi-Fi was restored." if res.get("ok") else " Automatic restore did not finish: reboot the Pi to restore Wi-Fi."), restored=res.get("ok"))
    except Exception as e:  # noqa
        res = restore(f"unexpected error {type(e).__name__}", base)
        return finish(False, "unexpected", f"Unexpected error: {e}. Normal Wi-Fi restored: {res.get('ok')}.")


def cmd_set_channel() -> dict:
    req = load_request()
    st = get_state()
    mon = st.get("monitor_iface")
    if st.get("state") != "monitor" or not mon:
        raise HelperError("not_monitoring", "Built-in Wi-Fi is not in monitor mode.")
    args = ["iw", "dev", mon, "set", "channel", str(req["channel"])] + ([req["width"]] if req["width"] != "HT20" else [])
    r = run(args)
    if r.returncode:
        raise HelperError("channel_failed", f"Could not set channel {req['channel']}: {(r.stderr or r.stdout).strip()[:120]}")
    set_state(channel=req["channel"])
    return finish(True, "channel", f"Channel {req['channel']}", channel=req["channel"])


def cmd_watchdog() -> dict:
    st = get_state()
    s = st.get("state", "stock")
    if s == "monitor":
        try:
            last = os.path.getmtime(HEARTBEAT)
        except OSError:
            last = 0.0
        age = now() - max(last, float(st.get("since") or 0))            # a fresh monitor session gets its grace period before the first heartbeat
        if age > HEARTBEAT_TTL:
            log(f"no heartbeat for {int(min(age, 9999))} s: restoring normal Wi-Fi")
            return restore("watchdog: the app stopped (no heartbeat)")
    elif s in ("switching", "failed"):
        if now() - st.get("updated", 0) > SWITCH_TIMEOUT or s == "failed":
            return restore(f"watchdog: state '{s}'")
    return finish(True, "watchdog_ok", f"state {s}")


def cmd_inject_test() -> dict:
    """EXPERIMENTAL: aireplay-ng --test on the monitor interface."""
    st = get_state()
    mon = st.get("monitor_iface")
    if st.get("state") != "monitor" or not mon:
        raise HelperError("not_monitoring", "Start a built-in Wi-Fi capture first.")
    if not have("aireplay-ng"):
        raise HelperError("tool_missing", "aireplay-ng (package aircrack-ng) is not installed.")
    r = run(["aireplay-ng", "--test", mon], timeout=40)
    out = (r.stdout + r.stderr)
    ok = "Injection is working" in out
    return finish(ok, "inject_ok" if ok else "inject_failed", "Injection works (experimental)." if ok else "Injection did not work on this firmware (experimental feature).", output=out[-400:])


COMMANDS = {"status": cmd_status, "monitor": cmd_monitor, "restore": lambda: cmd_restore(False), "restore-if-needed": lambda: cmd_restore(True),
            "watchdog": cmd_watchdog, "inject-test": cmd_inject_test, "channel": cmd_set_channel}


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] not in COMMANDS and not (argv[1] == "restore" and "--if-needed" in argv[2:]):
        print("usage: wifi_helper.py status|monitor|channel|restore [--if-needed]|watchdog|inject-test", file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "restore" and "--if-needed" in argv[2:]:
        cmd = "restore-if-needed"
    try:
        res = COMMANDS[cmd]()
    except HelperError as e:
        res = finish(False, e.code, e.message)
    except Exception as e:  # noqa
        res = finish(False, "unexpected", f"{type(e).__name__}: {e}")
    print(json.dumps(res))
    return 0 if res.get("ok") or cmd in ("watchdog", "status") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
