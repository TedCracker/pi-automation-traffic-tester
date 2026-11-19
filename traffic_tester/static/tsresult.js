// The result of a test set: verdict, "Measured against target" (one row per application), "Alone, then together", and the Report block. Used on the Tests
// screen (the finished run) and on Results (the archive), so both show the same thing.
import { esc, num, dur } from './ui.js';

const f2 = v => v == null ? '–' : Number(v).toFixed(2);
const g3 = v => v == null ? '–' : String(+Number(v).toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2));
const pKey = t => !t || t.latency_percentile === 100 ? 'max' : 'p' + (t.latency_percentile || 99);

const tightest = a => {
  let best = null;
  for (const x of a) for (const c of x.checks) if (c.margin != null && c.limit) { const r = c.margin / c.limit; if (!best || r < best.r) best = { r, x, c }; }
  return best;
};
const cellOf = (m, key, what) => !m ? '–' : what === 'lat' ? ((m.lat_ms || {})[key] == null ? '–' : Math.round((m.lat_ms[key]) * 10) / 10) : what === 'loss' ? f2(m.loss_pct) : what === 'jit' ? (m.jitter_ms == null ? '–' : m.jitter_ms.toFixed(1)) : f2(m.rx_mbps);
export function tsResultHtml(sum, { job = null, runId = null, name = '', when = '', editable = true, compare = true } = {}) {
  const ts = sum.testset, v = sum.verdict, apps = ts.apps, tg = tightest(apps), hasAlone = apps.some(a => a.alone), hasFill = apps.some(a => a.fill);
  const lat = a => pKey(a.target);
  const tight = tg ? `Tightest: ${esc(tg.x.name)} ${esc(tg.c.label.toLowerCase())}, <b>${g3(tg.c.value)} ${esc(tg.c.unit)}</b> of ${g3(tg.c.limit)} ${esc(tg.c.unit)} ${tg.c.dir === 'min' ? 'required' : 'allowed'}.` : '';
  const stopped = sum.stopped_early ? ' <small>(stopped early)</small>' : '';
  const banner = `<div class="banner ${v.pass ? 'pass' : 'fail'}" data-testid="ts-verdict"><div class="big">${v.pass ? 'PASS' : 'FAIL'}</div><div><b>${esc(name || ts.name)}</b>${stopped}<small class="tsmeta">${esc([dur(ts.length_s), when].filter(Boolean).join(' · '))}</small>
    ${v.reasons.length ? '<ul>' + v.reasons.slice(0, 6).map(r => `<li>${esc(r)}</li>`).join('') + '</ul>' : `<div>${tight}</div>`}</div></div>`;
  const rows = apps.map((a, i) => {
    const t = a.target, c = Object.fromEntries(a.checks.map(x => [x.key, x])), cl = (k, fmt) => !c[k] ? '' : c[k].pass === false ? ' bad' : '';
    const cell = (k, val, lim, unit) => `<td class="n${cl(k)}" data-testid="ts-${k}-${i + 1}">${val}${lim != null ? `<small>${c[k] && c[k].dir === 'min' ? '≥' : '≤'} ${g3(lim)} ${unit}</small>` : ''}</td>`;
    const m = a.together;
    return `<tr class="${a.pass === false ? 'bad' : ''}" data-testid="ts-res-${i + 1}"><td class="nm"><b>${esc(a.name)}</b><small>${num(m.tx_pkts)} sent · ${num(m.lost)} lost</small></td>
      ${cell('latency', cellOf(m, lat(a), 'lat'), t.max_latency_ms, 'ms')}${cell('loss', cellOf(m, '', 'loss'), t.max_loss_pct, '%')}
      ${cell('jitter', cellOf(m, '', 'jit'), t.max_jitter_ms, 'ms').replace('class="n', 'class="n t2')}${cell('throughput', cellOf(m, '', 'mbps'), t.min_throughput_mbps || null, 'Mb/s').replace('class="n', 'class="n t2')}
      <td class="n st"><span class="vt ${a.pass ? 'pass' : a.pass === false ? 'fail' : 'na'}">${a.pass ? 'PASS' : a.pass === false ? 'FAIL' : '–'}</span></td></tr>`;
  }).join('');
  const ex = ts.example_targets ? '<small class="exnote">example targets</small>' : '';
  const t1 = `<section class="panel tsmeasured" data-testid="ts-measured"><header><span>Measured against target</span>${ex}</header><div class="gscroll"><table class="ptable gtab tstab"><thead><tr><th>Application</th><th class="r">Latency ms</th><th class="r">Loss %</th><th class="r t2">Jitter ms</th><th class="r t2">Mb/s</th><th class="r">Result</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
  const dlt = (a, b) => a == null || b == null ? '–' : `${b - a >= 0 ? '+' : ''}${Math.round((b - a) * 10) / 10}`;
  const rows2 = apps.map((a, i) => { const k = lat(a), A = a.alone && (a.alone.lat_ms || {})[k], T2 = (a.together.lat_ms || {})[k], F = a.fill && (a.fill.lat_ms || {})[k]; return `<tr data-testid="ts-cmp-${i + 1}"><td class="nm">${esc(a.name)}</td><td class="r">${A == null ? '–' : Math.round(A * 10) / 10}</td><td class="r">${T2 == null ? '–' : Math.round(T2 * 10) / 10}</td><td class="r">${dlt(A, T2)}</td>${hasFill ? `<td class="r">${F == null ? '–' : Math.round(F * 10) / 10}</td>` : ''}</tr>`; }).join('');
  const t2 = hasAlone ? `<section class="panel tscompare" data-testid="ts-compare"><header><span>Alone, then together</span><small>what sharing the link cost</small></header><table class="ptable"><thead><tr><th>Latency, ms</th><th class="r">Alone</th><th class="r">Together</th><th class="r">Change</th>${hasFill ? '<th class="r">+ fill</th>' : ''}</tr></thead><tbody>${rows2}</tbody></table></section>` : '';
  const j = job || {}, kv = (k, l, ph) => `<div class="kv"><span>${l}</span><b>${j[k] ? esc(j[k]) : `<em class="muted">— ${ph}</em>`}</b></div>`;
  const rep = `<section class="panel tsreport" data-testid="ts-report"><header><span>Report</span>${editable ? '<button type="button" class="linkbtn" data-job-edit data-testid="ts-job-edit">✎ Edit</button>' : ''}</header><div class="pbody">
    ${kv('site', 'Site', 'add site')}${kv('link', 'Link', 'add link')}${kv('by', 'Tested by', 'add name')}
    <div class="row tsrep"><a class="btn primary" href="/api/report.pdf?run_ids=${runId}" data-testid="ts-pdf" download>Report (PDF)</a>${compare ? '<a class="btn" href="#results" data-ts="compare" data-testid="ts-compare-btn">Compare</a>' : ''}</div>
    <p class="muted tsscope">Measured: the link between the two Pis. Not included: the devices behind it. The report has sign-off lines.</p></div></section>`;
  return banner + t1 + t2 + (runId ? rep : '');
}
