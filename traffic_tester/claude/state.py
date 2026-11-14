"""On/off state (default OFF), plain status, and the 'internet reachable' probe. Nothing here starts Claude."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from typing import Optional

from .env import ClaudeEnv

_net_cache = (0.0, False)


def internet_ok(max_age: float = 20.0) -> bool:
    """Can we reach Anthropic over HTTPS right now? (2 s timeout, cached.) Never blocks tests: runs only when the panel is used."""
    global _net_cache
    if os.environ.get("TT_CLAUDE_FAKE_ONLINE") in ("0", "1"):
        return os.environ["TT_CLAUDE_FAKE_ONLINE"] == "1"
    if time.monotonic() - _net_cache[0] < max_age:
        return _net_cache[1]
    ok = False
    try:
        with socket.create_connection(("api.anthropic.com", 443), timeout=2):
            ok = True
    except OSError:
        ok = False
    _net_cache = (time.monotonic(), ok)
    return ok


class ClaudeState:
    def __init__(self, env: ClaudeEnv):
        self.env = env

    def _read(self) -> dict:
        try:
            with open(self.env.state_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _write(self, d: dict) -> None:
        self.env.ensure_dirs()
        tmp = self.env.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.env.state_file)

    @property
    def enabled(self) -> bool:
        return bool(self._read().get("enabled", False))          # default after install: OFF

    @property
    def remote_wanted(self) -> bool:
        return bool(self._read().get("remote_control", False)) and self.enabled

    def set(self, **kw) -> None:
        d = self._read()
        d.update(kw)
        self._write(d)

    def cli(self, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
        b = self.env.binary()
        if not b:
            raise FileNotFoundError("claude not installed")
        self.env.ensure_dirs()                       # the CLI runs inside this folder, so it must exist even before the first sign-in
        return subprocess.run([b, *args], capture_output=True, text=True, timeout=timeout, env=self.env.process_env(), cwd=self.env.base)

    def logged_in(self) -> bool:
        """Ask the CLI itself (it never prints the token)."""
        if not self.env.binary():
            return False
        try:
            r = self.cli("auth", "status", "--json", timeout=15)
            return bool(json.loads(r.stdout or "{}").get("loggedIn"))
        except (OSError, subprocess.SubprocessError, ValueError):
            return False

    def version(self) -> Optional[str]:
        if not self.env.binary():
            return None
        try:
            return self.cli("--version", timeout=10).stdout.strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            return None
