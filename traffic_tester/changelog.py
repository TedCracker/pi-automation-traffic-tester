"""CHANGELOG.md is the single source for "what's new": the About page, the notes shown before an update installs and the release notes.

Format: `## <version> — <date>` headings (newest first), one `- ` bullet per change, plain English for a network engineer.
Bullets may use **bold**; a bullet can continue on indented lines."""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from .update.bundle import cmp_version, parse_version

HEAD = re.compile(r"^##\s+v?(\d+(?:\.\d+)*)\s*(?:[—–-]+\s*(\d{4}-\d{2}-\d{2}))?\s*$")


def parse(text: str) -> List[Dict]:
    """-> [{"version", "date", "notes": [bullet, ...]}] in file order (newest first). Raises ValueError for a version listed twice."""
    out: List[Dict] = []
    cur: Optional[Dict] = None
    for line in text.splitlines():
        m = HEAD.match(line.strip())
        if m:
            if any(e["version"] == m.group(1) for e in out):
                raise ValueError(f"CHANGELOG.md lists version {m.group(1)} twice.")
            cur = {"version": m.group(1), "date": m.group(2) or "", "notes": []}
            out.append(cur)
        elif cur is not None:
            s = line.rstrip()
            if s.lstrip().startswith("- "):
                cur["notes"].append(s.lstrip()[2:].strip())
            elif s.strip() and line.startswith((" ", "\t")) and cur["notes"]:
                cur["notes"][-1] += " " + s.strip()
    out.sort(key=lambda e: parse_version(e["version"]), reverse=True)
    return out


def load(app_root: str) -> List[Dict]:
    try:
        with open(os.path.join(app_root, "CHANGELOG.md"), encoding="utf-8") as f:
            return parse(f.read())
    except (OSError, ValueError):
        return []


def entry(entries: List[Dict], version: str) -> Optional[Dict]:
    return next((e for e in entries if e["version"] == version), None)


def newer_than(entries: List[Dict], installed: str, upto: Optional[str] = None) -> List[Dict]:
    """Entries after `installed` (and not after `upto`), newest first."""
    return [e for e in entries if cmp_version(e["version"], installed) > 0 and (upto is None or cmp_version(e["version"], upto) <= 0)]


def notes_text(e: Dict) -> str:
    """The entry as the release-notes string stored in the signed manifest."""
    return "\n".join("- " + n for n in e["notes"])
