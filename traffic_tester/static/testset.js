// Utility test sets (pillar 1): a path, the applications that share one link, a target for each, up to three steps (alone, together, plus fill),
// one result and one report. Setup, run and result are ONE screen in three states: the table of applications keeps its place and shape.
import { $, $$, esc, api, toast, confirmBox, promptBox, sheetBox, num, dur, bindOnce, myName, tip } from './ui.js';
import { S, onLive, resetLive, isTestset } from './state.js';
import { EP, loadPeers, peerOf, optionsFor, valueOfEp, epFromValue, TYPED } from './endpoints.js';
import { Job } from './job.js';
import { wifiCheck, turnOff } from './wifi.js';
import { readyHtml } from './home.js';
import { genConn, defaultEnds } from './generator.js';
import { tsResultHtml } from './tsresult.js';

export { isTestset };
// is the run in front of us (running, or just finished and not cleared) a test set?
export const tsRun = () => { const p = S.live.profile; if (p) return isTestset(p); return !!(S.status && S.status.run && S.status.run.running && S.status.run.testset); };

const clone = o => JSON.parse(JSON.stringify(o));
const f2 = v => v == null ? '–' : Number(v).toFixed(2);
const g3 = v => v == null ? '–' : String(+Number(v).toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2));
const dash = '<span class="mute">–</span>';
const STEP_NAMES = { alone: 'Alone', together: 'Together', fill: 'Plus fill' };
const TYPES = { udp: 'UDP', tcp: 'TCP', ip: 'IP', l2: 'Ethernet' };
const T = { p: null, id: null, from: null, to: null, pathOpen: false, off: null, nav: () => { }, lib: null, flash: null, done: null };

// ---------------------------------------------------------------- numbers about a stream
const meanSize = s => s.size.mode === 'fixed' ? s.size.size : s.size.mode === 'range' ? (s.size.min + s.size.max) / 2 : 361.5;
const ppsOf = s => { const base = s.rate.pps != null ? s.rate.pps : s.rate.mbps * 1e6 / 8 / Math.max(1, meanSize(s)); return s.burst_gap_ms > 0 ? s.burst_packets / (s.burst_gap_ms / 1000) : base; };
const mbpsOf = s => s.burst_gap_ms > 0 || s.rate.pps != null ? ppsOf(s) * meanSize(s) * 8 / 1e6 : s.rate.mbps;
const rateText = s => s.burst_gap_ms > 0 ? `${s.burst_packets} pkt / ${+(s.burst_gap_ms / 1000).toFixed(2)} s` : s.rate.pps != null ? `${g3(s.rate.pps)} pkt/s` : `${g3(s.rate.mbps)} Mb/s`;
const sizeText = s => s.size.mode === 'fixed' ? `${s.size.size} B` : s.size.mode === 'range' ? `${s.size.min}–${s.size.max} B` : 'IMIX';
const subLine = s => `${TYPES[s.type] || s.type} · ${rateText(s)}`;
const pKey = t => !t || t.latency_percentile === 100 ? 'max' : 'p' + (t.latency_percentile || 99);
const tgtOf = (s, p) => s.target || { ...(p.gen.rule || {}), latency_percentile: (p.gen.rule || {}).latency_percentile || 99 };
const aloneNow = () => { const g = S.gen.last, t = g && g.testset; return !t || t.kind === 'alone'; };
const lvl = (v, lim) => v == null || lim == null || lim === '' ? '' : v > lim ? ' bad' : v > lim * 0.7 ? ' amb' : '';

// ---------------------------------------------------------------- the profile this page runs
function endsOf(p) {
  const s = p.gen.streams.find(x => x.src && x.dst && (x.src.pi || x.src.host) && (x.dst.pi || x.dst.host));
  if (s) return [s.src, s.dst];
  return defaultEnds();
}
const epName = e => { const v = valueOfEp(e, 'self'); return v === 'self' ? (EP.me ? EP.me.name : myName()) : v === TYPED ? (e.host || 'a typed address') : ((peerOf(v) || {}).name || e.name || v); };
function problem() {
  if (!T.p.gen.streams.some(s => s.enabled !== false)) return 'Switch at least one application on.';
  for (const [e, w] of [[T.from, 'From'], [T.to, 'To']]) if (valueOfEp(e, 'self') === TYPED && !String(e.host || '').trim()) return `Type the IP address for “${w}”, or pick a Pi from the list.`;
  const k = e => { const v = valueOfEp(e, 'self'); return v === TYPED ? 'addr:' + String(e.host || '').trim().toLowerCase() : v; };
  if (k(T.from) === k(T.to)) return 'From and To are the same Pi. Pick two different Pis.';
  return '';
}
function effProfile() {
  const p = clone(T.p); delete p.id; const c = genConn();
  p.net = { ...(p.net || {}), peer_host: c.peer_host };
  p.gen = { ...p.gen, peer_api: c.peer_api, interface: c.interface || 'auto', peer_interface: c.peer_interface || 'auto', local_host: c.local_host, peer_password: c.peer_password };
  for (const s of p.gen.streams) { s.src = clone(T.from); s.dst = clone(T.to); delete s.src.typed; delete s.dst.typed; }
  return p;
}
const offered = () => {
  const on = T.p.gen.streams.filter(s => s.enabled !== false), mb = on.reduce((a, s) => a + mbpsOf(s), 0), pps = on.reduce((a, s) => a + ppsOf(s), 0), ts = T.p.gen.testset;
  const secs = (ts.alone && on.length > 1 ? on.length * ts.alone_s : 0) + T.p.gen.duration_s + (ts.fill ? ts.fill_s : 0);
  const mbytes = (ts.alone && on.length > 1 ? on.reduce((a, s) => a + mbpsOf(s) * ts.alone_s, 0) : 0) / 8 + mb * T.p.gen.duration_s / 8 + (ts.fill ? (mb + ts.fill_mbps) * ts.fill_s / 8 : 0);
  return { mb, pps, secs, mbytes };
};
const bytesText = mb => mb >= 1000 ? `${(mb / 1000).toFixed(1)} GB` : `${Math.round(mb)} MB`;
const lenName = s => s % 3600 === 0 && s <= 48 * 3600 ? `${s / 3600} h` : s % 86400 === 0 ? `${s / 86400} days` : s % 60 === 0 && s < 3600 ? `${s / 60} min` : s % 60 === 0 ? `${Math.round(s / 60)} min` : `${s} s`;

// ---------------------------------------------------------------- the page
export async function renderTestset(root, nav, { id } = {}) {
  T.nav = nav; if (T.off) T.off();
  await loadPeers();
  const run = S.status && S.status.run && S.status.run.running, done = !run && S.live.done && S.showDone && S.live.profile && isTestset(S.live.profile);
  if ((run || done) && !S.live.profile && S.live.runId) { const r = await api(`/runs/${S.live.runId}`).catch(() => null); if (r) S.live.profile = r.profile; }
  if (id != null) { const q = S.profiles.find(x => x.id === id); if (!q) return nav('tests'); T.id = id; T.p = clone(q); [T.from, T.to] = endsOf(T.p); T.done = null; }
  if (!T.p && S.live.profile) { T.p = clone(S.live.profile); [T.from, T.to] = endsOf(T.p); }
  if (view() !== 'run') S.wizard.nt = { id: T.id, mode: 'ts' };
  draw(root);
  T.off = onLive(e => {
    if (!root.isConnected) return;
    if (e.ev === 'gstate') liveUpdate(root);
    else if (e.ev === 'done' || e.ev === 'started') draw(root);
    else if (e.ev === 'lamp') import('./run.js').then(m => m.lampLine());
  });
  bindOnce(root, 'click', ev => click(ev, root));
  bindOnce(root, 'change', ev => change(ev, root));
  bindOnce(root, 'input', ev => input(ev, root));
}
const view = () => S.live.running || (S.status && S.status.run && S.status.run.running) ? 'run' : S.live.done && S.showDone && S.live.profile && isTestset(S.live.profile) ? 'result' : 'setup';

function draw(root) {
  const v = view(), p = v === 'setup' ? T.p : (S.live.profile || T.p); if (!p) { root.innerHTML = '<div class="card empty"><p>Pick a test set on Tests.</p></div>'; return; }
  const ts = p.gen.testset;
  const head = `<div class="tshead"><h1 data-testid="ts-name">${esc(p.name)}</h1>${v === 'setup' ? `<p class="sub">${esc(p.description || '')}</p>` : ''}</div>`;
  if (v === 'setup') {
    const lens = (ts.lengths || []).slice(), cur = T.p.gen.duration_s;
    root.innerHTML = `<div class="tsp" data-testid="ts-page" data-state="setup">${head}
      <div class="tslen"><div class="seg lenseg" role="radiogroup" aria-label="Length of the all-together step">${lens.map(s => `<button type="button" role="radio" aria-checked="${s === cur}" class="${s === cur ? 'on' : ''}" data-ts="len" data-v="${s}" data-testid="ts-len-${s}">${lenName(s)}</button>`).join('')}<button type="button" role="radio" aria-checked="${!lens.includes(cur)}" class="${lens.includes(cur) ? '' : 'on'}" data-ts="len-custom" data-testid="ts-len-custom">${lens.includes(cur) ? 'Other…' : lenName(cur)}</button></div>
        <span class="tsgap"></span><button class="btn" data-ts="more" aria-label="More" data-testid="ts-more">⋯</button></div>
      ${pathHtml()}${appsHtml(p, v, null)}${stepsHtml()}${readyPanel()}
      <div class="row tsjob"><div id="tsJob"></div></div>
      <button type="button" class="btn primary tb-src" data-nt="start" data-testid="ts-start" title="Run this test now" ${problem() ? 'disabled' : ''}>▶ Run test</button></div>`;
    Job.line($('#tsJob', root));
    paintReady(root);
  } else if (v === 'run') {
    root.innerHTML = `<div class="tsp" data-testid="ts-page" data-state="run">${head}<div id="tsLive"></div>
      <div id="lampLine" data-testid="run-lamp"></div><p class="muted tsnote">Switch an application off or on with its switch during the “all together” step. Stop to change anything else.</p></div>`;
    liveUpdate(root);
  } else {
    const d = S.live.done, sum = d.summary;
    root.innerHTML = `<div class="tsp" data-testid="ts-page" data-state="result">${d.status === 'failed' || !sum || !sum.verdict ? `<div class="banner fail"><div class="big">✕ COULD NOT RUN</div><div>${esc(d.error || 'The test failed.')}</div></div>
        <div class="row"><button class="btn" data-ts="again" data-testid="ts-again">Change settings</button></div>`
      : `<div id="tsRes"></div>`}
      <button type="button" class="btn primary tb-src" data-nt="start" data-testid="ts-start">▶ Run again</button></div>`;
    if ($('#tsRes', root)) fillResult(root, sum, d);
  }
}

// ---------------------------------------------------------------- setup pieces
function pathHtml() {
  const a = epName(T.from), b = epName(T.to), sel = (w, e, label) => `<div class="epcol"><label class="f"><span>${label}</span><select data-ts-end="${w}" data-testid="ts-${w === 'src' ? 'from' : 'to'}">${optionsFor(valueOfEp(e, 'self'), e, { typed: true })}</select></label>
    ${valueOfEp(e, 'self') === TYPED ? `<label class="f eptyped"><span>IP address or name of the Pi</span><input data-ts-host="${w}" data-testid="ts-${w === 'src' ? 'from' : 'to'}-host" inputmode="decimal" autocomplete="off" placeholder="e.g. 192.168.1.50" value="${esc(e.host || '')}"></label>` : ''}</div>`;
  return `<section class="panel tspath" data-testid="ts-path"><header><span>Path</span><button type="button" class="linkbtn" data-ts="path" data-testid="ts-path-change">${T.pathOpen ? 'Done' : 'Change'}</button></header>
    ${T.pathOpen ? `<div class="pbody tsends">${sel('src', T.from, 'From')}<button type="button" class="btn swap" data-ts="swap" aria-label="Swap From and To" title="Swap" data-testid="ts-swap">⇄</button>${sel('dst', T.to, 'To')}</div>`
      : `<div class="pbody tspathline"><b>${esc(a)}</b><span class="tsarr">⇄</span><b>${esc(b)}</b></div>`}</section>`;
}
function targetCell(s, k, unit, i, p) {
  const t = tgtOf(s, p), v = t[k];
  return `<td class="n tgtc"><span class="tgtw"><input class="tgt" type="number" min="0" step="any" inputmode="decimal" data-ts-tg="${k}" data-i="${i}" value="${v == null ? '' : v}" placeholder="–" aria-label="${esc(s.name)} ${k.replace(/_/g, ' ')}" data-testid="ts-tg-${k}-${i + 1}"><i>${unit}</i></span>${k === 'max_latency_ms' && v != null ? `<small class="pk">${pKey(t)}</small>` : ''}</td>`;
}
// The applications table, in its three states. cur = the numbers of the moment (run) or the final ones (result).
function appsHtml(p, v, cur) {
  const sts = p.gen.streams, on = i => v === 'run' ? sts[i].enabled !== false && !(cur && cur[i] && cur[i].on === false) : sts[i].enabled !== false;
  const ex = sts.some(s => s.target && s.target.example);
  const off = offered(), setup = v === 'setup';
  const head = setup ? `<th class="tg">On</th><th>Application</th><th class="r">Latency ≤</th><th class="r">Loss ≤</th><th class="r t2">Jitter ≤</th><th class="r t2">Min Mb/s</th>`
    : `<th class="tg">On</th><th>Application</th><th class="r">Latency ms</th><th class="r">Loss %</th><th class="r t2">Jitter ms</th><th class="r t2">Mb/s</th><th class="r sth">State</th>`;
  const rows = sts.map((s, i) => {
    const t = tgtOf(s, p), n = !setup && cur ? cur[i] : null, isOn = on(i);
    if (setup) return `<tr class="${isOn ? '' : 'off'}" data-testid="ts-row-${i + 1}"><td class="tg"><button type="button" class="swb ${isOn ? 'on' : ''}" role="switch" aria-checked="${isOn}" aria-label="${esc(s.name)} on" data-ts="tgl" data-i="${i}" data-testid="ts-on-${i + 1}"><i></i></button></td>
      <td class="nm"><button type="button" class="appname" data-ts="edit" data-i="${i}" data-testid="ts-app-${i + 1}"><b>${esc(s.name)}</b><small>${esc(subLine(s))}</small></button></td>
      ${targetCell(s, 'max_latency_ms', 'ms', i, p)}${targetCell(s, 'max_loss_pct', '%', i, p)}${targetCell(s, 'max_jitter_ms', 'ms', i, p).replace('class="n tgtc"', 'class="n tgtc t2"')}${targetCell(s, 'min_throughput_mbps', 'Mb/s', i, p).replace('class="n tgtc"', 'class="n tgtc t2"')}</tr>`;
    const has = n && n.has && isOn, bl = has ? lvl(n.loss, t.max_loss_pct) : '', bt = has ? lvl(n.lat, t.max_latency_ms) : '', bj = has ? lvl(n.jit, t.max_jitter_ms) : '';
    const state = !has ? '' : (bl.includes('bad') || bt.includes('bad') || bj.includes('bad') || (t.min_throughput_mbps && v === 'result' && n.mbps < t.min_throughput_mbps)) ? 'bad' : (bl || bt || bj) ? 'amb' : 'ok';
    return `<tr class="${isOn ? '' : 'off'} ${state === 'bad' ? 'bad' : state === 'amb' ? 'amb' : ''}" data-testid="ts-row-${i + 1}"><td class="tg"><button type="button" class="swb ${isOn ? 'on' : ''}" role="switch" aria-checked="${isOn}" aria-label="${esc(s.name)} on" data-ts="tgl" data-i="${i}" data-testid="ts-on-${i + 1}" ${v === 'run' && !aloneNow() && sts[i].enabled !== false ? '' : 'disabled'}><i></i></button></td>
      <td class="nm"><b>${esc(s.name)}</b><small>${esc(subLine(s))}${n && n.step ? ' · ' + esc(n.step) : ''}</small>${state ? `<em class="stw ${state}">${state === 'bad' ? 'FAILING' : state === 'amb' ? 'CLOSE' : 'PASSING'}</em>` : ''}</td>
      <td class="n${bt}">${has && n.lat != null ? Math.round(n.lat * 10) / 10 : dash}${t.max_latency_ms != null ? `<small>≤ ${g3(t.max_latency_ms)} ms · ${pKey(t)}</small>` : ''}</td>
      <td class="n${bl}">${has ? f2(n.loss) : dash}${t.max_loss_pct != null ? `<small>≤ ${g3(t.max_loss_pct)} %</small>` : ''}</td>
      <td class="n t2${bj}">${has && n.jit != null ? n.jit.toFixed(1) : dash}${t.max_jitter_ms != null ? `<small>≤ ${g3(t.max_jitter_ms)} ms</small>` : ''}</td>
      <td class="n t2">${has ? f2(n.mbps) : dash}${t.min_throughput_mbps ? `<small>≥ ${g3(t.min_throughput_mbps)}</small>` : ''}</td>
      <td class="n stc">${state ? `<span class="vt ${state === 'bad' ? 'fail' : state === 'amb' ? 'warn' : 'pass'}" data-testid="ts-state-${i + 1}">${state === 'bad' ? 'FAILING' : state === 'amb' ? 'CLOSE' : 'PASSING'}</span>` : ''}</td></tr>`;
  }).join('');
  const k = sts.filter((s, i) => on(i)).length;
  const sum = setup ? `<tr class="sum"><td></td><td class="nm"><b>Offered load</b></td><td class="n" colspan="2"><b>${g3(off.mb)} Mb/s · ${Math.round(off.pps)} pkt/s</b><small>≈ ${bytesText(off.mbytes)} over ${dur(off.secs)}</small></td><td class="t2"></td><td class="t2"></td></tr>`
    : `<tr class="sum"><td></td><td class="nm"><b>${k} application${k === 1 ? '' : 's'} on</b></td><td class="n"></td><td class="n"></td><td class="n t2"></td><td class="n t2"></td><td></td></tr>`;
  const note = setup ? (ex ? 'targets are examples: edit any' : 'tap a name to edit') : v === 'run' ? 'live, against each target' : 'final, together step';
  return `<section class="panel tsapps" data-testid="ts-apps"><header><span>Applications on the link</span>${setup ? '<button type="button" class="linkbtn" data-ts="add" data-testid="ts-add">＋ Add application</button>' : `<small>${note}</small>`}</header>
    <div class="gscroll"><table class="ptable gtab tstab"><thead><tr>${head}</tr></thead><tbody id="tsRows">${rows}${sum}</tbody></table></div>
    ${setup ? `<div class="tsnote2 muted">${note}${ex ? ' · each row shows its own “example target” until you change it' : ''}</div>` : ''}</section>`;
}
function stepsHtml() {
  const ts = T.p.gen.testset, many = T.p.gen.streams.filter(s => s.enabled !== false).length > 1, a = ts.alone && many;
  return `<div class="steps3" data-testid="ts-steps">
    <div class="st3 ${a ? 'on' : 'offs'}"><div class="st3b"><button type="button" class="st3t" data-ts="alone" aria-pressed="${ts.alone}" data-testid="ts-step-alone"><b>1 · Alone</b></button>
      ${a ? `<button type="button" class="st3s" data-ts="alone-s" data-testid="ts-alone-s">${ts.alone_s} s each</button>` : `<small>${many ? 'off' : 'one application'}</small>`}</div></div>
    <div class="st3 on judged"><div class="st3b"><b>2 · Together</b><small>${lenName(T.p.gen.duration_s)} · judged</small></div></div>
    <div class="st3 ${ts.fill ? 'on' : 'offs'}"><div class="st3b"><button type="button" class="st3t" data-ts="fill" aria-pressed="${ts.fill}" data-testid="ts-step-fill"><b>3 · Plus fill</b></button>
      ${ts.fill ? `<button type="button" class="st3s" data-ts="fill-s" data-testid="ts-fill-s">${g3(ts.fill_mbps)} Mb/s · ${ts.fill_s} s</button>` : '<small>optional · off</small>'}</div></div></div>`;
}
async function stepSheet(kind, root) {
  const ts = T.p.gen.testset, f = (k, l, v, u, mn) => `<label class="f"><span>${l}</span><input type="number" min="${mn}" step="any" inputmode="decimal" data-k="${k}" value="${v}" data-testid="ts-sheet-${k}"> <i class="muted">${u}</i></label>`;
  const html = kind === 'alone' ? f('alone_s', 'Each application runs alone for', ts.alone_s, 's', 5) : f('fill_mbps', 'Fill traffic', ts.fill_mbps, 'Mb/s', 0.1) + f('fill_s', 'Step 3 lasts', ts.fill_s, 's', 5);
  const pr = sheetBox(kind === 'alone' ? 'Step 1 · Alone' : 'Step 3 · Plus fill', html, [{ id: 'ok', label: 'Done', cls: 'primary' }], 'ts-step-sheet');
  $$('#modal [data-k]').forEach(el => { el.onchange = el.oninput = () => { const n = Number(el.value); if (n > 0) ts[el.dataset.k] = el.dataset.k === 'alone_s' ? Math.min(300, Math.max(5, n)) : n; }; });
  await pr; draw(root);
}
const readyPanel = () => `<section class="panel tsready" data-testid="ts-ready"><header><span>Ready</span></header><div class="pbody"><div class="ready" id="tsReady">checking…</div><p class="why bad" id="startHint" ${problem() ? '' : 'hidden'}>${esc(problem())}</p></div></section>`;
async function paintReady(root) {
  const box = $('#tsReady', root); if (!box) return;
  const peers = EP.peers || [], far = peerOf(valueOfEp(T.to, 'self')) || peerOf(valueOfEp(T.from, 'self')) || peers[0], host = far && far.host;
  let wifi = null; try { wifi = host ? await wifiCheck(host) : null; } catch { }
  if (!box.isConnected) return;
  box.innerHTML = readyHtml(far ? [far, ...peers.filter(x => x !== far)] : peers, wifi);
  $$('[data-wifi-off]', box).forEach(b => b.onclick = async () => { b.disabled = true; await turnOff(b.dataset.wifiOff); paintReady(root); });
}

// ---------------------------------------------------------------- running
function numbers() {
  const g = S.gen.last, p = S.live.profile || T.p, ts = g && g.testset, now = {}, sts = p.gen.streams;
  const done = (ts && ts.done) || [], last = {};
  for (const st of done) for (const [k, m] of Object.entries(st.apps)) last[k] = { m, step: STEP_NAMES[st.kind] };
  sts.forEach((s, i) => {
    const t = tgtOf(s, p), pk = pKey(t), m = last[i];
    if (m) now[i] = { has: true, loss: m.m.loss_pct, lat: (m.m.lat_ms || {})[pk], jit: m.m.jitter_ms, mbps: m.m.rx_mbps, step: m.step.toLowerCase() };
  });
  if (g && g.streams_now) for (const x of g.streams_now) {
    const s = sts[x.i]; if (!s) continue;
    const lp = x.tx_pkts ? 100 * x.lost / x.tx_pkts : 0;
    now[x.i] = { ...(now[x.i] || {}), has: true, on: x.on, loss: lp, lat: x.lat_ms, jit: x.jit_ms, mbps: x.rx_mbps, step: ts ? ts.kind : '' };
  }
  return now;
}
function stepStrip() {
  const g = S.gen.last, ts = g && g.testset; if (!ts) return '<div class="tsstep muted">Starting…</div>';
  const p = S.live.profile, sts = p.gen.streams, cfg = p.gen.testset, many = sts.filter(s => s.enabled !== false).length > 1;
  const list = []; let n = 0;
  if (cfg.alone && many) sts.forEach((s, i) => { if (s.enabled !== false) list.push({ k: 'alone', label: s.name }); });
  list.push({ k: 'together', label: 'All together' }); if (cfg.fill) list.push({ k: 'fill', label: 'Plus fill' });
  const cells = list.map((x, j) => `<i class="${j + 1 < ts.n ? 'done' : j + 1 === ts.n ? 'now' : ''}" title="${esc(x.label)}"></i>`).join('');
  return `<div class="tsstep" data-testid="ts-step"><div class="tsdots">${cells}</div><b>Step ${ts.n} of ${ts.of}</b> · ${esc(ts.label)} <span class="muted">· ${dur(ts.step_elapsed_s)} of ${dur(ts.step_s)}${ts.kind === 'together' ? '' : ' · not judged'}</span></div>`;
}
function liveUpdate(root) {
  const box = $('#tsLive', root); if (!box) return;
  const p = S.live.profile || T.p; if (!p) return;
  if (!$('#tsStrip', box)) box.innerHTML = '<div id="tsStrip"></div><div id="tsTab"></div>';
  $('#tsStrip', box).innerHTML = stepStrip();
  const html = appsHtml(p, 'run', numbers()), tab = $('#tsTab', box), body = $('#tsRows', tab);
  if (!body) { tab.innerHTML = html; return; }
  const t = document.createElement('div'); t.innerHTML = html;
  const a = [...body.children], b = [...$('#tsRows', t).children];                      // update only the cells that changed: a tap on a switch is never lost to a redraw
  if (a.length !== b.length) { tab.innerHTML = html; return; }
  b.forEach((nr, k) => { const or = a[k]; if (or.className !== nr.className) or.className = nr.className;
    [...nr.children].forEach((nc, j) => { const oc = or.children[j]; if (!oc) return; if (oc.className !== nc.className) oc.className = nc.className; if (oc.innerHTML !== nc.innerHTML) oc.innerHTML = nc.innerHTML; }); });
}

// ---------------------------------------------------------------- result (tsresult.js, also drawn on Results)
async function fillResult(root, sum, d) {
  const box = $('#tsRes', root); if (!box) return;
  const id = S.live.runId; let r = null; try { r = await api(`/runs/${id}`); } catch { }
  const html = () => tsResultHtml(sum, { job: r && r.job, runId: id, name: S.live.profile && S.live.profile.name, when: r && r.started_at ? new Date(r.started_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '' });
  box.innerHTML = html();
  bindOnce(box, 'click', async ev => {
    const e = ev.target.closest('[data-job-edit]'); if (e) { const j = await Job.edit(id, (r && r.job) || {}); if (j) { r = { ...(r || {}), job: j }; box.innerHTML = html(); } return; }
    if (ev.target.closest('[data-ts=compare]')) { ev.preventDefault(); S.openRun = id; T.nav('results'); }
  });
}

// ---------------------------------------------------------------- editing
function applyTarget(el) {
  const s = T.p.gen.streams[+el.dataset.i]; if (!s) return;
  if (!s.target) s.target = { ...(T.p.gen.rule || {}), latency_percentile: (T.p.gen.rule || {}).latency_percentile || 99 };
  const v = el.value.trim(); s.target[el.dataset.tsTg] = v === '' ? null : Number(v); s.target.example = false;
}
function input(ev) { const el = ev.target; if (el.dataset && el.dataset.tsTg !== undefined && el.tagName === 'INPUT') applyTarget(el); }
function change(ev, root) {
  const el = ev.target;
  if (el.dataset.tsTg !== undefined) { applyTarget(el); draw(root); return; }
  if (el.dataset.tsEnd) { const w = el.dataset.tsEnd, cur = w === 'src' ? T.from : T.to; const ne = epFromValue(el.value, cur); if (w === 'src') T.from = ne; else T.to = ne;
    if (el.value === TYPED && !ne.host) { const c = genConn(); try { ne.host = new URL((c.peer_api || '').includes('//') ? c.peer_api : '//' + (c.peer_api || '')).hostname; } catch { } }
    if (el.value && el.value !== 'self' && el.value !== TYPED) { try { localStorage.setItem('tt-gen-other', el.value); } catch { } }
    draw(root); return; }
  if (el.dataset.tsHost) { const e = el.dataset.tsHost === 'src' ? T.from : T.to; e.host = el.value.trim(); e.typed = true; draw(root); }
}
async function addSheet() {
  T.lib = T.lib || await api('/apps');
  const list = T.lib.filter(a => a.group !== 'Fill'), by = {};
  list.forEach(a => (by[a.group] ||= []).push(a));
  const html = Object.entries(by).map(([g, l]) => `<h4 class="libg">${esc(g === 'DA' ? 'DA · distribution automation' : 'FAN · field area network')}</h4>${l.map(a => `<button type="button" class="libapp" data-lib="${esc(a.id)}" data-testid="ts-lib-${esc(a.id)}"><b>${esc(a.name)}</b><small>${esc(a.about)}</small></button>`).join('')}`).join('');
  const pr = sheetBox('Add an application', html, [{ id: 'cancel', label: 'Cancel' }], 'ts-add-sheet');
  $$('#modal .libapp').forEach(b => b.onclick = () => { const a = T.lib.find(x => x.id === b.dataset.lib); T.p.gen.streams.push({ enabled: true, app: a.id, name: a.name, ...clone(a.stream), target: { example: true, ...clone(a.target) } }); T.flash = T.p.gen.streams.length - 1; $('#sb-cancel').click(); });
  await pr;
}
async function editSheet(i, root) {
  const s = T.p.gen.streams[i], t = tgtOf(s, T.p), u = s.rate.pps != null ? 'pps' : 'mbps';
  const num_ = (k, label, val, unit) => `<label class="f"><span>${label}</span><input type="number" min="0" step="any" inputmode="decimal" data-k="${k}" data-testid="ts-ed-${k}" value="${val == null ? '' : val}" placeholder="not judged"> <i class="muted">${unit}</i></label>`;
  const html = `<label class="f"><span>Name</span><input data-k="name" maxlength="60" value="${esc(s.name)}" data-testid="ts-ed-name"></label>
    <div class="grid g2">${s.burst_gap_ms > 0 ? '' : `<label class="f"><span>Rate (${u === 'pps' ? 'packets/s' : 'Mb/s'})</span><input type="number" min="0.001" step="any" data-k="rate" value="${s.rate[u]}" data-testid="ts-ed-rate"></label>`}
      <label class="f"><span>Packet size (bytes)</span><input type="number" min="24" max="9000" data-k="size" data-testid="ts-ed-size" value="${s.size.mode === 'fixed' ? s.size.size : s.size.max}" ${s.size.mode === 'fixed' ? '' : 'disabled'}></label></div>
    <h4 class="libg">Target <span class="muted">(blank = not judged)</span></h4><div class="grid g2">${num_('max_loss_pct', 'Loss ≤', t.max_loss_pct, '%')}${num_('max_latency_ms', 'Latency ≤', t.max_latency_ms, 'ms')}
      <label class="f"><span>Latency measured at</span><select data-k="latency_percentile" data-testid="ts-ed-latency_percentile">${[50, 95, 99, 100].map(x => `<option value="${x}" ${t.latency_percentile === x ? 'selected' : ''}>${x === 100 ? 'the slowest packet' : 'p' + x}</option>`).join('')}</select></label>
      ${num_('max_jitter_ms', 'Jitter ≤', t.max_jitter_ms, 'ms')}${num_('min_throughput_mbps', 'Carries at least', t.min_throughput_mbps, 'Mb/s')}</div>`;
  const pr = sheetBox(s.name, html, [{ id: 'rm', label: 'Remove' }, { id: 'ok', label: 'Done', cls: 'primary' }], 'ts-edit-sheet');
  const upd = el => {
    const k = el.dataset.k, v = el.value;
    if (k === 'name') s.name = v.trim() || s.name;
    else if (k === 'rate') { const n = Number(v); if (n > 0) s.rate = { [u]: n }; }
    else if (k === 'size') { const n = Number(v); if (n >= 24 && n <= 9000) s.size.size = Math.round(n); }
    else { if (!s.target) s.target = { ...t }; s.target[k] = k === 'latency_percentile' ? Number(v) : v === '' ? null : Number(v); s.target.example = false; }
  };
  $$('#modal [data-k]').forEach(el => { el.oninput = () => upd(el); el.onchange = () => upd(el); });
  const r = await pr;
  if (r === 'rm') { if (T.p.gen.streams.length > 1) T.p.gen.streams.splice(i, 1); else toast('A test set keeps at least one application.', true); }
  draw(root);
}
async function moreSheet(root) {
  const p = T.p, mine = !p.builtin, id = T.id;
  const items = [['save', mine ? 'Save changes' : 'Save as my test set'], ['dup', 'Save a copy as my test set'], ['exp', 'Export'], [mine ? 'del' : 'hide', mine ? 'Delete' : 'Hide this test']];
  const pr = sheetBox('Test set', items.map(([k, l]) => `<button type="button" class="libapp" data-m="${k}" data-testid="ts-m-${k}"><b>${l}</b></button>`).join(''), [{ id: 'close', label: 'Close' }], 'ts-more-sheet');
  let pick = null; $$('#modal [data-m]').forEach(b => b.onclick = () => { pick = b.dataset.m; $('#sb-close').click(); });
  await pr; if (!pick) return;
  try {
    if (pick === 'save' || pick === 'dup') {
      const base = effProfile(); base.gen.peer_password = ''; base.builtin = false;
      const asCopy = pick === 'dup' || !mine;
      const name = asCopy ? await promptBox('Save as my test set', 'Name', p.builtin ? `${p.name} (mine)` : `${p.name} (copy)`) : p.name; if (!name) return;
      base.name = name; base.gen.testset.group = 'mine'; base.gen.streams.forEach(s => { delete s.src.pi; });
      base.gen.streams = base.gen.streams.map(s => ({ ...s, src: { pi: '', host: '', name: '', port: null, serial_port: '', iface: 'auto' }, dst: { pi: '', host: '', name: '', port: null, serial_port: '', iface: 'auto' } }));
      const r = asCopy ? await api('/profiles', { method: 'POST', body: base }) : await api(`/profiles/${id}`, { method: 'PUT', body: base });
      toast(asCopy ? `Saved “${r.name}” under My tests.` : 'Saved.'); S.profiles = await api('/profiles'); T.id = r.id; T.p = clone(r); [T.from, T.to] = endsOf(T.p);
    } else if (pick === 'exp') { const r = await fetch(`/api/profiles/export?ids=${id}`); const { download } = await import('./ui.js'); download(`test-set-${id}.json`, await r.text()); }
    else if (pick === 'hide') { if (await confirmBox('Hide this test?', `“${p.name}” will be taken off the list. Show it again under “Show hidden tests”.`, 'Hide')) { await api(`/profiles/${id}/hide`, { method: 'POST' }); T.p = null; return T.nav('tests', { force: true }); } }
    else if (pick === 'del') { if (await confirmBox('Delete this test set?', `“${p.name}” will be deleted. Results you already have are kept.`, 'Delete', true)) { await api(`/profiles/${id}`, { method: 'DELETE' }); T.p = null; return T.nav('tests', { force: true }); } }
  } catch (e) { toast(e.message, true, 8000); }
  draw(root);
}

async function click(ev, root) {
  const a = ev.target.closest('[data-ts],[data-nt=start]'); if (!a) return;
  const act = a.dataset.ts || 'start', i = a.dataset.i !== undefined ? +a.dataset.i : null, v = view();
  try {
    if (act === 'len') { T.p.gen.duration_s = +a.dataset.v; draw(root); }
    else if (act === 'len-custom') {
      const cur = Math.round(T.p.gen.duration_s / 60 * 100) / 100, r = await promptBox('Length of the all-together step', 'Minutes', String(cur), 'Save'); if (r == null) return;
      const m = Number(r); if (!(m > 0) || m > 7 * 24 * 60) return toast('Enter a length between 1 and 10080 minutes.', true); T.p.gen.duration_s = Math.round(m * 60); draw(root);
    }
    else if (act === 'path') { T.pathOpen = !T.pathOpen; draw(root); }
    else if (act === 'swap') { [T.from, T.to] = [T.to, T.from]; draw(root); }
    else if (act === 'tgl') {
      if (v === 'run') {
        const cur = numbers()[i], on = !(cur && cur.on === false); a.setAttribute('aria-checked', String(!on)); a.classList.toggle('on', !on);
        try { await api(`/runs/${S.live.runId}/stream`, { method: 'POST', body: { i, on: !on } }); } catch (e) { a.setAttribute('aria-checked', String(on)); a.classList.toggle('on', on); throw e; }
      } else { const s = T.p.gen.streams[i]; s.enabled = s.enabled === false; draw(root); }
    }
    else if (act === 'edit') await editSheet(i, root);
    else if (act === 'add') { await addSheet(); draw(root); }
    else if (act === 'alone') { T.p.gen.testset.alone = !T.p.gen.testset.alone; draw(root); }
    else if (act === 'fill') {
      const ts = T.p.gen.testset;
      if (ts.fill) { ts.fill = false; draw(root); } else { ts.fill = true; draw(root); await stepSheet('fill', root); }
    }
    else if (act === 'alone-s' || act === 'fill-s') await stepSheet(act === 'alone-s' ? 'alone' : 'fill', root);
    else if (act === 'more') await moreSheet(root);
    else if (act === 'again') { S.live.done = null; S.showDone = false; draw(root); }
    else if (act === 'start') {
      if (v === 'result') { S.live.done = null; S.showDone = false; return draw(root); }
      const pr = problem(); if (pr) return toast(pr, true, 8000);
      const p = effProfile(); a.disabled = true;
      try { const r = await api('/runs', { method: 'POST', body: { profile: p, job: Job.value() } }); Job.started(); resetLive(p, r.run_id); S.status = await api('/status'); T.nav('tests', { force: true }); }
      catch (e) { toast(e.message, true, 9000); a.disabled = false; }
    }
  } catch (e) { toast(e.message, true, 8000); }
}
