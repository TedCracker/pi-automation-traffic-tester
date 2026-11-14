"""Find anything that still looks like a Claude credential after log-out (tokens, OAuth JSON, setup-token env files)."""
from __future__ import annotations

import os
import re
import time
from typing import Iterable, List, Optional

TOKEN_RE = re.compile(rb"sk-ant-(?:oat|ort|api|sid)\d*-[A-Za-z0-9_\-]{8,}")
KEY_RE = re.compile(rb'"(?:accessToken|refreshToken|claudeAiOauth|oauthAccount)"\s*:\s*[\{"]')
ENV_RE = re.compile(rb"CLAUDE_CODE_OAUTH_TOKEN\s*=\s*\S+|ANTHROPIC_(?:API_KEY|AUTH_TOKEN)\s*=\s*\S+")
SKIP_DIRS = {"proc", "sys", "dev", "node_modules", ".git", "__pycache__", "venv", ".venv", "claude-install-cache"}


def default_roots(data_dir: str) -> List[str]:
    if os.environ.get("TT_SCAN_ROOTS"):                       # tests / unusual layouts: scan exactly these (plus the data dir)
        return [data_dir] + [r for r in os.environ["TT_SCAN_ROOTS"].split(os.pathsep) if r]
    roots = [data_dir, "/etc/traffic-tester", "/etc/systemd/system", "/opt/traffic-tester", "/var/lib/traffic-tester", "/tmp", "/var/tmp", os.path.expanduser("~")]
    try:
        roots += [os.path.join("/home", u) for u in os.listdir("/home")]
    except OSError:
        pass
    roots.append("/root")
    seen, out = set(), []
    for r in roots:
        rp = os.path.realpath(r)
        if rp not in seen and os.path.exists(rp):
            seen.add(rp)
            out.append(rp)
    return out


def scan(roots: Iterable[str], max_seconds: float = 40.0, max_file: int = 4 * 1024 * 1024, only_names: Optional[str] = None) -> List[dict]:
    """Walk the roots; return [{'path', 'what'}] for every file holding a token-like string or credential JSON key."""
    t0 = time.monotonic()
    hits: List[dict] = []
    done = set()
    for root in roots:
        for dp, dns, fns in os.walk(root, followlinks=False):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            for fn in fns:
                if time.monotonic() - t0 > max_seconds:
                    return hits
                p = os.path.join(dp, fn)
                rp = os.path.realpath(p)
                if rp in done or os.path.islink(p):
                    continue
                done.add(rp)
                try:
                    if os.path.getsize(p) > max_file or os.path.getsize(p) == 0:
                        continue
                    with open(p, "rb") as f:
                        b = f.read()
                except OSError:
                    continue
                for rx, what in ((TOKEN_RE, "token"), (KEY_RE, "oauth credential"), (ENV_RE, "token in environment file")):
                    if rx.search(b):
                        hits.append({"path": p, "what": what})
                        break
    return hits
