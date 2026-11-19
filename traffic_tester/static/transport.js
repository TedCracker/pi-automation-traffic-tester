// The transport bar: one state lamp + word, one timer, Clear, Mark and ONE Start that becomes Stop, for all three engines (tests, generator, sniffer).
// On a phone it sits above the tab bar (at the thumb); on a desktop it sits under the lamp strip. It never moves between screens.
// Start does what the screen in front of you would do: Tests (form open) -> that test; Generator / Sniffer -> their own setup; anywhere else -> the loaded test.
import { $, api, toast, confirmBox, clock, NAMES } from './ui.js';
import { S, onLive, resetLive, isTestset } from './state.js';

let nav = () => { }, tab = 'home', timeNow = 0, timeTotal = 0, lastKey = '';
export const setTransportTab = t => { tab = t; paint(); watchSource(); };
export const initTransport = fn => { nav = fn; paint(); wire(); };

// ---- state ------------------------------------------------------------------------------------------------------------------------------
export const runKind = () => {
  const r = S.status && S.status.run;
  if (r && r.running) return r.kind === 'generator' ? 'generator' : r.kind === 'sniffer' ? 'sniffer' : 'trip';
  const p = S.live.profile; return p && (p.kind === 'generator' || p.kind === 'sniffer') ? p.kind : 'trip';
};
export const isRunning = () => !!(S.live.running || (S.status && S.status.run && S.status.run.running));
export const runId = () => S.live.runId || (S.status && S.status.run && S.status.run.run_id) || null;
export const runName = () => (S.live.profile && S.live.profile.name) || (S.status && S.status.run && (S.status.run.profile_name || S.status.run.name)) || '';
export const stateOf = () => isRunning() ? (runKind() === 'sniffer' ? 'capturing' : 'running') : (S.live.done && S.showDone ? 'done' : 'ready');
export const pillarOfRun = () => !isRunning() ? null : runKind() === 'generator' ? (S.live.profile ? isTestset(S.live.profile) : (S.status && S.status.run && S.status.run.testset)) ? 'tests' : 'generator' : runKind() === 'sniffer' ? 'sniffer' : 'tests';

function readTime() {
  const k = runKind(), L = S.live;
  if (!isRunning() && !(L.done && S.showDone)) { timeNow = 0; timeTotal = 0; return; }
  const e = k === 'generator' ? S.gen.last : k === 'sniffer' ? S.sniff.last : L.state;
  if (e && e.elapsed_s != null) { timeNow = e.elapsed_s; timeTotal = e.total_s || 0; }
  else if (!isRunning() && !(L.done && S.showDone)) timeNow = 0;
}

// the loaded test: what Start runs from Home and from the Tests list
export const loadedId = () => { try { return +localStorage.getItem('tt-loaded') || 0; } catch { return 0; } };
export const setLoaded = id => { try { localStorage.setItem('tt-loaded', String(id)); } catch { } paint(); };
export function loadedProfile() {
  const tr = (S.profiles || []).filter(p => (!p.kind || p.kind === 'trip' || isTestset(p)) && !p.hidden);
  return tr.find(p => p.id === loadedId()) || tr.find(p => /quick link check/i.test(p.name)) || tr[0] || null;
}

// ---- painting ---------------------------------------------------------------------------------------------------------------------------
const WORD = { ready: 'READY', running: 'RUNNING', capturing: 'CAPTURING', done: 'DONE' };
export function paint() {
  const bar = $('#transport'); if (!bar) return;
  readTime();
  const st = stateOf(), k = runKind(), running = st === 'running' || st === 'capturing';
  const lp = loadedProfile();
  const name = running ? runName() : st === 'done' ? runName() : (tab === 'generator' ? 'Generator' : tab === 'sniffer' ? 'Sniffer' : lp ? lp.name : '');
  const key = [st, tab, name, Math.floor(timeNow), k].join('|');
  bar.dataset.state = st; bar.dataset.kind = running ? k : '';
  $('#tbWord').textContent = WORD[st];
  $('#tbTime').textContent = clock(timeNow);
  const n = $('#tbName'); if (n && n.textContent !== name) n.textContent = name;
  const s = $('#tbStart'); s.dataset.state = running ? 'running' : 'ready';
  $('.tb-ico', s).textContent = running ? '■' : '▶'; $('.tb-lbl', s).textContent = running ? 'STOP' : 'START';
  s.setAttribute('aria-label', running ? 'Stop' : 'Start');
  $('#tbClear').disabled = !(st === 'done' || (st === 'running' && k === 'generator') || (st === 'capturing' && tab === 'sniffer'));      // a generator test keeps going: Clear zeroes its counters
  $('#tbMark').disabled = !(running && k !== 'sniffer');
  syncDisabled();
  // the pillar that is running gets a dot on its tab
  const p = pillarOfRun();
  ['tests', 'generator', 'sniffer'].forEach(t => {
    const b = $(`#tabs [data-tab=${t}]`); if (!b) return; let d = $('.live', b);
    if (p === t && !d) { d = document.createElement('span'); d.className = 'live'; d.title = 'Running now'; b.appendChild(d); }
    else if (p !== t && d) d.remove();
  });
  lastKey = key;
}

// While the New-test form is open the Start button follows the form's own state (enabled only when the checks pass).
const srcStart = () => $('#main [data-nt=start]');
function syncDisabled() {
  const s = $('#tbStart'); if (!s) return;
  const running = s.dataset.state === 'running';
  const open = !running && tab === 'tests' && !!S.wizard.nt, src = open ? srcStart() : null;      // a test page that is still loading counts as "not ready"
  s.disabled = open && (!src || src.disabled);
}
let mo = null;
function watchSource() {
  if (mo) mo.disconnect(); const m = $('#main'); if (!m) return;
  let q = 0; mo = new MutationObserver(() => { if (q) return; q = requestAnimationFrame(() => { q = 0; syncDisabled(); }); });
  mo.observe(m, { subtree: true, childList: true, attributes: true, attributeFilter: ['disabled'] });
}

// ---- actions ----------------------------------------------------------------------------------------------------------------------------
async function stop() {
  const k = runKind(), id = runId(); if (!id) return;
  const [t, x, b] = k === 'sniffer' ? ['Stop the capture?', 'The files so far are kept.', 'Stop']
    : k === 'generator' ? ['Stop the test?', 'Results so far are kept and judged.', 'Stop'] : ['Stop the test?', 'The results so far are kept and judged as a shorter run.', 'Stop test'];
  if (await confirmBox(t, x, b, true)) await api(`/runs/${id}/stop`, { method: 'POST' }).catch(e => toast(e.message, true));
}

// Start the loaded test from anywhere: open it on the Tests screen and press its Start as soon as the checks pass.
let auto = 0;
export async function startLoaded() {
  const p = loadedProfile(); if (!p) { toast('No test is loaded. Pick one on the Tests screen.', true); return nav('tests'); }
  const { autoRun } = await import('./setup.js');
  const my = ++auto; await nav('tests');
  await autoRun(p.id, () => my === auto);
}

async function start() {
  if (tab === 'generator' || tab === 'sniffer') { const b = $('#main [data-act=start]'); if (b) return b.click(); return toast(tab === 'sniffer' ? 'Pick a capture setup first, then press Start.' : 'Pick a traffic test first, then press Start.', true); }
  if (tab === 'tests') { const b = srcStart(); if (b) return b.click(); }
  return startLoaded();
}

async function clearIt() {
  if (isRunning() && runKind() === 'sniffer') { document.dispatchEvent(new Event('tt-sniff-clear')); return; }      // a capture keeps recording: Clear empties the packet list
  if (isRunning() && runKind() === 'generator') { await api(`/runs/${runId()}/clear`, { method: 'POST' }).then(() => toast('Counters cleared. The test goes on.')).catch(e => toast(e.message, true)); return; }
  S.live.done = null; S.showDone = false; S.live.state = null; paint(); nav(tab === 'results' || tab === 'config' ? tab : tab, { force: true });
}

function wire() {
  $('#tbStart').onclick = () => { auto++; isRunning() ? stop() : start(); };
  $('#tbClear').onclick = clearIt;
  $('#tbMark').onclick = async () => { (await import('./run.js')).dropMark(); };
  onLive(e => {
    if (e.ev === 'done') S.showDone = true;
    if (e.ev === 'started') S.showDone = false;
    if (['state', 'gstate', 'sstate', 'started', 'done', '_conn'].includes(e.ev)) paint();
  });
  setInterval(paint, 2000);
  document.addEventListener('click', () => { syncDisabled(); setTimeout(syncDisabled, 0); });      // a tap that opens a test page flips the Start state before the page has drawn
  document.addEventListener('tt-run-started', () => { S.showDone = false; paint(); });
}
