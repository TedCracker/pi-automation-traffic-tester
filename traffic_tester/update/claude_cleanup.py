"""One-time cleanup of what the removed 'Ask Claude' feature (versions up to 1.6) left on a Pi: Remote Control systemd units, the Claude Code package and its apt
repository, the Claude sign-in folder, and the app's git history of Claude's changes. Idempotent (nothing to remove -> nothing happens) and it never touches
tests, results or settings. Runs as root: from the update helper (op "claude_cleanup") and from install-pi.sh (python -m traffic_tester.update.claude_cleanup)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Callable, List, Optional

UNITS = ("traffic-tester-claude-rc.path", "traffic-tester-claude-rc.service", "traffic-tester-claude-rc-sync.service")
APP_FILES = ("scripts/tt-rc-run", "scripts/tt-rc-sync", "scripts/claude_real_probe.py", ".git", ".gitignore")


def _sh(argv: List[str]) -> int:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


def leftovers(data_dir: str, etc_dir: str = "/etc", app_dir: Optional[str] = None) -> List[str]:
    """What is still here (cheap, read-only: the app calls it as the service user to decide whether the privileged helper is needed)."""
    found = []
    for u in UNITS:
        if os.path.exists(os.path.join(etc_dir, "systemd/system", u)):
            found.append(u)
    for p in (os.path.join(etc_dir, "apt/sources.list.d/claude-code.list"), os.path.join(etc_dir, "apt/keyrings/claude-code.asc"), os.path.join(data_dir or "/nonexistent", "claude")):
        if os.path.exists(p):
            found.append(p)
    if app_dir:
        found += [os.path.join(app_dir, f) for f in APP_FILES if os.path.exists(os.path.join(app_dir, f))]
        base = os.path.dirname(os.path.abspath(app_dir))
        if os.path.exists(os.path.join(base, "install-claude.sh")):
            found.append(os.path.join(base, "install-claude.sh"))
    if etc_dir == "/etc" and os.path.exists("/var/lib/dpkg/info/claude-code.list"):
        found.append("claude-code package")
    return found


def cleanup(data_dir: str, app_dir: Optional[str] = None, etc_dir: str = "/etc", run: Callable[[List[str]], int] = _sh, log: Callable[[str], None] = print) -> List[str]:
    """Remove everything listed by leftovers(); returns what was removed (also logged, one line each)."""
    removed: List[str] = []
    real = etc_dir == "/etc"
    units = [u for u in UNITS if os.path.exists(os.path.join(etc_dir, "systemd/system", u))]
    if units:
        if real:
            run(["systemctl", "disable", "--now", *UNITS])
        for u in units:
            os.unlink(os.path.join(etc_dir, "systemd/system", u))
            removed.append(f"systemd unit {u}")
        if real:
            run(["systemctl", "daemon-reload"])
    if real and os.path.exists("/var/lib/dpkg/info/claude-code.list") and run(["dpkg", "-s", "claude-code"]) == 0:
        if run(["apt-get", "remove", "-y", "-qq", "claude-code"]) == 0:
            removed.append("package claude-code")
    for p in (os.path.join(etc_dir, "apt/sources.list.d/claude-code.list"), os.path.join(etc_dir, "apt/keyrings/claude-code.asc")):
        if os.path.exists(p):
            os.unlink(p)
            removed.append(p)
    cdir = os.path.join(data_dir, "claude") if data_dir else ""
    if cdir and os.path.isdir(cdir):
        shutil.rmtree(cdir, ignore_errors=True)
        removed.append(cdir)
    if app_dir:
        for f in APP_FILES:
            p = os.path.join(app_dir, f)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
                removed.append(p)
            elif os.path.lexists(p):
                os.unlink(p)
                removed.append(p)
        old = os.path.join(os.path.dirname(os.path.abspath(app_dir)), "install-claude.sh")
        if os.path.exists(old):
            os.unlink(old)
            removed.append(old)
    for r in removed:
        log("Removed (Ask Claude is gone from this app): " + r)
    return removed


if __name__ == "__main__":
    a = sys.argv[1:]
    get = lambda k, d=None: a[a.index(k) + 1] if k in a else d      # noqa: E731
    out = cleanup(get("--data", "/var/lib/traffic-tester"), get("--app", "/opt/traffic-tester/app"))
    if not out:
        print("Nothing from Ask Claude to remove.")
