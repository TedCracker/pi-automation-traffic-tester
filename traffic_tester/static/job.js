// Job info (SPEC 7.4): four optional texts about the job a test belongs to: Site, Link / radio, By, Note.
// It is never a question: one pre-filled line in the Start bar ("Job: Utility · Site A ✎"), editable there and on the result afterwards.
//   Job.line(container, { onChange })  draws that line into `container` (pre-filled from the last run on this Pi); a tap opens the sheet
//   Job.value()                         the dict for the body of POST /api/runs as `job` ({} when nothing is filled in)
//   Job.started()                       call after a test started: Site / Link / By stay for the next test, the note (about one run) is cleared
//   Job.edit(runId, job)                the same sheet for a stored result: saves it and resolves to the new job (null = closed without saving)
//   Job.text(job) / Job.block(job, o)   the one-line text / the block shown on a result
import { $, $$, esc, api, toast, sheetBox } from './ui.js';

export const JOB_FIELDS = [
  { key: 'site', label: 'Site', max: 80, hint: 'e.g. Utility · Site A' },
  { key: 'link', label: 'Link / radio', max: 80, hint: 'e.g. 5G router, bench cable' },
  { key: 'by', label: 'By', max: 60, hint: 'who ran the test' },
  { key: 'note', label: 'Note', max: 300, hint: 'signal level, antenna, weather…', area: true },
];
const clean = j => { const o = {}; for (const f of JOB_FIELDS) { const v = String((j || {})[f.key] ?? '').replace(/\s+/g, ' ').trim().slice(0, f.max); if (v) o[f.key] = v; } return o; };

let draft = null, dirty = false, mounts = [];

// 'Utility · Site A · by Alex' (the note is shown on its own)
export const jobText = j => { j = j || {}; return [j.site, j.link, j.by ? 'by ' + j.by : ''].filter(Boolean).join(' · '); };

// The sheet with the four fields. Resolves to the cleaned job when Save was pressed, else null.
function sheet(job, title, lead) {
  const vals = { ...clean(job) };
  const p = sheetBox(title, `<p class="muted" style="margin:0 0 10px">${esc(lead)}</p>
    <div class="jobform">${JOB_FIELDS.map(f => `<label class="f">${esc(f.label)}${f.area
    ? `<textarea rows="2" maxlength="${f.max}" data-job="${f.key}" data-testid="job-${f.key}" placeholder="${esc(f.hint)}">${esc(vals[f.key] || '')}</textarea>`
    : `<input maxlength="${f.max}" data-job="${f.key}" data-testid="job-${f.key}" placeholder="${esc(f.hint)}" value="${esc(vals[f.key] || '')}" autocomplete="off">`}</label>`).join('')}</div>`,
    [{ id: 'job-clear', label: 'Clear' }, { id: 'job-cancel', label: 'Cancel' }, { id: 'job-save', label: 'Save', cls: 'primary' }], 'job-sheet');
  // the sheet empties itself when a button is pressed: keep the values as they are typed
  $$('[data-job]').forEach(el => {
    el.oninput = () => { vals[el.dataset.job] = el.value; };
    if (el.tagName === 'INPUT') el.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); $('#sb-job-save').click(); } };
  });
  const first = $('[data-job]'); if (first) first.focus();
  return p.then(btn => btn === 'job-save' ? clean(vals) : btn === 'job-clear' ? {} : null);
}

function draw(m) {
  const t = jobText(draft), note = (draft || {}).note;
  m.el.innerHTML = `<button type="button" class="jobline" data-testid="job-line" aria-label="Job info: ${esc(t || 'none yet')}. Tap to change.">
    <span class="jl-k">Job</span><span class="jl-v${t || note ? '' : ' muted'}">${esc(t || (note ? note : 'Add site, link, who (optional)'))}</span><span class="jl-e" aria-hidden="true">✎</span></button>`;
  $('button', m.el).onclick = async () => {
    const j = await sheet(draft, 'Job info', 'Optional. It is stored with the result and printed on the report. Starting a test never waits for it.');
    if (j === null) return;
    draft = j; dirty = true;
    mounts = mounts.filter(x => x.el.isConnected); mounts.forEach(draw);
    if (m.onChange) m.onChange(Job.value());
  };
}

export const Job = {
  // Draw the Start-bar line. Returns at once; the pre-fill (the last job used on this Pi) arrives a moment later.
  line(container, { onChange } = {}) {
    if (!container) return;
    const m = { el: container, onChange };
    mounts = mounts.filter(x => x.el.isConnected && x.el !== container); mounts.push(m);
    if (draft === null) draft = {};
    draw(m);
    if (!dirty) api('/job/last').then(r => { if (!dirty) { draft = clean(r.job); mounts.filter(x => x.el.isConnected).forEach(draw); } }).catch(() => { });
  },
  value() { return clean(draft); },
  started() { if (draft && draft.note) { draft = { ...draft }; delete draft.note; } dirty = false; mounts.filter(x => x.el.isConnected).forEach(draw); },
  async edit(runId, job) {
    const j = await sheet(job, 'Job info of this result', 'Where, on what and by whom this test was made. Shown in the results list, on the report and in the exports.');
    if (j === null) return null;
    try { const r = await api(`/runs/${runId}/job`, { method: 'PUT', body: { job: j } }); toast(Object.keys(r.job).length ? 'Job info saved.' : 'Job info cleared.'); return r.job; }
    catch (e) { toast(e.message, true, 6000); return null; }
  },
  text: jobText,
  // The block on a result: Site / Link / By / Note with ✎ Edit; with nothing filled in, one quiet line that offers to add it.
  block(job, { editable = true } = {}) {
    const j = clean(job), has = Object.keys(j).length > 0;
    const edit = editable ? `<button type="button" class="btn small ghost" data-job-edit data-testid="result-job-edit">${has ? '✎ Edit' : '＋ Add job info'}</button>` : '';
    if (!has) return editable ? `<div class="card jobcard empty-job" data-testid="result-job"><div class="row sp"><span class="muted">No job info (site, link, who, note).</span>${edit}</div></div>` : '';
    return `<div class="card jobcard" data-testid="result-job"><div class="row sp"><b>Job</b>${edit}</div>
      <div class="jobkv">${JOB_FIELDS.filter(f => j[f.key]).map(f => `<span>${esc(f.label)}</span><b data-testid="result-job-${f.key}">${esc(j[f.key])}</b>`).join('')}</div></div>`;
  },
};
