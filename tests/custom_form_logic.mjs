// Runs the custom-test form's own logic (validation, unit conversion, summary sentence) without a browser. Started by tests/test_custom_form_js.py.
import assert from 'node:assert/strict';
const lim = JSON.parse(process.argv[2]);
globalThis.document = { }; globalThis.window = { };
const C = await import('./custom.js');
const { S } = await import('./state.js');
S.status = { goose_raw: { ok: false, reason: 'no raw sockets' }, netem: { ok: false, reason: 'no netem' }, clock: { synced: true }, interfaces: [] };
globalThis.fetch = async url => ({ ok: true, status: 200, text: async () => JSON.stringify(url.endsWith('/profile-limits') ? lim : url.endsWith('/ports') ? { ports: [] } : {}) });
await C.loadCustomLimits();
const mk = (name = 'T') => { const c = C.newDraft([]); c.p.name = name; return c; };
let c = mk(); C.derive(c);
assert.equal(Object.keys(C.validate(c, [])).length, 0, 'defaults are valid');
// the example sentence of the task: 20 msgs/s for 10 min = 12,000 samples, ~140 B/s each way; p99 <= 60 ms, loss <= 0.1 %
c.p.frame = 'compact';
let s = C.summary(c);
assert.match(s, /Sends 20 msgs\/s for 10 min = 12,000 samples, ~140 B\/s each way; passes if p99 ≤ 60 ms and loss ≤ 0\.1 %\./, s);
// validation
const errsFor = (mut) => { const d = mk(); mut(d); C.derive(d); return C.validate(d, []); };
assert.ok(errsFor(d => d.ui.rate = 0)['ui.rate']);
assert.ok(errsFor(d => d.ui.rate = 60001)['ui.rate'] && /1 to 60,000/.test(errsFor(d => d.ui.rate = 60001)['ui.rate']));
assert.ok(!errsFor(d => d.ui.rate = 1)['ui.rate'] && !errsFor(d => d.ui.rate = 60000)['ui.rate']);
assert.ok(errsFor(d => { d.ui.rateUnit = 'hz'; d.ui.rate = 1001; })['ui.rate'] && !errsFor(d => { d.ui.rateUnit = 'hz'; d.ui.rate = 1000; })['ui.rate']);
assert.ok(errsFor(d => d.ui.len = 0)['ui.len']);
assert.match(errsFor(d => { d.ui.lenUnit = 'messages'; d.ui.len = 5; })['ui.len'], /under 1 second/);
assert.ok(errsFor(d => { d.ui.lenUnit = 'hours'; d.ui.len = 200; })['ui.len']);       // > 7 days
assert.ok(errsFor(d => d.p.threshold_ms = 0).threshold_ms);
assert.match(errsFor(d => { d.p.threshold_ms = 100; d.p.timeout_ms = 100; }).timeout_ms, /larger than the delay limit/);
assert.ok(errsFor(d => d.p.rule.max_loss_pct = 101)['rule.max_loss_pct'] && errsFor(d => d.p.rule.max_loss_pct = -1)['rule.max_loss_pct']);
assert.ok(!errsFor(d => d.p.rule.max_loss_pct = 0)['rule.max_loss_pct']);
assert.ok(errsFor(d => d.p.name = '  ').test_name && errsFor(d => d.p.name = 'x'.repeat(81)).test_name);
assert.ok(C.validate(Object.assign(mk('Dup'), { id: null }), [{ id: 3, name: 'Dup' }]).test_name);                 // name already used
assert.ok(!C.validate(Object.assign(mk('Dup'), { id: 3 }), [{ id: 3, name: 'Dup' }]).test_name);                 // ... but not by itself when editing
assert.ok(errsFor(d => d.ui.gaps = '5, x')['ui.gaps'] && errsFor(d => d.ui.gaps = '0')['ui.gaps'] && !errsFor(d => d.ui.gaps = '5, 30, 120')['ui.gaps']);
assert.ok(errsFor(d => d.p.net.port = 70000)['net.port'] && errsFor(d => d.p.net.port = 0)['net.port'] && errsFor(d => d.p.net.dscp = 64)['net.dscp']);
assert.ok(errsFor(d => d.p.net.peer_host = '300.1.1.1')['net.peer_host'] && !errsFor(d => d.p.net.peer_host = '192.168.1.50')['net.peer_host'] && !errsFor(d => d.p.net.peer_host = '')['net.peer_host']);
assert.ok(errsFor(d => { d.p.transport = 'serial'; d.p.serial.baud = 100; })['serial.baud'] && !errsFor(d => { d.p.transport = 'serial'; d.p.serial.baud = 115200; })['serial.baud']);
assert.ok(errsFor(d => { d.p.transport = 'goose_l2'; d.p.goose.dest_mac = 'nope'; })['goose.dest_mac'] && errsFor(d => { d.p.transport = 'goose_l2'; d.p.goose.vlan_prio = 8; })['goose.vlan_prio']);
assert.ok(errsFor(d => { d.p.transport = 'goose_l3'; d.p.goose.group_a = '10.0.0.1'; })['goose.group_a'] && !errsFor(d => { d.p.transport = 'goose_l3'; })['goose.group_a']);
// the sentence follows the settings
c = mk(); C.derive(c); c.p.rule.percentile = 100; c.p.load_mbps = 10; c.p.impairment = 'Bad cell'; c.p.idle_gaps_s = [5, 30]; c.p.one_way = true;
s = C.summary(c); assert.match(s, /slowest message/); assert.match(s, /Bad cell/); assert.match(s, /10 Mbit\/s/); assert.match(s, /quiet/); assert.match(s, /Each direction/);
// export -> profile: trimmed name, no builtin flag, id gone
c = mk('  Spaces  '); c.p.builtin = true; const prof = C.toProfile(c); assert.equal(prof.name, 'Spaces'); assert.ok(!('builtin' in prof));
// editing keeps fields the form does not show (nothing is lost on re-save)
const e = C.editDraft({ id: 9, ...lim.defaults, name: 'E', gen: { a: 1 }, goose: { ...lim.defaults.goose, appid_a: 77 }, interval_ms: 25, duration_s: 7200 });
assert.equal(e.id, 9); assert.equal(e.ui.lenUnit, 'hours'); assert.equal(e.ui.len, 2); assert.equal(C.toProfile(e).goose.appid_a, 77); assert.deepEqual(C.toProfile(e).gen, { a: 1 });
// Mirrored Bits over serial defaults to 38400 baud; the plain serial test stays at 19200; a speed the user set (or a saved test) is never changed
{
  const d = mk(); d.p.transport = 'serial'; C.derive(d);
  assert.equal(d.p.serial.baud, 19200, 'plain serial test: 19200');
  d.p.mode = 'mirrored'; C.derive(d); assert.equal(d.p.serial.baud, 38400, 'Mirrored Bits over serial: 38400');
  d.p.mode = 'echo'; C.derive(d); assert.equal(d.p.serial.baud, 19200, 'back to the plain test: 19200 again');
  d.p.mode = 'mirrored'; d.p.transport = 'udp'; C.derive(d); assert.equal(d.p.mirrored.rate_baud, 19200, 'UDP Mirrored Bits keeps its own rate');
  d.p.transport = 'serial'; C.derive(d); assert.equal(d.p.serial.baud, 38400, 'switching the link to serial in Mirrored Bits mode: 38400');
  const u = mk(); u.p.transport = 'serial'; u.p.mode = 'mirrored'; C.derive(u);
  C.applyField(u, { dataset: { k: 'serial.baud', t: 'num' }, value: '19200', type: 'number' }); assert.equal(u.p.serial.baud, 19200, 'a speed the user picked stays');
  u.p.mode = 'echo'; u.p.mode = 'mirrored'; C.derive(u); assert.equal(u.p.serial.baud, 19200);
  const sv = C.editDraft({ id: 3, ...lim.defaults, name: 'Saved', transport: 'serial', mode: 'mirrored', serial: { ...lim.defaults.serial, baud: 19200 } }); C.derive(sv);
  assert.equal(sv.p.serial.baud, 19200, 'a saved Mirrored Bits test keeps its 19200');
  const sv2 = C.editDraft({ id: 4, ...lim.defaults, name: 'Saved2', transport: 'serial', mode: 'echo', serial: { ...lim.defaults.serial, baud: 38400 } }); sv2.p.mode = 'mirrored'; C.derive(sv2);
  assert.equal(sv2.p.serial.baud, 38400);
}
console.log('custom form logic: all assertions passed');
// a half-typed value in a link type that is not chosen must not block saving (it goes back to the default)
{ const d = mk(); d.p.serial.baud = 100; d.p.net.port = 99999; d.p.transport = 'serial'; const q = C.toProfile(d);
  assert.equal(q.serial.baud, 100); assert.equal(q.net.port, 20500);          // active group untouched, inactive one repaired
  d.p.transport = 'udp'; const u = C.toProfile(d); assert.equal(u.serial.baud, 19200); assert.equal(u.net.port, 99999); }
console.log('hidden-settings repair ok');

// ---- Mirrored Bits mode (1.1.3): no send rate, its own fields and sentence; saving an echo test repairs a half-typed mirrored value
{
  const m = mk('MB'); m.p.mode = 'mirrored'; m.p.transport = 'serial'; C.derive(m);
  assert.equal(Object.keys(C.validate(m, [])).length, 0, 'a mirrored default is valid (no send rate to check)');
  assert.equal(m.p.frame, 'compact'); assert.deepEqual(m.p.idle_gaps_s, []);
  const ms = C.summary(m);
  assert.match(ms, /38,400 baud \(one every 1\.04 ms/); assert.match(ms, /not SEL wire-compatible/); assert.match(ms, /every 2 s a test bit is flipped/); assert.match(ms, /RBAD never asserts/); assert.ok(!/Pi A|Pi B|Responder/.test(ms), 'no fixed-role wording');
  const bad = (mut) => { const d = mk(); d.p.mode = 'mirrored'; mut(d); C.derive(d); return C.validate(d, []); };
  assert.ok(bad(d => d.p.mirrored.trip_every_s = 0)['mirrored.trip_every_s']);
  assert.ok(!bad(d => { d.p.mirrored.trip_every_s = 0.5; d.p.timeout_ms = 1000; })['mirrored.trip_every_s'], 'the test bit is not a request/answer trip any more: no give-up-time rule');
  assert.ok(bad(d => d.p.mirrored.max_down_pct = 101)['mirrored.max_down_pct']);
  assert.ok(bad(d => { d.p.transport = 'udp'; d.p.mirrored.rate_baud = 10; })['mirrored.rate_baud'] && !bad(d => { d.p.transport = 'udp'; d.p.mirrored.rate_baud = 38400; })['mirrored.rate_baud']);
  assert.ok(bad(d => d.p.transport = 'goose_l2').transport, 'GOOSE cannot be Mirrored Bits');
  assert.ok(!bad(d => d.ui.rate = 0)['ui.rate'], 'the send rate is not used in mirrored mode');
  const one = bad(d => d.p.mirrored.peer_api = '192.168.1.5:8080'); assert.equal(Object.keys(one).length, 0);
  const x = mk(); x.p.mode = 'mirrored'; x.p.mirrored.peer_api = '1.2.3.4:8080'; C.derive(x); assert.equal(x.p.one_way, false, 'one-way timing is decided while the test runs (clocks synced or not), not by a form field');
  const echo = mk(); echo.p.mirrored.trip_every_s = -3; assert.equal(C.toProfile(echo).mirrored.trip_every_s, 2, 'an echo test does not keep a half-typed mirrored value');
}
console.log('mirrored form logic ok');

// ---- Send from / Send to / Direction (no fixed roles)
{
  const E = await import('./endpoints.js');
  assert.equal(mk().p.direction, 'round_trip', 'round trip is the default direction');
  const d = mk(); d.p.direction = 'forward'; C.derive(d);
  assert.equal(d.p.one_way, true, 'a one-way direction turns on one-way timing'); assert.equal(d.p.frame, 'extended');
  assert.match(C.summary(d), /Timed one way/);
  const b = mk(); b.p.direction = 'both'; C.derive(b); assert.match(C.summary(b), /at the same time/);
  assert.equal(Object.keys(C.validate(b, [])).length, 0, 'both ways at once is fine for UDP');
  for (const t of ['serial', 'goose_l2', 'goose_l3']) {                                  // a port cannot send and answer at once
    const x = mk(); x.p.transport = t; x.p.direction = 'both'; C.derive(x);
    assert.match(C.validate(x, []).direction || '', /not for serial or GOOSE/, t);
  }
  const m = mk(); m.p.mode = 'mirrored'; m.p.direction = 'both'; C.derive(m); assert.ok(!C.validate(m, []).direction, 'Mirrored Bits ignores the direction');
  // a typed address becomes the destination and stays in net.peer_host
  const t = mk(); t.p.net.peer_host = '10.1.2.3'; const q = C.toProfile(t);
  assert.equal(q.dest.host, '10.1.2.3'); assert.equal(q.net.peer_host, '10.1.2.3');
  assert.ok(E.directionReason({ ...t.p, direction: 'reverse' }, 'reverse', {}), 'a typed address cannot start a test, so "to -> from" is refused');
  assert.equal(E.directionReason({ ...t.p, direction: 'forward' }, 'forward', {}), '', 'one way from this Pi to an address is fine');
  console.log('endpoints form logic ok');
}
