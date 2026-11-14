// Traffic generator tab: presets -> connection -> streams -> pass rules -> start; live charts; result card.
import { $, $$, esc, api, toast, confirmBox, promptBox, tip, ms, num, pct, dur, download, bindOnce, fitPlot, myName } from './ui.js';
import { S, onLive, resetLive } from './state.js';
import { EP, loadPeers, peerOf, optionsFor, valueOfEp, epFromValue, TYPED, addPiSheet, watchPeers } from './endpoints.js';
import { mountWifi } from './wifi.js';
import { Steps } from './steps.js';
import { markPlugin } from './results.js';

const TYPES = { udp: 'UDP', tcp: 'TCP', ip: 'Raw IP', l2: 'Raw Ethernet (Layer 2)' };
const TYPE_TIP = 'UDP / TCP are normal traffic. Raw IP sends your own IP protocol number. Raw Ethernet sends bare frames (needs both Pis on the same Layer-2 network).';
let G = { profiles: [], selId: null, p: null, conn: loadConn(), off: null, plots: [] };
let navigate = () => { };

function loadConn() { try { return { peer_api: '', peer_host: '', interface: 'auto', peer_interface: 'auto', local_host: '', peer_password: '', ...JSON.parse(localStorage.getItem('tt-gen-conn') || '{}') }; } catch { return { peer_api: '', peer_host: '', interface: 'auto', peer_interface: 'auto', local_host: '', peer_password: '' }; } }
const saveConn = () => localStorage.setItem('tt-gen-conn', JSON.stringify(G.conn));
const clone = o => JSON.parse(JSON.stringify(o));
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
    for (const [w, label] of [['src', 'From'], ['dst', 'To']]) if (valueOfEp(st[w], 'self') === TYPED && !String(st[w].host || '').trim()) return `Stream “${st.name}”: type the IP address for “${label}”, or pick a Pi from the list.`;
    if (endKey(st.src) === endKey(st.dst)) return `Stream “${st.name}” goes from ${endName(st.src)} to the same Pi. Pick two different Pis.`;
  }
  return '';
}
function endName(e) { const v = valueOfEp(e, 'self'); return v === 'self' ? (EP.me ? EP.me.name : myName()) : v === TYPED ? (e.host || 'the typed address') : ((peerOf(v) || {}).name || e.name || v); }
const newStream = n => ({ name: `Stream ${n}`, type: 'udp', direction: 'a_to_b', src: SELF(), dst: otherPi(), rate: { mbps: 10 }, size: { mode: 'fixed', size: 1000, min: 64, max: 1518 }, burst_packets: 1, burst_gap_ms: 0, duration_s: null, packets: null, start_offset_s: 0, rtt_probe_pps: 10, dscp: 0, ttl: 64, dst_port: null, dst_mac: '', src_mac: '', ethertype: 0x88B5, vlan: false, vlan_id: 0, vlan_prio: 0, ip_proto: 253, seed: 1 });

function effProfile() {
  const p = clone(G.p); delete p.id;
  const c = G.conn;
  p.net = { ...(p.net || {}), peer_host: c.peer_host };
  p.gen = { ...p.gen, peer_api: c.peer_api, interface: c.interface || 'auto', peer_interface: c.peer_interface || 'auto', local_host: c.local_host, peer_password: c.peer_password };
  for (const st of p.gen.streams) for (const w of ['src', 'dst']) if (st[w]) delete st[w].typed;      // the "typed address" marker is for this page only
  return p;
}

export async function renderGenerator(root, nav) {
  navigate = nav; if (G.off) G.off(); G.plots.forEach(p => p.destroy()); G.plots = [];
  G.profiles = (await api('/profiles')).filter(p => p.kind === 'generator');
  await loadPeers();
  if (!G.p && G.profiles.length) { G.selId = G.profiles[0].id; G.p = initEnds(clone(G.profiles[0])); }
  else if (G.p) initEnds(G.p);
  draw(root);
  watchPeers(root, () => { if (!isRunning() && !hasResult()) draw(root); });      // the list of Pis refreshes by itself while this page is open
  G.off = onLive(e => { if (e.ev === 'lamp') import('./run.js').then(m => m.lampLine()); if (['gstate', 'done', 'started'].includes(e.ev)) { if (e.ev !== 'gstate') draw(root); else liveUpdate(); } });
  bindOnce(root, 'click', ev => click(ev, root));
  bindOnce(root, 'change', ev => change(ev, root));
}

const isRunning = () => S.live.profile ? (S.live.running && S.live.profile.kind === 'generator') : (S.status.run.running && S.status.run.kind === 'generator');
const hasResult = () => S.live.done && S.live.profile && S.live.profile.kind === 'generator';

// Where this Pi's traffic goes first (for the Wi-Fi check): the first stream's far end that is not this Pi.
function genTarget() {
  if (!G.p) return '';
  for (const st of G.p.gen.streams) for (const e of [st.dst, st.src]) { const v = valueOfEp(e, 'self'); if (v === 'self') continue; if (v === TYPED) return (e.host || '').trim() || G.conn.peer_host; const pe = peerOf(v); if (pe) return pe.host; }
  return G.conn.peer_host || webHost(G.conn.peer_api);
}

function draw(root) {
  const run = isRunning(), res = hasResult() && !run;
  root.innerHTML = `
    <h1>Traffic generator</h1>
    <p class="muted" style="max-width:760px">Send test traffic from one Pi to another, or from several Pis to several, and see what the link does to it: how much gets through, what is lost, and how late and uneven the packets are. Every Pi on the network can take part; each stream says where it starts and where it ends.</p>
    <div id="liveBox">${run ? liveHtml() : res ? resultHtml() : ''}</div>
    ${run ? '' : editorHtml()}`;
  if (run) liveInit(); else mountWifi($('#gWifi', root), genTarget);
  if (!run && G.p && $('#gSteps', root)) Steps.form($('#gSteps', root), G.p.gen, { onChange: () => draw(root) });      // Throughput: Steps (find the clean rate) or Flood
  if (res && $('#gStepsRes', root)) Steps.result($('#gStepsRes', root), S.live.done.summary);
}

// ------------------------------------------------------------------ editor
function pcard(p) {
  const sel = p.id === G.selId;
  return `<div class="pcard ${sel ? 'sel' : ''}" data-act="pick" data-id="${p.id}" tabindex="0" role="button">
    <h3>${esc(p.name)} <span class="tag ${p.builtin ? 'preset' : ''}">${p.builtin ? 'Preset' : 'Saved'}</span></h3><p>${esc(p.description || '')}</p>
    <div class="meta">${p.gen.streams.length} stream${p.gen.streams.length > 1 ? 's' : ''} · ${Steps.isOn(p.gen) ? esc(Steps.summary(p.gen)) : dur(p.gen.duration_s)}</div>
    <div class="acts"><button class="btn small ghost" data-act="dup" data-id="${p.id}">Duplicate to edit</button><button class="btn small ghost" data-act="exp1" data-id="${p.id}">Export</button>${p.builtin ? '' : `<button class="btn small ghost" data-act="del" data-id="${p.id}">Delete</button>`}</div></div>`;
}

function field(label, inner, t = '') { return `<label class="f"><span>${label} ${t ? tip(t) : ''}</span>${inner}</label>`; }
const inp = (attrs, v, type = 'text', extra = '') => `<input type="${type}" ${attrs} value="${esc(v ?? '')}" ${extra}>`;

function editorHtml() {
  if (!G.p) return '<div class="card empty"><h2>No presets found</h2></div>';
  const c = G.conn, g = G.p.gen, r = g.rule;
  // Rarely needed link details stay folded unless one of them is in use.
  const notAuto = v => v && v !== 'auto';
  const connAdv = c.peer_api || c.peer_host || c.local_host || notAuto(c.interface) || notAuto(c.peer_interface) || c.peer_password;
  const sel = (attrs, opts, cur) => `<select ${attrs}>${opts.map(([v, l]) => `<option value="${esc(v)}" ${String(cur) === String(v) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
  return `
  <div class="card"><div class="row sp"><div><h2>1 · Pick a test</h2><p class="muted">Start from a preset, then change anything below.</p></div>
    <div class="row"><button class="btn small" data-act="imp">⤒ Import</button><input type="file" id="gImp" accept=".json,application/json" hidden></div></div>
    <div class="grid g3">${G.profiles.map(pcard).join('')}</div></div>

  <div class="card"><h2>2 · Which Pis?</h2><p class="muted">Each stream below picks where it starts (From) and where it ends (To). Pis on this network appear in the lists by themselves; a Pi that does not show up can be added by its IP address.</p>
    <div class="row epbar"><span class="muted" data-testid="gen-pis-count" role="status">${EP.loaded ? (EP.peers.length ? `${EP.peers.length} other Pi${EP.peers.length > 1 ? 's' : ''} on this network` : 'No other Pi found yet') : 'Looking for other Pis…'}</span>
      <button type="button" class="btn small" data-act="pis-refresh" data-testid="gen-refresh-pis">↻ Look again</button><button type="button" class="btn small" data-act="pis-add" data-testid="gen-add-pi">＋ Add a Pi by IP</button></div>
    <div id="gWifi" style="margin-top:10px"></div>
    <details id="gConnAdv" ${connAdv ? 'open' : ''} data-testid="gen-advanced"><summary>Advanced: data address, web address or password for a Pi that is not in the list</summary><div class="grid g3" style="margin-top:8px">
      ${field("Data address of a typed Pi (IP)", inp('data-conn="peer_host" placeholder="192.168.1.51"', c.peer_host), 'Where UDP, TCP and raw-IP packets are sent to a Pi you typed by address. Leave it empty to use the address you typed in the stream; fill it in only when the data takes another path (for example the one that goes through the radio).')}
      ${field("Web address of a typed Pi", inp('data-conn="peer_api" placeholder="192.168.1.51:8080"', c.peer_api), 'The address you would type in a browser to open that Pi\'s Traffic Tester. It is used to control the test, not to carry it. Leave it empty to use the address typed in the stream.')}
      ${field('Password of a typed Pi (if set)', inp('data-conn="peer_password" autocomplete="off"', c.peer_password, 'password'), 'Only if you protected that Pi with a password. For Pis in the list, enter the password in Settings. Kept on this Pi only, never saved in profiles.')}
      ${field("This Pi's data address (optional)", inp('data-conn="local_host" placeholder="auto"', c.local_host), 'Needed only for streams that end on this Pi and start on a Pi that cannot work out the way back. Blank = taken from this Pi\'s port.')}
      ${field('This Pi\'s network port', inp('data-conn="interface" placeholder="auto"', c.interface), 'Used by raw Ethernet streams. "auto" = the first wired port.')}
      ${field("Network port on the other Pis", inp('data-conn="peer_interface" placeholder="auto"', c.peer_interface), 'Same, on the other Pis.')}
    </div></details></div>

  <div class="card"><div class="row sp"><div><h2>3 · Traffic to send</h2><p class="muted">Each stream is its own flow. Add several to mix traffic.</p></div>
    <div class="row"><button class="btn small" data-act="addstream" data-testid="gen-add-stream">＋ Add stream</button></div></div>
    ${g.streams.map(streamHtml).join('')}</div>

  <div class="card"><h2>4 · How long, and when is it a PASS?</h2>
    <div id="gSteps" style="margin-bottom:12px"></div>
    <div class="grid g3">
      ${Steps.isOn(g) && !Steps.blocker(g) ? '' : field('Test length (seconds)', inp('data-g="duration_s"', g.duration_s, 'number', 'step="any" min="1"'))}
      ${field('Lost packets must be under (%)', inp('data-rule="max_loss_pct"', r.max_loss_pct, 'number', 'step="any" min="0" placeholder="no limit"'), 'loss')}
      ${field('Delay must be under (ms)', inp('data-rule="max_latency_ms"', r.max_latency_ms, 'number', 'step="any" min="0" placeholder="no limit"'), 'One-way delay of packets from sender to receiver. Needs both Pis\' clocks synced (the app checks).')}
      ${field('…measured at', sel('data-rule="latency_percentile" data-t="num"', [[50, 'Typical (p50)'], [95, 'p95'], [99, 'p99 (recommended)'], [100, 'Slowest (max)']], r.latency_percentile), 'p99')}
      ${field('Jitter must be under (ms)', inp('data-rule="max_jitter_ms"', r.max_jitter_ms, 'number', 'step="any" min="0" placeholder="no limit"'), 'jitter')}
      ${field('Throughput at least (Mb/s)', inp('data-rule="min_throughput_mbps"', r.min_throughput_mbps, 'number', 'step="any" min="0" placeholder="no limit"'), 'Total received rate across all streams.')}
    </div>
    <div class="row sp" style="margin-top:14px"><button class="btn" data-act="save">💾 Save as profile…</button><button class="btn primary" data-act="start">▶ Start traffic test</button></div></div>`;
}

function endPicker(s, i, w, label) {
  const ep = s[w] || blank(), cur = valueOfEp(ep, 'self'), n = `gen-stream-${i + 1}-${w === 'src' ? 'from' : 'to'}`;
  return `<div class="epcol"><label class="f"><span>${label}</span><select data-gp="${w}" data-i="${i}" data-testid="${n}">${optionsFor(cur, ep, { typed: true })}</select></label>
    ${cur === TYPED ? `<label class="f eptyped"><span>IP address or name of the Pi</span><input data-gh="${w}" data-i="${i}" data-testid="${n}-host" inputmode="decimal" autocomplete="off" placeholder="e.g. 192.168.1.50" value="${esc(ep.host || '')}"></label>` : ''}</div>`;
}
function streamHtml(s, i) {
  const rateUnit = s.rate.pps != null ? 'pps' : 'mbps';
  const rateVal = s.rate.pps ?? s.rate.mbps;
  const sel = (attrs, opts, cur) => `<select ${attrs}>${opts.map(([v, l]) => `<option value="${esc(v)}" ${String(cur) === String(v) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
  const d = `data-i="${i}"`;
  const bad = endsProblem({ gen: { streams: [s] } });
  return `<div class="card" style="background:var(--panel2);margin-bottom:12px" data-testid="gen-stream-${i + 1}"><div class="row sp"><div class="row">${inp(`${d} data-k="name" style="font-weight:700;min-width:200px;padding:8px 10px;border:1px solid var(--line);border-radius:10px;background:var(--panel)"`, s.name)}</div>
    <div class="row"><button class="btn small ghost" data-act="both" data-i="${i}" data-testid="gen-stream-${i + 1}-reverse" title="Add the same stream the other way round (To and From swapped)">⇄ Also reverse</button><button class="btn small ghost" data-act="rm" data-i="${i}" ${G.p.gen.streams.length < 2 ? 'disabled' : ''}>Remove</button></div></div>
    ${bad ? `<p class="why" style="color:var(--warn);font-weight:600;margin:8px 0 0" data-testid="gen-stream-${i + 1}-problem" role="alert">${esc(bad)}</p>` : ''}
    <div class="grid g4w" style="margin-top:8px">
      ${field('Type', sel(`${d} data-k="type"`, Object.entries(TYPES), s.type), TYPE_TIP)}
      ${endPicker(s, i, 'src', 'From')}${endPicker(s, i, 'dst', 'To')}
      ${field('Rate', `<div class="row" style="flex-wrap:nowrap">${inp(`${d} data-rate="val" style="min-width:90px;flex:1"`, rateVal, 'number', 'step="any" min="0"')}${sel(`${d} data-rate="unit" style="width:112px;flex:none"`, [['mbps', 'Mb/s'], ['pps', 'packets/s']], rateUnit)}</div>`, 'How fast to send. Mb/s counts whole Ethernet frames (without the 4-byte check at the end).')}
      ${field('Packet size', sel(`${d} data-k="size.mode"`, [['fixed', 'Fixed'], ['range', 'Random range'], ['imix', 'IMIX (mixed)']], s.size.mode), 'IMIX = 7 small (64 B), 4 medium (594 B), 1 large (1,514 B) frames: a typical internet mix. Sizes are counted without the 4-byte Ethernet check, so 1,514 B is the largest frame a standard 1,500-byte MTU link carries.')}
      ${s.size.mode === 'fixed' ? field('Size (bytes)', inp(`${d} data-k="size.size" data-t="num"`, s.size.size, 'number', 'min="24" max="9000"'), 'Whole frame size in bytes. The smallest possible depends on the type (raw 60, UDP 66).') : ''}
      ${s.size.mode === 'range' ? field('Smallest (bytes)', inp(`${d} data-k="size.min" data-t="num"`, s.size.min, 'number')) + field('Largest (bytes)', inp(`${d} data-k="size.max" data-t="num"`, s.size.max, 'number')) : ''}
    </div>
    <details ${s.burst_packets > 1 || s.burst_gap_ms > 0 || s.duration_s || s.packets || s.start_offset_s > 0 || s.vlan ? 'open' : ''}><summary>Advanced (bursts, timing, priority, addresses)</summary><div class="grid g4w" style="margin-top:8px">
      ${field('Packets per burst', inp(`${d} data-k="burst_packets" data-t="num"`, s.burst_packets, 'number', 'min="1"'), 'Packets sent back to back in one go. 1 = evenly spaced.')}
      ${field('Gap between bursts (ms)', inp(`${d} data-k="burst_gap_ms" data-t="num"`, s.burst_gap_ms, 'number', 'min="0" step="any"'), '0 = keep the rate steady. More than 0 = send each burst at full speed, then wait this long.')}
      ${field('This stream runs for (seconds)', inp(`${d} data-k="duration_s" data-t="nul"`, s.duration_s, 'number', 'min="1" step="any" placeholder="whole test"'))}
      ${field('Or stop after (packets)', inp(`${d} data-k="packets" data-t="nul"`, s.packets, 'number', 'min="1" placeholder="no limit"'))}
      ${field('Start after (seconds)', inp(`${d} data-k="start_offset_s" data-t="num"`, s.start_offset_s, 'number', 'min="0" step="any"'))}
      ${field('Round-trip probes per second', inp(`${d} data-k="rtt_probe_pps" data-t="num"`, s.rtt_probe_pps, 'number', 'min="0"'), 'A few packets per second are echoed back to measure round-trip time. 0 = off. (Not for TCP.)')}
      ${s.type !== 'l2' ? field('Priority (DSCP 0-63)', inp(`${d} data-k="dscp" data-t="num"`, s.dscp, 'number', 'min="0" max="63"'), 'dscp') + field('TTL', inp(`${d} data-k="ttl" data-t="num"`, s.ttl, 'number', 'min="1" max="255"')) : ''}
      ${s.type === 'udp' || s.type === 'tcp' ? field('Port', inp(`${d} data-k="dst_port" data-t="nul"`, s.dst_port, 'number', 'placeholder="automatic"')) : ''}
      ${s.type === 'ip' ? field('IP protocol number', inp(`${d} data-k="ip_proto" data-t="num"`, s.ip_proto, 'number', 'min="0" max="255"')) : ''}
      ${s.type === 'l2' ? field('Destination MAC', inp(`${d} data-k="dst_mac" placeholder="other Pi (automatic)"`, s.dst_mac)) + field('EtherType (decimal)', inp(`${d} data-k="ethertype" data-t="num"`, s.ethertype, 'number'), '34997 = 0x88B5, an Ethernet type reserved for local experiments.') +
        `<label class="chk"><input type="checkbox" ${d} data-k="vlan" ${s.vlan ? 'checked' : ''}> VLAN tag</label>` + field('VLAN id', inp(`${d} data-k="vlan_id" data-t="num"`, s.vlan_id, 'number', 'min="0" max="4094"')) + field('VLAN priority (0-7)', inp(`${d} data-k="vlan_prio" data-t="num"`, s.vlan_prio, 'number', 'min="0" max="7"')) : ''}
    </div></details></div>`;
}

// ------------------------------------------------------------------ events
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
  if (el.dataset.g) { G.p.gen[el.dataset.g] = Number(el.value); return; }
  if (el.dataset.rule) { const k = el.dataset.rule; G.p.gen.rule[k] = el.dataset.t === 'num' ? Number(el.value) : (el.value === '' ? null : Number(el.value)); return; }
  if (el.dataset.i !== undefined) {
    const s = G.p.gen.streams[+el.dataset.i];
    if (el.dataset.rate) {
      const row = el.closest('.row'); const v = Number(row.querySelector('input').value), u = row.querySelector('select').value;
      s.rate = u === 'pps' ? { pps: v } : { mbps: v }; return;
    }
    const k = el.dataset.k; let v = el.type === 'checkbox' ? el.checked : el.value;
    if (el.dataset.t === 'num') v = Number(v); if (el.dataset.t === 'nul') v = v === '' ? null : Number(v);
    setp(s, k, v);
    if (k === 'type' || k === 'size.mode') { if (k === 'type' && v === 'l2') s.size.size = Math.max(s.size.size, 64); draw(root); }
  }
}

async function click(ev, root) {
  const a = ev.target.closest('[data-act]'); if (!a) return;
  const act = a.dataset.act, id = +a.dataset.id, i = a.dataset.i !== undefined ? +a.dataset.i : null;
  const reload = async () => { G.profiles = (await api('/profiles')).filter(p => p.kind === 'generator'); draw(root); };
  try {
    if (act === 'pick') { const p = G.profiles.find(x => x.id === id); G.selId = id; G.p = initEnds(clone(p)); draw(root); }
    else if (act === 'pis-refresh') { a.disabled = true; a.textContent = 'Looking…'; await loadPeers(true); draw(root); }
    else if (act === 'pis-add') { const pe = await addPiSheet(); if (pe) { await loadPeers(); draw(root); toast(`Added ${pe.name}.`); } }
    else if (act === 'dup') { const p = await api(`/profiles/${id}/duplicate`, { method: 'POST' }); G.selId = p.id; G.p = initEnds(clone(p)); toast(`Created “${p.name}”. It's yours to edit.`); await reload(); }
    else if (act === 'del') { if (await confirmBox('Delete this profile?', 'Past results are kept.', 'Delete', true)) { await api(`/profiles/${id}`, { method: 'DELETE' }); if (G.selId === id) { G.selId = null; G.p = null; } await renderGenerator(root, navigate); } }
    else if (act === 'exp1') { const r = await fetch(`/api/profiles/export?ids=${id}`); download(`traffic-generator-profile-${id}.json`, await r.text()); }
    else if (act === 'imp') $('#gImp', root).click();
    else if (act === 'addstream') { G.p.gen.streams.push(newStream(G.p.gen.streams.length + 1)); draw(root); }
    else if (act === 'rm') { G.p.gen.streams.splice(i, 1); draw(root); }
    else if (act === 'both') { const s = clone(G.p.gen.streams[i]); s.direction = s.direction === 'a_to_b' ? 'b_to_a' : 'a_to_b'; [s.src, s.dst] = [s.dst, s.src]; s.name = s.name + ' (reverse)'; G.p.gen.streams.splice(i + 1, 0, s); draw(root); }
    else if (act === 'save') {
      const name = await promptBox('Save as profile', 'Name for this traffic test', G.p.builtin ? `${G.p.name} (mine)` : G.p.name); if (!name) return;
      const p = effProfile(); p.gen.peer_password = ''; p.name = name; p.builtin = false;
      const cur = G.profiles.find(x => x.id === G.selId && !x.builtin && x.name === name);
      const r = cur ? await api(`/profiles/${cur.id}`, { method: 'PUT', body: p }) : await api('/profiles', { method: 'POST', body: p });
      G.selId = r.id; G.p = initEnds(clone(r)); toast('Profile saved.'); await reload();
    }
    else if (act === 'start') {
      const problem = endsProblem(G.p); if (problem) return toast(problem, true, 8000);
      const p = effProfile(); a.disabled = true;
      try { const r = await api('/runs', { method: 'POST', body: { profile: p } }); resetLive(p, r.run_id); S.status = await api('/status'); draw(root); }
      catch (e) { toast(e.message, true, 9000); a.disabled = false; }
    }
    else if (act === 'mark') { (await import('./run.js')).dropMark(); }
    else if (act === 'stop') { if (await confirmBox('Stop the test?', 'Results so far are kept and judged.', 'Stop', true)) await api(`/runs/${S.live.runId}/stop`, { method: 'POST' }); }
    else if (act === 'again') { S.live.done = null; draw(root); }
    else if (act === 'results') { S.openRun = S.live.runId; navigate('results'); }
  } catch (e) { toast(e.message, true, 8000); }
}

// ------------------------------------------------------------------ live view
const stat = (id, label, val, sub, t = '') => `<div class="stat"><small>${label} ${t ? tip(t) : ''}</small><b id="${id}">${val}</b><span id="${id}s">${sub}</span></div>`;
function liveHtml() {
  return `<div class="banner run"><div class="big">● RUNNING</div><div><b>Traffic test in progress</b> — you can leave this page; it keeps going.</div></div><div id="lampLine" data-testid="run-lamp"></div>
  <div class="card"><div class="row sp"><div><b id="gPhase">Starting…</b><div class="muted" id="gTime"></div></div><div class="row"><button class="btn mark" data-act="mark" data-testid="run-mark" title="Drop a flag now (e.g. antenna moved)">⚑ Mark</button><button class="btn danger" data-act="stop">■ Stop test</button></div></div><div class="bar" style="margin-top:12px"><div id="gBar" style="width:0%"></div></div></div>
  <div id="gStepsLive" data-testid="gen-steps-live"></div>
  <div class="grid g4" style="margin-bottom:16px">${stat('gTx', 'Sending', '–', 'Mb/s')}${stat('gRx', 'Receiving', '–', 'Mb/s')}${stat('gLost', 'Lost', '–', 'packets', 'loss')}${stat('gLat', 'Delay now', '–', 'ms one-way', 'The average one-way delay of packets in the last second.')}${stat('gJit', 'Jitter', '–', 'ms', 'jitter')}</div>
  <div class="card"><h2>Throughput</h2><div id="gC1" class="chart"></div></div>
  <div class="card"><h2>Delay and jitter</h2><div id="gC2" class="chart"></div></div>`;
}
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function liveInit() {
  if (!window.uPlot) return;
  const axis = { stroke: css('--muted'), grid: { stroke: css('--line'), width: 1 }, ticks: { stroke: css('--line') } };
  const mk = (el, series, label) => { const w = Math.max(200, el.clientWidth); const p = new uPlot({ plugins: [markPlugin(() => S.live.marks)], width: w, height: 240, padding: [8, 8, 0, 0], scales: { x: { time: false }, y: { range: (u, a, b) => [0, Math.max(1, (b || 0) * 1.15)] } }, axes: [{ ...axis, label: 'seconds' }, { ...axis, label, size: 56 }], series: [{ label: 'Time' }, ...series] }, [[], ...series.map(() => [])], el); fitPlot(p, el); G.plots.push(p); return p; };
  G.c1 = mk($('#gC1'), [{ label: 'Sent', stroke: css('--muted'), width: 1.6, points: { show: false } }, { label: 'Received', stroke: css('--accent'), width: 1.8, points: { show: false } }], 'Mb/s');
  G.c2 = mk($('#gC2'), [{ label: 'Delay (avg)', stroke: css('--accent'), width: 1.6, points: { show: false } }, { label: 'Jitter', stroke: css('--warn'), width: 1.6, points: { show: false } }], 'ms');
  liveUpdate();
}
function liveUpdate() {
  const g = S.gen, e = g.last; if (!e || !$('#gTx')) return;
  $('#gPhase').textContent = Steps.progressText(e) || 'Sending test traffic';
  if (e.steps || $('#gStepsLive').firstChild) Steps.live($('#gStepsLive'), e); $('#gTime').textContent = `${dur(e.elapsed_s)} of about ${dur(e.total_s)}`;
  $('#gBar').style.width = Math.min(100, e.elapsed_s / Math.max(1, e.total_s) * 100) + '%';
  $('#gTx').textContent = e.tx_mbps; $('#gTxs').textContent = `Mb/s · ${num(e.tx_pps)} packets/s`;
  $('#gRx').textContent = e.rx_mbps; $('#gRxs').textContent = `Mb/s · ${num(e.rx_pps)} packets/s`;
  $('#gLost').textContent = num(e.lost); $('#gLost').parentElement.className = 'stat ' + (e.lost ? 'bad' : 'good'); $('#gLosts').textContent = `of ${num(e.tx_pkts)} sent`;
  $('#gLat').textContent = e.lat_ms ?? '–'; $('#gJit').textContent = e.jitter_ms ?? '–';
  if (G.c1) { G.c1.setData([g.xs, g.tx, g.rx]); G.c2.setData([g.xs, g.lat, g.jit]); }
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
