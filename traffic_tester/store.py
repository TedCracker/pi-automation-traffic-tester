"""SQLite storage: profiles, sessions, runs, messages, intervals, settings (docs/SPEC.md section 7)."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import shutil
import sqlite3
import threading
import zipfile
from typing import Any, Dict, List, Optional

from . import marks as MK
from . import profiles as P

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, builtin INTEGER NOT NULL DEFAULT 0,
  json TEXT NOT NULL, created_at TEXT, updated_at TEXT, preset_key TEXT, position REAL, hidden INTEGER NOT NULL DEFAULT 0, override_json TEXT);
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT, notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, profile_json TEXT, role TEXT,
  transport TEXT, port TEXT, started_at TEXT, ended_at TEXT, status TEXT, summary_json TEXT, verdict TEXT, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS messages(run_id INTEGER NOT NULL, seq INTEGER, phase TEXT, idle_gap_s REAL, t_send_ms REAL,
  rtt_us INTEGER, up_us INTEGER, down_us INTEGER, status TEXT);
CREATE INDEX IF NOT EXISTS messages_run ON messages(run_id);
CREATE TABLE IF NOT EXISTS intervals(run_id INTEGER NOT NULL, t_offset_s REAL, sent INTEGER, received INTEGER, lost INTEGER,
  late INTEGER, p50_us INTEGER, p95_us INTEGER, p99_us INTEGER, max_us INTEGER);
CREATE INDEX IF NOT EXISTS intervals_run ON intervals(run_id);
CREATE TABLE IF NOT EXISTS gen_intervals(run_id INTEGER NOT NULL, t_offset_s REAL, stream INTEGER, tx_pkts INTEGER, tx_bytes INTEGER,
  rx_pkts INTEGER, rx_bytes INTEGER, lost_cum INTEGER, lat_avg_us INTEGER, lat_p95_us INTEGER, lat_max_us INTEGER, jitter_us REAL, rtt_avg_us INTEGER);
CREATE INDEX IF NOT EXISTS gen_intervals_run ON gen_intervals(run_id);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class BuiltinError(Exception):
    pass


class Store:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "traffic-tester.db")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.last_import: dict = {}
        self.db.executescript(SCHEMA)
        self._migrate()
        self.seed_builtins()
        if not self.db.execute("SELECT 1 FROM sessions").fetchone():
            self.new_session("First session")
        # runs left 'running' by a crash/power loss
        self.db.execute("UPDATE runs SET status='interrupted', ended_at=? WHERE status='running'", (now(),))

    # ---- settings
    def get_setting(self, key, default=None):
        with self.lock:
            r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_setting(self, key, value):
        with self.lock:
            self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value)))

    # ---- profiles
    # A built-in test ("preset") is a row with a stable preset_key (its ORIGINAL name). The defaults live in code (profiles.builtin_presets);
    # what the user changed is stored as `override_json` (only the differing fields), so an app update never wipes an edit and a reset always works.
    # `hidden` takes a test off the Setup list; `position` is the one order of the Setup list (built-in and own tests together).
    def _migrate(self):
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(profiles)")}
        for c, ddl in (("preset_key", "TEXT"), ("position", "REAL"), ("hidden", "INTEGER NOT NULL DEFAULT 0"), ("override_json", "TEXT")):
            if c not in cols:
                self.db.execute(f"ALTER TABLE profiles ADD COLUMN {c} {ddl}")
        rcols = {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}
        for c, ddl in (("group_id", "INTEGER"), ("leg", "TEXT")):        # tests with two directions at once: one run per direction, tied together by group_id
            if c not in rcols:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {c} {ddl}")
        if "job" not in rcols:                                           # job info of a run: {"site", "link", "by", "note"} as JSON (SPEC 7.4)
            self.db.execute("ALTER TABLE runs ADD COLUMN job TEXT")

    def _next_position(self) -> float:
        r = self.db.execute("SELECT MAX(position) AS m FROM profiles").fetchone()
        return (r["m"] if r and r["m"] is not None else -1) + 1

    @staticmethod
    def _effective(default: dict, override_json: Optional[str]):
        """(effective profile, error text or None). A stored override that no longer validates (schema moved on) is kept but not applied."""
        ov = json.loads(override_json) if override_json else None
        if not ov:
            return default, None
        try:
            return P.validate_profile({**P.merge_profile(default, ov), "builtin": True}), None
        except ValueError as e:
            return default, str(e)

    def seed_builtins(self):
        with self.lock:
            defaults = P.builtin_presets()
            prev_pos: Optional[float] = None
            for idx, d in enumerate(defaults):
                key = P.preset_key(d["name"])
                row = self.db.execute("SELECT * FROM profiles WHERE preset_key=?", (key,)).fetchone()
                if row is None:       # rows written before test editing existed: adopt by the default name
                    row = self.db.execute("SELECT * FROM profiles WHERE name=? AND builtin=1 AND preset_key IS NULL", (d["name"],)).fetchone()
                if row:
                    eff, _ = self._effective(d, row["override_json"])
                    name = eff["name"]
                    if self.db.execute("SELECT 1 FROM profiles WHERE name=? AND id<>?", (name, row["id"])).fetchone():
                        name = row["name"]                       # another test took that name: keep the one the row has
                        eff = {**eff, "name": name}
                    self.db.execute("UPDATE profiles SET json=?, builtin=1, name=?, preset_key=?, updated_at=? WHERE id=?",
                                    (json.dumps(eff), name, key, now(), row["id"]))
                    if row["position"] is None:
                        self.db.execute("UPDATE profiles SET position=? WHERE id=?", (float(idx), row["id"]))
                        prev_pos = float(idx)
                    else:
                        prev_pos = row["position"]
                else:
                    clash = self.db.execute("SELECT id FROM profiles WHERE name=?", (d["name"],)).fetchone()
                    if clash:                                     # one of the user's own tests already has this name: it gets a new one
                        newn = self._unique_name(f"{d['name']} (mine)")
                        cj = json.loads(self.db.execute("SELECT json FROM profiles WHERE id=?", (clash["id"],)).fetchone()["json"])
                        cj["name"] = newn
                        self.db.execute("UPDATE profiles SET name=?, json=? WHERE id=?", (newn, json.dumps(cj), clash["id"]))
                    if prev_pos is None:
                        r = self.db.execute("SELECT MIN(position) AS m FROM profiles").fetchone()
                        pos = float(idx) if r["m"] is None else min(float(idx), r["m"] - 1)
                    else:
                        nxt = self.db.execute("SELECT MIN(position) AS m FROM profiles WHERE position>?", (prev_pos,)).fetchone()["m"]
                        pos = prev_pos + 1 if nxt is None else (prev_pos + nxt) / 2
                    self.db.execute("INSERT INTO profiles(name,builtin,json,created_at,updated_at,preset_key,position) VALUES(?,1,?,?,?,?,?)",
                                    (d["name"], json.dumps(d), now(), now(), key, pos))
                    prev_pos = pos
            # own tests saved before positions existed go after everything else, in the order they were made
            for r in self.db.execute("SELECT id FROM profiles WHERE position IS NULL ORDER BY id").fetchall():
                self.db.execute("UPDATE profiles SET position=? WHERE id=?", (self._next_position(), r["id"]))
            self._renumber()

    def _renumber(self):
        rows = self.db.execute("SELECT id, position FROM profiles ORDER BY position, id").fetchall()
        for i, r in enumerate(rows):
            if r["position"] != float(i):
                self.db.execute("UPDATE profiles SET position=? WHERE id=?", (float(i), r["id"]))

    def _defaults_by_key(self) -> Dict[str, dict]:
        return {P.preset_key(d["name"]): d for d in P.builtin_presets()}

    def _prof(self, r, defaults=None) -> dict:
        d = json.loads(r["json"])
        d["id"] = r["id"]
        d["builtin"] = bool(r["builtin"])
        d["hidden"] = bool(r["hidden"])
        d["position"] = r["position"]
        if r["builtin"]:
            defaults = defaults if defaults is not None else self._defaults_by_key()
            dflt = defaults.get(r["preset_key"])
            d["preset_key"] = r["preset_key"]
            d["default_name"] = dflt["name"] if dflt else d["name"]
            d["edited"] = bool(r["override_json"])
            if dflt and r["override_json"]:
                _, err = self._effective(dflt, r["override_json"])
                if err:
                    d["override_error"] = err
        return d

    def list_profiles(self, include_hidden: bool = True) -> List[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM profiles ORDER BY position, id").fetchall()
        dflt = self._defaults_by_key()
        out = [self._prof(r, dflt) for r in rows]
        return out if include_hidden else [p for p in out if not p["hidden"]]

    def get_profile(self, pid: int) -> Optional[dict]:
        with self.lock:
            r = self.db.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
        return self._prof(r) if r else None

    def _unique_name(self, name: str) -> str:
        n, i = name, 1
        while self.db.execute("SELECT 1 FROM profiles WHERE name=?", (n,)).fetchone():
            i += 1
            n = f"{name} ({i})"
        return n

    def create_profile(self, data: dict, unique: bool = False, position: Optional[float] = None) -> dict:
        p = P.validate_profile({**data, "builtin": False})
        with self.lock:
            if unique:
                p["name"] = self._unique_name(p["name"])
            elif self.db.execute("SELECT 1 FROM profiles WHERE name=?", (p["name"],)).fetchone():
                raise ValueError(f"A test named '{p['name']}' already exists. Pick another name.")
            cur = self.db.execute("INSERT INTO profiles(name,builtin,json,created_at,updated_at,position) VALUES(?,0,?,?,?,?)",
                                  (p["name"], json.dumps(p), now(), now(), self._next_position() if position is None else position))
            if position is not None:
                self._renumber()
        return self.get_profile(cur.lastrowid)

    def update_profile(self, pid: int, data: dict) -> dict:
        """Own tests: replaced. Built-in TRIP tests: the changes are stored as an override of the default (Reset to default undoes them)."""
        with self.lock:
            row = self.db.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
            if not row:
                raise KeyError(pid)
            if row["builtin"]:
                dflt = self._defaults_by_key().get(row["preset_key"])
                if not dflt or dflt.get("kind", "trip") != "trip":
                    raise BuiltinError("This built-in preset is read-only. Use 'Duplicate to edit'.")
                p = P.validate_profile({**data, "builtin": True})
                if p.get("kind", "trip") != "trip":
                    raise ValueError("A built-in test stays a trip test.")
                clash = self.db.execute("SELECT id FROM profiles WHERE name=? AND id<>?", (p["name"], pid)).fetchone()
                if clash:
                    raise ValueError(f"A test named '{p['name']}' already exists. Pick another name.")
                ov = P.diff_profile(dflt, p)
                self.db.execute("UPDATE profiles SET name=?, json=?, override_json=?, updated_at=? WHERE id=?",
                                (p["name"], json.dumps(P.merge_profile(dflt, ov) if ov else dflt), json.dumps(ov) if ov else None, now(), pid))
                return self.get_profile(pid)
            p = P.validate_profile({**data, "builtin": False})
            clash = self.db.execute("SELECT id FROM profiles WHERE name=? AND id<>?", (p["name"], pid)).fetchone()
            if clash:
                raise ValueError(f"A test named '{p['name']}' already exists. Pick another name.")
            self.db.execute("UPDATE profiles SET name=?, json=?, updated_at=? WHERE id=?", (p["name"], json.dumps(p), now(), pid))
        return self.get_profile(pid)

    def reset_profile(self, pid: int) -> dict:
        """Built-in test back to the shipped settings, name and description (hidden/order are not touched)."""
        with self.lock:
            row = self.db.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
            if not row:
                raise KeyError(pid)
            dflt = self._defaults_by_key().get(row["preset_key"]) if row["builtin"] else None
            if not dflt:
                raise ValueError("Only the built-in tests have a default to go back to.")
            if self.db.execute("SELECT 1 FROM profiles WHERE name=? AND id<>?", (dflt["name"], pid)).fetchone():
                raise ValueError(f"Another test is now called '{dflt['name']}'. Rename that one first, then reset this test.")
            self.db.execute("UPDATE profiles SET name=?, json=?, override_json=NULL, updated_at=? WHERE id=?", (dflt["name"], json.dumps(dflt), now(), pid))
        return self.get_profile(pid)

    def set_hidden(self, pid: int, hidden: bool) -> dict:
        with self.lock:
            if not self.db.execute("SELECT 1 FROM profiles WHERE id=?", (pid,)).fetchone():
                raise KeyError(pid)
            self.db.execute("UPDATE profiles SET hidden=? WHERE id=?", (1 if hidden else 0, pid))
        return self.get_profile(pid)

    def restore_hidden(self) -> int:
        with self.lock:
            n = self.db.execute("SELECT COUNT(*) AS n FROM profiles WHERE hidden=1").fetchone()["n"]
            self.db.execute("UPDATE profiles SET hidden=0 WHERE hidden=1")
        return n

    def set_order(self, ids: List[int]):
        """Put the given tests in this order (anything not listed keeps its place after them)."""
        with self.lock:
            have = [r["id"] for r in self.db.execute("SELECT id FROM profiles ORDER BY position, id")]
            ids = [i for i in dict.fromkeys(ids) if i in have]
            rest = [i for i in have if i not in ids]
            # tests of other kinds (generator/sniffer presets) keep their slots: only the listed ones are re-sequenced among their own slots
            slots = sorted(have.index(i) for i in ids)
            order = list(have)
            for slot, pid in zip(slots, ids):
                order[slot] = pid
            for n, pid in enumerate(order):
                self.db.execute("UPDATE profiles SET position=? WHERE id=?", (float(n), pid))
        return self.list_profiles()

    def delete_profile(self, pid: int) -> dict:
        """Own tests only (built-in ones are hidden, not deleted). Returns what was deleted so the caller can offer Undo."""
        cur = self.get_profile(pid)
        if not cur:
            raise KeyError(pid)
        if cur["builtin"]:
            raise BuiltinError("Built-in tests cannot be deleted. Hide the test instead; it can be shown again.")
        with self.lock:
            self.db.execute("DELETE FROM profiles WHERE id=?", (pid,))
        return cur

    def undelete_profile(self, data: dict) -> dict:
        """Undo for delete: the same test back in the same place in the list (name made unique if something else took it meanwhile)."""
        pos = data.get("position")
        d = {k: v for k, v in data.items() if k not in ("id", "position", "hidden", "builtin", "preset_key", "edited", "default_name")}
        ok = isinstance(pos, (int, float)) and not isinstance(pos, bool)
        return self.create_profile(d, unique=True, position=pos - 0.5 if ok else None)

    def duplicate_profile(self, pid: int) -> dict:
        cur = self.get_profile(pid)
        if not cur:
            raise KeyError(pid)
        for k in ("id", "position", "hidden", "preset_key", "edited", "default_name", "override_error"):
            cur.pop(k, None)
        cur["name"] = f"{cur['name']} (copy)"
        return self.create_profile(cur, unique=True)

    # ---- export / import (profiles + the state of the Setup list)
    def export_profiles(self, ids: Optional[List[int]] = None) -> dict:
        profs = [p for p in self.list_profiles() if ids is None or p["id"] in ids]
        doc = P.export_profiles(profs)
        if ids is None:                     # "Export all" also carries the edits to built-in tests, what is hidden and the order
            doc["test_state"] = {
                "hidden": [p["preset_key"] for p in profs if p["builtin"] and p["hidden"]],
                "order": [p["preset_key"] if p["builtin"] else p["name"] for p in profs if (p.get("kind") or "trip") == "trip"],
            }
        return doc

    def import_profiles(self, doc: Any) -> List[dict]:
        P.parse_import(doc)                                       # whole file validated before anything is changed
        dflt = self._defaults_by_key()
        by_name = {d["name"]: k for k, d in dflt.items()}
        own, applied = [], 0
        for q in doc["profiles"]:
            key = q.get("preset_key") if q.get("builtin") else None
            if q.get("builtin") and (key in dflt or q.get("name") in by_name):
                key = key if key in dflt else by_name[q["name"]]
                if self._apply_preset_import(key, q):
                    applied += 1
                continue                                          # a built-in test is never imported as a second copy
            own.append(q)
        out = []
        for p in P.parse_import({**doc, "profiles": own}) if own else []:
            with self.lock:
                if self.db.execute("SELECT 1 FROM profiles WHERE name=?", (p["name"],)).fetchone():
                    p["name"] = self._unique_name(f"{p['name']} (imported)")
            out.append(self.create_profile(p, unique=True))
        self.import_state(doc.get("test_state"), {p["name"]: p["id"] for p in out}, [(q.get("name"), p["name"]) for q, p in zip(own, out)])
        self.last_import = {"presets_updated": applied}
        return out

    def _apply_preset_import(self, key: str, q: dict) -> bool:
        """An exported edited built-in test: becomes the override of the local one (only when it differs from the default)."""
        with self.lock:
            row = self.db.execute("SELECT * FROM profiles WHERE preset_key=?", (key,)).fetchone()
            dflt = self._defaults_by_key().get(key)
            if not row or not dflt or dflt.get("kind", "trip") != "trip":
                return False
            try:
                p = P.validate_profile({**q, "builtin": True})
            except ValueError:
                return False
            ov = P.diff_profile(dflt, p)
            if not ov:
                return False
            clash = self.db.execute("SELECT 1 FROM profiles WHERE name=? AND id<>?", (p["name"], row["id"])).fetchone()
            if clash:
                ov.pop("name", None)
                p = P.validate_profile({**P.merge_profile(dflt, ov), "builtin": True})
            self.db.execute("UPDATE profiles SET name=?, json=?, override_json=?, updated_at=? WHERE id=?",
                            (p["name"], json.dumps(P.merge_profile(dflt, ov)), json.dumps(ov), now(), row["id"]))
            return True

    def import_state(self, state: Any, created: Dict[str, int], renamed: List[tuple]):
        if not isinstance(state, dict):
            return
        with self.lock:
            rows = self.db.execute("SELECT id, name, preset_key, builtin FROM profiles").fetchall()
            ref_to_id = {(r["preset_key"] if r["builtin"] else r["name"]): r["id"] for r in rows}
            for old, new in renamed:                              # a test imported under a new name is still the one the file means
                if old and new and new in ref_to_id and old not in ref_to_id:
                    ref_to_id[old] = ref_to_id[new]
            for key in state.get("hidden") or []:
                if isinstance(key, str) and key in ref_to_id:
                    self.db.execute("UPDATE profiles SET hidden=1 WHERE id=? AND builtin=1", (ref_to_id[key],))
            order = [ref_to_id[k] for k in (state.get("order") or []) if isinstance(k, str) and k in ref_to_id]
        if order:
            self.set_order(order)

    # ---- sessions
    def new_session(self, name: Optional[str] = None) -> dict:
        name = name or f"Session {dt.datetime.now():%Y-%m-%d %H:%M}"
        with self.lock:
            cur = self.db.execute("INSERT INTO sessions(name,created_at) VALUES(?,?)", (name, now()))
        return self.get_session(cur.lastrowid)

    def get_session(self, sid: int) -> Optional[dict]:
        with self.lock:
            r = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None

    def current_session(self) -> dict:
        with self.lock:
            r = self.db.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r)

    def list_sessions(self) -> List[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM runs r WHERE r.session_id=s.id) AS runs FROM sessions s ORDER BY s.id DESC").fetchall()
        return [dict(r) for r in rows]

    def rename_session(self, sid: int, name: str):
        with self.lock:
            self.db.execute("UPDATE sessions SET name=? WHERE id=?", (name, sid))

    def clear_session(self, sid: int):
        with self.lock:
            ids = [r["id"] for r in self.db.execute("SELECT id FROM runs WHERE session_id=?", (sid,))]
            for rid in ids:
                self._delete_run(rid)

    def delete_session(self, sid: int):
        self.clear_session(sid)
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            if not self.db.execute("SELECT 1 FROM sessions").fetchone():
                self.new_session("First session")

    # ---- runs
    def create_run(self, session_id: int, profile: dict, role: str, port: str, group_id: Optional[int] = None, leg: Optional[dict] = None) -> int:
        if (profile.get("gen") or {}).get("peer_password"):      # the other Pi's web password must not end up in run exports (JSON / ZIP / USB)
            profile = {**profile, "gen": {**profile["gen"], "peer_password": ""}}
        if (profile.get("mirrored") or {}).get("peer_password"):
            profile = {**profile, "mirrored": {**profile["mirrored"], "peer_password": ""}}
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO runs(session_id,profile_json,role,transport,port,started_at,status,group_id,leg) VALUES(?,?,?,?,?,?,'running',?,?)",
                (session_id, json.dumps(profile), role, profile["transport"], port, now(), group_id, json.dumps(leg) if leg else None))
        return cur.lastrowid

    def set_group(self, run_id: int, group_id: int) -> None:
        with self.lock:
            self.db.execute("UPDATE runs SET group_id=? WHERE id=?", (group_id, run_id))

    def group_run_ids(self, rid: int) -> List[int]:
        """Every run of the test `rid` belongs to (the links / directions of one test share a group id); [rid] for a test with one run."""
        with self.lock:
            r = self.db.execute("SELECT group_id FROM runs WHERE id=?", (rid,)).fetchone()
            if not r:
                return []
            if r["group_id"] is None:
                return [rid]
            return [x["id"] for x in self.db.execute("SELECT id FROM runs WHERE group_id=? ORDER BY id", (r["group_id"],))] or [rid]

    # ---- job info (Site, Link / radio, By, Note): the same on every run of one test
    def set_job(self, rid: int, job: dict) -> List[int]:
        ids = self.group_run_ids(rid)
        with self.lock:
            for i in ids:
                self.db.execute("UPDATE runs SET job=?, note=? WHERE id=?", (json.dumps(job) if job else None, (job or {}).get("note", ""), i))
        return ids

    def last_job(self) -> dict:
        """The job info of the newest run that has any: what the next test on this Pi most likely belongs to."""
        with self.lock:
            r = self.db.execute("SELECT job FROM runs WHERE job IS NOT NULL AND job NOT IN ('', '{}') ORDER BY id DESC LIMIT 1").fetchone()
        try:
            j = json.loads(r["job"]) if r else {}
        except ValueError:
            j = {}
        return j if isinstance(j, dict) else {}

    def add_messages(self, run_id: int, rows: List[dict]):
        if not rows:
            return
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                [(run_id, r["seq"], r["phase"], r.get("idle_gap_s"), r.get("t_send_ms"), r.get("rtt_us"),
                  r.get("up_us"), r.get("down_us"), r["status"]) for r in rows])
            self.db.execute("COMMIT")

    def add_intervals(self, run_id: int, rows: List[dict]):
        if not rows:
            return
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany(
                "INSERT INTO intervals VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(run_id, r["t_offset_s"], r["sent"], r["received"], r["lost"], r["late"], r["p50_us"], r["p95_us"],
                  r["p99_us"], r["max_us"]) for r in rows])
            self.db.execute("COMMIT")

    def add_gen_intervals(self, run_id: int, rows: List[dict]):
        if not rows:
            return
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT INTO gen_intervals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                [(run_id, r["t_offset_s"], r["stream"], r["tx_pkts"], r["tx_bytes"], r["rx_pkts"], r["rx_bytes"], r["lost_cum"],
                                  r["lat_avg_us"], r["lat_p95_us"], r["lat_max_us"], r["jitter_us"], r["rtt_avg_us"]) for r in rows])
            self.db.execute("COMMIT")

    def run_gen_intervals(self, rid: int) -> List[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM gen_intervals WHERE run_id=? ORDER BY t_offset_s, stream", (rid,))]

    def gen_csv(self, rid: int) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["t_s", "stream", "tx_pkts", "tx_mbps", "rx_pkts", "rx_mbps", "lost_cumulative", "latency_avg_ms", "latency_p95_ms", "latency_max_ms", "jitter_ms", "rtt_avg_ms"] + self._JOB_HEAD + ["from_pi", "to_pi", "mark"])
        f = lambda v: "" if v is None else round(v / 1000.0, 3)
        run = self.get_run(rid) or {}
        job = self._job_cols(run)
        ends = {x.get("id"): (x.get("src", ""), x.get("dst", "")) for x in (run.get("summary") or {}).get("streams", [])}
        mk = (run.get("summary") or {}).get("marks") or []
        for r in self.run_gen_intervals(rid):
            w.writerow([r["t_offset_s"], r["stream"], r["tx_pkts"], round(r["tx_bytes"] * 8 / 1e6, 3), r["rx_pkts"], round(r["rx_bytes"] * 8 / 1e6, 3),
                        "" if r["lost_cum"] is None else r["lost_cum"], f(r["lat_avg_us"]), f(r["lat_p95_us"]), f(r["lat_max_us"]),
                        "" if r["jitter_us"] is None else round(r["jitter_us"] / 1000.0, 3), f(r["rtt_avg_us"])] + job + list(ends.get(r["stream"], ("", ""))) + [MK.mark_at(mk, r["t_offset_s"])])
        return buf.getvalue()

    def finish_run(self, run_id: int, status: str, summary: Optional[dict]):
        v = None
        if summary and "verdict" in summary:
            v = "PASS" if summary["verdict"]["pass"] else "FAIL"
        with self.lock:
            self.db.execute("UPDATE runs SET ended_at=?, status=?, summary_json=?, verdict=? WHERE id=?",
                            (now(), status, json.dumps(summary) if summary else None, v, run_id))

    def update_summary(self, run_id: int, summary: dict) -> None:
        with self.lock:
            self.db.execute("UPDATE runs SET summary_json=? WHERE id=?", (json.dumps(summary), run_id))

    def _run(self, r) -> dict:
        d = dict(r)
        d["profile"] = json.loads(d.pop("profile_json") or "{}")
        d["summary"] = json.loads(d.pop("summary_json")) if d.get("summary_json") else None
        d.pop("summary_json", None)
        d["leg"] = json.loads(d["leg"]) if d.get("leg") else None
        try:
            job = json.loads(d["job"]) if d.get("job") else {}
        except ValueError:
            job = {}
        d["job"] = job if isinstance(job, dict) else {}
        if not d["job"] and d.get("note"):                      # a note written before there was job info
            d["job"] = {"note": d["note"]}
        return d

    def get_run(self, rid: int) -> Optional[dict]:
        with self.lock:
            r = self.db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        return self._run(r) if r else None

    def list_runs(self, session_id: Optional[int] = None, limit: Optional[int] = None) -> List[dict]:
        q, a = "SELECT * FROM runs", ()
        if session_id:
            q, a = q + " WHERE session_id=?", (session_id,)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY id DESC" + (f" LIMIT {int(limit)}" if limit else ""), a).fetchall()
        from . import compare as C
        out, base = [], C.BaselineView(self)
        for r in rows:
            d = self._run(r)
            d["kind"] = C.kind_of(d)
            d["job_line"] = C.job_line(d["job"])
            d["vs_baseline"] = base.vs(d)
            s = d["summary"] or {}
            if s.get("kind") == "sniffer":
                a = s.get("analysis") or {}
                d["headline"] = {"p99_ms": None, "max_ms": None, "loss_pct": None, "sent": a.get("packets"), "over_threshold": None, "mbps": a.get("mbps")}
            elif s.get("kind") == "generator":
                t = s.get("totals") or {}
                d["headline"] = {"p99_ms": ((t.get("lat_ms") or {}).get("p99")), "max_ms": ((t.get("lat_ms") or {}).get("max")), "loss_pct": t.get("loss_pct"),
                                 "sent": t.get("tx_pkts"), "over_threshold": None, "mbps": t.get("rx_mbps")}
            else:
                d["headline"] = {"p99_ms": (s.get("rtt_ms") or {}).get("p99"), "max_ms": (s.get("rtt_ms") or {}).get("max"),
                                 "loss_pct": s.get("loss_pct"), "sent": s.get("sent"), "over_threshold": s.get("over_threshold")}
            d["name"] = d["profile"].get("name")
            d.pop("summary", None)
            out.append(d)
        return out

    def run_messages(self, rid: int, limit: Optional[int] = None) -> List[dict]:
        q = "SELECT * FROM messages WHERE run_id=? ORDER BY seq, rowid"
        if limit:
            q += f" LIMIT {int(limit)}"
        with self.lock:
            return [dict(r) for r in self.db.execute(q, (rid,))]

    def run_intervals(self, rid: int) -> List[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM intervals WHERE run_id=? ORDER BY t_offset_s", (rid,))]

    def _delete_run(self, rid: int):
        # capture files written by the sniffer live in <data>/captures/run-N: remove them with the test (the SD card is small).
        # A capture saved straight to a USB drive is somewhere else and is the user's copy: it is left alone.
        cap = os.path.join(self.data_dir, "captures", f"run-{int(rid)}")
        if os.path.isdir(cap) and not os.path.islink(cap):
            shutil.rmtree(cap, ignore_errors=True)
        for t in ("messages", "intervals", "gen_intervals"):
            self.db.execute(f"DELETE FROM {t} WHERE run_id=?", (rid,))
        self.db.execute("DELETE FROM runs WHERE id=?", (rid,))

    def delete_run(self, rid: int):
        with self.lock:
            self._delete_run(rid)

    # ---- exports
    _JOB_HEAD = ["site", "link_radio", "by", "note"]                      # job info: the same on every row, before the columns older tools read from the end
    _RADIO_HEAD = ["radio_recorded", "radio_char_timeout_ms", "radio_max_packet_bytes", "radio_serial_mode", "radio_note", "started_local", "ended_local", "time_tag"]
    _LEG_HEAD = _JOB_HEAD + ["group_id", "leg", "sender", "answering_pi", "basis"] + _RADIO_HEAD

    def _job_cols(self, r: Optional[dict]) -> list:
        j = (r or {}).get("job") or {}
        return [j.get("site", ""), j.get("link", ""), j.get("by", ""), j.get("note", "")]

    def _leg_cols(self, rid: int) -> list:
        """The same values on every row of a trip run's CSV: which test group (the two directions of one test share it), which direction, who sent, who answered, and the job info."""
        r = self.get_run(rid) or {}
        lg = r.get("leg") or ((r.get("summary") or {}).get("endpoints")) or {}
        gid = r.get("group_id")
        rd = (r.get("profile") or {}).get("radio") or {}
        w = (r.get("summary") or {}).get("when") or {}
        radio = ["yes" if any(rd.get(k) not in (None, "", 0) for k in ("char_timeout_ms", "max_packet_bytes", "serial_mode", "note")) else "no (radio packetization not recorded)",
                 "" if rd.get("char_timeout_ms") is None else rd["char_timeout_ms"], "" if rd.get("max_packet_bytes") is None else rd["max_packet_bytes"], rd.get("serial_mode") or "", rd.get("note") or "",
                 w.get("start_local", ""), w.get("end_local", ""), w.get("tag", "")]
        return self._job_cols(r) + ["" if gid is None else gid, lg.get("label") or "", lg.get("tester") or lg.get("source") or "", lg.get("reflector") or lg.get("dest") or "", lg.get("basis") or ""] + radio

    def _marks(self, rid: int) -> list:
        """The ⚑ marks dropped during the run (summary.marks): the CSV "mark" column names the one in force on each row."""
        return ((self.get_run(rid) or {}).get("summary") or {}).get("marks") or []

    def messages_csv(self, rid: int) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["seq", "phase", "idle_gap_s", "t_send_ms", "rtt_ms", "up_ms", "down_ms", "status"] + self._LEG_HEAD + ["mark"])
        extra, mk = self._leg_cols(rid), self._marks(rid)
        for m in self.run_messages(rid):
            f = lambda v: "" if v is None else round(v / 1000.0, 3)
            w.writerow([m["seq"], m["phase"], m["idle_gap_s"] if m["idle_gap_s"] is not None else "",
                        m["t_send_ms"], f(m["rtt_us"]), f(m["up_us"]), f(m["down_us"]), m["status"]] + extra +
                       [MK.mark_at(mk, None if m["t_send_ms"] is None else m["t_send_ms"] / 1000.0)])
        return buf.getvalue()

    def intervals_csv(self, rid: int) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["t_offset_s", "sent", "received", "lost", "late", "p50_ms", "p95_ms", "p99_ms", "max_ms"] + self._LEG_HEAD + ["mark"])
        extra, mk = self._leg_cols(rid), self._marks(rid)
        f = lambda v: "" if v is None else round(v / 1000.0, 3)
        for i in self.run_intervals(rid):
            w.writerow([i["t_offset_s"], i["sent"], i["received"], i["lost"], i["late"],
                        f(i["p50_us"]), f(i["p95_us"]), f(i["p99_us"]), f(i["max_us"])] + extra + [MK.mark_at(mk, i["t_offset_s"])])
        return buf.getvalue()

    def run_json(self, rid: int) -> dict:
        r = self.get_run(rid)
        r["intervals"] = self.run_gen_intervals(rid) if r["transport"] == "generator" else self.run_intervals(rid)
        return r

    def session_zip(self, sid: int) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            s = self.get_session(sid)
            z.writestr("session.json", json.dumps(s, indent=1))
            for r in self.list_runs(sid):
                base = f"run-{r['id']}"
                z.writestr(f"{base}/run.json", json.dumps(self.run_json(r["id"]), indent=1))
                if r.get("transport") == "generator":
                    z.writestr(f"{base}/generator-per-second.csv", self.gen_csv(r["id"]))
                    sm = (self.get_run(r["id"]) or {}).get("summary") or {}
                    if sm.get("steps"):                                 # throughput in steps: the step table too
                        from .gen.run import steps_csv
                        z.writestr(f"{base}/steps.csv", steps_csv(sm))
                elif r.get("transport") != "sniffer":             # a capture has no trip messages: no empty CSVs
                    z.writestr(f"{base}/messages.csv", self.messages_csv(r["id"]))
                    z.writestr(f"{base}/intervals.csv", self.intervals_csv(r["id"]))
        return buf.getvalue()
