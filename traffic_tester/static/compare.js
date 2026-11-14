// Compare runs (SPEC 7.5): 2 to 4 results of the same kind side by side: the headline numbers with the change against A, one overlaid chart,
// one sentence in words, what differed in the setup, Set as baseline and the one-page PDF.
//   renderCompare(root, ids, { onBack })   draws the page into `root` (the Results screen hands over its own root and gets it back through onBack)
//   pickState(runs)                         what the Select bar says for the ticked rows of the results list: { ok, text }
//   ensureCompareCss()                      loads compare.css once (the results list and the job block use it too)
import { $, $$, esc, api, toast, when, fitPlot } from './ui.js';
import { jobText } from './job.js';

export const KIND_NAMES = { trip: 'trip test', generator: 'traffic generator test', mirrored: 'Mirrored Bits test', sniffer: 'capture' };
// The four series colours (A to D), fixed order, checked for colour-blind separation on the light and on the dark panel.
const SERIES = { light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100'], dark: ['#3987e5', '#d95926', '#199e70', '#c98500'] };
const colours = () => SERIES[document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'];
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
let plots = [];

export function ensureCompareCss() {
  if (document.querySelector('link[data-compare-css]')) return;
  const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = 'compare.css'; l.dataset.compareCss = '1'; document.head.appendChild(l);
}

// The Select bar of the results list. `runs` = the ticked rows ({ id, kind, status }).
export function pickState(runs) {
  const n = runs.length, kinds = new Set(runs.map(r => r.kind));
  if (n === 0) return { ok: false, text: 'Tick 2 to 4 results of the same kind.' };
  if (kinds.size > 1) return { ok: false, text: 'Pick results of the same kind.', warn: true };
  if (n === 1) return { ok: false, text: `1 picked. Tick one more ${KIND_NAMES[runs[0].kind] || 'result'}.` };
  if (n > 4) return { ok: false, text: 'Pick at most 4 results.', warn: true };
  return { ok: true, text: `${n} picked.` };
}

const fmt = (v, unit = '') => {
  if (v == null) return '–';
  let t;
  if (Number.isInteger(v)) t = v.toLocaleString();
  else { const a = Math.abs(v); t = (a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2)).replace(/\.?0+$/, ''); }
  return unit ? `${t} ${unit}` : t;
};
const changeHtml = (ch, unit) => {
  if (!ch) return '<span class="muted">–</span>';
  if (ch.state === 'same') return '<span class="muted">same</span>';
  const word = ch.judged ? `<small>${ch.judged}</small>` : '';
  return `<b class="chg-${ch.judged || 'plain'}">${ch.abs > 0 ? '+' : '−'}${esc(fmt(Math.abs(ch.abs), unit))}</b>${word}`;
};

export async function renderCompare(root, ids, { onBack } = {}) {
  ensureCompareCss();
  plots.forEach(p => p.destroy()); plots = [];
  let c;
  try { c = await api(`/compare?run_ids=${ids.join(',')}`); }
  catch (e) {
    root.innerHTML = `<div class="card" data-testid="compare-error"><h2>Compare</h2><p>${esc(e.message)}</p><button type="button" class="btn" id="cmpBack" data-testid="compare-back">‹ Back to results</button></div>`;
    $('#cmpBack', root).onclick = () => onBack && onBack();
    return;
  }
  const L = c.runs.map(r => r.letter), two = L.length === 2, col = colours();
  const chip = (r, i) => `<div class="cmp-run" data-testid="compare-run-${r.letter}"><span class="cmp-chip" style="--c:${col[i]}"><i></i>${r.letter}${r.baseline ? ' · Baseline' : ''}</span>
      <div><b>${esc(r.name || 'Test')}</b> <span class="pill ${r.verdict === 'PASS' ? 'pass' : r.verdict === 'FAIL' ? 'fail' : 'na'}">${esc(r.verdict || (r.status || '').toUpperCase())}</span>
      <small class="muted">${esc([r.route, when(r.started_at)].filter(Boolean).join(' · '))}${jobText(r.job) ? `<br>${esc(jobText(r.job))}` : ''}${r.job && r.job.note ? `<br>${esc(r.job.note)}` : ''}</small></div></div>`;
  const head = `<tr><th>${esc(c.kind_name)}</th>${L.map((l, i) => `<th><span class="cmp-dot" style="--c:${col[i]}"></span>${l}</th>`).join('')}${two ? '<th>Change</th>' : ''}</tr>`;
  const body = c.rows.map(r => `<tr data-testid="compare-row-${r.key}"><td>${esc(r.label)}${r.unit ? ` <small class="muted">${esc(r.unit)}</small>` : ''}</td>${r.values.map((v, i) =>
    `<td>${esc(fmt(v))}${!two && i > 0 ? `<div class="cmp-sub">${changeHtml(r.change[i], '')}</div>` : ''}</td>`).join('')}${two ? `<td class="cmp-chg">${changeHtml(r.change[1], '')}</td>` : ''}</tr>`).join('');
  const verdictRow = `<tr><td>Result</td>${c.runs.map(r => `<td><span class="${r.verdict === 'PASS' ? 'cmp-pass' : r.verdict === 'FAIL' ? 'cmp-fail' : 'muted'}">${esc(r.verdict || '–')}</span></td>`).join('')}${two ? `<td class="muted">${c.runs[0].verdict === c.runs[1].verdict ? 'same' : 'changed'}</td>` : ''}</tr>`;
  const base = c.runs.find(r => r.baseline), A = c.runs[0];
  const ch = c.chart;
  root.innerHTML = `<div class="cmp" data-testid="compare-page">
    <div class="row sp cmp-top"><button type="button" class="btn small" id="cmpBack" data-testid="compare-back">‹ Back to results</button><h2 style="margin:0">Compare</h2><span></span></div>
    <div class="card cmp-runs">${c.runs.map(chip).join('')}</div>
    <p class="cmp-say" data-testid="compare-sentence">${esc(c.sentence)}</p>
    <div class="card cmp-tbl"><div class="scroll"><table data-testid="compare-table" class="${two ? 'two' : 'many'}"><thead>${head}</thead><tbody>${body}${verdictRow}</tbody></table></div>
      ${two ? '' : '<p class="muted cmp-note">The small figure under B, C and D is the change against A.</p>'}</div>
    ${ch ? `<div class="card"><h2>${esc(ch.type === 'hist' ? ch.title : 'Received per second')}</h2><div id="cmpChart" class="chart" data-testid="compare-chart"></div>
      ${ch.type === 'lines' ? '<h2 style="margin-top:14px">Delay per second</h2><div id="cmpChart2" class="chart" data-testid="compare-chart-delay"></div>' : ''}
      <p class="muted cmp-note">${esc(ch.note)}</p></div>` : '<div class="card"><p class="muted">These results have no stored messages to draw.</p></div>'}
    <div class="card cmp-setup" data-testid="compare-setup"><b>Setup differences</b><p>${esc(c.setup_line)}</p></div>
    <div class="card cmp-acts"><div class="tools">
      <button type="button" class="btn small" id="cmpBase" data-testid="compare-baseline">${base && base.id === A.id ? 'Baseline: A ✓' : 'Set A as baseline'}</button>
      <a class="btn small primary" href="/api/compare.pdf?run_ids=${ids.join(',')}" download data-testid="compare-pdf">📄 PDF comparison</a></div>
      <p class="muted cmp-note">${base && base.id === A.id ? `Later runs of “${esc(A.name)}” show their change against A under the verdict.` : `Makes A the baseline of “${esc(A.name)}”: later runs of that test show “vs baseline” under the verdict.`}</p></div></div>`;
  $('#cmpBack', root).onclick = () => { plots.forEach(p => p.destroy()); plots = []; onBack && onBack(); };
  $('#cmpBase', root).onclick = async () => {
    try {
      if (base && base.id === A.id) { await api(`/runs/${A.id}/baseline`, { method: 'DELETE' }); toast('Baseline removed.'); }
      else { await api(`/runs/${A.id}/baseline`, { method: 'POST' }); toast(`A is now the baseline of “${A.name}”.`); }
      renderCompare(root, ids, { onBack });
    } catch (e) { toast(e.message, true, 6000); }
  };
  if (ch && window.uPlot) draw(ch, L, col);
}

function draw(ch, L, col) {
  const axis = { stroke: css('--muted'), grid: { stroke: css('--line'), width: 1 }, ticks: { stroke: css('--line') } };
  const line = (label, i) => ({ label, stroke: col[i], width: 2, points: { show: false }, spanGaps: true });
  const mk = (el, xlabel, ylabel, series, data, opts = {}) => {
    if (!el) return;
    const p = new uPlot({ width: Math.max(200, el.clientWidth), height: 240, padding: [10, 12, 0, 0], legend: { live: true }, cursor: { points: { size: 7 } },
      scales: { x: { time: false }, y: { range: opts.yrange || ((u, a, b) => [0, Math.max(1, (b || 0) * 1.15)]) } },
      axes: [{ ...axis, label: xlabel }, { ...axis, label: ylabel, size: 52 }], series: [{ label: xlabel }, ...series], hooks: opts.hooks || {} }, data, el);
    fitPlot(p, el); plots.push(p);
  };
  if (ch.type === 'hist') {
    const limit = ch.limit_ms, fail = css('--fail');
    const mark = u => {                                   // the delay limit, as on every other chart: a dashed red line
      if (limit == null || limit > u.scales.x.max) return;
      const x = Math.round(u.valToPos(limit, 'x', true)), c = u.ctx;
      c.save(); c.strokeStyle = fail; c.lineWidth = 2 * devicePixelRatio; c.setLineDash([8 * devicePixelRatio, 6 * devicePixelRatio]);
      c.beginPath(); c.moveTo(x, u.bbox.top); c.lineTo(x, u.bbox.top + u.bbox.height); c.stroke();
      c.setLineDash([]); c.fillStyle = fail; c.font = `${11 * devicePixelRatio}px system-ui, sans-serif`; c.textAlign = 'right';
      c.fillText(`limit ${limit} ms`, x - 6 * devicePixelRatio, u.bbox.top + 12 * devicePixelRatio); c.restore();
    };
    mk($('#cmpChart'), 'delay, ms', '% of messages', ch.series.map((s, i) => line(`${s.letter} (${s.n.toLocaleString()} messages)`, i)), [ch.edges, ...ch.series.map(s => s.cum)],
      { yrange: () => [0, 100], hooks: { draw: [mark] } });
    return;
  }
  // generator runs: every test starts at 0 s; one chart per measure (never two scales on one chart)
  const ts = [...new Set(ch.series.flatMap(s => s.t))].sort((a, b) => a - b);
  const col2 = k => ch.series.map(s => { const m = new Map(s.t.map((t, i) => [t, s[k][i]])); return ts.map(t => m.has(t) ? m.get(t) : null); });
  mk($('#cmpChart'), 'seconds', 'Mb/s', ch.series.map((s, i) => line(`${s.letter} received`, i)), [ts, ...col2('rx_mbps')]);
  const el2 = $('#cmpChart2');
  if (ch.series.some(s => s.lat_ms.some(v => v != null))) mk(el2, 'seconds', 'ms', ch.series.map((s, i) => line(`${s.letter} delay`, i)), [ts, ...col2('lat_ms')]);
  else if (el2) el2.innerHTML = '<p class="muted">One-way delay needs synced clocks; none of these runs had it.</p>';
}
