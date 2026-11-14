// Throughput in steps (docs/SPEC.md 13.9): the method switch (Steps / Flood) for a traffic form, the live step table and the result.
// Self-contained: mount Steps.form() in any form that edits a generator profile's `gen` block.
import { esc, num, pct, ms, tip, fitPlot } from './ui.js';

export const FRACTIONS = [0.125, 0.25, 0.5, 0.75, 1.0];
const SHARE = { 0.125: '1/8', 0.25: '1/4', 0.5: '1/2', 0.75: '3/4', 1: 'all' };
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

// 4, 0.5, 12.5, 94.3, 1000 (same rounding as the server's fmt_mbps)
export function mbps(v) {
  if (v == null || isNaN(v)) return '?';
  if (v >= 100) return String(Math.round(v));
  return String(parseFloat(v.toFixed(v >= 10 ? 1 : v >= 0.1 ? 2 : 3)));
}
export const defaults = () => ({ enabled: true, top_mbps: null, fractions: [...FRACTIONS], step_s: 10, stop_after_fail: 2, flood_s: 5, flood_mbps: 1000 });
const stepsOf = gen => (gen.steps ??= { ...defaults(), enabled: false });
export const isOn = gen => !!(gen && gen.steps && gen.steps.enabled);
// Why steps cannot be used with these streams ('' = they can).
export function blocker(gen) {
  const ss = (gen && gen.streams) || [];
  if (ss.some(s => s.burst_gap_ms > 0)) return 'Steps need a steady rate: set “Gap between bursts” to 0 first.';
  if (ss.some(s => s.packets)) return 'Steps run every stream for the length of a step: clear “Or stop after (packets)” first.';
  return '';
}
const cleanText = rule => { const r = rule || {}, a = []; if (r.max_loss_pct != null) a.push(`lost ≤ ${r.max_loss_pct} %`); if (r.max_latency_ms != null) a.push(`${r.latency_percentile === 100 ? 'slowest' : 'p' + (r.latency_percentile || 99)} delay ≤ ${r.max_latency_ms} ms`); return a.length ? a.join(' and ') : 'something arrives'; };
// How long the whole test takes, in seconds (flood + steps + the short pauses between them).
export function totalSeconds(gen) { const s = stepsOf(gen); return Math.round((s.top_mbps ? 0 : s.flood_s + 2) + s.fractions.length * (s.step_s + 2)); }

// One line for a closed row / a test card: "Steps: 1 to 8 Mb/s in 5 steps of 10 s" · "Steps: top rate measured first (5 s flood), then 5 steps of 10 s" · "Flood: 30 s at 10 Mb/s".
export function summary(gen) {
  if (!gen) return '';
  const s = gen.steps;
  if (!s || !s.enabled) { const tot = (gen.streams || []).reduce((a, x) => a + (x.rate && x.rate.mbps || 0), 0); return `Flood: ${gen.duration_s} s${tot ? ` at ${mbps(tot)} Mb/s` : ''}`; }
  const n = s.fractions.length;
  return s.top_mbps ? `Steps: ${mbps(s.top_mbps * s.fractions[0])} to ${mbps(s.top_mbps)} Mb/s in ${n} steps of ${s.step_s} s`
    : `Steps: top rate measured first (${s.flood_s} s flood), then ${n} steps of ${s.step_s} s`;
}

// The method switch + top rate, written into gen.steps (and gen.rule.min_clean_mbps). opts.onChange(gen) after every change.
export function form(container, gen, opts = {}) {
  const onChange = opts.onChange || (() => { });
  const draw = () => {
    const s = stepsOf(gen), why = blocker(gen), on = s.enabled && !why, auto = !s.top_mbps;
    const rates = s.top_mbps ? s.fractions.map(f => mbps(s.top_mbps * f)).join(' · ') + ' Mb/s' : s.fractions.map(f => SHARE[f] || f).join(' · ') + ' of the top rate';
    container.innerHTML = `<div class="stepsform" data-testid="steps-form">
      <div class="f"><span>Method ${tip('Steps sends the same traffic at a rising rate and shows the highest rate the link carries cleanly. Flood sends at one rate for the whole test and reports what got through.')}</span>
        <div class="seg" role="radiogroup" aria-label="Throughput method">
          <button type="button" class="${on ? 'on' : ''}" role="radio" aria-checked="${on}" data-steps="on" data-testid="steps-method-steps" ${why ? 'disabled' : ''}>Steps (find the clean rate)</button>
          <button type="button" class="${on ? '' : 'on'}" role="radio" aria-checked="${!on}" data-steps="off" data-testid="steps-method-flood">Flood (as fast as possible)</button></div></div>
      ${why ? `<p class="why muted" data-testid="steps-blocked">${esc(why)}</p>` : ''}
      ${on ? `<div class="grid g3" style="margin-top:8px">
        <label class="f"><span>Top rate ${tip('The highest rate to try. Auto sends a short flood first and uses what arrived.')}</span>
          <div class="seg"><button type="button" class="${auto ? 'on' : ''}" data-steps="auto" data-testid="steps-top-auto">Auto</button><button type="button" class="${auto ? '' : 'on'}" data-steps="typed" data-testid="steps-top-typed">Mb/s</button></div></label>
        ${auto ? '' : `<label class="f"><span>Top rate (Mb/s)</span><input type="number" min="0.01" max="10000" step="any" inputmode="decimal" data-steps="top" data-testid="steps-top-mbps" value="${esc(s.top_mbps)}"></label>`}
        <label class="f"><span>Needs at least (Mb/s clean) ${tip('Optional. The test fails when the highest clean rate is below this.')}</span><input type="number" min="0" step="any" inputmode="decimal" placeholder="no limit" data-steps="need" data-testid="steps-min-clean" value="${esc((gen.rule || {}).min_clean_mbps ?? '')}"></label></div>
        <p class="muted" style="margin:8px 0 0" data-testid="steps-explain">${auto ? `A ${s.flood_s} s flood measures the top rate first. Then ` : ''}${s.fractions.length} steps of ${s.step_s} s: ${esc(rates)}. A step is clean when ${esc(cleanText(gen.rule))}. Stops after ${s.stop_after_fail} failed steps in a row. About ${totalSeconds(gen)} s in all.</p>` : ''}
    </div>`;
  };
  const set = fn => { fn(stepsOf(gen)); draw(); onChange(gen); };
  container.onclick = ev => {
    const b = ev.target.closest('button[data-steps]'); if (!b || b.disabled) return;
    const k = b.dataset.steps;
    if (k === 'on') set(s => { Object.assign(s, { ...defaults(), ...s, enabled: true }); });
    else if (k === 'off') set(s => { s.enabled = false; });
    else if (k === 'auto') set(s => { s.top_mbps = null; });
    else if (k === 'typed') set(s => { s.top_mbps = s.top_mbps || Math.max(0.01, (gen.streams || []).reduce((a, x) => a + (x.rate && x.rate.mbps || 0), 0) || 10); });
  };
  container.onchange = ev => {
    const el = ev.target, k = el.dataset && el.dataset.steps; if (!k) return;
    const v = el.value === '' ? null : Number(el.value);
    if (k === 'top') set(s => { s.top_mbps = v && v > 0 ? v : null; });
    else if (k === 'need') { (gen.rule ??= {}).min_clean_mbps = v && v > 0 ? v : null; onChange(gen); }
  };
  draw();
  return { redraw: draw };
}

// ------------------------------------------------------------------ the step table (live and result)
const mark = r => r.clean === true ? '<b class="ok" title="clean">✓</b>' : r.clean === false ? '<b class="bad" title="not clean">✗</b>' : r.state === 'running' ? '<span class="muted">…</span>' : '<span class="muted">–</span>';
function rowHtml(r) {
  const run = r.state === 'done' || r.state === 'stopped';
  const sub = r.state === 'skipped' ? 'not sent' : r.state === 'stopped' ? 'stopped before it ended' : r.state === 'running' ? 'sending now' : r.state === 'waiting' ? '' : [r.why, r.note].filter(Boolean).join(' · ');
  return `<tr data-testid="step-row" data-n="${r.n}" data-state="${esc(r.state)}" data-clean="${r.clean}" class="${r.clean === false ? 'failrow' : r.state === 'running' ? 'nowrow' : ''}">
    <td><b>${r.n}</b></td><td><b>${r.asked_mbps != null ? mbps(r.asked_mbps) : '?'}</b> <small class="muted">Mb/s</small>${sub ? `<br><small class="${r.clean === false ? 'bad' : 'muted'}">${esc(sub)}</small>` : ''}</td>
    <td>${run ? mbps(r.sent_mbps) : '–'}</td><td>${run ? mbps(r.got_mbps) : '–'}</td>
    <td ${run && r.lost ? 'class="bad"' : ''}>${run ? `${pct(r.loss_pct)}<br><small class="muted">${num(r.lost)} of ${num(r.tx_pkts)}</small>` : '–'}</td>
    <td>${run && r.delay_ms != null ? ms(r.delay_ms) : '–'}</td><td class="stepmark">${mark(r)}</td></tr>`;
}
function tableHtml(rows, pctl) {
  const d = pctl === 100 ? 'Slowest' : 'p' + (pctl || 99) + ' delay';
  return `<div class="scroll"><table class="steps" data-testid="steps-table"><thead><tr><th>Step</th><th>Rate asked</th><th>Sent Mb/s</th><th>Got Mb/s</th><th>Lost</th><th>${d}</th><th>Clean</th></tr></thead><tbody>${rows.map(rowHtml).join('')}</tbody></table></div>`;
}
const limitLine = l => l ? `A step is clean when ${esc(cleanText(l))}.` : '';

// Live: `e` = the newest "gstate" event of a run in steps (it carries e.step and e.steps). Returns false when the run is not in steps.
export function live(container, e) {
  const sp = e && e.steps; if (!sp) { if (container) container.innerHTML = ''; return false; }
  const now = e.step, rows = [...sp.rows];
  const failsStop = sp.rows.slice(-2).length === 2 && sp.rows.slice(-2).every(r => r.clean === false) && !now;
  for (let n = rows.length + 1; n <= sp.of; n++) {
    const running = now && now.phase === 'step' && now.n === n;
    rows.push({ n, of: sp.of, asked_mbps: running ? now.mbps : (sp.top_mbps ? sp.top_mbps * ((sp.fractions || FRACTIONS)[n - 1] ?? 0) || null : null), state: running ? 'running' : failsStop ? 'skipped' : 'waiting', clean: null });
  }
  const head = now ? now.text : rows.length && rows.every(r => r.state !== 'waiting' && r.state !== 'running') ? 'Steps finished' : 'Between steps…';
  const top = sp.top_mbps ? `Top rate ${mbps(sp.top_mbps)} Mb/s${sp.auto ? ` (measured: ${mbps(sp.flood ? sp.flood.got_mbps : sp.top_mbps)} Mb/s arrived in the flood)` : ''}` : 'Top rate: being measured';
  container.innerHTML = `<div class="card" data-testid="steps-live"><div class="row sp"><h2 style="margin:0" data-testid="steps-progress">${esc(head)}</h2><span class="muted">${esc(top)}</span></div>
    <p class="muted" style="margin:4px 0 10px">${limitLine(sp.limit)}</p>${tableHtml(rows, (sp.limit || {}).latency_percentile)}</div>`;
  return true;
}
// The progress line alone ("Step 3 of 5 · 4 Mb/s"), for a phase label.
export const progressText = e => (e && e.step && e.step.text) || (e && e.steps ? 'Between steps…' : '');

// Result: `run` = a stored run (GET /api/runs/<id>) or just its summary. Draws headline + table + chart; returns false when the run was not in steps.
export function result(container, run, opts = {}) {
  const s = (run && run.summary) || run || {}, sp = s.steps;
  if (!sp) { if (container) container.innerHTML = ''; return false; }
  const need = (((run && run.profile) || {}).gen || {}).rule;
  const needLine = need && need.min_clean_mbps != null ? ` · needs at least ${mbps(need.min_clean_mbps)} Mb/s clean` : '';
  const flood = sp.flood ? `<p class="muted" data-testid="steps-flood">Top rate ${mbps(sp.top_mbps)} Mb/s, measured by a ${sp.flood.duration_s} s flood (${mbps(sp.flood.sent_mbps)} Mb/s sent, ${mbps(sp.flood.got_mbps)} Mb/s arrived).</p>`
    : `<p class="muted">Top rate ${mbps(sp.top_mbps)} Mb/s (typed).</p>`;
  container.innerHTML = `<div class="card" data-testid="steps-result">
    <div class="row sp"><div><small class="muted">Throughput in steps${esc(needLine)}</small><h2 style="margin:2px 0 0;font-size:1.5rem" class="${sp.clean_step ? 'ok' : 'bad'}" data-testid="steps-headline">${esc(sp.headline)}</h2></div></div>
    ${flood}<p class="muted" style="margin:4px 0 10px">${limitLine(sp.limit)} ${sp.step_s} s per step.</p>
    ${tableHtml(sp.rows, (sp.limit || {}).latency_percentile)}
    ${(sp.notes || []).map(n => `<p class="muted" style="margin:8px 0 0">${esc(n)}</p>`).join('')}
    ${opts.chart === false ? '' : '<h3 style="margin:14px 0 4px">What arrived at each rate</h3><div class="chart" data-testid="steps-chart"></div>'}</div>`;
  const el = container.querySelector('.chart');
  if (el) chart(el, sp);
  return true;
}

// Got against sent (Mb/s), with the delay on the right-hand axis; the dashed line is "everything arrived".
export function chart(el, sp) {
  const rows = (sp.rows || []).filter(r => r.state === 'done' || r.state === 'stopped');
  if (!rows.length || !window.uPlot) { el.innerHTML = '<p class="muted">No finished step to draw.</p>'; return null; }
  const xs = rows.map(r => r.sent_mbps), hasDelay = rows.some(r => r.delay_ms != null);
  const axis = { stroke: css('--muted'), grid: { stroke: css('--line'), width: 1 }, ticks: { stroke: css('--line') } };
  const series = [{ label: 'Sent (Mb/s)' }, { label: 'Got (Mb/s)', stroke: css('--accent'), width: 2, points: { show: true, size: 8 } }, { label: 'Everything arrives', stroke: css('--muted'), width: 1, dash: [5, 5], points: { show: false } }];
  const data = [xs, rows.map(r => r.got_mbps), xs];
  const axes = [{ ...axis, label: 'sent, Mb/s' }, { ...axis, label: 'got, Mb/s', size: 56 }];
  const scales = { x: { time: false }, y: { range: (u, a, b) => [0, Math.max(0.1, (b || 0) * 1.15)] } };
  if (hasDelay) {
    series.push({ label: `p${(sp.limit || {}).latency_percentile || 99} delay (ms)`, stroke: css('--warn'), width: 1.6, scale: 'ms', points: { show: true, size: 6 } });
    data.push(rows.map(r => r.delay_ms));
    axes.push({ ...axis, scale: 'ms', side: 1, label: 'delay, ms', size: 56, grid: { show: false } });
    scales.ms = { range: (u, a, b) => [0, Math.max(1, (b || 0) * 1.3)] };
  }
  const p = new uPlot({ width: Math.max(200, el.clientWidth), height: 240, padding: [8, 8, 0, 0], scales, axes, series }, data, el);
  fitPlot(p, el);
  return p;
}

export const Steps = { form, summary, live, result, chart, isOn, blocker, defaults, totalSeconds, progressText, mbps, FRACTIONS };
export default Steps;
