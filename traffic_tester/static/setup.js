// Tests = the list of tests (built-in and your own). ▶ Run, Edit and "＋ New test" all open the same one-page form (newtest.js):
// Port -> Protocol -> From / To -> Traffic, with the Start bar always in the same place. No numbered wizard, no "two Pis" assumption.
import { $, $$, esc, api, toast, confirmBox, sheetBox, describeProfile, TRANSPORT_NAMES, download, dur, bindOnce } from './ui.js';
import { S, isTestset } from './state.js';
import { renderTestset } from './testset.js';
import { loadPeers } from './endpoints.js';
import { openNewTest, newTestOpen, closeNewTest } from './newtest.js';
import { Job } from './job.js';
import { setLoaded, loadedProfile } from './transport.js';

let goNext = () => { };

export async function loadProfiles() {
  S.profiles = await api('/profiles');
  if (S.sel) S.sel = S.profiles.find(p => p.id === S.sel.id) || null;
}

export async function renderSetup(root, onStart) {
  goNext = onStart;
  closeNewTest();                                       // the Tests tab always lands on the list: a test page left open by another tab must not swallow the taps
  await Promise.all([loadProfiles(), loadPeers()]);
  root.innerHTML = '<div id="stepBody"></div>';
  bindOnce(root, 'click', ev => listClick(ev, root));
  bindOnce(root, 'keydown', ev => { if (ev.key === 'Enter' && ev.target.matches('.pcard')) listClick(ev, root); });
  S.wizard.nt = null;                                   // the Setup tab always lands on the list
  drawStep();
}

function listClick(ev, root) {
  if (newTestOpen()) return;                            // the form handles its own taps (data-nt)
  const a = ev.target.closest('[data-act]'); if (!a) return;
  if (a.classList.contains('pcard') && ev.target.closest('details.more')) return;       // the ⋮ button itself is not a tap on the card
  const act = a.dataset.act, id = +a.dataset.id;
  const menu = a.closest('details.more'); if (menu && menu.open) menu.open = false;
  ({ pick, dup, del, exp1, expAll, imp, create, edit, info, rename, up, down, reset, hide, unhide, restoreAll })[act]?.(id, root, a);
}

const WIDE = matchMedia('(min-width:701px)');            // wide screens show Delete / Hide as clear buttons; phones keep them in the ⋮ menu (one place only, never both)
const trips = () => S.profiles.filter(p => !p.kind || p.kind === 'trip' || isTestset(p));          // the Tests list: trip tests and test sets
// Test sets in groups (docs/design/UX-INSTRUMENT-FABLE.md 5.2): DA, FAN, Link basics, My tests. A test set names its own group; the older trip tests fall into the group they belong to.
const GROUPS = [['da', 'DA · distribution automation'], ['fan', 'FAN · many apps, one link'], ['link', 'Link basics'], ['mine', 'My tests']];
const groupOf = p => isTestset(p) ? (p.builtin ? p.gen.testset.group : 'mine') : !p.builtin ? 'mine' : /^(DER|GOOSE|Mirrored|Recloser)/i.test(p.name) ? 'da' : /^FAN/i.test(p.name) ? 'fan' : 'link';
const tsLine = p => { const n = p.gen.streams.filter(s => s.enabled !== false).length, t = p.gen.testset; return `${n} application${n === 1 ? '' : 's'} · ${t.alone && n > 1 ? 'alone, then together' : 'together'}${t.fill ? ', plus fill' : ''} · ${dur(p.gen.duration_s)}${(t.lengths || []).length ? ' (length is a choice)' : ''}`; };
const kindLine = p => isTestset(p) ? tsLine(p) : `${p.mode === 'mirrored' ? 'Mirrored Bits mode' : TRANSPORT_NAMES[p.transport]} · ${p.mode === 'mirrored' ? (p.transport === 'serial' ? p.serial.baud : p.mirrored.rate_baud) + ' baud · test bit every ' + p.mirrored.trip_every_s + ' s' : p.interval_ms + ' ms'} · ${dur(p.duration_s)} · limit ${p.threshold_ms} ms`;
function tcardHtml(p, i, list) {
  const nm = esc(p.name);
  return `<div class="pcard" data-act="info" data-id="${p.id}" data-tcard="1" tabindex="0" role="button" aria-label="${nm}: open what this test checks and how it works">
    <div class="ptop"><h3>${isTestset(p) ? `<span class="pname ro">${nm}</span>` : `<button type="button" class="pname" data-act="rename" data-id="${p.id}" title="Tap the name to rename this test" aria-label="Rename">${nm}</button>`}
      <span class="tags">${loadedProfile() && loadedProfile().id === p.id ? '<span class="tag loaded" data-testid="tag-loaded">Loaded</span>' : ''}<span class="tag ${p.builtin ? 'preset' : ''}">${p.builtin ? 'Built-in' : 'Mine'}</span>${p.edited ? '<span class="tag edited">Edited</span>' : ''}</span></h3>
      <details class="more pmenu"><summary aria-label="More actions" title="More actions">⋮</summary><div>
        ${isTestset(p) ? '' : `<button type="button" class="btn small" data-act="rename" data-id="${p.id}">Rename</button>`}
        <button type="button" class="btn small" data-act="dup" data-id="${p.id}">Duplicate</button>
        <button type="button" class="btn small" data-act="exp1" data-id="${p.id}">Export</button>
        ${isTestset(p) ? '' : `<button type="button" class="btn small" data-act="up" data-id="${p.id}" ${i === 0 ? 'disabled' : ''}>Move up</button>
        <button type="button" class="btn small" data-act="down" data-id="${p.id}" ${i === list.length - 1 ? 'disabled' : ''}>Move down</button>`}
        ${p.edited ? `<button type="button" class="btn small" data-act="reset" data-id="${p.id}">Reset to default</button>` : ''}
        ${WIDE.matches ? '' : p.builtin ? `<button type="button" class="btn small" data-act="hide" data-id="${p.id}">Hide this test</button>` : `<button type="button" class="btn small" data-act="del" data-id="${p.id}">Delete</button>`}
      </div></details></div>
    <p>${esc(p.description || describeProfile(p))}</p>
    <div class="meta">${kindLine(p)}</div>
    <div class="acts"><button type="button" class="btn primary" data-act="pick" data-id="${p.id}" title="Open it ready to start: check where it goes, then ▶ Start">▶ Run</button>
      ${isTestset(p) && p.builtin ? '' : `<button type="button" class="btn pill" data-act="edit" data-id="${p.id}" title="Change this test and save it">Edit</button>`}
      ${WIDE.matches ? `<button type="button" class="btn desk" data-act="${p.builtin ? 'hide' : 'del'}" data-id="${p.id}">${p.builtin ? 'Hide' : 'Delete'}</button>` : ''}
      <span class="pinfo">ⓘ What this test does</span></div></div>`;
}

function drawStep() {
  const root = $('#main'), body = $('#stepBody', root); if (!body) return;
  S.wizard.nt = null;
  const trip = trips(), shown = trip.filter(p => !p.hidden), hid = trip.filter(p => p.hidden);
  body.innerHTML = `<div class="listhead"><h1 style="margin:0">Tests</h1>
      <button type="button" class="btn primary big" data-act="create" data-testid="setup-create-custom-test" aria-label="＋ New test" title="Choose the port, the protocol and where to send: one page">＋ New test</button></div>
    <p class="sub">▶ Run opens a test ready to start. Tap a card to read what it checks. ＋ New test builds your own in four rows: Port, Protocol, From → To, Traffic.</p>
    ${shown.length ? `<div id="testGrid">${GROUPS.map(([k, t]) => { const l = shown.filter(p => groupOf(p) === k); return l.length ? `<h3 class="grp" data-testid="tests-group-${k}">${t}</h3><div class="grid g3">${l.map((p, i) => tcardHtml(p, i, l)).join('')}</div>` : ''; }).join('')}</div>` : '<div class="card empty"><p>No tests in your list. Tap <b>＋ New test</b>, or show the hidden ones below.</p></div>'}
    ${hid.length ? `<details class="hidden-tests card" id="hiddenTests" ${S.wizard.hiddenOpen ? 'open' : ''}><summary>Show hidden tests (${hid.length})</summary>
      ${hid.map(p => `<div class="hrow"><span><b>${esc(p.name)}</b> <span class="muted">· ${p.builtin ? 'built-in' : 'mine'}</span></span><button type="button" class="btn small" data-act="unhide" data-id="${p.id}">Show again</button></div>`).join('')}
      <div class="row" style="margin-top:10px"><button type="button" class="btn small" data-act="restoreAll">Restore all hidden tests</button></div></details>` : ''}
    <div class="tools" style="margin-top:14px"><span class="muted" style="font-size:.85rem">Tests as files:</span><button class="btn small" data-act="imp">⤒ Import</button><button class="btn small" data-act="expAll">⤓ Export all</button></div>
    <input type="file" id="impFile" accept="application/json,.json" hidden>`;
  const hd = $('#hiddenTests', root); if (hd) hd.ontoggle = () => { S.wizard.hiddenOpen = hd.open; };
  if (S.wizard.flash) { const c = $(`.pcard[data-id="${S.wizard.flash}"]`, root); S.wizard.flash = null; if (c) { c.classList.add('flash'); c.scrollIntoView({ block: 'center' }); } }
  $('#impFile', root).onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    try { const r = await api('/profiles/import', { method: 'POST', body: JSON.parse(await f.text()) }); toast(`Imported ${r.imported.length} test(s)${r.presets_updated ? ` and ${r.presets_updated} edited built-in test(s)` : ''}.`); await loadProfiles(); drawStep(); }
    catch (er) { toast(er.message || 'That file could not be imported.', true, 7000); }
  };
}

// ---------------------------------------------------------------- the one-page form (newtest.js)
const EXTRAS = { job: el => Job.line(el), jobValue: () => Job.value() };
async function openPage(id, mode) {
  const body = $('#stepBody', $('#main')); if (!body) return;
  const p = id != null ? S.profiles.find(x => x.id === id) : null;
  if (id != null && !p) { S.wizard.nt = null; return drawStep(); }
  S.wizard.nt = { id, mode }; S.sel = p || null;
  if (p && isTestset(p)) {                                // a test set: one screen for the applications, their targets, the run and the result
    try { await renderTestset(body, (t, o) => { if (t === 'tests' || !t) { S.wizard.nt = null; return goNext(t, o); } return goNext(t, o); }, { id }); } catch (e) { S.wizard.nt = null; toast(e.message, true, 7000); drawStep(); }
    return;
  }
  try {
    await openNewTest(body, {
      profile: p, mode, extras: EXTRAS,
      cb: {
        started: r => { Job.started(); S.wizard.nt = null; goNext(r.run_id); },
        saved: async r => { S.wizard.nt = null; await loadProfiles(); S.wizard.flash = r.id; drawStep(); },
        closed: () => { S.wizard.nt = null; drawStep(); window.scrollTo({ top: 0 }); },
      },
    });
  } catch (e) { S.wizard.nt = null; toast(e.message, true, 7000); drawStep(); }
}
const create = () => openPage(null, 'new');
const edit = id => openPage(id, 'edit');
const pick = id => { setLoaded(id); return openPage(id, 'run'); };

// Start from anywhere: open the test on this screen and press its Start as soon as the checks pass (stops when the checks fail, or the page changes).
export async function autoRun(id, alive = () => true) {
  await pick(id);
  let bad = 0;
  for (let i = 0; i < 160 && alive(); i++) {
    const b = $('#main [data-nt=start]'); if (!b) return;
    if (!b.disabled) { b.click(); return; }
    bad = $('#startHint.bad') ? bad + 1 : 0; if (bad > 8) return;
    await new Promise(r => setTimeout(r, 250));
  }
}

// ---------------------------------------------------------------- list actions
async function dup(id) { try { const p = await api(`/profiles/${id}/duplicate`, { method: 'POST' }); await loadProfiles(); S.wizard.flash = p.id; toast(`Made “${p.name}”: your own copy at the end of the list. Tap Edit on it to change it.`, false, 6000); drawStep(); } catch (e) { toast(e.message, true); } }
async function del(id, root) {
  const p = S.profiles.find(x => x.id === id); if (!p) return;
  if (!await confirmBox('Delete this test?', `“${p.name}” will be deleted from your list. Results you already have are kept. You can undo this right after.`, 'Delete', true)) return;
  try {
    const r = await api(`/profiles/${id}`, { method: 'DELETE' });
    if (S.sel?.id === id) S.sel = null;
    await loadProfiles(); drawStep();
    toast(`Deleted “${p.name}”.`, false, 9000, { label: 'Undo', fn: async () => { try { const b = await api('/profiles/undelete', { method: 'POST', body: r.deleted }); await loadProfiles(); S.wizard.flash = b.id; drawStep(); toast(`Brought back “${b.name}”.`); } catch (e) { toast(e.message, true); } } });
  } catch (e) { toast(e.message, true); }
}
async function hide(id) {
  const p = S.profiles.find(x => x.id === id); if (!p) return;
  if (!await confirmBox('Hide this test?', `“${p.name}” will be taken off your list. Nothing is deleted: show it again any time under “Show hidden tests” at the bottom of the list.`, 'Hide')) return;
  try {
    await api(`/profiles/${id}/hide`, { method: 'POST' });
    if (S.sel?.id === id) S.sel = null;
    await loadProfiles(); drawStep();
    toast(`Hid “${p.name}”.`, false, 9000, { label: 'Undo', fn: async () => { await api(`/profiles/${id}/unhide`, { method: 'POST' }); await loadProfiles(); S.wizard.flash = id; drawStep(); } });
  } catch (e) { toast(e.message, true); }
}
async function unhide(id) { try { await api(`/profiles/${id}/unhide`, { method: 'POST' }); await loadProfiles(); S.wizard.flash = id; drawStep(); } catch (e) { toast(e.message, true); } }
async function restoreAll() { try { const r = await api('/profiles/restore-hidden', { method: 'POST' }); await loadProfiles(); drawStep(); toast(`Showing ${r.restored} hidden test(s) again.`); } catch (e) { toast(e.message, true); } }
async function reset(id) {
  const p = S.profiles.find(x => x.id === id); if (!p) return;
  if (!await confirmBox('Reset to default?', `“${p.name}” goes back to the settings, name and description it shipped with. Your changes to it are lost.`, 'Reset', true)) return;
  try { const r = await api(`/profiles/${id}/reset`, { method: 'POST' }); await loadProfiles(); S.wizard.flash = id; drawStep(); toast(`“${r.name}” is back to its default.`); } catch (e) { toast(e.message, true, 7000); }
}
async function move(id, dir) {
  const me = trips().find(p => p.id === id), shown = trips().filter(p => !p.hidden && me && groupOf(p) === groupOf(me)), i = shown.findIndex(p => p.id === id), j = i + dir;      // Move up / down stay inside the group the test is shown in
  if (i < 0 || j < 0 || j >= shown.length) return;
  const all = trips().map(p => p.id), a = all.indexOf(shown[i].id), b = all.indexOf(shown[j].id);
  [all[a], all[b]] = [all[b], all[a]];
  try { await api('/profiles/order', { method: 'POST', body: { ids: all } }); await loadProfiles(); S.wizard.flash = id; drawStep(); } catch (e) { toast(e.message, true); }
}
const up = id => move(id, -1), down = id => move(id, 1);
// Rename right on the card: the name turns into a text box; Enter or tapping away saves, Esc cancels. A built-in test keeps its link to its default (Reset restores the original name).
function rename(id, root, a) {
  const p = S.profiles.find(x => x.id === id); if (!p) return;
  const card = $(`.pcard[data-id="${id}"]`, root); const btn = card && $('.pname', card); if (!btn) return;
  const inp = document.createElement('input'); inp.className = 'pname-edit'; inp.value = p.name; inp.maxLength = 80; inp.setAttribute('aria-label', 'Test name'); inp.dataset.testidAuto = '';
  btn.replaceWith(inp); inp.focus(); inp.select();
  let done = false;
  const finish = async save => {
    if (done) return; done = true;
    const name = inp.value.trim();
    if (!save || !name || name === p.name) return drawStep();
    try { const body = { ...p, name }; await api(`/profiles/${id}`, { method: 'PUT', body }); await loadProfiles(); S.wizard.flash = id; toast(`Renamed to “${name}”.`); }
    catch (e) { toast(e.message, true, 7000); }
    drawStep();
  };
  inp.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); finish(true); } else if (e.key === 'Escape') { e.stopPropagation(); finish(false); } };
  inp.onblur = () => finish(true);
  inp.onclick = e => e.stopPropagation();
}
// Tapping a test opens a plain-English panel: what it tests, how it works, what you need, what "pass" means, typical use. Built-in tests carry their text in the app (one source, also used by the AI guide).
export function infoBody(p) {
  if (isTestset(p)) {
    const t = p.gen.testset, rows = p.gen.streams.map(s => { const g = s.target || {}; return `<li><b>${esc(s.name)}</b> <span class="muted">${esc([g.max_latency_ms != null ? `latency ≤ ${g.max_latency_ms} ms` : '', g.max_loss_pct != null ? `loss ≤ ${g.max_loss_pct} %` : '', g.max_jitter_ms != null ? `jitter ≤ ${g.max_jitter_ms} ms` : '', g.min_throughput_mbps ? `at least ${g.min_throughput_mbps} Mb/s` : ''].filter(Boolean).join(' · '))}</span></li>`; }).join('');
    return `<p><span class="tag ${p.builtin ? 'preset' : ''}">${p.builtin ? 'Built-in' : 'Mine'}</span> <span class="muted">${esc(tsLine(p))}</span></p><h3>What it tests</h3><p>${esc(p.description)}</p><h3>The applications and their targets</h3><ul>${rows}</ul>
      <h3>How it runs</h3><p>${t.alone ? `Each application runs alone for ${t.alone_s} s (not judged), then ` : ''}all of them run together for the length you choose (judged against each target)${t.fill ? `, then once more with ${t.fill_mbps} Mb/s of fill traffic (shown, not judged)` : ''}. One result, one report.</p>
      <p class="muted">The targets are example values, not from a standard. Edit any of them on the test page.</p>`;
  }
  const secs = [['what', 'What it tests'], ['how', 'How it works'], ['need', 'What you need'], ['pass_means', 'What “pass” means'], ['typical', 'Typical use']];
  const five = i => secs.map(([k, t]) => `<h3>${t}</h3><p>${esc(i[k])}</p>`).join('');
  const tags = `<p><span class="tag ${p.builtin ? 'preset' : ''}">${p.builtin ? 'Built-in' : 'Mine'}</span> ${p.edited ? '<span class="tag edited">Edited</span> ' : ''}<span class="muted">${esc(kindLine(p))}</span></p>`;
  if (p.info && !p.edited) return tags + five(p.info);
  const own = p.description ? `<h3>About this test</h3><p>${esc(p.description)}</p>` : '<p class="muted">No description yet. Tap Edit and describe what this test is for.</p>';
  return tags + own + `<h3>What it does now</h3><p>${esc(describeProfile(p))}</p>` + (p.info ? `<details><summary>About the original “${esc(p.default_name || p.name)}” test</summary>${five(p.info)}</details>` : '');
}
async function info(id) {
  const p = S.profiles.find(x => x.id === id); if (!p) return;
  const r = await sheetBox(p.name, infoBody(p), [{ id: 'close', label: 'Close' }, { id: 'edit', label: 'Edit' }, { id: 'run', label: '▶ Run this test', cls: 'primary' }]);
  if (r === 'run') pick(id); else if (r === 'edit') edit(id);
}
async function exp1(id) { const r = await fetch(`/api/profiles/export?ids=${id}`); download(`traffic-tester-profile-${id}.json`, await r.text()); }
async function expAll() { const r = await fetch('/api/profiles/export'); download('traffic-tester-profiles.json', await r.text()); }
const imp = () => $('#impFile').click();

export async function initSetup() { }
document.addEventListener('keydown', ev => { if (ev.key === 'Escape') document.querySelectorAll('details.pmenu[open]').forEach(d => { d.open = false; }); });      // Esc closes an open ⋮ menu
