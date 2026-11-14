#!/usr/bin/env python3
"""Evaluate the generated polkit rule (JavaScript) in a headless browser with a stubbed `polkit` object: only the service user, only
`start`, only the five fixed tt-wifi-helper@<command>.service units get YES. usage: test-polkit-rule.py <rule file>"""
import json, sys
from playwright.sync_api import sync_playwright

rule = open(sys.argv[1]).read()
cases = [
    ("traffic-tester", "start", "tt-wifi-helper@monitor.service", True), ("traffic-tester", "start", "tt-wifi-helper@restore.service", True),
    ("traffic-tester", "start", "tt-wifi-helper@status.service", True), ("traffic-tester", "start", "tt-wifi-helper@channel.service", True),
    ("traffic-tester", "start", "tt-wifi-helper@inject-test.service", True),
    ("traffic-tester", "stop", "tt-wifi-helper@monitor.service", False), ("traffic-tester", "start", "tt-wifi-helper@monitor.service.evil", False),
    ("traffic-tester", "start", "tt-wifi-helper@../../x.service", False), ("traffic-tester", "start", "ssh.service", False),
    ("traffic-tester", "start", "tt-wifi-helper@watchdog.service", False), ("traffic-tester", "restart", "traffic-tester.service", False),
    ("mallory", "start", "tt-wifi-helper@monitor.service", False),
]
js = """([rule, cases]) => {
  let fn = null; const polkit = { addRule: f => { fn = f; }, Result: { YES: 'YES', NO: 'NO' } };
  new Function('polkit', rule)(polkit);
  return cases.map(([user, verb, unit]) => { const a = { id: 'org.freedesktop.systemd1.manage-units', lookup: k => ({ unit, verb })[k] };
    const bad = fn({ id: 'org.freedesktop.login1.power-off', lookup: () => null }, { user });
    return [fn(a, { user }) === 'YES', bad === undefined || bad === null]; });
}"""
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(); res = pg.evaluate(js, [rule, cases]); b.close()
bad = [(c, r) for c, r in zip(cases, res) if r[0] != c[3] or not r[1]]
print(json.dumps({"cases": len(cases), "wrong": bad}))
sys.exit(1 if bad else 0)
