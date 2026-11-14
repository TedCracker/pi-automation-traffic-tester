"""Claude Code integration, tested against tests/fake_claude.py (no Anthropic traffic). HOME is sandboxed so log-out can never touch a real login."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from traffic_tester.server import create_app          # noqa: E402
from traffic_tester.claude import scan as scanmod, tools as ctools     # noqa: E402
from traffic_tester.claude.env import OFF_MSG          # noqa: E402

FAKE = os.path.join(HERE, "fake_claude.py")
PW = "secret1"


def _live_app(root):
    for rel, txt in (("traffic_tester/__init__.py", "x=1\n"), ("traffic_tester/static/app.js", "// app\n"), ("traffic_tester/claude/__init__.py", ""),
                     ("scripts/install-pi.sh", "#!/bin/sh\n"), ("README.md", "hi\n")):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(txt)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    live = tmp_path / "live"
    _live_app(str(live))
    data = tmp_path / "data"
    data.mkdir()
    for k, v in {"HOME": str(home), "TT_CLAUDE_BIN": FAKE, "TT_CLAUDE_FAKE_ONLINE": "1", "TT_APP_DIR": str(live), "TT_RESTART_ON_APPLY": "0", "TT_WIPE_STRAY": "1",
                 "TT_SYSTEMCTL": str(tmp_path / "systemctl-stub"), "TT_RC_WAIT": "3"}.items():
        monkeypatch.setenv(k, v)
    # stand-in for systemd + the path unit: "active" exactly while state.json says Remote Control is wanted
    (tmp_path / "systemctl-stub").write_text("#!/bin/sh\npython3 - \"%s\" <<'PY'\nimport json,sys\ntry: d=json.load(open(sys.argv[1]))\nexcept Exception: d={}\nprint('active' if d.get('enabled') and d.get('remote_control') else 'inactive')\nPY\n" % (tmp_path / "data" / "claude" / "state.json"))
    os.chmod(tmp_path / "systemctl-stub", 0o755)
    monkeypatch.setattr(scanmod, "default_roots", lambda d: [d, str(home), str(live)])
    return {"home": str(home), "live": str(live), "data": str(data), "tmp": str(tmp_path)}


@pytest.fixture()
def app(env, monkeypatch):
    """The app under TestClient (for requests) AND on a real local port: the Claude tool server calls back over real HTTP, as it does on the Pi."""
    import socket
    import threading
    import uvicorn
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("TT_PORT", str(port))
    a = create_app(env["data"], "A")
    srv = uvicorn.Server(uvicorn.Config(a, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    with TestClient(a) as c:
        c.st = a.state.tt
        c.env = env
        c.pushed = []
        orig = c.st.push_ui
        c.st.push_ui = lambda ev: (c.pushed.append(ev), orig(ev))[1]       # the WebSocket broadcaster drains the queue; keep a copy for assertions
        yield c
    srv.should_exit = True
    th.join(5)


def sign_in(c, pw=None):
    """Drive the real login flow against the fake CLI; returns the final login info."""
    if pw:
        assert c.post("/api/auth/set", json={"password": pw}).status_code == 200
        assert c.post("/api/auth/login", json={"password": pw}).status_code == 200
    r = c.post("/api/claude/login/start")
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://")
    r = c.post("/api/claude/login/code", json={"code": "good-code"})
    return r.json()


def chat(c, msg):
    r = c.post("/api/claude/chat", json={"message": msg})
    assert r.status_code == 200, r.text
    return [json.loads(l) for l in r.text.splitlines() if l.strip()]


def enable(c):
    c.st.cstate.set(enabled=True)
    os.makedirs(c.st.cenv.config_dir, exist_ok=True)
    json.dump({"claudeAiOauth": {"accessToken": "x"}}, open(os.path.join(c.st.cenv.config_dir, ".credentials.json"), "w"))


# ------------------------------------------------------------------ off state
OFF_ENDPOINTS = [("post", "/api/claude/chat", {"message": "hi"}), ("post", "/api/claude/stop", None), ("post", "/api/claude/new", None), ("get", "/api/claude/changes", None),
                 ("post", "/api/claude/changes/apply", {}), ("post", "/api/claude/changes/discard", None), ("get", "/api/claude/history", None),
                 ("post", "/api/claude/changes/revert", {"commit": "abcdef1"}), ("post", "/api/claude/remote/start", None), ("post", "/api/claude/remote/stop", None),
                 ("post", "/api/claude/tool/get_screen", {}), ("post", "/api/claude/tool/start_test", {"profile_id": 1})]


def test_default_is_off_and_every_endpoint_refuses(app):
    s = app.get("/api/claude/status").json()
    assert s["enabled"] is False and s["message"] == "Claude Code is off." and "logged_in" not in s
    for m, p, body in OFF_ENDPOINTS:
        r = getattr(app, m)(p, **({"json": body} if body is not None else {}))
        assert r.status_code == 403 and r.json()["detail"] == OFF_MSG, (p, r.status_code, r.text)
    assert not app.st.runs.busy()


def test_screen_state_not_kept_while_off(app):
    app.post("/api/ui/state", json={"tab": "run"})
    assert app.st.ui_state == {}


def test_login_needs_web_password(app):
    r = app.post("/api/claude/login/start")
    assert r.status_code == 403 and "password" in r.json()["detail"].lower()
    assert app.get("/api/claude/status").json()["can_login"] is False


def test_login_endpoints_protected_by_password_and_logout_is_not(app):
    assert app.post("/api/auth/set", json={"password": PW}).status_code == 200
    app.cookies.clear()                                                     # not signed in to the web app
    assert app.post("/api/claude/login/start").status_code == 401
    assert app.post("/api/claude/login/code", json={"code": "x"}).status_code == 401
    assert app.get("/api/claude/changes").status_code == 401
    assert app.post("/api/claude/logout").status_code == 200                # sign-out never asks for the password
    assert app.get("/api/claude/status").status_code == 200


def test_full_login_turns_on_then_logout_turns_off(app):
    info = sign_in(app, PW)
    assert info["status"] == "success", info
    st = app.get("/api/claude/status").json()
    assert st["enabled"] and st["logged_in"] and st["password_set"]
    assert app.get("/api/claude/history").status_code == 200
    out = app.post("/api/claude/logout").json()
    assert out["off"] and out["verified"] and not out["leftovers"], out
    assert app.get("/api/claude/status").json()["enabled"] is False
    for m, p, body in OFF_ENDPOINTS:
        assert getattr(app, m)(p, **({"json": body} if body is not None else {})).status_code == 403, p


def test_bad_code_fails_cleanly(app):
    app.post("/api/auth/set", json={"password": PW})
    app.post("/api/auth/login", json={"password": PW})
    app.post("/api/claude/login/start")
    r = app.post("/api/claude/login/code", json={"code": "wrong"}).json()
    assert r["status"] == "failed" and r["error"]
    assert app.get("/api/claude/status").json()["enabled"] is False


# ------------------------------------------------------------------ log-out wipes every credential (verified by an independent scan)
def test_logout_wipes_planted_credentials_everywhere(app):
    sign_in(app, PW)
    home, live, data = app.env["home"], app.env["live"], app.env["data"]
    planted = [os.path.join(home, ".claude", ".credentials.json"), os.path.join(home, ".claude.json"), os.path.join(home, ".config", "claude", "auth.json")]
    for p in planted:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-PLANTEDTOKEN0123456789abcdef"}}))
    cfg = os.path.join(data, "claude", "config", ".credentials.json")
    assert os.path.isfile(cfg)
    assert scanmod.scan([home, data])                                            # the tokens really are there before
    out = app.post("/api/claude/logout").json()
    assert out["verified"] and out["leftovers"] == [], out
    assert not os.path.exists(cfg) and not any(os.path.exists(p) for p in planted)
    assert scanmod.scan([home, data, live, app.env["tmp"]]) == []                # independent scan: nothing token-like anywhere
    assert app.st.cstate.enabled is False


def test_logout_reports_leftovers_it_cannot_delete(app):
    sign_in(app, PW)
    stray = os.path.join(app.env["live"], "notes.txt")                           # not a place log-out is allowed to clean
    open(stray, "w").write("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-SOMETHINGLEFTBEHIND12345678\n")
    out = app.post("/api/claude/logout").json()
    assert out["off"] and not out["verified"] and any(h["path"] == stray for h in out["leftovers"])


def test_strip_token_lines_function(tmp_path):
    from traffic_tester.claude import auth
    f = tmp_path / "config.env"
    f.write_text("TT_PORT=8080\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-ABCDEFGHIJKLMNOP\nANTHROPIC_API_KEY=sk-ant-api03-zzzzzzzzzzzz\n")
    assert auth._strip_token_lines([str(f)])
    assert f.read_text() == "TT_PORT=8080\n"


def test_process_env_never_passes_api_keys(env, monkeypatch):
    from traffic_tester.claude.env import ClaudeEnv
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-nope")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-nope")
    e = ClaudeEnv(env["data"]).process_env()
    assert not [k for k in e if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))]
    assert e["HOME"].startswith(env["data"]) and e["CLAUDE_CONFIG_DIR"].startswith(env["data"])


# ------------------------------------------------------------------ chat (stubbed Claude)
def test_chat_success_and_screen_reading(app):
    enable(app)
    app.post("/api/ui/state", json={"tab": "run", "profile_name": "Trip test", "profile_id": 1})
    ev = chat(app, "scenario:explain what is on screen")
    kinds = [e["t"] for e in ev]
    assert kinds[-1] == "done" and "action" in kinds and "action_result" in kinds
    acts = [e for e in ev if e["t"] == "action"]
    assert acts[0]["name"] == "get_screen"
    assert any("run tab with profile Trip test" in e.get("text", "") for e in ev if e["t"] == "text")
    res = [e for e in ev if e["t"] == "action_result"][0]
    assert res["ok"] and '"tab": "run"' in res["text"]                          # Claude really read the app state through MCP


def test_chat_start_and_stop_test_updates_ui(app):
    enable(app)
    import socket
    from traffic_tester import engine
    from traffic_tester.transports import UdpTransport
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    resp = engine.Responder(UdpTransport("responder", "", port), 0, 0, hooks_enabled=True)
    resp.start()
    try:
        pid = [p for p in app.get("/api/profiles").json() if p.get("kind", "trip") == "trip"][0]["id"]
        ev = chat(app, f"scenario:start profile {pid} duration 20 peer 127.0.0.1:{port}")
        res = [e for e in ev if e["t"] == "action_result"][0]
        assert res["ok"] and '"ok": true' in res["text"], res
        assert app.st.runs.busy()                                              # a real test is running because the chat asked for it
        started = [e for e in app.pushed if e.get("cmd") == "run_started"]
        assert started and started[0]["tab"] == "run"                          # the left-hand screen is told to switch to the live view
        assert any("started" in l for l in app.st.applog.tail(20))
        ev2 = chat(app, "scenario:stop now")
        assert [e for e in ev2 if e["t"] == "action"][0]["name"] == "stop_test"
        for _ in range(100):
            if not app.st.runs.busy():
                break
            time.sleep(0.1)
        assert not app.st.runs.busy()
    finally:
        resp.stop()


def test_chat_changes_a_setting_via_save_profile(app):
    enable(app)
    pid = app.get("/api/profiles").json()[0]["id"]
    ev = chat(app, f"scenario:setting profile {pid}")
    assert any(e["t"] == "action" and e["name"] == "save_profile" for e in ev)
    mine = [p for p in app.get("/api/profiles").json() if p["name"] == "Claude tuned"]
    assert mine and mine[0]["threshold_ms"] == 33 and not mine[0]["builtin"]
    assert any(e.get("cmd") == "profile_saved" for e in app.pushed)


def test_chat_open_screen_pushes_ui_command(app):
    enable(app)
    chat(app, "scenario:open results")
    assert any(e.get("cmd") == "open" and e.get("tab") == "results" for e in app.pushed)


def test_chat_tool_error_is_reported_as_failed_action(app):
    enable(app)
    ev = chat(app, "scenario:bad-tool")
    r = [e for e in ev if e["t"] == "action_result"][0]
    assert r["ok"] is False


def test_chat_not_signed_in_message(app):
    enable(app)
    os.remove(os.path.join(app.st.cenv.config_dir, ".credentials.json"))
    ev = chat(app, "hello")
    assert ev[-1]["t"] == "error" and ev[-1]["code"] == "not_signed_in" and "not signed in" in ev[-1]["message"]
    assert app.get("/api/status").status_code == 200                           # the tester itself is unaffected


def test_chat_expired_token_message(app):
    enable(app)
    app.st.runner._li_cache = (0.0, False)
    ev = chat(app, "scenario:expired")
    assert ev[-1]["t"] == "error" and ev[-1]["code"] == "not_signed_in"


def test_chat_offline_message(app, monkeypatch):
    enable(app)
    monkeypatch.setenv("TT_CLAUDE_FAKE_ONLINE", "0")
    ev = chat(app, "hello")
    assert ev == [{"t": "error", "code": "offline", "message": ev[0]["message"]}] and "Wi-Fi" in ev[0]["message"]
    monkeypatch.setenv("TT_CLAUDE_FAKE_ONLINE", "1")
    ev = chat(app, "scenario:offline")                                          # network drops mid-request
    assert ev[-1]["t"] == "error" and ev[-1]["code"] == "offline"


def test_chat_not_installed_message(app, monkeypatch):
    enable(app)
    monkeypatch.setattr("traffic_tester.claude.env.ClaudeEnv.binary", lambda self: None)
    ev = chat(app, "hello")
    assert ev[-1]["code"] == "not_installed"


def test_chat_runs_low_priority_in_scratch_dir_with_restricted_flags(app):
    enable(app)
    ev = chat(app, "scenario:prio")
    t = " ".join(e.get("text", "") for e in ev if e["t"] == "text")
    assert "nice=19" in t and "SCHED_IDLE" in t
    assert os.path.join(app.env["data"], "claude", "work") in t and os.path.join(app.env["data"], "claude", "home") in t
    args = json.loads(" ".join(e.get("text", "") for e in chat(app, "scenario:args") if e["t"] == "text").split("args=", 1)[1])
    for flag in ("--restricted", "--strict-mcp-config", "--output-format"):
        assert flag in args
    assert "--dangerously-skip-permissions" not in args and "Bash" not in " ".join(args[args.index("--allowedTools"):args.index("--append-system-prompt")])


def test_chat_busy_and_stop(app):
    enable(app)
    import threading
    out = []
    th = threading.Thread(target=lambda: out.append(chat(app, "scenario:slow")))
    th.start()
    for _ in range(50):
        if app.st.runner.proc:
            break
        time.sleep(0.1)
    ev = chat(app, "second message")
    assert ev[-1]["code"] == "busy"
    assert app.post("/api/claude/stop").status_code == 200
    th.join(15)
    assert not th.is_alive() and out[0][-1]["code"] == "stopped"


def test_chat_timeout(app):
    enable(app)
    app.st.runner.timeout_s = 2
    t0 = time.time()
    ev = chat(app, "scenario:slow")
    assert time.time() - t0 < 20 and ev[-1]["t"] == "error"


def test_asking_claude_does_not_block_or_slow_the_app(app):
    enable(app)
    import threading
    th = threading.Thread(target=lambda: chat(app, "scenario:slow"))
    th.start()
    time.sleep(1.0)
    t0 = time.time()
    for _ in range(10):
        assert app.get("/api/status").status_code == 200
    assert time.time() - t0 < 5
    app.post("/api/claude/stop")
    th.join(15)


def test_tool_endpoint_needs_token_and_enabled(app):
    enable(app)
    assert app.post("/api/claude/tool/get_screen", json={}).status_code == 403
    assert app.post("/api/claude/tool/get_screen", json={}, headers={"X-TT-Token": "wrong"}).status_code == 403
    assert app.post("/api/claude/tool/get_screen", json={}, headers={"X-TT-Token": app.st.ctoken}).status_code == 200
    assert app.post("/api/claude/tool/rm_rf", json={}, headers={"X-TT-Token": app.st.ctoken}).status_code == 404


# ------------------------------------------------------------------ feature changes: diff, apply, revert, protected paths
def test_change_flow_diff_apply_revert(app):
    enable(app)
    ev = chat(app, "scenario:feature")
    done = ev[-1]
    assert done["t"] == "done" and done["changes"] and "traffic_tester/static/claude-added.txt" in done["changed_files"]
    d = app.get("/api/claude/changes").json()
    assert not d["empty"] and "claude-added.txt" in d["patch"] and not d["blocked"]
    assert app.post("/api/claude/changes/discard").status_code == 200 and app.get("/api/claude/changes").json()["empty"]
    chat(app, "scenario:feature")
    r = app.post("/api/claude/changes/apply", json={"message": "Add file"})
    assert r.status_code == 200 and r.json()["commit"]
    assert os.path.isfile(os.path.join(app.env["live"], "traffic_tester/static/claude-added.txt"))
    h = app.get("/api/claude/history").json()
    assert h[0]["by_claude"] and h[0]["subject"] == "Add file"
    assert app.post("/api/claude/changes/revert", json={"commit": h[0]["commit"]}).status_code == 200
    assert not os.path.exists(os.path.join(app.env["live"], "traffic_tester/static/claude-added.txt"))
    assert app.get("/api/claude/history").json()[0]["is_revert"]


def test_cannot_revert_a_change_not_made_by_claude(app):
    enable(app)
    first = app.get("/api/claude/history").json()[-1]["commit"]
    assert app.post("/api/claude/changes/revert", json={"commit": first}).status_code == 409


def test_protected_files_cannot_be_applied(app):
    enable(app)
    ev = chat(app, "scenario:protected")
    d = app.get("/api/claude/changes").json()
    assert d["blocked"] == ["traffic_tester/claude/evil.py"]
    r = app.post("/api/claude/changes/apply", json={})
    assert r.status_code == 409 and "keep Claude Code safe" in r.json()["detail"]
    assert not os.path.exists(os.path.join(app.env["live"], "traffic_tester/claude/evil.py"))


def test_apply_refused_while_a_test_runs(app, monkeypatch):
    enable(app)
    chat(app, "scenario:feature")
    monkeypatch.setattr(app.st.runs, "busy", lambda: True)
    r = app.post("/api/claude/changes/apply", json={})
    assert r.status_code == 409 and "test is running" in r.json()["detail"]


# ------------------------------------------------------------------ MCP server speaks JSON-RPC and forwards to the app
def test_mcp_server_handshake_and_tool_list():
    p = subprocess.Popen([sys.executable, "-m", "traffic_tester.claude.mcp_server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                         cwd=os.path.dirname(HERE), env={**os.environ, "TT_API": "http://127.0.0.1:9", "TT_TOKEN": "t"})
    def rpc(i, m, params=None):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": params or {}}) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())
    assert rpc(1, "initialize", {"protocolVersion": "2024-11-05"})["result"]["serverInfo"]["name"] == "tt"
    names = [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]]
    assert {"get_screen", "start_test", "stop_test", "save_profile", "open_screen", "get_run"} <= set(names)
    r = rpc(3, "tools/call", {"name": "get_screen", "arguments": {}})["result"]              # app not reachable: a plain error, not a crash
    assert r["isError"] and "not reachable" in r["content"][0]["text"]
    assert "error" in rpc(4, "nope")
    p.stdin.close()
    p.wait(5)


def test_tool_specs_cover_every_toolbox_method():
    for t in ctools.TOOL_SPECS:
        assert hasattr(ctools.ToolBox, t["name"]), t["name"]


# ------------------------------------------------------------------ app log + network check API
def test_app_log_records_run_lifecycle_and_is_readable(app):
    enable(app)
    logging_lines = app.st.applog.tail(50)
    assert any("started" in l for l in logging_lines)
    r = app.post("/api/claude/tool/get_log", json={"lines": 20}, headers={"X-TT-Token": app.st.ctoken}).json()
    assert isinstance(r["lines"], list) and r["lines"]


def test_net_check_api_shape(app):
    j = app.get("/api/net/check").json()
    assert j["status"] in ("ok", "warn", "bad") and isinstance(j["checks"], list)


# ------------------------------------------------------------------ Remote Control (state-file design: the web app never needs privileges)
def test_remote_control_start_stop_and_logout_disables_it(app):
    sign_in(app, PW)
    assert app.get("/api/claude/status").json()["remote"]["active"] is False
    r = app.post("/api/claude/remote/start")
    assert r.status_code == 200 and r.json()["active"] is True
    assert json.load(open(os.path.join(app.st.cenv.base, "state.json")))["remote_control"] is True
    assert app.get("/api/claude/status").json()["remote"]["wanted"] is True
    assert app.post("/api/claude/remote/stop").json()["active"] is False
    app.post("/api/claude/remote/start")
    out = app.post("/api/claude/logout").json()
    st = json.load(open(os.path.join(app.st.cenv.base, "state.json")))
    assert st["remote_control"] is False and st["enabled"] is False                 # tt-rc-run / the path unit stop the service from this
    assert any("Remote Control" in s for s in out["steps"]) and out["verified"]
    assert app.st.rc.status()["active"] is False


def test_remote_control_refused_when_not_signed_in_or_not_installed(app, monkeypatch):
    enable(app)
    os.remove(os.path.join(app.st.cenv.config_dir, ".credentials.json"))
    r = app.post("/api/claude/remote/start")
    assert r.status_code == 409 and "not signed in" in r.json()["detail"].lower()
    sign_in_again = None
    monkeypatch.setenv("TT_SYSTEMCTL", "/nonexistent/systemctl")
    json.dump({"claudeAiOauth": {"accessToken": "x"}}, open(os.path.join(app.st.cenv.config_dir, ".credentials.json"), "w"))
    r = app.post("/api/claude/remote/start")
    assert r.status_code == 409 and "not set up" in r.json()["detail"]


def test_rc_scripts_only_start_when_wanted(tmp_path):
    """tt-rc-sync (root, run by the systemd path unit) starts the unit only when enabled AND remote_control are both true; otherwise stops it."""
    log = tmp_path / "calls"
    stub = tmp_path / "systemctl"
    stub.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
    stub.chmod(0o755)
    base = tmp_path / "claude"
    base.mkdir()
    script = os.path.join(os.path.dirname(HERE), "scripts", "tt-rc-sync")
    for state, want in (({"enabled": True, "remote_control": True}, "start"), ({"enabled": True, "remote_control": False}, "stop"), ({"enabled": False, "remote_control": True}, "stop"), (None, "stop")):
        if state is None:
            (base / "state.json").unlink(missing_ok=True)
        else:
            (base / "state.json").write_text(json.dumps(state))
        log.write_text("")
        subprocess.run(["bash", script], env={**os.environ, "TT_CLAUDE_BASE": str(base), "TT_SYSTEMCTL": str(stub)}, check=True)
        assert log.read_text().split()[0] == want, (state, log.read_text())


def test_rc_run_refuses_to_start_when_off(tmp_path):
    base = tmp_path / "claude"
    base.mkdir()
    (base / "state.json").write_text(json.dumps({"enabled": False, "remote_control": True}))
    r = subprocess.run(["bash", os.path.join(os.path.dirname(HERE), "scripts", "tt-rc-run")], env={**os.environ, "TT_CLAUDE_BASE": str(base), "TT_RC_SOCK": str(tmp_path / "s")}, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and "off" in r.stderr and not (tmp_path / "s").exists()


def test_logout_before_any_sign_in_still_runs_the_cli_signout(app):
    """Regression (found on arm64 with the real CLI): the CLI runs inside <data>/claude, which did not exist before the first sign-in."""
    assert not os.path.isdir(app.st.cenv.base) or True
    shutil.rmtree(app.st.cenv.base, ignore_errors=True)
    out = app.post("/api/claude/logout").json()
    assert out["off"] and out["verified"] and any("signed out with the Claude CLI" in s for s in out["steps"]), out


def test_cancelling_a_sign_in_does_not_crash_the_reader_thread(app, recwarn):
    import threading
    errs = []
    old = threading.excepthook
    threading.excepthook = lambda a: errs.append(a)
    try:
        app.post("/api/auth/set", json={"password": PW}); app.post("/api/auth/login", json={"password": PW})
        app.post("/api/claude/login/start")
        app.post("/api/claude/login/cancel")
        time.sleep(1.0)
    finally:
        threading.excepthook = old
    assert not errs, errs
