"""Command line for the same actions as the web app: status | login | logout. Run as the service user:
   sudo -u traffic-tester /opt/traffic-tester/venv/bin/python -m traffic_tester.claude.cli logout"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .auth import LoginFlow, logout
from .env import ClaudeEnv
from .remote import RemoteControl
from .state import ClaudeState


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tt-claude")
    ap.add_argument("action", choices=["status", "login", "logout"])
    ap.add_argument("--data-dir", default="/var/lib/traffic-tester")
    a = ap.parse_args(argv)
    env = ClaudeEnv(a.data_dir)
    st = ClaudeState(env)
    if a.action == "status":
        print(json.dumps({"enabled": st.enabled, "installed": bool(env.binary()), "logged_in": st.logged_in() if st.enabled else False}))
        return 0
    if a.action == "logout":
        r = logout(env, st, stop_remote=lambda: RemoteControl(st).stop())
        print(r["message"])
        for s in r["steps"]:
            print(" -", s)
        for h in r["leftovers"]:
            print(" ! still there:", h["path"], f"({h['what']})")
        return 0 if r["verified"] else 2
    f = LoginFlow(env, st)
    f.start()
    shown = False
    while f.status in ("starting", "waiting_for_code"):
        if f.url and not shown:
            print("\nOpen this link on your phone or PC and sign in:\n\n  " + f.url + "\n")
            code = input("Paste the code shown there: ")
            f.submit(code)
            shown = True
        time.sleep(0.5)
    print("Signed in. Claude Code is on." if f.status == "success" else f"Sign-in failed: {f.error}")
    return 0 if f.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
