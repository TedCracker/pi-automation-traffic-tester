"""Scratch git copy of the app where Claude may edit; a diff is shown, applying commits to the live app (revertible)."""
from __future__ import annotations

import os
import subprocess
from typing import List, Optional

PROTECTED = ("traffic_tester/claude/", "scripts/", "docs/INSTALL-PI.md", ".git/")
BASE_AUTHOR = ["-c", "user.name=Pi Automation Traffic Tester (installer)", "-c", "user.email=installer@localhost"]
AUTHOR = ["-c", "user.name=Pi Automation Traffic Tester (Claude)", "-c", "user.email=claude@localhost"]


class WorkspaceError(Exception):
    pass


def _git(cwd: str, *args: str, check: bool = True, timeout: float = 60, input_: Optional[str] = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input_)
    if check and r.returncode:
        raise WorkspaceError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else f"git {args[0]} failed")
    return r.stdout


class Workspace:
    def __init__(self, live_dir: str, work_dir: str):
        self.live, self.work = live_dir, work_dir

    def ensure(self) -> None:
        if not os.path.isdir(os.path.join(self.live, ".git")):
            _git(self.live, "init", "-q")
            _git(self.live, "add", "-A")
            _git(self.live, *BASE_AUTHOR, "commit", "-q", "-m", "Initial import of the installed app", check=False)
        if not os.path.isdir(os.path.join(self.work, ".git")):
            os.makedirs(os.path.dirname(self.work), exist_ok=True)
            _git(os.path.dirname(self.work), "clone", "-q", "--no-hardlinks", self.live, self.work)
            _git(self.work, "config", "user.name", "Pi Automation Traffic Tester (Claude)")
            _git(self.work, "config", "user.email", "claude@localhost")

    def pending(self) -> bool:
        return bool(_git(self.work, "status", "--porcelain").strip())

    def sync(self) -> None:
        """Bring the scratch copy up to the live app (only when nothing is waiting for approval)."""
        self.ensure()
        if self.pending():
            return
        _git(self.work, "fetch", "-q", "origin")
        branch = _git(self.live, "rev-parse", "--abbrev-ref", "HEAD").strip()
        _git(self.work, "reset", "-q", "--hard", f"origin/{branch}")

    def diff(self) -> dict:
        self.ensure()
        _git(self.work, "add", "-A")
        # --no-renames: a moved file shows as a delete + an add, so moving a protected file away is seen as touching it;
        # quotepath off: a name with special characters is listed as it is, not quoted (a quoted name never matched PROTECTED)
        names = [l for l in _git(self.work, "-c", "core.quotepath=false", "diff", "--cached", "--no-renames", "--name-status").splitlines() if l.strip()]
        files = [{"status": l.split("\t")[0], "path": l.split("\t")[-1].strip('"')} for l in names]
        patch = _git(self.work, "diff", "--cached", "--no-color", "-U3")
        stat = _git(self.work, "diff", "--cached", "--stat").strip().splitlines()
        blocked = [f["path"] for f in files if f["path"].startswith(PROTECTED)]
        return {"files": files, "patch": patch[:200_000], "truncated": len(patch) > 200_000, "stat": stat[-1] if stat else "", "blocked": blocked, "empty": not files}

    def discard(self) -> None:
        self.ensure()
        _git(self.work, "reset", "-q", "--hard", "HEAD")
        _git(self.work, "clean", "-qfd")

    def apply(self, message: str) -> dict:
        d = self.diff()
        if d["empty"]:
            raise WorkspaceError("There is nothing to apply.")
        if d["blocked"]:
            raise WorkspaceError("These changes touch the parts that keep Claude Code safe and cannot be applied from here: " + ", ".join(d["blocked"]))
        if _git(self.live, "status", "--porcelain").strip():
            raise WorkspaceError("The app folder has changes that are not saved in git. Save or undo them first, then apply.")
        patch = _git(self.work, "diff", "--cached", "--binary", "--no-color")
        _git(self.live, "apply", "--check", "-", input_=patch)
        _git(self.live, "apply", "--index", "-", input_=patch)
        _git(self.live, *AUTHOR, "commit", "-q", "-m", (message or "Change made by Claude")[:200])
        commit = _git(self.live, "rev-parse", "HEAD").strip()
        self.discard()
        self.sync()
        return {"commit": commit, "files": d["files"]}

    def history(self, n: int = 15) -> List[dict]:
        self.ensure()
        out = _git(self.live, "log", f"-{n}", "--format=%H%x09%an%x09%ad%x09%s", "--date=format:%Y-%m-%d %H:%M")
        rows = []
        for l in out.splitlines():
            h, who, when, subj = (l.split("\t") + ["", "", "", ""])[:4]
            rows.append({"commit": h, "short": h[:8], "who": who, "when": when, "subject": subj, "by_claude": who.startswith("Pi Automation Traffic Tester (Claude)"), "is_revert": subj.startswith("Revert")})
        return rows

    def revert(self, commit: str) -> dict:
        self.ensure()
        if not commit or not all(c in "0123456789abcdef" for c in commit.lower()) or len(commit) < 7:
            raise WorkspaceError("That is not a valid change id.")
        who = _git(self.live, "show", "-s", "--format=%an", commit).strip()
        if not who.startswith("Pi Automation Traffic Tester (Claude)"):
            raise WorkspaceError("Only changes made by Claude can be undone here.")
        if _git(self.live, "status", "--porcelain").strip():
            raise WorkspaceError("The app folder has changes that are not saved in git.")
        _git(self.live, *AUTHOR, "revert", "--no-edit", commit)
        self.discard()
        self.sync()
        return {"reverted": commit, "head": _git(self.live, "rev-parse", "HEAD").strip()}
