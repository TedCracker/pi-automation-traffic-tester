// Home: the instrument. Three panels built from what the status API already gives: Links (one row per test port), Loaded test (what Start will run,
// with one Ready line), Last results (the newest three). On a wide screen a small "This Pi" block sits beside them.
import { $, $$, esc, api, ms, pct, when, myName, NAMES, dur } from './ui.js';
import { S, isTestset } from './state.js';
import { loadedProfile, setLoaded, pillarOfRun, runName, isRunning } from './transport.js';
import { loadPeers, EP } from './endpoints.js';
import { wifiCheck, turnOff } from './wifi.js';

const kindWord = p => p.mode === 'mirrored' ? 'Mirrored Bits' : ({ serial: 'Serial', udp: 'UDP', tcp: 'TCP', goose_l2: 'GOOSE L2', goose_l3: 'GOOSE L3' })[p.transport] || p.transport;
const speed = p => p.speed_mbps ? (p.speed_mbps >= 1000 ? p.speed_mbps / 1000 + 'G' : p.speed_mbps + 'M') : '';

function linksHtml(ports, peers) {
  const up = peers.filter(p => p.reachable), to = up[0] || peers[0];
  const rows = ports.filter(p => p.kind !== 'loopback').map(p => {
    const test = p.kind === 'eth' || p.kind === 'usb_eth' || p.kind === 'serial';
    const lvl = !test ? 'off' : p.link ? 'ok' : 'warn';
    const dest = test && to ? `→ ${esc(to.name)}${p.kind !== 'serial' && to.host ? ' ' + esc(to.host) : ''}` : test ? '' : (p.you_are_here ? 'this page' : '');
    return `<tr class="${test ? '' : 'dim'}" data-testid="home-port-${esc(p.id)}"><td class="pn"><span class="sd ${lvl}"></span><b>${esc(p.name)}</b><small>${esc(p.label || '')}${speed(p) ? ' · ' + speed(p) : ''} ${dest}</small></td>
      <td class="st">${esc(p.state || (p.link ? 'link up' : 'no link'))}</td><td class="rt">${test && p.link && to ? (to.reachable ? 'answers' : 'no answer') : '—'}</td></tr>`;
  }).join('');
  return `<section class="panel" data-testid="home-links"><header><span>Links</span><small>${peers.length ? `this Pi ⇄ ${esc((to || {}).name || '')}` : 'no other Pi found yet'}</small></header>
    <table class="ptable"><thead><tr><th>Port</th><th>State</th><th>${peers.length ? esc((to || {}).name || 'Peer') : 'Peer'}</th></tr></thead><tbody>${rows || '<tr><td colspan="3" class="muted">No test ports found.</td></tr>'}</tbody></table></section>`;
}

function tsLoadedHtml(p, ready) {
  const ts = p.gen.testset, on = p.gen.streams.filter(s => s.enabled !== false), ex = on.some(s => s.target && s.target.example);
  const rows = on.map(s => { const t = s.target || {}; return `<tr><td><b>${esc(s.name)}</b><small>${esc(s.type.toUpperCase())}${s.rate && s.rate.pps != null ? ' · ' + s.rate.pps + ' pkt/s' : s.rate && s.rate.mbps != null ? ' · ' + s.rate.mbps + ' Mb/s' : ''}</small></td>
    <td class="r">${t.max_latency_ms != null ? '≤ ' + t.max_latency_ms + ' ms' : '—'}${ex ? '<small>example target</small>' : ''}</td><td class="r">${t.max_loss_pct != null ? '≤ ' + t.max_loss_pct + ' %' : '—'}</td></tr>`; }).join('');
  return `<section class="panel" data-testid="home-loaded"><header><span>Loaded test</span><a href="#tests" data-testid="home-change">Tests ›</a></header><div class="pbody">
    <div class="lt-head"><div><h2 class="lt-name" data-testid="home-loaded-name">${esc(p.name)}</h2><div class="lt-meta">Test set · ${on.length} application${on.length === 1 ? '' : 's'} · ${ts.alone && on.length > 1 ? 'alone, then ' : ''}together ${dur(p.gen.duration_s)}${ts.fill ? ', plus fill' : ''}</div></div><a class="btn" href="#tests" data-testid="home-change-btn">Change</a></div>
    <table class="ptable apps"><thead><tr><th>Application</th><th class="r">Latency</th><th class="r">Loss</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="ready" data-testid="home-ready">${ready}</div></div></section>`;
}

function loadedHtml(p, ready) {
  if (p && isTestset(p)) return tsLoadedHtml(p, ready);
  if (!p) return `<section class="panel" data-testid="home-loaded"><header><span>Loaded test</span><a href="#tests" data-testid="home-change">Tests ›</a></header><div class="pbody"><p class="muted">No test is loaded. Open Tests and tap ▶ Run on one.</p></div></section>`;
  const mb = p.mode === 'mirrored', thr = p.threshold_ms, loss = p.rule && p.rule.max_loss_pct;
  const where = p.transport === 'serial' ? `${p.serial.port && p.serial.port !== 'auto' ? p.serial.port : 'serial'} · ${p.serial.baud} baud` : p.transport === 'udp' || p.transport === 'tcp' ? `${p.transport.toUpperCase()} ${p.net.port}` : p.transport === 'goose_l2' ? 'GOOSE L2' : 'GOOSE L3';
  const target = mb ? `bit transfer ≤ ${thr} ms` : `${p.rule && p.rule.percentile === 100 ? 'max' : 'p' + (p.rule ? p.rule.percentile : 99)} ≤ ${thr} ms`;
  return `<section class="panel" data-testid="home-loaded"><header><span>Loaded test</span><a href="#tests" data-testid="home-change">Tests ›</a></header><div class="pbody">
    <div class="lt-head"><div><h2 class="lt-name" data-testid="home-loaded-name">${esc(p.name)}</h2><div class="lt-meta">${esc(kindWord(p))} · ${esc(where)} · ${dur(p.duration_s)}</div></div><a class="btn" href="#tests" data-testid="home-change-btn">Change</a></div>
    <table class="ptable apps"><thead><tr><th>Application</th><th class="r">Target</th><th class="r">Loss ≤</th></tr></thead><tbody>
      <tr><td><b>${mb ? 'Mirrored Bits' : 'Trip message'}</b><small>${esc(kindWord(p))}${mb ? '' : ` · every ${p.interval_ms} ms`}</small></td><td class="r">${esc(target)}<small>example target</small></td><td class="r">${mb ? `${p.mirrored.max_down_pct} %` : `${loss ?? '—'} %`}</td></tr></tbody></table>
    <div class="ready" data-testid="home-ready">${ready}</div></div></section>`;
}

export function readyHtml(peers, wifi) {
  const up = peers.filter(p => p.reachable), c = (S.status || {}).clock || {};
  const items = [];
  items.push(!peers.length ? `<span class="rl"><i class="sd off"></i>no other Pi known · <a href="#config/pis" data-testid="home-find-pi">find it</a></span>`
    : up.length ? `<span class="rl"><i class="sd ok"></i>${esc(up[0].name)} answers</span>` : `<span class="rl"><i class="sd bad"></i>${esc(peers[0].name)} does not answer</span>`);
  items.push(c.synced ? `<span class="rl"><i class="sd ok"></i>clocks agree${c.offset_ms != null ? ` ±${Math.abs(c.offset_ms).toFixed(1)} ms` : ''}</span>` : `<span class="rl"><i class="sd warn"></i>clocks not synced · <a href="#config/time" data-testid="home-time-link">Time</a></span>`);
  if (wifi && wifi.s === 'warn') items.push(`<span class="rl"><i class="sd warn"></i>Wi-Fi power saving on <button type="button" class="btn small" data-wifi-off="${esc(wifi.iface || '')}" data-testid="home-wifi-fix">Fix</button></span>`);
  return items.join('');
}

function resultsHtml(runs) {
  const rows = runs.map(r => {
    const h = r.headline || {}, v = r.status === 'running' ? 'RUN' : r.verdict;
    const tag = v === 'PASS' ? '<span class="vt pass">PASS</span>' : v === 'FAIL' ? '<span class="vt fail">FAIL</span>' : v === 'RUN' ? '<span class="vt run">RUNNING</span>' : `<span class="vt na">${esc((r.status || '').toUpperCase().slice(0, 7) || '—')}</span>`;
    const big = h.p99_ms != null ? [ms(h.p99_ms), 'p99'] : h.mbps != null ? [Number(h.mbps).toFixed(1) + ' Mb/s', r.kind === 'sniffer' ? 'captured' : 'received'] : ['—', ''];
    return `<tr class="click" data-run="${r.id}" data-testid="home-result" tabindex="0"><td class="vc">${tag}</td><td><b>${esc(r.name || '')}</b><small>${esc(when(r.started_at || r.created_at || r.at || ''))}${r.transport ? ' · ' + esc(r.transport) : ''}</small></td>
      <td class="r ${v === 'FAIL' ? 'red' : ''}"><span class="big">${esc(big[0])}</span><small>${esc(big[1])}</small></td><td class="r ${h.loss_pct > 0 ? 'red' : ''}"><span class="big">${h.loss_pct != null ? pct(h.loss_pct) : '—'}</span><small>lost</small></td></tr>`;
  }).join('');
  return `<section class="panel" data-testid="home-results"><header><span>Last results</span><a href="#results" data-testid="home-all-results">All results ›</a></header>
    ${runs.length ? `<table class="ptable res"><tbody>${rows}</tbody></table>` : '<div class="pbody"><p class="muted">No results yet. Load a test and press Start.</p></div>'}</section>`;
}

function thisPiHtml(peers) {
  const st = S.status || {}, hw = st.hardware || {}, c = st.clock || {}, up = peers.filter(p => p.reachable);
  const row = (k, v) => `<div class="kv"><span>${k}</span><b>${v}</b></div>`;
  return `<section class="panel thispi" data-testid="home-thispi"><header><span>This Pi</span></header><div class="pbody">
    ${row('Name', esc(myName()) + (hw.model ? ' · ' + esc(hw.model.replace('Raspberry Pi ', 'Raspberry Pi ')) : ''))}${row('Software', esc(st.version || ''))}
    ${row('Clock', c.synced ? `${esc(c.source || 'synced')}${c.offset_ms != null ? ` · ±${Math.abs(c.offset_ms).toFixed(2)} ms` : ''}` : 'not synced')}
    ${row('Other Pis', peers.length ? esc(peers.map(p => p.name + (p.reachable ? ' ✓' : ' ✗')).join(', ')) : 'none yet')}${row('Scheduled', '<span id="homeSched">…</span>')}</div></section>`;
}

let stop = 0;
export async function renderHome(root, nav) {
  const my = ++stop;
  const [profiles, peersR, portsR, runs] = await Promise.all([api('/profiles').catch(() => S.profiles), api('/peers').catch(() => ({ peers: [] })), api('/testports').catch(() => ({ ports: [] })), api('/runs?limit=3').catch(() => [])]);
  if (my !== stop) return;
  S.profiles = profiles; const peers = peersR.peers || [];
  const lp = loadedProfile();
  const run = isRunning();
  const live = run ? `<section class="panel live" data-testid="home-running"><header><span>${pillarOfRun() === 'sniffer' ? 'Capturing' : 'Running'}</span><a href="#${pillarOfRun()}" data-testid="home-open-live">Open ›</a></header><div class="pbody"><b>${esc(runName() || 'A run')}</b> is in progress. The timer and Stop are in the bar above. <a href="#${pillarOfRun()}" data-testid="home-open-live-2">Open it</a></div></section>` : '';
  const target = lp && peers.find(p => p.reachable);
  const wifi = target && target.host && lp && lp.transport !== 'serial' ? await wifiCheck(target.host) : null;
  if (my !== stop) return;
  root.innerHTML = `<div class="home">${live}<div class="homecols"><div class="hc1">${linksHtml(portsR.ports || [], peers)}${loadedHtml(lp, readyHtml(peers, wifi))}</div><div class="hc2">${resultsHtml((runs || []).slice(0, 3))}${thisPiHtml(peers)}</div></div></div>`;
  $$('[data-run]', root).forEach(tr => tr.onclick = tr.onkeydown = ev => { if (ev.type === 'keydown' && ev.key !== 'Enter') return; S.openRun = +tr.dataset.run; nav('results'); });
  $$('[data-wifi-off]', root).forEach(b => b.onclick = async () => { b.disabled = true; await turnOff(b.dataset.wifiOff); renderHome(root, nav); });
  api('/repeats').then(r => { const e = $('#homeSched'); if (e) e.textContent = r && r.length ? `${r.length} repeat${r.length > 1 ? 's' : ''}` : 'none'; }).catch(() => { const e = $('#homeSched'); if (e) e.textContent = 'none'; });
}
