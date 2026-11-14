#!/usr/bin/env python3
"""Stand-in for the `claude` CLI used by the tests (never talks to Anthropic). Behaves like the real one where the app depends on it:
`auth status --json`, `auth login --claudeai` (prints a link, waits for a pasted code), `auth logout`, and `-p ... --output-format stream-json`.
Credentials are a planted fake token under CLAUDE_CONFIG_DIR / HOME so the log-out tests can prove they are wiped. The prompt picks the scenario.
For tool use it starts the REAL MCP server from --mcp-config and speaks JSON-RPC to it, so the whole path to the app is exercised."""
import json
import os
import re
import subprocess
import sys
import time

CFG = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.environ.get("HOME", "."), ".claude")
HOME = os.environ.get("HOME", ".")
CRED = os.path.join(CFG, ".credentials.json")
FAKE_TOKEN = "sk-ant-oat01-FAKETOKENFORTESTS0123456789abcdef"


def logged_in() -> bool:
    return os.path.isfile(CRED)


def emit(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


def auth(argv):
    if argv[:1] == ["status"]:
        print(json.dumps({"loggedIn": logged_in(), "authMethod": "claude.ai" if logged_in() else None}))
        return 0
    if argv[:1] == ["logout"]:
        try:
            os.remove(CRED)
        except OSError:
            pass
        print("Logged out")
        return 0
    if argv[:1] == ["login"]:
        print("Opening browser to sign in…")
        print("If the browser did not open, visit: https://claude.ai/oauth/authorize?code=true&client_id=FAKE&state=abc123")
        print("Paste code here if prompted > ", end="", flush=True)
        code = sys.stdin.readline().strip()
        if code != "good-code":
            print("\nInvalid code")
            return 1
        os.makedirs(CFG, exist_ok=True)
        with open(CRED, "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": FAKE_TOKEN, "refreshToken": "sk-ant-ort01-FAKEREFRESH0123456789abcdef", "expiresAt": 9999999999999}}, f)
        with open(os.path.join(HOME, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"emailAddress": "someone@example.com"}}, f)
        print("\nLogin successful")
        return 0
    return 2


class Mcp:
    def __init__(self, cfg_path):
        cfg = json.load(open(cfg_path))["mcpServers"]["tt"]
        env = {**os.environ, **cfg.get("env", {})}
        self.p = subprocess.Popen([cfg["command"], *cfg.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fake", "version": "0"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.p.stdin.flush()

    def rpc(self, method, params):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def call(self, name, args):
        r = self.rpc("tools/call", {"name": name, "arguments": args}).get("result", {})
        return "".join(c.get("text", "") for c in r.get("content", [])), bool(r.get("isError"))

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=3)
        except Exception:
            self.p.kill()


def use_tool(mcp, name, args, n=[0]):
    n[0] += 1
    tid = f"toolu_{n[0]}"
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": f"mcp__tt__{name}", "input": args}]}})
    text, err = mcp.call(name, args)
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}], "is_error": err}]}})
    return text, err


def say(t):
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}})


def prompt_mode(argv):
    msg = argv[argv.index("-p") + 1]
    mcp_path = argv[argv.index("--mcp-config") + 1] if "--mcp-config" in argv else None
    low = msg.lower()
    emit({"type": "system", "subtype": "init", "session_id": "fake-session", "model": "fake"})
    if not logged_in():
        emit({"type": "result", "subtype": "error", "is_error": True, "result": "Not logged in · Please run /login"})
        return 1
    if "scenario:offline" in low:
        sys.stderr.write("Error: getaddrinfo ENOTFOUND api.anthropic.com\n")
        return 1
    if "scenario:expired" in low:
        emit({"type": "result", "subtype": "error", "is_error": True, "result": "OAuth token has expired. Please run /login"})
        return 1
    if "scenario:slow" in low:
        time.sleep(60)
        return 0
    if "scenario:prio" in low:
        pol = os.sched_getscheduler(0)
        say(f"nice={os.nice(0)} policy={'SCHED_IDLE' if pol == getattr(os, 'SCHED_IDLE', -1) else pol} cwd={os.getcwd()} home={HOME}")
        emit({"type": "result", "is_error": False, "result": "ok"})
        return 0
    if "scenario:args" in low:
        say("args=" + json.dumps(argv))
        emit({"type": "result", "is_error": False, "result": "ok"})
        return 0
    mcp = Mcp(mcp_path) if mcp_path else None
    try:
        if "scenario:explain" in low:
            say("Let me look at your screen.")
            screen, _ = use_tool(mcp, "get_screen", {})
            ui = json.loads(screen).get("ui", {})
            m = re.search(r"run (\d+)", low)
            if m:
                use_tool(mcp, "get_run", {"id": int(m.group(1))})
            say(f"You are on the {ui.get('tab')} tab with profile {ui.get('profile_name')}.")
        elif "scenario:start" in low:
            pid = int(re.search(r"profile (\d+)", low).group(1))
            dur = int(re.search(r"duration (\d+)", low).group(1)) if "duration" in low else 5
            ov = {"duration_s": dur}
            m = re.search(r"peer (\S+):(\d+)", low)
            if m:
                ov.update({"transport": "udp", "interval_ms": 20, "net": {"peer_host": m.group(1), "port": int(m.group(2))}})
            text, err = use_tool(mcp, "start_test", {"profile_id": pid, "overrides": ov})
            say("Started." if not err and '"ok": true' in text else f"Could not start: {text}")
        elif "scenario:stop" in low:
            use_tool(mcp, "stop_test", {})
            say("Stopped the test.")
        elif "scenario:setting" in low:
            pid = int(re.search(r"profile (\d+)", low).group(1))
            prof = json.loads(use_tool(mcp, "get_profile", {"id": pid})[0])
            prof.pop("id", None)
            prof["name"] = "Claude tuned"
            prof["threshold_ms"] = 33
            prof["builtin"] = False
            text, err = use_tool(mcp, "save_profile", {"profile": prof})
            say("Saved a profile with a 33 ms threshold." if not err else f"Could not save: {text}")
        elif "scenario:open" in low:
            use_tool(mcp, "open_screen", {"tab": "results"})
            say("Opened Results.")
        elif "scenario:feature" in low:
            os.makedirs("traffic_tester/static", exist_ok=True)
            with open("traffic_tester/static/claude-added.txt", "w") as f:
                f.write("added by the fake claude\n")
            say("I added a small file. Review the change and apply it if you like.")
        elif "scenario:protected" in low:
            os.makedirs("traffic_tester/claude", exist_ok=True)
            with open("traffic_tester/claude/evil.py", "w") as f:
                f.write("# tries to weaken the guard\n")
            say("Done.")
        elif "scenario:bad-tool" in low:
            text, err = use_tool(mcp, "start_test", {"profile_id": 99999})
            say("That profile does not exist." if err or "error" in text else "started?")
        else:
            say("Hello from the fake Claude.")
    finally:
        if mcp:
            mcp.close()
    emit({"type": "result", "is_error": False, "result": "done", "session_id": "fake-session"})
    return 0


def main():
    a = sys.argv[1:]
    if a[:1] == ["--version"]:
        print("2.1.0 (Claude Code, fake)")
        return 0
    if a[:1] == ["auth"]:
        return auth(a[1:])
    if "-p" in a:
        return prompt_mode(a)
    if a[:1] == ["remote-control"]:
        time.sleep(3600)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
