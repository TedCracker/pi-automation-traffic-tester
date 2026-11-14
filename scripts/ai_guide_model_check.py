#!/usr/bin/env python3
"""Model check (optional): feed what Tag Along's Watch would extract from the app to a SMALL local Ollama model and ask 6 questions.
Skips (with the reason in the output) when a private/phone call is live or Ollama holds a big model (scripts never evicts anything).
Output: docs/ai-guide-model-check.md (verbatim answers + an honest keyword-rubric judgement). Run with system python3 (playwright)."""
import json, os, re, signal, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "ai-guide-model-check.md")
MODEL = os.environ.get("AIG_MODEL", "qwen3-vl:8b")
PORTS = {"A": 18350, "B": 18351}
TMP = tempfile.mkdtemp(prefix="tt-aigm-"); procs = []


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def http(port, method, path, body=None, raw=False):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as f:
        d = f.read(); return d if raw else (json.loads(d) if d else None)


def start(role):
    p = subprocess.Popen([os.path.join(ROOT, "venv/bin/python"), "-m", "traffic_tester", "--role", role, "--port", str(PORTS[role]), "--host", "127.0.0.1", "--data-dir", f"{TMP}/{role}"],
                         env=dict(os.environ, PYTHONPATH=ROOT, TT_ASSUME_SYNCED="1", TT_TEST_HOOKS="1"), stdout=open(f"{TMP}/{role}.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    for _ in range(80):
        try: http(PORTS[role], "GET", "/api/status"); return
        except Exception: time.sleep(0.25)
    raise RuntimeError("did not start")


def skip(reason):
    open(OUT, "w").write(f"# AI guide model check\n\n**Skipped:** {reason}\n\nRe-run `python3 scripts/ai_guide_model_check.py` when the GPU is free.\n")
    print("SKIPPED:", reason); sys.exit(0)


chk = sh("bash ~/local-llm/personaplex/live_check.sh")
if chk.returncode != 0:
    skip(f"live_check.sh exit {chk.returncode}: {(chk.stdout or chk.stderr).strip()}")
tags = json.loads(urllib.request.urlopen("http://localhost:11434/api/tags", timeout=10).read())
if MODEL not in [m["name"] for m in tags["models"]]:
    skip(f"model {MODEL} not installed in Ollama")

QUESTIONS = [
    ("How do I run a 60 ms trip test over serial?", [["DER trip test"], ["Serial port"], ["Start test"], ["Connect & check", "Link check", "link check", "step 2", "Connect"]]),
    ("Why did the last test fail?", [["fail", "FAIL", "did not pass", "didn't pass"], ["p99", "99"], ["limit", "60 ms"], ["delay", "slow", "late"]]),
    ("Is GOOSE available on this Pi?", "GOOSE"),
    ("What does the 'Over the limit' number mean?", [["late", "later", "slower"], ["not lost", "arrived", "than the limit", "delay limit"]]),
    ("How do I get a PDF report of the last test?", [["Results", "Run screen", "Run"], ["PDF report"]]),
    ("How do I set up the second Pi so I can test over Ethernet?", [["Start Responder"], ["Responder"], ["Ethernet", "UDP", "Link type"]]),
]
try:
    start("A"); start("B")
    http(PORTS["B"], "POST", "/api/responder/start", {"transport": "udp", "net": {"port": 20500}, "frame": "extended", "delay_ms": 85})   # a slow responder => the test really FAILS
    prof = http(PORTS["A"], "GET", "/api/profiles")
    q = next(p for p in prof if p["name"] == "Quick link check")
    q = {k: v for k, v in q.items() if k != "id"}
    q.update(duration_s=6, builtin=False, name="Quick link check", net={**q["net"], "peer_host": "127.0.0.1"})
    http(PORTS["A"], "POST", "/api/runs", {"profile": q})
    for _ in range(80):
        if not http(PORTS["A"], "GET", "/api/status")["run"]["running"]: break
        time.sleep(0.5)
    runs = http(PORTS["A"], "GET", "/api/runs")
    last = runs[0]
    with sync_playwright() as pw:
        b = pw.chromium.launch(); pg = b.new_page(); pg.goto(f"http://127.0.0.1:{PORTS['A']}/ai-guide", wait_until="load")
        pg.add_script_tag(content=open(os.path.join(ROOT, "scripts", "tagalong_extractors.js"), encoding="utf-8").read())
        page_text = pg.evaluate("() => window.__ta.extractPageContent()")
        pg.goto(f"http://127.0.0.1:{PORTS['A']}/", wait_until="load"); pg.wait_for_timeout(1000)
        pg.add_script_tag(content=open(os.path.join(ROOT, "scripts", "tagalong_extractors.js"), encoding="utf-8").read())
        app_text = pg.evaluate("() => window.__ta.extractPageContent()")
        b.close()
    guide_txt = http(PORTS["A"], "GET", "/ai-guide.txt", raw=True).decode()
    goose_unavail = bool(re.search(r"^- GOOSE tests", guide_txt, re.M))
    # judge GOOSE against the guide's own live state (this dev PC may or may not have the permission)
    QUESTIONS[2] = (QUESTIONS[2][0], [["not available", "unavailable", "cannot", "can't", "no,", "not"] if goose_unavail else ["available", "yes", "can", "works", "supported"]])

    SYSTEM = ("You are an assistant in a browser side panel. The text between the markers is the content of the web page the user has open (read by the sidebar). "
              "Treat it as reference material about the app on that page and answer the user's question from it, briefly and concretely, using the exact button labels it gives. "
              "If the page does not say, say you cannot tell.")
    rows, passed = [], 0
    for qn, (qtext, rubric) in enumerate(QUESTIONS, 1):
        body = {"model": MODEL, "stream": False, "think": False, "options": {"num_ctx": 8192, "temperature": 0, "num_predict": 3500},
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"<<<BEGIN_PAGE_CONTENT>>>\n{page_text}\n<<<END_PAGE_CONTENT>>>\n\nQuestion: {qtext}"}]}
        t0 = time.time()
        r = json.loads(urllib.request.urlopen(urllib.request.Request("http://localhost:11434/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}), timeout=300).read())
        ans = (r["message"]["content"] or "").strip()
        if not ans:        # say why there is no answer instead of showing a blank (seen with qwen3-vl: all tokens spent on hidden thinking)
            ans = f"(no answer text: done_reason={r.get('done_reason')}, output tokens={r.get('eval_count')}, hidden thinking={len((r['message'].get('thinking') or ''))} chars)"
        missing = [alts for alts in rubric if not any(a.lower() in ans.lower() for a in alts)]
        ok = not missing
        passed += ok
        rows.append((qtext, ans, ok, missing, round(time.time() - t0, 1), r.get("prompt_eval_count")))
        print(f"Q{qn} {'pass' if ok else 'FAIL'} ({rows[-1][4]}s, prompt {r.get('prompt_eval_count')} tok): {qtext}", flush=True)
    md = [f"# AI guide model check\n", f"Model: **{MODEL}** (local Ollama, temperature 0). Input: exactly what Tag Along's Watch extracts (its own `extractPageContent`, first 15 000 characters) from `/ai-guide` on a live Pi A with a real **FAILED** test (Responder delayed 85 ms; Quick link check, 6 s) behind it. Judgement = keyword rubric (every group must appear), stated per row; the answers are verbatim.\n",
          f"**Result: {passed}/{len(rows)} answered correctly.**  Extracted guide text: {len(page_text)} chars (cap 15 000). Guide's own last-result line: `{re.search(r'Last result: (.*)', guide_txt).group(1)[:260]}`\n"]
    for i, (qtext, ans, ok, missing, secs, ptok) in enumerate(rows, 1):
        md += [f"\n## Q{i}. {qtext}\n", f"**{'PASS' if ok else 'FAIL'}** · {secs}s · prompt {ptok} tokens" + ("" if ok else f" · rubric groups not found in the answer: {missing}"), "\n> " + ans.replace("\n", "\n> ")]
    md += ["\n## Same questions need the per-screen note too\n", f"Text Watch extracts from the app's own screen (`/`), {len(app_text)} chars; it starts: `{app_text[:300]}`"]
    open(OUT, "w").write("\n".join(md) + "\n")
    print(f"# {passed}/{len(rows)} correct -> {OUT}")
finally:
    for p in procs:
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: pass
