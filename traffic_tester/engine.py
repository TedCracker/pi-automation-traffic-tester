"""Test engine: Tester (Pi A) and Responder (Pi B). One blocking thread each (docs/SPEC.md section 4)."""
from __future__ import annotations

import heapq
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional

from . import frames as F
from . import metrics as M
from . import system
from .transports import Transport, mono, real

NS = 1_000_000_000
RESPONDER_RETRY_S = 2.0        # after a port error the Responder closes the port and tries again this often


class PairingError(Exception):
    pass


def plan_steps(p: dict) -> List[tuple]:
    gaps = [g for g in p.get("idle_gaps_s", []) for _ in range(p.get("idle_repeats", 1))]
    dur = float(p["duration_s"])
    if not gaps:
        return [("steady", dur)]
    if p.get("idle_mode") == "interleaved":
        seg = dur / (len(gaps) + 1)
        steps = [("steady", seg)]
        for g in gaps:
            steps += [("idle", g), ("steady", seg)]
        return steps
    return [("steady", dur)] + [("idle", g) for g in gaps]


class Tester:
    """Runs one test. `emit(event_dict)` is called from the engine thread for live UI updates,
    `on_messages(rows)` / `on_interval(row)` persist results."""

    def __init__(self, profile: dict, transport: Transport, emit: Callable[[dict], None] = lambda e: None,
                 on_message: Callable[[dict], None] = lambda r: None,
                 on_interval: Callable[[dict], None] = lambda r: None):
        self.p = profile
        self.tr = transport
        self.emit, self.on_message, self.on_interval = emit, on_message, on_interval
        self.stop_evt = threading.Event()
        self.ext = profile["frame"] == "extended"
        self.seq_bits = 32 if self.ext else 16
        self.thr_us = int(profile["threshold_ms"] * 1000)
        self.timeout_ns = int(profile["timeout_ms"] * 1_000_000)
        self.interval_ns = int(profile["interval_ms"] * 1_000_000)
        self.next_seq = 0
        self.pending: Dict[int, dict] = {}
        self.order: deque = deque()
        self.done: Dict[int, str] = {}
        self.highest_rx = -1
        self.cnt = {"duplicates": 0, "out_of_order": 0, "stale": 0}
        self.leg = profile.get("leg_info") or {}                     # which Pi sends / which one reflects (set by the planner); empty for a plain local test
        self.basis = self.leg.get("basis", "rtt")                # "up" = pass/fail on the one-way delay tester -> reflector
        self.acc = M.Accumulator(profile["threshold_ms"], basis=self.basis)
        self.t0 = mono()
        self.phase = "starting"
        self.sent_total = self.recv_total = self.lost_total = self.late_total = 0
        self.recent: deque = deque(maxlen=2000)
        self._bucket = 0
        self._bucket_rows: List[dict] = []
        self._last_state = 0.0
        self.peer_info: dict = {}
        self.one_way_req = bool(profile.get("one_way"))
        self.one_way_ok = False
        self._ow_bad = False
        self.last_good_rx: Optional[int] = None
        self.tcp_reconnects: List[dict] = []
        self._tcp_pending_outage: Optional[dict] = None
        self.stalls = 0
        self._tx_late_ns = 0
        self._consec_lost = 0
        self.error: Optional[str] = None
        self.status = "created"
        self.total_s = 0.0

    # ---------------------------------------------------------------- control
    def stop(self):
        self.stop_evt.set()

    def _state(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_state < 0.4:
            return
        self._last_state = now
        rec = list(self.recent)
        self.emit({"ev": "state", "phase": self.phase, "elapsed_s": round((mono() - self.t0) / NS, 1),
                   "total_s": self.total_s, "sent": self.sent_total, "received": self.recv_total,
                   "lost": self.lost_total, "late": self.late_total,
                   "now_ms": None if not rec else round(rec[-1] / 1000, 2),
                   "p99_ms": None if not rec else round(M.percentile(rec, 99) / 1000, 2),
                   "max_ms": None if not rec else round(max(rec) / 1000, 2),
                   "link": "red" if (self._consec_lost >= 1) else "ok",
                   "tx_late_ms": round(self._tx_late_ns / 1e6, 2), "interval_ms": self.p["interval_ms"]})
        self._tx_late_ns = 0

    # ---------------------------------------------------------------- main
    def run(self) -> dict:
        p = self.p
        steps = plan_steps(p)
        self.total_s = round(sum(s[1] for s in steps), 1)
        try:
            self.tr.open()
            self._hello()
            self.t0 = mono()
            self.status = "running"
            self.emit({"ev": "started", "total_s": self.total_s, "transport": self.tr.describe(),
                       "one_way": self.one_way_ok, "peer": self.peer_info})
            for kind, val in steps:
                if self.stop_evt.is_set():
                    break
                if kind == "steady":
                    self.phase = "steady"
                    self._steady(val)
                else:
                    self.phase = "idle"
                    self._idle(val)
            self.phase = "finishing"
            self._drain()
            self.status = "stopped" if self.stop_evt.is_set() else "finished"
        except PairingError as e:
            self.error, self.status = str(e), "failed"
        except Exception as e:  # noqa
            self.error, self.status = f"{type(e).__name__}: {e}", "failed"
        finally:
            try:
                self.tr.close()
            except Exception:
                pass
        self._flush_bucket()
        summary = self.summary()
        self.emit({"ev": "done", "status": self.status, "error": self.error, "summary": summary})
        return summary

    def summary(self) -> dict:
        counters = dict(self.cnt)
        counters["corrupted"] = self.tr.corrupted
        extra = {}
        if hasattr(self.tr, "connect_ms"):
            cm = self.tr.connect_ms
            extra["tcp"] = {"sessions": self.tr.sessions, "setup_ms": cm[0] if cm else None,
                            "connect_ms_avg": round(sum(cm) / len(cm), 3) if cm else None,
                            "reconnects": self.tcp_reconnects, "stalls": self.stalls}
        if hasattr(self.tr, "goose_stats"):
            extra["goose"] = self.tr.goose_stats()
        if extra:
            counters["extra"] = extra
        s = self.acc.summary(self.p, counters, one_way_ok=self.one_way_ok and not self._ow_bad)
        s["peer"] = self.peer_info
        if self.leg:
            s["leg"] = {k: v for k, v in self.leg.items() if k not in ("clock_agree", "clock_note")}
        s["transport_desc"] = self.tr.describe()
        s["local_overhead_note"] = None
        return s

    # ---------------------------------------------------------------- pairing
    def _hello(self):
        # drain anything left over from earlier traffic
        for _ in range(20):
            if not self.tr.read(0.01):
                break
        got = None
        for attempt in range(3):
            if self.stop_evt.is_set():
                return
            try:
                self.tr.write(F.Frame(F.T_HELLO, 0, 0, 0, t1=real(), extended=self.ext))
            except OSError:
                pass
            end = time.monotonic() + 1.0
            while time.monotonic() < end and not got:
                for f, _, _ in self.tr.read(0.05):
                    if f.type == F.T_HELLO_ACK:
                        got = f
                        break
            if got:
                break
        if not got:
            raise PairingError(f"{self.leg.get('dest') or 'The other Pi'} did not answer. Check the cable, port or address, and that the Pi is on and running Pi Automation Traffic Tester.")
        self.peer_info = {"name": self.leg.get("dest"), "synced": bool(got.flags & F.F_SYNCED),
                          "offset_ms": round(got.offset_us / 1000, 3) if got.extended else None}
        if self.one_way_req:
            maxo = self.p["max_clock_offset_ms"]
            a = system.clock_state()
            a_ok = system.clock_ok(maxo)
            b_ok = got.extended and bool(got.flags & F.F_SYNCED) and abs(got.offset_us) <= maxo * 1000
            agree = self.leg.get("clock_agree")                  # the controller's check that both clocks follow the SAME time source (None = not checked)
            self.one_way_ok = bool(a_ok and b_ok and agree is not False)
            if not self.one_way_ok:
                why = []
                if not a_ok:
                    why.append("%s's clock is not synced within %g ms" % (self.leg.get("source") or "this Pi", maxo))
                if not b_ok:
                    why.append("%s's clock is not synced within %g ms (offset %s)" % (self.leg.get("dest") or "the other Pi", maxo, self.peer_info.get("offset_ms")))
                if agree is False and a_ok and b_ok:
                    why.append(self.leg.get("clock_note") or "the two Pis do not follow the same time source")
                self.peer_info["one_way_unavailable"] = "; ".join(why)
            self.peer_info["a_offset_ms"] = a.get("offset_ms")
        if self.basis == "up" and not self.one_way_ok:
            raise PairingError("One-way timing needs the clocks of both Pis to agree within %g ms, and they do not yet: %s. Sync them (Settings → Time source) or choose Round trip." % (
                self.p["max_clock_offset_ms"], self.peer_info.get("one_way_unavailable") or "unknown"))

    # ---------------------------------------------------------------- send/receive
    def _steady(self, seconds: float):
        nxt = mono()
        end = nxt + int(seconds * NS)
        while not self.stop_evt.is_set():
            now = mono()
            if now >= end:
                break
            if now >= nxt:
                if now - nxt > self._tx_late_ns:           # how late this message left against its schedule (tester lamp: is the Pi keeping up?)
                    self._tx_late_ns = now - nxt
                self._send("steady", None)
                nxt += self.interval_ns
                if now - nxt > 5 * self.interval_ns:      # fell behind: don't burst to catch up
                    nxt = now + self.interval_ns
            self._pump(max(0.0, (min(nxt, end) - mono()) / NS))
            self._maintenance()

    def _idle(self, gap_s: float):
        end = mono() + int(gap_s * NS)
        self.emit({"ev": "idle_wait", "gap_s": gap_s})
        while not self.stop_evt.is_set() and mono() < end:
            self._pump(min(0.25, max(0.0, (end - mono()) / NS)))
            self._maintenance()
        if self.stop_evt.is_set():
            return
        seq = self._send("idle", gap_s)
        while not self.stop_evt.is_set() and seq in self.pending:
            self._pump(0.05)
            self._maintenance()

    def _drain(self):
        end = mono() + self.timeout_ns + 50_000_000
        while self.pending and mono() < end and not self.stop_evt.is_set():
            self._pump(0.02)
            self._maintenance()
        # whatever is still pending counts as lost
        self._expire(force=True)

    def _send(self, phase: str, gap) -> int:
        seq = self.next_seq
        self.next_seq += 1
        bits = (1 << (seq % 8)) if phase == "steady" else 0x81
        t_real = real()
        flags = F.F_IDLE if phase == "idle" else 0
        fr = F.Frame(F.T_TRIP, seq & ((1 << self.seq_bits) - 1), bits, flags, t1=t_real, extended=self.ext)
        t_send = mono()
        rec = {"seq": seq, "phase": phase, "gap": gap, "t_send": t_send, "t_real": t_real, "bits": bits}
        if phase == "steady":
            self.sent_total += 1               # counted before the write: a message the link could not take is sent-and-lost, as in the final summary
        try:
            self.tr.write(fr)
        except OSError:
            self._finish(rec, None, M.LOST, None, None, None)
            return seq
        self.pending[seq] = rec
        self.order.append(seq)
        self.emit({"ev": "tx", "seq": seq, "bits": bits, "phase": phase})
        return seq

    def _pump(self, timeout: float):
        frames = self.tr.read(timeout)
        if self.tr.events:
            self._tcp_events()
        for f, rx_m, rx_r in frames:
            self._expire(rx_m)
            self._rx(f, rx_m, rx_r)
        self._expire()

    def _rx(self, f: F.Frame, rx_m: int, rx_r: int):
        if f.type != F.T_ECHO:
            return
        seq = F.unwrap_seq(f.seq, max(self.next_seq - 1, 0), self.seq_bits)
        rec = self.pending.pop(seq, None)
        if rec is None:
            st = self.done.get(seq)
            if st == M.LOST:
                self.cnt["stale"] += 1
            elif st is not None:
                self.cnt["duplicates"] += 1
            return
        rtt_us = (rx_m - rec["t_send"]) // 1000
        status = M.classify(rtt_us, self.thr_us, self.timeout_ns // 1000)
        if seq < self.highest_rx:
            self.cnt["out_of_order"] += 1
        else:
            self.highest_rx = seq
        up = down = None
        if f.extended and f.t2 and self.one_way_ok:
            maxo = self.p["max_clock_offset_ms"]
            if not (f.flags & F.F_SYNCED) or abs(f.offset_us) > maxo * 1000 or not system.clock_ok(maxo):
                self._ow_bad = True
            else:
                up = (f.t2 - rec["t_real"]) // 1000
                down = (rx_r - f.t3) // 1000
        if self.basis == "up":
            if up is not None:
                status = M.LATE if up > self.thr_us else M.OK         # judged on the delay tester -> reflector; the message did arrive
        self.last_good_rx = rx_m
        self._consec_lost = 0
        self._finish(rec, rtt_us, status, up, down, f.bits)
        if self._tcp_pending_outage is not None:
            o = self._tcp_pending_outage
            o["outage_ms"] = round((rx_m - o["since"]) / 1e6, 1)
            o.pop("since")
            self.tcp_reconnects.append(o)
            self._tcp_pending_outage = None

    def _expire(self, now: Optional[int] = None, force=False):
        now = now or mono()
        while self.order:
            seq = self.order[0]
            rec = self.pending.get(seq)
            if rec is None:
                self.order.popleft()
                continue
            if not force and now - rec["t_send"] <= self.timeout_ns:
                break
            self.order.popleft()
            del self.pending[seq]
            self._finish(rec, None, M.LOST, None, None, None)
            self._consec_lost += 1
            if hasattr(self.tr, "connect_ms") and self.tr.role == "tester" and self._consec_lost >= 5 and self.tr.connected:
                self.stalls += 1
                self._consec_lost = 0
                self.emit({"ev": "stall"})
                self.tr.force_reconnect()

    def _finish(self, rec: dict, rtt_us, status, up, down, bits_rx):
        seq = rec["seq"]
        self.done[seq] = status
        m = {"seq": seq, "phase": rec["phase"], "idle_gap_s": rec["gap"], "rtt_us": rtt_us, "status": status,
             "up_us": up, "down_us": down, "t_send_ms": round((rec["t_send"] - self.t0) / 1e6, 3)}
        self.acc.add(m)
        self.on_message(m)
        lat_us = up if (self.basis == "up" and up is not None) else rtt_us         # the number the live screen shows and judges on
        if rec["phase"] == "steady":
            if status == M.LOST:
                self.lost_total += 1
            else:
                self.recv_total += 1
                self.recent.append(lat_us)
                if status == M.LATE:
                    self.late_total += 1
            self._bucket_rows.append(m)
        self.emit({"ev": "msg", "seq": seq, "phase": rec["phase"], "status": status,
                   "rtt_ms": None if lat_us is None else round(lat_us / 1000, 3),
                   "bits_tx": rec["bits"], "bits_rx": bits_rx, "t_s": round(m["t_send_ms"] / 1000, 3),
                   "idle_gap_s": rec["gap"]})
        self._state()

    def _maintenance(self):
        idx = int((mono() - self.t0) / NS / self.p["summary_interval_s"])
        if idx > self._bucket:
            self._flush_bucket()
            self._bucket = idx
        if len(self.done) > 150_000:
            lim = self.next_seq - 70_000
            self.done = {k: v for k, v in self.done.items() if k > lim}
        self._state()

    def _flush_bucket(self):
        if not self._bucket_rows:
            return
        row = M.interval_summary(self._bucket_rows)
        row["t_offset_s"] = round(self._bucket * self.p["summary_interval_s"], 3)
        self._bucket_rows = []
        self.on_interval(row)
        self.emit({"ev": "interval", **row})

    def _tcp_events(self):
        ev, self.tr.events = self.tr.events, []
        for e in ev:
            if e["event"] == "disconnected":
                self.emit({"ev": "link", "state": "down"})
                self._tcp_pending_outage = {"since": self.last_good_rx or e["t_mono_ns"]}
            elif e["event"] == "connected":
                if self._tcp_pending_outage is not None:
                    self._tcp_pending_outage["connect_ms"] = e["connect_ms"]
                self.emit({"ev": "link", "state": "up", "connect_ms": e.get("connect_ms")})


class Responder:
    """Pi B: echo every TRIP (docs/SPEC.md 2). Test hooks (delay/drop) only work when TT_TEST_HOOKS=1."""

    def __init__(self, transport: Transport, delay_ms: float = 0.0, drop_pct: float = 0.0,
                 hooks_enabled: bool = False):
        self.tr = transport
        self.stop_evt = threading.Event()
        self.delay_ns = int(delay_ms * 1e6) if hooks_enabled else 0
        self.drop_pct = drop_pct if hooks_enabled else 0.0
        self.echoed = 0
        self.hellos = 0
        self.last_rx: Optional[float] = None
        self.error: Optional[str] = None
        self.thread: Optional[threading.Thread] = None
        self.started = time.time()
        self.running = False
        self.recoveries = 0
        self._rnd = __import__("random").Random(1)

    def start(self):
        self.tr.open()           # fail fast (port busy, no permission) in the caller
        self.running = True
        self.thread = threading.Thread(target=self._loop, name="responder", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=3)
        try:
            self.tr.close()
        except Exception:
            pass
        self.running = False

    def status(self) -> dict:
        return {"running": self.running, "echoed": self.echoed, "hellos": self.hellos,
                "last_rx_ago_s": None if self.last_rx is None else round(time.time() - self.last_rx, 1),
                "transport": self.tr.describe(), "error": self.error, "recoveries": self.recoveries,
                "tcp_sessions": getattr(self.tr, "sessions", None)}

    def _reply(self, f: F.Frame, rx_r: int, t3: Optional[int] = None):
        st = system.clock_state()
        flags = (f.flags & F.F_IDLE) | (F.F_SYNCED if st["synced"] else 0)
        r = F.Frame(F.T_ECHO, f.seq, f.bits, flags, t1=f.t1, t2=rx_r, t3=t3 or real(),
                    offset_us=system.clock_offset_us(), extended=f.extended)
        try:
            self.tr.write(r)
        except OSError:
            pass

    def _loop(self):
        """Pi B usually runs unattended: a port error (USB-serial adapter replugged, interface down) must not end the Responder.
        Close the port, wait, open it again, and keep echoing; `error` shows what is wrong while it is down."""
        try:
            while not self.stop_evt.is_set():
                try:
                    self._serve()
                except Exception as e:  # noqa
                    self.error = f"{type(e).__name__}: {e}"
                while not self.stop_evt.is_set():
                    try:
                        self.tr.close()
                    except Exception:
                        pass
                    if self.stop_evt.wait(RESPONDER_RETRY_S):
                        break
                    try:
                        self.tr.open()
                        self.recoveries += 1
                        self.error = None
                        break
                    except Exception as e:  # noqa
                        self.error = f"{type(e).__name__}: {e}"
        finally:
            self.running = False

    def _serve(self):
        heap: list = []
        n = 0
        while not self.stop_evt.is_set():
            timeout = 0.1
            if heap:
                timeout = max(0.0, min(timeout, (heap[0][0] - mono()) / NS))
            for f, rx_m, rx_r in self.tr.read(timeout):
                self.last_rx = time.time()
                if f.type == F.T_HELLO:
                    self.hellos += 1
                    st = system.clock_state()
                    try:
                        self.tr.write(F.Frame(F.T_HELLO_ACK, f.seq, 0, F.F_SYNCED if st["synced"] else 0,
                                              t1=f.t1, t2=rx_r, t3=real(), offset_us=system.clock_offset_us(),
                                              extended=f.extended))
                    except OSError:
                        pass
                elif f.type == F.T_TRIP:
                    if self.drop_pct and self._rnd.random() * 100 < self.drop_pct:
                        continue
                    self.echoed += 1
                    if self.delay_ns:
                        n += 1
                        heapq.heappush(heap, (rx_m + self.delay_ns, n, f, rx_r))
                    else:
                        self._reply(f, rx_r)
            while heap and heap[0][0] <= mono():
                _, _, f, rx_r = heapq.heappop(heap)
                self._reply(f, rx_r)
