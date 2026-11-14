#!/usr/bin/env python3
"""Probe the REAL `claude` CLI the way the Pi Automation Traffic Tester uses it (no sign-in needed, nothing is sent from your account).
Run as the service user on the Pi or in an arm64 container:
  sudo -u traffic-tester env PYTHONPATH=/opt/traffic-tester/app /opt/traffic-tester/venv/bin/python /opt/traffic-tester/app/scripts/claude_real_probe.py [--data-dir DIR]"""
import argparse, os, sys, tempfile, time, subprocess, json

ap = argparse.ArgumentParser(); ap.add_argument("--data-dir", default=None); a = ap.parse_args()
from traffic_tester.claude.env import ClaudeEnv
from traffic_tester.claude.state import ClaudeState
from traffic_tester.claude.auth import LoginFlow
from traffic_tester.claude.runner import ChatRunner, NOT_SIGNED_IN
from traffic_tester.claude.workspace import Workspace

data = a.data_dir or tempfile.mkdtemp(prefix="tt-probe-")
env = ClaudeEnv(data, live_dir=os.environ.get("TT_APP_DIR") or os.getcwd()); env.ensure_dirs()
st = ClaudeState(env)
res = []
def check(name, ok, detail=""):
    res.append(ok); print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)

b = env.binary()
check("claude binary found", bool(b), str(b))
if not b: sys.exit(1)
v = st.version(); check("claude --version works", bool(v), str(v)); print("   version:", v)
h = st.cli("--help").stdout
for flag in ("--output-format", "--verbose", "--restricted", "--strict-mcp-config", "--mcp-config", "--permission-mode", "--allowedTools", "--append-system-prompt", "--session-id", "--resume"):
    check(f"CLI accepts {flag}", flag in h, "missing in --help")
sub = st.cli("auth", "--help").stdout
check("`claude auth` has login/logout/status", all(x in sub for x in ("login", "logout", "status")), sub[:200])
check("`claude auth login --claudeai` is a known option", "--claudeai" in st.cli("auth", "login", "--help").stdout)
check("`claude remote-control` exists", "remote-control" in h or "Remote Control" in st.cli("remote-control", "--help").stdout)
r = st.cli("auth", "status", "--json")
try: j = json.loads(r.stdout or "{}")
except ValueError: j = {}
check("auth status --json reports loggedIn=false before sign-in", j.get("loggedIn") is False, r.stdout[:200] + r.stderr[:200])
check("logged_in() agrees", st.logged_in() is False)
# the sign-in flow: the app must be able to show the link (needs internet; nothing is signed in)
f = LoginFlow(env, st); f.start()
for _ in range(60):
    if f.url or f.status in ("failed", "success"): break
    time.sleep(0.5)
check("login flow produced a sign-in link for the app to show", bool(f.url and f.url.startswith("https://")), f"{f.status} {f.error} {f.output[-300:]!r}")
print("   link host:", (f.url or "").split("/")[2] if f.url else None)
f.cancel()
# asking while not signed in must produce the plain message, not a crash
os.environ["TT_CLAUDE_FAKE_ONLINE"] = "1"
ws = Workspace(env.live_dir, env.work_dir)
ev = list(ChatRunner(env, st, ws, "tok", timeout_s=60).stream("hello"))
check("chat while not signed in -> plain 'not signed in' message", ev and ev[-1].get("t") == "error" and ev[-1].get("code") == "not_signed_in", str(ev)[-300:])
pe = env.process_env()
check("process env carries no API keys", not [k for k in pe if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))])
print(f"\n{sum(res)}/{len(res)} probe checks passed"); sys.exit(0 if all(res) else 1)
