"""Remote Control (continue the Pi's Claude session from the Claude app on a phone) as a systemd service.
The web app has NO privilege to run it: it only writes `remote_control` into state.json; a root-owned systemd path unit
(traffic-tester-claude-rc.path -> scripts/tt-rc-sync) starts/stops the service to match, and scripts/tt-rc-run also stops itself within
seconds when the flag is cleared (log-out). Status is read with an unprivileged `systemctl is-active`."""
from __future__ import annotations

import os
import subprocess
import time
from typing import Dict

from .state import ClaudeState

UNIT = "traffic-tester-claude-rc.service"


class RemoteControl:
    def __init__(self, state: ClaudeState):
        self.state = state

    def _active(self) -> str:
        try:
            r = subprocess.run([os.environ.get("TT_SYSTEMCTL", "systemctl"), "is-active", UNIT], capture_output=True, text=True, timeout=8)
            return r.stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unavailable"

    def _wait(self, want_active: bool, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if (self._active() == "active") == want_active:
                return True
            time.sleep(0.5)
        return (self._active() == "active") == want_active

    def start(self) -> Dict:
        if self._active() == "unavailable":
            raise RuntimeError("Remote Control is not set up on this Pi (reinstall with: sudo scripts/install-pi.sh --with-claude).")
        self.state.set(remote_control=True)
        if not self._wait(True, float(os.environ.get("TT_RC_WAIT", "15"))):
            self.state.set(remote_control=False)
            raise RuntimeError(f"Remote Control did not start. See: journalctl -u {UNIT} -n 30")
        return self.status()

    def stop(self) -> Dict:
        self.state.set(remote_control=False)
        self._wait(False, float(os.environ.get("TT_RC_WAIT", "15")))
        return self.status()

    def status(self) -> Dict:
        a = self._active()
        return {"active": a == "active", "detail": a}
