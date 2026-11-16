"""Scheduled repeats: run the same test again and again ("every 6 h for 7 days", or "at 08:00, 12:00, 17:30, 22:00 every day incl. weekends"), so a
result covers busy hours and weekends, not one quiet window.  The schedules live in the app's settings table, so they survive a reboot; a missed
slot (the Pi was off, or the last run was still going) is counted, never silently run late.  One combined summary across the runs of a schedule.

Pure scheduling maths (occurrences) + a small thread (RepeatManager.tick) that asks the run manager to start the test."""
from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

from . import radio as RADIO

MAX_OCCURRENCES = 500
GRACE_MAX_S = 30 * 60                       # a slot more than this late (or the Pi was busy that long) is missed, not run late
DAYS = {"all": "every day, including weekends", "weekdays": "weekdays only", "weekend": "weekends only"}
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class BadSchedule(ValueError):
    pass


def validate_schedule(s: dict) -> dict:
    """Return a clean schedule dict or raise BadSchedule with a plain message."""
    if not isinstance(s, dict):
        raise BadSchedule("Give a schedule.")
    kind = s.get("kind")
    try:
        for_days = float(s.get("for_days", 7))
    except (TypeError, ValueError):
        raise BadSchedule("'For how many days' must be a number.")
    if not 0.04 <= for_days <= 90:
        raise BadSchedule("A schedule can run for 1 hour to 90 days.")
    out = {"kind": kind, "for_days": for_days}
    if kind == "every":
        try:
            every_h = float(s.get("every_h"))
        except (TypeError, ValueError):
            raise BadSchedule("'Every how many hours' must be a number.")
        if not 0.25 <= every_h <= 24 * 30:
            raise BadSchedule("Repeat every 15 minutes to 30 days.")
        out["every_h"] = every_h
    elif kind == "times":
        raw = s.get("times")
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[,\s;]+", raw) if x]
        if not isinstance(raw, list) or not raw:
            raise BadSchedule("Give at least one time of day, like 08:00.")
        times = []
        for t in raw:
            m = _HHMM.match(str(t).strip())
            if not m:
                raise BadSchedule(f"'{t}' is not a time of day like 08:00 or 17:30.")
            times.append(f"{int(m.group(1)):02d}:{m.group(2)}")
        out["times"] = sorted(set(times))
        out["days"] = s.get("days", "all")
        if out["days"] not in DAYS:
            raise BadSchedule("Days must be all, weekdays or weekend.")
    else:
        raise BadSchedule("Pick 'every N hours' or 'at these times each day'.")
    return out


def occurrences(sch: dict, start_at: float) -> List[float]:
    """Every start time of a schedule (epoch seconds), oldest first, capped at MAX_OCCURRENCES.  Local time, so '08:00' is 08:00 on the Pi's clock."""
    end = start_at + sch["for_days"] * 86400
    out: List[float] = []
    if sch["kind"] == "every":
        step = sch["every_h"] * 3600
        k = 0
        while start_at + k * step <= end and len(out) < MAX_OCCURRENCES:
            out.append(start_at + k * step)
            k += 1
        return out
    d = dt.datetime.fromtimestamp(start_at).date()
    last = dt.datetime.fromtimestamp(end).date()
    while d <= last and len(out) < MAX_OCCURRENCES:
        wk = d.weekday() >= 5
        if sch["days"] == "all" or (sch["days"] == "weekend") == wk:
            for t in sch["times"]:
                h, m = int(t[:2]), int(t[3:])
                ts = time.mktime(dt.datetime(d.year, d.month, d.day, h, m).timetuple())
                if start_at - 1 <= ts <= end:
                    out.append(ts)
        d += dt.timedelta(days=1)
    return sorted(out)


def describe(sch: dict) -> str:
    if sch["kind"] == "every":
        h = sch["every_h"]
        return f"every {h:g} h for {sch['for_days']:g} days"
    return f"at {', '.join(sch['times'])} ({DAYS[sch['days']]}) for {sch['for_days']:g} days"


class RepeatManager:
    """Holds the schedules, starts the test when a slot is due.  `start_fn(profile) -> run id`, `busy_fn() -> bool`."""

    def __init__(self, store, start_fn: Callable[[dict], int], busy_fn: Callable[[], bool], now: Callable[[], float] = time.time):
        self.store, self.start_fn, self.busy_fn, self.now = store, start_fn, busy_fn, now
        self.lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---- persistence
    def _all(self) -> List[dict]:
        return list(self.store.get_setting("repeats", []) or [])

    def _save(self, specs: List[dict]) -> None:
        self.store.set_setting("repeats", specs)

    def list(self) -> List[dict]:
        with self.lock:
            return [self._view(s) for s in self._all()]

    def get(self, rid: str) -> Optional[dict]:
        with self.lock:
            return next((s for s in self._all() if s["id"] == rid), None)

    def _view(self, s: dict) -> dict:
        occ = occurrences(s["schedule"], s["start_at"])
        nxt = next((t for t in occ if t > s.get("last_handled", 0)), None)
        return {**{k: v for k, v in s.items() if k != "profile"}, "profile_name": s["profile"].get("name"), "text": describe(s["schedule"]), "total_slots": len(occ),
                "next_at": nxt, "next_local": time.strftime("%a %Y-%m-%d %H:%M", time.localtime(nxt)) if nxt else None,
                "status": "stopped" if s.get("stopped") else ("done" if nxt is None else "waiting")}

    def create(self, profile: dict, schedule: dict, start_at: Optional[float] = None, name: str = "") -> dict:
        sch = validate_schedule(schedule)
        now = self.now()
        start_at = float(start_at) if start_at else now
        if not occurrences(sch, start_at):
            raise BadSchedule("That schedule never starts a run: check the times and the number of days.")
        spec = {"id": uuid.uuid4().hex[:8], "name": (name or profile.get("name") or "Repeated test")[:80], "profile": profile, "schedule": sch, "start_at": start_at,
                "created": now, "last_handled": start_at - 1, "runs": [], "missed": 0, "errors": [], "stopped": False}
        with self.lock:
            self._save(self._all() + [spec])
        return self._view(spec)

    def stop(self, rid: str) -> bool:
        with self.lock:
            specs = self._all()
            for s in specs:
                if s["id"] == rid:
                    s["stopped"] = True
                    self._save(specs)
                    return True
        return False

    def delete(self, rid: str) -> bool:
        with self.lock:
            specs = self._all()
            keep = [s for s in specs if s["id"] != rid]
            self._save(keep)
            return len(keep) != len(specs)

    # ---- the clock
    def tick(self) -> int:
        """Start what is due.  Returns the number of runs started."""
        started = 0
        with self.lock:
            specs = self._all()
            changed = False
            now = self.now()
            for s in specs:
                if s.get("stopped"):
                    continue
                occ = occurrences(s["schedule"], s["start_at"])
                period = min((b - a for a, b in zip(occ, occ[1:])), default=GRACE_MAX_S * 2)
                grace = min(GRACE_MAX_S, period / 2)
                for t in [x for x in occ if x > s.get("last_handled", 0) and x <= now]:
                    if now - t > grace:                               # too late to be the slot it was meant to be
                        s["missed"] += 1
                        s["last_handled"] = t
                        changed = True
                        continue
                    if self.busy_fn():
                        break                                         # try again on the next tick, until the grace runs out
                    try:
                        rid = self.start_fn(s["profile"])
                        s["runs"].append(rid)
                        started += 1
                    except Exception as e:  # noqa
                        s["errors"] = (s.get("errors") or [])[-9:] + [f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(t))}: {str(e)[:160]}"]
                        s["missed"] += 1
                        logging.getLogger("tt").warning("Scheduled repeat %s could not start: %s", s["id"], e)
                    s["last_handled"] = t
                    changed = True
                    break                                             # one run at a time
            if changed:
                self._save(specs)
        return started

    def start(self, every_s: float = 15.0) -> None:
        if self._thread:
            return

        def loop():
            while not self._stop.wait(every_s):
                try:
                    self.tick()
                except Exception:  # noqa
                    logging.getLogger("tt").exception("repeat tick")

        self._thread = threading.Thread(target=loop, name="repeats", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    # ---- the combined summary
    def combined(self, rid: str) -> Optional[dict]:
        s = self.get(rid)
        if not s:
            return None
        runs = [self.store.get_run(i) for i in s["runs"]]
        return combine(self._view(s), [r for r in runs if r])


def combine(spec: dict, runs: List[dict]) -> dict:
    """One summary across the runs of a schedule.  Percentiles of separate runs cannot be merged exactly, so the figures are the WORST of any run
    (the safe reading), and the table says so."""
    rows, tags, hod = [], {}, {}
    cov = {"busy_7_9am": False, "busy_4_7pm": False, "weekend": False}
    for r in runs:
        sm = r.get("summary") or {}
        rt = sm.get("rtt_ms") or {}
        w = sm.get("when") or {}
        done = r.get("status") in ("finished", "stopped") and "error" not in sm
        row = {"run_id": r["id"], "status": r.get("status"), "start": w.get("start_local") or r.get("started_at"), "end": w.get("end_local"), "tag": w.get("tag") or "-",
               "hour": int(time.strftime("%H", time.localtime(w["start_epoch"]))) if w.get("start_epoch") else None,
               "verdict": ("PASS" if (sm.get("verdict") or {}).get("pass") else "FAIL") if done and sm.get("verdict") else None,
               "sent": sm.get("sent"), "lost": sm.get("lost"), "loss_pct": sm.get("loss_pct"), "p50": rt.get("p50"), "p99": rt.get("p99"), "p999": rt.get("p999"), "max": rt.get("max"),
               "bundling": bool(((sm.get("mirrored") or {}).get("packetization") or {}).get("suspected"))}
        rows.append(row)
        for k, src in (("busy_7_9am", "in_7_9am"), ("busy_4_7pm", "in_4_7pm"), ("weekend", "weekend")):   # a run that ran inside a busy window counts, even a short one
            cov[k] = cov[k] or bool(w.get(src))
        for key, bucket in ((row["tag"], tags), (row["hour"], hod)):
            if key is None or key == "-":
                continue
            b = bucket.setdefault(key, {"runs": 0, "fails": 0, "sent": 0, "lost": 0, "worst_p99": None, "worst_p999": None, "worst_max": None})
            b["runs"] += 1
            b["fails"] += 1 if row["verdict"] == "FAIL" else 0
            b["sent"] += row["sent"] or 0
            b["lost"] += row["lost"] or 0
            for fld, src in (("worst_p99", "p99"), ("worst_p999", "p999"), ("worst_max", "max")):
                if row[src] is not None and (b[fld] is None or row[src] > b[fld]):
                    b[fld] = row[src]
    ok = [x for x in rows if x["verdict"]]
    worst = lambda k: max((x[k] for x in ok if x[k] is not None), default=None)
    totals = {"runs": len(rows), "judged": len(ok), "passed": sum(1 for x in ok if x["verdict"] == "PASS"), "failed": sum(1 for x in ok if x["verdict"] == "FAIL"),
              "missed_slots": spec.get("missed", 0), "frames_sent": sum(x["sent"] or 0 for x in ok), "frames_lost": sum(x["lost"] or 0 for x in ok),
              "worst_p99": worst("p99"), "worst_p999": worst("p999"), "worst_max": worst("max"), "bundling_runs": sum(1 for x in rows if x["bundling"])}
    missing = [n for n, k in (("weekday 7-9 am", "busy_7_9am"), ("weekday 4-7 pm", "busy_4_7pm"), ("a weekend", "weekend")) if not cov[k]]
    note = ("All busy windows and a weekend are covered." if not missing else "Not covered yet: " + ", ".join(missing) + ".")
    return {"id": spec["id"], "name": spec["name"], "schedule_text": spec["text"], "status": spec.get("status"), "totals": totals, "rows": rows,
            "by_tag": [{"tag": k, **v} for k, v in sorted(tags.items())], "by_start_hour": [{"hour": k, **v} for k, v in sorted(hod.items())], "coverage": cov, "coverage_note": note,
            "errors": spec.get("errors") or [], "basis": "Percentiles are the worst value of any single run (separate runs cannot be merged exactly)."}
