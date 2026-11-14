"""Run manager: one test at a time (which may have two directions running at once), DB persistence, live-event fan-out, netem, background load.

A test is planned into LEGS (plan.py): "this Pi sends, that Pi reflects". A leg whose sender is this Pi runs here; a leg whose sender is another Pi
runs there (remote.RemoteTester) and its result is copied back. Every leg is stored as its own run; legs of one test share a group id."""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections import deque
from typing import Optional

from . import engine, factory, health, marks as MK, mbrun, orchestrate as OR, plan as PL, profiles as P, system
from .reflector import Busy as PortBusy
from .remote import RemoteTester
from .gen import node as gnode, run as grun
from .sniff import session as ssession
from .store import Store


class Busy(Exception):
    pass


class LoadGen(threading.Thread):
    """Background UDP bulk traffic (latency-under-load mode)."""

    def __init__(self, host: str, port: int, mbps: float):
        super().__init__(daemon=True)
        self.host, self.port, self.mbps = host, port, mbps
        self.stop_evt = threading.Event()
        self.sent_bytes = 0

    def run(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        pkt = b"\x00" * 1200
        per_s = self.mbps * 1e6 / 8 / len(pkt)
        t0 = time.monotonic()
        n = 0
        while not self.stop_evt.is_set():
            due = t0 + n / per_s
            now = time.monotonic()
            if due > now:
                time.sleep(min(0.01, due - now))
                continue
            try:
                s.sendto(pkt, (self.host, self.port))
                self.sent_bytes += len(pkt)
            except OSError:
                pass
            n += 1
        s.close()




class LegRun:
    """One direction of a test: the run row, the tester that runs it (here or on another Pi) and its thread."""

    def __init__(self, idx: int, rid: int, profile: dict, tester, leg: Optional[PL.Leg]):
        self.idx, self.rid, self.profile, self.tester, self.leg = idx, rid, profile, tester, leg
        self.thread: Optional[threading.Thread] = None
        self.state: Optional[dict] = None
        self.status = "running"
        self.summary: Optional[dict] = None
        self.error: Optional[str] = None
        self.done_evt: Optional[dict] = None
        self.done = False                       # the row is final and everything prepared for it is released: a new test may start
        self.recent: deque = deque(maxlen=600)  # this link's newest messages (the live chart of a link picked in the table of links)
        self.since = None                       # the numbers since the last mark (marks.Since / marks.SinceCounters)


class RunManager:
    def __init__(self, store: Store, role=None, gen_node=None, services: Optional[OR.Services] = None):
        self.store = store                       # `role` is accepted and ignored: there are no fixed roles any more
        self.services = services or OR.Services()
        self.gen_node = gen_node or gnode.Node()
        self.lock = threading.Lock()
        self.legs: list = []
        self.sniff = None
        self.profile: Optional[dict] = None
        self.ui_events: deque = deque(maxlen=8000)
        self.recent_msgs: deque = deque(maxlen=1500)
        self.last_state: Optional[dict] = None
        self.last_mb: Optional[dict] = None
        self.last_mbpeer: Optional[dict] = None
        self.netem_iface: Optional[str] = None
        self.stop_by = ""
        self.owner = ""
        self.prepared: list = []
        self.claimed: list = []
        self._fin_lock = threading.Lock()
        self._finalized = True
        self.state = "idle"                      # planning | checking | preparing | running | collecting | done | failed | stopped
        self.marks: list = []                    # flags dropped during the running test: [{"n", "t_s", "name", "at"}]
        self.lamp = health.Lamp()                # tester lamp of the running (or last) test
        self.last_gstate: Optional[dict] = None
        self._t_zero: Optional[float] = None     # monotonic time of "0 s" on the live chart
        self._gen = 0                            # counts tests, so a watcher thread of an older test ends by itself
        leftover = store.get_setting("netem_applied")
        if leftover:                       # app was killed mid-run last time: remove our qdisc
            system.netem_clear(leftover)
            store.set_setting("netem_applied", None)

    # ---- the first direction is "the" run for everything that only knows about one
    @property
    def run_id(self) -> Optional[int]:
        return self.legs[0].rid if self.legs else None

    @property
    def tester(self):
        return self.legs[0].tester if self.legs else None

    @property
    def thread(self):
        return self.legs[0].thread if self.legs else None

    def run_ids(self) -> list:
        return [l.rid for l in self.legs]

    def _path_info(self, profile: dict) -> Optional[dict]:
        """Is the test traffic going over Wi-Fi? (Then the result says so; with power saving on, idle-gap results are skewed by the Pi itself.)"""
        from .gen.run import api_host
        net = profile.get("net") or {}
        host = net.get("peer_host") or (api_host((profile.get("gen") or {}).get("peer_api", "")) if profile.get("kind") == "generator" else "")
        if profile.get("kind") == "sniffer" or profile.get("transport") in ("serial", "goose_l2", "goose_l3") or not host:
            return None
        w = system.wifi_path(host)
        if w and w.get("power_save") and self.store.get_setting("wifi_powersave_off", False):
            system.wifi_powersave_off(w["interface"])                       # the user asked to keep it off: it is switched back after a reconnect or reboot
            w = system.wifi_path(host)
        return w

    def busy(self) -> bool:
        return any(l.thread is not None and l.thread.is_alive() and not l.done for l in self.legs)

    def busy_except_capture(self) -> bool:
        """Like busy(), but a running capture does not count: it only listens, so this Pi can still be the far end of another Pi's generator test
        (real Pis 2026-11-13: "pi2 is already running a test" stopped the natural check - sniff on pi2 while pi1 generates)."""
        return any(l.thread is not None and l.thread.is_alive() and not l.done and l.profile.get("kind") != "sniffer" for l in self.legs)

    # ------------------------------------------------------------ start
    def start(self, profile: dict, session_id: Optional[int] = None, controller: str = "") -> int:
        with self.lock:
            if self.busy():
                raise Busy("A test is already running.")
            self.state = "planning"
            profile = P.validate_profile(profile)
            try:
                if profile["kind"] == "trip":
                    return self._start_trip(profile, session_id, controller)
                return self._start_single(profile, session_id, controller)
            except Exception:
                self.state = "failed"
                raise

    def _start_single(self, profile: dict, session_id, controller: str) -> int:
        """Generator and sniffer tests: one run, no reflecting end to prepare."""
        is_gen = profile["kind"] == "generator"
        is_sniff = profile["kind"] == "sniffer"
        if profile.get("impairment"):                     # found on the real Pis: a generator profile with a preset ran perfectly clean and said nothing
            raise factory.ConfigError(f"'Simulate a bad link' ({profile['impairment']}) works in trip tests only; this {'generator' if is_gen else 'capture'} test would run without it. "
                                      "Remove it from the test, or run the bad link from a trip test.")
        if is_gen and self.gen_node.busy():               # checked before the test is recorded: a refused start must leave no row behind
            raise Busy("A generator test is already running on this Pi.")
        sid = session_id or self.store.current_session()["id"]
        me = self.services.me()
        rid = self.store.create_run(sid, profile, me["name"], profile["gen"]["peer_api"] if is_gen else profile["sniff"]["mode"])
        self._begin(profile)
        leg = LegRun(0, rid, profile, None, None)
        self.legs = [leg]

        def build(emit, on_message, on_interval):
            if is_sniff:
                t = ssession.SnifferRun(profile, os.path.join(self.store.data_dir, "captures", f"run-{rid}"), emit)
                self.sniff = t
                return t
            return grun.GeneratorRun(profile, self.gen_node, emit, on_interval, services=self.services)
        self._launch(leg, build, netem=None, controller=controller)
        self._watch()
        return rid

    def _begin(self, profile: dict) -> None:
        self.profile = profile
        self.ui_events.clear()
        self.recent_msgs.clear()
        self.last_state = self.last_mb = self.last_mbpeer = None
        self.sniff = None
        self.stop_by = ""
        self.prepared, self.claimed = [], []
        self._finalized = False
        self.marks = []
        self.lamp = health.Lamp()
        self.last_gstate = None
        self._t_zero = None
        self._gen += 1

    def _start_trip(self, profile: dict, session_id, controller: str) -> int:
        svc = self.services
        legs = PL.plan_trip(profile, svc.context())
        self.state = "checking"
        OR.check_nodes(svc, legs)
        legs = PL.plan_trip(profile, svc.context())          # again: asking the Pis may have corrected an address in the list
        OR.check_clocks(svc, legs, profile)
        if profile["impairment"]:
            ok = system.can_netem()
            if not ok["ok"]:
                raise factory.ConfigError("Impairment needs extra permission: " + ok["reason"])
        # local senders: open the transport now, so a bad port / address is refused before anything is recorded
        local: dict = {}
        claimed: list = []
        owner = OR.new_owner(svc)
        try:
            for l in legs:
                if not l.tester.is_self:
                    continue
                p = l.profile
                is_mb = mbrun.is_mirrored(p)
                tr = None if is_mb else factory.make_transport(p, "tester")
                link = mbrun.make_link(p, "A") if is_mb else None
                if svc.claims is not None and p["transport"] in ("serial", "goose_l2"):
                    key = ("serial:" + factory.resolve_serial_port(p["serial"]["port"])) if p["transport"] == "serial" else f"iface:{p['goose']['interface']}"
                    try:
                        svc.claims.claim(key, owner, key.split(":", 1)[1])
                    except PortBusy as e:
                        raise PL.PlanError(str(e))
                    claimed.append(key)
                local[l.index] = (tr, link)
            self.state = "preparing"
            prepared = OR.prepare_reflectors(svc, legs, owner)
        except Exception:
            for k in claimed:
                svc.claims.release(k, owner)
            raise
        sid = session_id or self.store.current_session()["id"]
        self._begin(profile)
        self.owner, self.prepared, self.claimed = owner, prepared, claimed
        me = svc.me()
        self.legs = []
        self._finalized = False
        gid = None
        try:
            for l in legs:
                p = l.profile
                info = {"label": l.label, "kind": l.kind, "basis": l.basis, "controller": me["name"], "tester": l.tester.label(), "reflector": l.reflector.label()}
                rid = self.store.create_run(sid, p, l.tester.label(), factory.port_label(p), group_id=gid, leg=info)
                if gid is None and len(legs) > 1:              # the directions of one test share the id of the first one
                    gid = rid
                    self.store.set_group(rid, gid)
                self.legs.append(LegRun(l.index, rid, p, None, l))
            for lr in self.legs:
                l = lr.leg
                if l.tester.is_self:
                    tr, link = local[l.index]

                    def build(emit, on_message, on_interval, p=l.profile, tr=tr, link=link):
                        if link is not None:
                            far = svc.client(l.reflector) if l.reflector.kind == "peer" else None
                            return mbrun.MirroredRun(p, link, emit, on_message, registry=svc.mb, far_client=far, far_label=l.reflector.label(),
                                                     me_label=svc.me()["name"], on_interval=on_interval)
                        return engine.Tester(p, tr, emit, on_message, on_interval)
                    netem = (p_iface(l.profile) if l.profile["impairment"] else None)
                else:
                    client = svc.client(l.tester)

                    def build(emit, on_message, on_interval, p=l.profile, client=client, n=l.tester):
                        return RemoteTester(p, client, n.label(), emit, on_message, on_interval, controller=me["name"])
                    netem = None
                self._launch(lr, build, netem=netem, controller=controller or me["name"], multi=len(legs) > 1)
        except Exception as e:
            for lr in self.legs:                            # whatever already started is stopped; rows that never started are closed
                if lr.thread is not None and lr.thread.is_alive() and lr.tester is not None:
                    lr.tester.stop()
                elif lr.thread is None:
                    self.store.finish_run(lr.rid, "failed", {"error": f"{type(e).__name__}: {e}"})
            self._finalized = True
            self._cleanup()
            raise
        self.state = "running"
        self._watch()
        return self.legs[0].rid

    # ------------------------------------------------------------ one leg
    def _launch(self, leg: LegRun, build, netem: Optional[str], controller: str = "", multi: bool = False) -> None:
        profile, rid, idx = leg.profile, leg.rid, leg.idx
        is_gen = profile["kind"] == "generator"
        is_sniff = profile["kind"] == "sniffer"
        logging.getLogger("tt").info("Test #%s started: %s (%s)%s", rid, profile.get("name"), profile.get("kind", "trip"),
                                     f" [{leg.leg.label}]" if leg.leg else "")
        buf_m, buf_i, last = [], [], [time.monotonic()]

        def flush(force=False):
            if buf_m and (force or len(buf_m) >= 200 or time.monotonic() - last[0] > 1.0):
                rows = buf_m[:]
                del buf_m[:]
                self.store.add_messages(rid, rows)
                last[0] = time.monotonic()
            if buf_i:
                rows = buf_i[:]
                del buf_i[:]
                self.store.add_intervals(rid, rows)

        def on_message(m):
            buf_m.append(m)
            flush()

        def on_interval(r):
            if is_gen:
                self.store.add_gen_intervals(rid, [r])
            else:
                buf_i.append(r)

        def emit(e):
            ev = e.get("ev")
            if ev == "msg":
                leg.recent.append(e)
                if isinstance(leg.since, MK.Since):
                    leg.since.add(e)
            elif ev in ("state", "gstate"):
                if idx == 0 and e.get("elapsed_s") is not None:
                    self._t_zero = time.monotonic() - float(e["elapsed_s"])
                if ev == "gstate":
                    self.last_gstate = e
                if leg.since is not None:
                    e = {**e, "since": leg.since.view() if isinstance(leg.since, MK.Since) else leg.since.view(e)}
            if idx == 0 or not multi:
                if e.get("ev") == "done" and multi:
                    leg.done_evt = e                         # the test is over when every direction is: one "done" for the screen, sent by _finish_leg
                    return
                if multi:
                    e = {**e, "leg": 0}
                self.ui_events.append(e)
                if e["ev"] == "msg":
                    self.recent_msgs.append(e)
                elif e["ev"] == "state":
                    self.last_state = e
                    leg.state = e
                elif e["ev"] == "mb":
                    self.last_mb = e
                elif e["ev"] == "mbpeer":
                    self.last_mbpeer = e
            else:                                            # another direction: only its headline numbers reach the screen
                if e.get("ev") == "done":
                    leg.done_evt = e
                elif e.get("ev") == "state":
                    leg.state = e
                    self.ui_events.append({**e, "ev": "legstate", "leg": idx})

        path = None
        try:
            path = self._path_info(profile)
        except Exception:
            path = None
        try:
            t = build(emit, on_message, on_interval)
        except Exception as e:                             # could not even be set up: record why instead of leaving it "running"
            self.store.finish_run(leg.rid, "failed", {"error": f"{type(e).__name__}: {e}"})
            raise
        leg.tester = t

        def work():
            load = None
            seeded = [False]
            status, summary = "failed", None
            try:
                if netem:
                    seeded[0] = system.netem_apply(netem, P.NETEM_PRESETS[profile["impairment"]]["args"], seed=1)
                    self.netem_iface = netem
                    self.store.set_setting("netem_applied", netem)
                    emit({"ev": "info", "text": f"Impairment '{profile['impairment']}' applied to {netem}"})
                if profile["load_mbps"] > 0 and profile["net"]["peer_host"]:
                    load = LoadGen(profile["net"]["peer_host"], profile["net"]["port"] + 1, profile["load_mbps"])
                    load.start()
                summary = t.run()
                status = t.status
                if is_sniff and t.error and not (summary.get("files")):
                    status = "failed"
                if path and isinstance(summary, dict):
                    summary["path"] = {"kind": "wifi", **path}
                if status == "stopped" and self.stop_by and isinstance(summary, dict):
                    summary["stopped_by"] = self.stop_by
                if load:
                    summary["load_mbps"] = profile["load_mbps"]
                if netem:
                    summary["impairment"] = {"preset": profile["impairment"], "iface": netem,
                                             "args": P.NETEM_PRESETS[profile["impairment"]]["args"],
                                             "repeatable": seeded[0]}
            except Exception as e:  # noqa
                t.error = f"{type(e).__name__}: {e}"
                emit({"ev": "done", "status": "failed", "error": t.error, "summary": None})
            finally:
                if load:
                    load.stop_evt.set()
                if isinstance(summary, dict):
                    try:
                        self._final_health(profile, summary)
                        summary["tester"] = self.lamp.summary((leg.state or self.last_gstate or {}).get("elapsed_s"))
                    except Exception:  # noqa
                        logging.getLogger("tt").exception("tester lamp summary")
                    if self.marks:
                        summary["marks"] = [dict(m) for m in self.marks]
                if netem:
                    system.netem_clear(netem)
                    self.store.set_setting("netem_applied", None)
                    self.netem_iface = None
                flush(True)
                if summary is None:
                    summary = {"error": t.error}
                elif t.error:
                    summary["error"] = t.error
                if isinstance(summary, dict) and profile["kind"] == "trip":
                    summary["endpoints"] = self._endpoints(profile, leg, controller)
                    w = path_warning(profile["net"]["peer_host"]) if profile["transport"] in ("udp", "tcp") else None
                    if w:                                          # e.g. the traffic went over the Wi-Fi access point / the management port
                        summary.setdefault("warnings", []).append(w)
                leg.status, leg.summary, leg.error = status, summary, getattr(t, "error", None)
                last = self._last_leg(leg)
                if last:
                    self._cleanup()                                # what was prepared on the other Pis is released BEFORE the result says "finished"
                self.store.finish_run(rid, status, summary)
                leg.done = True
                logging.getLogger("tt").info("Test #%s %s%s", rid, status, (" - " + t.error) if getattr(t, "error", None) else "")
                if last:
                    self._announce_end(multi)

        leg.thread = threading.Thread(target=work, name=f"tester-run-{idx}", daemon=True)
        leg.thread.start()

    @staticmethod
    def _endpoints(profile: dict, leg: LegRun, controller: str) -> dict:
        li = profile.get("leg_info") or {}
        return {"source": li.get("source"), "dest": li.get("dest"), "source_id": li.get("source_id"), "dest_id": li.get("dest_id"), "kind": li.get("kind", "round_trip"),
                "basis": li.get("basis", "rtt"), "controller": controller, "address": li.get("data_host"), "label": li.get("label")}

    def _last_leg(self, leg: LegRun) -> bool:
        """True for exactly one direction: the last one to end (it frees what the test prepared and announces the end)."""
        with self._fin_lock:
            if self._finalized or any(l.status == "running" and l is not leg for l in self.legs):
                return False
            self._finalized = True
            self.state = "collecting"
            return True

    def _announce_end(self, multi: bool) -> None:
        if multi:
            worst = "failed" if any(l.status == "failed" for l in self.legs) else "stopped" if any(l.status == "stopped" for l in self.legs) else "finished"
            err = next((l.error for l in self.legs if l.error), None)
            self.ui_events.append({"ev": "done", "status": worst, "error": err, "summary": (self.legs[0].done_evt or {}).get("summary"),
                                   "legs": [{"index": l.idx, "run_id": l.rid, "label": l.leg.label if l.leg else "", "status": l.status,
                                             "pass": ((l.summary or {}).get("verdict") or {}).get("pass"), **_leg_numbers(l.summary)} for l in self.legs]})
        self.state = "stopped" if any(l.status == "stopped" for l in self.legs) else "failed" if any(l.status == "failed" for l in self.legs) else "done"

    def _cleanup(self) -> None:
        svc = self.services
        try:
            OR.release_reflectors(svc, self.prepared)
        finally:
            self.prepared = []
            if svc.claims is not None and self.owner:
                svc.claims.release_owner(self.owner)
            self.claimed = []

    # ------------------------------------------------------------ control
    def stop(self, by: str = ""):
        """`by` = who asked (client address): kept in the log and in the result so an unexpected "stopped" can always be traced."""
        if self.legs and self.busy():
            self.stop_by = by or self.stop_by
            if by:
                logging.getLogger("tt").info("Test #%s: stop requested by %s", self.run_id, by)
            for l in self.legs:
                if l.tester is not None and l.thread is not None and l.thread.is_alive():
                    l.tester.stop()

    def wait(self, timeout=None):
        for l in self.legs:
            if l.thread:
                l.thread.join(timeout)

    def status(self) -> dict:
        p = self.profile or {}
        d = {"running": self.busy(), "run_id": self.run_id, "profile_name": p.get("name"), "kind": p.get("kind", "trip"),
             "threshold_ms": p.get("threshold_ms"), "mode": p.get("mode"), "state": self.last_state, "phase": self.state}
        if len(self.legs) > 1:
            d["legs"] = [{"index": l.idx, "run_id": l.rid, "label": l.leg.label if l.leg else "", "running": bool(l.thread and l.thread.is_alive()),
                          "state": l.state, "status": l.status, "tester": l.leg.tester.label() if l.leg else "", "reflector": l.leg.reflector.label() if l.leg else "",
                          "basis": l.leg.basis if l.leg else "rtt"} for l in self.legs]
        if self.legs:
            d["marks"] = list(self.marks)
            d["lamp"] = self.lamp.now
        return d

    # ------------------------------------------------------------ mark (docs/SPEC.md 7.5)
    def mark(self, name: str = "") -> dict:
        """Drop a flag on the running test: shown on the charts, kept with the result; the "since mark" numbers start again from it."""
        if not self.busy():
            raise Busy("No test is running.")
        if len(self.marks) >= MK.MAX_MARKS:
            raise ValueError(f"A test can carry at most {MK.MAX_MARKS} marks.")
        t_s = round(max(0.0, time.monotonic() - self._t_zero), 1) if self._t_zero is not None else 0.0
        m = {"n": len(self.marks) + 1, "t_s": t_s, "name": MK.clean_name(name), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.marks.append(m)
        for l in self.legs:
            p = l.profile
            if p.get("kind", "trip") == "trip" and not mbrun.is_mirrored(p):
                l.since = MK.Since(t_s)
            else:
                l.since = MK.SinceCounters(t_s, self.last_gstate if p.get("kind") == "generator" else l.state)
        self.ui_events.append({"ev": "mark", **m})
        logging.getLogger("tt").info("Test #%s: mark %d at %.1f s%s", self.run_id, m["n"], t_s, f" ({m['name']})" if m["name"] else "")
        return m

    def leg_live(self, rid: int) -> Optional[dict]:
        """The newest messages and numbers of one link of the running test (None = not a link of it)."""
        for l in self.legs:
            if l.rid == rid:
                first = l.idx == 0
                return {"recent": list(self.recent_msgs if first else l.recent), "state": l.state if l.state is not None else (self.last_state if first else None),
                        "mb": self.last_mb if first else None, "mbpeer": self.last_mbpeer if first else None,
                        "running": self.busy(), "marks": list(self.marks), "lamp": self.lamp.now}
        return None

    # ------------------------------------------------------------ tester lamp (docs/SPEC.md 7.6)
    def health_inputs(self) -> list:
        """What may be wrong with THIS Pi right now, as tester-lamp reasons (also served to the Pi that controls a test this Pi takes part in)."""
        me = self.services.me()["name"]
        base = {"pi": me, "cpu": health.cpu_percent()}
        if not self.busy():
            return health.assess(base)
        groups = []
        extra = self._lamp_static()
        for l in self.legs:
            if l.leg is not None and not l.leg.tester.is_self:
                continue                                    # that Pi reports its own
            p, st = l.profile, l.state or {}
            i = dict(base, **extra)
            kind = p.get("kind", "trip")
            if kind == "trip" and not mbrun.is_mirrored(p):
                prev = getattr(l, "_h_prev", None)
                now = st.get("elapsed_s")                   # the test's own clock of this state: the count and the time come from the same moment
                now = float(now) if now is not None else time.monotonic()
                if st.get("phase") == "steady" and st.get("sent") is not None:
                    asked = 1000.0 / float(p["interval_ms"])
                    total = st.get("total_s")
                    near_end = total is not None and st.get("elapsed_s") is not None and float(st["elapsed_s"]) > float(total) - 1.5   # the last second sends less by design
                    if prev and prev[2] == "steady" and not near_end and now - prev[1] >= 0.9 and asked * (now - prev[1]) >= 8:
                        i.update(asked_pps=asked, sent_pps=(st["sent"] - prev[0]) / (now - prev[1]), unit="messages/s")
                    if st.get("tx_late_ms") is not None:
                        i.update(late_ms=st["tx_late_ms"], interval_ms=p["interval_ms"])
                l._h_prev = (st.get("sent") or 0, now, st.get("phase"))
                if (l.leg is not None and l.leg.basis == "up") or p.get("one_way"):
                    i.update(one_way=True, clock_offset_ms=system.clock_state().get("offset_ms"), max_clock_offset_ms=p.get("max_clock_offset_ms"))
            elif kind == "generator":
                g = self.last_gstate or {}
                asked = _gen_asked_pps(p)
                if asked and g.get("tx_pps") is not None and 2.0 <= (g.get("elapsed_s") or 0) <= (g.get("total_s") or 0) - 1.5:
                    i.update(asked_pps=asked, sent_pps=g["tx_pps"], unit="packets/s")
            groups.append(health.assess(i))
        return health.merge(groups) if groups else health.assess(base)

    def _lamp_static(self) -> dict:
        """Things that do not change during a test (worked out once per test): Wi-Fi power saving on the test's path, the USB adapter's hold time."""
        key = self._gen
        if getattr(self, "_lamp_static_for", None) == key:
            return self._lamp_static_val
        out: dict = {}
        try:
            for l in self.legs:
                if l.leg is not None and not l.leg.tester.is_self:
                    continue
                p = l.profile
                w = self._path_info(p)
                if w and w.get("power_save"):
                    out.update(wifi_power_save=True, wifi_iface=w.get("interface"))
                if p.get("transport") == "serial":
                    lt = system.ftdi_latency(factory.resolve_serial_port(p["serial"]["port"]))
                    if lt is not None:
                        out["ftdi_latency_ms"] = lt
        except Exception:  # noqa
            pass
        self._lamp_static_for, self._lamp_static_val = key, out
        return out

    def _far_nodes(self) -> list:
        seen, out = set(), []
        for l in self.legs:
            if l.leg is None:
                continue
            for n in (l.leg.tester, l.leg.reflector):
                if n.kind == "peer" and n.id not in seen:
                    seen.add(n.id)
                    out.append(n)
        return out

    def _watch(self) -> None:
        gen = self._gen
        lamp = self.lamp

        def loop():
            far: dict = {}
            tick = 0
            health.cpu_percent()
            while gen == self._gen and not self._finalized:
                time.sleep(1.0)
                if gen != self._gen or self._finalized:
                    break
                tick += 1
                try:
                    groups = [self.health_inputs()]
                    if tick % 3 == 1:
                        for n in self._far_nodes():
                            try:
                                r = self.services.client(n).get("/api/peer/health", timeout=2)
                                far[n.id] = (time.monotonic(), (r or {}).get("reasons") or [])
                            except Exception:  # noqa  (an older app version has no such page: that Pi is simply not reported on)
                                pass
                    groups += [rs for at, rs in far.values() if time.monotonic() - at < 10]
                    t_s = (time.monotonic() - self._t_zero) if self._t_zero is not None else 0.0
                    changed = lamp.update(t_s, health.merge(groups))
                    if changed is not None:
                        self.ui_events.append({"ev": "lamp", **changed})
                except Exception:  # noqa
                    logging.getLogger("tt").exception("tester lamp")
        threading.Thread(target=loop, name="tester-lamp", daemon=True).start()

    def _final_health(self, profile: dict, summary: dict) -> None:
        """What only the finished test knows: a generator stream whose Pi could not send at the asked rate; a capture that lost packets."""
        if profile.get("kind") == "sniffer":               # real Pis 2026-11-13: 9 % of the packets of a 450 Mb/s SPAN capture were lost and the lamp said "Pi OK the whole run"
            drops = int(((summary.get("engine") or {}).get("kernel_dropped")) or 0)
            if drops > 0:
                rs = health.assess({"pi": self.services.me()["name"], "rx_drops": drops})
                self.lamp.update(float(summary.get("elapsed_s") or 0), health.merge([rs]))
            return
        if profile.get("kind") != "generator":
            return
        rs = []
        for s in summary.get("streams") or []:
            tx = s.get("tx") or {}
            if tx.get("target_pps") and tx.get("pps") is not None and not s.get("packets"):
                rs += health.assess({"pi": s.get("src") or self.services.me()["name"], "asked_pps": tx["target_pps"], "sent_pps": tx["pps"], "unit": "packets/s"})
        if rs:
            t_s = float((self.last_gstate or {}).get("elapsed_s") or 0)
            self.lamp.update(t_s, health.merge([rs]))


def _leg_numbers(summary: Optional[dict]) -> dict:
    s = summary or {}
    r = s.get("rtt_ms") or {}
    basis = (s.get("leg") or {}).get("basis") or (s.get("endpoints") or {}).get("basis")
    if basis == "up" and isinstance(s.get("up_ms"), dict):
        r = s["up_ms"]
    return {"p99_ms": r.get("p99"), "p50_ms": r.get("p50"), "max_ms": r.get("max"), "sent": s.get("sent"), "lost": s.get("lost"), "late": s.get("late")}


def _gen_asked_pps(profile: dict) -> float:
    """Packets per second a steady generator test asks of its senders in total (0 = it varies, so the lamp does not judge the rate)."""
    g = profile.get("gen") or {}
    if (g.get("steps") or {}).get("enabled"):
        return 0.0
    total = 0.0
    for s in g.get("streams") or []:
        if s.get("burst_gap_ms") or s.get("start_offset_s") or s.get("duration_s") or s.get("packets"):
            return 0.0
        try:
            total += gnode.stream_pps(s)[1]
        except Exception:  # noqa
            return 0.0
    return total


def path_warning(host: str) -> Optional[str]:
    """A plain warning when the traffic to `host` leaves through the Wi-Fi access point / management port (the network settings feature)."""
    try:
        from .netcfg import path_warning as pw
        return pw(host)
    except ImportError:
        return None
    except Exception:
        return None


def p_iface(profile: dict) -> str:
    """The port an impairment is applied to: the one chosen in the test, else the one the traffic to the other Pi leaves through."""
    if profile.get("impair_iface"):
        return profile["impair_iface"]
    host = profile["net"]["peer_host"]
    if host.startswith("127."):
        return "lo"
    return next((i["name"] for i in system.interfaces() if i["wired"]), "eth0")
