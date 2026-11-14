"""What the web API shows of the emulator(s) on this Pi: the local end (when a test or the answering side runs here), a poller that follows the
far end through ITS web API, and an in-process self-test pair."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque, Optional

from .engine import Emulator, default_pair
from .links import InternalLink
from .pairing import proto_conflict, id_conflict
from .status import Settings


class Registry:
    def __init__(self):
        self.lock = threading.RLock()
        self.local: Optional[Emulator] = None
        self.role = ""                       # "near" (this Pi started the test) | "far" (this Pi answers) | "selftest"
        self.far_client = None               # PeerClient to the other Pi, or None
        self.far_label = ""
        self.local_label = ""
        self.far_local: Optional[Emulator] = None      # self-test: the other end lives in this process
        self.remote: Optional[dict] = None
        self.remote_error: Optional[str] = None
        self.remote_tx: Deque[dict] = deque(maxlen=600)
        self.remote_rx: Deque[dict] = deque(maxlen=600)
        self._poll_stop = threading.Event()
        self._poller: Optional[threading.Thread] = None
        self._st_stop = threading.Event()
        self._st_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ attach
    def set_local(self, emu: Emulator, role: str, far_client=None, far_label: str = "", local_label: str = "") -> None:
        with self.lock:
            self._stop_poller()
            self.local, self.role, self.far_client, self.far_label, self.local_label = emu, role, far_client, far_label, local_label
            self.far_local = None
            self.remote, self.remote_error = None, None
            self.remote_tx.clear()
            self.remote_rx.clear()
            if far_client is not None:
                self._start_poller()

    def stop_follow(self) -> None:
        with self.lock:
            self._stop_poller()

    # ------------------------------------------------------------ far-end poller
    def _start_poller(self) -> None:
        self._poll_stop = threading.Event()
        self._poller = threading.Thread(target=self._poll, args=(self._poll_stop, self.far_client), name="mb-far-poll", daemon=True)
        self._poller.start()

    def _stop_poller(self) -> None:
        self._poll_stop.set()

    def _poll(self, stop: threading.Event, client) -> None:
        since_tx = since_rx = 0
        while not stop.wait(0.25):
            try:
                r = client.get(f"/api/mb/live?since_tx={since_tx}&since_rx={since_rx}", timeout=2.5)
                if not r or not r.get("running_any"):
                    continue
                loc = r.get("local") or r
                with self.lock:
                    if loc.get("tx_chips"):
                        self.remote_tx.extend(loc["tx_chips"])
                    if loc.get("rx_chips"):
                        self.remote_rx.extend(loc["rx_chips"])
                    since_tx, since_rx = loc.get("tx_last", since_tx), loc.get("rx_last", since_rx)
                    self.remote = {k: v for k, v in loc.items() if k not in ("tx_chips", "rx_chips")}
                    self.remote_error = None
            except Exception as e:  # noqa
                with self.lock:
                    self.remote_error = str(e)[:200]

    # ------------------------------------------------------------ views
    def view(self, since_tx: int = 0, since_rx: int = 0, r_since_tx: int = 0, r_since_rx: int = 0) -> dict:
        with self.lock:
            emu, role = self.local, self.role
            far_local = self.far_local
        if emu is None:
            return {"running_any": False, "role": "", "local": None, "remote": None}
        loc = emu.live(since_tx, since_rx)
        loc["label"] = self.local_label or emu.name
        remote = None
        rerr = None
        r_tx = r_rx = []
        if far_local is not None:
            remote = far_local.live(r_since_tx, r_since_rx)
            r_tx, r_rx = remote.pop("tx_chips"), remote.pop("rx_chips")
            remote["label"] = self.far_label or far_local.name
        elif self.far_client is not None:
            with self.lock:
                remote = dict(self.remote) if self.remote else None
                rerr = self.remote_error
                r_tx = [c for c in self.remote_tx if c["id"] > r_since_tx]
                r_rx = [c for c in self.remote_rx if c["id"] > r_since_rx]
            if remote is not None:
                remote["label"] = self.far_label
        warnings = []
        w = proto_conflict(loc.get("proto"), (remote or {}).get("proto"))
        if w:
            warnings.append(w)
        if remote:
            w = id_conflict(loc.get("settings") or {}, remote.get("settings") or {})
            if w:
                warnings.append(w)
        return {"running_any": True, "role": role, "local": loc, "remote": remote, "remote_error": rerr, "warnings": warnings, "far_label": self.far_label,
                "remote_tx_chips": r_tx, "remote_rx_chips": r_rx,
                "remote_tx_last": r_tx[-1]["id"] if r_tx else r_since_tx, "remote_rx_last": r_rx[-1]["id"] if r_rx else r_since_rx}

    # ------------------------------------------------------------ in-process self-test (two instances, one process)
    def start_selftest(self, duration_s: float = 60.0, a: Optional[dict] = None, b: Optional[dict] = None, impair_a=None, impair_b=None,
                       baud: int = 19200, names=("Emulator A", "Emulator B")) -> dict:
        with self.lock:
            if self.local is not None and self.local.running and self.role != "selftest":
                raise RuntimeError("A test is running on this Pi: stop it before starting the self-test.")
            self.stop_selftest()
            sa, sb = default_pair({"baud": baud, **(a or {})}, {"baud": baud, **(b or {})})
            la, lb = InternalLink.pair()
            ea = Emulator(sa, la, names[0], duration_s=duration_s)
            eb = Emulator(sb, lb, names[1], duration_s=duration_s)
            if impair_a:
                ea.imp.set(impair_a, 0)
            if impair_b:
                eb.imp.set(impair_b, 0)
            self._stop_poller()
            self.local, self.role, self.far_client, self.far_local = ea, "selftest", None, eb
            self.local_label, self.far_label = names
            self.remote, self.remote_error = None, None
            eb.start()
            ea.start()
        return {"ok": True, "duration_s": duration_s}

    def stop_selftest(self) -> None:
        with self.lock:
            for e in (self.local, self.far_local):
                if e is not None and self.role == "selftest":
                    e.stop(join=False)
