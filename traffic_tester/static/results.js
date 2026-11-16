import { $, $$, esc, api, toast, confirmBox, promptBox, tip, ms, num, pct, dur, when, TRANSPORT_NAMES, fitPlot } from './ui.js';
import { S } from './state.js';
import { barHtml, bindBar, setRefreshTarget } from './refresh.js';
import { renderCompare, pickState, ensureCompareCss } from './compare.js';
import { Job } from './job.js';
import { radioResultHtml, mountRepeats } from './radioui.js';

let sessions = [], sid = null, plots = [], resRoot = null, runSig = '', curRuns = [];
// Compare (SPEC 7.5): Select mode puts a tick box on every result that has numbers; 2 to 4 of the same kind open the Compare page in place of the list.
let selMode = false, cmpIds = null;
const picked = new Set();
const sigOf = runs => JSON.stringify(runs.map(r => [r.id, r.status, r.verdict, r.job_line, (r.job || {}).note, r.vs_baseline && r.vs_baseline.text]));
const comparable = r => r.kind !== 'sniffer' && r.status !== 'running' && r.headline && r.headline.sent != null;
// Open the Compare page for these results (also used by "Compare with…" on an opened result: it starts Select mode with that result ticked).
export function openCompare(ids) { cmpIds = ids.slice(0, 4); if (resRoot) renderResults(resRoot); }
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
document.addEventListener('click', ev => { const m = $('#sMore'); if (m && m.open && !m.contains(ev.target)) m.open = false; }, true);   // the "More" menu closes when you click elsewhere

// A finished or newly started test appears in the list by itself (live event, or polling when the live connection is down).
export async function refreshResults() {
  if (!resRoot || !$('#sesSel', resRoot)) return;
  const runs = await api(`/runs?session_id=${sid}`);
  if (sigOf(runs) !== runSig) return renderResults(resRoot);
  if (S.openRun && runs.find(r => r.id === S.openRun && r.status === 'running')) showRun(S.openRun);
}

export async function renderResults(root) {
  resRoot = root;
  setRefreshTarget(refreshResults);
  plots.forEach(p => p.destroy()); plots = [];
  ensureCompareCss();
  if (cmpIds) return renderCompare(root, cmpIds, { onBack: () => { cmpIds = null; renderResults(root); } });
  sessions = await api('/sessions');
  if (!sid || !sessions.find(s => s.id === sid)) sid = S.status.session ? S.status.session.id : sessions[0].id;
  if (S.openRun) { const r = await api(`/runs/${S.openRun}`).catch(() => null); if (r) sid = r.session_id; }
  const runs = await api(`/runs?session_id=${sid}`);
  runSig = sigOf(runs); curRuns = runs;
  [...picked].forEach(i => { if (!runs.find(r => r.id === i && comparable(r))) picked.delete(i); });
  const nCmp = runs.filter(comparable).length;
  if (nCmp < 2) { selMode = false; picked.clear(); }
  if (selMode) groupRuns(runs).forEach(g => g.group && openGroups.add(String(g.id)));          // the links of a test are picked one by one
  const cur = sessions.find(s => s.id === sid), usb = (S.status.usb || []).length > 0;
  // Session bar with the list of its tests on top (pick one, the detail opens below); the rarely used session tools sit under "More".
  root.innerHTML = `${barHtml()}<div class="card"><div class="row sp">
      <label class="f" style="min-width:240px;flex:1;max-width:420px"><span>Session ${tip('A session groups related tests — for example one site visit.')}</span><select id="sesSel">${sessions.map(s => `<option value="${s.id}" ${s.id === sid ? 'selected' : ''}>${esc(s.name)} (${s.runs} test${s.runs === 1 ? '' : 's'})</option>`).join('')}</select></label>
      <div class="tools"><a class="btn small" href="/api/report.pdf?session_id=${sid}&title=${encodeURIComponent(cur.name)}" download ${runs.length ? '' : 'hidden'}>📄 PDF report</a>
      <a class="btn small" href="/api/sessions/${sid}/export.zip" download ${runs.length ? '' : 'hidden'}>⤓ Export ZIP</a>
      ${usb ? '<button class="btn small" id="sUsb">💾 Save to USB drive</button>' : ''}
      <button class="btn small" id="sSel" data-testid="results-select" aria-pressed="${selMode}" title="Tick 2 to 4 results of the same kind and compare them side by side" ${nCmp >= 2 ? '' : 'hidden'}>${selMode ? '✕ Stop selecting' : '☑ Select to compare'}</button>
      <button class="btn small" id="sNew">＋ New session</button>
      <details class="more" id="sMore"><summary aria-label="More session actions">More ▾</summary><div><button class="btn small" id="sRen">Rename session</button><button class="btn small" id="sClr">Clear tests</button>
        ${usb ? '' : '<button class="btn small" id="sUsb" disabled title="Plug in a USB drive">💾 Save to USB drive</button>'}</div></details></div></div>
      ${selMode ? `<div class="selbar" data-testid="results-selbar"><span class="grow" id="selTxt" data-testid="results-select-hint"></span><button type="button" class="btn small primary" id="selGo" data-testid="results-compare">Compare</button><button type="button" class="btn small" id="selNo" data-testid="results-select-cancel">Cancel</button></div>` : ''}
      ${runs.length ? `<div class="runlist"><table><thead><tr>${selMode ? '<th class="pick"></th>' : ''}<th>Result</th><th>Test</th><th>When</th><th>Link</th><th>p99</th><th>Lost</th><th>Messages</th></tr></thead><tbody>
      ${groupRuns(runs).map(g => g.group ? groupRows(g) : runRow(g.run)).join('')}</tbody></table></div>`
        : '<div class="empty"><p>No tests in this session yet. Run one from the Setup screen.</p></div>'}</div>
    <div id="repeatsCard"></div><div id="detail"></div>`;
  bindBar(root);
  mountRepeats($('#repeatsCard', root), id => { S.openRun = id; showRun(id); window.scrollTo({ top: 0 }); });
  $('#sesSel').onchange = e => { sid = +e.target.value; S.openRun = null; renderResults(root); };
  $('#sNew').onclick = async () => { const n = await promptBox('New session', 'Name (e.g. site and date)', ''); if (n === null) return; const s = await api('/sessions', { method: 'POST', body: { name: n } }); sid = s.id; S.status.session = s; toast('New session started.'); renderResults(root); };
  $('#sRen').onclick = async () => { const n = await promptBox('Rename session', 'Name', cur.name); if (n) { await api(`/sessions/${sid}`, { method: 'PUT', body: { name: n } }); renderResults(root); } };
  $('#sClr').onclick = async () => { if (await confirmBox('Clear all tests in this session?', 'This permanently deletes the tests and their data from this Pi. Export first if you need them.', 'Delete tests', true)) { try { await api(`/sessions/${sid}/runs`, { method: 'DELETE' }); S.openRun = null; renderResults(root); } catch (e) { toast(e.message, true); } } };
  $('#sUsb').onclick = async () => { try { const r = await api('/usb/save', { method: 'POST', body: { session_id: sid } }); toast('Saved to ' + r.path); } catch (e) { toast(e.message, true, 7000); } };
  $('#sSel').onclick = () => { selMode = !selMode; if (!selMode) picked.clear(); renderResults(root); };
  if (selMode) {
    const upd = () => {
      const st = pickState(runs.filter(r => picked.has(r.id)));
      $('#selTxt').textContent = st.text; $('#selTxt').classList.toggle('warn', !!st.warn); $('#selGo').disabled = !st.ok;
      $$('tr[data-run]', root).forEach(tr => tr.classList.toggle('on', picked.has(+tr.dataset.run)));
    };
    $$('input[data-pick]', root).forEach(cb => { cb.onclick = ev => ev.stopPropagation(); cb.onchange = () => { cb.checked ? picked.add(+cb.dataset.pick) : picked.delete(+cb.dataset.pick); upd(); }; });
    $('#selGo').onclick = () => { const ids = runs.filter(r => picked.has(r.id)).map(r => r.id).sort((a, b) => a - b); selMode = false; picked.clear(); openCompare(ids); };      // A = the oldest
    $('#selNo').onclick = () => { selMode = false; picked.clear(); renderResults(root); };
    upd();
  }
  $$('tr[data-run]', root).forEach(tr => tr.onclick = () => {
    if (selMode) { const cb = $('input[data-pick]', tr); if (cb && !cb.disabled) { cb.checked = !cb.checked; cb.onchange(); } return; }
    S.openRun = +tr.dataset.run;
    if (tr.dataset.group) { const open = !openGroups.has(tr.dataset.group); open ? openGroups.add(tr.dataset.group) : openGroups.delete(tr.dataset.group); $$(`tr.sub[data-of="${tr.dataset.group}"]`, root).forEach(x => x.hidden = !open); tr.setAttribute('aria-expanded', String(open)); }
    showRun(S.openRun);
  });
  if (selMode) return;                                    // picking: the opened result stays out of the way
  if (S.openRun) showRun(S.openRun); else if (runs[0]) showRun(runs[0].id);
}
// What a row of the list says under the test's name: the job, and the change against the baseline of that test.
const pickCell = r => !selMode ? '' : `<td class="pick">${r && comparable(r) ? `<input type="checkbox" data-pick="${r.id}" ${picked.has(r.id) ? 'checked' : ''} aria-label="Pick this result to compare" data-testid="results-pick-${r.id}">` : ''}</td>`;
const jobSmall = r => r.job_line || (r.job || {}).note ? `<small class="muted jobl" data-testid="result-job-line">${esc(r.job_line || r.job.note)}</small>` : '';
const vsParts = v => v.items.map(i => `<span class="${i.judged ? 'b-' + i.judged : ''}">${esc(i.text)}</span>`).join(' · ');
const vsSmall = r => { const v = r.vs_baseline; return !v || v.is_baseline ? '' : `<small class="muted vsb" data-testid="result-vs-baseline-row">vs baseline: ${vsParts(v)}</small>`; };
const baseTag = r => r.vs_baseline && r.vs_baseline.is_baseline ? ' <span class="tag base" data-testid="result-baseline-tag">Baseline</span>' : '';
const openGroups = new Set();
const kindName = k => k === 'one_way' ? 'One way' : 'Round trip';
const isTrip = r => !['generator', 'sniffer'].includes((r.profile || {}).kind) && r.transport !== 'generator' && r.transport !== 'sniffer';
// Who sent and who received, by name. Runs from before there were Pi names say "This Pi → <address>".
const legText = r => { const l = r.leg || {}; if (l.label) return l.label; const p = r.profile || {}; return `This Pi → ${(p.net && p.net.peer_host) || 'the other Pi'}`; };
function groupRuns(runs) {            // the directions of one test share a group_id: one entry
  const out = [], seen = new Map();
  for (const r of runs) {
    if (r.group_id) { let g = seen.get(r.group_id); if (!g) { g = { group: true, id: r.group_id, runs: [] }; seen.set(r.group_id, g); out.push(g); } g.runs.push(r); }
    else out.push({ group: false, run: r });
  }
  out.forEach(g => g.group && g.runs.sort((a, b) => a.id - b.id));
  return out;
}
// A test with several links, in one line: "pi1 ⇄ pi2 · Both ways at once" when the two links are the same pair both ways, else "pi1 → pi2, pi3 · 2 links".
export function groupLabel(g) {
  const ls = g.runs.map(x => x.leg || {}), a = ls[0];
  if (ls.length === 2 && a.tester && a.tester === ls[1].reflector && a.reflector === ls[1].tester) return `${a.tester} ⇄ ${a.reflector} · Both ways at once`;
  const uniq = k => [...new Set(ls.map(l => l[k] || '?'))];
  const src = uniq('tester'), dst = uniq('reflector');
  return `${src.join(', ')} → ${dst.join(', ')} · ${ls.length} links`;
}
function groupPill(g) {
  const rs = g.runs;
  if (rs.some(r => r.status === 'running')) return '<span class="pill run">RUNNING</span>';
  if (rs.every(r => r.verdict === 'PASS')) return '<span class="pill pass">PASS</span>';
  if (rs.some(r => r.verdict === 'FAIL')) return '<span class="pill fail">FAIL</span>';
  return `<span class="pill na">${esc((rs[0].status || '–').toUpperCase())}</span>`;
}
const runRow = (r, sub = false, gid = '') => `<tr class="click${sub ? ' sub' : ''}" data-run="${r.id}" ${sub ? `data-of="${gid}" ${openGroups.has(gid) ? '' : 'hidden'}` : ''} data-testid="results-row${sub ? '-direction' : ''}-${r.id}">${pickCell(r)}<td>${pill(r)}</td><td>${sub ? '↳ ' : ''}${esc(sub ? legText(r) : r.name || '')}${sub ? '' : baseTag(r)}${!sub && isTrip(r) ? `<br><small class="muted" data-testid="result-ends">${esc(legText(r))} · ${kindName((r.leg || {}).kind)}</small>` : sub ? ` <small class="muted">· ${kindName((r.leg || {}).kind)}</small>` : ''}${sub ? '' : jobSmall(r)}${vsSmall(r)}</td><td>${when(r.started_at)}</td><td>${esc(TRANSPORT_NAMES[r.transport] || r.transport)}</td><td>${ms(r.headline.p99_ms)}</td><td>${pct(r.headline.loss_pct)}</td><td>${num(r.headline.sent)}</td></tr>`;
function groupRows(g) {
  const rs = g.runs, open = openGroups.has(String(g.id)) || (S.openRun && rs.some(r => r.id === S.openRun));
  if (open) openGroups.add(String(g.id));
  const mx = k => { const v = rs.map(r => r.headline[k]).filter(x => x != null); return v.length ? Math.max(...v) : null; };
  const sent = rs.reduce((a, r) => a + (r.headline.sent || 0), 0);
  return `<tr class="click grp" data-run="${rs[0].id}" data-group="${g.id}" aria-expanded="${open}" data-testid="results-group-${g.id}">${pickCell(null)}<td>${groupPill(g)}</td><td>${esc(rs[0].name || '')}${baseTag(rs[0])}<br><small class="muted" data-testid="result-ends">${esc(groupLabel(g))}</small>${jobSmall(rs[0])}</td><td>${when(rs[0].started_at)}</td><td>${esc(TRANSPORT_NAMES[rs[0].transport] || rs[0].transport)}</td><td>${ms(mx('p99_ms'))}</td><td>${pct(mx('loss_pct'))}</td><td>${num(sent)}</td></tr>`
    + rs.map(r => runRow(r, true, String(g.id))).join('');
}
const pill = r => r.status === 'running' ? '<span class="pill run">RUNNING</span>' : r.verdict ? `<span class="pill ${r.verdict === 'PASS' ? 'pass' : 'fail'}">${r.verdict}</span>` : `<span class="pill na">${esc((r.status || '–').toUpperCase())}</span>`;

// The test traffic went over Wi-Fi: say so (power saving on adds wake-up delay of its own, so an idle-gap result may be worse than the link deserves).
const pathNote = s => s.path && s.path.kind === 'wifi' ? `<div class="check ${s.path.power_save ? 'warn' : 'ok'}" data-testid="wifi-path-note"><div class="ic">${s.path.power_save ? '!' : '✓'}</div><div><b>This test went over Wi-Fi (${esc(s.path.interface)})</b><small>${s.path.power_save ? 'Wi-Fi power saving was ON, which adds wake-up delay to the first message after a quiet period. Turn it off in Setup (pre-flight) and run again to see the link without that.' : s.path.power_save === false ? 'Wi-Fi power saving was off.' : 'Wi-Fi power saving could not be read.'}</small></div></div>` : '';
const card = (label, val, sub, cls = '', t = '') => `<div class="stat ${cls}"><small>${label} ${t ? tip(t) : ''}</small><b>${val}</b><span>${sub || ''}</span></div>`;

// Under the verdict of an opened result: the change against the baseline of its saved test (or that this result IS the baseline).
const vsLine = r => { const v = r.vs_baseline; if (!v) return '';
  return v.is_baseline ? `<div class="vsbase" data-testid="result-vs-baseline"><span class="tag base">Baseline</span><span class="grow">${esc(v.text)}</span></div>`
    : `<div class="vsbase" data-testid="result-vs-baseline"><span class="grow">vs baseline: ${v.items.map(i => `<b class="${i.judged ? 'b-' + i.judged : ''}">${esc(i.text)}</b>`).join(' · ')}</span><button type="button" class="btn small ghost" data-cmp-base="${v.run_id}" data-testid="result-compare-baseline">Compare with the baseline</button></div>`; };
// Compare / baseline buttons of an opened result (next to its exports).
const cmpBtns = r => r.kind === 'sniffer' || !r.summary || !r.summary.verdict ? '' : `<button type="button" class="btn small" data-cmp-pick data-testid="result-compare">⇄ Compare with…</button><button type="button" class="btn small" data-base-toggle data-testid="result-baseline">${r.vs_baseline && r.vs_baseline.is_baseline ? 'Baseline ✓ (remove)' : '⚑ Set as baseline'}</button>`;
function bindExtras(box, r) {
  const again = () => renderResults(resRoot);
  $$('[data-job-edit]', box).forEach(b => b.onclick = async () => { if (await Job.edit(r.id, r.job) !== null) again(); });
  $$('[data-cmp-base]', box).forEach(b => b.onclick = () => openCompare([+b.dataset.cmpBase, r.id]));
  $$('[data-cmp-pick]', box).forEach(b => b.onclick = () => { selMode = true; picked.clear(); picked.add(r.id); again().then(() => { const l = $('.runlist', resRoot); if (l) l.scrollIntoView({ block: 'nearest' }); window.scrollTo({ top: 0 }); }); });
  $$('[data-base-toggle]', box).forEach(b => b.onclick = async () => {
    const is = r.vs_baseline && r.vs_baseline.is_baseline;
    try { await api(`/runs/${r.id}/baseline`, { method: is ? 'DELETE' : 'POST' }); toast(is ? 'Baseline removed.' : `This result is now the baseline of “${(r.profile || {}).name || 'this test'}”. Later runs show their change against it.`, false, 6000); again(); }
    catch (e) { toast(e.message, true, 6000); }
  });
}

// Marks dropped during the test (⚑ on the Run screen): one line on the result, a flag on the delay chart.
export const marksLine = s => (s.marks || []).length ? `<p class="marksline" data-testid="result-marks">${s.marks.map(m => `<span>⚑${m.n} at ${dur(m.t_s)}${m.name ? ' · ' + esc(m.name) : ''}</span>`).join('')}</p>` : '';
// The tester lamp of the whole run: was the Pi itself the limit at any time?
export const testerLine = t => t && t.level ? `<div class="tlamp ${esc(t.level)}" data-testid="result-tester"><span class="lic">${{ ok: '●', warn: '!', limit: '✕' }[t.level] || '●'}</span><b>${esc(t.title || '')}</b>${t.text ? `<span>${esc(t.text)}</span>` : ''}</div>` : '';
export const markPlugin = src => ({ hooks: { draw: u => { const marks = typeof src === 'function' ? src() : src; if (!(marks || []).length) return; const c = u.ctx; c.save(); c.strokeStyle = css('--accent'); c.fillStyle = css('--accent'); c.lineWidth = 2; c.setLineDash([4, 4]); c.font = `bold ${12 * devicePixelRatio}px system-ui`;
  for (const m of marks) { const x = u.valToPos(m.t_s, 'x', true); if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue; c.beginPath(); c.moveTo(x, u.bbox.top); c.lineTo(x, u.bbox.top + u.bbox.height); c.stroke(); c.fillText('⚑' + m.n, x + 4, u.bbox.top + 14 * devicePixelRatio); } c.restore(); } } });
// A test with two or more links: one table of every link heads the result (tap a row to open that link).
function linksTable(r, grp, thr) {
  const all = [curRuns.find(x => x.id === r.id) || r, ...grp].sort((a, b) => a.id - b.id), h = x => x.headline || {};
  const bad = all.filter(x => x.verdict === 'FAIL' || x.status === 'failed').length;
  const worst = all.reduce((w, x) => (h(x).p99_ms ?? -1) > (w ? (h(w).p99_ms ?? -1) : -1) ? x : w, null);
  return `<div class="card linktbl" data-testid="result-links"><div class="row sp"><b class="${bad ? 'bad' : 'ok'}" data-testid="result-links-banner">${bad ? `${bad} of ${all.length} links over the limit` : `All ${all.length} links within the limit`}</b><small class="muted">Tap a link to open it</small></div>
    <table><thead><tr><th>Link</th><th>p99</th><th>Lost</th><th></th></tr></thead><tbody>${all.map(x => `<tr ${x.id === r.id ? 'class="sel"' : `data-open-run="${x.id}"`} data-testid="result-link-${x.id}" tabindex="0"${worst === x && bad ? ' style="font-weight:700"' : ''}>
      <td><span class="sdot ${x.verdict === 'PASS' ? 'ok' : x.verdict === 'FAIL' || x.status === 'failed' ? 'bad' : ''}"></span>${esc(legText(x))}</td><td class="${h(x).p99_ms > thr ? 'red' : ''}">${ms(h(x).p99_ms)}</td><td class="${h(x).loss_pct ? 'red' : ''}">${h(x).loss_pct == null ? '–' : pct(h(x).loss_pct)}</td><td>${esc(x.verdict || x.status || '')}</td></tr>`).join('')}</tbody></table></div>`;
}

async function showRun(id) {
  const box = $('#detail'); if (!box) return;
  $$('tr[data-run]').forEach(tr => tr.classList.toggle('on', +tr.dataset.run === id));
  plots.forEach(p => p.destroy()); plots = [];
  const r = await api(`/runs/${id}`); const s = r.summary || {}; const v = s.verdict;
  if (s.kind === 'generator') return showGen(box, r, id);
  if (s.kind === 'sniffer') return showSniff(box, r, id);
  const p = r.profile || {}, thr = s.threshold_ms || p.threshold_ms || 60;
  const ep = s.endpoints || {}, lg = r.leg || {}, tester = ep.source || lg.tester || 'This Pi', refl = ep.dest || lg.reflector || ((p.net && p.net.peer_host) || 'the other Pi');
  const grp = r.group_id ? curRuns.filter(x => x.group_id === r.group_id && x.id !== r.id).sort((a, b) => a.id - b.id) : [];
  const up = s.basis === 'up';
  const head = `<div class="card runhead" data-testid="result-header"><div class="row sp"><b style="font-size:1.1rem" data-testid="result-ends-label">${esc(tester)} → ${esc(refl)}</b><span class="tag">${grp.length ? `${grp.length + 1} links` : kindName(ep.kind || lg.kind)}</span></div>
      <p class="muted" style="margin:6px 0 0" data-testid="result-ends-line">Sent from <b>${esc(tester)}</b> to <b>${esc(refl)}</b> · ${grp.length ? `one of ${grp.length + 1} links of this test` : kindName(ep.kind || lg.kind)} · ${up ? 'judged on the one-way delay' : 'judged on the round trip'}${ep.controller ? ` · started from ${esc(ep.controller)}` : ''}${ep.address ? ` · address ${esc(ep.address)}` : ''}</p>
      ${marksLine(s)}</div>`;
  let banner;
  if (r.status === 'failed' || (!v && s.error)) banner = `<div class="banner fail"><div class="big">✕ NO RESULT</div><div>${esc(s.error || 'The test could not run.')}</div></div>`;
  else if (!v) banner = `<div class="banner run"><div class="big">● ${esc((r.status || '').toUpperCase())}</div><div>No verdict yet.</div></div>`;
  else banner = `<div class="banner ${v.pass ? 'pass' : 'fail'}"><div class="big">${v.pass ? '✓ PASS' : '✕ FAIL'}</div><div><b>${esc(r.profile.name)}</b> — ${v.pass ? 'every requirement was met.' : 'the requirement was not met.'}${r.status === 'stopped' ? ' <small>(stopped early)</small>' : ''}${v.reasons.length ? '<ul>' + v.reasons.map(x => `<li>${esc(x)}</li>`).join('') + '</ul>' : ''}</div></div>`;
  const rt = (up && s.up_ms) ? s.up_ms : (s.rtt_ms || {});
  const cards = s.rtt_ms ? `${up ? `<p class="muted" style="margin:0 0 8px">These numbers are the delay one way, from ${esc(tester)} to ${esc(refl)}.</p>` : ''}<div class="tiles">
    ${card('Typical (p50)', ms(rt.p50), '', '', 'p50')}${card('p95', ms(rt.p95), '', '', 'p95')}
    ${card('p99', ms(rt.p99), `limit ${thr} ms`, rt.p99 > thr ? 'bad' : 'good', 'p99')}${card('Slowest', ms(rt.max), '', rt.max > thr ? 'warn' : '', 'max')}
    ${card('Lost', num(s.lost), pct(s.loss_pct) + ' of ' + num(s.sent), s.lost ? 'bad' : 'good', 'loss')}${card('Over the limit', num(s.over_threshold), pct(s.over_threshold_pct), s.over_threshold ? 'warn' : 'good', 'threshold')}
    ${card('Jitter', ms(s.jitter_ms), 'change between messages', '', 'jitter')}${card('Messages', num(s.sent), `${num(s.duplicates)} duplicate · ${num(s.out_of_order)} out of order · ${num(s.corrupted)} corrupted`)}</div>` : '';
  const ow = s.one_way && s.up_ms ? `<div class="card"><h2>Each direction ${tip('oneway')}</h2><div class="scroll"><table><thead><tr><th></th><th>p50</th><th>p95</th><th>p99</th><th>Slowest</th></tr></thead><tbody>
      ${['up', 'down'].map(k => `<tr><td><b>${esc(k === 'up' ? tester + ' → ' + refl : refl + ' → ' + tester)}</b></td>${['p50', 'p95', 'p99', 'max'].map(q => `<td>${ms(s[k + '_ms'][q])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
      <p class="muted">Clock offset at the start: ${esc(tester)} ${s.peer && s.peer.a_offset_ms != null ? s.peer.a_offset_ms + ' ms' : '?'} · ${esc(refl)} ${s.peer && s.peer.offset_ms != null ? s.peer.offset_ms + ' ms' : '?'}.</p></div>`
    : (p.one_way ? `<div class="card"><h2>Each direction</h2><p class="muted">Not available: ${esc((s.peer && s.peer.one_way_unavailable) || 'clocks not synced')}. Round trip only.</p></div>` : '');
  const idle = s.idle && s.idle.count ? `<div class="card"><h2>First message after a quiet period ${tip('idle')}</h2><div class="scroll"><table><thead><tr><th>Quiet for</th><th>Tests</th><th>Fastest</th><th>Median</th><th>Slowest</th><th>Lost</th></tr></thead><tbody>
      ${s.idle.gaps.map(g => `<tr><td>${dur(g.gap_s)}</td><td>${g.count}</td><td>${ms(g.min_ms)}</td><td>${ms(g.median_ms)}</td><td ${g.max_ms > thr ? 'style="color:var(--fail);font-weight:700"' : ''}>${ms(g.max_ms)}</td><td>${g.lost}</td></tr>`).join('')}</tbody></table></div></div>` : '';
  const tcp = s.extra && s.extra.tcp ? `<div class="card"><h2>TCP session</h2><div class="grid g4">${card('Connect time', ms(s.extra.tcp.setup_ms), 'first session')}${card('Sessions', num(s.extra.tcp.sessions), 'opened')}${card('Reconnects', num(s.extra.tcp.reconnects.length), s.extra.tcp.reconnects.map(x => `outage ${x.outage_ms ?? '?'} ms · connect ${x.connect_ms ?? '?'} ms`).join('; ') || 'none')}${card('Stalls', num(s.extra.tcp.stalls), 'silent drops detected')}</div></div>` : '';
  const goose = s.extra && s.extra.goose ? gooseCard(s.extra.goose) : '';
  const imp = s.impairment ? `<p class="muted">Bad-link simulation was on: <b>${esc(s.impairment.preset)}</b> on ${esc(s.impairment.iface)} (${esc(s.impairment.args)})${s.impairment.repeatable ? '' : ' — not repeatable on this Pi (iproute2 has no fixed seed)'}.</p>` : '';
  box.innerHTML = `${grp.length ? linksTable(r, grp, thr) : ''}${head}${banner}${testerLine(s.tester)}${vsLine(r)}${Job.block(r.job)}${pathNote(s)}${cards}${imp}
    <div class="card" style="padding:12px 16px"><div class="row sp"><div class="tools"><b>Export this test:</b><a class="btn small" href="/api/report.pdf?run_ids=${id}" download>📄 PDF report</a><a class="btn small" href="/api/runs/${id}/export.csv" download>Messages (CSV)</a><a class="btn small" href="/api/runs/${id}/export.csv?kind=intervals" download>Summary per second (CSV)</a><a class="btn small" href="/api/runs/${id}/export.json" download>Everything (JSON)</a>${cmpBtns(r)}</div>
      <button class="btn small ghost" id="delRun">Delete this test</button></div>
      <p class="muted" style="margin:8px 0 0;font-size:.85rem">Setup used: ${esc(TRANSPORT_NAMES[r.transport] || r.transport)} · ${esc(r.port || '')} · ${when(r.started_at)}</p>${grp.length ? `<p class="muted" style="margin:4px 0 0;font-size:.85rem"><a href="/api/report.pdf?run_ids=${[r.id, ...grp.map(x => x.id)].sort((a, b) => a - b).join(',')}" download data-testid="result-pdf-both">📄 PDF report with every link</a></p>` : ''}</div>
    <div class="grid g2"><div class="card"><h2>Delay over time</h2><div id="rChart" class="chart"></div></div><div class="card"><h2>How delays were spread</h2><div id="rHist"></div></div></div>
    <div class="card"><h2>Per-second summary</h2><div id="iChart" class="chart"></div></div>
    ${mbSection(s, id)}${radioResultHtml(s)}${ow}${idle}${tcp}${goose}`;
  $('#delRun').onclick = async () => { if (await confirmBox('Delete this test?', 'Its data is removed permanently.', 'Delete', true)) { await api(`/runs/${id}`, { method: 'DELETE' }); S.openRun = null; renderResults($('#main').firstElementChild || $('#main')); } };
  $$('[data-open-run]', box).forEach(b => b.onclick = () => { S.openRun = +b.dataset.openRun; showRun(S.openRun); window.scrollTo({ top: 0 }); });
  bindExtras(box, r);
  if (s.rtt_ms) charts(id, thr, r.intervals, s.marks);
}

// Mirrored Bits: the emulator's result (bit transfer time, ROK / RBAD / CBAD, rejects by reason, inter-arrival histogram, impairment used, far end).
// Tests saved by the first Mirrored Bits mode (before the emulator) have no `emulator` flag and keep their old card.
const REJ = ['parity', 'framing', 'overrun', 'consistency', 'crc', 'address', 'loopback', 'timing', 'missing'];
const mbSection = (s, id) => {
  const m = s.mirrored; if (!m) return '';
  return m.emulator ? mbEmu(s, m, id) : mbOld(s, m);
};
// Frame codec (Trip frame): the round trip of every frame is the headline; lost frames are exact (sequence numbers), CRC errors are damaged frames.
const frameTiles = (m, t, r) => {
  const f = m.frames || {}, thr = t.threshold_ms || 1e9, limit = (m.settings || {}).rtt_max_ms;
  return `${card('Round trip (every frame)', r.p999 == null ? '–' : ms(r.p999), `p99.9 · p50 ${ms(r.p50)} · p99 ${ms(r.p99)} · max ${ms(r.max)} · jitter ${ms(r.jitter_ms)}`, (r.p999 > thr || r.max > thr) ? (r.p999 > thr ? 'bad' : 'warn') : 'good', 'Measured from the echo in the frames, no clock sync. One way is about half (an estimate: true one-way needs GPS time). Limit ' + ms(thr) + ' at the chosen percentile.')}
    ${card('Frames lost', num(f.lost), `${f.lost_pct}% of ${num((f.good || 0) + (f.lost || 0))} · ${num(f.lost_events)} gap(s) · longest ${num(f.longest_gap)}`, f.lost ? 'bad' : 'good', 'Counted exactly from the frame numbers; includes frames that arrived damaged.')}
    ${card('CRC errors', num(f.crc_errors), `${num(f.duplicates)} repeated · ${num(f.out_of_order)} out of order`, f.crc_errors ? 'warn' : 'good')}`;
};
const mbEmu = (s, m, id) => {
  const t = m.transfer || {}, r = m.rtt || {}, h = m.histogram || { counts: [], labels: [] }, rej = m.rejects || {}, far = m.far, imp = m.impair || {};
  const impOn = ['latency_ms', 'jitter_ms', 'drop_pct', 'burst_len_ms', 'bit_error_pct'].filter(k => imp[k] > 0).map(k => `${k.replace(/_/g, ' ')} ${imp[k]}`).concat(imp.break_link ? ['link broken by hand'] : []);
  const mx = Math.max(1, ...h.counts), outs = (m.outages || []).slice(0, 20);
  const rejTot = REJ.reduce((a, k) => a + (rej[k] || 0), 0);
  return `<div class="card" data-testid="mb-result"><h2>Mirrored Bits channel <span class="tag preset" style="text-transform:none">${num(m.baud)} baud · PROTO ${esc(m.proto)}</span></h2>
    <p class="muted" style="margin:0 0 8px">${esc(m.codec)}. ${num(m.tx_msgs)} messages sent (${num(m.tx_rate)} per second, one every ${m.wire_period_ms} ms), ${num(m.rx_good)} good ones received here, ${num(rejTot)} rejected.</p>
    <div class="tiles">${m.frame_mode ? frameTiles(m, t, r) : ''}${m.frame_mode ? '' : card('Bit transfer time', t.p99 == null ? '–' : ms(t.p99), `p99 · p50 ${ms(t.p50)} · max ${ms(t.max)}`, t.p99 > (t.threshold_ms || 1e9) ? 'bad' : 'good', 'How long a bit change takes to cross the link. ' + (t.basis === 'one_way' ? 'Measured one way (both clocks synced).' : 'Estimated as half the round trip of the echo bit.'))}
      ${m.frame_mode ? '' : card('Round trip (echo bit)', ms(r.p50), `p99 ${ms(r.p99)} · max ${ms(r.max)} · jitter ${ms(r.jitter_ms)}`, r.alarm_events ? 'bad' : 'good')}
      ${card('ROK availability', pct(m.availability_pct), `ROK was 0 for ${pct(m.unavail_pct)} of the time`, m.unavail_pct > 0.1 ? 'warn' : 'good')}
      ${card('Longest outage', (m.longest_outage_ms || 0) + ' ms', `${num(m.outage_count)} outage(s) · ${num(m.rok_drops)} ROK drops`, m.longest_outage_ms ? 'warn' : 'good')}
      ${card('RBAD / CBAD', `${num(m.rbad_events)} / ${num(m.cbad_events)}`, `events · lost-channel ${num(m.lost_events)}`, m.rbad_events || m.cbad_events ? 'bad' : 'good')}
      ${card(m.frame_mode ? 'Slow round trips' : 'Echo alarms', num(r.alarm_events), `round trip over ${r.rtt_max_ms} ms`, r.alarm_events ? 'bad' : 'good')}</div>
    ${m.one_way_gps ? `<p class="muted" data-testid="mb-gps-note">${esc(m.one_way_gps)}.</p>` : ''}
    ${m.one_way_note ? `<p class="muted">${esc(m.one_way_note)}</p>` : ''}
    <div class="grid g2" style="margin-top:12px"><div class="scroll"><table><thead><tr><th>Rejected messages</th><th>Count</th></tr></thead><tbody>${REJ.map(k => `<tr><td>${k}</td><td>${num(rej[k] || 0)}</td></tr>`).join('')}</tbody></table>
      ${m.overrun_available === false ? '<p class="muted">Overrun counting is not available on this serial port (the driver has no counter).</p>' : ''}</div>
      <div><b>Gap between good messages</b> <span class="muted">(nominal ${m.histogram ? m.histogram.nominal_ms : '–'} ms; shows radio bunching)</span>
        <div class="mbhist" data-testid="mb-hist">${h.counts.map((c, i) => `<div title="${esc(h.labels[i])} ms: ${num(c)}"><i style="height:${Math.round(60 * Math.log10(1 + c) / Math.log10(1 + mx))}px"></i><small>${esc(h.labels[i].replace('<', ''))}</small></div>`).join('')}</div></div></div>
    ${impOn.length ? `<p class="muted"><b>Impairment injector was on:</b> ${esc(impOn.join(', '))} (${num(imp.dropped)} dropped, ${num(imp.corrupted)} corrupted, ${num(imp.delayed)} delayed).</p>` : ''}
    ${far ? `<p class="muted"><b>The other end</b> (${esc(far.name || '')} · PROTO ${esc(far.proto || '')}): ROK drops ${num(far.counters && far.counters.rok_drops)}, RBAD ${num(far.counters && far.counters.rbad_events)}, CBAD ${num(far.counters && far.counters.cbad_events)}, rejected ${num(far.counters && far.counters.rx_rejected)}.</p>` : ''}
    ${outs.length ? `<div class="scroll"><table><thead><tr><th>ROK was 0 at</th><th>For</th></tr></thead><tbody>${outs.map(o => `<tr><td>${o.start_s} s</td><td>${o.dur_ms} ms${o.open ? ' (still 0 at the end)' : ''}</td></tr>`).join('')}</tbody></table></div>` : ''}
    <div class="row" style="margin-top:10px"><a class="btn small" href="/api/runs/${id}/export.csv?kind=mbevents" download data-testid="mb-events-csv">⬇ Event log (CSV)</a></div></div>`;
};
const mbOld = (s, m) => {
  const a = m.a_rx || {}, bad = (a.msgs_bad || 0) + (a.startup_bad || 0);
  const outs = (m.outages || []).slice(0, 20);
  return `<div class="card" data-testid="mb-result"><h2>Mirrored Bits channel <span class="tag preset" style="text-transform:none">${num(m.baud)} baud</span></h2>
    <p class="muted" style="margin-top:0">Test made with the first Mirrored Bits mode (before the emulator). Both ends sent one ${m.wire_period_ms} ms message after another for the whole test (${num(m.tx_msgs)} sent by the sending Pi, ${m.tx_rate} per second). These numbers are what the sending Pi received from the other Pi. ${esc(m.note || '')}</p>
    <div class="tiles">${card('Trips', num(m.trips), `${num(m.trips_lost)} lost`, m.trips_lost ? 'bad' : 'good')}${card('Messages received', num(a.msgs_good), 'valid from the other Pi')}
      ${card('Bad messages', num(bad), 'framing / copy / CRC', bad ? 'warn' : 'good')}${card('Lost messages', num(a.msgs_lost), 'sequence gaps', a.msgs_lost ? 'warn' : 'good')}
      ${card('Channel OK', pct(100 - (m.down_pct || 0)), `failed ${pct(m.down_pct || 0)} of the time`, m.down_pct ? 'warn' : 'good')}${card('Longest outage', (m.longest_outage_ms || 0) + ' ms', `${outs.length} outage(s)`, m.longest_outage_ms ? 'warn' : 'good')}
      ${card('Failure detected in', m.detect_ms_avg == null ? '–' : m.detect_ms_avg + ' ms', m.detect_ms_max == null ? `after ${m.fail_after} bad or missing in a row` : `worst ${m.detect_ms_max} ms`)}</div>
    ${m.one_way_note ? `<p class="muted">${esc(m.one_way_note)}</p>` : ''}
    ${outs.length ? `<div class="scroll"><table><thead><tr><th>Outage at</th><th>Length</th><th>Detected after</th></tr></thead><tbody>${outs.map(o => `<tr><td>${o.start_s} s</td><td>${o.dur_ms} ms${o.open ? ' (still failed at the end)' : ''}</td><td>${o.detect_ms == null ? '–' : o.detect_ms + ' ms'}</td></tr>`).join('')}</tbody></table></div>` : ''}</div>`;
};

const gooseCard = g => `<div class="card"><h2>GOOSE details ${tip('goose')}</h2><div class="grid g4">
  ${card('Events sent', num(g.events_tx), 'state changes')}${card('Events seen by the receiver', num(g.events_rx), '')}${card('Retransmissions', num(g.retx_rx), 'repeat frames received')}
  ${card('sqNum gaps', num(g.sq_gaps), 'lost repeat frames', g.sq_gaps ? 'warn' : 'good')}${card('TAL expiries', num(g.tal_expiries), 'link looked dead', g.tal_expiries ? 'bad' : 'good', 'tal')}
  ${card('Frame size', num(g.frame_size) + ' B', g.frame_size > 1400 ? 'too big for GRE' : 'fits GRE tunnel')}${card('VLAN tag seen', g.vlan_seen === null || g.vlan_seen === undefined ? '–' : g.vlan_seen ? 'yes' : 'no', g.prio_seen != null ? 'priority ' + g.prio_seen : '')}${card('Wrong source', num(g.foreign_frames), 'unexpected frames ignored')}</div></div>`;

async function charts(id, thr, ints, marks) {
  const msgs = await api(`/runs/${id}/messages?limit=200000`);
  const steady = msgs.filter(m => m.phase === 'steady');
  const BUCKETS = 1500, n = steady.length, step = Math.max(1, Math.ceil(n / BUCKETS));
  const xs = [], ys = [], thrs = [], lost = [];
  for (let i = 0; i < n; i += step) {
    let mx = null, lo = null; for (let j = i; j < Math.min(n, i + step); j++) { const m = steady[j]; if (m.rtt_us == null) lo = 1; else mx = Math.max(mx ?? 0, m.rtt_us / 1000); }
    xs.push(steady[i].t_send_ms / 1000); ys.push(mx); thrs.push(thr); lost.push(lo ? thr * 1.5 : null);
  }
  const axis = { stroke: css('--muted'), grid: { stroke: css('--line'), width: 1 }, ticks: { stroke: css('--line') } };
  const mk = (el, series, data, ylabel) => { if (!el || !window.uPlot) return; const p = new uPlot({ plugins: [markPlugin(marks)], width: Math.max(200, el.clientWidth), height: 260, padding: [8, 8, 0, 0], legend: { live: false }, scales: { x: { time: false }, y: { range: (u, mn, mx) => [0, Math.max(thr * 1.6, (mx || 0) * 1.1)] } }, axes: [{ ...axis, label: 'seconds' }, { ...axis, label: ylabel, size: 56 }], series: [{ label: 'Time' }, ...series] }, data, el); fitPlot(p, el); plots.push(p); };
  const line = (label, stroke, extra = {}) => ({ label, stroke, width: 1.6, points: { show: false }, ...extra });
  mk($('#rChart'), [line('Delay (worst in each slice)', css('--accent')), line('Limit', css('--fail'), { width: 2, dash: [8, 6] }), { label: 'Lost', stroke: css('--fail'), width: 0, points: { show: true, size: 9, fill: css('--fail') } }], [xs, ys, thrs, lost], 'ms');
  if (ints && ints.length) {
    const ix = ints.map(i => i.t_offset_s), f = k => ints.map(i => i[k] == null ? null : i[k] / 1000);
    mk($('#iChart'), [line('p50', css('--pass')), line('p95', css('--warn')), line('p99', css('--accent')), line('Slowest', css('--muted'), { width: 1 }), line('Limit', css('--fail'), { width: 2, dash: [8, 6] })], [ix, f('p50_us'), f('p95_us'), f('p99_us'), f('max_us'), ints.map(() => thr)], 'ms');
  } else if ($('#iChart')) $('#iChart').innerHTML = '<p class="muted">No per-second data.</p>';
  // histogram
  const vals = steady.filter(m => m.rtt_us != null).map(m => m.rtt_us / 1000);
  const h = $('#rHist'); if (!h) return; if (!vals.length) { h.innerHTML = '<p class="muted">No replies.</p>'; return; }
  const mx = Math.max(...vals, thr * 1.2), bins = 40, bw = mx / bins, cnt = new Array(bins).fill(0);
  vals.forEach(v => cnt[Math.min(bins - 1, Math.floor(v / bw))]++);
  const top = Math.max(...cnt);
  h.innerHTML = `<div class="hist">${cnt.map((c, i) => `<div class="${(i + 0.5) * bw > thr ? 'over' : ''}" style="height:${Math.max(c ? 2 : 0, c / top * 100)}%" title="${(i * bw).toFixed(1)}–${((i + 1) * bw).toFixed(1)} ms: ${c}"></div>`).join('')}</div>
    <div class="axis"><span>0 ms</span><span>limit ${thr} ms (red = over)</span><span>${mx.toFixed(0)} ms</span></div>`;
}


// ---------------------------------------------------------------- traffic generator run
async function showGen(box, r, id) {
  const s = r.summary || {}, v = s.verdict, p = r.profile || {}, t = s.totals || {}, rule = (p.gen || {}).rule || {};
  let banner;
  if (!v) banner = `<div class="banner fail"><div class="big">✕ NO RESULT</div><div>${esc(s.error || 'The test could not run.')}</div></div>`;
  else banner = `<div class="banner ${v.pass ? 'pass' : 'fail'}"><div class="big">${v.pass ? '✓ PASS' : '✕ FAIL'}</div><div><b>${esc(p.name || '')}</b> — ${s.steps ? esc(s.steps.headline) + '.' : v.pass ? 'the link carried this traffic within your limits.' : 'the link did not meet your limits.'}${s.stopped_early ? ' <small>(stopped early)</small>' : ''}${v.reasons.length ? '<ul>' + v.reasons.map(x => `<li>${esc(x)}</li>`).join('') + '</ul>' : ''}</div></div>`;
  const lat = t.lat_ms, c = s.clocks || {};
  const cards = t.tx_pkts !== undefined ? `<div class="grid g4">
    ${card('Sent', t.tx_mbps + ' Mb/s', `${num(t.tx_pkts)} packets · ${num(Math.round(t.tx_pps))}/s`)}${card('Received', t.rx_mbps + ' Mb/s', `${num(t.rx_pkts)} packets`, rule.min_throughput_mbps != null && t.rx_mbps < rule.min_throughput_mbps ? 'bad' : '')}
    ${card('Lost', num(t.lost), pct(t.loss_pct), t.lost ? 'bad' : 'good', 'loss')}${card('Delay p99', lat ? ms(lat.p99) : 'n/a', lat ? `typical ${ms(lat.p50)} · slowest ${ms(lat.max)}` : 'clocks not synced', '', 'One-way delay, sender to receiver.')}</div>` : '';
  const cn = Object.values(c.nodes || {}), offs = cn.map(n => `${esc(n.name)} ${n.offset_ms ?? '?'} ms`).join(' · ');
  const unsynced = cn.filter(n => !n.synced).map(n => n.name);
  const clk = lat ? `<p class="muted">One-way delay is shown because the clocks agree within ${c.limit_ms} ms (${offs || 'checked'}).</p>`
    : `<p class="muted">One-way delay is not shown: ${unsynced.length ? esc(unsynced.join(' and ')) + (unsynced.length > 1 ? ' have clocks that are' : ' has a clock that is') + ' not synced' : 'the clock offset is larger than ' + c.limit_ms + ' ms' + (offs ? ' (' + offs + ')' : '')}. Jitter and round-trip time are still valid.</p>`;
  const pairs = s.pairs || [];
  const pairTbl = pairs.length ? `<div class="card" data-testid="gen-pairs"><h2>Per pair of Pis</h2><div class="scroll"><table><thead><tr><th>From → to</th><th>Streams</th><th>Sent</th><th>Received</th><th>Lost</th><th>Sent Mb/s</th><th>Received Mb/s</th><th>Jitter ${tip('jitter')}</th><th>Delay typical / p99 / slowest</th></tr></thead><tbody>
    ${pairs.map(x => `<tr data-testid="gen-pair"><td><b>${esc(x.src)} → ${esc(x.dst)}</b></td><td>${x.streams}</td><td>${num(x.tx_pkts)}</td><td>${num(x.rx_pkts)}</td><td ${x.lost ? 'style="color:var(--fail);font-weight:700"' : ''}>${num(x.lost)} (${pct(x.loss_pct)})</td><td>${x.tx_mbps}</td><td>${x.rx_mbps}</td><td>${ms(x.jitter_ms)}</td><td>${x.lat_ms ? ms(x.lat_ms.p50) + ' / ' + ms(x.lat_ms.p99) + ' / ' + ms(x.lat_ms.max) : '–'}</td></tr>`).join('')}</tbody></table></div></div>` : '';
  const rows = (s.streams || []).map(x => `<tr><td><b>${esc(x.name)}</b>${x.warnings.map(w => `<br><small style="color:var(--warn)">${esc(w)}</small>`).join('')}</td><td>${esc(x.type.toUpperCase())}<br><small class="muted">${esc(x.src || '')} → ${esc(x.dst || '')}</small></td><td>${esc(x.size)}</td><td>${x.tx.mbps}</td><td>${x.rx.mbps}</td><td>${num(Math.round(x.rx.pps))}</td><td ${x.lost ? 'style="color:var(--fail);font-weight:700"' : ''}>${pct(x.loss_pct)}</td><td>${x.lat_ms ? ms(x.lat_ms.p50) + ' / ' + ms(x.lat_ms.p99) + ' / ' + ms(x.lat_ms.max) : '–'}</td><td>${ms(x.jitter_ms)}</td><td>${x.rtt_ms ? ms(x.rtt_ms.avg) : '–'}</td><td>${x.rx.dup + x.rx.ooo ? `${x.rx.dup} dup · ${x.rx.ooo} reordered` : '–'}</td></tr>`).join('');
  box.innerHTML = `${banner}${testerLine(s.tester)}${marksLine(s)}${vsLine(r)}${Job.block(r.job)}${pathNote(s)}${s.steps ? '<div id="stepsRes"></div>' : ''}${cards}${clk}${pairTbl}
    <div class="card"><h2>Streams</h2><div class="scroll"><table><thead><tr><th>Stream</th><th>Type</th><th>Size</th><th>Sent Mb/s</th><th>Received Mb/s</th><th>Packets/s</th><th>Lost</th><th>Delay typical / p99 / slowest</th><th>Jitter ${tip('jitter')}</th><th>Round trip</th><th>Duplicates / order</th></tr></thead><tbody>${rows}</tbody></table></div></div>
    <div class="grid g2"><div class="card"><h2>Throughput per second</h2><div id="gr1" class="chart"></div></div><div class="card"><h2>Delay and jitter per second</h2><div id="gr2" class="chart"></div></div></div>
    <div class="card"><h2>Export</h2><div class="row">${s.steps ? `<a class="btn small" href="/api/runs/${id}/export.csv?kind=steps" download data-testid="steps-csv">Steps (CSV)</a>` : ''}<a class="btn small" href="/api/runs/${id}/export.csv?kind=gen" download>Per second (CSV)</a><a class="btn small" href="/api/runs/${id}/export.json" download>Everything (JSON)</a><a class="btn small" href="/api/report.pdf?run_ids=${id}" download>📄 PDF report</a>${cmpBtns(r)}<button class="btn small ghost" id="delRun">Delete this test</button></div>
      <p class="muted">${esc(r.profile.name || '')} · ${(s.streams || []).length} stream(s) · ${when(r.started_at)} · Pis: ${esc((s.nodes || []).join(', ') || r.port || '')}</p></div>`;
  $('#delRun').onclick = async () => { if (await confirmBox('Delete this test?', 'Its data is removed permanently.', 'Delete', true)) { await api(`/runs/${id}`, { method: 'DELETE' }); S.openRun = null; renderResults($('#main').firstElementChild || $('#main')); } };
  if (s.steps) (await import('./steps.js')).Steps.result($('#stepsRes'), r);      // throughput in steps: headline, one row per step, got against sent
  bindExtras(box, r);
  const ints = r.intervals || []; if (!ints.length || !window.uPlot) return;
  const by = {}; for (const i of ints) { const d = (by[i.t_offset_s] ??= { tx: 0, rx: 0, lat: [], jit: [] }); d.tx += i.tx_bytes * 8 / 1e6; d.rx += i.rx_bytes * 8 / 1e6; if (i.lat_avg_us != null) d.lat.push(i.lat_avg_us / 1000); if (i.jitter_us != null) d.jit.push(i.jitter_us / 1000); }
  const ts = Object.keys(by).map(Number).sort((a, b) => a - b), avg = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
  const axis = { stroke: css('--muted'), grid: { stroke: css('--line'), width: 1 }, ticks: { stroke: css('--line') } };
  const mk = (el, series, data, label) => { const pl = new uPlot({ width: Math.max(200, el.clientWidth), height: 240, padding: [8, 8, 0, 0], scales: { x: { time: false }, y: { range: (u, a, b) => [0, Math.max(1, (b || 0) * 1.15)] } }, axes: [{ ...axis, label: 'seconds' }, { ...axis, label, size: 56 }], series: [{ label: 'Time' }, ...series] }, data, el); fitPlot(pl, el); plots.push(pl); };
  const ln = (label, stroke) => ({ label, stroke, width: 1.7, points: { show: false } });
  mk($('#gr1'), [ln('Sent', css('--muted')), ln('Received', css('--accent'))], [ts, ts.map(t => by[t].tx), ts.map(t => by[t].rx)], 'Mb/s');
  if (ts.some(t => by[t].lat.length)) mk($('#gr2'), [ln('Delay (avg)', css('--accent')), ln('Jitter', css('--warn'))], [ts, ts.map(t => avg(by[t].lat)), ts.map(t => by[t].jit.length ? Math.max(...by[t].jit) : null)], 'ms');
  else $('#gr2').innerHTML = '<p class="muted">One-way delay needs synced clocks; jitter is not available for this run.</p>';
}


// ---------------------------------------------------------------- traffic sniffer capture
async function showSniff(box, r, id) {
  const s = r.summary || {}, a = s.analysis || {}, p = r.profile || {};
  const modes = { eth_bridge: 'Inline Ethernet tap', eth_span: 'SPAN / mirror port', serial: 'Serial tap', wifi: 'Wi-Fi monitor' };
  const kb = n => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
  const banner = s.error && !(s.files || []).length ? `<div class="banner fail"><div class="big">✕ NO CAPTURE</div><div>${esc(s.error)}</div></div>`
    : `<div class="banner ${s.stop_reason === 'disk_full' ? 'run' : 'pass'}"><div class="big">${s.stop_reason === 'disk_full' ? '■ STOPPED' : '✓ SAVED'}</div><div><b>${esc(p.name || '')}</b> — ${esc(modes[s.mode] || '')}, ${dur(s.elapsed_s || 0)}.${s.error ? `<br><small>${esc(s.error)}</small>` : ''}</div></div>`;
  const cards = a.packets !== undefined ? `<div class="grid g4">${card('Packets', num(a.packets), a.pps ? `${num(a.pps)} per second` : '')}${card('Data', kb(a.bytes || 0), a.mbps ? `${a.mbps} Mb/s average` : '')}
    ${card('Errors', num(a.errors || 0), `${num(a.crc_failures || 0)} CRC failures`, (a.errors || a.crc_failures) ? 'bad' : 'good')}${card('Longest quiet gap', ms(a.max_gap_ms), 'between two packets')}</div>` : '';
  const tbl = (h, rows) => `<div class="scroll"><table><thead><tr>${h.map(x => `<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
  const prot = a.protocols ? `<div class="card"><h2>Protocols</h2>${tbl(['Protocol', 'Packets', 'Share'], Object.entries(a.protocols).map(([k, v]) => `<tr><td><b>${esc(k)}</b></td><td>${num(v)}</td><td>${(100 * v / a.packets).toFixed(1)} %</td></tr>`))}</div>` : '';
  const talk = (a.top_talkers || []).length ? `<div class="card"><h2>Top talkers</h2>${tbl(['Source → destination', 'Packets', 'Data'], a.top_talkers.map(t => `<tr><td>${esc(t.pair)}</td><td>${num(t.packets)}</td><td>${kb(t.bytes)}</td></tr>`))}</div>` : '';
  const ind = (a.dnp3_frames || a.modbus_frames) ? `<div class="card"><h2>DNP3 and Modbus</h2><div class="grid g4">${card('DNP3 frames', num(a.dnp3_frames))}${card('Modbus frames', num(a.modbus_frames))}${card('CRC ok', num(a.crc_ok), '', 'good')}${card('CRC failed', num(a.crc_failures), '', a.crc_failures ? 'bad' : 'good')}</div>
    ${Object.keys(a.functions || {}).length ? tbl(['Function', 'Frames'], Object.entries(a.functions).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${num(v)}</td></tr>`)) : ''}</div>` : '';
  const wifi = (a.wifi_networks || []).length ? `<div class="card"><h2>Wi-Fi networks heard</h2>${tbl(['Network (SSID)', 'BSSID', 'Channel', 'Best signal', 'Beacons'], a.wifi_networks.map(n => `<tr><td>${esc(n.ssid || '(hidden)')}</td><td>${esc(n.bssid)}</td><td>${n.channel ?? '–'}</td><td>${n.signal != null ? n.signal + ' dBm' : '–'}</td><td>${n.beacons}</td></tr>`))}</div>` : '';
  const files = `<div class="card"><h2>Files</h2>${tbl(['File', 'Size', ''], (s.files || []).map(f => `<tr><td>${esc(f.name)}</td><td>${kb(f.bytes)}</td><td><a class="btn small" href="/api/runs/${id}/capture/${encodeURIComponent(f.name)}" download>⤓ Download</a></td></tr>`))}
    <p class="muted">Stored in ${esc(s.location || '')}. Open the .pcapng files in Wireshark. ${esc(s.ring_note || '')}</p>
    ${s.serial ? `<p class="muted">${esc(s.serial.timestamp_note)}</p>` : ''}${a.truncated ? '<p class="muted">The summary covers only the first part of a very large capture.</p>' : ''}
    <div class="row"><button class="btn small" id="toUsb">💾 Copy to USB drive</button><a class="btn small" href="/api/runs/${id}/export.json" download>Summary (JSON)</a><a class="btn small" href="/api/report.pdf?run_ids=${id}" download>📄 PDF report</a><button class="btn small ghost" id="delRun">Delete this capture</button></div></div>`;
  const lost = s.engine && s.engine.kernel_dropped > 0 ? `<div class="check warn" data-testid="capture-lost"><div class="ic">!</div><div><b>The capture is not complete: the system dropped ${num(s.engine.kernel_dropped)} packets.</b> The Pi could not save them fast enough. Capture with a filter, a short snap length (headers only), or to a USB drive.</div></div>` : '';
  const eng = (s.engine ? `<p class="muted">Capture engine: ${esc(s.engine.engine)}${s.engine.kernel_dropped != null ? ` · packets the system dropped: ${s.engine.kernel_dropped}` : ''}.</p>` : '');
  box.innerHTML = `${banner}${lost}${Job.block(r.job)}${cards}${prot}${talk}${ind}${wifi}${files}${eng}`;
  bindExtras(box, r);
  $('#toUsb').onclick = async () => { try { const x = await api(`/runs/${id}/capture/usb`, { method: 'POST' }); toast(`Copied ${x.files} file(s) to ${x.path}`); } catch (e) { toast(e.message, true, 7000); } };
  $('#delRun').onclick = async () => { if (await confirmBox('Delete this capture?', 'The record is removed. The capture files stay on the storage.', 'Delete', true)) { await api(`/runs/${id}`, { method: 'DELETE' }); S.openRun = null; renderResults($('#main').firstElementChild || $('#main')); } };
}
