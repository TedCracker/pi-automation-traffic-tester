// "Send from" / "Send to" / "Direction": the shared picker used by Setup (step 2), Create custom test and the Generator page.
// There are no fixed roles: every Pi is a peer. Each test names where it is sent from, where it is sent to, and which way (docs/design/ROLES-DESIGN.md).
// A Pi is an Endpoint object {pi, host, name, port, serial_port, iface}: blank = the Pi you are using; `pi` = a Pi from GET /api/peers (id); `host` = a typed address.
import { $, $$, esc, api, toast, tip, NAMES, myName, epName, DIRECTION_NAMES } from './ui.js';
import { S } from './state.js';

export const EP = { me: null, peers: [], loaded: false, err: '', ports: {}, ifaces: {}, active: null, timer: 0 };
const IP = t => t === 'udp' || t === 'tcp';
const TYPED = '__typed';
const blankEp = () => ({ pi: '', host: '', name: '', port: null, serial_port: '', iface: 'auto' });
const hasEnd = e => !!(e && (e.pi || e.host));

// ---------------------------------------------------------------- the list of Pis
export const peerOf = ref => !ref ? null : EP.peers.find(x => x.id === ref) || EP.peers.find(x => x.name && x.name.toLowerCase() === String(ref).toLowerCase()) || EP.peers.find(x => x.host === ref || (x.ips || []).includes(ref)) || null;
function setNames() {
  if (EP.me && EP.me.name) NAMES.me = EP.me.name;
  NAMES.byId = {};
  for (const p of EP.peers) { NAMES.byId[p.id] = p.name; if (p.host) NAMES.byId[p.host] = p.name; }
}
const sig = () => JSON.stringify([EP.me && EP.me.name, EP.peers.map(p => [p.id, p.name, p.reachable, p.clock && p.clock.synced, p.auth, p.busy])]);
// -> true when something changed. `refresh` asks the server to look at every Pi again first (it takes a moment when one is switched off).
export async function loadPeers(refresh = false) {
  const before = sig();
  try {
    if (refresh) await api('/peers/refresh', { method: 'POST' });
    const d = await api('/peers');
    EP.me = d.me; EP.peers = d.peers || []; EP.err = d.discovery_error || ''; EP.loaded = true; setNames();
  } catch (e) { if (e.message !== 'Password required') EP.err = e.message; }
  return sig() !== before;
}
async function loadPortsOf(key) {
  if (EP.ports[key]) return;
  try { const d = await api(key === 'self' ? '/ports' : `/peers/${encodeURIComponent(key)}/ports`); EP.ports[key] = { list: d.ports || [], error: '' }; }
  catch (e) { EP.ports[key] = { list: [], error: e.message }; }
}
async function loadIfacesOf(key) {
  if (key === 'self' || EP.ifaces[key]) return;
  try { const d = await api(`/peers/${encodeURIComponent(key)}/interfaces`); EP.ifaces[key] = { list: Object.entries(d.interfaces || {}).map(([name, v]) => ({ name, mac: v.mac, wired: v.wired })), error: '' }; }
  catch (e) { EP.ifaces[key] = { list: [], error: e.message }; }
}

// ---------------------------------------------------------------- reading an Endpoint
// The value of the Pi select for an end of the test: 'self' | a Pi's id | '__typed' | '' (not chosen yet).
export function valueOf(p, which, ui = {}) {
  const ep = p[which] || {};
  if (ep.host) { const pe = peerOf(ep.host); return pe ? pe.id : TYPED; }
  if (ep.pi === 'self' || (ep.pi && EP.me && ep.pi === EP.me.id)) return 'self';
  if (ep.pi) { const pe = peerOf(ep.pi); return pe ? pe.id : ep.pi; }
  if (which === 'source') return 'self';
  if (IP(p.transport)) return (p.net.peer_host || ui.destTyped || !EP.peers.length) ? TYPED : '';
  return '';
}
const nameOfValue = (v, p, which) => v === 'self' ? (EP.me ? EP.me.name : myName()) : v === TYPED ? ((which === 'dest' ? p.net.peer_host : (p[which] || {}).host) || 'the typed address') : v === '' ? 'the other end' : ((peerOf(v) || {}).name || (p[which] || {}).name || v);
// {self, peer, addr, none}
const kindOf = v => v === 'self' ? 'self' : v === TYPED ? 'addr' : v === '' ? 'none' : 'peer';
export function ends(p, ui = {}) {
  const fv = valueOf(p, 'source', ui), tv = valueOf(p, 'dest', ui);
  const typedEmpty = tv === TYPED && !(p.net.peer_host || '').trim();          // "Type an IP address" chosen but nothing typed yet: not named
  return { fv, tv, from: nameOfValue(fv, p, 'source'), to: nameOfValue(tv, p, 'dest'), fk: kindOf(fv), tk: typedEmpty ? 'none' : kindOf(tv) };
}
export function targetHost(p, ui = {}) {         // where this Pi's traffic goes: the typed address, or the chosen Pi's address
  const { tv } = ends(p, ui);
  if (tv === 'self') return '';
  if (tv === TYPED || tv === '') return (p.net.peer_host || '').trim();
  const pe = peerOf(tv); return pe ? pe.host : (p.net.peer_host || '').trim();
}

// ---------------------------------------------------------------- clocks and what is possible
function clockOf(p, which, ui) {
  const v = valueOf(p, which, ui), name = nameOfValue(v, p, which);
  if (v === 'self') { const c = S.status && S.status.clock; return c ? { name, state: c.synced ? 'ok' : 'bad', off: c.offset_ms } : { name, state: 'unknown' }; }
  const pe = v === TYPED || v === '' ? null : peerOf(v);
  if (!pe || !pe.reachable || !pe.clock) return { name, state: 'unknown' };
  return { name, state: pe.clock.synced ? 'ok' : 'bad', off: pe.clock.offset_ms };
}
// Why a direction cannot be used right now ('' = it can).
export function directionReason(p, d, ui = {}) {
  if (d === 'round_trip') return '';
  if (p.mode === 'mirrored') return 'Mirrored Bits mode already sends both ways all the time, and times the round trip.';
  const e = ends(p, ui);
  if (d === 'both') {
    if (p.transport === 'serial' || p.transport === 'goose_l2' || p.transport === 'goose_l3') return 'not for serial or GOOSE: a port cannot send and answer at the same time.';
    if (e.tk === 'addr') return `${e.to} is a typed address, not a Pi in your list, so this Pi cannot start a test there. Add it with “Add a Pi by IP”.`;
    return '';
  }
  if (d === 'reverse' && e.tk === 'addr') return `${e.to} is a typed address, not a Pi in your list, so this Pi cannot start a test there. Add it with “Add a Pi by IP”.`;
  const bad = [clockOf(p, 'source', ui), clockOf(p, 'dest', ui)].filter(c => c.state === 'bad');
  if (bad.length) return `${bad.map(c => c.name).join(' and ')}: the clock is not synced. One-way timing needs both Pis’ clocks synced (Setup → Time).`;
  return '';
}
const DIRS = [
  ['round_trip', 'Round trip', () => 'The far end sends every message straight back, and the sending Pi times the whole trip. The classic test; works with every link type.'],
  ['forward', 'One way: from → to', e => `Times only the trip from ${e.from} to ${e.to}. Needs both Pis’ clocks synced.`],
  ['reverse', 'One way: to → from', e => `Times only the trip from ${e.to} to ${e.from}. Needs both Pis’ clocks synced.`],
  ['both', 'Both ways at once', e => `${e.from} and ${e.to} send to each other at the same time; each direction is timed as a round trip. UDP and TCP only.`],
];
// Problems with the choice itself (before anything is asked of the network): [{s, title, text, link?}]
export function endpointProblems(p, ui = {}) {
  const out = [], d = p.direction || 'round_trip', r = directionReason(p, d, ui);
  if (r) out.push({ s: 'bad', title: `${DIRECTION_NAMES[d]} is not possible here`, text: r.charAt(0).toUpperCase() + r.slice(1), link: /clock/.test(r) ? { href: '#config/time', label: 'Setup → Time', testid: 'setup-time-source-link' } : null });
  return out;
}

// ---------------------------------------------------------------- the pieces of markup
export const peerLabel = pe => `${pe.name} ${pe.reachable ? '✓' : '✗'}${pe.reachable ? (pe.clock ? (pe.clock.synced ? ' · clock synced' : ' · clock not synced') : '') : ' · not answering'}${pe.auth === 'needs_password' ? ' · password needed' : ''}${pe.busy ? ' · busy' : ''}`;
// The <option>s of a Pi select. `ep` = the Endpoint being edited (for a Pi that is no longer in the list), `typed` adds "Type an IP address…", `blank` = text of an empty first choice.
export function optionsFor(cur, ep, { typed = false, blank = '' } = {}) {
  const me = EP.me ? EP.me.name : myName();
  let o = '';
  if (blank) o += `<option value="" ${cur === '' ? 'selected' : ''}>${esc(blank)}</option>`;
  o += `<option value="self" ${cur === 'self' ? 'selected' : ''}>This Pi (${esc(me)})</option>`;
  for (const pe of EP.peers) o += `<option value="${esc(pe.id)}" ${cur === pe.id ? 'selected' : ''}>${esc(peerLabel(pe))}</option>`;
  if (cur && cur !== 'self' && cur !== TYPED && !peerOf(cur)) o += `<option value="${esc(cur)}" selected>${esc((ep && ep.name) || cur)} (not in your list)</option>`;
  if (typed) o += `<option value="${TYPED}" ${cur === TYPED ? 'selected' : ''}>Type an IP address…</option>`;
  return o;
}
function piOptions(p, which, cur) {
  const typed = which === 'dest' && IP(p.transport);
  return optionsFor(cur, p[which], { typed, blank: which === 'dest' && cur === '' ? `Choose a Pi${typed ? ' or type an address' : ''}…` : '' });
}
// The select value of one Endpoint on its own (generator streams): 'self' | a Pi's id | '__typed' | `blank`.
export function valueOfEp(ep, blank = 'self') {
  ep = ep || {};
  if (ep.host) { const pe = peerOf(ep.host); return pe ? pe.id : TYPED; }
  if (ep.pi === 'self' || (ep.pi && EP.me && ep.pi === EP.me.id)) return 'self';
  if (ep.pi) { const pe = peerOf(ep.pi); return pe ? pe.id : ep.pi; }
  return ep.typed ? TYPED : blank;
}
export { TYPED };

// One Pi select (+ the box for a typed address). `attrs` go on the select. Used by the trip tests and, per stream, by the generator.
export function pickerHtml({ p, which, ui, label, attrs = '', hostAttrs = '', testid, hostTestid, tipText = '' }) {
  const cur = valueOf(p, which, ui);
  const hostVal = which === 'dest' ? (p.net.peer_host || '') : ((p[which] || {}).host || '');
  return `<div class="epcol"><label class="f"><span>${esc(label)} ${tipText ? tip(tipText) : ''}</span><select ${attrs} data-ep="${which}" data-ep-key="pi-${which}" data-testid="${testid}">${piOptions(p, which, cur)}</select></label>
    ${cur === TYPED ? `<label class="f eptyped"><span>IP address or name of the Pi</span><input ${hostAttrs} data-ep="host" data-which="${which}" data-ep-key="host-${which}" data-testid="${hostTestid}" ${which === 'dest' ? 'data-path="net.peer_host"' : ''} inputmode="decimal" autocomplete="off" placeholder="e.g. 192.168.1.50" value="${esc(hostVal)}"></label>` : ''}</div>`;
}
function dirOpt(value, checked, title, text, off) {
  return `<label class="opt ${off ? 'off' : ''}"><input type="radio" name="ep-direction" value="${value}" data-ep="direction" data-ep-key="dir-${value}" data-testid="setup-direction-${value}" ${checked ? 'checked' : ''} ${off ? 'disabled' : ''}>
    <span><b>${esc(title)}</b><small ${off ? 'data-why="1"' : ''}>${esc(off ? 'Not available here: ' + off : text)}</small></span></label>`;
}
function clockLine(p, ui, e) {
  const d = p.direction || 'round_trip';
  if (!(e.fk === 'peer' || e.tk === 'peer' || d === 'forward' || d === 'reverse')) return '';
  const cs = [clockOf(p, 'source', ui), clockOf(p, 'dest', ui)].filter((c, i, a) => c.name && (i === 0 || c.name !== a[0].name));
  const known = cs.some(c => c.state !== 'unknown') || cs.length > 1;
  if (!known) return '';
  const txt = c => `${esc(c.name)}: ${c.state === 'ok' ? 'clock synced' + (c.off != null ? ` (${Math.abs(c.off) < 10 ? Number(c.off).toFixed(1) : Math.round(c.off)} ms off)` : '') : c.state === 'bad' ? '<b>clock not synced</b>' : 'clock checked when you press Check'}`;
  return `<p class="muted epclocks" data-testid="setup-clocks">${cs.map(txt).join(' · ')} · <a href="#config/time" data-testid="setup-time-source-link">Setup → Time</a></p>`;
}
const portLine = (what, name, inner) => `<label class="f"><span>${esc(what)} <b>${esc(name)}</b></span>${inner}</label>`;
function serialSelect(p, which, e, ui) {
  const v = which === 'source' ? e.fv : e.tv, key = v === 'self' ? 'self' : v;
  if (v === '' || v === TYPED) return `<p class="muted">Choose the Pi that answers to pick its serial port.</p>`;
  const st = EP.ports[key], list = st ? st.list : [], ep = p[which] || {};
  const cur = ep.serial_port || (which === 'source' ? p.serial.port : 'auto');
  const known = cur === 'auto' || list.some(x => x.path === cur);
  const name = which === 'source' ? e.from : e.to, sends = (which === 'source') === (p.direction !== 'reverse');
  const sel = `<select data-ep="serial-port" data-which="${which}" data-ep-key="sp-${which}" data-testid="setup-${which === 'source' ? 'from' : 'to'}-serial-port"><option value="auto" ${cur === 'auto' ? 'selected' : ''}>Auto-detect (recommended)</option>${list.map(x => `<option value="${esc(x.path)}" ${cur === x.path ? 'selected' : ''}>${esc(x.label)} — ${esc(x.path)}${x.adapter_slow ? ' ⚠ slow adapter' : ''}</option>`).join('')}${known ? '' : `<option value="${esc(cur)}" selected>${esc(cur)}</option>`}</select>`;
  return `${portLine('Serial port on', name, sel)}
    <small class="muted">${sends ? 'This port sends the test messages.' : 'This port is the reflector: it sends every message straight back.'}${st && st.error ? ` Could not list its ports: ${esc(st.error)}.` : list.length || !st ? '' : ' No serial adapter found on that Pi yet.'}</small>
    <label class="f"><span>Or type a port path</span><input data-ep="serial-typed" data-which="${which}" data-ep-key="spt-${which}" data-testid="setup-${which === 'source' ? 'from' : 'to'}-serial-path" placeholder="/dev/ttyUSB0" autocomplete="off" value="${cur !== 'auto' && !list.some(x => x.path === cur) && st ? esc(cur) : ''}"></label>`;
}
function ifaceSelect(p, which, e) {
  const v = which === 'source' ? e.fv : e.tv;
  if (v === '' || v === TYPED) return `<p class="muted">Choose the Pi that answers to pick its network port.</p>`;
  const ep = p[which] || {}, cur = ep.iface && ep.iface !== 'auto' ? ep.iface : (which === 'source' && p.goose.interface && p.goose.interface !== 'auto' ? p.goose.interface : 'auto');
  const list = v === 'self' ? (S.status.interfaces || []) : ((EP.ifaces[v] || {}).list || []), name = which === 'source' ? e.from : e.to;
  const sel = `<select data-ep="iface" data-which="${which}" data-ep-key="if-${which}" data-testid="setup-${which === 'source' ? 'from' : 'to'}-iface"><option value="auto" ${cur === 'auto' ? 'selected' : ''}>Auto (first wired port)</option>${list.map(i => `<option value="${esc(i.name)}" ${cur === i.name ? 'selected' : ''}>${esc(i.name)}${i.wired ? '' : ' (wireless/virtual)'}${i.mac ? ' · ' + esc(i.mac) : ''}</option>`).join('')}${cur !== 'auto' && !list.some(i => i.name === cur) ? `<option value="${esc(cur)}" selected>${esc(cur)}</option>` : ''}</select>`;
  return portLine('Network port on', name, sel);
}

// ---------------------------------------------------------------- the whole block
// opts.ports: also show the port of each Pi (Setup step 2). The custom-test form leaves the ports to the Pi's own settings.
export function endpointsHtml(p, ui = {}, opts = {}) {
  const e = ends(p, ui), t = p.transport, d = p.direction || 'round_trip';
  const dirs = DIRS.map(([v, title, text]) => dirOpt(v, d === v, title, text(e), directionReason(p, v, ui))).join('');
  const found = EP.peers.length;
  const bar = `<div class="row epbar"><span class="muted" data-testid="setup-pis-count" role="status">${EP.loaded ? (found ? `${found} other Pi${found > 1 ? 's' : ''} on this network` : 'No other Pi found yet') : 'Looking for other Pis…'}</span>
      <button type="button" class="btn small" data-ep-act="refresh" data-testid="setup-refresh-pis" title="Look for Pis on the network again">↻ Look again</button>
      <button type="button" class="btn small" data-ep-act="add" data-testid="setup-add-pi" title="Add a Pi that does not show up by itself, by its IP address">＋ Add a Pi by IP</button></div>`;
  let ports = '';
  if (opts.ports) {
    if (t === 'serial') ports = `<div class="grid g2 epports">${['source', 'dest'].map(w => `<div>${serialSelect(p, w, e, ui)}</div>`).join('')}</div>`;
    else if (IP(t)) ports = `<div class="grid g2 epports"><label class="f"><span>Port ${tip('Every Pi always answers on port 20500, so this normally stays as it is. If you change it, the Pi that answers opens the new port for the test.')}</span><input type="number" data-ep="net-port" data-ep-key="net-port" data-testid="setup-port" data-path="net.port" value="${esc(p.net.port)}"></label></div>`;
    else ports = `<div class="grid g2 epports">${['source', 'dest'].map(w => `<div>${ifaceSelect(p, w, e)}</div>`).join('')}</div>`;
  }
  return `<div class="grid g2">${pickerHtml({ p, which: 'source', ui, label: 'Send from', testid: 'setup-from', hostTestid: 'setup-from-host', tipText: 'The Pi that sends the test messages (the one you are using, or another Pi on the network).' })}
      ${pickerHtml({ p, which: 'dest', ui, label: 'Send to', testid: 'setup-to', hostTestid: 'setup-to-host', tipText: 'reflector' })}</div>
    ${bar}${EP.err && !found ? `<p class="muted">${esc(EP.err)}</p>` : ''}
    <h3 class="epdir">Direction ${tip('direction')}</h3><div class="opts" role="radiogroup" aria-label="Direction" data-testid="setup-direction">${dirs}</div>
    ${clockLine(p, ui, e)}${ports}`;
}

// ---------------------------------------------------------------- behaviour
const endpoint = (value, prev, p, which) => {
  if (value === 'self') return { ...blankEp(), pi: 'self', name: EP.me ? EP.me.name : myName() };
  if (value === TYPED || value === '') return { ...blankEp(), host: (prev && prev.host) || '', ...(value === TYPED ? { typed: true } : {}) };
  const pe = peerOf(value);
  return { ...blankEp(), pi: value, name: pe ? pe.name : (prev && prev.name) || '' };
};
export const epFromValue = endpoint;

// ctx = { get() -> the profile as it would run, set(path, value), changed(delayMs), ui: {destTyped} }
export function mountEndpoints(box, ctx, opts = {}) {
  if (!box) return;
  EP.active = { box, ctx, opts };
  box.setAttribute('data-ep-root', '1');
  const paint = () => {
    const a = document.activeElement, key = a && box.contains(a) && a.dataset ? a.dataset.epKey : null, pos = a && a.selectionStart != null && a.type !== 'number' ? a.selectionStart : null;
    box.innerHTML = endpointsHtml(ctx.get(), ctx.ui || {}, opts);
    if (key) { const el = box.querySelector(`[data-ep-key="${key}"]`); if (el) { el.focus({ preventScroll: true }); if (pos != null && el.setSelectionRange) try { el.setSelectionRange(pos, pos); } catch { } } }
  };
  box._paint = paint;
  const typing = () => { const a = document.activeElement; return !!(a && box.contains(a) && (a.tagName === 'INPUT' && a.type !== 'radio' && a.type !== 'checkbox')); };
  box._typing = typing;
  const loadMore = async () => {
    const p = ctx.get(), e = ends(p, ctx.ui || {});
    if (!EP.loaded) await loadPeers();
    const jobs = [];
    if (opts.ports && p.transport === 'serial') for (const v of [e.fv, e.tv]) if (v && v !== TYPED) jobs.push(loadPortsOf(v === 'self' ? 'self' : v));
    if (opts.ports && (p.transport === 'goose_l2' || p.transport === 'goose_l3')) for (const v of [e.fv, e.tv]) if (v && v !== TYPED && v !== 'self') jobs.push(loadIfacesOf(v));
    await Promise.all(jobs);
    if (EP.active && EP.active.box === box && box.isConnected && !typing()) paint();
  };
  box._load = loadMore;
  const changed = d => { if (ctx.changed) ctx.changed(d); };
  const apply = (path, v) => ctx.set(path, v);
  const redraw = () => { paint(); loadMore(); };

  const onChange = async ev => {
    const el = ev.target, k = el.dataset && el.dataset.ep; if (!k) return;
    const p = ctx.get(), w = el.dataset.which;
    if (k === 'source' || k === 'dest') {
      const prev = p[k] || {}, ep = endpoint(el.value, prev, p, k);
      ctx.ui = ctx.ui || {};
      if (k === 'dest') ctx.ui.destTyped = el.value === TYPED;
      apply(k, ep);
      if (k === 'source' && el.value === 'self') { /* keep the test's own serial port / interface as the sending Pi's port */ }
      const q = ctx.get(), d = q.direction || 'round_trip';
      if (d !== 'round_trip' && directionReason(q, d, ctx.ui || {})) { apply('direction', 'round_trip'); toast('Direction set back to Round trip: the other choices are not possible with this selection.', false, 5000); }
      redraw(); changed(250);
    } else if (k === 'direction') {
      if (!el.disabled) { apply('direction', el.value); redraw(); changed(250); }
    } else if (k === 'serial-port' || k === 'serial-typed') {
      const v = (el.value || '').trim() || (k === 'serial-typed' ? '' : 'auto');
      if (k === 'serial-typed' && !v) return;
      apply(`${w}.serial_port`, v);
      if (w === 'source' && ends(p, ctx.ui || {}).fv === 'self') apply('serial.port', v);
      redraw(); changed(250);
    } else if (k === 'iface') {
      apply(`${w}.iface`, el.value);
      if (w === 'source' && ends(p, ctx.ui || {}).fv === 'self') apply('goose.interface', el.value);
      redraw(); changed(250);
    } else if (k === 'net-port') { apply('net.port', el.value === '' ? null : Number(el.value)); changed(250); }
    else if (k === 'host') { apply('net.peer_host', el.value.trim()); changed(250); }
  };
  const onInput = ev => {
    const el = ev.target, k = el.dataset && el.dataset.ep;
    if (k === 'host') { apply('net.peer_host', el.value.trim()); changed(700); }
    else if (k === 'net-port') { apply('net.port', el.value === '' ? null : Number(el.value)); changed(700); }
  };
  const onClick = async ev => {
    const b = ev.target.closest('[data-ep-act]'); if (!b) return;
    if (b.dataset.epAct === 'refresh') { b.disabled = true; b.textContent = 'Looking…'; try { await loadPeers(true); } finally { redraw(); } }
    else if (b.dataset.epAct === 'rescan') { EP.ports = {}; redraw(); }
    else if (b.dataset.epAct === 'add') { const pe = await addPiSheet(); if (pe) { await loadPeers(); redraw(); toast(`Added ${pe.name}.`); } }
  };
  box.addEventListener('change', box._onChange = onChange);
  box.addEventListener('input', box._onInput = onInput);
  box.addEventListener('click', box._onClick = onClick);
  paint(); loadMore();
  if (!EP.timer) EP.timer = setInterval(tick, 10000);
}

// Keep a screen's Pi list fresh without a picker of its own (the generator page): `repaint()` is called when the list changed and nobody is typing.
export function watchPeers(box, repaint) {
  box._paint = repaint; box._load = () => { }; box._typing = () => { const a = document.activeElement; return !!(a && box.contains(a) && a.tagName === 'INPUT' && a.type !== 'checkbox'); };
  EP.active = { box, ctx: null, opts: {} };
  if (!EP.timer) EP.timer = setInterval(tick, 10000);
}

// Every 10 s while a picker is on screen: ask for the list again (a Pi that went away or came back, a clock that got synced).
async function tick() {
  const a = EP.active;
  if (!a || !a.box.isConnected) { EP.active = null; return; }
  if (document.hidden) return;
  if (await loadPeers() && !a.box._typing()) { a.box._paint(); a.box._load(); }
}

// ---------------------------------------------------------------- Add a Pi by IP (the full list lives in Settings)
export function addPiSheet() {
  return new Promise(res => {
    const m = $('#modal');
    m.innerHTML = `<div class="box wide" role="dialog" aria-modal="true" aria-label="Add a Pi by IP address" data-testid="add-pi-sheet"><div class="row sp sheet-head"><h2>Add a Pi by IP address</h2><button type="button" class="icon sheet-x" id="apX" aria-label="Close" data-testid="add-pi-close">✕</button></div>
      <div class="sheet-body"><p class="muted">Use this when a Pi does not show up by itself (some networks block the way Pis find each other). The full list of Pis is in Settings.</p>
        <label class="f">IP address or name<input id="apHost" data-testid="add-pi-address" inputmode="decimal" autocomplete="off" placeholder="e.g. 192.168.1.51 or 192.168.1.51:8080"></label>
        <label class="f" style="margin-top:10px">Its password (only if you set one on that Pi)<input id="apPw" type="password" autocomplete="off" data-testid="add-pi-password"></label>
        <div class="check bad" id="apErr" role="alert" data-testid="add-pi-error" hidden style="margin-top:10px"><div class="ic">✕</div><div><b>Could not add it</b><small id="apMsg"></small></div></div></div>
      <div class="row sheet-acts"><button type="button" class="btn" id="apNo" data-testid="add-pi-cancel">Cancel</button><button type="button" class="btn primary" id="apGo" data-testid="add-pi-go">Add this Pi</button></div></div>`;
    m.hidden = false;
    const done = v => { document.removeEventListener('keydown', key, true); m.onclick = null; m.hidden = true; m.innerHTML = ''; res(v); };
    const key = e => { if (e.key === 'Escape') { e.stopPropagation(); done(null); } };
    document.addEventListener('keydown', key, true);
    m.onclick = e => { if (e.target === m) done(null); };
    $('#apX').onclick = $('#apNo').onclick = () => done(null);
    const go = async () => {
      const host = $('#apHost').value.trim(); if (!host) { $('#apMsg').textContent = 'Type the IP address of the other Pi.'; $('#apErr').hidden = false; return; }
      const b = $('#apGo'); b.disabled = true; b.textContent = 'Looking…'; $('#apErr').hidden = true;
      try { const pe = await api('/peers', { method: 'POST', body: { host, password: $('#apPw').value } }); done(pe); }
      catch (er) { b.disabled = false; b.textContent = 'Add this Pi'; $('#apMsg').textContent = er.message; $('#apErr').hidden = false; }
    };
    $('#apGo').onclick = go;
    m.querySelectorAll('input').forEach(i => i.onkeydown = e => { if (e.key === 'Enter') go(); });
    $('#apHost').focus();
  });
}

// ---------------------------------------------------------------- remembered in this browser, like the old "type the address once"
const KEY = 'tt-conn-ep';
const slim = e => { e = e || {}; return { pi: e.pi || '', host: e.host || '', name: e.name || '' }; };
export function rememberEndpoints(p) {
  if (p.kind && p.kind !== 'trip') return;
  try { localStorage.setItem(KEY, JSON.stringify({ source: slim(p.source), dest: slim(p.dest), direction: p.direction || 'round_trip', host: (p.net && p.net.peer_host) || '' })); } catch { }
}
// Overrides for a test that leaves both ends open (an older test, or a new one): the ends and the direction of the last test started in this browser.
export function prefillEndpoints(p) {
  let c; try { c = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch { c = null; }
  if (!c || hasEnd(p.source) || hasEnd(p.dest) || (p.net && (p.net.peer_host || '').trim())) return {};
  const ok = e => !e.pi || e.pi === 'self' || !!peerOf(e.pi);
  if (!ok(c.source) || !ok(c.dest)) return {};
  const o = {};
  if (c.source.pi) o.source = { ...blankEp(), ...c.source };
  if (c.dest.pi) o.dest = { ...blankEp(), ...c.dest };
  else if (IP(p.transport) && (c.dest.host || c.host)) { o.net = { peer_host: c.dest.host || c.host }; }
  const probe = { ...p, ...o, source: o.source || p.source, dest: o.dest || p.dest, net: { ...(p.net || {}), ...(o.net || {}) } };
  if (c.direction && c.direction !== 'round_trip' && !directionReason({ ...probe, direction: c.direction }, c.direction)) o.direction = c.direction;
  return o;
}

// A typed address is also the destination (kept in net.peer_host for the code paths that read it).
export function normalizeEndpoints(p) {
  if (!p || (p.kind && p.kind !== 'trip')) return p;
  p.source = { ...blankEp(), ...(p.source || {}) }; p.dest = { ...blankEp(), ...(p.dest || {}) };
  delete p.source.typed; delete p.dest.typed;
  if (IP(p.transport) && !hasEnd(p.dest) && (p.net.peer_host || '').trim()) p.dest.host = p.net.peer_host.trim();
  if (p.dest.host && IP(p.transport)) p.net.peer_host = p.dest.host;
  return p;
}
