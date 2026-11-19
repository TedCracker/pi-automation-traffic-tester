import { $, $$, esc, api, toast, tip, ms, num, pct, dur, confirmBox, sheetBox, stateBar, setStateBar } from './ui.js';
import { S, onLive, resetLive, loadLegs } from './state.js';
import { renderMonitor } from './monitor.js';
import { mountMB } from './mblive.js';
import { barHtml, bindBar, setRefreshTarget } from './refresh.js';
import { pullLive } from './state.js';

let off = null, go = () => { };
// a Mirrored Bits test: known from the profile this page started it with, or (page opened mid-run, test started elsewhere) from the running test's own status
const isMb = () => !!((S.live.profile && S.live.profile.mode === 'mirrored') || (S.status && S.status.run && S.status.run.mode === 'mirrored') || (S.live.done && S.live.done.summary && S.live.done.summary.mirrored && S.live.done.summary.mirrored.emulator));

export function renderRun(root, nav) {
  go = nav; if (off) off();
  const L = S.live;
  // keeps itself current: live events normally, polling when the live connection is down (refresh.js); a test started elsewhere appears here by itself
  setRefreshTarget(async () => { await pullLive(); if (!$('#runBanner') && (S.live.running || S.live.done)) renderRun(root, nav); });
  if (!L.running && !L.done) { root.innerHTML = `${barHtml()}<div class="card empty"><h2>No test is running</h2><p>Load a test on Tests and press Start.</p><div class="row" style="justify-content:center"><a class="btn primary" href="#tests">Go to Tests</a></div></div>`; bindBar(root); return; }
  // One live screen: verdict / progress, the key numbers, then the link diagram, delay chart and message bits (the former Monitor tab).
  root.innerHTML = `${barHtml()}<div id="runBanner"></div><div id="lampLine" data-testid="run-lamp"></div>
    <div class="card runhead"><div class="row sp"><div><b id="phaseTxt">Starting…</b><div class="muted" id="timeTxt"></div></div><div class="row" id="runActs"></div></div></div>
    <div id="sinceSw"></div><div class="tiles t5" id="stats"></div>
    <div id="monHost"></div>`;
  bindBar(root);
  renderMonitor($('#monHost'), { mirrored: isMb() });
  off = onLive(e => { if (['state', 'legstate', 'done', '_batch', 'mark', 'lamp'].includes(e.ev)) update(); });
  update();
  loadLegs().then(() => { if ($('#runBanner')) { update(); drawNames(); } }).catch(() => { });      // who sends and who answers, by name (from the stored run rows)
}

// Setup -> Demo, no cable: the Mirrored Bits self-test (two emulators inside this Pi, joined by an in-process wire).
export function renderDemo(root) {
  const host = $('#mbSelfHost', root); if (!host) return;
  host.insertAdjacentHTML('beforebegin', `<div class="row" style="margin:0 0 12px"><button class="btn primary" id="mbSelf" data-testid="mb-selftest" title="Two Mirrored Bits emulators inside this Pi, joined by an in-process wire: try the screen without a second Pi">Start the Mirrored Bits self-test</button></div>`);
  let mbOff = null; const showMb = () => { if (mbOff) mbOff(); mbOff = mountMB(host); };
  $('#mbSelf', root).onclick = async () => { try { await api('/mb/selftest', { method: 'POST', body: { duration_s: 600, baud: 38400 } }); showMb(); } catch (e) { toast(e.message, true, 7000); } };
  api('/mb/live').then(d => { if (d && d.running_any && !mbOff) showMb(); }).catch(() => { });      // a self-test (or the answering side) is already running here: show it
}

// The names on the link diagram: the first direction's sender on the left, its reflector on the right.
function drawNames() {
  const l = S.live.legs && S.live.legs[0], a = $('#nodeA b'), b = $('#nodeB b');
  if (l && a && b) { a.textContent = l.tester || 'Sender'; b.textContent = l.reflector || 'Other end'; const m = $('#nodeA small'), n = $('#nodeB small'); if (m) m.textContent = 'sends'; if (n) n.textContent = 'answers'; }
}
const multi = () => (S.live.legs || []).length > 1 || (S.live.runIds || []).length > 1 || !!(S.live.done && S.live.done.legs && S.live.done.legs.length > 1) || !!(S.status.run && S.status.run.legs);
// A test with two or more links: ONE table, a row per link (Link · Now · p99 · Lost), the worst in red; tap a row for its own tiles (docs/SPEC.md 7.7).
function linkRows() {
  const L = S.live, dl = (L.done && L.done.legs) || [], legs = L.legs.length > 1 ? L.legs : dl;
  return legs.map((l, i) => { const st = i === 0 ? L.state : L.legStates[i]; return { i, l, st: since(st), d: dl[i], name: l.label || (l.tester ? `${l.tester} → ${l.reflector}` : 'Link ' + (i + 1)) }; });
}
const over = (x, thr) => !!(x.st && (x.st.p99_ms > thr || x.st.lost));
function linkTable() {
  const L = S.live, thr = L.thr, rows = linkRows(); if (!rows.length) return '';
  const worst = rows.reduce((w, x) => (x.st && x.st.p99_ms != null && (!w || !w.st || x.st.p99_ms > w.st.p99_ms)) ? x : w, null);
  const bad = rows.filter(x => over(x, thr) || (x.d && x.d.pass === false)).length;
  const dot = x => x.d ? (x.d.status === 'failed' ? 'bad' : x.d.pass === false ? 'bad' : x.d.pass ? 'ok' : 'na') : !x.st ? 'wait' : over(x, thr) ? 'bad' : 'ok';
  return `<div class="card linktbl" data-testid="run-links"><div class="row sp"><b data-testid="run-links-banner" class="${bad ? 'bad' : 'ok'}">${bad ? `${bad} of ${rows.length} links over the limit` : `All ${rows.length} links within ${thr} ms`}</b><small class="muted">Tap a link for its numbers</small></div>
    <table><thead><tr><th>Link</th><th>Now</th><th>p99</th><th>Lost</th></tr></thead><tbody>${rows.map(x => `<tr data-leg="${x.i}" data-testid="run-leg-card" class="${L.pickLeg === x.i ? 'sel' : ''}${worst === x && rows.length > 1 && over(x, thr) ? ' worst' : ''}" tabindex="0">
      <td><span class="sdot ${dot(x)}"></span>${esc(x.name)}</td><td class="${x.st && x.st.now_ms > thr ? 'red' : ''}">${x.st ? ms(x.st.now_ms) : '–'}</td><td class="${x.st && x.st.p99_ms > thr ? 'red' : ''}">${x.st ? ms(x.st.p99_ms) : '–'}</td><td class="${x.st && x.st.lost ? 'red' : ''}">${x.st ? `${num(x.st.lost)}<small> / ${num(x.st.sent)}</small>` : '–'}</td></tr>`).join('')}</tbody></table></div>`;
}
// "Since mark" view: the numbers from the last ⚑ mark on (the server sends them with each state); the verdict is always the whole test.
function since(st) {
  if (!st || !st.since || !S.live.sinceView || !(S.live.marks || []).length) return st;
  const v = st.since; return { ...st, now_ms: v.now_ms ?? st.now_ms, p99_ms: v.p99_ms, max_ms: v.max_ms, lost: v.lost, sent: v.sent, late: v.late, _whole: st };
}
function sinceSwitch() {
  const L = S.live, m = (L.marks || []).at(-1), box = $('#sinceSw'); if (!box) return;
  if (!m) { box.innerHTML = ''; return; }
  box.innerHTML = `<div class="row sincesw" data-testid="run-since"><div class="seg" role="radiogroup" aria-label="Numbers shown"><button type="button" role="radio" aria-checked="${L.sinceView}" class="${L.sinceView ? 'on' : ''}" data-since="1" data-testid="run-since-mark" title="${esc(m.name || 'Mark ' + m.n)} at ${dur(m.t_s)}">Since ⚑${m.n}</button><button type="button" role="radio" aria-checked="${!L.sinceView}" class="${L.sinceView ? '' : 'on'}" data-since="0" data-testid="run-since-whole">Whole test</button></div><small class="muted">⚑${m.n}${m.name ?  + esc(m.name) : ''} at ${dur(m.t_s)} · the verdict is always the whole test</small></div>`;
  $$('[data-since]', box).forEach(b => b.onclick = () => { L.sinceView = b.dataset.since === '1'; update(); });
}
export async function dropMark() {
  const L = S.live; if (!L.running) return;
  const t0 = L.state ? L.state.elapsed_s : null;
  const pr = sheetBox('⚑ Mark this moment', `<label class="f"><span>Name (optional)</span><input id="markName" data-testid="run-mark-name" maxlength="40" autocomplete="off" placeholder="e.g. antenna moved"></label><p class="muted" style="margin-top:8px">The flag goes on the chart, the event log, the PDF and the CSV. The numbers since the mark show next to the whole test.</p>`, [{ id: 'no', label: 'Cancel' }, { id: 'go', label: '⚑ Mark', cls: 'primary' }], 'mark-sheet');
  const inp = $('#markName'); inp.focus(); let name = ''; inp.oninput = () => { name = inp.value; }; inp.onkeydown = e => { if (e.key === 'Enter') $('#sb-go').click(); };
  if (await pr !== 'go') return;
  try { const m = await api(`/runs/${L.runId}/mark`, { method: 'POST', body: { name: name.trim() } }); if (!(L.marks || []).some(x => x.n === m.n)) (L.marks ||= []).push(m); L.sinceView = true; toast(`⚑ Mark ${m.n} at ${dur(m.t_s)}${m.name ? ' · ' + m.name : ''}${t0 != null ? '' : ''}.`); update(); }
  catch (e) { toast(e.message, true, 6000); }
}
// Tester lamp: is the Pi itself (not the link) the limit? One line under the banner, naming the Pi and the reason (docs/SPEC.md 7.6).
export function lampLine() {
  const box = $('#lampLine'), lp = S.live.lamp; if (!box) return;
  if (!lp || !lp.level) { box.innerHTML = ''; return; }
  const ic = { ok: '●', warn: '!', limit: '✕' }[lp.level] || '●';
  box.innerHTML = `<div class="tlamp ${esc(lp.level)}" title="Is the Pi itself keeping up? (sending rate, processor, receive drops, clocks, Wi-Fi power saving, USB adapter delay)"><span class="lic">${ic}</span><b>${esc(lp.title || '')}</b>${lp.text ? `<span>${esc(lp.text)}</span>` : ''}</div>`;
}

const lvl = (v, thr) => v == null ? '' : v > thr ? 'bad' : v > thr * 0.7 ? 'warn' : '';      // colour rule: ink; amber past 70 % of the limit; red only when over
const stat = (label, val, sub, cls = '', t = '') => `<div class="stat ${cls}"><small>${label} ${t ? tip(t) : ''}</small><b>${val}</b><span>${sub}</span></div>`;

function tiles(st, thr, mbm) {
  const lossP = st.sent ? st.lost / st.sent * 100 : 0, w = st._whole, small = (v, f) => w ? ` · whole test ${f(v)}` : '';
  return mbm ?
      stat('Bit transfer time', ms(st.now_ms), `limit ${thr} ms (≈ RTT/2)`, lvl(st.now_ms, thr), 'The time for a bit change to cross the link, estimated as half the round trip of the echo bit (or measured one way when both clocks are synced).') +
      stat('Slowest 1 % (p99)', ms(st.p99_ms), 'of recent echoes', lvl(st.p99_ms, thr), 'p99') +
      stat('Slowest echo', ms(st.max_ms), 'recent', st.max_ms > thr ? 'warn' : '', 'max') +
      stat('Echo alarms', num(st.lost), 'round trip over RTT_MAX', st.lost ? 'bad' : '', 'The echo bit did not come back within RTT_MAX: a steady delay that ROK alone would not notice.') +
      stat('ROK now', st.rok === 0 ? '0' : st.rok === 1 ? '1' : '–', `RBAD ${st.rbad ? 'ON' : 'off'} · CBAD ${st.cbad ? 'ON' : 'off'}`, st.rok === 0 ? 'bad' : '', 'ROK is 1 while good messages keep arriving.') :
      stat('Delay now', ms(st.now_ms), `limit ${thr} ms`, lvl(st.now_ms, thr), 'rtt') +
      stat('Slowest 1 % (p99)', ms(st.p99_ms), w ? 'since the mark' + small(w.p99_ms, ms) : 'last 2,000 messages', lvl(st.p99_ms, thr), 'p99') +
      stat('Slowest ever', ms(st.max_ms), 'since start', st.max_ms > thr ? 'warn' : '', 'max') +
      stat('Lost', num(st.lost), pct(lossP) + ' of ' + num(st.sent) + ' sent' + small(w && w.lost, num), st.lost ? 'bad' : '', 'loss') +
      stat('Over the limit', num(st.late), `slower than ${thr} ms`, st.late ? 'warn' : '', 'threshold');
}
function bindRows() { $$('#stats tr[data-leg]').forEach(tr => tr.onclick = tr.onkeydown = ev => { if (ev.type === 'keydown' && ev.key !== 'Enter') return; const i = +tr.dataset.leg; S.live.pickLeg = S.live.pickLeg === i ? null : i; update(); }); }

export function update() {
  const L = S.live, st = L.state; if (!$('#runBanner')) return;
  const thr = L.thr;
  if (st) {
    setStateBar(st.elapsed_s, st.total_s);
    $('#phaseTxt').textContent = { steady: isMb() ? 'Both ends sending continuously — Mirrored Bits channel' : 'Sending trip messages', idle: 'Quiet-period check (waiting, then one message)', finishing: 'Finishing up…', starting: 'Starting…' }[st.phase] || st.phase;
    $('#timeTxt').textContent = `${dur(st.elapsed_s)} of ${dur(st.total_s)} · ${dur(Math.max(0, st.total_s - st.elapsed_s))} left`;
    const lossP = st.sent ? st.lost / st.sent * 100 : 0, mbm = isMb();
    const two = multi();
    $('#stats').className = two ? 'legcards' : 'tiles t5';
    const pick = two && L.pickLeg != null ? linkRows().find(x => x.i === L.pickLeg) : null;
    if (two) { $('#stats').innerHTML = linkTable() + (pick && pick.st ? `<div class="tiles t5" data-testid="run-leg-tiles">${tiles(pick.st, thr, false)}</div>` : ''); bindRows(); } else
    $('#stats').innerHTML = tiles(since(st), thr, mbm);
  }
  else if (multi() && (!$('#stats').firstChild || L.done)) { $('#stats').className = 'legcards'; $('#stats').innerHTML = linkTable() || '<div class="card"><span class="muted">Waiting for the first message…</span></div>'; bindRows(); }
  else if (!$('#stats').firstChild)      // before the first numbers arrive (up to a second): the five tiles are there already, so nothing jumps
    $('#stats').innerHTML = stat('Delay now', '–', `limit ${thr} ms`, '', 'rtt') + stat('Slowest 1 % (p99)', '–', 'last 2,000 messages', '', 'p99') + stat('Slowest ever', '–', 'since start', '', 'max') +
      stat('Lost', '–', 'waiting for the first message', '', 'loss') + stat('Over the limit', '–', `slower than ${thr} ms`, '', 'threshold');
  sinceSwitch(); lampLine();
  
  const b = $('#runBanner');
  if (L.done) {
    const d = L.done, v = d.summary && d.summary.verdict, dl = d.legs && d.legs.length > 1 ? d.legs : null;
    if (dl && d.status !== 'failed') {
      const pass = dl.every(l => l.pass === true), rows = dl.map(l => `<li><b>${esc(l.label)}</b>: ${l.status === 'failed' ? 'could not run' : l.pass == null ? esc(l.status) : l.pass ? 'PASS' : 'FAIL'}</li>`).join('');
      const nb = dl.filter(l => l.pass !== true).length;
      b.innerHTML = `<div class="banner ${pass ? 'pass' : 'fail'}"><div class="big">${pass ? '✓ PASS' : '✕ FAIL'}</div><div><b>${pass ? `All ${dl.length} links meet the requirement.` : `${nb} of ${dl.length} links do not meet the requirement.`}</b>${d.status === 'stopped' ? ' <small>(stopped early)</small>' : ''}<ul>${rows}</ul></div></div>`;
    } else if (d.status === 'failed') b.innerHTML = `<div class="banner fail"><div class="big">✕ COULD NOT RUN</div><div>${esc(d.error || 'The test failed to start.')}</div></div>`;
    else if (v) b.innerHTML = `<div class="banner ${v.pass ? 'pass' : 'fail'}"><div class="big">${v.pass ? '✓ PASS' : '✕ FAIL'}</div><div><b>${v.pass ? 'The link meets the requirement.' : 'The link does not meet the requirement.'}</b>${d.status === 'stopped' ? ' <small>(stopped early)</small>' : ''}${v.reasons.length ? '<ul>' + v.reasons.map(r => `<li>${esc(r)}</li>`).join('') + '</ul>' : `<div>99 % of messages arrived within ${ms(d.summary.rtt_ms.p99)} (limit ${d.summary.threshold_ms} ms).</div>`}</div></div>`;
    if (!$('#seeRes')) {           // finished: everything the user does next is one click away
      $('#phaseTxt').textContent = d.status === 'failed' ? 'Did not run' : d.status === 'stopped' ? 'Stopped' : 'Finished';
      $('#runActs').innerHTML = `<button class="btn primary" id="seeRes">See full results →</button>${d.status === 'failed' ? '' : `<a class="btn" id="runPdf" href="/api/report.pdf?run_ids=${dl ? dl.map(l => l.run_id).join(',') : L.runId}" download>📄 PDF report</a>`}
        ${L.profile ? '<button class="btn" id="again" title="Start the same test again with the same settings">↻ Run again</button>' : ''}<button class="btn" id="chg">Change setup</button>`;
      $('#seeRes').onclick = () => { S.openRun = L.runId; go('results'); };
      $('#chg').onclick = () => { S.live.done = null; S.showDone = false; go('tests'); };
      if ($('#again')) $('#again').onclick = async ev => {
        ev.target.disabled = true; const p = L.profile;
        try { const x = await api('/runs', { method: 'POST', body: { profile: p } }); resetLive(p, x.run_id, x.run_ids); S.showDone = false; go('tests'); } catch (e) { toast(e.message, true, 8000); ev.target.disabled = false; }
      };
    }
  } else {
    if (!$('[data-state-time]')) { b.innerHTML = stateBar('RUNNING', 'you can leave this page; it keeps going'); if (st) setStateBar(st.elapsed_s, st.total_s); }
  }
}
