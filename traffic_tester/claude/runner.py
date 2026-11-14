"""'Ask Claude': runs `claude -p` (headless, restricted, low priority) and turns its stream into chat events. Never touches a running test."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Dict, Iterator, Optional

from .env import ClaudeEnv
from .state import ClaudeState, internet_ok
from .workspace import Workspace

OFFLINE = "Ask Claude needs Wi-Fi internet. The app works fully offline without it. Check the Pi's Wi-Fi connection (Settings → Network)."
NOT_SIGNED_IN = "Claude Code is not signed in on this Pi. Open Settings → Claude Code and log in (about a minute, finished on your phone)."
NOT_INSTALLED = "Claude Code is not installed on this Pi. Install it with: sudo scripts/install-pi.sh --with-claude"
BUSY = "Claude is still working on the previous message. Wait for it or press Stop."

SYSTEM_PROMPT = """You are the assistant built into the Pi Automation Traffic Tester, a Raspberry Pi 5 network test tool used by network engineers (not programmers). The user is looking at the web app right now; you see it through the mcp__tt__ tools, not through screenshots.
- To understand what they mean by 'this', 'here', 'the last test', call get_screen first, then get_run / get_log as needed. Answer in plain words: what happened, why, what to try. Short answers; numbers with units; no jargon without a few words of explanation.
- You can operate the app with the mcp__tt__ tools (start_test, stop_test, save_profile, open_screen). Do what was asked, then say in one line what you did. Never start a test the user did not ask for. A running test must not be disturbed.
- You cannot run commands. The files in the current directory are a scratch copy of the app. If the user wants a feature or an interface change, edit those files; the user then sees a diff and decides whether to apply it. Never say a change is live before it is applied. Do not edit traffic_tester/claude/, scripts/install-pi.sh or other safety scripts.
- Be honest about what you cannot see or verify."""


def _niceness() -> None:
    try:
        os.setsid()
        os.nice(19)
        os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))        # never competes with the timing engine
    except (OSError, AttributeError):
        pass


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)


class ChatRunner:
    def __init__(self, env: ClaudeEnv, state: ClaudeState, ws: Workspace, token: str, port_fn=lambda: int(os.environ.get("TT_PORT", "8080")), timeout_s: float = 300.0):
        self.env, self.state, self.ws, self.token, self.port_fn, self.timeout_s = env, state, ws, token, port_fn, timeout_s
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.session_id: Optional[str] = None
        self._li_cache = (0.0, False)

    def _signed_in(self) -> bool:
        if time.monotonic() - self._li_cache[0] < 30 and self._li_cache[1]:
            return True
        ok = self.state.logged_in()
        self._li_cache = (time.monotonic(), ok)
        return ok

    def stop(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass

    def new_chat(self) -> None:
        self.session_id = None

    def _mcp_config(self) -> str:
        port = self.port_fn()
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cfg = {"mcpServers": {"tt": {"command": sys.executable, "args": ["-c", f"import sys; sys.path.insert(0, {root!r}); from traffic_tester.claude.mcp_server import main; main()"],
                                      "env": {"TT_API": f"http://127.0.0.1:{port}", "TT_TOKEN": self.token, "PYTHONPATH": root}}}}
        p = os.path.join(self.env.base, "mcp.json")
        self.env.ensure_dirs()
        with open(p, "w") as f:
            json.dump(cfg, f)
        os.chmod(p, 0o600)
        return p

    def stream(self, message: str) -> Iterator[Dict]:
        """Yield {'t': 'text'|'action'|'action_result'|'error'|'done', ...}. Always ends with 'done' or 'error'."""
        if not self.env.binary():
            yield {"t": "error", "code": "not_installed", "message": NOT_INSTALLED}
            return
        if not internet_ok():
            yield {"t": "error", "code": "offline", "message": OFFLINE}
            return
        if not self._signed_in():
            yield {"t": "error", "code": "not_signed_in", "message": NOT_SIGNED_IN}
            return
        if not self.lock.acquire(blocking=False):
            yield {"t": "error", "code": "busy", "message": BUSY}
            return
        try:
            self.ws.sync()
            first = self.session_id is None
            sid = self.session_id or str(uuid.uuid4())
            cmd = [self.env.binary(), "-p", message, "--output-format", "stream-json", "--verbose", "--restricted", "--strict-mcp-config", "--mcp-config", self._mcp_config(),
                   "--permission-mode", "acceptEdits", "--allowedTools", "mcp__tt__*", "Read", "Grep", "Glob", "Edit", "Write", "--append-system-prompt", SYSTEM_PROMPT]
            cmd += ["--session-id", sid] if first else ["--resume", sid]
            self.proc = subprocess.Popen(cmd, cwd=self.ws.work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env.process_env(), preexec_fn=_niceness)
            self.session_id = sid
            err_lines = []
            threading.Thread(target=lambda: [err_lines.append(l.rstrip()) for l in self.proc.stderr], daemon=True).start()
            killer = threading.Timer(self.timeout_s, self.stop)
            killer.start()
            saw_text, error_text = False, None
            try:
                for line in self.proc.stdout:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    t = ev.get("type")
                    if t == "assistant":
                        for c in (ev.get("message") or {}).get("content", []):
                            if c.get("type") == "text" and c.get("text", "").strip():
                                saw_text = True
                                yield {"t": "text", "text": c["text"]}
                            elif c.get("type") == "tool_use":
                                yield {"t": "action", "id": c.get("id"), "name": c.get("name", "").replace("mcp__tt__", ""), "input": c.get("input", {})}
                    elif t == "user":
                        for c in (ev.get("message") or {}).get("content", []):
                            if isinstance(c, dict) and c.get("type") == "tool_result":
                                txt = _text_of(c.get("content"))
                                yield {"t": "action_result", "id": c.get("tool_use_id"), "ok": not c.get("is_error"), "text": txt[:600]}
                    elif t == "result":
                        if ev.get("is_error"):
                            error_text = _text_of(ev.get("result"))
                        elif not saw_text and ev.get("result"):
                            yield {"t": "text", "text": ev["result"]}
                self.proc.wait(timeout=10)
            finally:
                killer.cancel()
                if self.proc.poll() is None:
                    self.stop()
            blob = (error_text or "") + " " + " ".join(err_lines[-5:])
            if error_text or self.proc.returncode not in (0, None):
                low = blob.lower()
                if any(k in low for k in ("not logged in", "/login", "invalid api key", "authentication", "401", "oauth", "unauthorized", "please run")):
                    self._li_cache = (0.0, False)
                    yield {"t": "error", "code": "not_signed_in", "message": NOT_SIGNED_IN}
                elif any(k in low for k in ("enotfound", "econn", "fetch failed", "network", "timed out", "offline", "getaddrinfo", "unable to connect")):
                    yield {"t": "error", "code": "offline", "message": OFFLINE}
                elif self.proc.returncode == -signal.SIGTERM:
                    yield {"t": "error", "code": "stopped", "message": "Stopped."}
                else:
                    yield {"t": "error", "code": "failed", "message": "Claude could not answer: " + (blob.strip()[:240] or f"exit code {self.proc.returncode}")}
                return
            d = self.ws.diff()
            yield {"t": "done", "session_id": sid, "changes": (not d["empty"]), "changed_files": [f["path"] for f in d["files"]][:20]}
        finally:
            self.proc = None
            self.lock.release()
