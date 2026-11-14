"""Paths, binary lookup and the contained environment Claude Code runs in. All of Claude's credentials live under <data_dir>/claude/."""
from __future__ import annotations

import os
import shutil
from typing import Dict, Optional

OFF_MSG = "Claude Code is off. Log in from Settings to turn it on. The app works fully without it."


class ClaudeEnv:
    def __init__(self, data_dir: str, live_dir: Optional[str] = None):
        self.data_dir = data_dir
        self.base = os.path.join(data_dir, "claude")
        self.config_dir = os.path.join(self.base, "config")      # CLAUDE_CONFIG_DIR: credentials, sessions, settings
        self.home_dir = os.path.join(self.base, "home")          # HOME for the CLI (~/.claude.json lands here)
        self.work_dir = os.path.join(self.base, "work")          # scratch git copy of the app: where Claude may edit
        self.state_file = os.path.join(self.base, "state.json")
        self.live_dir = live_dir or os.environ.get("TT_APP_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def ensure_dirs(self) -> None:
        for d in (self.base, self.config_dir, self.home_dir):
            os.makedirs(d, mode=0o700, exist_ok=True)

    def binary(self) -> Optional[str]:
        for cand in (os.environ.get("TT_CLAUDE_BIN"), shutil.which("claude"), "/usr/local/bin/claude", "/usr/bin/claude", "/opt/traffic-tester/claude/bin/claude"):
            if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        return None

    def process_env(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Environment for the CLI: contained HOME/config, no API keys (we only ever use the signed-in subscription)."""
        e = {k: v for k, v in os.environ.items() if not (k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_") or k in ("CLAUDE_CONFIG_DIR",))}
        e.update({"HOME": self.home_dir, "CLAUDE_CONFIG_DIR": self.config_dir, "DISABLE_AUTOUPDATER": "1", "NO_COLOR": "1", "TERM": "dumb"})
        e.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        if extra:
            e.update(extra)
        return e
