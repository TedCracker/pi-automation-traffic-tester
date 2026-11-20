// "Create custom test": the guided form in Setup (docs/SPEC.md 6.4). Tests are saved through the normal profiles API: a custom test is an ordinary
// saved profile (schema_version unchanged), so edit / duplicate / delete / export / import all work on it already.
// Allowed ranges come from GET /api/profile-limits (read from the server's schema), so this form cannot disagree with the server.
import { mbFieldsHtml, parseMb, putMb, MB_TYPES } from './mbsettings.js';
import { $, $$, esc, api, tip, dur, bindOnce, TRANSPORT_NAMES } from './ui.js';
import { S } from './state.js';
import { radioErrors } from './radioui.js';
import { mountEndpoints, directionReason, normalizeEndpoints } from './endpoints.js';

const LINKS = [
  ['serial', 'A USB-serial adapter wired to the radio or relay, like a Mirrored Bits link.'],
  ['udp', "Messages over the radio's Ethernet port (UDP). The simplest place to start."],
  ['tcp', 'Keeps one TCP session open and also times the connection set-up and recovery.'],
  ['goose_l2', 'IEC 61850 GOOSE frames straight on Ethernet, with VLAN priority.'],
  ['goose_l3', 'Routed GOOSE (R-GOOSE) over UDP multicast.'],
];
const UNIT_S = { seconds: 1, minutes: 60, hours: 3600 };
const FRAME_BYTES = { compact: 7, extended: 40 };
const round3 = x => Math.round(x * 1000) / 1000;
const clone = o => JSON.parse(JSON.stringify(o));
const getPath = (o, path) => path.split('.').reduce((a, k) => a == null ? a : a[k], o);
const setPath = (o, path, v) => { const k = path.split('.'); let t = o; k.slice(0, -1).forEach(x => t = (t[x] ??= {})); t[k.at(-1)] = v; };
const fmt = n => Number.isInteger(n) ? n.toLocaleString() : String(+n.toFixed(2));

let limits = null, netem = {}, ports = [];
export async function loadCustomLimits() {
  if (!limits) limits = await api('/profile-limits');
  try { netem = await api('/netem-presets'); } catch { }
  try { ports = (await api('/ports')).ports; } catch { ports = []; }
}

// ---------------------------------------------------------------- draft <-> profile
function uiFrom(p) {
  const s = p.duration_s, unit = s % 3600 === 0 ? 'hours' : s % 60 === 0 ? 'minutes' : 'seconds';
  return { rate: p.interval_ms, rateUnit: 'ms', len: s / UNIT_S[unit], lenUnit: unit, gaps: (p.idle_gaps_s || []).join(', '), port: '' };
}
// Default speed of a serial test: 19200 for the plain trip test, 38400 for Mirrored Bits mode (real Pis: p50 bit transfer 2.7 ms vs 5.7 ms at 9600, docs/e2e-real-pis-serial.md).
// Only a draft whose speed nobody has set yet (c.baudSet) follows the mode: saved tests, presets and a speed the user picked keep theirs.
export const MB_SERIAL_BAUD = 38400, SERIAL_BAUD = 19200;
function followModeBaud(c) {
  const p = c.p;
  if (c.baudSet || p.transport !== 'serial' || ![SERIAL_BAUD, MB_SERIAL_BAUD].includes(p.serial.baud)) return;
  p.serial.baud = p.mode === 'mirrored' ? MB_SERIAL_BAUD : SERIAL_BAUD;
}
export function derive(c) {
  const { p, ui } = c;
  followModeBaud(c);
  p.interval_ms = ui.rateUnit === 'ms' ? ui.rate : (ui.rate > 0 ? round3(1000 / ui.rate) : NaN);
  p.duration_s = ui.lenUnit === 'messages' ? round3(ui.len * p.interval_ms / 1000) : ui.len * UNIT_S[ui.lenUnit];
  if (p.mode === 'mirrored') { p.one_way = false; p.frame = 'compact'; p.idle_gaps_s = []; if (ui.lenUnit === 'messages') { ui.lenUnit = 'minutes'; ui.len = Math.max(1, round3(p.duration_s / 60)); } }
  else {
    if (p.direction === 'forward' || p.direction === 'reverse') p.one_way = true;            // the server does the same: one-way timing needs the timestamps
    if (p.one_way || p.transport === 'goose_l2' || p.transport === 'goose_l3') p.frame = 'extended';
  }
  if (p.mode !== 'mirrored') p.idle_gaps_s = String(ui.gaps || '').split(/[,\s]+/).filter(Boolean).map(Number);
}
const uniqueName = (base, profiles) => { let n = base, i = 1; while (profiles.some(x => x.name === n)) n = `${base} ${++i}`; return n; };
export function newDraft(profiles) {
  const p = clone(limits.defaults); p.name = uniqueName('My custom test', profiles);
  const c = { id: null, p, ui: uiFrom(p), server: '' }; c.base = JSON.stringify([c.p, c.ui]); return c;
}
export function editDraft(profile) {
  const p = clone(profile); delete p.id;
  const c = { id: profile.id, baudSet: true, builtin: !!profile.builtin, defaultName: profile.default_name || '', p, ui: uiFrom(p), server: '' }; c.base = JSON.stringify([c.p, c.ui]); return c;
}
export const isDirty = c => JSON.stringify([c.p, c.ui]) !== c.base;
// Settings of a link type that is not chosen are not shown, so a half-typed value there (a baud of 100 typed before switching to UDP) must not block the save:
// anything out of range in an inactive group goes back to the default.
const ACTIVE = { serial: ['serial'], udp: ['net'], tcp: ['net'], goose_l2: ['goose'], goose_l3: ['goose'] };
const MBMODE = p => p.mode === 'mirrored';
export function toProfile(c) {
  const p = clone(c.p); p.name = String(p.name).trim(); delete p.builtin;
  if (p.mode === 'mirrored') p.direction = 'round_trip';
  normalizeEndpoints(p);
  for (const key of Object.keys(limits.fields)) {
    const grp = key.split('.')[0];
    if (grp === 'mirrored' ? MBMODE(p) : (!['serial', 'net', 'goose'].includes(grp) || (ACTIVE[p.transport] || []).includes(grp))) continue;
    if (!inRange(key, getPath(p, key))) setPath(p, key, getPath(limits.defaults, key));
  }
  return p;
}

// ---------------------------------------------------------------- what this install can run (same checks the rest of Setup uses)
function caps(p) {
  const g = S.status.goose_raw || {}, n = S.status.netem || {}, ck = S.status.clock || {};
  return {
    goose: g.ok ? '' : (g.reason || 'This install has no raw-socket permission for GOOSE.'),
    netem: n.ok ? '' : (n.reason || 'Not allowed on this install.'),
    load: p.transport === 'udp' || p.transport === 'tcp' ? '' : 'Background load is sent over IP, so it needs an Ethernet (UDP or TCP) link.',
    oneWayNote: ck.synced ? '' : "This Pi's clock is not synced, so each-direction timing may not be available (the test then measures round trip only).",
  };
}

// ---------------------------------------------------------------- validation (inline, in plain words)
function bound(key) { return (limits.fields[key]) || {}; }
function rangeMsg(key, unit = '') {
  const b = bound(key), lo = b.ge ?? b.gt, hi = b.le ?? b.lt, u = unit ? ' ' + unit : '';
  if (lo != null && hi != null) return `Enter a number from ${fmt(lo)} to ${fmt(hi)}${u}.`;
  if (lo != null) return b.gt != null ? `Enter a number above ${fmt(lo)}${u}.` : `Enter a number of at least ${fmt(lo)}${u}.`;
  if (hi != null) return `Enter a number up to ${fmt(hi)}${u}.`;
  return 'Enter a number.';
}
function inRange(key, v) {
  if (v == null || v === '' || !Number.isFinite(Number(v))) return false;
  const b = bound(key); v = Number(v);
  return !((b.ge != null && v < b.ge) || (b.gt != null && v <= b.gt) || (b.le != null && v > b.le) || (b.lt != null && v >= b.lt));
}
const IPV4 = /^(\d{1,3})(\.\d{1,3}){3}$/, HOST = /^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$/;
const MAC = /^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$/i;
export function validate(c, profiles) {
  const { p, ui } = c, e = {};
  const name = String(p.name ?? '').trim();
  if (!name) e.test_name = 'Give the test a name.';
  else if (name.length > (bound('name').max_length || 80)) e.test_name = `Use at most ${bound('name').max_length} characters.`;
  else if (profiles.some(x => x.id !== c.id && x.name === name)) e.test_name = 'You already have a test with this name. Pick another one.';
  const iv = limits.fields.interval_ms, mb = p.mode === 'mirrored';
  if (mb) { /* continuous: no send rate to check */ } else if (ui.rate == null || !Number.isFinite(ui.rate) || ui.rate <= 0) e['ui.rate'] = ui.rateUnit === 'ms' ? rangeMsg('interval_ms', 'ms') : 'Enter how many messages per second.';
  else if (!inRange('interval_ms', p.interval_ms)) e['ui.rate'] = ui.rateUnit === 'ms' ? rangeMsg('interval_ms', 'ms')
    : `Enter ${+(1000 / iv.le).toFixed(3)} to ${fmt(1000 / iv.ge)} messages per second.`;
  if (ui.len == null || !Number.isFinite(ui.len) || ui.len <= 0) e['ui.len'] = ui.lenUnit === 'messages' ? 'Enter how many messages to send.' : 'Enter how long the test runs.';
  else if (ui.lenUnit === 'messages' && !Number.isInteger(ui.len)) e['ui.len'] = 'Use a whole number of messages.';
  else if (!e['ui.rate'] && !inRange('duration_s', p.duration_s)) {
    const b = bound('duration_s');
    e['ui.len'] = p.duration_s < b.ge ? (ui.lenUnit === 'messages' ? `That is under ${b.ge} second. Send at least ${Math.ceil(b.ge * 1000 / p.interval_ms)} messages at this rate.` : `A test runs for at least ${b.ge} second.`)
      : `A test can run at most ${dur(b.le)}.`;
  }
  if (!inRange('threshold_ms', p.threshold_ms)) e.threshold_ms = rangeMsg('threshold_ms', 'ms');
  if (!inRange('timeout_ms', p.timeout_ms)) e.timeout_ms = rangeMsg('timeout_ms', 'ms');
  else if (!e.threshold_ms && p.timeout_ms <= p.threshold_ms) e.timeout_ms = `Must be larger than the delay limit (${p.threshold_ms} ms).`;
  if (mb) {
    const m = p.mirrored;
    if (!inRange('mirrored.trip_every_s', m.trip_every_s)) e['mirrored.trip_every_s'] = rangeMsg('mirrored.trip_every_s', 's');
    if (!inRange('mirrored.max_down_pct', m.max_down_pct)) e['mirrored.max_down_pct'] = rangeMsg('mirrored.max_down_pct', '%');
    if (p.transport !== 'serial' && (!inRange('mirrored.rate_baud', m.rate_baud) || !Number.isInteger(Number(m.rate_baud)))) e['mirrored.rate_baud'] = `Enter a whole number from ${fmt(bound('mirrored.rate_baud').ge)} to ${fmt(bound('mirrored.rate_baud').le)} baud (a relay channel uses 9600, 19200 or 38400).`;
  }
  if (p.transport === 'serial') Object.assign(e, radioErrors(p, inRange));
  if (!inRange('rule.max_loss_pct', p.rule.max_loss_pct)) e['rule.max_loss_pct'] = rangeMsg('rule.max_loss_pct', '%');
  if (!mb && (!inRange('idle_repeats', p.idle_repeats) || !Number.isInteger(Number(p.idle_repeats)))) e.idle_repeats = rangeMsg('idle_repeats').replace('a number', 'a whole number');
  const g = limits.fields.idle_gaps_s;
  if (mb) { /* no quiet-period checks: the link is never quiet */ } else if (p.idle_gaps_s.length > g.max_count) e['ui.gaps'] = `At most ${g.max_count} gaps.`;
  else if (p.idle_gaps_s.some(x => !Number.isFinite(x) || x <= g.gt || x > g.le)) e['ui.gaps'] = `Numbers of seconds separated by commas, each above 0 and up to ${fmt(g.le)}. Example: 5, 30, 120.`;
  if (p.load_mbps !== 0 && !inRange('load_mbps', p.load_mbps)) e.load_mbps = rangeMsg('load_mbps', 'Mbit/s');
  const t = p.transport;
  const dr = directionReason(p, p.direction || 'round_trip', c.ui);
  if (dr && !mb) e.direction = 'This direction is not possible: ' + dr;
  if (mb && (t === 'goose_l2' || t === 'goose_l3')) e.transport = 'Mirrored Bits mode runs over serial, UDP or TCP.';
  if (t === 'serial') {
    if (!inRange('serial.baud', p.serial.baud) || !Number.isInteger(Number(p.serial.baud))) e['serial.baud'] = `Enter a whole number from ${fmt(bound('serial.baud').ge)} to ${fmt(bound('serial.baud').le)} baud.`;
    if (ui.port && /\s/.test(ui.port)) e['ui.port'] = 'A port path has no spaces, for example /dev/ttyUSB0.';
  }
  if (t === 'udp' || t === 'tcp') {
    const h = String(p.net.peer_host || '').trim();
    if (h && !(IPV4.test(h) ? h.split('.').every(x => +x <= 255) : HOST.test(h))) e['net.peer_host'] = 'Use an IP address like 192.168.1.50 (or leave it empty).';
    if (!inRange('net.port', p.net.port) || !Number.isInteger(Number(p.net.port))) e['net.port'] = `Enter a port from ${bound('net.port').ge} to ${fmt(bound('net.port').le)}.`;
    if (!inRange('net.dscp', p.net.dscp) || !Number.isInteger(Number(p.net.dscp))) e['net.dscp'] = `Enter a whole number from ${bound('net.dscp').ge} to ${bound('net.dscp').le}.`;
    if (t === 'tcp' && !inRange('net.keepalive_idle_s', p.net.keepalive_idle_s)) e['net.keepalive_idle_s'] = rangeMsg('net.keepalive_idle_s', 's');
  }
  if (t === 'goose_l2' || t === 'goose_l3') {
    const q = p.goose;
    if (t === 'goose_l2') {
      if (!MAC.test(q.dest_mac || '')) e['goose.dest_mac'] = 'Looks like 01:0c:cd:01:00:01 (six pairs of hex digits).';
      if (!inRange('goose.vlan_id', q.vlan_id) || !Number.isInteger(Number(q.vlan_id))) e['goose.vlan_id'] = `Enter a whole number from ${bound('goose.vlan_id').ge} to ${fmt(bound('goose.vlan_id').le)}.`;
      if (!inRange('goose.vlan_prio', q.vlan_prio) || !Number.isInteger(Number(q.vlan_prio))) e['goose.vlan_prio'] = `Enter a whole number from ${bound('goose.vlan_prio').ge} to ${bound('goose.vlan_prio').le}.`;
    } else {
      const mc = v => IPV4.test(v) && +v.split('.')[0] >= 224 && +v.split('.')[0] <= 239 && v.split('.').every(x => +x <= 255);
      if (!mc(q.group_a || '')) e['goose.group_a'] = 'A multicast address from 224.0.0.0 to 239.255.255.255.';
      if (!mc(q.group_b || '')) e['goose.group_b'] = 'A multicast address from 224.0.0.0 to 239.255.255.255.';
      for (const [k, w] of [['udp_port', 'port'], ['ttl', 'TTL'], ['dscp', 'priority']]) if (!inRange('goose.' + k, q[k]) || !Number.isInteger(Number(q[k]))) e['goose.' + k] = rangeMsg('goose.' + k).replace('a number', 'a whole number');
      const u = String(q.unicast_peer || '').trim();
      if (u && !(IPV4.test(u) ? u.split('.').every(x => +x <= 255) : HOST.test(u))) e['goose.unicast_peer'] = 'An IP address, or empty.';
    }
  }
  return e;
}

// ---------------------------------------------------------------- the live summary sentence
export function summary(c) {
  if (c.p.mode === 'mirrored') return summaryMb(c.p);
  const p = c.p, ok = Number.isFinite(p.interval_ms) && p.interval_ms > 0 && Number.isFinite(p.duration_s) && p.duration_s > 0;
  if (!ok) return 'Fill in the highlighted fields to see what this test will do.';
  const rate = 1000 / p.interval_ms, n = Math.round(p.duration_s * 1000 / p.interval_ms);
  const bytes = Math.round(rate * FRAME_BYTES[p.frame]);
  const who = p.rule.percentile === 100 ? 'the slowest message' : `p${p.rule.percentile}`;
  let s = `Sends ${fmt(+rate.toPrecision(3))} msgs/s for ${dur(p.duration_s)} = ${n.toLocaleString()} samples, ~${bytes.toLocaleString()} B/s each way; passes if ${who} ≤ ${p.threshold_ms} ms and loss ≤ ${p.rule.max_loss_pct} %.`;
  if (p.idle_gaps_s.length && p.idle_gaps_s.every(Number.isFinite)) s += ` Then checks the first message after ${p.idle_gaps_s.map(g => dur(g)).join(', ')} of quiet (${p.idle_repeats}× each).`;
  if (p.one_way) s += ' Each direction is timed separately.';
  if (p.direction === 'forward' || p.direction === 'reverse') s += ' Timed one way, from the Pi that sends to the Pi that receives.';
  else if (p.direction === 'both') s += ' Both Pis send to each other at the same time, each direction timed as a round trip.';
  if (p.impairment) s += ` Bad link simulated: ${p.impairment}.`;
  if (p.load_mbps > 0 && (p.transport === 'udp' || p.transport === 'tcp')) s += ` With ${fmt(p.load_mbps)} Mbit/s of background traffic.`;
  return s;
}

function summaryMb(p) {
  const m = p.mirrored, baud = p.transport === 'serial' ? p.serial.baud : m.rate_baud;
  if (![baud, m.trip_every_s, p.duration_s, p.threshold_ms].every(Number.isFinite) || baud <= 0) return 'Fill in the highlighted fields to see what this test will do.';
  const per = 4 * 10 / baud * 1000, who = p.rule.percentile === 100 ? 'the slowest bit change' : `${p.rule.percentile} % of bit changes`;
  if (m.codec !== 'sim') return `Two Mirrored Bits emulators, one at each end, send numbered, time-stamped 19-byte frames back to back at ${fmt(baud)} baud and check every frame they receive. Each frame carries the echo of the last one heard, so the round trip of every frame is measured and lost, out-of-order and corrupted frames are counted separately. Every ${m.trip_every_s} s a test bit is flipped, for ${dur(p.duration_s)}. Passes if ${p.rule.percentile === 100 ? 'the slowest frame' : `${p.rule.percentile} % of frames`} cross within ${p.threshold_ms} ms (one way, estimated as half the round trip), ROK is 0 at most ${m.max_down_pct} % of the time and RBAD never asserts. Timed format: not SEL wire-compatible.`;
  return `Two Mirrored Bits emulators, one at each end, send a full 4-character message back to back at ${fmt(baud)} baud (one every ${per.toFixed(2)} ms, ~${fmt(Math.round(1000 / per))} messages/s each way) and check every message they receive: ROK drops at once on any bad, late or missing message. An echo bit gives the round trip; every ${m.trip_every_s} s a test bit is flipped and timed to the other end, for ${dur(p.duration_s)}. Passes if ${who} cross within ${p.threshold_ms} ms, ROK is 0 at most ${m.max_down_pct} % of the time and RBAD never asserts. Real Mirrored Bits load (SimCodec): not SEL wire-compatible.`;
}

// ---------------------------------------------------------------- form markup
const lbl = (text, t) => `<span>${esc(text)}${t ? ' ' + tip(t) : ''}</span>`;
export const errEl = k => `<small class="err" data-e="${k}" hidden></small>`;
export function field(c, k, label, { type = 'num', t = '', hint = '', attrs = '' } = {}) {
  const v = k.startsWith('ui.') ? c.ui[k.slice(3)] : getPath(c.p, k);
  const shown = v == null || (typeof v === 'number' && !Number.isFinite(v)) ? '' : v;
  return `<label class="f">${lbl(label, t)}<input data-k="${k}" data-t="${type}" type="${type === 'num' ? 'number' : 'text'}" ${type === 'num' ? 'step="any" inputmode="decimal"' : 'autocomplete="off"'} value="${esc(shown)}" ${attrs}>${hint ? `<small class="muted">${esc(hint)}</small>` : ''}${errEl(k)}</label>`;
}
export function select(c, k, label, opts, { t = '', type = 'str', disabled = false, note = '' } = {}) {
  const cur = String(k.startsWith('ui.') ? c.ui[k.slice(3)] : getPath(c.p, k) ?? '');
  return `<label class="f">${lbl(label, t)}<select data-k="${k}" data-t="${type}" ${disabled ? 'disabled' : ''}>${opts.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === cur ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>${note ? `<small class="why">${esc(note)}</small>` : ''}${errEl(k)}</label>`;
}
export const check = (c, k, label, t = '') => `<label class="chk"><input type="checkbox" data-k="${k}" data-t="bool" ${getPath(c.p, k) ? 'checked' : ''}> ${esc(label)} ${t ? tip(t) : ''}</label>`;
function opt(name, value, checked, title, text, off = '') {
  return `<label class="opt ${off ? 'off' : ''}"><input type="radio" name="${name}" value="${esc(value)}" data-k="${name}" ${checked ? 'checked' : ''} ${off ? 'disabled' : ''}>
    <span><b>${esc(title)}</b><small>${esc(off ? 'Not available here: ' + off : text)}</small></span></label>`;
}

// The one choice that decides what a Mirrored Bits test shows (default Timed, since 1.12.3).  Used by this form and by the New test page.
export function codecChoice(c) {
  const cur = c.p.mirrored.codec === 'sim' ? 'sim' : 'trip';
  return `<div data-testid="setup-message-format" style="grid-column:1/-1;margin:12px 0 4px"><h3 style="margin:0 0 8px">Message format</h3>
    <div class="opts" role="radiogroup" aria-label="Message format" data-testid="mb-codec-choice">
      ${opt('mirrored.codec', 'trip', cur === 'trip', 'Timed (recommended)', 'Numbered, time-stamped messages: best latency numbers; tells lost, out-of-order and corrupted apart.')}
      ${opt('mirrored.codec', 'sim', cur === 'sim', 'Real Mirrored Bits load', 'Nonstop like a relay channel: shows whether the channel stays up (ROK / RBAD / CBAD).')}</div>
    <small class="muted">Both are our own formats for Pi-to-Pi tests, not SEL wire-compatible. Timed judges the time limit on half the round trip (an estimate of the one-way time: a true one-way time needs GPS time).</small></div>`;
}

function mirroredCard(c) {
  const p = c.p, m = p.mirrored;
  return `<div class="card"><h2>4 · How long, and which signal is timed?</h2><div class="grid g3">
      <label class="f">${lbl('Test length')}<div class="fieldrow"><input data-k="ui.len" data-t="num" type="number" step="any" inputmode="decimal" value="${Number.isFinite(c.ui.len) ? c.ui.len : ''}">
        <select data-k="ui.lenUnit" data-t="str">${['seconds', 'minutes', 'hours'].map(u => `<option value="${u}" ${c.ui.lenUnit === u ? 'selected' : ''}>${u}</option>`).join('')}</select></div>${errEl('ui.len')}</label>
      ${field(c, 'mirrored.trip_every_s', 'Flip the test bit every (seconds)', { t: 'The starting Pi flips the test bit (TMB1) this often; the time until it shows at the other Pi is the bit transfer time.' })}
      ${p.transport === 'serial' ? '' : field(c, 'mirrored.rate_baud', 'Message speed (baud)', { hint: 'Over IP the same message pace as a serial channel of this speed: 9600, 19200 or 38400.' })}</div>
    ${codecChoice(c)}
    ${mbFieldsHtml(p, (path, type) => ` data-k="${path}" data-t="${type}"`, p.transport === 'serial')}
    ${p.transport === 'serial' ? '<p class="muted" style="margin:10px 0 0">The serial speed above sets the message pace: at 38400 baud (the default here) one 4-character message takes 1.04 ms, at 19200 baud 2.08 ms; the line is never idle.</p>' : ''}</div>`;
}

function linkFields(c) {
  const p = c.p, t = p.transport;
  if (t === 'serial') {
    const cur = p.serial.port, known = cur === 'auto' || ports.some(x => x.path === cur);
    const bauds = [...new Set([9600, 19200, 38400, 57600, 115200, p.serial.baud].filter(Number.isFinite))].sort((a, b) => a - b);
    return `<div class="grid g2">
      <label class="f">${lbl('Serial port', 'Auto picks the first USB-serial adapter.')}<select data-k="serial.port" data-t="str"><option value="auto" ${cur === 'auto' ? 'selected' : ''}>Auto-detect (recommended)</option>${ports.map(x => `<option value="${esc(x.path)}" ${cur === x.path ? 'selected' : ''}>${esc(x.label)} — ${esc(x.path)}</option>`).join('')}${known ? '' : `<option value="${esc(cur)}" selected>${esc(cur)}</option>`}</select></label>
      <label class="f">${lbl('Or type a port path')}<input data-k="ui.port" data-t="str" placeholder="/dev/ttyUSB0" autocomplete="off" value="${esc(c.ui.port)}">${errEl('ui.port')}</label>
      <label class="f">${lbl('Speed (baud)', 'baud')}<input data-k="serial.baud" data-t="num" type="number" inputmode="numeric" list="cBauds" value="${Number.isFinite(p.serial.baud) ? p.serial.baud : ''}"><datalist id="cBauds">${bauds.map(b => `<option value="${b}">`).join('')}</datalist>${errEl('serial.baud')}</label></div>
      <details style="margin-top:10px"><summary>More serial settings</summary><div class="grid g3" style="margin-top:10px">
        ${select(c, 'serial.bytesize', 'Data bits', [5, 6, 7, 8].map(x => [x, x]), { type: 'num' })}
        ${select(c, 'serial.parity', 'Parity', [['N', 'None'], ['E', 'Even'], ['O', 'Odd']])}
        ${select(c, 'serial.stopbits', 'Stop bits', [[1, '1'], [2, '2']], { type: 'num' })}</div></details>`;
  }
  if (t === 'udp' || t === 'tcp') {
    return `<div class="grid g3">
      ${field(c, 'net.port', 'Port', { hint: `Same number on the Pi that answers. Allowed ${bound('net.port').ge}–${bound('net.port').le}.` })}
      ${field(c, 'net.dscp', 'Priority (DSCP 0-63)', { t: 'dscp' })}</div>
      ${t === 'tcp' ? `<details style="margin-top:10px"><summary>More TCP settings</summary><div class="grid g3" style="margin-top:10px">${check(c, 'net.keepalive', 'Keep the session alive (TCP keepalive)')}${field(c, 'net.keepalive_idle_s', 'Keepalive after quiet (seconds)')}</div></details>` : ''}`;
  }
  const ifs = (S.status.interfaces || []).map(i => `<option value="${esc(i.name)}" ${p.goose.interface === i.name ? 'selected' : ''}>${esc(i.name)}${i.wired ? '' : ' (wireless/virtual)'} ${i.mac ? '· ' + i.mac : ''}</option>`).join('');
  const iface = `<label class="f">${lbl('Network port (interface)', 'goose')}<select data-k="goose.interface" data-t="str"><option value="auto">Auto (first wired port)</option>${ifs}</select></label>`;
  if (t === 'goose_l2') return `<div class="grid g3">${iface}${field(c, 'goose.dest_mac', 'Destination MAC (multicast)', { type: 'str' })}${field(c, 'goose.vlan_id', 'VLAN ID (0 = priority only)', { t: 'vlan' })}${field(c, 'goose.vlan_prio', 'Priority (0-7)', { t: 'vlan' })}${check(c, 'goose.vlan', 'VLAN tag (802.1Q)', 'vlan')}</div>`;
  return `<div class="grid g3">${iface}${field(c, 'goose.group_a', 'Group A→B', { type: 'str' })}${field(c, 'goose.group_b', 'Group B→A', { type: 'str' })}${field(c, 'goose.udp_port', 'UDP port')}${field(c, 'goose.ttl', 'TTL')}${field(c, 'goose.dscp', 'Priority (DSCP 0-63)', { t: 'dscp' })}
    ${field(c, 'goose.unicast_peer', 'Unicast fallback address (optional)', { type: 'str', t: 'Only if the carrier path drops multicast. Non-standard.' })}</div>`;
}

export function formHtml(c) {
  const p = c.p, cp = caps(p), editing = c.id != null;
  const fixedFrame = p.one_way || p.transport === 'goose_l2' || p.transport === 'goose_l3';
  const imps = [['', 'None (use the real link)'], ...Object.keys(netem).map(k => [k, `${k} — ${netem[k]}`])];
  return `<div id="customForm"><div class="row sp"><h2 style="margin:0;font-size:1.3rem">${editing ? (c.builtin ? 'Edit built-in test' : 'Edit test') : 'Create a new test'}</h2></div>
    <p class="sub muted">${c.builtin ? `You are changing the built-in test “${esc(c.defaultName || p.name)}”. The original stays in the app: <b>Reset to default</b> (in the test’s ⋮ menu) brings it back, and app updates never erase your changes.` : 'Describe the test in plain words; the sentence at the bottom says exactly what it will do. It is added to the end of your list on the first step.'}</p>
    <div id="cServerErr" class="check bad" hidden role="alert"><div class="ic">✕</div><div><b>It could not be saved</b><small id="cServerMsg"></small></div></div>
    <div class="cgrid"><div class="cmain">
    <div class="card"><h2>1 · Name it</h2><div class="grid">
      <label class="f">${lbl('Name')}<input data-k="test_name" data-t="str" maxlength="${bound('name').max_length || 80}" autocomplete="off" value="${esc(p.name)}">${errEl('test_name')}</label>
      <label class="f">${lbl('What is this test for? (optional)')}<textarea data-k="description" data-t="str" rows="2" placeholder="e.g. Site 4 trip signal over the radio, 60 ms limit">${esc(p.description)}</textarea></label></div></div>

    <div class="card"><h2>2 · How are the Pis connected?</h2>
      <div class="opts" role="radiogroup" aria-label="Link type">${LINKS.map(([k, about]) => {
        const off = (k === 'goose_l2' || k === 'goose_l3') ? cp.goose : '';
        return opt('transport', k, p.transport === k, TRANSPORT_NAMES[k], about, off);
      }).join('')}</div>
      <div style="margin-top:14px">${linkFields(c)}</div>
      <h3 style="margin:16px 0 8px">How do the two Pis talk?</h3>
      <div class="opts" role="radiogroup" aria-label="Mode">
        ${opt('mode', 'echo', p.mode !== 'mirrored', 'Echo (request and answer)', 'The sending Pi sends a message every so often and the other Pi sends it straight back. The sending Pi times each round trip.')}
        ${opt('mode', 'mirrored', p.mode === 'mirrored', 'Mirrored Bits mode (continuous both ways)', 'Both ends send all the time, back to back, like a pair of protection relays, and check every message. Not wire-compatible with SEL relays. Serial, UDP or TCP only.', (p.transport === 'goose_l2' || p.transport === 'goose_l3') ? 'it needs a serial, UDP or TCP link.' : '')}</div></div>

    <div class="card" data-testid="custom-endpoints-card"><h2>3 · Who sends, and who answers?</h2>
      <p class="muted" style="margin:0 0 10px">Pick the Pis for this test. The ports each Pi uses are chosen when you run it (Setup → Connect &amp; check).</p>
      <div id="cEpBox" data-testid="custom-endpoints"></div>${errEl('net.peer_host')}${errEl('direction')}
      ${p.direction === 'round_trip' && p.mode !== 'mirrored' ? `<div style="margin-top:12px">${check(c, 'one_way', 'Also time each direction (needs both clocks synced)', 'oneway')}</div>` : ''}
      ${p.one_way && cp.oneWayNote && p.mode !== 'mirrored' ? `<div class="check warn" style="margin-top:10px"><div class="ic">!</div><div><b>Heads up</b><small>${esc(cp.oneWayNote)}</small></div></div>` : ''}</div>

    ${p.mode === 'mirrored' ? mirroredCard(c) : `<div class="card"><h2>4 · What should it send?</h2><div class="grid g2">
      <label class="f">${lbl('Send rate', 'interval')}<div class="fieldrow"><input data-k="ui.rate" data-t="num" type="number" step="any" inputmode="decimal" value="${Number.isFinite(c.ui.rate) ? c.ui.rate : ''}">
        <select data-k="ui.rateUnit" data-t="str"><option value="ms" ${c.ui.rateUnit === 'ms' ? 'selected' : ''}>ms between messages</option><option value="hz" ${c.ui.rateUnit === 'hz' ? 'selected' : ''}>messages per second</option></select></div>${errEl('ui.rate')}</label>
      <label class="f">${lbl('Test length')}<div class="fieldrow"><input data-k="ui.len" data-t="num" type="number" step="any" inputmode="decimal" value="${Number.isFinite(c.ui.len) ? c.ui.len : ''}">
        <select data-k="ui.lenUnit" data-t="str">${['seconds', 'minutes', 'hours', 'messages'].map(u => `<option value="${u}" ${c.ui.lenUnit === u ? 'selected' : ''}>${u}</option>`).join('')}</select></div>${errEl('ui.len')}</label>
      ${select(c, 'frame', 'Message size', [['compact', 'Compact, 7 bytes (Mirrored-Bits-like)'], ['extended', 'Extended, 40 bytes (with timestamps)']], { disabled: fixedFrame, note: fixedFrame ? 'This test needs the extended message (it carries the timestamps), so it is fixed.' : '' })}</div>
</div>`}

    <div class="card"><h2>5 · When does it pass?</h2><div class="grid g3">
      ${field(c, 'threshold_ms', 'Delay limit (ms)', { t: 'threshold' })}
      ${select(c, 'rule.percentile', 'Pass rule: percentile', [[50, 'Typical (p50)'], [95, 'p95'], [99, 'p99 (recommended)'], [99.9, 'p99.9 (long runs)'], [100, 'Slowest (max)']], { t: 'rule', type: 'num' })}
      ${field(c, 'rule.max_loss_pct', p.mode === 'mirrored' ? 'Allowed trips lost (%)' : 'Allowed loss (%)', { t: 'loss' })}
      ${p.mode === 'mirrored' ? field(c, 'mirrored.max_down_pct', 'Channel may be failed (% of the time)', { t: 'Pass limit for the share of time the channel was declared failed. Default 0.1 %.' }) : ''}</div></div>

    <div class="card"><h2>6 · Make it harder <span class="tag">optional</span></h2><div class="grid g2">
      ${select(c, 'impairment', 'Simulate a bad link', imps, { t: 'netem', type: 'nul', disabled: !!cp.netem, note: cp.netem ? 'Not available here: ' + cp.netem : '' })}
      <label class="f">${lbl('Background load (Mbit/s, IP only)', 'load')}<input data-k="load_mbps" data-t="num" type="number" step="any" inputmode="decimal" value="${Number.isFinite(p.load_mbps) ? p.load_mbps : ''}" ${cp.load ? 'disabled' : ''}>${cp.load ? `<small class="why">Not available here: ${esc(cp.load)}</small>` : ''}${errEl('load_mbps')}</label></div></div>

    <details class="card" id="cMore"><summary>More options</summary><div class="grid g3" style="margin-top:12px">
      ${field(c, 'timeout_ms', 'Give up after (ms)', { t: 'timeout' })}
      ${field(c, 'ui.gaps', 'Quiet gaps to test (seconds, comma-separated)', { type: 'str', t: 'idle', hint: 'Leave empty for none. Example: 5, 30, 120.' })}
      ${field(c, 'idle_repeats', 'Repeats per gap')}
      ${select(c, 'idle_mode', 'Quiet checks happen', [['after', 'After the main test'], ['interleaved', 'Mixed in (soak)']])}</div></details>
    </div>
    <aside class="csum"><div class="card sumcard"><h2>What this test does</h2><p id="cSummary" aria-live="polite">${esc(summary(c))}</p></div></aside></div>

    <div class="actionbar cbar"><button class="btn" data-act="c-cancel">Cancel</button><span class="hint muted" id="cHint"></span>
      <button class="btn" data-act="c-save" title="Saves the test to your list">💾 Save</button>
      <button class="btn primary" data-act="c-save-check" title="Saves the test, then opens the link check (step 2) so you can start it">Save and run pre-flight</button></div></div>`;
}

// ---------------------------------------------------------------- behaviour
export function paint(host, c, profiles) {
  const errs = validate(c, profiles);
  $$('small.err[data-e]', host).forEach(s => {
    const m = errs[s.dataset.e] || '';
    s.textContent = m; s.hidden = !m;
    const inp = $(`[data-k="${s.dataset.e}"]`, host); if (inp) { inp.setAttribute('aria-invalid', m ? 'true' : 'false'); }
  });
  const sum = $('#cSummary', host); if (sum) sum.textContent = summary(c);
  const n = Object.keys(errs).length, h = $('#cHint', host);
  if (h) h.textContent = n ? `${n} field${n > 1 ? 's' : ''} to fix` : 'Looks good';
  return errs;
}

export function applyField(c, el) { return apply(c, el); }
export const customData = () => ({ limits, netem, ports });
function apply(c, el) {
  const k = el.dataset.k, t = el.dataset.t;
  let v;
  if (el.type === 'checkbox') v = el.checked;
  else if (el.type === 'radio') v = k === 'one_way' ? el.value === 'true' : el.value;
  else v = el.value;
  if (t === 'num') v = el.value === '' ? NaN : Number(el.value);
  if (t === 'nul') v = el.value || null;
  if (MB_TYPES.includes(t)) { putMb(c.p, k, parseMb(el, t)); derive(c); return; }
  if (k === 'ui.rateUnit' || k === 'ui.lenUnit') {
    // keep the same real-world meaning when the unit changes: 50 ms <-> 20 per second, 10 minutes <-> 600 seconds
    if (k === 'ui.rateUnit' && v !== c.ui.rateUnit && Number.isFinite(c.ui.rate) && c.ui.rate > 0) c.ui.rate = round3(1000 / c.ui.rate);
    if (k === 'ui.lenUnit' && v !== c.ui.lenUnit && Number.isFinite(c.ui.len)) {
      const secs = c.ui.lenUnit === 'messages' ? c.ui.len * c.p.interval_ms / 1000 : c.ui.len * UNIT_S[c.ui.lenUnit];
      if (v === 'messages') c.ui.len = Math.max(1, Math.round(secs * 1000 / c.p.interval_ms));
      else c.ui.len = round3(secs / UNIT_S[v]);
    }
  }
  if (k === 'serial.baud') c.baudSet = true;
  if (k === 'ui.port') { c.ui.port = String(v).trim(); if (c.ui.port) c.p.serial.port = c.ui.port; }
  else if (k === 'serial.port') { c.ui.port = ''; setPath(c.p, k, v); }
  else if (k.startsWith('ui.')) c.ui[k.slice(3)] = v;
  else setPath(c.p, k === 'test_name' ? 'name' : k, v);     // (data-k "name" is reserved by the AI-guide id rules for stream / channel names)
  derive(c);
}

// `ctx`: { state (the draft), profiles, onSave(profile, preflight), onCancel() }. Called by setup.js whenever the form should be (re)drawn.
export function renderCustom(host, ctx) {
  const c = ctx.state; derive(c);
  const draw = () => {
    const a = document.activeElement, key = a && a.dataset && a.dataset.k, val = a && a.type === 'radio' ? a.value : null;
    const open = $('#cMore', host)?.open;
    host.innerHTML = formHtml(c);
    if (open) $('#cMore', host).open = true;
    mountEndpoints($('#cEpBox', host), { get: () => c.p, ui: c.ui, set: (path, v) => { setPath(c.p, path, v); derive(c); }, changed: d => { if (d >= 700) paint(host, c, ctx.profiles); else draw(); } }, { ports: false });
    paint(host, c, ctx.profiles);
    if (key) { const el = val != null ? $(`[data-k="${key}"][value="${val}"]`, host) : $(`[data-k="${key}"]`, host); if (el) el.focus({ preventScroll: true }); }
  };
  const redrawKeys = new Set(['transport', 'one_way', 'mode', 'ui.rateUnit', 'ui.lenUnit']);
  bindOnce(host, 'input', ev => {
    const el = ev.target; if (!el.dataset.k || el.type === 'radio' || el.tagName === 'SELECT' || redrawKeys.has(el.dataset.k)) return;
    apply(c, el); paint(host, c, ctx.profiles);
  });
  bindOnce(host, 'change', ev => {
    const el = ev.target; if (!el.dataset.k) return;
    apply(c, el);
    if (redrawKeys.has(el.dataset.k) || el.dataset.k === 'serial.port') draw(); else paint(host, c, ctx.profiles);
  });
  draw();
}

export async function submitCustom(host, ctx, preflight) {
  const c = ctx.state, errs = paint(host, c, ctx.profiles), keys = Object.keys(errs);
  if (keys.length) {
    const first = $(`[data-k="${keys[0]}"]`, host); if (first) { first.closest('details')?.setAttribute('open', ''); first.scrollIntoView({ block: 'center', behavior: 'smooth' }); first.focus({ preventScroll: true }); }
    return { ok: false, error: `Fix the ${keys.length} marked field${keys.length > 1 ? 's' : ''} first.` };
  }
  const body = toProfile(c);
  try {
    const r = c.id != null ? await api(`/profiles/${c.id}`, { method: 'PUT', body }) : await api('/profiles', { method: 'POST', body });
    return { ok: true, profile: r };
  } catch (e) {
    const box = $('#cServerErr', host); if (box) { box.hidden = false; $('#cServerMsg', host).textContent = e.message; box.scrollIntoView({ block: 'nearest' }); }
    return { ok: false, error: e.message };
  }
}
