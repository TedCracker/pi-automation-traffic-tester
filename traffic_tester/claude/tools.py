"""What Claude can see and do inside the app: structured state (not screenshots) and the same actions a user has. Called only through
the token-protected /api/claude/tool/* endpoint, which refuses when Claude is off."""
from __future__ import annotations

import copy
import time
from typing import Any, Callable, Dict, List, Optional

from .. import netcheck, profiles as P, system


def _merge(a: dict, b: dict) -> dict:
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v
    return a


def _brief_run(r: dict) -> dict:
    s = r.get("summary") or {}
    out = {"id": r["id"], "name": r.get("name") or (r.get("profile") or {}).get("name"), "kind": s.get("kind", "trip"), "status": r["status"], "verdict": r.get("verdict"), "started_at": r["started_at"], "headline": r.get("headline")}
    return out


def run_digest(run: dict) -> dict:
    """Compact, readable view of one finished run for Claude (not the raw per-message data)."""
    s = run.get("summary") or {}
    d = {"id": run["id"], "status": run["status"], "verdict": run.get("verdict"), "profile": {k: (run.get("profile") or {}).get(k) for k in ("name", "kind", "transport", "threshold_ms", "interval_ms", "duration_s")}, "started_at": run["started_at"], "error": s.get("error")}
    kind = s.get("kind", "trip")
    if kind == "generator":
        d["totals"] = s.get("totals")
        d["clocks"] = s.get("clocks")
        d["reasons"] = (s.get("verdict") or {}).get("reasons")
        d["streams"] = [{k: x.get(k) for k in ("name", "type", "direction", "size", "loss_pct", "lat_ms", "jitter_ms", "rtt_ms", "warnings")} | {"tx_mbps": x["tx"]["mbps"], "rx_mbps": x["rx"]["mbps"]} for x in s.get("streams", [])]
    elif kind == "sniffer":
        a = s.get("analysis") or {}
        d["mode"] = s.get("mode")
        d["stop_reason"] = s.get("stop_reason")
        d["analysis"] = {k: a.get(k) for k in ("packets", "bytes", "duration_s", "protocols", "top_talkers", "errors", "crc_failures", "dnp3_frames", "modbus_frames", "max_gap_ms", "wifi_networks")}
        d["engine"] = s.get("engine")
    else:
        d["metrics"] = {k: s.get(k) for k in ("sent", "received", "lost", "loss_pct", "over_threshold", "rtt_ms", "jitter_ms", "idle", "duplicates", "out_of_order", "corrupted", "one_way", "up_ms", "down_ms", "extra")}
        d["reasons"] = (s.get("verdict") or {}).get("reasons")
    return d


class ToolBox:
    def __init__(self, st: Any):
        self.st = st

    # ---- reading
    def get_screen(self, **_) -> dict:
        st = self.st
        live: Dict[str, Any] = {"running": st.runs.busy(), "state": st.runs.last_state, "run": st.runs.status()}
        if st.runs.sniff and st.runs.busy():
            lv = st.runs.sniff.live(0)
            live["sniffer"] = {"packets_seen": lv.get("n"), "last_rows": [{k: r.get(k) for k in ("proto", "src", "dst", "len", "info", "crc_ok", "channel_name")} for r in lv.get("rows", [])[-12:]]}
        return {"ui": st.ui_state, "live": live, "network": {"status": netcheck.check().get("status")}, "hint": "ui = what the user is looking at (tab, selected profile, settings on screen)"}

    def get_status(self, **_) -> dict:
        st = self.st
        return {"pi": {"id": st.me.id, "name": st.me.name}, "run": st.runs.status(), "reflector": st.reflectors.status(), "peers": [{"name": p["name"], "host": p["host"], "reachable": p["reachable"]} for p in st.peers.list()], "clock": system.clock_state(), "hardware": system.pi_model(), "session": st.store.current_session()}

    def list_profiles(self, **_) -> dict:
        return {"profiles": [{"id": p["id"], "name": p["name"], "kind": p.get("kind", "trip"), "builtin": p["builtin"], "transport": p.get("transport"), "description": p.get("description", "")[:160]} for p in self.st.store.list_profiles()]}

    def get_profile(self, id: int, **_) -> dict:
        p = self.st.store.get_profile(int(id))
        return p or {"error": f"No profile with id {id}."}

    def list_runs(self, limit: int = 10, **_) -> dict:
        return {"runs": [_brief_run(r) for r in self.st.store.list_runs()[: int(limit)]]}

    def get_run(self, id: int, **_) -> dict:
        r = self.st.store.get_run(int(id))
        return run_digest(r) if r else {"error": f"No test with id {id}."}

    def get_log(self, lines: int = 80, **_) -> dict:
        return {"lines": self.st.applog.tail(int(lines))}

    def network_check(self, **_) -> dict:
        peers = netcheck.peers_from_profiles(self.st.store.list_profiles())
        return netcheck.check(peers)

    # ---- operating
    def open_screen(self, tab: str, profile_id: Optional[int] = None, **_) -> dict:
        if tab == "monitor":                     # the live chart moved into the Run screen (2026-11); the old name still works
            tab = "run"
        if tab not in ("setup", "run", "results", "generator", "sniffer", "settings"):
            return {"error": "tab must be one of setup, run, results, generator, sniffer, settings"}
        self.st.push_ui({"ev": "uicmd", "cmd": "open", "tab": tab, "profile_id": profile_id})
        return {"ok": True, "opened": tab}

    def start_test(self, profile_id: Optional[int] = None, profile: Optional[dict] = None, overrides: Optional[dict] = None, **_) -> dict:
        st = self.st
        if profile_id is not None:
            base = st.store.get_profile(int(profile_id))
            if not base:
                return {"error": f"No profile with id {profile_id}."}
            base = copy.deepcopy(base)
            base.pop("id", None)
            prof = _merge(base, overrides or {})
        elif profile:
            prof = _merge(copy.deepcopy(profile), {})
        else:
            return {"error": "Give profile_id (and optional overrides) or a full profile."}
        try:
            prof = P.validate_profile(prof)
            rid = st.runs.start(prof)
        except Exception as e:  # noqa
            msg = getattr(e, "detail", None) or str(e)
            return {"error": msg}
        tab = {"generator": "generator", "sniffer": "sniffer"}.get(prof["kind"], "run")
        st.push_ui({"ev": "uicmd", "cmd": "run_started", "tab": tab, "run_id": rid, "profile": prof})
        return {"ok": True, "run_id": rid, "name": prof["name"], "shown_on_screen": tab}

    def stop_test(self, **_) -> dict:
        st = self.st
        if not st.runs.busy():
            return {"ok": True, "note": "No test is running."}
        st.runs.stop()
        return {"ok": True, "stopping": st.runs.run_id}

    def save_profile(self, profile: dict, **_) -> dict:
        st = self.st
        try:
            p = P.validate_profile({**profile, "builtin": False})
            existing = next((x for x in st.store.list_profiles() if x["name"] == p["name"]), None)
            if existing and existing["builtin"]:
                p["name"] = p["name"] + " (mine)"
                existing = None
            saved = st.store.update_profile(existing["id"], p) if existing else st.store.create_profile(p)
        except Exception as e:  # noqa
            return {"error": getattr(e, "detail", None) or str(e)}
        st.push_ui({"ev": "uicmd", "cmd": "profile_saved", "profile_id": saved["id"], "kind": saved.get("kind", "trip")})
        return {"ok": True, "id": saved["id"], "name": saved["name"]}


TOOL_SPECS: List[dict] = [
    {"name": "get_screen", "description": "What the user sees right now: current tab, selected profile, the settings on screen, live test state, and a few recent live packets. Call this first when asked about 'this', 'here', 'the screen'.", "schema": {"type": "object", "properties": {}}},
    {"name": "get_status", "description": "Overall app status: this Pi (id, name), the running test, whether this Pi is answering tests (reflector), the other Pis it knows, clock sync, hardware.", "schema": {"type": "object", "properties": {}}},
    {"name": "list_profiles", "description": "All saved and built-in test profiles (id, name, kind: trip/generator/sniffer).", "schema": {"type": "object", "properties": {}}},
    {"name": "get_profile", "description": "One profile with every setting.", "schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "list_runs", "description": "Recent tests with their verdicts and headline numbers.", "schema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "get_run", "description": "Readable results of one test: verdict, reasons, percentiles, loss, jitter, idle checks, per-stream numbers or capture analysis.", "schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "get_log", "description": "The app's recent log lines (tests started/finished, errors).", "schema": {"type": "object", "properties": {"lines": {"type": "integer"}}}},
    {"name": "network_check", "description": "Routing self-check: internet must leave over Wi-Fi, test traffic over Ethernet.", "schema": {"type": "object", "properties": {}}},
    {"name": "open_screen", "description": "Switch the user's screen to a tab (setup, run, results, generator, sniffer, settings; the live chart is on run), optionally selecting a profile.", "schema": {"type": "object", "properties": {"tab": {"type": "string"}, "profile_id": {"type": "integer"}}, "required": ["tab"]}},
    {"name": "start_test", "description": "Start a test from a saved profile (profile_id) with optional overrides (a partial profile, e.g. {\"duration_s\": 30, \"threshold_ms\": 40}) or a full profile object. The screen switches to the live view.", "schema": {"type": "object", "properties": {"profile_id": {"type": "integer"}, "overrides": {"type": "object"}, "profile": {"type": "object"}}}},
    {"name": "stop_test", "description": "Stop the running test (results so far are kept).", "schema": {"type": "object", "properties": {}}},
    {"name": "save_profile", "description": "Create or update a saved profile (full profile object with a name; built-in presets are never overwritten).", "schema": {"type": "object", "properties": {"profile": {"type": "object"}}, "required": ["profile"]}},
]
