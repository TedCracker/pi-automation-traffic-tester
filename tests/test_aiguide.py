"""AI guide: coverage (fails when the app grows something the guide does not describe), no secrets, ONE guide for every Pi (no roles), live state, login rule, read-only."""
import json
import re
import tempfile

import pytest
from fastapi.testclient import TestClient

from traffic_tester import profiles as P
from traffic_tester.aiguide import build as B, content as K
from traffic_tester.aiguide.model import to_html, to_text
from traffic_tester.server import create_app


@pytest.fixture()
def app_a():
    app = create_app(tempfile.mkdtemp())
    with TestClient(app) as c:
        yield app, c


class _Words:
    """The old fixed roles: nowhere in the guide. (The two older API aliases /api/responder/start|stop are real route paths and may be listed.)"""
    rx = re.compile(r"\bPi [AB]\b|Responder|responder|(?<!Traffic )\bTester\b|\bPi A/B\b")

    def findall(self, t):
        return self.rx.findall(t.replace("/api/responder/", "/api/reflector-alias/"))

    def search(self, t):
        return self.rx.search(t.replace("/api/responder/", "/api/reflector-alias/"))


ROLE_WORDS = _Words()


def text_of(app, authed=True):
    return to_text(B.build_doc(app.state.tt, app, authed))


# ---------------------------------------------------------------------------------------------- coverage
def test_every_tab_in_the_page_has_a_view_and_a_summary():
    tabs = dict(B.tab_labels())
    assert len(tabs) >= 6          # Setup, Run, Results, Generator, Sniffer, Settings (Monitor is part of Run since 2026-11)
    view_tabs = {v["tab"] for v in K.VIEWS.values()}
    for key, label in tabs.items():
        assert key in view_tabs, f"tab '{label}' has no view in aiguide/content.py VIEWS"
        assert key in K.VIEWS_BY_TAB_SUMMARY, f"tab '{label}' has no one-line summary"


def test_new_tab_without_words_fails_the_guide(app_a, monkeypatch):
    app, _ = app_a
    monkeypatch.setattr(B, "tab_labels", lambda: [("setup", "① Setup"), ("brandnew", "⑦ Brand new")])
    with pytest.raises(B.GuideCoverageError, match="brandnew"):
        B.check_coverage(app)


def test_every_preset_kind_transport_has_text_and_a_recipe(app_a):
    app, _ = app_a
    txt = text_of(app)
    ids = {r["id"] for r in K.RECIPES}
    for p in P.builtin_presets():
        assert p["name"] in txt, f"preset '{p['name']}' is not in the guide"
        assert B.recipe_for_preset(p) in ids
    for k in B.profile_kinds():
        assert k in K.KIND_ABOUT
    for t in B.transports():
        assert t in K.TRANSPORT_ABOUT and t in K.RECIPE_FOR_TRANSPORT


def test_new_preset_with_unknown_transport_fails(monkeypatch):
    fake = {"name": "Future test", "transport": "lora", "kind": "trip"}
    with pytest.raises(B.GuideCoverageError, match="Future test"):
        B.recipe_for_preset(fake)


def test_every_route_is_described_and_listed(app_a):
    app, _ = app_a
    txt = text_of(app)
    for m, path in B.routes(app):
        assert f"{m} {path}" in K.RN, f"route {m} {path} has no description in aiguide/content.py RN"
        assert re.search(rf"^{m} \| {re.escape(path)} \|", txt, re.M), f"{m} {path} missing from the guide"
    assert len(B.routes(app)) >= 70


def test_new_route_without_description_fails(app_a):
    app, _ = app_a

    @app.get("/api/brand-new-thing")
    def _x():
        return {}
    with pytest.raises(B.GuideCoverageError, match="brand-new-thing"):
        B.build_doc(app.state.tt, app, True)
    with pytest.raises(B.GuideCoverageError):
        B.check_coverage(app)


def test_recipes_only_use_registered_controls():
    """Every control a recipe tells the agent to use is also in the screen/control tables (same id, same label)."""
    reg = {t: l for v in K.VIEWS.values() for t, l, _ in v["controls"]}
    reg.update({t: l for t, l, _ in K.TOP_CONTROLS})
    tabs = dict(B.tab_labels())
    for r in K.RECIPES:
        for s in r["steps"]:
            if s["do"] == "goto":
                assert s["tab"] in tabs, (r["id"], s)
            elif s["do"] in ("click", "type", "choose"):
                tid = s["tid"]
                match = tid in reg or any(re.fullmatch(re.sub(r"<[^>]+>", ".+", re.escape(t).replace("\\<", "<").replace("\\>", ">")), tid) for t in reg if "<" in t)
                assert match, f"recipe {r['id']} uses {tid}, not in the control tables"
                assert re.sub(r"\s+", " ", s["label"]).split(" (choose")[0] in " ".join(reg.values()) + " ".join(l for l in reg.values()) or "choose this test" in s["label"] or ": open what this test checks" in s["label"], (r["id"], s["label"])
    assert {r["id"] for r in K.RECIPES} >= set(K.RECIPE_FOR_TRANSPORT.values()) | set(K.RECIPE_FOR_PRESET.values())


def test_every_ui_help_term_is_in_the_glossary(app_a):
    app, _ = app_a
    g = B.glossary_from_ui()
    assert len(g) >= 15 and "p99" in g
    txt = text_of(app)
    for k, v in g.items():
        assert v[:40] in txt, f"help term {k} missing from the glossary"


def test_all_screens_and_controls_present_in_text(app_a):
    app, _ = app_a
    txt = text_of(app)
    for _, label in B.tab_labels():
        assert label in txt
    for v in K.VIEWS.values():
        assert "role" not in v, "views have no role any more"
        for tid, label, _ in v["controls"]:
            assert tid in txt and label in txt, (tid, label)


# ---------------------------------------------------------------------------------------------- no secrets
def test_no_secrets_anywhere(app_a):
    app, c = app_a
    st = app.state.tt
    assert c.post("/api/auth/set", json={"password": "Zebra-Quartz-77"}).status_code == 200
    c.post("/api/auth/login", json={"password": "Zebra-Quartz-77"})
    st.store.set_setting("wifi_psk", "PSK-SECRET-123")
    gp = P.validate_profile({"name": "has pw", "kind": "generator", "gen": {"peer_password": "PeerSecret-99", "streams": [{"name": "s"}]}})
    st.store.create_profile(gp)
    blobs = [c.get(p).text for p in ("/ai-guide", "/ai-guide.txt", "/llms.txt", "/api/ai-context", "/")]
    blobs.append(text_of(app) + to_html(B.build_doc(st, app, True)))
    pw_hash = st.pw_hash()
    bad = [pw_hash, pw_hash.split(":")[1], "Zebra-Quartz-77", "PSK-SECRET-123", "PeerSecret-99", st.secret.hex(), "sk-ant", "oauth", "refresh_token", "BEGIN PRIVATE KEY"]
    for b in blobs:
        for s in bad:
            assert s.lower() not in b.lower(), s
        assert not re.search(r"\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b", b), "a MAC address leaked"
        assert not re.search(r"tt_token\s*=", b)


# ---------------------------------------------------------------------------------------------- one guide, no roles
def test_one_guide_for_every_pi_without_role_words(app_a):
    app, c = app_a
    st = app.state.tt
    txt = text_of(app)
    html = c.get("/ai-guide").text
    page = c.get("/").text
    for name, blob in (("guide text", txt), ("guide html", re.sub(r"<[^>]+>", " ", html)), ("page note", re.sub(r"<[^>]+>", " ", page.split('<main id="content">')[1].split('<div id="main">')[0])),
                       ("ai-context", c.get("/api/ai-context").json()["live"])):
        assert not ROLE_WORDS.search(blob), (name, ROLE_WORDS.findall(blob)[:5])
    assert txt.startswith(f"Pi Automation Traffic Tester - AI guide ({st.me.name}, ")
    from traffic_tester import __version__
    assert txt.splitlines()[0].endswith(f"v{__version__})")
    for gone in ("pair-the-pis", "setup-responder", "setup-start-responder", "setup-link-type", "setup-udp-port-to-listen-on", "Pair the two Pis", "Start Responder", "This Pi is the Responder", "Role chip"):
        assert gone not in txt, gone
    assert not hasattr(B, "ROLE_B_API_PREFIXES")
    from traffic_tester.server import AppState
    for dead in ("role", "responder", "responder_cfg"):
        assert not hasattr(AppState, dead), f"the deprecated AppState.{dead} shim should be gone"
    assert "setup_B" not in K.VIEWS_BY_TAB_SUMMARY and "setup-responder" not in K.VIEWS


def test_new_views_controls_and_recipes_are_in_the_guide(app_a):
    app, _ = app_a
    txt = text_of(app)
    for tid in ("top-pi-name", "top-signed-in", "top-sign-out", "setup-endpoints", "setup-from", "setup-to", "setup-to-host", "setup-direction-round_trip", "setup-direction-forward", "setup-direction-reverse", "setup-direction-both",
                "setup-check", "setup-checks", "setup-from-serial-port", "setup-to-serial-port", "setup-from-iface", "add-pi-sheet", "add-pi-address", "add-pi-go", "custom-endpoints", "run-leg-card", "results-group-<n>", "results-row-direction-<n>",
                "result-ends", "gen-pairs", "gen-stream-<n>-from", "gen-stream-<n>-to", "gen-add-pi", "gen-advanced", "thispi-name-input", "thispi-name-save", "peers-refresh", "peer-add-go", "peer-clock-source", "clock-source-select", "clock-save",
                "upd-other-select", "upd-other-go", "net-wifi-mode-ap", "net-wifi-mode-client", "net-wifi-apply", "net-port-method-<iface>-static", "net-keep", "net-revert-now", "net-fallback", "about-pi-name"):
        assert f'[data-testid="{tid}"]' in txt, tid
    for gone in ("setup-responder-s-ip-address", "setup-round-trip-loopback", "generator-direction--s<n>", "settings-partner-pi-address", "settings-update-the-partner-pi"):
        assert gone not in txt, gone
    ids = {r["id"] for r in K.RECIPES}
    assert {"add-a-pi", "run-trip-ethernet", "one-way-both-ways", "run-generator", "name-pi-and-time-source", "network-settings"} <= ids and "pair-the-pis" not in ids
    titles = {r["id"]: r["title"] for r in K.RECIPES}
    assert titles["add-a-pi"] == "Add another Pi to the list" and titles["run-trip-ethernet"] == "Run a test from this Pi to another (round trip)" and titles["one-way-both-ways"] == "One way or both ways at once"
    assert titles["run-generator"].startswith("Run the generator between several Pis (N → M)") and titles["name-pi-and-time-source"] == "Name this Pi / set the time source" and titles["network-settings"].startswith("Network: ")
    nets = " ".join(str(x) for x in next(r for r in K.RECIPES if r["id"] == "network-settings")["steps"])
    assert "90" in nets and "Keep" in nets and "static" in nets.lower() and "access point" in nets.lower() and "Join a Wi-Fi network" in nets


def test_trip_recipe_is_inside_the_first_15000_chars_of_extracted_text(app_a):
    """Tag Along Watch keeps the first 15 000 characters (whitespace collapsed): the essentials must be inside, for the page and the .txt."""
    import html as _h
    app = app_a[0]
    need = [app.state.tt.me.name, "ive state", "Recipe: Run a trip test over serial", "Recipe: Run a test from this Pi to another (round trip)", "Add another Pi to the list", "Start test", "/ai-guide"]
    as_html = _h.unescape(re.sub(r"<[^>]+>", " ", to_html(B.build_doc(app.state.tt, app, True))))
    for flat in (re.sub(r"\s+", " ", as_html), re.sub(r"\s+", " ", text_of(app))):
        for n in need:
            i = flat.lower().find(n.lower()) if n == "ive state" else flat.find(n)
            assert i != -1 and i < 12000, (n, i)


def test_html_and_text_are_the_same_content_and_self_contained(app_a):
    app, c = app_a
    html = c.get("/ai-guide").text
    assert html.startswith("<!doctype html>") and 'lang="en"' in html and "<h1>" in html and "<script" not in html
    assert not re.search(r'(src|href|action)\s*=\s*["\']?https?://|url\(\s*["\']?https?://|@import', html), "no external references (no CDN)"
    assert "prefers-color-scheme:dark" in html
    plain = re.sub(r"<[^>]+>", " ", html)
    import html as _h
    plain = _h.unescape(re.sub(r"\s+", " ", plain))
    for raw in text_of(app).splitlines():
        for line in re.split(r" \| |: ", re.sub(r"^(- |\d+\. )", "", raw)):
            line = line.strip()
            if len(line) > 25 and not set(line) <= set("=-"):
                assert re.sub(r"\s+", " ", line).lower() in plain.lower(), line[:80]
    assert c.get("/ai-guide.txt").text == c.get("/llms.txt").text
    assert c.get("/ai-guide.txt").headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------------------------- live state
def _finish_fake_run(st, verdict_pass=False):
    prof = P.validate_profile({"name": "DER trip test", "transport": "udp", "net": {"peer_host": "10.0.0.2"}, "threshold_ms": 60})
    rid = st.store.create_run(st.store.current_session()["id"], prof, "A", "udp:10.0.0.2")
    summary = {"kind": "trip", "threshold_ms": 60, "sent": 12000, "lost": 3, "loss_pct": 0.025, "over_threshold": 45,
               "rtt_ms": {"p50": 21.4, "p95": 44.0, "p99": 74.2, "max": 190.5},
               "verdict": {"pass": verdict_pass, "reasons": [] if verdict_pass else ["p99 74.2 ms is over the 60 ms limit"]}}
    st.store.finish_run(rid, "finished", summary)
    return rid


def test_live_state_with_a_fake_finished_run(app_a):
    app, c = app_a
    st = app.state.tt
    assert "none yet" in text_of(app)
    rid = _finish_fake_run(st, False)
    txt = text_of(app)
    m = re.search(r"Last result: (#\d+.*)", txt)
    assert m, txt[:2000]
    line = m.group(1)
    for frag in (f"#{rid} 'DER trip test'", "verdict FAIL", "limit 60 ms", "p99 74.2 ms", "slowest 190.5 ms", "lost 3 of 12000", "over the limit 45", "p99 74.2 ms is over the 60 ms limit"):
        assert frag in line, (frag, line)
    import html as _h
    assert "Last result: 'DER trip test' FAIL" in _h.unescape(c.get("/").text) and "FAIL" in c.get("/api/ai-context").json()["live"]
    _finish_fake_run(st, True)
    assert "verdict PASS" in text_of(app)


def test_live_state_while_a_test_runs(app_a, monkeypatch):
    app, c = app_a
    st = app.state.tt
    monkeypatch.setattr(st.runs, "busy", lambda: True)
    monkeypatch.setattr(st.runs, "status", lambda: {"running": True, "run_id": 7, "profile_name": "Quick link check", "kind": "trip", "state": None})
    st.runs.last_state = {"elapsed_s": 12, "total_s": 60, "phase": "steady"}
    txt = text_of(app)
    assert "Test running: 'Quick link check' (kind trip, run #7). Progress: 12 s of 60 s, phase steady" in txt
    assert "RUNNING: 'Quick link check'" in c.get("/api/ai-context").json()["live"]


def test_live_state_names_what_is_unavailable_and_why(app_a, monkeypatch):
    app, _ = app_a
    monkeypatch.setattr(B, "unavailable", lambda: [("GOOSE tests", "no CAP_NET_RAW"), ("Bad-link simulation", "no CAP_NET_ADMIN"), ("Sniffer captures", "no tshark")])
    txt = text_of(app)
    assert "NOT available on this install (and why):" in txt
    assert "- GOOSE tests: no CAP_NET_RAW" in txt and "- Sniffer captures: no tshark" in txt


def test_live_state_covers_reflector_peers_clock_ports_and_interfaces(app_a):
    app, _ = app_a
    txt = text_of(app)
    me = app.state.tt.me
    for frag in (f"This Pi: '{me.name}' (id {me.id})", "Reflector (every Pi always answers test messages", "UDP and TCP port 20500", "Other Pis this Pi knows: none yet", "Clock sync", "Time source setting: auto",
                 "USB-serial adapters / serial ports seen:", "Network interfaces seen:", "Hardware:", "Web password:"):
        assert frag in txt, frag


def test_live_state_lists_other_pis_and_directions_without_secrets(app_a, monkeypatch):
    app, c = app_a
    st = app.state.tt
    fake = [{"id": "ab12cd34", "name": "pi2", "host": "10.0.0.2", "port": 8080, "source": "manual", "ips": ["10.0.0.2"], "reachable": True, "version": "1.2.0", "clock": {"synced": True, "offset_ms": 0.3},
             "busy": False, "password_required": True, "auth": "needs_password", "has_password": False, "error": "", "last_seen": 1, "setup_at": "2026-01-01T00:00:00", "hardware": "Raspberry Pi 5", "clock_source": "auto"},
            {"id": "ee11ff22", "name": "pi3", "host": "10.0.0.3", "port": 8080, "source": "mdns", "ips": ["10.0.0.3"], "reachable": False, "version": "", "clock": None, "busy": False, "password_required": False,
             "auth": "unknown", "has_password": False, "error": "no answer", "last_seen": None, "setup_at": None, "hardware": None, "clock_source": None}]
    monkeypatch.setattr(st.peers, "list", lambda: fake)
    txt = text_of(app)
    assert "pi2 at 10.0.0.2:8080 reachable, v1.2.0, clock synced, needs its password (Settings)" in txt
    assert "pi3 at 10.0.0.3:8080 NOT answering" in txt
    assert "Other Pis listed: 2 (1 reachable)." in c.get("/api/ai-context").json()["live"]
    monkeypatch.setattr(st.runs, "status", lambda: {"running": True, "run_id": 4, "profile_name": "Quick link check", "kind": "trip", "state": None,
                                                      "legs": [{"index": 0, "run_id": 4, "label": "pi1 → pi2", "running": True, "state": None, "status": "running"}, {"index": 1, "run_id": 5, "label": "pi2 → pi1", "running": True, "state": None, "status": "running"}]})
    monkeypatch.setattr(st.runs, "busy", lambda: True)
    txt = text_of(app)
    assert "Directions running at the same time: pi1 → pi2 (run #4, running); pi2 → pi1 (run #5, running)." in txt
    assert not ROLE_WORDS.search(txt)


# ---------------------------------------------------------------------------------------------- login rule + read-only
def test_password_login_rule_for_guide_and_page(app_a):
    app, c = app_a
    _finish_fake_run(app.state.tt, True)
    anon = TestClient(app)
    open_txt = anon.get("/ai-guide.txt").text
    assert "Last result: #" in open_txt
    assert c.post("/api/auth/set", json={"password": "pw1234"}).status_code == 200
    anon2 = TestClient(app)
    locked = anon2.get("/ai-guide.txt")
    assert locked.status_code == 200
    assert "Hidden: this app is password-protected" in locked.text and "Last result: #" not in locked.text and "Test running:" not in locked.text
    assert "Recipe: Run a trip test over serial" in locked.text                         # the how-to part is not live data
    assert "Last result" not in anon2.get("/").text and "Live state hidden" in anon2.get("/").text and "Other Pis listed" not in anon2.get("/").text and "Reflector (" not in locked.text
    assert anon2.get("/api/ai-context").status_code == 401
    c.post("/api/auth/login", json={"password": "pw1234"})
    assert "Last result: #" in c.get("/ai-guide.txt").text and "Last result: #" in c.get("/llms.txt").text
    assert c.get("/api/ai-context").status_code == 200


def test_guide_is_read_only(app_a):
    app, c = app_a
    st = app.state.tt

    def snap():
        return (len(st.store.list_profiles()), len(st.store.list_runs()), len(st.store.list_sessions()), st.pw_hash(), json.dumps(st.runs.status()), json.dumps(st.reflectors.status()["ports"]), json.dumps(st.peers.list()))
    before = snap()
    for p in ("/ai-guide", "/ai-guide.txt", "/llms.txt", "/api/ai-context", "/", "/index.html"):
        for _ in range(2):
            assert c.get(p).status_code == 200
    assert snap() == before
    for p in ("/ai-guide", "/ai-guide.txt", "/llms.txt", "/api/ai-context"):
        assert c.post(p).status_code in (404, 405) and c.delete(p).status_code in (404, 405) and c.put(p).status_code in (404, 405)
    new = [(m, p) for m, p in B.routes(app) if p in ("/ai-guide", "/ai-guide.txt", "/llms.txt", "/api/ai-context")]
    assert new and all(m == "GET" for m, _ in new)


def test_index_page_carries_the_note_the_extractors_read(app_a):
    app, c = app_a
    html = c.get("/").text
    main = html.split('<main id="content">')[1].split('<div id="main">')[0]
    assert "For AI assistants" in main and f"named {app.state.tt.me.name}" in main and 'href="/ai-guide"' in main and "No test is running" in main and "Other Pis listed" in main
    assert 'id="aiData"' in html and "<!--AI_CONTEXT-->" not in html and "<!--AI_DATA-->" not in html
    assert html.count('href="/ai-guide"') >= 2 and 'data-testid="foot-ai-guide"' in html     # the note and the footer (Settings has one too, drawn by script)
    for key, _ in B.tab_labels():
        assert f'data-testid="tab-{key}"' in html
    assert len(re.sub(r"<[^>]+>", "", main)) < 900, "the per-page note must stay short"
    # nothing hidden: the note is not display:none / aria-hidden / inside nav, header, footer or aside
    assert "aria-hidden" not in main and "display:none" not in main
    b = c.get("/api/ai-context").json()
    assert b["guide"] == "/ai-guide" and app.state.tt.me.name in b["live"]
