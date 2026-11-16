import { $, $$, api, toast, esc, NAMES } from './ui.js';
import { S, connectWS, onLive } from './state.js';
import { renderSetup, initSetup } from './setup.js';
import { renderRun } from './run.js';
import { renderResults, refreshResults } from './results.js';
import { setRefreshTarget } from './refresh.js';
import { renderGenerator } from './generator.js';
import { renderSniffer } from './sniffer.js';
import { renderSettings } from './settings.js';
import { renderAbout, maybeUpdatedBanner } from './about.js';
import { updateBadge } from './update.js';
import { initAiGuide, setScreen, refreshLive } from './aiguide.js';
import { renderFirstBoot } from './firstboot.js';

let tab = null;
// The pulsing dot on the Run tab while a test is running (the tab holds an icon and a label, so only the dot is added or removed).
function runDot(on) {
  const b = $('[data-tab=run]'); let d = $('.live', b);
  if (on && !d) { d = document.createElement('span'); d.className = 'live'; d.title = 'A test is running'; b.appendChild(d); }
  else if (!on && d) d.remove();
}
const main = $('#main');

function theme(t) { document.documentElement.dataset.theme = t; localStorage.setItem('tt-theme', t); const m = document.querySelector('meta[name=theme-color]'); if (m) m.content = t === 'dark' ? '#151d26' : '#0f766e'; }
theme(localStorage.getItem('tt-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
$('#themeBtn').onclick = () => { theme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'); show(tab); };

const TABS = ['setup', 'run', 'results', 'generator', 'sniffer', 'settings', 'about'];       // 'about' has no tab button: it opens from the version label and the top bar
const tabOf = h => h === 'monitor' ? 'run' : TABS.includes(h) ? h : null;      // 'monitor' was its own tab once: old links still land on the live screen

export async function show(t) {
  t = tabOf(t) || 'setup';
  tab = t; setRefreshTarget(null); if (location.hash.slice(1) !== t) location.hash = t; setScreen(t);
  $$('#tabs button').forEach(b => b.setAttribute('aria-selected', b.dataset.tab === t));
  const root = document.createElement('div'); main.replaceChildren(root);
  try {
    S.status = await api('/status'); setTop(S.status);         // a renamed Pi or a new password shows in the top bar at once
    if (t === 'setup') await renderSetup(root, () => show('run'));
    else if (t === 'run') renderRun(root, show);
    else if (t === 'results') await renderResults(root);
    else if (t === 'generator') await renderGenerator(root, show);
    else if (t === 'sniffer') await renderSniffer(root, show);
    else if (t === 'settings') await renderSettings(root);
    else if (t === 'about') await renderAbout(root);
    if (t !== 'results') S.openRun = null;
    updateBadge();
  } catch (e) { if (e.message !== 'Password required') root.innerHTML = `<div class="card"><h2>Something went wrong</h2><p>${esc(e.message)}</p></div>`; }
}
$$('#tabs button').forEach(b => b.onclick = () => show(b.dataset.tab));
window.addEventListener('hashchange', () => { const t = tabOf(location.hash.slice(1)); if (t && t !== tab) show(t); });   // browser Back / Forward and typed links

function loginScreen() {
  main.innerHTML = `<div class="card" style="max-width:380px;margin:60px auto"><h2>Password</h2><p class="muted">This Pi Automation Traffic Tester is password-protected.</p>
    <label class="f">Password<input id="pw" type="password" autofocus></label><div class="row" style="margin-top:12px"><button class="btn primary" id="pwGo">Unlock</button></div></div>`;
  const go = async () => { try { await api('/auth/login', { method: 'POST', body: { password: $('#pw').value } }); start(); } catch (e) { toast('Wrong password', true); } };
  $('#pwGo').onclick = go; $('#pw').onkeydown = e => { if (e.key === 'Enter') go(); };
}
document.addEventListener('need-login', loginScreen);

onLive(e => {
  if (e.ev === '_conn') $('#conn').className = 'dot' + (e.ok ? '' : ' off');
  if (e.ev === 'started' || e.ev === 'done') refreshLive();
  if (e.ev === 'started' || e.ev === 'done') runDot(e.ev === 'started');
  if ((e.ev === 'started' || e.ev === 'done') && tab === 'results') refreshResults().catch(() => { });      // a finished test moves into the list without a reload
});

// Top right: only this Pi's name, and who is signed in (only when a password is set). The page title carries the name too: several Pis = several browser tabs.
export function setTop(st) {
  const name = (st.pi && st.pi.name) || st.hostname || 'Pi';
  NAMES.me = name;
  $('#piName').textContent = name;
  document.title = `${name} · Pi Automation Traffic Tester`;
  const u = $('#userBox'); if (u) u.hidden = !st.password_enabled;
}
document.addEventListener('tt-pi-renamed', () => { if (S.status) setTop(S.status); });      // Settings renamed this Pi: the top bar follows at once
$('#signOut').onclick = async e => {
  e.preventDefault();
  try { await api('/auth/logout', { method: 'POST' }); } catch { }
  location.hash = ''; location.reload();
};

async function start() {
  const st = await api('/status');
  if (st.login_required) return loginScreen();
  if (st.first_boot) return renderFirstBoot(main);                 // card image, not named yet: only the "Name this Pi" screen
  S.status = st;
  setTop(st);
  $('#appVer').textContent = `v${st.version}`;                      // same source as /api/status; the server also writes it into the page
  const hw = st.hardware || {};
  $('#hw').textContent = hw.model ? (hw.is_pi5 ? hw.model : `${hw.model} (not a Pi 5)`) : 'development PC (not a Raspberry Pi)';
  $('#piName').title = (hw.model ? `Running on: ${hw.model}${hw.is_pi5 ? '' : ' — built for the Raspberry Pi 5; this works but is not the target'}` : 'Not a Raspberry Pi (development PC). Built for the Raspberry Pi 5.') + ' · tap for Settings';
  await initSetup();
  initAiGuide();
  $('#appVer').addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); show('about'); });   // the version label opens About
  connectWS();
  const h = location.hash.slice(1);
  const first = tabOf(h) || (st.run.running ? (st.run.kind === 'generator' ? 'generator' : st.run.kind === 'sniffer' ? 'sniffer' : 'run') : 'setup');
  if (st.run.running) { S.live.running = true; S.live.runId = st.run.run_id; S.live.thr = st.run.threshold_ms || 60; runDot(true); }
  show(first);
  maybeUpdatedBanner(st);
}
start().catch(e => { main.innerHTML = `<div class="card"><h2>Cannot reach the Pi Automation Traffic Tester</h2><p>${esc(e.message)}</p></div>`; });
