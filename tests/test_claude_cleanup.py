"""Ask Claude was removed in 1.7.0: the routes are gone and the one-time cleanup removes what older versions left on a Pi."""
import os
import tempfile

from fastapi.testclient import TestClient

from traffic_tester.server import create_app
from traffic_tester.update import claude_cleanup as cc


def _tree():
    root = tempfile.mkdtemp()
    etc, data, app = f"{root}/etc", f"{root}/data", f"{root}/opt/app"
    for d in (f"{etc}/systemd/system", f"{etc}/apt/sources.list.d", f"{etc}/apt/keyrings", f"{data}/claude/config", f"{app}/.git", f"{app}/scripts", f"{app}/traffic_tester"):
        os.makedirs(d)
    for f in ("traffic-tester-claude-rc.path", "traffic-tester-claude-rc.service", "traffic-tester-claude-rc-sync.service"):
        open(f"{etc}/systemd/system/{f}", "w").write("x")
    open(f"{etc}/apt/sources.list.d/claude-code.list", "w").write("deb x")
    open(f"{etc}/apt/keyrings/claude-code.asc", "w").write("k")
    open(f"{data}/claude/config/creds", "w").write("secret")
    open(f"{data}/traffic-tester.db", "w").write("MY SAVED TESTS")
    for f in ("scripts/tt-rc-run", "scripts/tt-rc-sync", "scripts/net-split.sh", ".gitignore"):
        open(f"{app}/{f}", "w").write("x")
    open(f"{root}/opt/install-claude.sh", "w").write("x")
    return etc, data, app


def test_cleanup_removes_everything_and_keeps_user_data():
    etc, data, app = _tree()
    assert len(cc.leftovers(data, etc, app)) >= 8
    log = []
    out = cc.cleanup(data, app, etc, run=lambda a: 0, log=log.append)
    assert len(out) == len(log) >= 8
    assert cc.leftovers(data, etc, app) == []
    assert not os.path.exists(f"{data}/claude") and not os.path.exists(f"{app}/.git")
    assert open(f"{data}/traffic-tester.db").read() == "MY SAVED TESTS"          # test data is never touched
    assert os.path.exists(f"{app}/scripts/net-split.sh")                          # the network-split helper stays
    assert cc.cleanup(data, app, etc, run=lambda a: 0, log=log.append) == []      # idempotent


def test_claude_routes_and_screens_are_gone():
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        for m, p in (("get", "/api/claude/status"), ("post", "/api/claude/chat"), ("post", "/api/claude/login/start"), ("post", "/api/claude/remote/start"), ("post", "/api/ui/state")):
            assert getattr(c, m)(p).status_code in (404, 405), p
        assert "claude.js" not in c.get("/").text and "askBtn" not in c.get("/").text and c.get("/claude.js").status_code == 404
        assert "Ask Claude" not in c.get("/ai-guide").text
