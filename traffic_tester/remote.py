"""A test that runs on ANOTHER Pi, started and watched from this one (the controller).

`RemoteTester` has the same face as `engine.Tester` (run / stop / status / error), so the run manager treats it like a local test: it starts the
test through the other Pi's normal API, relays what that Pi reports to this Pi's live screen, and when the test ends copies the result
(summary, per-message rows, per-second rows) into this Pi's own results. The other Pi keeps its own copy too."""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .peers import PeerClient, PeerError


class RemoteTester:
    POLL_S = 0.5
    LOST_GRACE_S = 20.0           # the other Pi may be unreachable this long after the test should have ended before the run is given up

    def __init__(self, profile: dict, client: PeerClient, name: str, emit: Callable[[dict], None] = lambda e: None,
                 on_message: Callable[[dict], None] = lambda r: None, on_interval: Callable[[dict], None] = lambda r: None, controller: str = ""):
        self.p, self.client, self.name = profile, client, name
        self.emit, self.on_message, self.on_interval = emit, on_message, on_interval
        self.controller = controller
        self.stop_evt = threading.Event()
        self.status = "created"
        self.error: Optional[str] = None
        self.rid: Optional[int] = None
        self.total_s = 0.0

    def stop(self) -> None:
        self.stop_evt.set()
        if self.rid is not None:
            try:
                self.client.post(f"/api/runs/{self.rid}/stop", {}, timeout=5)
            except PeerError:
                pass

    # ------------------------------------------------------------ run
    def run(self) -> dict:
        summary: Optional[dict] = None
        try:
            r = self.client.post("/api/runs", {"profile": self.p, "managed": True, "controller": self.controller}, timeout=15)
            self.rid = r["run_id"]
            self.status = "running"
            if self.stop_evt.is_set():                       # Stop was pressed while the start request was on its way: pass it on now
                self.stop()
            summary = self._follow()
        except PeerError as e:
            self.error, self.status = str(e), "failed"
        except Exception as e:  # noqa
            self.error, self.status = f"{type(e).__name__}: {e}", "failed"
        if summary is None:
            summary = {"error": self.error}
            self.emit({"ev": "done", "status": self.status, "error": self.error, "summary": None})
        return summary

    def _follow(self) -> Optional[dict]:
        last_seq = -1
        t0 = time.monotonic()
        started = False
        misses = 0
        planned = 0.0
        while True:
            time.sleep(self.POLL_S)
            try:
                try:
                    live = self.client.get(f"/api/runs/{self.rid}/live", timeout=4)
                except PeerError as e:
                    if getattr(e, "status", None) != 404:
                        raise
                    live = None
                misses = 0
            except PeerError as e:
                misses += 1
                if time.monotonic() - t0 > planned + self.LOST_GRACE_S and misses >= 5:
                    raise PeerError(f"Lost contact with {self.name} during the test ({e}).")
                continue
            if live is not None and live.get("running", True):
                st = live.get("state") or {}
                if not started:
                    started = True
                    self.total_s = float(st.get("total_s") or 0)
                    planned = self.total_s
                    self.emit({"ev": "started", "total_s": self.total_s, "transport": f"on {self.name}", "one_way": bool(self.p.get("one_way")), "peer": {"name": self.name}})
                if st:
                    planned = max(planned, float(st.get("total_s") or 0))
                    self.emit(st)
                for m in live.get("recent") or []:
                    if m.get("seq", -1) > last_seq:
                        last_seq = m["seq"]
                        self.emit(m)
                continue
            # not the running test any more: it ended
            try:
                run = self.client.get(f"/api/runs/{self.rid}", timeout=10)
                misses = 0
            except PeerError as e:                            # one failed request at the very end must not throw the result away: ask again
                if getattr(e, "status", None) == 404:
                    raise
                misses += 1
                if time.monotonic() - t0 > planned + self.LOST_GRACE_S and misses >= 5:
                    raise PeerError(f"Lost contact with {self.name} at the end of the test ({e}).")
                continue
            if run.get("status") == "running":
                continue
            return self._collect(run)

    def _collect(self, run: dict) -> dict:
        self.status = run.get("status") or "finished"
        if self.stop_evt.is_set() and self.status == "finished":
            self.status = "stopped"
        summary = run.get("summary") or {"error": "The other Pi sent no result."}
        try:
            msgs = self.client.get(f"/api/runs/{self.rid}/messages?limit=200000", timeout=60) or []
            for i in range(0, len(msgs), 500):
                for m in msgs[i:i + 500]:
                    self.on_message(m)
            for row in run.get("intervals") or []:
                self.on_interval(row)
        except PeerError as e:
            summary.setdefault("warnings", []).append(f"The per-message details could not be copied from {self.name}: {e}")
        if summary.get("error"):
            self.error = summary["error"]
        self.emit({"ev": "done", "status": self.status, "error": self.error, "summary": summary if "verdict" in summary else None})
        return summary
