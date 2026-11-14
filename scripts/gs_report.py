#!/usr/bin/env python3
"""Build docs/e2e-real-pis-gen-sniff.md from the CHECK rows (gs-results.jsonl) of the real-Pi generator + sniffer runs.
   python3 scripts/gs_report.py [results.jsonl] > docs/e2e-real-pis-gen-sniff.md   (the last row per section+check wins)"""
import collections, json, os, sys

res = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/pi-wired-run/gs-results.jsonl")
rows = collections.OrderedDict()
for l in open(res):
    r = json.loads(l)
    rows[(r["section"], r["check"])] = r
rows = list(rows.values())
cnt = collections.Counter(r["status"] for r in rows)
sections = collections.OrderedDict()
for r in rows:
    sections.setdefault(r["section"], []).append(r)


def esc(s):
    return str(s).replace("|", "/").replace("\n", " ")


print("# Generator + Sniffer on the two real Pis (direct cable)\n")
print(f"pi1 (192.168.77.1) <-> pi2 (192.168.77.2), eth0 back to back, 1 Gb/s; control over home Wi-Fi.  Scripts: `scripts/real_pis_gen.py`, `scripts/real_pis_sniff.py`, `scripts/real_pis_lib.py`.\n")
print(f"**{len(rows)} checks: {cnt['pass']} pass, {cnt['FAIL']} FAIL, {cnt['not testable']} not testable, {cnt['info']} informational (numbers recorded, no limit to pass).**\n")
extra = os.path.expanduser("~/pi-wired-run/gs-summary-extra.md")
if os.path.exists(extra):
    print(open(extra).read())
for sec, rs in sections.items():
    print(f"\n## {sec}\n")
    print("| # | Check | Result | Expected | Measured |\n|---|---|---|---|---|")
    for i, r in enumerate(rs, 1):
        mark = {"pass": "pass", "FAIL": "**FAIL**", "not testable": "not testable", "info": "info"}[r["status"]]
        print(f"| {i} | {esc(r['check'])} | {mark} | {esc(r['expected'])[:200]} | {esc(r['measured'])[:300]} |")
