"""Mirrored Bits emulator glue (docs/MIRRORED-BITS-SPEC.md): builds the link and the emulator settings from a test profile and adapts the emulator to
what the run manager expects of a test (the Pi that starts it: MirroredRun) and of the reflector (the Pi that answers: MirroredResponder)."""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from . import factory, metrics as M, system
from .mbemu import Emulator, Settings
from .mbemu import links as L
from .mbemu.engine import NS
from .mbemu.pairing import far_settings

NOTE = "SimCodec: our own framing for emulator-to-emulator tests. Not SEL wire-compatible."


def baud_of(p: dict) -> int:
    return int(p["serial"]["baud"] if p["transport"] == "serial" else p["mirrored"]["rate_baud"])


def is_mirrored(p: dict) -> bool:
    return p.get("kind", "trip") == "trip" and p.get("mode") == "mirrored"


def node_settings(p: dict) -> Settings:
    """The emulator settings of THIS end of a test profile: serial format from the port settings, everything else from mirrored.settings."""
    m = p["mirrored"]
    d = dict(m.get("settings") or {})
    d.setdefault("baud", baud_of(p))
    d["baud"] = baud_of(p)
    if p["transport"] == "serial":
        s = p["serial"]
        d["bytesize"], d["parity"], d["stopbits"] = s["bytesize"], s["parity"], s["stopbits"]
    d["codec"] = m.get("codec", "sim")
    d["rok_n"] = d.get("rok_n", 2)
    d.setdefault("trip_every_s", m.get("trip_every_s", 2.0))
    return Settings.from_dict(d)


def make_link(p: dict, role: str):
    """role 'A' (the Pi that starts the test) or 'B' (the Pi that answers).  PROTO A/B is a separate, per-end setting."""
    t = p["transport"]
    st = node_settings(p)
    if t == "serial":
        s = p["serial"]
        return L.MBSerialLink(factory.resolve_serial_port(s["port"]), s["baud"], s["bytesize"], s["parity"], s["stopbits"], bool(st.rtscts))
    n = p["net"]
    if role == "A" and not n["peer_host"]:
        raise factory.ConfigError("Pick the Pi to send to, or type its IP address.")
    if t == "udp":
        return L.UdpLink(role, n["peer_host"], n["port"], n["dscp"])
    if t == "tcp":
        return L.TcpLink(role, n["peer_host"], n["port"], n["dscp"])
    raise factory.ConfigError("Mirrored Bits mode runs over serial, UDP or TCP.")


class MirroredRun:
    """Gives the emulator of the Pi that starts the test the interface of engine.Tester (run / stop / status / error), plus the live 'state' event."""

    def __init__(self, profile: dict, link, emit: Callable[[dict], None] = lambda e: None, on_message: Callable[[dict], None] = lambda r: None,
                 registry=None, far_client=None, far_label: str = "", me_label: str = "", on_interval: Callable[[dict], None] = lambda r: None):
        self.p = profile
        self.emit = emit
        self.on_message, self.on_interval = on_message, on_interval
        self.status = "created"
        self.error: Optional[str] = None
        self.reg, self.far, self.far_label = registry, far_client, far_label
        self.me_label = me_label
        self.emu = Emulator(node_settings(profile), link, me_label or "this Pi", self._on_event, duration_s=profile["duration_s"])
        self.emu.imp.set(profile["mirrored"].get("impair") or {}, 0)
        self.stop_evt = self.emu.stop_evt
        self._last_state = 0.0

    @property
    def node(self):                               # older name
        return self.emu

    def stop(self):
        self.emu.stop(join=False)

    def _on_event(self, e: dict):
        self.emit(e)
        if e.get("ev") == "mb":
            self._state(e)

    def _state(self, e: dict):
        em = self.emu
        thr = float(self.p["threshold_ms"])
        rt = [x / 2.0 for x in em.rtt_ms[-2000:]]
        done = em.rtt_n
        p99 = M.percentile(rt, 99) if rt else None
        late = sum(1 for x in rt if x > thr)
        self.emit({"ev": "state", "phase": "steady", "elapsed_s": round(e["t_s"], 1), "total_s": self.p["duration_s"], "sent": done, "received": done,
                   "lost": em.rtt_alarm_events, "late": late, "now_ms": None if em.rtt_last is None else round(em.rtt_last / 2, 3),
                   "p99_ms": None if p99 is None else round(p99, 2), "max_ms": None if not rt else round(max(rt), 2),
                   "link": "red" if not e.get("rok") else "ok", "rok": e.get("rok"), "rbad": e.get("rbad"), "cbad": e.get("cbad")})

    def run(self) -> dict:
        if self.reg is not None:
            self.reg.set_local(self.emu, "near", self.far, self.far_label, self.me_label)
        try:
            self.emu.run()
        except Exception as ex:  # noqa
            self.error = f"{type(ex).__name__}: {ex}"
        finally:
            if self.reg is not None:
                self.reg.stop_follow()
        self.status = {"finished": "finished", "stopped": "stopped"}.get(self.emu.status_text, "failed")
        self.error = self.error or self.emu.error
        far_ev, far_st, far_note = self._collect_far()
        p = self.p
        one_way_ok, why = self._one_way_ok(far_ev)
        try:
            summary = self.emu.summary(threshold_ms=p["threshold_ms"], percentile=p["rule"]["percentile"], max_loss_pct=p["rule"]["max_loss_pct"],
                                       max_down_pct=p["mirrored"]["max_down_pct"], far=far_ev, one_way_ok=one_way_ok, far_status=far_st,
                                       tol_ms=max(1.0, p["max_clock_offset_ms"]))
        except Exception as ex:  # noqa
            self.error = self.error or f"summary: {type(ex).__name__}: {ex}"
            return {"error": self.error}
        m = summary["mirrored"]
        if far_note:
            m["one_way_note"] = far_note
        elif why and not one_way_ok:
            m["one_way_note"] = why
        summary["transport_desc"] = self.emu.link.describe()
        summary["peer"] = {"role": far_st.get("name") if far_st else "the other Pi"}
        self._chart_rows()
        self.emit({"ev": "done", "status": self.status, "error": self.error, "summary": summary})
        return summary

    # ---- the far end, once, at the end (before the run manager releases it)
    def _collect_far(self):
        if self.far is None:
            return None, None, None
        try:
            ev = self.far.get("/api/mb/reflect", timeout=8)
            st = self.far.get("/api/mb/live?since_tx=999999999&since_rx=999999999", timeout=8)
            return ev, (st or {}).get("local"), None
        except Exception as ex:  # noqa
            return None, None, f"The other Pi could not be read at the end of the test ({str(ex)[:120]}), so bit transfer time is estimated from the echo bit."

    def _one_way_ok(self, far_ev):
        p = self.p
        if far_ev is None:
            return False, None
        maxo = float(p["max_clock_offset_ms"])
        li = (p.get("leg_info") or {}).get("clock_agree")
        if li is False:
            return False, ((p.get("leg_info") or {}).get("clock_note") or "The two Pis' clocks do not follow the same time source") + ", so bit transfer time is estimated from the echo bit."
        here = system.clock_state(0)
        fc = far_ev.get("clock") or {}
        if not (here.get("synced") and here.get("offset_ms") is not None and abs(here["offset_ms"]) <= maxo):
            return False, f"This Pi's clock is not synced within {maxo:g} ms, so bit transfer time is estimated from the echo bit."
        if not (fc.get("synced") and fc.get("offset_ms") is not None and abs(fc["offset_ms"]) <= maxo):
            return False, f"The other Pi's clock is not synced within {maxo:g} ms, so bit transfer time is estimated from the echo bit."
        return True, None

    def _chart_rows(self):
        """Per-trip rows for the results chart (bit transfer time over the run, thinned to ~1500 points) and per-second rows for the report."""
        em = self.emu
        thr_us = int(float(self.p["threshold_ms"]) * 1000)
        ts, rs = em.rtt_t, em.rtt_ms
        n = len(rs)
        if not n:
            return
        step = max(1, n // 1500)
        seq = 0
        for i in range(0, n, step):
            us = int(rs[i] * 500)                             # RTT / 2 in microseconds
            self.on_message({"seq": seq, "phase": "steady", "idle_gap_s": None, "rtt_us": us, "status": M.LATE if us > thr_us else M.OK,
                             "t_send_ms": ts[i] * 1000.0, "up_us": None, "down_us": None})
            seq += 1
        sec: dict = {}
        for t, r in zip(ts, rs):
            sec.setdefault(int(t), []).append(r * 500)
        for k in sorted(sec):
            v = sorted(sec[k])
            pick = lambda q: int(v[min(len(v) - 1, int(q * (len(v) - 1)))])
            self.on_interval({"t_offset_s": k, "sent": len(v), "received": len(v), "lost": 0, "late": sum(1 for x in v if x > thr_us),
                              "p50_us": pick(0.5), "p95_us": pick(0.95), "p99_us": pick(0.99), "max_us": int(v[-1])})


class MirroredResponder:
    """The Pi that answers: its own emulator (the mirror image of the starter's settings).  Same face as engine.Responder."""

    def __init__(self, profile: dict, link, emit: Callable[[dict], None] = lambda e: None, registry=None, name: str = ""):
        st = node_settings(profile)
        self.emu = Emulator(st, link, name or "this Pi", emit)
        self.link, self.profile, self.reg, self._on = link, profile, registry, False

    @property
    def running(self) -> bool:
        return self._on and self.emu.thread is not None and self.emu.thread.is_alive()

    def start(self):
        try:                                   # fail fast (port busy, no permission) in the caller, like the echo Responder
            self.link.open()
            self.link.close()
        except OSError:
            raise
        self._on = True
        if self.reg is not None:
            self.reg.set_local(self.emu, "far", None, "", self.emu.name)
        self.emu.start()

    def stop(self):
        self.emu.stop()
        self._on = False

    def status(self) -> dict:
        s = self.emu.status()
        s["running"] = self.running
        s["transport"] = self.link.describe()
        s["mode"] = "mirrored"
        s["echoed"] = s["counters"]["rx_good"]
        s["hellos"] = 0
        s["last_rx_ago_s"] = None if self.emu.ch.last_arrival is None else round(max(0, self.emu.mono() - self.emu.ch.last_arrival) / NS, 2)
        return s

    def reflect_events(self) -> dict:
        return self.emu.reflect_events()


def probe(p: dict) -> dict:
    """Pre-flight: run an emulator for ~2 s against the other end and see whether its messages arrive and check out."""
    st = node_settings(p)
    st.trip_every_s = 0
    link = make_link(p, "A")
    emu = Emulator(st, link, "probe", duration_s=2.0)
    emu.run()
    if emu.error:
        return {"ok": False, "error": f"Could not open the link: {emu.error}"}
    good = emu.ch.msgs_good
    rej = emu.ch.msgs_rejected
    if good < 2:
        why = ""
        if rej:
            top = max(emu.ch.rejects.items(), key=lambda kv: kv[1])
            why = f" It did receive {rej} message(s) it rejected (mostly: {top[0]}) - check TX_ID / RX_ID, the baud rate and the serial format at both ends."
        return {"ok": False, "error": "The other Pi's messages did not arrive. Check the cable/port and speed, and that the other Pi is up and its app can start a Mirrored Bits test." + why}
    return {"ok": True, "peer": {"role": "answering Pi (Mirrored Bits mode)", "synced": None, "offset_ms": None},
            "one_way_ok": False, "mirrored": {"messages_checked": good, "bad": rej}}
