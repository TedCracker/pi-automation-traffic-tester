"""One sniffer run: starts the right capture for the mode, supervises it, emits live events, writes the summary (docs/SPEC.md 14)."""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from typing import Callable, Dict, List, Optional

from .. import system
from . import analyze, bpf as bpfmod, builtin_wifi, capture as cap, display, preflight as pre, workbench
from .serialtap import SerialTap


class SniffError(Exception):
    pass


def _rx_counters(ifs: List[str]) -> tuple:
    p = b = 0
    for i in ifs:
        try:
            p += int(open(f"/sys/class/net/{i}/statistics/rx_packets").read())
            b += int(open(f"/sys/class/net/{i}/statistics/rx_bytes").read())
        except (OSError, ValueError):
            pass
    return p, b


class SnifferRun:
    def __init__(self, profile: dict, out_dir: str, emit: Callable[[dict], None] = lambda e: None, usb_drives: Callable[[], List[dict]] = system.usb_drives):
        self.p, self.cfg = profile, profile["sniff"]
        self.out_dir, self.emit, self.usb_drives = out_dir, emit, usb_drives
        self.stop_evt = threading.Event()
        self.error: Optional[str] = None
        self.status = "created"
        self.tap: Optional[SerialTap] = None
        self.capture: Optional[cap.Capture] = None
        self.feeder: Optional[cap.LiveFeeder] = None
        self.bridge: Optional[cap.EthBridge] = None
        self.wifi: Optional[cap.WifiMonitor] = None
        self.stop_reason = "user"
        self.location = out_dir
        self.t0 = time.monotonic()
        self.ifaces: List[str] = []

    def stop(self) -> None:
        self.stop_evt.set()

    # ------------------------------------------------------------ live API
    def live(self, since: int = 0) -> dict:
        if self.tap:
            d = self.tap.live(since)
            return {**d, "mode": "serial"}
        if self.feeder:
            return {**self.feeder.live(since), "mode": self.cfg["mode"]}
        return {"n": 0, "rows": [], "mode": self.cfg["mode"]}

    def view(self, filt: str = "", after: int = 0, limit: int = 400) -> dict:
        """Rows for the workbench list, through the display filter."""
        if self.tap:
            pred = display.compile_filter(filt)
            with self.tap.lock:
                snap = list(self.tap.rows)
                total = self.tap.n
            shown = [r for r in snap if pred(r)]
            page = [r for r in shown if r["n"] > after]
            page = page[-limit:] if after == 0 else page[:limit]
            return {"rows": [{k: v for k, v in r.items() if k not in ("fields", "hex")} for r in page], "total": total, "buffered": len(snap), "shown": len(shown),
                    "errors": sum(1 for r in shown if r.get("errors")), "sampling": False, "mode": "serial", "first_n": snap[0]["n"] if snap else 0}
        if self.feeder:
            return {**self.feeder.view(filt, after, limit), "mode": self.cfg["mode"]}
        return {"rows": [], "total": 0, "buffered": 0, "shown": 0, "errors": 0, "mode": self.cfg["mode"]}

    def flows(self) -> List[dict]:
        if self.tap:
            with self.tap.lock:
                return workbench.serial_flows(list(self.tap.rows))
        return self.feeder.flows() if self.feeder else []

    def all_rows(self) -> List[dict]:
        if self.tap:
            with self.tap.lock:
                return list(self.tap.rows)
        return self.feeder.snapshot_rows() if self.feeder else []

    def capture_paths(self) -> List[str]:
        """The pcapng files written so far (the file being written is readable: a half-written last block is skipped)."""
        if self.tap:
            try:
                self.tap.pcap.flush()                         # the serial tap flushes once a second at most: make the newest frames readable now
            except Exception:  # noqa
                pass
            return [os.path.join(self.location, f) for f in sorted(os.listdir(self.location)) if f.endswith((".pcapng", ".pcap")) and "tap-rtu" not in f and not f.startswith("shown-")]
        if self.capture:
            try:
                return [f for f in self.capture.files() if f.endswith((".pcapng", ".pcap"))]
            except Exception:  # noqa
                return []
        return []

    def detail(self, n: int) -> Optional[dict]:
        if self.tap:
            r = next((r for r in self.tap.rows if r["n"] == n), None)
            return None if r is None else workbench.serial_detail(r)
        return self.feeder.detail(n) if self.feeder else None

    # ------------------------------------------------------------ storage
    def _choose_dir(self) -> None:
        st = self.cfg["storage"]
        if st["usb"]:
            drives = self.usb_drives()
            if not drives:
                raise SniffError("Save to USB drive is on, but no USB drive is plugged in.")
            d = drives[0]["path"]
            self.location = os.path.join(d, "traffic-tester-captures", os.path.basename(self.out_dir))
        os.makedirs(self.location, exist_ok=True)
        free = shutil.disk_usage(self.location).free / 1e6
        if free < st["min_free_mb"]:
            raise SniffError(f"Not enough free space on the storage ({free:.0f} MB free, {st['min_free_mb']} MB required).")

    # ------------------------------------------------------------ run
    def run(self) -> dict:
        c = self.cfg
        summary: dict = {}
        try:
            self._choose_dir()
            bpf = (c["filter"]["bpf"] or "").strip()
            if bpf and c["mode"] != "serial":
                err = bpfmod.validate(bpf, "IEEE802_11_RADIO" if c["mode"] == "wifi" else "EN10MB")
                if err:
                    raise SniffError(err)
            ring = {"ring_mb": c["storage"]["ring_mb"], "ring_minutes": c["storage"]["ring_minutes"], "ring_files": c["storage"]["ring_files"]}
            if c["mode"] == "serial":
                self._start_serial(ring)
            elif c["mode"] == "eth_bridge":
                self._start_bridge(ring, bpf)
            elif c["mode"] == "eth_span":
                self._start_span(ring, bpf)
            elif c["mode"] == "wifi":
                self._start_wifi(ring, bpf)
            else:
                raise SniffError(f"Unknown mode {c['mode']}")
            self.status = "running"
            self.emit({"ev": "started", "total_s": c["duration_s"] or 0, "transport": "sniffer", "mode": c["mode"], "location": self.location})
            self._monitor()
            self.status = "stopped" if self.stop_reason == "user" else "finished"
        except (SniffError, cap.CaptureError, cap.BridgeError, builtin_wifi.BuiltinWifiError) as e:
            self.error, self.status = str(e), "failed"
        except Exception as e:  # noqa
            self.error, self.status = f"{type(e).__name__}: {e}", "failed"
        finally:
            summary = self._finish()
        if self.error:
            summary["error"] = self.error
        self.emit({"ev": "done", "status": self.status, "error": self.error, "summary": None})
        return summary

    def _start_serial(self, ring: dict) -> None:
        s = self.cfg["serial"]
        chans = [ch for ch in s["channels"] if ch["port"]]
        if not chans:
            raise SniffError("Choose the serial port(s) to listen on.")
        if len({ch["port"] for ch in chans}) != len(chans):
            raise SniffError("Each direction needs its own serial adapter: two channels cannot use the same port.")
        self.tap = SerialTap({**s, "channels": chans}, self.location, self.emit, ring)
        try:
            self.tap.start()
        except Exception as e:
            raise SniffError(f"Cannot open the serial port: {e}")

    def _start_bridge(self, ring: dict, bpf: str) -> None:
        e = self.cfg["eth"]
        a, b = pre.bridge_legs(self.p)                      # the same resolver as pre-flight and the server's cut-off guard
        if not a or not b or a == b:
            raise SniffError("The inline tap needs two different network ports (for example the built-in port and a USB Ethernet adapter).")
        self.bridge = cap.EthBridge([a, b], "ttbr0", e["link_passthrough"])
        self.bridge.up()
        self.ifaces = [self.bridge.name] if e["capture_on"] == "bridge" else [a, b]
        self._start_capture(ring, bpf, rx_ifs=[a, b])

    def _start_span(self, ring: dict, bpf: str) -> None:
        iface = pre.span_iface(self.p)                      # the same resolver as pre-flight
        if not iface:
            raise SniffError("No wired network port found. Pick one in the settings.")
        self.ifaces = [iface]
        self._start_capture(ring, bpf, rx_ifs=[iface])

    def _start_wifi(self, ring: dict, bpf: str) -> None:
        w = self.cfg["wifi"]
        iface = w["interface"]
        if iface == "builtin":
            if not w["builtin_ack"]:
                raise SniffError("Built-in Wi-Fi sniffing turns normal Wi-Fi off. " + builtin_wifi.WARNING + " Tick the box to confirm, or choose a USB adapter.")
            self.wifi = builtin_wifi.NexmonMonitor("wlan0", w["channel"], w["width"], w["channels"] if w["hop"] else [], w["dwell_s"])
            self.wifi.up()                                         # raises BuiltinWifiError with a plain message (and has already restored Wi-Fi)
            self.ifaces = [self.wifi.monitor_iface]
            self._start_capture(ring, bpf, rx_ifs=[self.wifi.monitor_iface])
            return
        if iface == "auto":
            cands = [x for x in cap.list_wifi() if x.get("monitor_capable")]
            if not cands:
                raise SniffError("No Wi-Fi adapter that supports monitor mode was found. Plug in a USB adapter (for example Alfa AWUS036ACM, the reliable choice), "
                                 "or choose 'Built-in Wi-Fi (Nexmon, experimental)' - that switches normal Wi-Fi off while sniffing.")
            iface = cands[0]["name"]
        self.wifi = cap.WifiMonitor(iface, w["channel"], w["width"], w["channels"] if w["hop"] else [], w["dwell_s"])
        self.wifi.up()
        self.ifaces = [iface]
        self._start_capture(ring, bpf, rx_ifs=[iface])

    def _start_capture(self, ring: dict, bpf: str, rx_ifs: List[str]) -> None:
        c = self.cfg
        self.rx_ifs = rx_ifs
        st = c["storage"]
        free_mb = shutil.disk_usage(self.location).free / 1e6
        usable_mb = max(0.0, free_mb - st["min_free_mb"])
        max_kb = 0
        if st["ring_mb"] and st["ring_files"]:                           # ring buffer: total size is bounded, it must fit
            if st["ring_mb"] * st["ring_files"] > usable_mb:
                raise SniffError(f"The ring buffer ({st['ring_mb']} MB x {st['ring_files']} files) is bigger than the free space ({free_mb:.0f} MB, {st['min_free_mb']} MB kept free). Lower the size or the number of files.")
        else:                                                              # one growing file: stop it exactly before the guard is crossed
            max_kb = int(usable_mb * 0.9 * 1000)
        self.capture = cap.Capture(self.ifaces, self.location, bpf, ring, c["snaplen"], c["duration_s"], "capture", max_kb)
        self.max_kb = max_kb
        self.capture.start()
        self.feeder = cap.LiveFeeder(self.ifaces, bpf)
        self.feeder.start_proc()
        self._rx0 = _rx_counters(rx_ifs)

    # ------------------------------------------------------------ supervise
    def _monitor(self) -> None:
        c = self.cfg
        last_n = 0
        last = (time.monotonic(), 0, 0)
        while not self.stop_evt.is_set():
            time.sleep(0.25)
            now = time.monotonic()
            el = now - self.t0
            if c["duration_s"] and el >= c["duration_s"]:
                self.stop_reason = "duration"
                break
            free = shutil.disk_usage(self.location).free / 1e6
            if free < c["storage"]["min_free_mb"]:
                self.stop_reason = "disk_full"
                self.error = f"Capture stopped: only {free:.0f} MB left on the storage (guard {c['storage']['min_free_mb']} MB). Files so far are kept."
                break
            ev: Dict = {"ev": "sstate", "elapsed_s": round(el, 1), "total_s": c["duration_s"] or 0, "mode": c["mode"], "free_mb": round(free)}
            if self.tap:
                lv = self.tap.live(last_n)
                if lv["error"]:
                    self.error = lv["error"]
                    break
                tot_b = sum(lv["bytes"])
                dt_ = max(1e-3, now - last[0])
                ev.update(packets=lv["n"], bytes=tot_b, pps=round((lv["n"] - last[1]) / dt_, 1), mbps=round((tot_b - last[2]) * 8 / dt_ / 1e6, 4), crc_fail=lv["crc_fail"],
                          frames_per_channel=lv["frames"], bytes_per_channel=lv["bytes"])
                rows = lv["rows"][-60:]
                if rows:
                    last_n = rows[-1]["n"]
                    self.emit({"ev": "sserial", "rows": rows})
                last = (now, lv["n"], tot_b)
            else:
                if not self.capture.alive():
                    if self.capture.proc.returncode == 0 and self.max_kb and not (c["duration_s"] and el >= c["duration_s"] - 1):
                        self.stop_reason = "disk_full"
                        self.error = f"Capture stopped: the storage is nearly full ({self.max_kb // 1000} MB of space was available above the {c['storage']['min_free_mb']} MB guard). Files so far are kept."
                    elif self.capture.proc.returncode == 0 and c["duration_s"]:
                        self.stop_reason = "duration"
                    else:
                        self.error = cap._plain_error("\n".join(self.capture.stderr_lines), "The capture stopped unexpectedly")
                    break
                rx_p, rx_b = _rx_counters(self.rx_ifs)
                p, b = rx_p - self._rx0[0], rx_b - self._rx0[1]
                dt_ = max(1e-3, now - last[0])
                lv = self.feeder.live(last_n)
                ev.update(packets=p, bytes=b, pps=round((p - last[1]) / dt_), mbps=round((b - last[2]) * 8 / dt_ / 1e6, 3), file_bytes=self.capture.bytes_written(),
                          viewer={"seen": lv["seen"], "decoded": lv["decoded"], "sampling": lv["sampling"]}, protocols=lv["protocols"])
                rows = lv["rows"][-60:]
                if rows:
                    last_n = rows[-1]["n"]
                    self.emit({"ev": "spkt", "rows": rows})
                last = (now, p, b)
                if self.wifi:
                    ev["channel"] = self.wifi.current
                if self.bridge and self.bridge.events:
                    ev["bridge_events"] = self.bridge.events[-3:]
            self.emit(ev)

    # ------------------------------------------------------------ finish
    def _finish(self) -> dict:
        info: dict = {"kind": "sniffer", "mode": self.cfg["mode"], "stop_reason": self.stop_reason, "location": self.location, "elapsed_s": round(time.monotonic() - self.t0, 1)}
        files: List[str] = []

        def failed(e: Exception) -> None:                    # every resource gets its own attempt: one failure must not skip the others
            info.setdefault("cleanup_error", f"{type(e).__name__}: {e}")
            if not self.error:
                self.error = "Clean-up problem: " + info["cleanup_error"]
        if self.capture:
            try:
                info["engine"] = self.capture.stop()
            except Exception as e:  # noqa
                failed(e)
            try:
                files = self.capture.files()
            except Exception as e:  # noqa
                failed(e)
        if self.feeder:
            try:
                self.feeder.stop()
            except Exception as e:  # noqa
                failed(e)
        if self.tap:
            try:
                info["serial"] = self.tap.stop()
                files = [os.path.join(self.location, f) for f in info["serial"]["files"] if os.path.exists(os.path.join(self.location, f))]
            except Exception as e:  # noqa
                failed(e)
        if self.bridge:
            info["bridge_events"] = self.bridge.events
            info["bridge_legs"] = self.bridge.legs
            try:
                self.bridge.down()
            except Exception as e:  # noqa
                failed(e)
        if self.wifi:
            try:
                info["wifi"] = {"interface": getattr(self.wifi, "monitor_iface", None) or self.wifi.iface, "channels_visited": sorted(set(self.wifi.visited)), "hop": bool(self.wifi.hop),
                                "builtin_nexmon": isinstance(self.wifi, builtin_wifi.NexmonMonitor)}
            except Exception as e:  # noqa
                failed(e)
            try:
                self.wifi.down()
            except Exception as e:  # noqa
                failed(e)
        pc = [f for f in files if re.search(r"\.(pcapng|pcap\d*)$", f) and "tap-rtu" not in os.path.basename(f)]
        info["files"] = [{"name": os.path.basename(f), "bytes": os.path.getsize(f)} for f in files if os.path.exists(f)]
        try:
            info["analysis"] = analyze.summarize_files(pc)
        except Exception as e:  # noqa
            info["analysis"] = {"packets": 0, "error": f"{type(e).__name__}: {e}"}
        if self.cfg["storage"]["ring_files"] and info.get("engine"):
            info["ring_note"] = f"Ring buffer keeps the newest {self.cfg['storage']['ring_files']} files; older ones were deleted."
        return info
