// "New test": one page, four rows, in the order a test set asks its questions (docs/WIZARD-PROPOSAL.md, docs/SPEC.md 6.4):
//   1 Port (on this Pi)  ->  2 Protocol (only what that port can carry)  ->  3 From / To (who sends, which way, to one Pi or several)  ->  4 Traffic
// and a Start bar that is always in the same place. The same page creates a test, edits a saved one and runs one from the list.
// Nothing here assumes two Pis: "To" is a tick list. A saved test is an ordinary profile (profiles API), as before.
import { $, $$, esc, api, toast, tip, dur, bindOnce, sheetBox, confirmBox, myName } from './ui.js';
import { S, resetLive } from './state.js';
import { loadCustomLimits, newDraft, editDraft, isDirty, derive, validate, toProfile, field, select, check, errEl, applyField, customData, codecChoice } from './custom.js';
import { EP, loadPeers, peerOf, addPiSheet, rememberEndpoints } from './endpoints.js';
import { mbFieldsHtml } from './mbsettings.js';
import { radioFormHtml, loadUsage, usageBlock, usageKey, repeatSheet } from './radioui.js';
import { wifiCheck, turnOff } from './wifi.js';

const N = { c: null, mode: 'new', ports: [], goose: { ok: true }, portId: null, open: {}, checks: [], typed: false, root: null, cb: {}, seq: 0, timer: 0, far: {}, otherBaud: false, extras: null };
export const ntState = N;
const IPT = t => t === 'udp' || t === 'tcp';
const GOOSE = t => t === 'goose_l2' || t === 'goose_l3';
const ONE_FAR = p => p.transport === 'serial' || GOOSE(p.transport) || p.mode === 'mirrored';
const blankEp = () => ({ pi: '', host: '', name: '', port: null, serial_port: '', iface: 'auto' });
const meName = () => (EP.me && EP.me.name) || myName();
const isSelf = e => !e || (!e.host && (!e.pi || e.pi === 'self' || !!(EP.me && e.pi === EP.me.id)));
const named = e => !!(e && (e.pi || e.host));
const PORT_KEY = 'tt-port';

// ---------------------------------------------------------------- the test as "this Pi, the far Pis, which way"
// -> { flow: 'out' (this Pi sends) | 'in' (the far Pis send to this Pi) | 'other' (set up between other Pis), far: [Endpoint], dir: round_trip | one_way | both }
export function model(p) {
  let src = [p.source || {}, ...(p.sources || [])], dst = [p.dest || {}, ...(p.dests || [])];
  if (!named(dst[0]) && IPT(p.transport) && (p.net.peer_host || '').trim()) dst[0] = { ...blankEp(), host: p.net.peer_host.trim() };
  let d = p.direction || 'round_trip';
  if (d === 'reverse') { [src, dst] = [dst, src]; d = 'forward'; }
  const dir = d === 'forward' ? 'one_way' : d;
  if (src.length === 1 && isSelf(src[0])) return { flow: 'out', far: dst.filter(e => named(e) && !isSelf(e)), dir, here: src[0] };
  if (dst.length === 1 && isSelf(dst[0])) return { flow: 'in', far: src.filter(e => named(e) && !isSelf(e)), dir, here: dst[0] };
  return { flow: 'other', far: [], dir, here: {}, src, dst };
}
export function setModel(p, m) {
  if (m.flow === 'other') return p;
  const ser = p.transport === 'serial';
  const here = { ...blankEp(), pi: 'self', name: meName(), serial_port: ser ? (N.portId || '') : '', iface: ser ? 'auto' : (N.portId || 'auto') };
  const far = m.far.map(e => ({ ...blankEp(), ...e }));
  if (ONE_FAR(p)) far.splice(1);
  if (m.flow === 'in') { p.source = far[0] || blankEp(); p.sources = far.slice(1); p.dest = here; p.dests = []; }
  else { p.source = here; p.sources = []; p.dest = far[0] || blankEp(); p.dests = far.slice(1); }
  p.direction = p.mode === 'mirrored' ? 'round_trip' : m.dir === 'one_way' ? 'forward' : m.dir;
  if (IPT(p.transport)) p.net.peer_host = m.flow === 'out' && far[0] && far[0].host && !far[0].pi ? far[0].host : '';
  if (ser) p.serial.port = N.portId || 'auto';
  if (GOOSE(p.transport)) p.goose.interface = N.portId || 'auto';
  return p;
}
const farName = e => e.host && !e.pi ? ((peerOf(e.host) || {}).name || e.host) : ((peerOf(e.pi) || {}).name || e.name || e.pi);
const farKey = e => e.pi ? ((peerOf(e.pi) || {}).id || e.pi) : 'host:' + e.host;
const list = a => a.length <= 1 ? (a[0] || '') : a.slice(0, -1).join(', ') + ' and ' + a.at(-1);
const ARROW = { one_way: '→', round_trip: '⇄', both: '⇆' };

// Why a direction (or the swap) cannot be used now ('' = it can).
export function dirReason(p, m, d) {
  if (p.mode === 'mirrored') return d === 'round_trip' ? '' : 'Mirrored Bits always sends both ways and times the round trip.';
  const typed = m.far.some(e => e.host && !e.pi && !peerOf(e.host));
  if (d === 'both') {
    if (p.transport === 'serial' || GOOSE(p.transport)) return 'Both ways is not possible here: a port cannot send and answer at once. Use Mirrored Bits for nonstop both-way traffic.';
    if (typed) return 'A typed address is not a Pi in your list, so it can only answer. Add it with “Add a Pi by IP”.';
  }
  if (d === 'swap' && typed) return 'A typed address is not a Pi in your list, so this Pi cannot start a test there. Add it with “Add a Pi by IP”.';
  return '';
}

// ---------------------------------------------------------------- ports and protocols
const portOf = id => N.ports.find(x => x.id === id) || null;
function defaultPort(kindWanted) {
  const usable = N.ports.filter(x => x.kind === 'serial' ? kindWanted === 'serial' : kindWanted !== 'serial');
  let last = null; try { last = localStorage.getItem(PORT_KEY); } catch { }
  return usable.find(x => x.id === last && x.link) || usable.find(x => x.link && (x.kind === 'serial' || x.ip) && !x.you_are_here) || usable.find(x => x.link && (x.kind === 'serial' || x.ip)) || null;
}
// The port the page shows as chosen: the one picked, or (a saved test left on "automatic") the one the Pi would use.
function shownPort() {
  const p = N.c.p;
  return portOf(N.portId) || (N.portId ? { id: N.portId, kind: p.transport === 'serial' ? 'serial' : 'eth', name: N.portId.split('/').pop(), label: p.transport === 'serial' ? 'Serial' : 'Port', link: false, state: 'not found on this Pi', missing: true } : null);
}
// The port that reaches a far Pi: the one on the same network (first three numbers of the address); none = automatic (the Pi's own routing).
const net3 = ip => (ip || '').split('.').slice(0, 3).join('.');
export function portToward(ports, far) {
  const pe = far && (far.pi ? peerOf(far.pi) : peerOf(far.host)), ips = [far && far.host, pe && pe.host].filter(Boolean);      // the address the test will use, not every address the Pi has
  for (const a of ips) { const x = ports.find(q => q.kind !== 'serial' && q.link && q.ip && net3(q.ip) === net3(a)); if (x) return x.id; }
  return null;
}
function autoPort() {           // the user has not picked a port: follow the first far Pi
  const p = N.c.p; if (N.portPicked || p.transport === 'serial') return;
  const m = model(p); N.portId = m.far[0] ? portToward(N.ports, m.far[0]) : null; setModel(p, m);
}
const portKind = () => { const sp = shownPort(); return sp ? sp.kind : (N.c.p.transport === 'serial' ? 'serial' : 'eth'); };
export function protocols(kind, goose) {
  const g = goose && goose.ok ? '' : 'Needs the installed service (raw network access).';
  if (kind === 'serial') return [['serial', 'echo', 'Serial', 'Raw bytes on RS-232 / RS-485', ''], ['serial', 'mirrored', 'Mirrored Bits', 'Relay style: both ends send nonstop', '']];
  return [['udp', 'echo', 'UDP', 'Layer 4 · the simplest place to start', ''], ['tcp', 'echo', 'TCP', 'Layer 4 · one session; also times set-up and recovery', ''],
    ['goose_l3', 'echo', 'Routed GOOSE', 'Layer 3 · R-GOOSE over UDP multicast', g], ['goose_l2', 'echo', 'GOOSE', 'Layer 2 · IEC 61850 frames on Ethernet', kind === 'wifi' ? 'GOOSE needs a wired Ethernet port.' : g],
    ['udp', 'mirrored', 'Mirrored Bits · UDP', 'Relay style, over IP', ''], ['tcp', 'mirrored', 'Mirrored Bits · TCP', 'Relay style, over IP', '']];
}
const protoName = p => (protocols(p.transport === 'serial' ? 'serial' : 'eth', { ok: true }).find(x => x[0] === p.transport && x[1] === (p.mode === 'mirrored' ? 'mirrored' : 'echo')) || [])[2] || p.transport;

function fit(p) {         // after any change of port / protocol: drop what no longer applies, and say so
  const m = model(p), dropped = [];
  if (ONE_FAR(p) && m.far.length > 1) { dropped.push(`only ${farName(m.far[0])} is kept (this protocol runs between one pair of Pis)`); m.far.splice(1); }
  if (m.dir !== 'round_trip' && dirReason(p, m, m.dir)) { m.dir = 'round_trip'; dropped.push('direction set back to Round trip'); }
  if (!IPT(p.transport) && m.far.some(e => e.host && !e.pi)) { m.far = m.far.filter(e => e.pi); N.typed = false; dropped.push('the typed address is dropped (pick the Pi at the far end)'); }
  setModel(p, m); derive(N.c);
  if (dropped.length) toast(dropped.join('; ').replace(/^./, c => c.toUpperCase()) + '.', false, 6000);
}
function choosePort(id) {
  const p = N.c.p;
  if (!id) { if (p.transport === 'serial') { p.transport = 'udp'; toast('Protocol set to UDP: pick another one in row 2 if you need to.', false, 5000); } N.portId = null; N.portPicked = true; return fit(p); }
  const port = portOf(id); if (!port) return;
  const ser = port.kind === 'serial';
  if (ser !== (p.transport === 'serial')) { p.transport = ser ? 'serial' : 'udp'; toast(`Protocol set to ${ser ? 'Serial' : 'UDP'}: pick another one in row 2 if you need to.`, false, 5000); }
  if (port.kind === 'wifi' && p.transport === 'goose_l2') { p.transport = 'udp'; toast('GOOSE (Layer 2) needs a wired Ethernet port: protocol set to UDP.', false, 6000); }
  N.portId = id; N.portPicked = true; try { localStorage.setItem(PORT_KEY, id); } catch { }
  fit(p);
}
function chooseProto(t, mode) { const p = N.c.p; p.transport = t; p.mode = mode === 'mirrored' ? 'mirrored' : 'echo'; fit(p); }

// ---------------------------------------------------------------- summaries (the one line of a closed row, and the sentence in the Start bar)
function endsLine(p, m) {
  if (m.flow === 'other') return `${list(m.src.map(e => isSelf(e) ? meName() : farName(e)))} → ${list(m.dst.map(e => isSelf(e) ? meName() : farName(e)))}`;
  if (!m.far.length) return 'Choose where to send';
  const far = list(m.far.map(farName)), a = m.flow === 'in' ? far : meName(), b = m.flow === 'in' ? meName() : far;
  return `${a} ${ARROW[m.dir]} ${b} · ${{ one_way: 'One way', round_trip: 'Round trip', both: 'Both ways' }[m.dir]}${m.far.length > 1 ? ` · ${m.far.length} links` : ''}`;
}
function trafficLine(p) {
  if (N.extras && N.extras.line) { const s = N.extras.line(N.c); if (s) return s; }
  const rule = p.rule.percentile === 100 ? 'slowest' : 'p' + p.rule.percentile;
  if (p.mode === 'mirrored') return `${p.mirrored.codec === 'sim' ? 'real Mirrored Bits load' : 'timed'} · nonstop at ${p.transport === 'serial' ? p.serial.baud : p.mirrored.rate_baud} baud · ${dur(p.duration_s)} · pass if ${rule} ≤ ${p.threshold_ms} ms${p.mirrored.codec === 'sim' ? '' : ' one way (est.)'}`;
  return `1 message / ${p.interval_ms} ms · ${dur(p.duration_s)} · pass if ${rule} ≤ ${p.threshold_ms} ms`;
}
export function sentence(p, m) {
  if (m.flow === 'other' || !m.far.length) return '';
  const far = m.far.map(farName), n = far.length, links = n > 1 ? ` (${n * (m.dir === 'both' ? 2 : 1)} links)` : '';
  const how = { round_trip: 'Each link is timed as a round trip', one_way: 'Each link is timed one way (the clocks must agree)', both: 'Both directions run at the same time, each timed as a round trip' }[m.dir].replace(/^Each link/, n > 1 ? 'Each link' : 'It');
  if (p.mode === 'mirrored') return `${meName()} and ${far[0]} both send Mirrored Bits messages nonstop; a trip bit is timed there and back.`;
  const who = m.dir === 'both' ? `${meName()} and ${list(far)} send to each other${links}` : m.flow === 'in' ? `${list(far)} send${n > 1 ? '' : 's'} to ${meName()}${links}` : `${meName()} sends to ${list(far)}${links}`;
  return `${who}. ${how}.`;
}
export function autoName(p) { const m = model(p); return `${protoName(p)} · ${endsLine(p, m).split(' · ')[0]}`.slice(0, 80); }

// ---------------------------------------------------------------- markup
const chip = (attrs, on, title, sub, off = '') => `<button type="button" class="ntchip${on ? ' on' : ''}${off ? ' off' : ''}" ${attrs} aria-pressed="${on ? 'true' : 'false'}" ${off ? 'aria-disabled="true"' : ''}><b>${title}</b>${sub || off ? `<small>${esc(off || sub)}</small>` : ''}</button>`;
function rowHtml(id, n, title, line, ok, body) {
  const open = !!N.open[id];
  return `<section class="ntrow${open ? ' open' : ''}${ok ? ' ok' : ''}" data-row="${id}" data-testid="nt-row-${id}">
    <button type="button" class="nthead" data-nt="toggle" data-id="${id}" aria-expanded="${open}" data-testid="nt-head-${id}"><i>${n}</i><span class="ntt">${title}</span><span class="ntline" data-testid="nt-line-${id}">${esc(line)}</span><span class="ntok" aria-hidden="true">${ok ? '✓' : ''}</span></button>
    ${open ? `<div class="ntbody">${body}</div>` : ''}</section>`;
}
function portRow() {
  const p = N.c.p, sp = shownPort(), ser = p.transport === 'serial';
  const main = x => x.link || (sp && sp.id === x.id) || /usb/i.test(x.id + x.label);      // ports with a link, USB adapters and the chosen one; the rest behind "more ports"
  const few = N.allPorts ? N.ports : N.ports.filter(main), hidden = N.ports.length - few.length;
  const autoChip = chip('data-nt="port" data-id="" data-testid="nt-port-auto"', !sp && !ser, ser ? 'Network (automatic)' : 'Automatic', ser ? 'Switch to UDP / TCP over the port that reaches the far Pi' : 'The port that reaches the far Pi');
  const chips = autoChip + few.map(x => chip(`data-nt="port" data-id="${esc(x.id)}" data-testid="nt-port-${esc(x.name)}"`, sp && sp.id === x.id, `${esc(x.label)} <span class="mono">${esc(x.name)}</span>`, x.state + (x.you_are_here ? ' · you are connected here' : ''))).join('');
  const bauds = [9600, 19200, 38400], other = N.otherBaud || !bauds.includes(p.serial.baud);
  const speed = ser ? `<div class="ntsub"><span class="ntlab">Speed ${tip('baud')}</span><div class="ntchips">${bauds.map(b => chip(`data-nt="baud" data-id="${b}" data-testid="nt-baud-${b}"`, !other && p.serial.baud === b, String(b), '')).join('')}${chip('data-nt="baud" data-id="other" data-testid="nt-baud-other"', other, 'Other…', '')}</div>
      ${other ? `<label class="f ntother"><span>Speed (baud)</span><input data-k="serial.baud" data-t="num" type="number" inputmode="numeric" value="${Number.isFinite(p.serial.baud) ? p.serial.baud : ''}">${errEl('serial.baud')}</label>` : ''}</div>` : '';
  const none = !N.ports.length ? `<p class="muted">No test port found on this Pi. Plug in a cable or the USB-serial adapter.</p>` : '';
  const line = sp ? `${sp.label} ${sp.name}${ser ? ` · ${p.serial.baud} baud` : sp.ip ? ' · ' + sp.ip : ''}${N.portId ? '' : ' · automatic'}` : (ser ? 'First serial adapter (automatic)' : 'Automatic · the port that reaches the far Pi');
  return rowHtml('port', 1, 'Port', line, (!!sp && !sp.missing) || (!sp && !ser), `<div class="ntchips" role="group" aria-label="Port on ${esc(meName())}">${chips}</div>${hidden ? `<button type="button" class="btn small ghost" data-nt="allports" data-testid="nt-all-ports" style="margin-top:8px">＋ ${hidden} more port${hidden > 1 ? 's' : ''} (no link)</button>` : ''}${none}
    ${sp && sp.missing ? `<p class="why">${esc(sp.name)} is not on this Pi now. Pick one of the ports above.</p>` : ''}${speed}
    <div class="row"><button type="button" class="btn small" data-nt="rescan" data-testid="nt-rescan">↻ Look again</button><span class="muted">Ports of <b>${esc(meName())}</b>, the Pi you are on.</span></div>`);
}
function protoRow() {
  const p = N.c.p, cur = p.mode === 'mirrored' ? 'mirrored' : 'echo';
  const chips = protocols(portKind(), N.goose).map(([t, mode, name, about, off]) => chip(`data-nt="proto" data-id="${t}:${mode}" data-testid="nt-proto-${t}${mode === 'mirrored' ? '-mb' : ''}"`, p.transport === t && cur === mode, esc(name), about, off)).join('');
  return rowHtml('proto', 2, 'Protocol', protoName(p), true, `<div class="ntchips" role="group" aria-label="Protocol">${chips}</div>`);
}
function piState(pe) {
  if (!pe.reachable) return ['bad', 'not answering'];
  const bits = ['answers'];
  if (pe.clock) bits.push(pe.clock.synced ? 'clock synced' : 'clock not synced');
  if (pe.auth === 'needs_password') bits.push('password needed');
  if (pe.busy) bits.push('busy with a test');
  return [pe.auth === 'needs_password' || pe.busy ? 'warn' : 'ok', bits.join(' · ') + (pe.host ? ' · ' + pe.host : '')];
}
function endsRow() {
  const p = N.c.p, m = model(p), mb = p.mode === 'mirrored', sp = shownPort();
  if (m.flow === 'other') return rowHtml('ends', 3, 'From → To', endsLine(p, m), true, `<p>This test runs between other Pis: <b>${esc(endsLine(p, m))}</b>.</p>
    <div class="row"><button type="button" class="btn small" data-nt="mine" data-testid="nt-from-here">Send from ${esc(meName())} instead</button></div>`);
  const sel = new Set(m.far.map(farKey)), one = ONE_FAR(p);
  const here = `<div class="nthere" data-testid="nt-here"><b>${esc(meName())}</b><span class="muted"> · ${sp ? esc(sp.name + (sp.ip ? ' · ' + sp.ip : '')) : 'port chosen by the Pi'}</span></div>`;
  const rows = EP.peers.map(pe => { const [s, t] = piState(pe), on = sel.has(pe.id); return `<label class="ntpi ${s}${on ? ' on' : ''}"><input type="${one ? 'radio' : 'checkbox'}" name="nt-far" data-nt-far="${esc(pe.id)}" data-testid="nt-far-${esc(pe.name)}" ${on ? 'checked' : ''}><i class="ntbox${one ? ' r' : ''}" aria-hidden="true"></i><span><b>${esc(pe.name)}</b><small>${s === 'bad' ? '✗' : '✓'} ${esc(t)}</small></span></label>`; }).join('');
  const gone = m.far.filter(e => e.pi && !peerOf(e.pi)).map(e => `<label class="ntpi bad on"><input type="${one ? 'radio' : 'checkbox'}" name="nt-far" data-nt-far="${esc(e.pi)}" checked><i class="ntbox${one ? ' r' : ''}" aria-hidden="true"></i><span><b>${esc(e.name || e.pi)}</b><small>✗ not in your list of Pis now</small></span></label>`).join('');
  const typedEp = m.far.find(e => e.host && !e.pi), typedOn = IPT(p.transport) && (N.typed || !!typedEp);
  const typed = typedOn ? `<label class="f nttyped"><span>IP address of the far end</span><input data-nt-in="host" data-k="nt.host" data-testid="nt-far-host" inputmode="decimal" autocomplete="off" placeholder="e.g. 192.168.1.50" value="${esc(typedEp ? typedEp.host : '')}">${errEl('net.peer_host')}</label>` : '';
  const farList = `<div class="ntpis" role="group" aria-label="Far end" data-testid="nt-far-list">${rows}${gone}${!EP.peers.length && !gone ? `<p class="muted" data-testid="nt-no-pis">${EP.loaded ? 'No other Pi found on this network yet.' : 'Looking for other Pis…'}${EP.err ? ' ' + esc(EP.err) : ''}</p>` : ''}</div>${typed}
    <div class="row ntadd">${IPT(p.transport) && !typedOn ? '<button type="button" class="btn small" data-nt="typed" data-testid="nt-type-ip">＋ Type an IP address</button>' : ''}<button type="button" class="btn small" data-nt="addpi" data-testid="nt-add-pi">＋ Add a Pi by IP</button><button type="button" class="btn small" data-nt="lookpis" data-testid="nt-refresh-pis">↻ Look again</button></div>`;
  const seg = mb ? `<div class="ntlock" data-testid="nt-dir-locked">⇄ <b>Round trip, automatic</b>: both Pis send nonstop; a trip bit is timed there and back.</div>`
    : `<div class="ntdir"><div class="seg" role="radiogroup" aria-label="Direction" data-testid="nt-direction">${[['one_way', '→ One way'], ['round_trip', '⇄ Round trip'], ['both', '⇆ Both ways']].map(([d, t]) => { const why = dirReason(p, m, d); return `<button type="button" role="radio" aria-checked="${m.dir === d}" class="${m.dir === d ? 'on' : ''}${why ? ' off' : ''}" data-nt="dir" data-id="${d}" data-testid="nt-dir-${d}" ${why ? `aria-disabled="true" title="${esc(why)}"` : ''}>${t}</button>`; }).join('')}</div>
      <button type="button" class="btn small" data-nt="swap" data-testid="nt-swap" title="The other way round: the far end sends, this Pi receives">⇅ Swap</button></div>
      <p class="muted ntdirnote" data-testid="nt-dir-note">${esc({ one_way: 'One way: the delay in this direction only. Both Pis must be on the same clock (Setup → Time). Loss and jitter never need it.', round_trip: 'Round trip: the far end sends every message straight back, and the sender times the trip.', both: 'Both ways: each link runs in each direction at the same time.' }[m.dir])}</p>`;
  const why = one && !mb ? `<p class="muted">${p.transport === 'serial' ? 'A serial port reaches one far end.' : 'A GOOSE test runs between two Pis.'} For a second link, start another test.</p>` : mb ? '<p class="muted">Mirrored Bits is always one pair of Pis.</p>' : '';
  const fromBlock = `<div class="ntend"><span class="ntlab">From</span>${m.flow === 'in' ? farList : here}</div>`, toBlock = `<div class="ntend"><span class="ntlab">To</span>${m.flow === 'in' ? here : farList}</div>`;
  const farPort = one && m.far[0] && m.far[0].pi && !GOOSE(p.transport) && p.transport === 'serial' ? farPortHtml(m.far[0]) : '';
  const s = sentence(p, m);
  return rowHtml('ends', 3, 'From → To', endsLine(p, m), m.far.length > 0, `${fromBlock}${seg}${toBlock}${why}${farPort}${errEl('direction')}
    ${s ? `<p class="ntsent" data-testid="nt-sentence">${esc(s)}</p>` : ''}`);
}
function farPortHtml(ep) {
  const key = farKey(ep), st = N.far[key], cur = ep.serial_port || 'auto';
  if (!st) { loadFar(key); }
  const opts = (st ? st.list : []).filter(x => x.kind === 'serial').map(x => `<option value="${esc(x.id)}" ${cur === x.id ? 'selected' : ''}>${esc(x.label)} ${esc(x.name)}</option>`).join('');
  return `<details class="ntmore"><summary>Port on ${esc(farName(ep))}: ${cur === 'auto' ? 'found automatically' : esc(cur)}</summary><label class="f"><span>Serial port on ${esc(farName(ep))}</span><select data-nt-farport="1" data-testid="nt-far-port"><option value="auto">Automatic (its first serial adapter)</option>${opts}${cur !== 'auto' && !opts.includes(`"${cur}"`) ? `<option value="${esc(cur)}" selected>${esc(cur)}</option>` : ''}</select>${st && st.error ? `<small class="why">Could not list its ports: ${esc(st.error)}</small>` : ''}</label></details>`;
}
async function loadFar(key) {
  N.far[key] = { list: [], error: '', loading: true };
  try { const d = await api(`/peers/${encodeURIComponent(key)}/testports`); N.far[key] = { list: d.ports || [], error: '' }; } catch (e) { N.far[key] = { list: [], error: e.message }; }
  if (N.root && N.root.isConnected && N.open.ends) draw();
}
function trafficRow() {
  const c = N.c, p = c.p, mb = p.mode === 'mirrored', D = customData(), netem = D.netem || {}, t = p.transport;
  const lenUnits = mb ? ['seconds', 'minutes', 'hours'] : ['seconds', 'minutes', 'hours', 'messages'];
  const lc = (p.length_choices || []).slice(), curS = Number.isFinite(c.ui.len) ? c.ui.len * ({ seconds: 1, minutes: 60, hours: 3600 }[c.ui.lenUnit] || NaN) : NaN;
  const lenName = x => x % 3600 === 0 && x <= 48 * 3600 ? `${x / 3600} h` : x % 86400 === 0 ? `${x / 86400} days` : x % 60 === 0 ? `${x / 60} min` : `${x} s`;
  const chips = lc.length ? `<div class="seg ntlen" role="radiogroup" aria-label="Test length" data-testid="nt-len-choices">${lc.map(x => `<button type="button" role="radio" aria-checked="${Math.abs(curS - x) < 0.5}" class="${Math.abs(curS - x) < 0.5 ? 'on' : ''}" data-nt="len" data-v="${x}" data-testid="nt-len-${x}">${lenName(x)}</button>`).join('')}</div>` : '';
  const len = `<div class="ntlenwrap">${chips}<label class="f"><span>Test length</span><div class="fieldrow"><input data-k="ui.len" data-t="num" type="number" step="any" inputmode="decimal" value="${Number.isFinite(c.ui.len) ? c.ui.len : ''}"><select data-k="ui.lenUnit" data-t="str">${lenUnits.map(u => `<option value="${u}" ${c.ui.lenUnit === u ? 'selected' : ''}>${u}</option>`).join('')}</select></div>${errEl('ui.len')}</label></div>`;
  const rate = `<label class="f"><span>Send a message every ${tip('interval')}</span><div class="fieldrow"><input data-k="ui.rate" data-t="num" type="number" step="any" inputmode="decimal" value="${Number.isFinite(c.ui.rate) ? c.ui.rate : ''}"><select data-k="ui.rateUnit" data-t="str"><option value="ms" ${c.ui.rateUnit === 'ms' ? 'selected' : ''}>ms</option><option value="hz" ${c.ui.rateUnit === 'hz' ? 'selected' : ''}>messages per second</option></select></div>${errEl('ui.rate')}</label>`;
  const pass = `${field(c, 'threshold_ms', mb ? (p.mirrored.codec === 'sim' ? 'Pass: bit transfer within (ms)' : 'Pass: one way within (ms, = half the round trip)') : 'Pass: delay within (ms)', { t: 'threshold' })}
    ${select(c, 'rule.percentile', 'Judged on', [[50, 'Typical message (p50)'], [95, 'p95'], [99, 'p99 (recommended)'], [99.9, 'p99.9 (long runs)'], [100, 'Slowest message (max)']], { t: 'rule', type: 'num' })}
    ${field(c, 'rule.max_loss_pct', 'Loss allowed (%)', { t: 'loss' })}`;
  const imps = [['', 'None (use the real link)'], ...Object.keys(netem).map(k => [k, `${k}: ${netem[k]}`])];
  const nt = S.status.netem || {}, fixedFrame = p.one_way || GOOSE(t);
  const link = t === 'serial' ? `${select(c, 'serial.bytesize', 'Data bits', [5, 6, 7, 8].map(x => [x, x]), { type: 'num' })}${select(c, 'serial.parity', 'Parity', [['N', 'None'], ['E', 'Even'], ['O', 'Odd']])}${select(c, 'serial.stopbits', 'Stop bits', [[1, '1'], [2, '2']], { type: 'num' })}`
    : IPT(t) ? `${field(c, 'net.port', 'Test port', { hint: 'Every Pi answers on 20500. Another number is opened for the test, then closed.' })}${field(c, 'net.dscp', 'Priority (DSCP 0-63)', { t: 'dscp' })}${t === 'tcp' ? check(c, 'net.keepalive', 'Keep the session alive (TCP keepalive)') + field(c, 'net.keepalive_idle_s', 'Keepalive after quiet (s)') : ''}`
      : t === 'goose_l2' ? `${field(c, 'goose.dest_mac', 'Destination MAC (multicast)', { type: 'str' })}${field(c, 'goose.vlan_id', 'VLAN ID (0 = priority only)', { t: 'vlan' })}${field(c, 'goose.vlan_prio', 'Priority (0-7)', { t: 'vlan' })}${check(c, 'goose.vlan', 'VLAN tag (802.1Q)', 'vlan')}`
        : `${field(c, 'goose.group_a', 'Group, first direction', { type: 'str' })}${field(c, 'goose.group_b', 'Group, other direction', { type: 'str' })}${field(c, 'goose.udp_port', 'UDP port')}${field(c, 'goose.ttl', 'TTL')}${field(c, 'goose.dscp', 'Priority (DSCP 0-63)', { t: 'dscp' })}${field(c, 'goose.unicast_peer', 'Unicast fallback address (optional)', { type: 'str' })}`;
  const hard = `${select(c, 'impairment', 'Simulate a bad link', imps, { t: 'netem', type: 'nul', disabled: !nt.ok, note: nt.ok ? '' : 'Not available here: ' + (nt.reason || 'not allowed on this install.') })}
    ${mb ? '' : `<label class="f"><span>Background load (Mb/s) ${tip('load')}</span><input data-k="load_mbps" data-t="num" type="number" step="any" inputmode="decimal" value="${Number.isFinite(p.load_mbps) ? p.load_mbps : ''}" ${IPT(t) ? '' : 'disabled'}>${IPT(t) ? '' : '<small class="why">Needs a UDP or TCP test.</small>'}${errEl('load_mbps')}</label>`}`;
  const more = mb ? `${field(c, 'mirrored.trip_every_s', 'Flip the trip bit every (s)')}${t === 'serial' ? '' : field(c, 'mirrored.rate_baud', 'Message pace (same as baud)', { hint: '9600, 19200 or 38400, like a relay channel.' })}${field(c, 'mirrored.max_down_pct', 'ROK may be 0 (% of the time)')}${field(c, 'timeout_ms', 'Give up on a bit after (ms)', { t: 'timeout' })}${link}${hard}`
    : `${field(c, 'timeout_ms', 'Give up after (ms)', { t: 'timeout' })}${select(c, 'frame', 'Message size', [['compact', 'Compact, 7 bytes'], ['extended', 'Extended, 40 bytes (timestamps)']], { disabled: fixedFrame, note: fixedFrame ? 'Fixed: this test needs the timestamps.' : '' })}
      ${field(c, 'ui.gaps', 'Quiet gaps to test (s, comma-separated)', { type: 'str', t: 'idle', hint: 'Empty = none. Example: 5, 30, 120.' })}${field(c, 'idle_repeats', 'Repeats per gap')}${select(c, 'idle_mode', 'Quiet checks happen', [['after', 'After the main test'], ['interleaved', 'Mixed in (soak)']])}
      ${p.direction === 'round_trip' ? check(c, 'one_way', 'Also time each direction (clocks must agree)', 'oneway') : ''}${link}${hard}`;
  const ex = N.extras && N.extras.pattern ? N.extras.pattern(c) : '';
  const body = ex && ex.replaces ? ex.html : `${ex ? ex.html : ''}<div class="grid g3">${mb ? '' : rate}${len}${pass}</div>${mb ? codecChoice(c) : ''}
    <details class="ntmore" id="ntMore" data-testid="nt-more"><summary>More options</summary><div class="grid g3" style="margin-top:12px">${more}</div>${mb ? mbFieldsHtml(p, (path, type) => ` data-k="${path}" data-t="${type}"`, t === 'serial') : ''}</details>${radioFormHtml(c, field, select, errEl)}`;
  return rowHtml('traffic', 4, mb ? 'Pass rule' : 'Traffic', trafficLine(p), true, body);
}
const checkRow = c => `<div class="check ${c.s}" data-testid="setup-check-row" ${c.id ? `data-check="${esc(c.id)}"` : ''}><div class="ic">${{ ok: '✓', warn: '!', bad: '✕', wait: '…' }[c.s]}</div><div style="flex:1"><b>${esc(c.title)}</b><small class="muted">${esc(c.text || '')}</small>${c.link ? `<div style="margin-top:6px"><a href="${esc(c.link.href)}" data-testid="${esc(c.link.testid || 'setup-check-link')}">${esc(c.link.label)}</a></div>` : ''}${c.fix ? `<div style="margin-top:6px"><button type="button" class="btn small" data-nt="${esc(c.fixAct)}" ${c.iface ? `data-iface="${esc(c.iface)}"` : ''} ${c.port ? `data-port="${esc(c.port)}"` : ''}>${esc(c.fix)}</button></div>` : ''}</div></div>`;
function barHtml() {
  const cs = N.checks, bad = cs.filter(c => c.s === 'bad'), wait = cs.some(c => c.s === 'wait'), warn = cs.filter(c => c.s === 'warn'), ok = cs.filter(c => c.s === 'ok');
  const p = N.c.p, m = model(p);
  const hint = bad.length ? bad[0].title : wait ? 'Checking…' : (sentence(p, m) || 'Choose where to send');
  const chips = wait ? '' : ok.map(c => `<span class="ntck ok">✓ ${esc(c.chip || c.title)}</span>`).join('') + warn.map(c => `<span class="ntck warn">! ${esc(c.chip || c.title)}</span>`).join('');
  return `${bad.length || warn.length ? `<div id="checks" data-testid="setup-checks">${[...bad, ...warn].map(checkRow).join('')}</div>` : `<div id="checks" data-testid="setup-checks" hidden>${cs.map(checkRow).join('')}</div>`}
    <div class="actionbar ntbar" data-testid="nt-bar"><div class="nthint"><span id="startHint" data-testid="nt-hint" class="${bad.length ? 'bad' : ''}">${esc(hint)}</span><div class="ntcks" data-testid="nt-checks">${chips}</div><div id="ntJob" data-testid="nt-job"></div></div>
      <div class="row ntbtns"><button type="button" class="btn" data-nt="cancel" data-testid="nt-cancel">← Tests</button><button type="button" class="btn" data-nt="save" data-testid="nt-save" title="Keep this test in your list">💾 Save</button><button type="button" class="btn" data-nt="repeat" data-testid="nt-repeat" title="Run this test again and again on a schedule (busy hours, weekends)" ${bad.length || wait ? 'disabled' : ''}>🔁 Repeat…</button>
      <button type="button" class="btn primary big tb-src" data-nt="start" id="quickStart" data-testid="nt-start" title="Run this test now" ${bad.length || wait ? 'disabled' : ''}>▶ Run test</button></div></div>`;
}
function pageHtml() {
  const c = N.c, editing = c.id != null;
  return `<div id="ntPage" data-testid="nt-page"><div class="row sp nttop"><h1 style="margin:0">${editing ? esc(c.p.name) : 'New test'}</h1><span class="chip" title="The Pi you are on: its ports are in row 1">${esc(meName())}</span></div>
    ${editing ? `<p class="sub muted">${c.builtin ? 'Built-in test. ' : ''}Tap a row to change it. Run test runs it with what you see; Save keeps the changes.</p>` : '<p class="sub muted">Answer top to bottom. Every row has a default, so the shortest way is: port, protocol, tick the far Pi, Run test.</p>'}
    <div class="ntrows">${portRow()}${protoRow()}${endsRow()}${trafficRow()}</div><div id="ntBar">${barHtml()}</div></div>`;
}

// ---------------------------------------------------------------- drawing
function paintErrs() {
  const errs = validate(N.c, []); delete errs.test_name;
  const host = N.root;
  $$('small.err[data-e]', host).forEach(s => { const m = errs[s.dataset.e] || ''; s.textContent = m; s.hidden = !m; const inp = $(`[data-k="${s.dataset.e}"]`, host); if (inp) inp.setAttribute('aria-invalid', m ? 'true' : 'false'); });
  return errs;
}
function drawBar() { const b = $('#ntBar', N.root); if (b) { b.innerHTML = barHtml(); mountJob(); } }
function mountJob() { const j = $('#ntJob', N.root); if (j && N.extras && N.extras.job) N.extras.job(j); }
// the data-use table of a radio test comes from the Pi (one number source for the page, the results and the AI guide)
function fillUsage(host) {
  const p = N.c && N.c.p; if (!p || p.transport !== 'serial' || !$('#ntUsage', host)) return;
  const key = usageKey(p);
  loadUsage(p).then(d => { const el = $('#ntUsage', N.root); if (el && N.c && usageKey(N.c.p) === key) el.innerHTML = usageBlock(d, N.c.p); }).catch(() => {});
}
function draw() {
  const host = N.root; if (!host || !N.c) return;
  const a = document.activeElement, key = a && host.contains(a) && a.dataset ? (a.dataset.k || a.dataset.ntFar && 'far:' + a.dataset.ntFar) : null, pos = a && a.selectionStart != null && a.type !== 'number' ? a.selectionStart : null;
  const more = $('#ntMore', host)?.open, radioOpen = $('#ntRadio', host)?.open, y = window.scrollY;
  derive(N.c);
  host.innerHTML = pageHtml();
  if (more && $('#ntMore', host)) $('#ntMore', host).open = true;
  if (radioOpen && $('#ntRadio', host)) $('#ntRadio', host).open = true;
  fillUsage(host);
  if (N.extras && N.extras.mount) N.extras.mount(host, N.c, changed);
  paintErrs(); mountJob();
  if (key) { const el = key.startsWith('far:') ? $(`[data-nt-far="${key.slice(4)}"]`, host) : $(`[data-k="${key}"]`, host); if (el) { el.focus({ preventScroll: true }); if (pos != null && el.setSelectionRange) try { el.setSelectionRange(pos, pos); } catch { } } }
  window.scrollTo({ top: y });
}
function changed(delay = 350, redraw = true) {
  if (redraw) draw(); else { paintErrs(); const m = model(N.c.p); const l = $('[data-testid="nt-line-traffic"]', N.root); if (l) l.textContent = trafficLine(N.c.p); const e = $('[data-testid="nt-line-ends"]', N.root); if (e) e.textContent = endsLine(N.c.p, m); }
  clearTimeout(N.timer); N.timer = setTimeout(runChecks, delay);
}

// ---------------------------------------------------------------- the automatic check
const PIS_LINK = { href: '#config/pis', label: 'Setup → Pis on this network', testid: 'setup-pis-link' };
const cap = t => t ? t.charAt(0).toUpperCase() + t.slice(1) : t;
export function profileNow() { derive(N.c); return toProfile(N.c); }
async function runChecks() {
  if (!N.c || !N.root || !N.root.isConnected) return;
  if (runChecks.busy) { runChecks.again = true; return; }
  runChecks.busy = true;
  try { await doChecks(); } finally { runChecks.busy = false; if (runChecks.again) { runChecks.again = false; runChecks(); } }
}
async function doChecks() {
  const c = N.c, out = [], my = ++N.seq;
  N.checks = [{ s: 'wait', title: 'Checking…', text: '' }]; drawBar();
  const errs = validate(c, []); delete errs.test_name;
  const p = toProfile(c), m = model(p), sp = shownPort(), ip = IPT(p.transport);
  const st = await api('/status').catch(() => S.status); if (st && !st.login_required) S.status = st;
  if (!N.c || my !== N.seq) return;
  const keys = Object.keys(errs);
  if (keys.length) out.push({ s: 'bad', id: 'fields', title: keys.length > 1 ? `${keys.length} fields to fix` : 'One field to fix', text: errs[keys[0]] });
  if (S.status.run.running) out.push({ s: 'bad', id: 'running', title: `A test is already running on ${meName()}`, text: 'Wait for it, or stop it with STOP in the bar.', link: { href: '#tests', label: 'Open Run', testid: 'nt-open-run' } });
  if (sp && sp.missing) out.push({ s: 'bad', id: 'port', title: `${sp.name} is not on this Pi`, text: 'Pick one of the ports in row 1.' });
  else if (sp && N.portId && sp.kind !== 'serial') {
    if (!sp.link) out.push({ s: 'bad', id: 'port', title: `${sp.name} has no link`, text: sp.kind === 'wifi' ? 'This Wi-Fi port is not connected. Pick another port.' : 'Plug in the cable, or pick another port.' });
    else if (ip && !sp.ip) out.push({ s: 'bad', id: 'port', title: `${sp.name} has no IP address`, text: 'Give it one, or pick another port.', link: { href: '#config/network', label: 'Setup → Network', testid: 'nt-net-link' } });
    else if (sp.you_are_here && sp.kind === 'wifi') out.push({ s: 'warn', id: 'port-wifi', chip: `test shares ${sp.name} with you`, title: `You are connected to ${meName()} through ${sp.name}`, text: 'A test on it shares the air with your phone or laptop and may read worse. Use Ethernet for the test if you can.' });
  }
  if (!N.ports.length) out.push({ s: 'bad', id: 'port', title: 'No test port found', text: 'Plug in a cable or the USB-serial adapter, then “Look again” in row 1.' });
  if (m.flow !== 'other' && !m.far.length) out.push({ s: 'bad', id: 'address', title: 'Choose where to send', text: `Tick a Pi under ${m.flow === 'in' ? 'From' : 'To'} in row 3${ip ? ', or type an IP address' : ''}.` });
  if (p.transport === 'serial' && m.flow !== 'other') {
    try {
      const r = await api('/selfcheck', { method: 'POST', body: { port: N.portId || 'auto' } });
      out.push(r.ftdi ? (r.ok ? { s: 'ok', id: 'serial', chip: 'USB adapter fast', title: 'USB adapter is fast', text: r.message } : { s: 'warn', id: 'serial', chip: 'USB adapter adds delay', title: 'This USB adapter adds up to 16 ms of delay', text: r.message, fix: 'Fix now (set to 1 ms)', fixAct: 'fixftdi', port: N.portId || 'auto' })
        : { s: 'ok', id: 'serial', chip: 'serial port found', title: 'Serial port found', text: r.port + ': ' + r.message });
    } catch (er) { out.push({ s: 'bad', id: 'serial', title: 'No serial adapter found', text: er.message + ' Plug in the USB-serial cable, then “Look again” in row 1.' }); }
  }
  if (GOOSE(p.transport)) { const g = S.status.goose_raw || {}; if (!g.ok) out.push({ s: 'bad', id: 'goose', title: 'GOOSE is not possible on this install', text: g.reason || 'It needs the installed service (raw network access).' }); }
  if (!out.some(x => x.s === 'bad')) {
    const fars = m.flow === 'other' ? [null] : m.far;
    for (const ep of fars) {
      const one = JSON.parse(JSON.stringify(p));
      if (ep) setModel(one, { ...m, far: [ep] });
      const name = ep ? farName(ep) : 'The far Pi';
      try {
        const r = await api('/probe', { method: 'POST', body: one });
        if (!N.c || my !== N.seq) return;
        if (r.ok) {
          out.push({ s: 'ok', id: 'reachable', chip: `${name} answered`, title: `${cap(name)} answered`, text: '' });
          if (m.dir === 'one_way' || p.one_way) out.push(r.one_way_ok ? { s: 'ok', id: 'clocks', chip: 'clocks synced', title: 'Clocks synced for one-way timing', text: '' }
            : { s: m.dir === 'one_way' ? 'bad' : 'warn', id: 'clocks', chip: 'one-way timing unavailable', title: `One way needs ${meName()} and ${name} on the same clock`, text: ((r.peer && r.peer.one_way_unavailable) || 'The clocks are not synced.') + (m.dir === 'one_way' ? ' Use Round trip, or sync the clocks first.' : ' The test will time the round trip only.'), link: { href: '#settings', label: 'Setup → Time', testid: 'setup-time-source-link' } });
          for (const w of r.warnings || []) out.push({ s: 'warn', id: 'path', chip: 'check the path', title: 'Check the path', text: w });
        } else out.push({ s: 'bad', id: 'reachable', title: `${cap(name)} did not answer${sp && N.portId && ip ? ' from ' + sp.name : ''}`, text: r.error, link: /password/i.test(r.error || '') ? PIS_LINK : null });
      } catch (er) {
        if (!N.c || my !== N.seq) return;
        const msg = er.message || 'The check failed.', pw = /password/i.test(msg), busy = /\bbusy\b|in use/i.test(msg);
        out.push({ s: 'bad', id: pw ? 'password' : busy ? 'port-busy' : 'reachable', title: pw ? `${cap(name)} needs its password` : busy ? 'That port is busy' : `Cannot reach ${name}`, text: msg, link: pw ? PIS_LINK : null });
      }
    }
    const first = m.flow === 'out' && m.far[0] ? (m.far[0].host || (peerOf(m.far[0].pi) || {}).host) : '';
    if (ip && first) { const w = await wifiCheck(first); if (w && w.s !== 'ok') out.push({ ...w, id: 'wifi', chip: 'Wi-Fi power saving on' }); }
  }
  if (p.impairment) { const n = S.status.netem || {}; out.push(n.ok ? { s: 'ok', id: 'netem', chip: 'bad-link simulation on', title: 'Bad-link simulation allowed', text: '' } : { s: 'bad', id: 'netem', title: 'Bad-link simulation is not allowed here', text: n.reason }); }
  if (!N.c || my !== N.seq) return;
  const seen = new Set(); N.checks = out.filter(x => { const k = x.id + x.title; if (seen.has(k)) return false; seen.add(k); return true; });
  drawBar();
}

// ---------------------------------------------------------------- actions
async function loadPorts() {
  try { const d = await api('/testports'); N.ports = d.ports || []; N.goose = d.goose_raw || { ok: true }; } catch (e) { N.ports = []; toast('Could not list the ports: ' + e.message, true); }
}
function toggleFar(id, on) {
  const p = N.c.p, m = model(p), pe = peerOf(id), ep = { ...blankEp(), pi: id, name: pe ? pe.name : '' };
  const prev = m.far.find(e => farKey(e) === (pe ? pe.id : id));
  if (ONE_FAR(p)) m.far = on ? [prev || ep] : [];
  else if (on && !prev) m.far.push(ep);
  else if (!on) m.far = m.far.filter(e => farKey(e) !== (pe ? pe.id : id));
  if (m.dir !== 'round_trip' && dirReason(p, m, m.dir)) m.dir = 'round_trip';
  setModel(p, m); autoPort();
}
async function save() {
  const c = N.c, errs = paintErrs(), keys = Object.keys(errs);
  if (keys.length) { N.open.traffic = true; draw(); return toast(`Fix the ${keys.length} marked field${keys.length > 1 ? 's' : ''} first: ${errs[keys[0]]}`, true, 7000); }
  const editing = c.id != null, taken = n => S.profiles.some(x => x.id !== c.id && x.name === n);
  let name = editing ? c.p.name : autoName(c.p); if (!editing) { const base = name; let i = 1; while (taken(name)) name = `${base} ${++i}`.slice(0, 80); }
  let desc = c.p.description || '';
  const pr = sheetBox(editing ? 'Save changes' : 'Save this test', `<label class="f"><span>Name</span><input id="ntName" data-testid="nt-save-name" maxlength="80" autocomplete="off" value="${esc(name)}"><small class="err" id="ntNameErr" hidden></small></label>
    <label class="f" style="margin-top:10px"><span>What is this test for? (optional)</span><textarea id="ntDesc" data-testid="nt-save-desc" rows="2" placeholder="e.g. Site 4 trip signal over the radio, 60 ms limit">${esc(desc)}</textarea></label>
    <p class="muted" style="margin-top:10px">${esc(sentence(c.p, model(c.p)) || '')}</p>`, [{ id: 'no', label: 'Cancel' }, { id: 'go', label: editing ? 'Save changes' : 'Save', cls: 'primary' }], 'nt-save-sheet');
  const ni = $('#ntName'), di = $('#ntDesc'); ni.oninput = () => { name = ni.value.trim(); }; di.oninput = () => { desc = di.value; }; ni.focus(); ni.select();
  if (await pr !== 'go') return;
  if (!name) return toast('Give the test a name.', true);
  if (taken(name)) return toast('You already have a test with this name.', true, 6000);
  const body = toProfile(c); body.name = name; body.description = desc;
  try {
    const r = editing ? await api(`/profiles/${c.id}`, { method: 'PUT', body }) : await api('/profiles', { method: 'POST', body });
    toast(`${editing ? 'Saved changes to' : 'Saved'} “${r.name}”.`);
    N.c = null; N.cb.saved && N.cb.saved(r);
  } catch (e) { toast(e.message, true, 8000); }
}
async function start(btn) {
  const errs = paintErrs(), keys = Object.keys(errs);
  if (keys.length) { N.open.traffic = true; draw(); return toast(`Fix the ${keys.length} marked field${keys.length > 1 ? 's' : ''} first.`, true, 6000); }
  const p = toProfile(N.c); btn.disabled = true;
  try {
    const body = { profile: p }; if (N.extras && N.extras.jobValue) body.job = N.extras.jobValue();
    const r = await api('/runs', { method: 'POST', body });
    rememberEndpoints(p); resetLive(p, r.run_id, r.run_ids);
    N.c = null; N.cb.started && N.cb.started(r);
  } catch (e) { toast(e.message, true, 8000); btn.disabled = false; clearTimeout(N.timer); runChecks(); }
}
async function repeatTest() {
  const errs = paintErrs(), keys = Object.keys(errs);
  if (keys.length) { N.open.traffic = true; draw(); return toast(`Fix the ${keys.length} marked field${keys.length > 1 ? 's' : ''} first.`, true, 6000); }
  const p = toProfile(N.c), r = await repeatSheet(p);
  if (r) { N.c = null; N.cb.closed && N.cb.closed(); }
}
async function cancel() {
  if (N.c && isDirty(N.c) && N.mode !== 'run' && !await confirmBox('Leave this test?', 'What you changed has not been saved.', 'Leave', true)) return;
  N.c = null; N.cb.closed && N.cb.closed();
}
async function onClick(ev) {
  const a = ev.target.closest('[data-nt]'); if (!a || !N.c) return;
  const act = a.dataset.nt, id = a.dataset.id, p = N.c.p;
  if (a.getAttribute('aria-disabled') === 'true') { const why = a.title || $('small', a)?.textContent; if (why) toast(why, false, 6000); return; }
  if (act === 'len') { const x = +a.dataset.v, u = x % 3600 === 0 ? 'hours' : x % 60 === 0 ? 'minutes' : 'seconds'; N.c.ui.lenUnit = u; N.c.ui.len = x / { seconds: 1, minutes: 60, hours: 3600 }[u]; changed(0, true); }
  else if (act === 'toggle') { N.open[id] = !N.open[id]; draw(); if (N.open[id]) $(`[data-row="${id}"]`, N.root)?.scrollIntoView({ block: 'nearest' }); }
  else if (act === 'port') { choosePort(id); changed(); }
  else if (act === 'baud') { if (id === 'other') N.otherBaud = true; else { N.otherBaud = false; p.serial.baud = +id; } N.c.baudSet = true; changed(); }
  else if (act === 'allports') { N.allPorts = true; draw(); }
  else if (act === 'rescan') { a.disabled = true; await loadPorts(); changed(); }
  else if (act === 'proto') { const [t, mode] = id.split(':'); chooseProto(t, mode); changed(); }
  else if (act === 'dir') { const m = model(p); m.dir = id; setModel(p, m); changed(); }
  else if (act === 'swap') { const m = model(p), why = dirReason(p, m, 'swap'); if (why) return toast(why, false, 6000); if (m.flow === 'other') return; m.flow = m.flow === 'in' ? 'out' : 'in'; setModel(p, m); changed(); }
  else if (act === 'mine') { N.portId = N.portId || null; setModel(p, { flow: 'out', far: [], dir: 'round_trip' }); changed(); }
  else if (act === 'typed') { N.typed = true; draw(); $('[data-nt-in="host"]', N.root)?.focus(); }
  else if (act === 'addpi') { const pe = await addPiSheet(); if (pe) { await loadPeers(); toggleFar(pe.id, true); toast(`Added ${pe.name}.`); changed(); } }
  else if (act === 'lookpis') { a.disabled = true; a.textContent = 'Looking…'; try { await loadPeers(true); } finally { changed(); } }
  else if (act === 'fixftdi') { try { const r = await api('/selfcheck', { method: 'POST', body: { port: a.dataset.port || 'auto', fix: true } }); toast(r.message, !r.ok, 7000); } catch (e) { toast(e.message, true); } await loadPorts(); changed(); }
  else if (act === 'wifioff') { a.disabled = true; await turnOff(a.dataset.iface); changed(0, false); }
  else if (act === 'save') save();
  else if (act === 'repeat') repeatTest();
  else if (act === 'start') start(a);
  else if (act === 'cancel') cancel();
}
function onChange(ev) {
  const el = ev.target; if (!N.c) return;
  if (el.dataset.ntFar != null) { toggleFar(el.dataset.ntFar, el.checked); return changed(); }
  if (el.dataset.ntFarport) { const m = model(N.c.p); if (m.far[0]) { m.far[0].serial_port = el.value === 'auto' ? '' : el.value; setModel(N.c.p, m); } return changed(); }
  if (el.dataset.ntIn === 'host') { onHost(el, 350, false); return setTimeout(() => { if (N.c) draw(); }, 250); }      // redraw after the tap that took the focus away has landed
  if (!el.dataset.k) return;
  applyField(N.c, el);
  changed(350, ['ui.rateUnit', 'ui.lenUnit', 'one_way', 'impairment', 'frame', 'mirrored.codec'].includes(el.dataset.k) || el.tagName === 'SELECT' || el.type === 'checkbox');
}
function onHost(el, delay, redraw) {
  const p = N.c.p, m = model(p), v = el.value.trim();
  m.far = m.far.filter(e => !(e.host && !e.pi));
  if (v) { if (ONE_FAR(p)) m.far = []; m.far.push({ ...blankEp(), host: v }); if (m.flow === 'in') m.flow = 'out'; if (m.dir === 'both') m.dir = 'round_trip'; }
  N.typed = true; setModel(p, m); changed(delay, redraw);
}
function onInput(ev) {
  const el = ev.target; if (!N.c) return;
  if (el.dataset.ntIn === 'host') return onHost(el, 800, false);
  if (!el.dataset.k || el.type === 'radio' || el.type === 'checkbox' || el.tagName === 'SELECT') return;
  applyField(N.c, el); changed(800, false);
}

// ---------------------------------------------------------------- opening the page
// mode: 'new' (rows 1-3 open) | 'edit' / 'run' (a saved test: every row closed to its one line, the check already running)
// cb: { started(run), saved(profile), closed() }.  extras: hooks from other modules (job line, traffic patterns), all optional.
export async function openNewTest(root, { profile = null, mode = 'new', cb = {}, extras = null } = {}) {
  await Promise.all([loadCustomLimits(), loadPeers(), loadPorts()]);
  N.root = root; N.cb = cb; N.mode = mode; N.portPicked = !!profile; N.allPorts = false; N.checks = [{ s: 'wait', title: 'Checking…', text: '' }];      /* Start stays off until the first check is back */ N.typed = false; N.far = {}; N.otherBaud = false; N.extras = extras;
  N.c = profile ? editDraft(profile) : newDraft(S.profiles);
  const p = N.c.p; p.dests = p.dests || []; p.sources = p.sources || [];
  const m = model(p), here = m.here || {};
  if (profile) {
    N.portId = p.transport === 'serial' ? ((here.serial_port && here.serial_port !== 'auto' ? here.serial_port : '') || (p.serial.port !== 'auto' ? p.serial.port : '') || null)
      : ((here.iface && here.iface !== 'auto' ? here.iface : '') || (GOOSE(p.transport) && p.goose.interface !== 'auto' ? p.goose.interface : '') || null);
    if (!N.portId && p.transport === 'serial') { const d = defaultPort('serial'); N.autoShown = d ? d.id : null; }
  } else {
    const d = defaultPort('eth') || defaultPort('serial');
    N.portId = null; p.transport = d && d.kind === 'serial' ? 'serial' : 'udp';
    if (d && d.kind === 'serial') N.portId = d.id;
    m.flow = 'out';
  }
  if (m.flow === 'out' && !m.far.length) {        // nothing chosen yet (a new test, or a built-in test with no far end): tick the only other Pi, or the one used last
    if (EP.peers.length === 1 && EP.peers[0].reachable) m.far = [{ ...blankEp(), pi: EP.peers[0].id, name: EP.peers[0].name }];
    else { let last = null; try { last = JSON.parse(localStorage.getItem('tt-conn-ep') || 'null'); } catch { } const lp = last && last.dest && last.dest.pi && peerOf(last.dest.pi); if (lp) m.far = [{ ...blankEp(), pi: lp.id, name: lp.name }];
      else if (last && last.dest && last.dest.host && !last.dest.pi && IPT(p.transport)) { m.far = [{ ...blankEp(), host: last.dest.host }]; N.typed = true; } }      // an address typed once is offered again
  }
  setModel(p, m); if (!profile) autoPort(); derive(N.c);
  N.c.base = JSON.stringify([N.c.p, N.c.ui]);
  N.open = mode === 'new' ? { port: true, proto: true, ends: true, traffic: false } : { port: false, proto: false, ends: !model(p).far.length && model(p).flow !== 'other', traffic: false };
  bindOnce(root, 'click', onClick); bindOnce(root, 'change', onChange); bindOnce(root, 'input', onInput);
  draw(); window.scrollTo({ top: 0 });
  runChecks();
}
// "Open" = a draft exists AND its page is still on screen. Leaving the page without Cancel (Home / another tab, browser Back, a finished run) used to leave the draft behind,
// and the Tests list then ignored every tap until a reload (setup.js listClick stands down while the form is open).
export const newTestOpen = () => !!(N.c && N.root && N.root.isConnected);
export function closeNewTest() { clearTimeout(N.timer); N.seq++; N.c = null; N.root = null; N.checks = []; }      // the list is drawn again: no draft, no pending check
// The list of Pis changed (a Pi came back, a clock got synced): refresh row 3 unless the user is typing.
setInterval(async () => {
  if (!N.c || !N.root || !N.root.isConnected || document.hidden) return;
  const a = document.activeElement; if (a && N.root.contains(a) && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.tagName === 'SELECT') && a.type !== 'checkbox' && a.type !== 'radio') return;
  if ($('#modal') && !$('#modal').hidden) return;
  if (await loadPeers()) changed(0);
}, 10000);
