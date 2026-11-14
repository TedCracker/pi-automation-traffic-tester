"""Builds the AI guide document from the app's own registries (presets, routes, tabs, glossary) plus the hand-written content.py,
and renders the live state and the compact per-page "For AI assistants" note. Read-only: it never changes state and never includes secrets
(no tokens, password hashes, Wi-Fi keys, Claude credentials, MAC addresses)."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .. import __version__, profiles as P, system
from . import content as K
from .model import Doc, Section

STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
GUIDE_PATH, GUIDE_TXT, LLMS_TXT = "/ai-guide", "/ai-guide.txt", "/llms.txt"


class GuideCoverageError(AssertionError):
    """Raised when the app has something (preset, test kind, transport, route) the guide has no words for."""


# ------------------------------------------------------------------------------------------------ registries read from the code
def tab_labels() -> List[Tuple[str, str]]:
    """[(tab key, visible label)] exactly as in static/index.html."""
    html = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
    return [(m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()) for m in re.finditer(r'<button role="tab" data-tab="(\w+)"[^>]*>(.*?)</button>', html, re.S)]


def glossary_from_ui() -> Dict[str, str]:
    """The app's own tooltips (GLOSSARY in static/ui.js)."""
    src = open(os.path.join(STATIC, "ui.js"), encoding="utf-8").read()
    body = src.split("export const GLOSSARY = {", 1)[1].split("\n};", 1)[0]
    out = {}
    for m in re.finditer(r"^\s*(\w+):\s*'((?:[^'\\]|\\.)*)'\s*,?\s*$", body, re.M):
        out[m.group(1)] = m.group(2).replace("\\'", "'")
    return out


def presets() -> List[dict]:
    return P.builtin_presets()


def profile_kinds() -> List[str]:
    from typing import get_args
    return list(get_args(P.Profile.model_fields["kind"].annotation))


def transports() -> List[str]:
    from typing import get_args
    return list(get_args(P.Profile.model_fields["transport"].annotation))


def recipe_for_preset(p: dict) -> str:
    rid = K.RECIPE_FOR_PRESET.get(p["name"]) or K.RECIPE_FOR_TRANSPORT.get(p["transport"])
    if not rid or rid not in {r["id"] for r in K.RECIPES}:
        raise GuideCoverageError(f"AI guide: no recipe for preset '{p['name']}' (transport {p['transport']}). Add one in aiguide/content.py.")
    return rid


def screen_of(p: dict) -> str:
    return {"trip": "Setup", "generator": "Generator", "sniffer": "Sniffer"}[p.get("kind", "trip")]


def routes(app) -> List[Tuple[str, str]]:
    """[(METHOD, path)] of everything the app serves, from the live router."""
    from fastapi.routing import APIRoute
    from starlette.routing import Route, WebSocketRoute
    out = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            for m in sorted(r.methods or []):
                if m not in ("HEAD", "OPTIONS"):
                    out.append((m, r.path))
        elif isinstance(r, WebSocketRoute) or r.__class__.__name__ == "APIWebSocketRoute":
            out.append(("WS", r.path))
        elif isinstance(r, Route) and r.path and r.path != "/":
            for m in sorted(getattr(r, "methods", None) or ["GET"]):
                if m not in ("HEAD", "OPTIONS"):
                    out.append((m, r.path))
    return [x for x in out if f"{x[0]} {x[1]}" not in K.ROUTE_SKIP]


# ------------------------------------------------------------------------------------------------ live state (read-only)
def _fmt_ms(v) -> str:
    return "n/a" if v is None else f"{v:.1f} ms"


def _run_summary_line(run: dict) -> str:
    from ..claude.tools import run_digest
    d = run_digest(run)
    prof = d["profile"]
    s = run.get("summary") or {}
    kind = prof.get("kind") or "trip"
    head = f"#{run['id']} '{prof.get('name')}' ({kind}, {prof.get('transport')}) finished with status {run['status']}"
    if kind == "trip":
        v = s.get("verdict") or {}
        r = s.get("rtt_ms") or {}
        if not v:
            return f"{head}; no verdict. {s.get('error') or ''}".strip()
        parts = [f"verdict {'PASS' if v.get('pass') else 'FAIL'}", f"limit {s.get('threshold_ms')} ms", f"p50 {_fmt_ms(r.get('p50'))}", f"p95 {_fmt_ms(r.get('p95'))}", f"p99 {_fmt_ms(r.get('p99'))}",
                 f"slowest {_fmt_ms(r.get('max'))}", f"lost {s.get('lost')} of {s.get('sent')} sent ({s.get('loss_pct')} %)", f"over the limit {s.get('over_threshold')}"]
        txt = f"{head}: " + ", ".join(parts) + "."
        if v.get("reasons"):
            txt += " Reasons: " + "; ".join(v["reasons"]) + "."
        return txt
    if kind == "generator":
        t = s.get("totals") or {}
        v = s.get("verdict") or {}
        txt = f"{head}: verdict {'PASS' if v.get('pass') else 'FAIL' if v else 'none'}, sent {t.get('tx_mbps')} Mb/s, received {t.get('rx_mbps')} Mb/s, lost {t.get('lost')} ({t.get('loss_pct')} %)."
        if v.get("reasons"):
            txt += " Reasons: " + "; ".join(v["reasons"]) + "."
        return txt
    a = s.get("analysis") or {}
    return f"{head}: {a.get('packets')} packets captured, {a.get('errors', 0)} errors, {a.get('crc_failures', 0)} CRC failures{' - ' + s['error'] if s.get('error') else ''}."


def unavailable() -> List[Tuple[str, str]]:
    """What this install cannot do, and why, in plain words. Each entry: (feature, reason)."""
    out: List[Tuple[str, str]] = []
    try:
        from ..goose import raw_socket_ok
        g = raw_socket_ok()
        if not g.get("ok"):
            out.append(("GOOSE tests (raw Ethernet) and the generator's 'Raw Ethernet' type", g.get("reason") or "no raw-socket permission"))
    except Exception as e:                                         # pragma: no cover
        out.append(("GOOSE tests", f"could not be checked: {e}"))
    n = system.can_netem()
    if not n.get("ok"):
        out.append(("Bad-link simulation ('Simulate a bad link')", n.get("reason", "not permitted")))
    try:
        from ..sniff import capture as cap
        eng = cap.find_tools()["engine"]
        if eng is None:
            out.append(("Sniffer captures", "no capture tool (tshark / dumpcap or tcpdump) is installed"))
        elif eng == "tcpdump":
            out.append(("Sniffer pcapng output", "only tcpdump is installed: plain pcap, one port at a time"))
        if os.geteuid() != 0 and eng is not None and not _has_cap("CapEff", 13):
            out.append(("Sniffer (Ethernet / Wi-Fi capture)", "the app lacks the capture permission (CAP_NET_RAW); a full install with sudo gives it"))
    except Exception:                                              # pragma: no cover
        pass
    return out


def _has_cap(field: str, bit: int) -> bool:
    try:
        for line in open("/proc/self/status"):
            if line.startswith(field + ":"):
                return bool(int(line.split()[1], 16) & (1 << bit))
    except OSError:
        pass
    return False


def _peer_facts(st) -> List[Dict[str, Any]]:
    """The Pis this Pi knows: name, address, version, reachable, clock, auth. Never a password, token or MAC address."""
    out = []
    for p in st.peers.list():
        clk = p.get("clock") or {}
        out.append({"name": p.get("name"), "host": p.get("host"), "port": p.get("port"), "version": p.get("version") or "", "reachable": bool(p.get("reachable")),
                    "clock_synced": (clk.get("synced") if clk else None), "auth": p.get("auth"), "busy": bool(p.get("busy")), "found": p.get("source")})
    return out


def live_facts(st, authed: bool = True) -> Dict[str, Any]:
    """Structured live state; the text and HTML guide and /api/ai-context all render from this. There are no roles: every Pi is described the same way."""
    import socket
    f: Dict[str, Any] = {"version": __version__, "hostname": socket.gethostname(), "pi_name": st.me.name, "authed": authed}
    if not authed:
        return f
    f["pi_id"] = st.me.id
    rs = st.runs.status()
    f["running"] = rs
    state = st.runs.last_state or {}
    f["progress"] = {k: state.get(k) for k in ("elapsed_s", "total_s", "phase", "p99_ms", "lost", "sent", "late")} if rs["running"] else None
    runs = st.store.list_runs()
    f["last_run"] = None
    done = [r for r in runs if r["status"] != "running"]            # a run in progress is shown as "Test running", not as a result
    if done:
        full = st.store.get_run(done[0]["id"])
        try:
            f["last_run"] = _run_summary_line(full)
        except Exception as e:                                     # a run from an older version must never break the guide
            f["last_run"] = f"#{done[0]['id']} {done[0].get('name')}: status {done[0]['status']} (details unavailable: {e})"
    f["runs_total"] = len(runs)
    try:
        rf = st.reflectors.status()
        f["reflector"] = {"running": bool(rf.get("running")), "ports": rf.get("ports"), "echoed": rf.get("echoed", 0), "errors": dict(rf.get("default_errors") or {}), "claims": sorted(set((rf.get("claims") or {}).keys()))}
    except Exception:                                              # pragma: no cover
        f["reflector"] = {"running": False, "ports": [], "echoed": 0, "errors": {}, "claims": []}
    f["peers"] = _peer_facts(st)
    f["clock"] = system.clock_state()
    try:
        dec = st.clock.decision()
        f["clock_source"] = {"setting": st.clock.setting(), "mode": dec.get("mode"), "source_name": dec.get("source_name"), "why": dec.get("why")}
    except Exception:                                              # pragma: no cover
        f["clock_source"] = {"setting": "auto", "mode": None, "source_name": None, "why": ""}
    f["ports"] = [{"path": p["path"], "label": p["label"], "usb": p["usb"], "slow": p["adapter_slow"]} for p in system.list_ports()]
    f["interfaces"] = [{"name": i["name"], "wired": i["wired"], "up": i["up"]} for i in system.interfaces()]
    f["usb_drives"] = len(system.usb_drives())
    f["hardware"] = system.pi_model()
    f["unavailable"] = unavailable()
    f["password_enabled"] = bool(st.pw_hash())
    f["claude_on"] = bool(st.cstate.enabled)
    f["session"] = (st.store.current_session() or {}).get("name")
    return f


def live_line(st, authed: bool = True) -> str:
    """One short sentence for the 'For AI assistants' note; no numbers that change every second."""
    if not authed:
        return "Live state hidden: this app is password-protected; sign in first."
    f = live_facts(st, True)
    run = f["running"]
    t = f"A test is RUNNING: '{run['profile_name']}' ({run['kind']})." if run["running"] else "No test is running."
    if f["last_run"]:
        m = re.search(r"verdict (PASS|FAIL)", f["last_run"])
        n = re.match(r"#(\d+) '([^']*)'", f["last_run"])
        t += f" Last result: '{n.group(2)}' {m.group(1) if m else 'no verdict'}." if n else ""
    peers = f["peers"]
    t += f" Other Pis listed: {len(peers)}" + (f" ({sum(1 for p in peers if p['reachable'])} reachable)." if peers else ".")
    return f"This Pi is '{f['pi_name']}' (v{__version__}, host {f['hostname']}). {t}"


def _live_section(f: Dict[str, Any]) -> Section:
    s = Section("live", "2. Live state (right now)")
    if not f["authed"]:
        return s.p("Hidden: this app is password-protected and this browser is not signed in. Open the app, enter the password, then reload this guide.")
    s.p(f"This Pi: '{f['pi_name']}' (id {f['pi_id']}), host name {f['hostname']}, app version {f['version']}. Every Pi runs the same app; the Pi whose page you are on is the one that starts tests.")
    run = f["running"]
    if run["running"]:
        pr = f["progress"] or {}
        prog = f" Progress: {pr.get('elapsed_s')} s of {pr.get('total_s')} s, phase {pr.get('phase')}." if pr.get("elapsed_s") is not None else ""
        s.p(f"Test running: '{run['profile_name']}' (kind {run['kind']}, run #{run['run_id']}).{prog} Watch it on the Run screen; stop it with '■ Stop test'.")
        legs = run.get("legs") or []
        if legs:
            s.p("Directions running at the same time: " + "; ".join(f"{l.get('label') or 'direction ' + str(l.get('index'))} (run #{l.get('run_id')}, {'running' if l.get('running') else l.get('status') or 'done'})" for l in legs) + ".")
    else:
        s.p("Test running: no. Start one from Setup (trip tests), Generator or Sniffer.")
    s.p("Last result: " + (f["last_run"] or "none yet (no test has been run on this Pi)."))
    s.p(f"Tests stored on this Pi: {f['runs_total']}. Current session: {f['session']}.")
    rf = f["reflector"]
    ports = " and ".join(str(p) for p in (rf.get("ports") or [])) or "20500"
    if rf.get("running"):
        txt = f"Reflector (every Pi always answers test messages, so another Pi can send to this one): RUNNING on UDP and TCP port {ports}, {rf.get('echoed')} messages answered so far."
    else:
        txt = f"Reflector (every Pi always answers test messages on UDP and TCP port {ports}): not running"
        txt += (" - " + "; ".join(f"{k}: {v}" for k, v in rf["errors"].items())) if rf.get("errors") else " (it starts again by itself)."
    if rf.get("claims"):
        txt += " Held for a test right now: " + ", ".join(rf["claims"]) + "."
    s.p(txt)
    peers = f["peers"]
    if peers:
        s.p("Other Pis this Pi knows (found automatically on the network or added by IP): " + "; ".join(
            f"{p['name']} at {p['host']}:{p['port']} " + ("reachable" if p["reachable"] else "NOT answering") + (f", v{p['version']}" if p["version"] else "")
            + ("" if p["clock_synced"] is None else (", clock synced" if p["clock_synced"] else ", clock NOT synced")) + (", needs its password (Settings)" if p["auth"] == "needs_password" else "") + (", busy" if p["busy"] else "") for p in peers) + ".")
    else:
        s.p("Other Pis this Pi knows: none yet. They appear by themselves when they are on the same network; if one does not, add it with '＋ Add a Pi by IP' (recipe 'Add another Pi to the list').")
    clk = f["clock"]
    cs = f["clock_source"]
    src = ""
    if cs.get("mode"):
        src = {"serve": " This Pi is the time source.", "client": f" Time source: {cs.get('source_name')}.", "keep": " Time setup left as installed."}.get(cs["mode"], "") + (f" ({cs['why']})" if cs.get("why") else "")
    s.p("Clock sync (needed for one-way delay): " + (f"synced via {clk.get('source')}" + (f", offset {clk['offset_ms']:.2f} ms" if clk.get("offset_ms") is not None else "") if clk.get("synced") else "NOT synced - tests still work but only round trip is measured.") + "." + src + f" Time source setting: {cs['setting']}.")
    ports = f["ports"]
    shown = ports[:6]
    s.p("USB-serial adapters / serial ports seen: " + ("; ".join(f"{p['path']} ({p['label']}{', slow adapter - use Fix now' if p['slow'] else ''})" for p in shown) + (f"; and {len(ports) - 6} more" if len(ports) > 6 else "") if ports else "none. Serial tests cannot run until an adapter is plugged in."))
    ifs = [i for i in f["interfaces"] if not i["name"].startswith(("br-", "docker", "veth"))]
    s.p("Network interfaces seen: " + (", ".join(f"{i['name']} ({'wired' if i['wired'] else 'wireless/virtual'}, {'up' if i['up'] else 'down'})" for i in ifs[:8]) or "none") + f". USB drives plugged in: {f['usb_drives']}.")
    hw = f["hardware"]
    s.p("Hardware: " + (hw["model"] if hw.get("model") else "not a Raspberry Pi (development PC)") + f". Web password: {'set' if f['password_enabled'] else 'not set'}. Claude Code (optional Ask Claude): {'on' if f['claude_on'] else 'off (not needed; the tester works without it)'}.")
    un = f["unavailable"]
    if un:
        s.p("NOT available on this install (and why):")
        s.ul(f"{a}: {b}" for a, b in un)
        s.p("Everything else (UDP, TCP and serial tests, results, exports, the traffic generator over UDP/TCP) works.")
    else:
        s.p("Not available: nothing. This is a full install (GOOSE raw sockets, bad-link simulation and sniffer tools are all present).")
    return s


# ------------------------------------------------------------------------------------------------ steps -> text
def _tab_label(key: str, labels: Dict[str, str]) -> str:
    return labels.get(key, key)


def step_text(step: dict, labels: Dict[str, str]) -> str:
    do = step["do"]
    note = f" {step['note']}" if step.get("note") else ""
    sel = lambda tid: f'[data-testid="{tid}"]'
    if do == "goto":
        return f'Open the tab "{_tab_label(step["tab"], labels)}" (selector {sel(step["tid"])}).{note}'
    if do == "click":
        return f'Click "{step["label"]}" (selector {sel(step["tid"])}).{note}'
    if do == "type":
        return f'Type {step["value"]!r} into the field "{step["label"]}" (selector {sel(step["tid"])}).{note}'
    if do == "choose":
        return f'In the dropdown "{step["label"]}" choose "{step["option"]}" (selector {sel(step["tid"])}).{note}'
    return step["text"]


def recipe_steps(rec: dict, labels: Dict[str, str]) -> List[str]:
    return [step_text(s, labels) for s in rec["steps"]]


# ------------------------------------------------------------------------------------------------ the document
def route_rows(app) -> List[List[str]]:
    rows = []
    for m, path in routes(app):
        key = f"{m} {path}"
        note = K.RN.get(key)
        if note is None:
            raise GuideCoverageError(f"AI guide: route {key} has no description. Add it to ROUTE_NOTES (RN) in aiguide/content.py.")
        rows.append([m, path, "no" if m in ("GET", "WS") else "YES", note + (" (internal)" if key in K.INTERNAL_ROUTES else "")])
    return rows


def check_coverage(app) -> None:
    """Fail loudly when the app has a tab, test kind, link type, preset or route that the guide has no words for (unit-tested)."""
    have = {v["tab"] for v in K.VIEWS.values()}
    for key, label in tab_labels():
        if key not in have:
            raise GuideCoverageError(f"AI guide: tab '{label}' ({key}) has no view in aiguide/content.py VIEWS.")
        if key not in K.VIEWS_BY_TAB_SUMMARY:
            raise GuideCoverageError(f"AI guide: tab '{label}' ({key}) has no one-line summary (VIEWS_BY_TAB_SUMMARY).")
    for k in profile_kinds():
        if k not in K.KIND_ABOUT:
            raise GuideCoverageError(f"AI guide: test kind '{k}' has no description (KIND_ABOUT).")
    for t in transports():
        if t not in K.TRANSPORT_ABOUT or t not in K.RECIPE_FOR_TRANSPORT:
            raise GuideCoverageError(f"AI guide: link type '{t}' needs TRANSPORT_ABOUT and RECIPE_FOR_TRANSPORT entries.")
    for p in presets():
        recipe_for_preset(p)
    for m, path in routes(app):
        if f"{m} {path}" not in K.RN:
            raise GuideCoverageError(f"AI guide: route {m} {path} has no description. Add it to ROUTE_NOTES (RN) in aiguide/content.py.")


def build_doc(st, app, authed: bool = True) -> Doc:
    """ONE guide for every Pi (there are no roles): the live facts are about this Pi, the rest is the same everywhere."""
    check_coverage(app)
    facts = live_facts(st, authed)
    labels = dict(tab_labels())
    doc = Doc(
        title=f"Pi Automation Traffic Tester - AI guide ({facts['pi_name']}, {facts['hostname']}, v{__version__})",
        intro=[
            f"This device is the Pi named '{facts['pi_name']}' (host name {facts['hostname']}, app version {__version__}). Every Pi runs this same app, so there are no fixed roles: the Pi whose web page you are on starts the tests, collects the numbers and stores the results. Web app: / ; this guide: {GUIDE_PATH} (plain text: {GUIDE_TXT} or {LLMS_TXT}).",
            live_line(st, authed),
            "Purpose of this guide: let an AI assistant (a browser sidebar such as Tag Along, or any chat that can read this page) explain the app and, if it can click and type, operate it by exact button labels. It is read-only: opening it changes nothing. Section order: what this is, live state, quick start, screens and controls, recipes, how to read results, glossary, API.",
        ])
    # a. about
    a = doc.add(Section("about", "1. What this is"))
    a.p("Pi Automation Traffic Tester measures how fast and reliably a communications link carries small protection-style messages and ordinary traffic. Typical use: a utility checks that a cellular / radio link (for example a 5G router) delivers a 'trip' signal between a DER site and the substation within 60 ms. Two or more Raspberry Pis are used (one Pi can test its links to several others at once). Every Pi is the same: it always answers test messages (the 'reflector', UDP and TCP port 20500). Each test is set on one page with four rows: the Port of this Pi, the Protocol, From → To (this Pi and one or SEVERAL far Pis, ticked from a list; every ticked Pi is one 'link') with a Direction: ⇄ Round trip (the far end sends every message straight back; the classic test), → One way, or ⇆ Both ways (⇅ Swap reverses who sends), and the Traffic (rate, length, pass limit). The sending Pi measures the delay of every message and gives a PASS or FAIL for the whole test and for each link.")
    a.p("Pis have names (default: the host name, e.g. pi1, pi2; changed in Settings). They find each other on the network by themselves, or you add one by its IP address; the list is the tick list under 'To' on the New test page (and the From / To lists of the Generator). A serial port or a raw Ethernet port can only do one thing at a time, so it is claimed per test, and a clear 'busy' message appears if it is in use.")
    a.ul([K.KIND_ABOUT[k] for k in profile_kinds()])
    a.p("Link types for trip tests: " + "; ".join(K.TRANSPORT_ABOUT[t] for t in transports() if t not in ("generator", "sniffer")) + ".")
    a.p(f"The app runs in a browser. There are {len(tab_labels())} screens (tabs along the top): " + ", ".join(f'"{l}"' for _, l in tab_labels()) + ". Data is kept on the Pi; nothing is sent to the internet. Claude Code (the 'Ask Claude' panel) is an optional extra.")
    doc.add(_live_section(facts))
    # quick start: the essentials, early, for readers that cut the page
    qs = doc.add(Section("quickstart", "3. Quick start (the essentials)"))
    qs.p("The things people ask most. Details, every other task and every control follow below.")
    by_id = {r["id"]: r for r in K.RECIPES}
    for rid in ("run-trip-ethernet", "run-trip-serial"):
        qs.h3(f"Recipe: {by_id[rid]['title']}")
        qs.p("When: " + by_id[rid]["when"])
        qs.ol(recipe_steps(by_id[rid], labels))
    qs.p("The other Pi is not in the 'To' list? Click '＋ Add a Pi by IP' under the list (selector [data-testid=\"nt-add-pi\"]), type its IP address (for example 192.168.1.51) in 'IP address or name' and click 'Add this Pi'. Full steps: recipe 'Add another Pi to the list'. Two or more far Pis: tick them all (recipe 'One Pi to two or more Pis').")
    qs.h3("Read the result of a test")
    qs.p("Open Results (selector [data-testid=\"tab-results\"]). The list of tests is at the top and the newest test is open below it. (Right after a test the verdict is also on the Run screen.) The banner says ✓ PASS or ✕ FAIL with the reasons. Key tiles: p99 (99 out of 100 messages were at least this fast; compare with the limit, default 60 ms), Lost (no reply in time), Over the limit (late). A test with several links (Both ways, or one Pi to several Pis) is one entry that opens to a table of links and one line per link. Common causes of a FAIL are in section 6.")
    qs.p("PDF of a test: right after it ends click '📄 PDF report' on the Run screen (selector [data-testid=\"run-pdf-report\"]); later open Results, click the test's row, and click '📄 PDF report' in its 'Export this test' row. The first '📄 PDF report' at the top of Results covers the whole session.")
    qs.p("If you cannot click: tell the user which tab and button to use, quoting the exact labels above.")
    # c. screens
    sc = doc.add(Section("screens", "4. Every screen and control"))
    sc.p("Control ids: every button, link, field and dropdown carries a stable attribute data-testid and (fields) an aria-label equal to its visible label. Select with the CSS selector [data-testid=\"...\"] or click/type by the exact label shown in quotes. The tabs along the top are always available. A control is only present while its screen/step is open.")
    sc.table(["Tab (exact label)", "Selector", "What it is"], [[l, f'[data-testid="tab-{k}"]', K.VIEWS_BY_TAB_SUMMARY[k]] for k, l in tab_labels()])
    for vid, v in K.VIEWS.items():
        sc.h3(v["title"] + (f"  [tab {labels.get(v['tab'], v['tab'])}]" if v["tab"] != "*" else ""))
        sc.p(v["purpose"])
        if v["controls"]:
            sc.table(["Selector (data-testid)", "Exact label", "What it does"], [[f'[data-testid="{t}"]', l, w] for t, l, w in v["controls"]])
    sc.h3("Always present (top bar and footer)")
    sc.table(["Selector (data-testid)", "Exact label", "What it does"], [[f'[data-testid="{t}"]', l, w] for t, l, w in K.TOP_CONTROLS] + [["[data-testid=\"foot-ai-guide\"]", "AI guide", "Footer link to this guide (also inside the small 'For AI assistants' note and in Settings)."], ["[data-testid=\"foot-about\"]", "About", "Footer link to the About screen."]])
    sc.p("Renamed or removed controls (for assistants that remember an older version of this app, which had a three-step Setup with 'Send from' / 'Send to' pickers and a 'Create custom test' form): [data-testid=\"setup-from\"], [data-testid=\"setup-to\"], [data-testid=\"setup-to-host\"], [data-testid=\"setup-endpoints\"] and [data-testid=\"custom-endpoints\"] are now row 3 of the New test page ('From' = nt-here, 'To' = the tick list nt-far-<name of the other Pi>, typed address = nt-far-host). [data-testid=\"setup-direction-round_trip\"], [data-testid=\"setup-direction-forward\"], [data-testid=\"setup-direction-reverse\"] and [data-testid=\"setup-direction-both\"] are now nt-dir-round_trip, nt-dir-one_way (the other way: nt-swap) and nt-dir-both. [data-testid=\"setup-check\"] ('↻ Check again') is gone: the check runs by itself and its rows are in [data-testid=\"setup-checks\"]. [data-testid=\"setup-from-serial-port\"], [data-testid=\"setup-to-serial-port\"] and [data-testid=\"setup-from-iface\"] are now the port chips of row 1 (nt-port-<port name>) and 'Port on <far Pi>' (nt-far-port). 'Step 1 / 2 / 3', 'Continue with …', 'Review settings', '← Back' and '▶ Start test' are replaced by the four rows and ▶ Start (nt-start); 'Save and run pre-flight' by '💾 Save' (nt-save).")
    sc.p("Top right of the top bar there is only this Pi's name (tap it for Settings) and, when a web password is set, 'Signed in' with a 'Sign out' link; the small dot next to them is green when the live connection to the Pi is up.")
    pr = doc.sections[-1]
    pr.h3("Ready-made tests (presets) - generated from the app")
    pr.table(["Name (exact)", "Screen", "Link", "Details", "Recipe"], [[p["name"], screen_of(p), K.TRANSPORT_ABOUT.get(p["transport"], p["transport"]).split(" (")[0], _preset_details(p), K.RECIPES_TITLE[recipe_for_preset(p)]] for p in presets()])
    pr.p("Generator and Sniffer cards: [data-testid=\"<screen>-pick-<name in lower-case with dashes>\"], for example generator-pick-burst-stress. Setup test cards: the card itself is [data-testid=\"setup-open-<name in lower-case with dashes>\"] (it opens the information sheet) and its buttons are setup-run--<name> ('▶ Run', opens the New test page ready to start), setup-edit--<name>, setup-rename--<name> and setup-more-actions--<name> (the ⋮ menu), for example setup-run--quick-link-check. Your own tests (made with '＋ New test' and '💾 Save', or 'Duplicate') are in the same list, after the built-in ones unless you moved them.")
    pr.h3("What each built-in test means (the same text the app shows when you tap a test card)")
    for p in presets():
        if p.get("kind", "trip") != "trip" or not p.get("info"):
            continue
        pr.p(f"{p['name']}:")
        pr.ul([f"{title}: {p['info'][key]}" for key, title in P.INFO_SECTIONS])
    # d. recipes
    rc = doc.add(Section("recipes", "5. Recipes: how to do it by clicking"))
    rc.p("Each recipe lists numbered steps with the exact visible label and the selector. Do the steps in order. Lines starting 'Check' describe what you should see; they are not actions. Placeholders in <angle brackets> are values to fill in.")
    for rec in K.RECIPES:
        rc.h3(f"Recipe: {rec['title']}  [id {rec['id']}]")
        rc.p("When: " + rec["when"])
        rc.ol(recipe_steps(rec, labels))
    # e. results
    rs = doc.add(Section("results", "6. How to read results, and what to try when a test fails"))
    rs.p("Pass rule of a trip test: PASS when the chosen percentile (default p99) of all messages came back within the delay limit (default 60 ms) AND the lost share is within the allowed loss (default 0.1 %, see the presets) AND nothing else in the rules is violated. The reasons for a FAIL are printed in the banner.")
    rs.kv(K.READING)
    rs.h3("Common failures and what to try")
    rs.kv(K.FAILURES)
    # f. glossary
    gl = doc.add(Section("glossary", "7. Glossary (plain language)"))
    gl.kv(sorted(K.GLOSSARY_EXTRA.items()))
    gl.h3("Terms behind the small 'i' help icons in the app")
    gl.kv([(k, v) for k, v in glossary_from_ui().items()])
    # g. api
    ap = doc.add(Section("api", "8. API summary (for assistants that can send HTTP requests)"))
    ap.p("Base address is this page's own address. JSON in and out. If a web password is set, sign in first with POST /api/auth/login {\"password\": \"...\"} (a cookie is returned). 'Changes state' = YES means the call starts, stops, changes or deletes something; only call those if the user asked. The complete machine-readable list is GET /openapi.json.")
    ap.table(["Method", "Path", "Changes state", "What it does"], route_rows(app))
    return doc


def _preset_details(p: dict) -> str:
    k = p.get("kind", "trip")
    if k == "trip":
        return f"message every {p['interval_ms']:g} ms for {p['duration_s']:g} s, delay limit {p['threshold_ms']:g} ms, pass at p{p['rule']['percentile']} with loss <= {p['rule']['max_loss_pct']:g} %" + (f", quiet gaps {p['idle_gaps_s']} s" if p["idle_gaps_s"] else "")
    if k == "generator":
        return f"{len(p['gen']['streams'])} stream(s), {p['gen']['duration_s']:g} s: " + p["description"].split(".")[0]
    return p["sniff"]["mode"] + ": " + p["description"].split(".")[0]


# ------------------------------------------------------------------------------------------------ the per-page note (index.html)
def screens_json() -> Dict[str, str]:
    """One short line per screen for the 'For AI assistants' note (kept in sync with K.VIEWS_BY_TAB_SUMMARY)."""
    return dict(K.VIEWS_BY_TAB_SUMMARY)


def context_html(st, authed: bool) -> str:
    """The compact, always-rendered note placed first inside <main> so extractors that cut the page still get it. ~600 characters."""
    from html import escape as e
    tabs = ", ".join(l for _, l in tab_labels())
    return (f'<p><strong>For AI assistants:</strong> this is the Pi Automation Traffic Tester v{__version__}; every Pi runs the same app and this one is named {e(st.me.name)}. Screens: {e(tabs)}. '
            f'<span id="aiScreen">{e(K.VIEWS_BY_TAB_SUMMARY["setup"])}</span> <span id="aiLive">{e(live_line(st, authed))}</span></p>'
            f'<p>Full instructions, exact button labels, click-by-click recipes and live state: link <a href="{GUIDE_PATH}" data-testid="ai-context-guide" aria-label="AI guide: instructions for AI assistants">AI guide</a> ({GUIDE_PATH}, plain text {GUIDE_TXT}).</p>')


def data_json(st) -> str:
    d = {"screens": screens_json(), "guide": GUIDE_PATH}
    return json.dumps(d).replace("</", "<\\/")
