// Traffic generator tab: the stream list is the screen (on/off, live counters per stream), the editor sits beside it, run settings below.
import { $, $$, esc, api, toast, confirmBox, promptBox, sheetBox, tip, ms, num, pct, dur, stateBar, setStateBar, download, bindOnce, myName } from './ui.js';
import { S, onLive, resetLive, isTestset } from './state.js';
import { EP, loadPeers, peerOf, optionsFor, valueOfEp, epFromValue, TYPED, addPiSheet, watchPeers } from './endpoints.js';
import { mountWifi } from './wifi.js';
import { Steps } from './steps.js';
import { markPlugin } from './results.js';

const TYPES = { udp: 'UDP', tcp: 'TCP', ip: 'Raw IP', l2: 'Raw Ethernet (Layer 2)' };
const TYPE_TIP = 'UDP / TCP are normal traffic. Raw IP sends your own IP protocol number. Raw Ethernet sends bare frames (needs both Pis on the same Layer-2 network).';
let G = { profiles: [], selId: null, p: null, conn: loadConn(), off: null, plots: [], sel: 0, edOpen: false, ran: null, ov: {} };
let navigate = () => { };

function loadConn() { try { return { peer_api: '', peer_host: '', interface: 'auto', peer_interface: 'auto', local_host: '', peer_password: '', ...JSON.parse(localStorage.getItem('tt-gen-conn') || '{}') }; } catch { return { peer_api: '', peer_host: '', interface: 'auto', peer_interface: 'auto', local_host: '', peer_password: '' }; } }
const saveConn = () => localStorage.setItem('tt-gen-conn', JSON.stringify(G.conn));
const clone = o => JSON.parse(JSON.stringify(o));
const isRunning = () => S.live.profile ? (S.live.running && S.live.profile.kind === 'generator' && !isTestset(S.live.profile)) : (S.status.run.running && S.status.run.kind === 'generator' && !S.status.run.testset);
const hasResult = () => S.live.done && S.live.profile && S.live.profile.kind === 'generator' && !isTestset(S.live.profile);
const getp = (o, path) => path.split('.').reduce((a, k) => a == null ? a : a[k], o);
const setp = (o, path, v) => { const k = path.split('.'); let t = o; k.slice(0, -1).forEach(x => t = (t[x] ??= {})); t[k.at(-1)] = v; };
const blank = () => ({ pi: '', host: '', name: '', port: null, serial_port: '', iface: 'auto' });
const SELF = () => ({ ...blank(), pi: 'self', name: EP.me ? EP.me.name : myName() });
const webHost = a => { const t = String(a || '').trim(); if (!t) return ''; try { return new URL(t.includes('//') ? t : '//' + t).hostname; } catch { return ''; } };
// The Pi a stream goes to by default: the last one picked here, else the first Pi found, else a typed address (the old "other Pi's web address").
function otherPi() {
  let last = ''; try { last = localStorage.getItem('tt-gen-other') || ''; } catch { }
  const pe = (last && peerOf(last)) || EP.peers[0];
  if (pe) return { ...blank(), pi: pe.id, name: pe.name };
  return { ...blank(), host: webHost(G.conn.peer_api) || G.conn.peer_host || '', typed: true };
}
// for test sets (testset.js): the connection settings of this page and the default From → To
export const genConn = () => G.conn;
export const defaultEnds = () => [SELF(), otherPi()];
const named = e => !!(e && (e.pi || e.host || e.typed));
// A preset names its streams by direction only (this Pi -> other Pi): the pickers need real ends, so fill them in once.
function initEnds(p) {
  if (!p || !p.gen) return p;
  let other = null;
  for (const st of p.gen.streams) {
    if (named(st.src) || named(st.dst)) continue;
    other = other || otherPi();
    const fwd = (st.direction || 'a_to_b') === 'a_to_b';
    st.src = fwd ? SELF() : { ...other }; st.dst = fwd ? { ...other } : SELF();
  }
  return p;
}
const endKey = e => { const v = valueOfEp(e, 'self'); return v === TYPED ? 'addr:' + String(e.host || '').trim().toLowerCase() : v; };
// A plain sentence about what is wrong with the streams' ends, or ''.
function endsProblem(p) {
  for (const st of p.gen.streams) {
    if (st.enabled === false) continue;
    for (const [w, label] of [['src', 'From'], ['dst', 'To']]) if (valueOfEp(st[w], 'self') === TYPED && !String(st[w].host || '').trim()) return `Stream “${st.name}”: type the IP address for “${label}”, or pick a Pi from the list.`;
    if (endKey(st.src) === endKey(st.dst)) return `Stream “${st.name}” goes from ${endName(st.src)} to the same Pi. Pick two different Pis.`;
  }
  return '';
}
function endName(e) { const v = valueOfEp(e, 'self'); return v === 'self' ? (EP.me ? EP.me.name : myName()) : v === TYPED ? (e.host || 'the typed address') : ((peerOf(v) || {}).name || e.name || v); }
const newStream = n => ({ name: `Stream ${n}`, enabled: true, type: 'udp', direction: 'a_to_b', src: SELF(), dst: otherPi(), rate: { mbps: 10 }, size: { mode: 'fixed', size: 1000, min: 64, max: 1518 }, burst_packets: 1, burst_gap_ms: 0, duration_s: null, packets: null, start_offset_s: 0, rtt_probe_pps: 10, dscp: 0, ttl: 64, dst_port: null, dst_mac: '', src_mac: '', ethertype: 0x88B5, vlan: false, vlan_id: 0, vlan_prio: 0, ip_proto: 253, seed: 1 });

function effProfile() {
  const p = clone(G.p); delete p.id;
  const c = G.conn;
  p.net = { ...(p.net || {}), peer_host: c.peer_host };
  p.gen = { ...p.gen, peer_api: c.peer_api, interface: c.interface || 'auto', peer_interface: c.peer_interface || 'auto', local_host: c.local_host, peer_password: c.peer_password };
  for (const st of p.gen.streams) for (const w of ['src', 'dst']) if (st[w]) delete st[w].typed;      // the "typed address" marker is for this page only
  return p;
}

// Where this Pi's traffic goes first (for the Wi-Fi check): the first stream's far end that is not this Pi.
function genTarget() {
  if (!G.p) return '';
  for (const st of G.p.gen.streams) for (const e of [st.dst, st.src]) { const v = valueOfEp(e, 'self'); if (v === 'self') continue; if (v === TYPED) return (e.host || '').trim() || G.conn.peer_host; const pe = peerOf(v); if (pe) return pe.host; }
  return G.conn.peer_host || webHost(G.conn.peer_api);
}

export async function renderGenerator(root, nav) {
  navigate = nav; if (G.off) G.off(); G.plots.forEach(p => p.destroy()); G.plots = [];
  G.profiles = (await api('/profiles')).filter(p => p.kind === 'generator' && !isTestset(p));
  await loadPeers();
  if (!G.p && G.profiles.length) { G.selId = G.profiles[0].id; G.p = initEnds(clone(G.profiles[0])); }
  else if (G.p) initEnds(G.p);
  draw(root);
  watchPeers(root, () => { if (!isRunning() && !hasResult()) draw(root); });      // the list of Pis refreshes by itself while this page is open
  G.off = onLive(e => { if (e.ev === 'lamp') import('./run.js').then(m => m.lampLine()); if (['gstate', 'done', 'started'].includes(e.ev)) { if (e.ev !== 'gstate') draw(root); else liveUpdate(); } });
  bindOnce(root, 'click', ev => click(ev, root));
  bindOnce(root, 'change', ev => change(ev, root));
  bindOnce(root, 'input', ev => input(ev, root));
}

// ------------------------------------------------------------------ the stream list is the screen
// What a "What" chip fills in. The chip that matches the stream's settings is lit; anything else is Custom.
const CHIPS = [
  { k: 'trip', label: 'Trip-like', set: { type: 'udp', size: { mode: 'fixed', size: 80 }, rate: { pps: 200 }, burst_packets: 1, burst_gap_ms: 0, vlan: false } },
  { k: 'scada', label: 'SCADA poll', set: { type: 'tcp', size: { mode: 'range', min: 40, max: 250 }, rate: { pps: 20 }, burst_packets: 1, burst_gap_ms: 0, vlan: false } },
  { k: 'video', label: 'Video', set: { type: 'udp', size: { mode: 'fixed', size: 1358 }, rate: { mbps: 4 }, burst_packets: 7, burst_gap_ms: 0, vlan: false } },
  { k: 'bulk', label: 'Bulk', set: { type: 'udp', size: { mode: 'fixed', size: 1400 }, rate: { mbps: 50 }, burst_packets: 1, burst_gap_ms: 0, vlan: false } },
  { k: 'imix', label: 'IMIX', set: { type: 'udp', size: { mode: 'imix' }, rate: { mbps: 20 }, burst_packets: 1, burst_gap_ms: 0, vlan: false } },
  { k: 'nms', label: 'NMS poll', set: { type: 'udp', size: { mode: 'range', min: 80, max: 600 }, rate: { pps: 30 }, burst_packets: 1, burst_gap_ms: 0, vlan: false } },      // the application library of the test sets (traffic_tester/apps.py) offers the same shapes with targets
  { k: 'ami', label: 'AMI reads', set: { type: 'udp', size: { mode: 'fixed', size: 200 }, rate: { pps: 20 }, burst_packets: 20, burst_gap_ms: 1000, vlan: false } },
  { k: 'goose', label: 'GOOSE-like', set: { type: 'l2', size: { mode: 'fixed', size: 128 }, rate: { pps: 1000 }, burst_packets: 1, burst_gap_ms: 0, vlan: true, vlan_prio: 4, ethertype: 0x88B5 } },
];
const sig = s => [s.type, s.size.mode, s.size.mode === 'fixed' ? s.size.size : s.size.mode === 'range' ? `${s.size.min}-${s.size.max}` : '', s.rate.pps != null ? 'p' + s.rate.pps : 'm' + s.rate.mbps, s.burst_packets, !!s.vlan].join('|');
const CHIP_SIG = Object.fromEntries(CHIPS.map(c => [c.k, sig({ ...newStream(0), ...c.set, size: { ...newStream(0).size, ...c.set.size } })]));
const chipOf = s => (CHIPS.find(c => CHIP_SIG[c.k] === sig(s)) || { k: 'custom' }).k;
const meanSize = s => s.size.mode === 'fixed' ? s.size.size : s.size.mode === 'range' ? (s.size.min + s.size.max) / 2 : 361.5;      // IMIX: 7 x 64, 4 x 594, 1 x 1514
const ppsOf = s => s.rate.pps != null ? s.rate.pps : s.rate.mbps * 1e6 / 8 / Math.max(1, meanSize(s));
const mbpsOf = s => s.rate.mbps != null ? s.rate.mbps : s.rate.pps * meanSize(s) * 8 / 1e6;
const fmtRate = s => s.rate.pps != null ? `${num(s.rate.pps)} pkt/s` : `${s.rate.mbps} Mb/s`;
const fmtSize = s => s.type === 'l2' && s.size.mode === 'fixed' ? `Eth ${s.size.size} B` : `${TYPES[s.type] === 'UDP' || TYPES[s.type] === 'TCP' ? TYPES[s.type] : s.type === 'ip' ? 'IP' : 'Eth'} ${s.size.mode === 'fixed' ? s.size.size + ' B' : s.size.mode === 'range' ? `${s.size.min}–${s.size.max} B` : 'IMIX'}`;
const f2 = v => v == null ? '–' : Number(v).toFixed(2);
const f1 = v => v == null ? '–' : Number(v).toFixed(1);
const lvl = (v, lim) => v == null || lim == null || lim === '' ? '' : v > lim ? ' bad' : v > lim * 0.7 ? ' amb' : '';

// Header strip under the editor: what a packet of this stream is made of.
function previewHtml(s) {
  const L = [['ETH', 14]]; if (s.type === 'l2' && s.vlan) L.push(['VLAN', 4]);
  if (s.type === 'tcp') return `<span>TCP connection · each write carries ${s.size.mode === 'fixed' ? s.size.size + ' B' : 'a size you chose'} of data</span>`;
  if (s.type === 'udp' || s.type === 'ip') L.push(['IPv4', 20]); if (s.type === 'udp') L.push(['UDP', 8]); L.push(['TG', 24]);
  const head = L.reduce((a, x) => a + x[1], 0);
  const tail = s.size.mode === 'fixed' ? `data ${Math.max(0, s.size.size - head)} = ${s.size.size} B` : 'data (sizes vary)';
  return L.map(x => `${x[0]} ${x[1]}`).join(' › ') + ' › ' + tail;
}

// The rows to draw: the running test's own streams while it runs (it may differ from the list being edited), else the list, with the newest counters.
function rowsView() {
  const run = isRunning(), res = hasResult() && !run;
  const rp = (run || res) && S.live.profile && S.live.profile.gen;
  const nums = ((run || res) && S.gen.last && S.gen.last.streams_now) || [];
  let rows;
  if (run && rp) rows = rp.streams.filter(x => x.enabled !== false).map((s, k) => ({ s, i: k, n: nums[k] }));
  else {
    const ran = res ? G.ran : null;
    rows = G.p.gen.streams.map((s, i) => { const k = ran ? ran.indexOf(i) : -1; return { s, i, n: k >= 0 ? nums[k] : null, off: s.enabled === false }; });
  }
  if (run) rows.forEach(r => { if (G.ov[r.i] !== undefined) { if (r.n && r.n.on === G.ov[r.i]) delete G.ov[r.i]; else r.n = { ...(r.n || {}), on: G.ov[r.i] }; } });
  return { rows, run, res, rule: (rp || G.p.gen).rule || {}, sum: ((run || res) && S.gen.last && S.gen.last.sum) || null };
}
const sel = (attrs, opts, cur) => `<select ${attrs}>${opts.map(([v, l]) => `<option value="${esc(v)}" ${String(cur) === String(v) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;

function rowsHtml(V) {
  const e = V.rule;
  if (G.sel >= V.rows.length) G.sel = 0;
  const dash = '<span class="mute">–</span>';
  const out = V.rows.map(({ s, i, n }) => {
    const on = V.run ? !(n && n.on === false) : s.enabled !== false;
    const has = !!n && n.tx_pkts !== undefined && (V.run || V.res);
    const lp = has && n.tx_pkts ? 100 * n.lost / n.tx_pkts : (has ? 0 : null);
    const bad = lp != null && e.max_loss_pct != null ? lvl(lp, e.max_loss_pct) : (lp > 0 ? ' amb' : '');
    const live = has && on;
    const tg = (a, ...cls) => `<td class="${cls.join(' ')}">${a}</td>`;
    const rx = live ? f2(n.rx_mbps) : dash;
    return `<tr class="${i === G.sel ? 'sel' : ''} ${on ? '' : 'off'}${live ? bad : ''}" data-act="sel" data-i="${i}" data-testid="gen-row-${i + 1}" tabindex="0">
      <td class="tg"><button type="button" class="swb ${on ? 'on' : ''}" role="switch" aria-checked="${on}" aria-label="Stream ${i + 1} on" data-act="tgl" data-i="${i}" data-testid="gen-on-${i + 1}"><i></i></button></td>
      <td class="nm"><b>${esc(s.name)}</b><small class="t3h">${esc(endName(s.src))} → ${esc(endName(s.dst))} · ${esc(fmtSize(s))} · ${esc(fmtRate(s))}</small></td>
      <td class="n p-only">${rx}</td>
      <td class="t3 mono">${esc(endName(s.src))} → ${esc(endName(s.dst))}</td><td class="t3 mono">${esc(fmtSize(s))}</td>
      <td class="n t2">${esc(fmtRate(s))}</td>
      <td class="n t2">${live ? num(n.tx_pkts) : dash}</td><td class="n t2">${live ? num(n.rx_pkts) : dash}</td><td class="n t2${live && n.lost ? ' amb' : ''}">${live ? num(n.lost) : dash}</td>
      <td class="n${live ? bad : ''}">${live ? f2(lp) : dash}</td>
      <td class="n${live ? lvl(n.lat_ms, e.max_latency_ms) : ''}">${live && n.lat_ms != null ? Math.round(n.lat_ms) : dash}</td>
      <td class="n t2${live ? lvl(n.jit_ms, e.max_jitter_ms) : ''}">${live && n.jit_ms != null ? f1(n.jit_ms) : dash}</td>
      <td class="n t2 rxm"><b>${rx}</b></td></tr>`;
  }).join('');
  const sm = V.sum, k = V.rows.filter(r => V.run ? !(r.n && r.n.on === false) : r.s.enabled !== false).length;
  const have = sm && (V.run || V.res);
  const sumRow = `<tr class="sum"><td></td><td class="nm"><b>${k} stream${k === 1 ? '' : 's'} on</b></td><td class="n p-only">${have ? f2(sm.rx_mbps) : dash}</td><td class="t3"></td><td class="t3"></td>
    <td class="n t2">${have ? f2(sm.tx_mbps) + ' Mb/s' : dash}</td><td class="n t2">${have ? num(sm.tx_pkts) : dash}</td><td class="n t2">${have ? num(sm.rx_pkts) : dash}</td><td class="n t2">${have ? num(sm.lost) : dash}</td>
    <td class="n">${have ? f2(sm.loss_pct) : dash}</td><td class="n"></td><td class="n t2"></td><td class="n t2 rxm"><b>${have ? f2(sm.rx_mbps) : dash}</b></td></tr>`;
  return out + sumRow;
}

function tableHtml(V) {
  const note = V.run ? (S.gen.last && S.gen.last.cleared_s != null ? `counters since clear at ${dur(S.gen.last.cleared_s)}` : 'counters since the start') : V.res ? 'numbers of the last run' : 'tap a row to edit';
  return `<div class="panel" data-testid="gen-list"><header><span>Streams</span><small id="gNote">${note}</small></header><div class="gscroll"><table class="ptable gtab">
    <thead><tr><th class="tg">On</th><th>Stream</th><th class="r p-only">RX Mb/s</th><th class="t3">From → to</th><th class="t3">Type · size</th><th class="r t2">Rate</th><th class="r t2">TX pkts</th><th class="r t2">RX pkts</th><th class="r t2">Lost</th><th class="r">Loss %</th><th class="r">Delay ms</th><th class="r t2">Jitter</th><th class="r t2 rxm">RX Mb/s</th></tr></thead>
    <tbody id="gRows">${rowsHtml(V)}</tbody></table></div></div>`;
}

function draw(root) {
  if (!G.p) { root.innerHTML = '<div class="card empty"><h2>No presets found</h2></div>'; return; }
  const V = rowsView(), run = V.run, res = V.res;
  G.ov = run ? G.ov : {};
  const mine = run && S.live.profile && S.live.profile.gen ? S.live.profile.gen.streams.filter(x => x.enabled !== false)[G.sel] : G.p.gen.streams[G.sel];
  const idle = !run;
  root.innerHTML = `
    <input type="file" id="gImp" accept=".json,application/json" hidden>
    <div class="gtools" data-testid="gen-tools">
      ${idle ? `<button class="btn primary" data-act="addstream" data-testid="gen-add-stream">＋ Stream</button>
      <button class="btn d-tool" data-act="dupstream" data-testid="gen-dup-stream">Duplicate</button><button class="btn d-tool" data-act="both" data-i="${G.sel}" data-testid="gen-stream-${G.sel + 1}-reverse" title="Add the same stream the other way round (To and From swapped)">⇄ Reverse</button>
      <button class="btn d-tool" data-act="rm" data-i="${G.sel}" data-testid="gen-rm-stream" ${G.p.gen.streams.length < 2 ? 'disabled' : ''}>Delete</button>
      <span class="gsp"></span><button class="btn" data-act="save" data-testid="gen-save">Save as profile…</button><button class="btn" data-act="more" aria-label="More" data-testid="gen-more">⋯</button>`
      : '<span class="muted">Switch a stream off or on with its switch. Stop to change anything else.</span>'}
      <button class="btn primary tb-src" data-act="start" data-testid="gen-start" ${idle ? '' : 'hidden'}>▶ Start</button>
    </div>
    <div class="gcols"><div class="glist">
      <div id="liveBox">${res ? resultHtml() : ''}</div>
      ${tableHtml(V)}
    </div>
    <aside class="gedit panel ${G.edOpen ? 'open' : ''}" id="gEdit" data-testid="gen-editor">${mine ? editorHtml(mine, G.sel, !idle) : ''}</aside>
    <div class="grest">${run ? liveHtml() : idle ? settingsHtml() : ''}</div></div>`;
  if (run) liveInit(); else mountWifi($('#gWifi', root), genTarget);
  if (!run && $('#gSteps', root)) Steps.form($('#gSteps', root), G.p.gen, { onChange: () => draw(root) });      // Throughput: Steps (find the clean rate) or Flood
  if (res && $('#gStepsRes', root)) Steps.result($('#gStepsRes', root), S.live.done.summary);
}

// refresh only what a typed value changes (the list row, the maths line), so typing is never interrupted
function softRefresh(root) {
  syncRows(rowsHtml(rowsView()));
  const s = G.p.gen.streams[G.sel], m = $('#gMath', root); if (s && m) m.textContent = mathLine(s); const pv = $('#gPrev', root); if (s && pv) pv.innerHTML = previewHtml(s);
}

function mathLine(s) {
  const g = G.p.gen, len = s.packets ? `${num(s.packets)} packets` : s.duration_s ? `for ${dur(s.duration_s)}` : (!g.duration_s || Steps.isOn(g) ? (Steps.isOn(g) ? 'length set by the steps' : 'until stopped') : `for ${dur(g.duration_s)}`);
  return `≈ ${num(Math.round(ppsOf(s)))} pkt/s · ${f2(mbpsOf(s))} Mb/s · ${len}`;
}

// ------------------------------------------------------------------ the editor beside the list (a sheet on a phone)
function field(label, inner, t = '') { return `<label class="f"><span>${label} ${t ? tip(t) : ''}</span>${inner}</label>`; }
const inp = (attrs, v, type = 'text', extra = '') => `<input type="${type}" ${attrs} value="${esc(v ?? '')}" ${extra}>`;
function endPicker(s, i, w, label) {
  const ep = s[w] || blank(), cur = valueOfEp(ep, 'self'), n = `gen-stream-${i + 1}-${w === 'src' ? 'from' : 'to'}`;
  return `<div class="epcol"><label class="f"><span>${label}</span><select data-gp="${w}" data-i="${i}" data-testid="${n}">${optionsFor(cur, ep, { typed: true })}</select></label>
    ${cur === TYPED ? `<label class="f eptyped"><span>IP address or name of the Pi</span><input data-gh="${w}" data-i="${i}" data-testid="${n}-host" inputmode="decimal" autocomplete="off" placeholder="e.g. 192.168.1.50" value="${esc(ep.host || '')}"></label>` : ''}</div>`;
}
function editorHtml(s, i, locked) {
  const rateUnit = s.rate.pps != null ? 'pps' : 'mbps', rateVal = s.rate.pps ?? s.rate.mbps, d = `data-i="${i}"`, bad = endsProblem({ gen: { streams: [s] } });
  const chip = chipOf(s), bursts = s.burst_packets > 1 || s.burst_gap_ms > 0;
  const moreSum = [s.type === 'l2' ? (s.vlan ? `VLAN ${s.vlan_id} prio ${s.vlan_prio}` : 'VLAN off') : `DSCP ${s.dscp}`, s.type === 'udp' || s.type === 'tcp' ? `port ${s.dst_port || 'auto'}` : '', s.start_offset_s > 0 ? `starts after ${s.start_offset_s} s` : ''].filter(Boolean).join(' · ');
  return `<header><span>Stream ${i + 1} · ${esc(s.name)}</span><small>${locked ? 'locked while it runs' : 'changes apply at once'}</small><button type="button" class="btn small ged-x" data-act="edclose" data-testid="gen-ed-close">Done</button></header>
  <div class="pbody ged" ${locked ? 'inert' : ''}>
    ${field('Name', inp(`${d} data-k="name" data-testid="gen-name"`, s.name))}
    <h4>What</h4><div class="chips" data-testid="gen-chips">${CHIPS.map(c => `<button type="button" class="chip ${chip === c.k ? 'on' : ''}" data-act="chip" data-k="${c.k}" data-i="${i}" data-testid="gen-chip-${c.k}">${c.label}</button>`).join('')}<button type="button" class="chip ${chip === 'custom' ? 'on' : ''}" data-act="chip" data-k="custom" data-i="${i}" data-testid="gen-chip-custom">Custom</button></div>
    ${bad ? `<p class="why bad" data-testid="gen-stream-${i + 1}-problem" role="alert">${esc(bad)}</p>` : ''}
    <h4>From → to</h4><div class="ends">${endPicker(s, i, 'src', 'From')}<button type="button" class="btn small swap" data-act="swap" data-i="${i}" aria-label="Swap From and To" title="Swap From and To" data-testid="gen-swap">⇄</button>${endPicker(s, i, 'dst', 'To')}</div>
    <h4>How much</h4>
    <div class="row how"><input class="big" ${d} data-rate="val" type="number" step="any" min="0" value="${esc(rateVal)}" aria-label="Rate" data-testid="gen-rate"><div class="seg" role="group" aria-label="Unit"><button type="button" class="${rateUnit === 'mbps' ? 'on' : ''}" data-act="unit" data-u="mbps" ${d} data-testid="gen-unit-mbps">Mb/s</button><button type="button" class="${rateUnit === 'pps' ? 'on' : ''}" data-act="unit" data-u="pps" ${d} data-testid="gen-unit-pps">pkt/s</button></div></div>
    <div class="grid g2">${field('Type', sel(`${d} data-k="type"`, Object.entries(TYPES), s.type), TYPE_TIP)}${field('Packet size', sel(`${d} data-k="size.mode"`, [['fixed', 'Fixed'], ['range', 'Random range'], ['imix', 'IMIX (mixed)']], s.size.mode), 'IMIX = 7 small (64 B), 4 medium (594 B), 1 large (1,514 B) frames: a typical internet mix. Sizes are counted without the 4-byte Ethernet check.')}
      ${s.size.mode === 'fixed' ? field('Size (bytes)', inp(`${d} data-k="size.size" data-t="num"`, s.size.size, 'number', 'min="24" max="9000"'), 'Whole frame size in bytes. The smallest possible depends on the type (raw 60, UDP 66).') : ''}
      ${s.size.mode === 'range' ? field('Smallest (bytes)', inp(`${d} data-k="size.min" data-t="num"`, s.size.min, 'number')) + field('Largest (bytes)', inp(`${d} data-k="size.max" data-t="num"`, s.size.max, 'number')) : ''}
      ${field('Packets per burst', inp(`${d} data-k="burst_packets" data-t="num"`, s.burst_packets, 'number', 'min="1"'), 'Packets sent back to back in one go. 1 = evenly spaced.')}
      ${field('Gap between bursts (ms)', inp(`${d} data-k="burst_gap_ms" data-t="num"`, s.burst_gap_ms, 'number', 'min="0" step="any"'), '0 = keep the rate steady. More than 0 = send each burst at full speed, then wait this long.')}</div>
    <p class="math mono" id="gMath">${esc(mathLine(s))}</p>
    <details class="gmore" ${bursts || s.duration_s || s.packets || s.start_offset_s > 0 || s.vlan ? 'open' : ''}><summary data-testid="gen-more-options"><b>More options</b><span>${esc(moreSum)}</span></summary><div class="grid g2" style="margin-top:8px">
      ${field('This stream runs for (seconds)', inp(`${d} data-k="duration_s" data-t="nul"`, s.duration_s, 'number', 'min="1" step="any" placeholder="whole test"'))}
      ${field('Or stop after (packets)', inp(`${d} data-k="packets" data-t="nul"`, s.packets, 'number', 'min="1" placeholder="no limit"'))}
      ${field('Start after (seconds)', inp(`${d} data-k="start_offset_s" data-t="num"`, s.start_offset_s, 'number', 'min="0" step="any"'))}
      ${field('Round-trip probes per second', inp(`${d} data-k="rtt_probe_pps" data-t="num"`, s.rtt_probe_pps, 'number', 'min="0"'), 'A few packets per second are echoed back to measure round-trip time. 0 = off. (Not for TCP.)')}
      ${s.type !== 'l2' ? field('Priority (DSCP 0-63)', inp(`${d} data-k="dscp" data-t="num"`, s.dscp, 'number', 'min="0" max="63"'), 'dscp') + field('TTL', inp(`${d} data-k="ttl" data-t="num"`, s.ttl, 'number', 'min="1" max="255"')) : ''}
      ${s.type === 'udp' || s.type === 'tcp' ? field('Port', inp(`${d} data-k="dst_port" data-t="nul"`, s.dst_port, 'number', 'placeholder="automatic"')) : ''}
      ${s.type === 'ip' ? field('IP protocol number', inp(`${d} data-k="ip_proto" data-t="num"`, s.ip_proto, 'number', 'min="0" max="255"')) : ''}
      ${s.type === 'l2' ? field('Destination MAC', inp(`${d} data-k="dst_mac" placeholder="other Pi (automatic)"`, s.dst_mac)) + field('EtherType (decimal)', inp(`${d} data-k="ethertype" data-t="num"`, s.ethertype, 'number'), '34997 = 0x88B5, an Ethernet type reserved for local experiments.') +
        `<label class="chk"><input type="checkbox" ${d} data-k="vlan" ${s.vlan ? 'checked' : ''}> VLAN tag</label>` + field('VLAN id', inp(`${d} data-k="vlan_id" data-t="num"`, s.vlan_id, 'number', 'min="0" max="4094"')) + field('VLAN priority (0-7)', inp(`${d} data-k="vlan_prio" data-t="num"`, s.vlan_prio, 'number', 'min="0" max="7"')) : ''}
    </div></details>
    <div class="prev mono" id="gPrev" data-testid="gen-preview">${previewHtml(s)}</div>
  </div>`;
}

// ------------------------------------------------------------------ settings under the list (idle)
function settingsHtml() {
  const c = G.conn, g = G.p.gen, r = g.rule, stepsOn = Steps.isOn(g) && !Steps.blocker(g);
  const notAuto = v => v && v !== 'auto';
  const connAdv = c.peer_api || c.peer_host || c.local_host || notAuto(c.interface) || notAuto(c.peer_interface) || c.peer_password;
  return `
  <div class="panel" data-testid="gen-run-settings"><header><span>How long, and when is it a pass?</span><small>the limits below are example targets: change them</small></header><div class="pbody">
    <div id="gSteps" style="margin-bottom:12px"></div>
    <div class="grid g3">
      ${stepsOn ? '' : field('Test length', sel('data-glen="mode" data-testid="gen-length-mode"', [['until', 'Until stopped'], ['fixed', 'A set time']], g.duration_s ? 'fixed' : 'until'), 'Until stopped: the streams send until you press Stop (the result covers the time they ran). A set time ends by itself.')}
      ${stepsOn || !g.duration_s ? '' : field('Seconds', inp('data-g="duration_s" data-testid="gen-length"', g.duration_s, 'number', 'step="any" min="1"'))}
      ${field('Lost packets must be under (%)', inp('data-rule="max_loss_pct"', r.max_loss_pct, 'number', 'step="any" min="0" placeholder="no limit"'), 'loss')}
      ${field('Delay must be under (ms)', inp('data-rule="max_latency_ms"', r.max_latency_ms, 'number', 'step="any" min="0" placeholder="no limit"'), 'One-way delay of packets from sender to receiver. Needs both Pis\' clocks synced (the app checks).')}
      ${field('…measured at', sel('data-rule="latency_percentile" data-t="num"', [[50, 'Typical (p50)'], [95, 'p95'], [99, 'p99 (recommended)'], [100, 'Slowest (max)']], r.latency_percentile), 'p99')}
      ${field('Jitter must be under (ms)', inp('data-rule="max_jitter_ms"', r.max_jitter_ms, 'number', 'step="any" min="0" placeholder="no limit"'), 'jitter')}
      ${field('Throughput at least (Mb/s)', inp('data-rule="min_throughput_mbps"', r.min_throughput_mbps, 'number', 'step="any" min="0" placeholder="no limit"'), 'Total received rate across all streams.')}
    </div></div></div>
  <details class="panel gpis" open data-testid="gen-pis"><summary data-testid="gen-pis-summary"><span>Pis and link</span><small data-testid="gen-pis-count" role="status">${EP.loaded ? (EP.peers.length ? `${EP.peers.length} other Pi${EP.peers.length > 1 ? 's' : ''} on this network` : 'No other Pi found yet') : 'Looking for other Pis…'}</small></summary><div class="pbody">
    <p class="muted">Each stream picks where it starts (From) and where it ends (To). Pis on this network appear in the lists by themselves; a Pi that does not show up can be added by its IP address.</p>
    <div class="row epbar"><button type="button" class="btn small" data-act="pis-refresh" data-testid="gen-refresh-pis">↻ Look again</button><button type="button" class="btn small" data-act="pis-add" data-testid="gen-add-pi">＋ Add a Pi by IP</button></div>
    <div id="gWifi" style="margin-top:10px"></div>
    <details id="gConnAdv" ${connAdv ? 'open' : ''} data-testid="gen-advanced"><summary>Advanced: data address, web address or password for a Pi that is not in the list</summary><div class="grid g3" style="margin-top:8px">
      ${field("Data address of a typed Pi (IP)", inp('data-conn="peer_host" placeholder="192.168.1.51"', c.peer_host), 'Where UDP, TCP and raw-IP packets are sent to a Pi you typed by address. Leave it empty to use the address you typed in the stream; fill it in only when the data takes another path (for example the one that goes through the radio).')}
      ${field("Web address of a typed Pi", inp('data-conn="peer_api" placeholder="192.168.1.51:8080"', c.peer_api), 'The address you would type in a browser to open that Pi\'s Traffic Tester. It is used to control the test, not to carry it. Leave it empty to use the address typed in the stream.')}
      ${field('Password of a typed Pi (if set)', inp('data-conn="peer_password" autocomplete="off"', c.peer_password, 'password'), 'Only if you protected that Pi with a password. For Pis in the list, enter the password in Settings. Kept on this Pi only, never saved in profiles.')}
      ${field("This Pi's data address (optional)", inp('data-conn="local_host" placeholder="auto"', c.local_host), 'Needed only for streams that end on this Pi and start on a Pi that cannot work out the way back. Blank = taken from this Pi\'s port.')}
      ${field('This Pi\'s network port', inp('data-conn="interface" placeholder="auto"', c.interface), 'Used by raw Ethernet streams. "auto" = the first wired port.')}
      ${field("Network port on the other Pis", inp('data-conn="peer_interface" placeholder="auto"', c.peer_interface), 'Same, on the other Pis.')}
    </div></details></div></details>`;
}

function pcard(p) {
  const sl = p.id === G.selId;
  return `<div class="pcard ${sl ? 'sel' : ''}" data-act="pick" data-id="${p.id}" tabindex="0" role="button">
    <h3>${esc(p.name)} <span class="tag ${p.builtin ? 'preset' : ''}">${p.builtin ? 'Preset' : 'Saved'}</span></h3><p>${esc(p.description || '')}</p>
    <div class="meta">${p.gen.streams.length} stream${p.gen.streams.length > 1 ? 's' : ''} · ${Steps.isOn(p.gen) ? esc(Steps.summary(p.gen)) : p.gen.duration_s ? dur(p.gen.duration_s) : 'until stopped'}</div>
    <div class="acts"><button class="btn small ghost" data-act="dup" data-id="${p.id}">Duplicate to edit</button><button class="btn small ghost" data-act="exp1" data-id="${p.id}">Export</button>${p.builtin ? '' : `<button class="btn small ghost" data-act="del" data-id="${p.id}">Delete</button>`}</div></div>`;
}

// The ⋯ sheet: ready-made and saved traffic tests, import, export.
function moreSheet(root) {
  const m = $('#modal');
  const nst = G.p.gen.streams.length;
  const p = sheetBox('More', `<h3 class="grp" style="margin-top:0">The selected stream</h3><div class="row" style="margin-bottom:8px"><button class="btn" data-act="dupstream" data-testid="gen-more-dup">Duplicate</button><button class="btn" data-act="both" data-i="${G.sel}" data-testid="gen-more-reverse">⇄ Reverse</button><button class="btn" data-act="rm" data-i="${G.sel}" data-testid="gen-more-delete" ${nst < 2 ? 'disabled' : ''}>Delete</button></div>
    <h3 class="grp">Traffic tests</h3><p class="muted">Load a ready-made or saved traffic test into the list, or bring one in from a file.</p><div class="grid g2" data-testid="gen-presets">${G.profiles.map(pcard).join('')}</div>
    <div class="row" style="margin-top:12px"><button class="btn" data-act="imp" data-testid="gen-import">⤒ Import a file</button><button class="btn" data-act="exp1" data-id="${G.selId || 0}" ${G.selId ? '' : 'disabled'}>Export the loaded one</button></div>`, [{ id: 'close', label: 'Close' }], 'gen-more-sheet');
  const h = ev => { const a = ev.target.closest('[data-act]'); if (!a) return; click(ev, root); if (['pick', 'dup', 'imp', 'del', 'dupstream', 'both', 'rm'].includes(a.dataset.act)) $('#mX') && $('#mX').click(); };
  m.addEventListener('click', h); p.then(() => m.removeEventListener('click', h));
}

// ------------------------------------------------------------------ events
function applyField(el) {                                      // a value typed or picked in the editor or the settings; true = the page must be drawn again
  if (el.dataset.glen) { const g = G.p.gen; g.duration_s = el.value === 'until' ? 0 : (g.duration_s || 60); return true; }
  if (el.dataset.g) { G.p.gen[el.dataset.g] = Number(el.value); return false; }
  if (el.dataset.rule) { const k = el.dataset.rule; G.p.gen.rule[k] = el.dataset.t === 'num' ? Number(el.value) : (el.value === '' ? null : Number(el.value)); return false; }
  if (el.dataset.i !== undefined) {
    const s = G.p.gen.streams[+el.dataset.i]; if (!s) return false;
    if (el.dataset.rate) { const u = s.rate.pps != null ? 'pps' : 'mbps'; s.rate = { [u]: Number(el.value) }; return false; }
    const k = el.dataset.k; let v = el.type === 'checkbox' ? el.checked : el.value;
    if (el.dataset.t === 'num') v = Number(v); if (el.dataset.t === 'nul') v = v === '' ? null : Number(v);
    setp(s, k, v);
    if (k === 'type' && v === 'l2') s.size.size = Math.max(s.size.size, 64);
    return k === 'type' || k === 'size.mode' || k === 'vlan';
  }
  return false;
}
function input(ev, root) {                                     // while typing: keep the model and the list row current, never redraw the field being typed in
  const el = ev.target; if (el.tagName !== 'INPUT' || el.type === 'checkbox' || el.dataset.gh || el.dataset.conn || el.dataset.glen) return;
  if (el.dataset.g === undefined && el.dataset.rule === undefined && el.dataset.i === undefined) return;
  if (applyField(el)) return; softRefresh(root);
}

function change(ev, root) {
  const el = ev.target;
  if (el.id === 'gImp') { const f = el.files[0]; if (f) f.text().then(async t => { try { const r = await api('/profiles/import', { method: 'POST', body: JSON.parse(t) }); toast(`Imported ${r.imported.length} profile(s).`); renderGenerator(root, navigate); } catch (e) { toast(e.message || 'That file could not be imported.', true, 7000); } }); return; }
  if (el.dataset.gp) {                                       // a stream's From / To
    const st = G.p.gen.streams[+el.dataset.i], w = el.dataset.gp;
    st[w] = epFromValue(el.value, st[w]);
    if (el.value === TYPED && !st[w].host && G.conn.peer_api) st[w].host = webHost(G.conn.peer_api);
    if (el.value && el.value !== 'self' && el.value !== TYPED) { try { localStorage.setItem('tt-gen-other', el.value); } catch { } }
    draw(root); return;
  }
  if (el.dataset.gh) { const st = G.p.gen.streams[+el.dataset.i]; st[el.dataset.gh].host = el.value.trim(); st[el.dataset.gh].typed = true; draw(root); return; }
  if (el.dataset.conn) { G.conn[el.dataset.conn] = el.value.trim(); saveConn(); if (el.dataset.conn === 'peer_api') for (const st of G.p.gen.streams) for (const w of ['src', 'dst']) if (valueOfEp(st[w], 'self') === TYPED && !st[w].host) st[w].host = webHost(G.conn.peer_api); if (el.dataset.conn === 'peer_api' || el.dataset.conn === 'peer_host') mountWifi($('#gWifi'), genTarget); return; }
  if (applyField(el)) draw(root); else if (el.tagName !== 'INPUT' || el.type === 'checkbox') softRefresh(root);      // a typed value was already applied while typing: rebuilding the list now would swallow the tap that caused this blur
}

// Update the list in place: only the cells whose text changed are touched, so a tap on a switch is never lost to a redraw that lands between touch-down and touch-up.
function syncRows(html) {
  const body = $('#gRows'); if (!body) return;
  const t = document.createElement('tbody'); t.innerHTML = html;
  const a = [...body.children], b = [...t.children];
  if (a.length !== b.length) { body.innerHTML = html; return; }
  b.forEach((nr, k) => {
    const or = a[k]; if (or.className !== nr.className) or.className = nr.className;
    [...nr.children].forEach((nc, j) => { const oc = or.children[j]; if (!oc) return; if (oc.className !== nc.className) oc.className = nc.className; if (oc.innerHTML !== nc.innerHTML) oc.innerHTML = nc.innerHTML; });
  });
}

function setChip(s, k) {
  const c = CHIPS.find(x => x.k === k); if (!c) return;
  const was = CHIPS.find(x => x.label === s.name);
  Object.assign(s, clone(c.set), { size: { ...s.size, ...c.set.size } });
  if (/^Stream \d+$/.test(s.name) || was) s.name = c.label;
}

async function click(ev, root) {
  const a = ev.target.closest('[data-act]'); if (!a) return;
  const act = a.dataset.act, id = +a.dataset.id, i = a.dataset.i !== undefined ? +a.dataset.i : null;
  const reload = async () => { G.profiles = (await api('/profiles')).filter(p => p.kind === 'generator' && !isTestset(p)); draw(root); };
  const run = isRunning();
  try {
    if (act === 'sel') {
      if (ev.target.closest('[data-act=tgl]')) return;       // the switch has its own action
      G.sel = i; G.edOpen = true; draw(root); if (innerWidth < 1180) $('#gEdit', root).scrollIntoView({ block: 'start' }); return;
    }
    if (act === 'edclose') { G.edOpen = false; $('#gEdit', root).classList.remove('open'); $('.glist', root).scrollIntoView({ block: 'start' }); }
    else if (act === 'tgl') {
      ev.stopPropagation();
      if (run) {
        const cur = rowsView().rows.find(r => r.i === i), on = !(cur && cur.n && cur.n.on === false);
        G.ov[i] = !on; softRefresh(root);
        try { await api(`/runs/${S.live.runId}/stream`, { method: 'POST', body: { i, on: !on } }); } catch (e) { delete G.ov[i]; softRefresh(root); throw e; }
      } else { const s = G.p.gen.streams[i]; s.enabled = s.enabled === false; draw(root); }
    }
    else if (act === 'more') moreSheet(root);
    else if (act === 'pick') { const p = G.profiles.find(x => x.id === id); G.selId = id; G.p = initEnds(clone(p)); G.sel = 0; G.ran = null; draw(root); }
    else if (act === 'pis-refresh') { a.disabled = true; a.textContent = 'Looking…'; await loadPeers(true); draw(root); }
    else if (act === 'pis-add') { const pe = await addPiSheet(); if (pe) { await loadPeers(); draw(root); toast(`Added ${pe.name}.`); } }
    else if (act === 'dup') { const p = await api(`/profiles/${id}/duplicate`, { method: 'POST' }); G.selId = p.id; G.p = initEnds(clone(p)); toast(`Created “${p.name}”. It's yours to edit.`); await reload(); }
    else if (act === 'del') { if (await confirmBox('Delete this profile?', 'Past results are kept.', 'Delete', true)) { await api(`/profiles/${id}`, { method: 'DELETE' }); if (G.selId === id) { G.selId = null; G.p = null; } await renderGenerator(root, navigate); } }
    else if (act === 'exp1') { const r = await fetch(`/api/profiles/export?ids=${id}`); download(`traffic-generator-profile-${id}.json`, await r.text()); }
    else if (act === 'imp') $('#gImp', root).click();
    else if (act === 'addstream') { G.p.gen.streams.push(newStream(G.p.gen.streams.length + 1)); G.sel = G.p.gen.streams.length - 1; G.ran = null; G.edOpen = true; draw(root); if (innerWidth < 1180) $('#gEdit', root).scrollIntoView({ block: 'start' }); }
    else if (act === 'dupstream') { const s = clone(G.p.gen.streams[G.sel]); s.name += ' (copy)'; G.p.gen.streams.splice(G.sel + 1, 0, s); G.sel++; G.ran = null; draw(root); }
    else if (act === 'rm') { G.p.gen.streams.splice(i, 1); G.sel = Math.max(0, Math.min(G.sel, G.p.gen.streams.length - 1)); G.ran = null; G.edOpen = false; draw(root); }
    else if (act === 'both') { const s = clone(G.p.gen.streams[i]); s.direction = s.direction === 'a_to_b' ? 'b_to_a' : 'a_to_b'; [s.src, s.dst] = [s.dst, s.src]; s.name = s.name + ' (reverse)'; G.p.gen.streams.splice(i + 1, 0, s); G.ran = null; draw(root); }
    else if (act === 'swap') { const s = G.p.gen.streams[i]; [s.src, s.dst] = [s.dst, s.src]; s.direction = s.direction === 'a_to_b' ? 'b_to_a' : 'a_to_b'; draw(root); }
    else if (act === 'chip') { const s = G.p.gen.streams[i]; setChip(s, a.dataset.k); draw(root); }
    else if (act === 'unit') {
      const s = G.p.gen.streams[i], to = a.dataset.u, now = s.rate.pps != null ? 'pps' : 'mbps'; if (to === now) return;
      s.rate = to === 'pps' ? { pps: Math.max(1, Math.round(ppsOf(s))) } : { mbps: Math.round(mbpsOf(s) * 100) / 100 || 1 }; draw(root);
    }
    else if (act === 'save') {
      const name = await promptBox('Save as profile', 'Name for this traffic test', G.p.builtin ? `${G.p.name} (mine)` : G.p.name); if (!name) return;
      const p = effProfile(); p.gen.peer_password = ''; p.name = name; p.builtin = false;
      const cur = G.profiles.find(x => x.id === G.selId && !x.builtin && x.name === name);
      const r = cur ? await api(`/profiles/${cur.id}`, { method: 'PUT', body: p }) : await api('/profiles', { method: 'POST', body: p });
      G.selId = r.id; G.p = initEnds(clone(r)); toast('Profile saved.'); await reload();
    }
    else if (act === 'start') {
      const problem = endsProblem(G.p); if (problem) return toast(problem, true, 8000);
      if (!G.p.gen.streams.some(s => s.enabled !== false)) return toast('Switch at least one stream on.', true, 6000);
      const p = effProfile(); a.disabled = true;
      G.ran = G.p.gen.streams.map((s, k) => s.enabled !== false ? k : -1).filter(k => k >= 0); G.ov = {};
      try { const r = await api('/runs', { method: 'POST', body: { profile: p } }); resetLive(p, r.run_id); S.status = await api('/status'); G.edOpen = false; draw(root); }
      catch (e) { toast(e.message, true, 9000); a.disabled = false; G.ran = null; }
    }
    else if (act === 'mark') { (await import('./run.js')).dropMark(); }
    else if (act === 'stop') { if (await confirmBox('Stop the test?', 'Results so far are kept and judged.', 'Stop', true)) await api(`/runs/${S.live.runId}/stop`, { method: 'POST' }); }
    else if (act === 'again') { S.live.done = null; G.ran = null; draw(root); }
    else if (act === 'results') { S.openRun = S.live.runId; navigate('results'); }
  } catch (e) { toast(e.message, true, 8000); }
}

// ------------------------------------------------------------------ live view (under the list)
const PAL = ['#0f766e', '#2563eb', '#d97706', '#9333ea', '#dc2626', '#0891b2', '#65a30d', '#db2777'];
function liveHtml() {
  return `${stateBar('RUNNING', 'you can leave this page; it keeps going')}<div id="lampLine" data-testid="run-lamp"></div>
  <div class="gtiles" data-testid="gen-tiles"><div><small>TX frames</small><b id="gTx">–</b><span id="gTxs"></span></div><div><small>RX frames</small><b id="gRx">–</b><span id="gRxs"></span></div><div><small>Lost</small><b id="gLost">–</b><span id="gLosts"></span></div><div><small>RX rate</small><b id="gRate">–</b><span>Mb/s</span></div></div>
  <div id="gStepsLive" data-testid="gen-steps-live"></div>
  <div class="panel"><header><span>RX rate per stream</span><small id="gLeg1"></small></header><div id="gC1" class="chart"></div></div>
  <div class="panel"><header><span>Delay per stream</span><small id="gLeg2">ms, one-way</small></header><div id="gC2" class="chart"></div></div>`;
}
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function liveInit() {
  if (!window.uPlot || !$('#gC1') || !$('#gC2')) return;      // the screen changed under us (a tab was tapped while the live view was being drawn)
  const axis = { stroke: css('--muted'), grid: { stroke: css('--line'), width: 1 }, ticks: { stroke: css('--line') } };
  const nst = Math.max(1, ((S.gen.last && S.gen.last.streams_now) || []).length || rowsView().rows.length);
  G.nst = nst;
  const names = rowsView().rows.map(r => r.s.name);
  const mk = (el, label) => { const w = Math.max(200, el.clientWidth); const ser = Array.from({ length: nst }, (_, k) => ({ label: names[k] || `Stream ${k + 1}`, stroke: PAL[k % PAL.length], width: 1.6, points: { show: false }, spanGaps: false }));
    const p = new uPlot({ plugins: [markPlugin(() => S.live.marks)], width: w, height: 200, padding: [8, 8, 0, 0], legend: { show: false }, scales: { x: { time: false }, y: { range: (u, a, b) => [0, Math.max(1, (b || 0) * 1.15)] } }, axes: [{ ...axis, label: 'seconds' }, { ...axis, label, size: 56 }], series: [{ label: 'Time' }, ...ser] }, [[], ...ser.map(() => [])], el); G.plots.push(p); return p; };
  G.c1 = mk($('#gC1'), 'Mb/s'); G.c2 = mk($('#gC2'), 'ms');
  const leg = names.map((n, k) => `<span style="color:${PAL[k % PAL.length]}">▬ ${esc(n)}</span>`).join(' ');
  $('#gLeg1').innerHTML = leg + ' · Mb/s';
  liveUpdate();
}
function liveUpdate() {
  const g = S.gen, e = g.last; if (!e || !$('#gTx')) return;
  if ($('#gRows')) { syncRows(rowsHtml(rowsView())); const nt = $('#gNote'); if (nt) nt.textContent = e.cleared_s != null ? `counters since clear at ${dur(e.cleared_s)}` : 'counters since the start'; }
  setStateBar(e.elapsed_s, e.total_s);
  if (e.steps || $('#gStepsLive').firstChild) Steps.live($('#gStepsLive'), e);
  const sm = e.sum || { tx_pkts: e.tx_pkts, rx_pkts: e.rx_pkts, lost: e.lost, rx_mbps: e.rx_mbps, loss_pct: 0 };
  $('#gTx').textContent = num(sm.tx_pkts); $('#gTxs').textContent = `${num(e.tx_pps)} /s`;
  $('#gRx').textContent = num(sm.rx_pkts); $('#gRxs').textContent = `${num(e.rx_pps)} /s`;
  $('#gLost').textContent = num(sm.lost); $('#gLost').parentElement.className = sm.lost ? 'bad' : ''; $('#gLosts').textContent = `${f2(sm.loss_pct)} %`;
  $('#gRate').textContent = f2(sm.rx_mbps);
  if (G.c1 && g.sr) { const n = G.nst; G.c1.setData([g.xs, ...Array.from({ length: n }, (_, k) => g.sr[k] || [])]); G.c2.setData([g.xs, ...Array.from({ length: n }, (_, k) => g.sl[k] || [])]); }
}

function resultHtml() {
  const d = S.live.done, s = d.summary, v = s && s.verdict;
  if (d.status === 'failed' || !v) return `<div class="banner fail"><div class="big">✕ COULD NOT RUN</div><div>${esc(d.error || 'The test failed.')}</div></div><div class="row" style="margin-bottom:16px"><button class="btn" data-act="again">Change settings</button></div>`;
  const t = s.totals;
  if (s.steps) return `<div class="banner ${v.pass ? 'pass' : 'fail'}"><div class="big">${v.pass ? '✓ PASS' : '✕ FAIL'}</div><div><b data-testid="gen-steps-headline">${esc(s.steps.headline)}</b>${d.status === 'stopped' ? ' <small>(stopped early)</small>' : ''}${v.reasons.length ? '<ul>' + v.reasons.map(r => `<li>${esc(r)}</li>`).join('') + '</ul>' : ''}</div></div>
  <div id="gStepsRes"></div>
  <div class="row" style="margin-bottom:16px"><button class="btn primary" data-act="results">See full results →</button><button class="btn" data-act="again">Run another</button></div>`;
  return `<div class="banner ${v.pass ? 'pass' : 'fail'}"><div class="big">${v.pass ? '✓ PASS' : '✕ FAIL'}</div><div><b>${v.pass ? 'The link carried this traffic within your limits.' : 'The link did not meet your limits.'}</b>${d.status === 'stopped' ? ' <small>(stopped early)</small>' : ''}${v.reasons.length ? '<ul>' + v.reasons.map(r => `<li>${esc(r)}</li>`).join('') + '</ul>' : ''}</div></div>
  <div class="grid g4" style="margin-bottom:12px"><div class="stat"><small>Sent</small><b>${t.tx_mbps}</b><span>Mb/s · ${num(t.tx_pkts)} packets</span></div><div class="stat"><small>Received</small><b>${t.rx_mbps}</b><span>Mb/s · ${num(t.rx_pkts)} packets</span></div><div class="stat ${t.lost ? 'bad' : 'good'}"><small>Lost</small><b>${num(t.lost)}</b><span>${pct(t.loss_pct)}</span></div><div class="stat"><small>Delay p99</small><b>${t.lat_ms ? ms(t.lat_ms.p99) : 'n/a'}</b><span>${t.lat_ms ? 'one-way' : 'clocks not synced'}</span></div></div>
  <div class="row" style="margin-bottom:16px"><button class="btn primary" data-act="results">See full results →</button><button class="btn" data-act="again">Run another</button></div>`;
}
