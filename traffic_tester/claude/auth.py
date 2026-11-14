"""Sign in (a one-time flow he finishes himself) and sign out (wipes every credential, verifies nothing is left)."""
from __future__ import annotations

import getpass
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

from . import scan as scanmod
from .env import ClaudeEnv
from .state import ClaudeState

URL_RE = re.compile(r"https://[^\s\"'<>]+")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


class LoginFlow:
    """`claude auth login --claudeai` under a pty. The app shows the URL; he opens it on his phone and pastes the code back."""

    def __init__(self, env: ClaudeEnv, state: ClaudeState):
        self.env, self.state = env, state
        self.id = uuid.uuid4().hex[:12]
        self.proc: Optional[subprocess.Popen] = None
        self.fd: Optional[int] = None
        self.output = ""
        self.url: Optional[str] = None
        self.status = "starting"          # starting | waiting_for_code | success | failed
        self.error: Optional[str] = None
        self.t0 = time.monotonic()
        self.lock = threading.Lock()

    def start(self) -> None:
        b = self.env.binary()
        if not b:
            raise FileNotFoundError("Claude Code is not installed on this Pi. Run: sudo scripts/install-pi.sh --with-claude")
        self.env.ensure_dirs()
        master, slave = pty.openpty()
        self.proc = subprocess.Popen([b, "auth", "login", "--claudeai"], stdin=slave, stdout=slave, stderr=slave, close_fds=True, cwd=self.env.base,
                                     env=self.env.process_env({"BROWSER": "/bin/true"}), start_new_session=True)
        os.close(slave)
        self.fd = master
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        fd = self.fd                                    # cancel() may close self.fd while we wait: keep our own handle and stop quietly
        while self.proc and self.proc.poll() is None and time.monotonic() - self.t0 < 600:
            try:
                r, _, _ = select.select([fd], [], [], 0.3)
            except (OSError, ValueError, TypeError):
                break
            if r:
                try:
                    d = os.read(fd, 4096)
                except OSError:
                    break
                if not d:
                    break
                with self.lock:
                    self.output += ANSI_RE.sub("", d.decode("utf-8", "replace"))
                    if not self.url:
                        m = URL_RE.search(self.output)
                        if m:
                            self.url, self.status = m.group(0).rstrip(".,)"), "waiting_for_code"
        time.sleep(0.3)
        rc = self.proc.poll() if self.proc else 1
        if rc is None:
            self._kill()
            self.status, self.error = "failed", "The sign-in took too long and was cancelled. Start again."
        elif rc == 0 and self.state.logged_in():
            self.status = "success"
            self.state.set(enabled=True)
        elif self.status != "success":
            self.status, self.error = "failed", "Sign-in did not complete" + (f": {self.output.strip().splitlines()[-1][:160]}" if self.output.strip() else ".")
        self._close()

    def submit(self, code: str) -> None:
        if self.fd is None or self.status != "waiting_for_code":
            raise RuntimeError("There is no sign-in waiting for a code. Start again.")
        os.write(self.fd, (code.strip() + "\r").encode())

    def info(self) -> Dict:
        with self.lock:
            return {"id": self.id, "status": self.status, "url": self.url, "error": self.error, "hint": "Open the link on your phone or PC, sign in to your Claude account, then paste the code it shows here."}

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass

    def _close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def cancel(self) -> None:
        self._kill()
        self._close()
        self.status = "failed" if self.status != "success" else self.status


def logout(env: ClaudeEnv, state: ClaudeState, stop_remote: Callable[[], None] = lambda: None, extra_roots: Optional[List[str]] = None, scan_seconds: float = 40.0) -> dict:
    """Leave nobody at the site able to use his account: CLI log-out, wipe every credential, stop Remote Control, turn everything off, verify."""
    steps: List[str] = []
    try:
        if env.binary():
            state.cli("auth", "logout", timeout=20)
            steps.append("signed out with the Claude CLI")
    except Exception as e:  # noqa
        steps.append(f"CLI sign-out skipped ({type(e).__name__})")
    state.set(enabled=False, remote_control=False)             # off first: endpoints refuse from this moment on
    try:
        stop_remote()
        steps.append("Remote Control stopped and disabled")
    except Exception as e:  # noqa
        steps.append(f"Remote Control stop reported: {e}")
    for d in (env.config_dir, env.home_dir):
        shutil.rmtree(d, ignore_errors=True)
    steps.append("saved credentials, sessions and settings deleted")
    # stray copies outside the contained directory (older installs, hand-made setup-token files, systemd drop-ins)
    # (only for the dedicated service user, or when TT_WIPE_STRAY=1: never delete a developer's own Claude login by accident; the scan below still reports it)
    wipe_stray = os.environ.get("TT_WIPE_STRAY") == "1" or getpass.getuser() == "traffic-tester"
    for p in (("~/.claude", "~/.claude.json", "~/.config/claude", "~/.config/claude-code") if wipe_stray else ()):
        q = os.path.expanduser(p)
        if os.path.isdir(q) and not os.path.realpath(q).startswith(os.path.realpath(env.base)):
            shutil.rmtree(q, ignore_errors=True)
        elif os.path.isfile(q):
            try:
                os.remove(q)
            except OSError:
                pass
    removed = _strip_token_lines(["/etc/traffic-tester/config.env"])
    steps += removed
    env.ensure_dirs()
    state.set(enabled=False, remote_control=False)
    roots = scanmod.default_roots(env.data_dir) + list(extra_roots or [])
    leftovers = scanmod.scan(roots, max_seconds=scan_seconds)
    return {"off": True, "steps": steps, "leftovers": leftovers, "verified": not leftovers,
            "message": "Claude Code is off. No saved sign-in was found on this Pi." if not leftovers else "Claude Code is off, but some files that look like credentials are still on this Pi: see the list."}


def _strip_token_lines(paths: List[str]) -> List[str]:
    out = []
    for p in paths:
        try:
            with open(p) as f:
                lines = f.readlines()
        except OSError:
            continue
        keep = [l for l in lines if not scanmod.ENV_RE.search(l.encode())]
        if len(keep) != len(lines):
            try:
                with open(p, "w") as f:
                    f.writelines(keep)
                out.append(f"token line removed from {p}")
            except OSError:
                pass
    return out
