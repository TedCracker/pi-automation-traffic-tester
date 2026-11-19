"""Passive serial tap (RS-232 / RS-485): one reader per USB-serial adapter, per-chunk host timestamps, framing, DNP3 / Modbus RTU decoding.

Files written (docs/SPEC.md 14): tap_*.pcapng (Ethernet-wrapped so Wireshark decodes DNP3/Modbus as-is, rotating), tap-rtu_*.pcapng (raw frames,
LINKTYPE_USER0, optional), tap-raw.bin (every byte chunk with its timestamp), tap-frames.csv."""
from __future__ import annotations

import csv
import datetime as dt
import os
import select
import struct
import threading
import time
from typing import Callable, Dict, List, Optional

from . import dnp3, modbus, pcapng, wrap

TIMESTAMP_NOTE = ("Timestamps are the host time when the USB-serial adapter delivered the bytes: expect ±1-2 ms (USB frame + latency_timer), "
                  "not the exact time on the wire; gaps shorter than about 2 ms inside one delivery cannot be seen.")


class SerialTap:
    def __init__(self, cfg: dict, out_dir: str, emit: Callable[[dict], None] = lambda e: None, ring: Optional[dict] = None):
        self.cfg, self.out_dir, self.emit = cfg, out_dir, emit
        self.ring = ring or {}
        self.proto = cfg.get("protocol", "modbus_rtu")
        self.channels: List[dict] = cfg["channels"]
        os.makedirs(out_dir, exist_ok=True)
        self.lock = threading.Lock()
        self.rows: List[dict] = []
        self.n = 0
        self.stop_evt = threading.Event()
        self.threads: List[threading.Thread] = []
        self.sers: list = []
        self.error: Optional[str] = None
        self.t_start = time.time()
        self.stats = {"bytes": [0] * len(self.channels), "frames": [0] * len(self.channels), "crc_fail": 0, "garbage_bytes": 0, "malformed": 0,
                      "max_gap_ms": 0.0, "func": {}}
        self._last_frame_ns: Optional[int] = None
        self.wrapper = wrap.Wrapper()
        mb = int(self.ring.get("ring_mb", 100)) * 1024 * 1024
        rs = float(self.ring.get("ring_minutes", 0)) * 60
        keep = int(self.ring.get("ring_files", 10))
        ifs = [(pcapng.LINKTYPE_ETHERNET, "serial-tap", f"{self.proto} wrapped for Wireshark (DNP3 as UDP/20000, Modbus as TCP/502)")]
        self.pcap = pcapng.RingWriter(out_dir, "tap", ifs, mb, rs, keep)
        self.pcap_rtu = pcapng.RingWriter(out_dir, "tap-rtu", [(pcapng.LINKTYPE_USER0, "serial-raw", "raw serial frames (map DLT_USER0 to mbrtu / dnp3)")], mb, rs, keep) if cfg.get("also_user_dlt") else None
        self.raw = open(os.path.join(out_dir, "tap-raw.bin"), "wb")
        self.csvf = open(os.path.join(out_dir, "tap-frames.csv"), "w", newline="")
        self.csv = csv.writer(self.csvf)
        self.csv.writerow(["time", "t_ns", "channel", "protocol", "kind", "length", "crc", "hex", "summary"])
        self.framers = []
        for ch in self.channels:
            if self.proto == "dnp3":
                self.framers.append(dnp3.Dnp3Framer())
            elif self.proto == "modbus_rtu":
                gap = cfg.get("gap_ms") or modbus.default_gap_ms(int(ch.get("baud", 9600)))
                self.framers.append(modbus.ModbusFramer(gap))
            else:
                self.framers.append(None)
        self._expect_request = True              # single-channel Modbus: request / response alternate

    # ---------------------------------------------------------------- start / stop
    def start(self) -> None:
        import serial
        try:
            for ch in self.channels:
                s = serial.Serial(ch["port"], int(ch.get("baud", 9600)), bytesize=int(ch.get("bytesize", 8)), parity=ch.get("parity", "N"),
                                  stopbits=int(ch.get("stopbits", 1)), timeout=0)
                try:
                    s.reset_input_buffer()
                except Exception:
                    pass
                self.sers.append(s)
        except Exception:
            self._close_ports()
            raise
        for i, s in enumerate(self.sers):
            t = threading.Thread(target=self._reader, args=(i, s), name=f"serial-tap-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    def _close_ports(self) -> None:
        for s in self.sers:
            try:
                s.close()
            except Exception:
                pass

    def stop(self) -> dict:
        self.stop_evt.set()
        for t in self.threads:
            t.join(timeout=2)
        for i, f in enumerate(self.framers):
            if f is not None:
                for fr in (f.flush() if hasattr(f, "flush") else f.tick(time.time_ns() + 10 ** 12)):
                    self._frame(i, *fr)
        self._close_ports()
        for w in (self.pcap, self.pcap_rtu):
            if w:
                w.close()
        self.raw.close()
        self.csvf.close()
        return self.result()

    def result(self) -> dict:
        self.stats["garbage_bytes"] = sum(getattr(f, "skipped", 0) for f in self.framers)      # bytes the DNP3 framer had to skip (Modbus has no byte count)
        files = sorted(os.path.basename(p) for p in self.pcap.files if os.path.exists(p))
        if self.pcap_rtu:
            files += sorted(os.path.basename(p) for p in self.pcap_rtu.files if os.path.exists(p))
        files += ["tap-raw.bin", "tap-frames.csv"]
        return {"frames": self.n, "bytes": list(self.stats["bytes"]), "frames_per_channel": list(self.stats["frames"]), "crc_failures": self.stats["crc_fail"],
                "malformed": self.stats["malformed"], "garbage_bytes": self.stats["garbage_bytes"], "max_gap_ms": round(self.stats["max_gap_ms"], 3),
                "functions": dict(self.stats["func"]), "files": files, "files_deleted_by_ring": self.pcap.deleted, "timestamp_note": TIMESTAMP_NOTE,
                "protocol": self.proto}

    # ---------------------------------------------------------------- reading
    def _reader(self, idx: int, ser) -> None:
        fd = ser.fileno()
        framer = self.framers[idx]
        try:
            while not self.stop_evt.is_set():
                r, _, _ = select.select([fd], [], [], 0.02)
                if r:
                    data = os.read(fd, 4096)
                    t = time.time_ns()
                    if not data:                               # readable but nothing to read = the adapter is gone (unplugged)
                        if not self.stop_evt.is_set():
                            self.error = f"Serial adapter {self.channels[idx]['port']} was unplugged."
                        break
                    if data:
                        self.stats["bytes"][idx] += len(data)
                        self.raw.write(struct.pack("<QBH", t, idx, len(data)) + data)
                        if framer is None:
                            self._frame(idx, data, t, t, True)
                        else:
                            for fr in framer.feed(data, t):
                                self._frame(idx, *fr)
                if framer is not None:
                    for fr in framer.tick(time.time_ns()):
                        self._frame(idx, *fr)
        except Exception as e:  # noqa
            if not self.stop_evt.is_set():
                self.error = f"{type(e).__name__}: {e}"

    # ---------------------------------------------------------------- one decoded frame
    def _frame(self, ch: int, data: bytes, t_first: int, t_last: int, crc_ok: bool) -> None:
        with self.lock:
            self.n += 1
            n = self.n
            self.stats["frames"][ch] += 1
            if self._last_frame_ns is not None and t_first > self._last_frame_ns:
                self.stats["max_gap_ms"] = max(self.stats["max_gap_ms"], (t_first - self._last_frame_ns) / 1e6)
            self._last_frame_ns = t_last
            if self.proto == "dnp3":
                d = dnp3.decode_frame(data)
                from_a = d.get("from_master", ch == 0)
                eth = self.wrapper.dnp3(data if data[:2] == dnp3.START else b"\x05\x64" + data, from_a) if d["error"] is None else None
                proto, kind = "DNP3", "from master" if from_a else "from outstation"
            elif self.proto == "modbus_rtu":
                if len(self.channels) >= 2:
                    is_req = ch == 0
                else:
                    g = modbus.decode_rtu(data, None)
                    is_req = (g["kind"] == "request") if g.get("kind") in ("request", "response") else self._expect_request
                    self._expect_request = not is_req
                d = modbus.decode_rtu(data, is_req)
                from_a = is_req
                eth = self.wrapper.modbus(data, from_a) if len(data) >= 4 else None
                proto, kind = "Modbus RTU", d.get("kind", "?")
            else:
                d = {"summary": f"{len(data)} raw byte(s)", "crc_ok": True, "error": None, "fields": {}}
                from_a = ch == 0
                eth, proto, kind = self.wrapper.raw(data, from_a), "raw", ""
            ok = bool(d.get("crc_ok", crc_ok))
            if not ok:
                self.stats["crc_fail"] += 1
            if d.get("error"):
                self.stats["malformed"] += 1
            fkey = d["summary"].split("|")[1].strip().split(" ")[0] if (self.proto == "dnp3" and "|" in d["summary"]) else (f"fc{d.get('func')}" if self.proto == "modbus_rtu" else "raw")
            self.stats["func"][fkey] = self.stats["func"].get(fkey, 0) + 1
            comment = f"channel {self.channels[ch]['name']}; {proto}; crc={'ok' if ok else 'FAIL'}; first byte {t_first} ns, last byte {t_last} ns"
            if eth is not None:
                self.pcap.write(0, t_first, eth, comment)
            if self.pcap_rtu:
                self.pcap_rtu.write(0, t_first, data, comment)
            iso = dt.datetime.fromtimestamp(t_first / 1e9).isoformat(timespec="microseconds")
            self.csv.writerow([iso, t_first, self.channels[ch]["name"], proto, kind, len(data), "ok" if ok else "FAIL", data.hex(), d["summary"]])
            prev_t, self._row_t = getattr(self, "_row_t", None), t_first
            resp = None
            if from_a:                                           # a request from the master side: the next frame the other way is its response
                self._pend_ns = getattr(self, "_pend_ns", None) or t_last
            elif getattr(self, "_pend_ns", None):
                resp, self._pend_ns = round((t_first - self._pend_ns) / 1e6, 3), None
            row = {"n": n, "t_ns": t_first, "channel": ch, "channel_name": self.channels[ch]["name"], "proto": proto, "kind": kind, "len": len(data), "crc_ok": ok,
                   "hex": data[:256].hex(), "info": d["summary"], "fields": d.get("fields", {}), "errors": 0 if ok and not d.get("error") else 1, "crc_bad": not ok,
                   "resp_ms": resp, "delta_ms": resp if resp is not None else (round((t_first - prev_t) / 1e6, 3) if prev_t else 0.0), "flow": "serial", "dir": "→" if from_a else "←"}
            self.rows.append(row)
            if len(self.rows) > 2000:
                del self.rows[:500]

    def live(self, since: int = 0) -> dict:
        with self.lock:
            rows = [r for r in self.rows if r["n"] > since][-300:]
            return {"n": self.n, "rows": rows, "bytes": list(self.stats["bytes"]), "frames": list(self.stats["frames"]), "crc_fail": self.stats["crc_fail"],
                    "error": self.error}
