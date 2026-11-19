import { $, $$, api, toast, esc, NAMES } from './ui.js';
import { S, connectWS, onLive, pullLive, isTestset } from './state.js';
import { renderTestset, tsRun } from './testset.js';
import { renderSetup, initSetup } from './setup.js';
import { renderHome } from './home.js';
import { renderConfig } from './config.js';
import { initTransport, setTransportTab, paint as paintTransport, runKind } from './transport.js';
import { renderRun } from './run.js';
import { renderResults, refreshResults } from './results.js';
import { setRefreshTarget } from './refresh.js';
import { renderGenerator } from './generator.js';
import { renderSniffer } from './sniffer.js';
import { maybeUpdatedBanner } from './about.js';
import { updateBadge } from './update.js';
import { initLamps, drawLamps } from './lamps.js';
import { initAiGuide, setScreen, refreshLive } from './aiguide.js';
import { renderFirstBoot } from './firstboot.js';

let tab = null;
const main = $('#main');

function theme(t) { document.documentElement.dataset.theme = t; localStorage.setItem('tt-theme', t); const m = document.querySelector('meta[name=theme-color]'); if (m) m.content = '#16202a'; }
theme(localStorage.getItem('tt-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
$('#themeBtn').onclick = () => { theme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'); show(tab); };

const TABS = ['home', 'tests', 'generator', 'sniffer', 'results', 'config'];
// Old links still work: #setup was the test list, #run / #monitor the live screen, #settings the settings, #about the About page.
const ALIAS = { setup: 'tests', run: 'tests', monitor: 'tests', settings: 'config', about: 'config/help' };
const parseHash = h => { const [a, b] = String(h || '').replace(/^#/, '').split('/'); const t = ALIAS[a] || a; const [t2, b2] = t.split('/'); return TABS.includes(t2) ? { t: t2, sub: b || b2 || '' } : null; };
const tabOf = h => { const x = parseHash(h); return x ? x.t : null; };

export async function show(t, opts = {}) {
  let sub = opts.sub || '';
  if (typeof t === 'string' && (t.includes('/') || ALIAS[t])) { const x = parseHash(t); if (x) { t = x.t; sub = x.sub; } }
  t = tabOf(t) || 'home';
  // Opening Tests (tap, link, typed address) after a finished trip test gives the list again (the finished run is in Results; Clear does the same on the spot). A run that is still going stays.
  if (t === 'tests' && !S.live.running && S.live.done && !(S.live.profile && ['generator', 'sniffer'].includes(S.live.profile.kind) && !isTestset(S.live.profile))) { S.live.done = null; S.showDone = false; }      // a finished test set behaves like a finished trip test: opening Tests gives the list again (its result is in Results)
  tab = t; setRefreshTarget(null); const hash = sub ? `${t}/${sub}` : t; if (location.hash.slice(1) !== hash) { lastHash = hash; location.hash = hash; }      // our own hash change must not render the screen a second time
  setScreen(t === 'tests' ? 'setup' : t === 'config' ? 'settings' : t, t);
  $$('#tabs button').forEach(b => b.setAttribute('aria-selected', b.dataset.tab === t));
  $('#setupBtn').setAttribute('aria-selected', t === 'config');
  setTransportTab(t);
  const root = document.createElement('div'); main.replaceChildren(root);
  try {
    S.status = await api('/status'); setTop(S.status); drawLamps();         // a renamed Pi or a new password shows in the top bar at once
    if (t === 'home') await renderHome(root, show);
    else if (t === 'tests') {
      const r = S.status.run;                                                                 // a trip test started elsewhere (or just now) that the live connection has not told us about yet
      if (r.running && r.kind !== 'generator' && r.kind !== 'sniffer' && !S.live.running) await pullLive();
      const trip = runKind() === 'trip';
      const tsOn = r.running ? !!r.testset : (S.live.done && S.showDone && isTestset(S.live.profile));      // a test set: the page is the table of applications (setup, run and result are one screen)
      if (tsOn) { setScreen('run', 'tests'); await renderTestset(root, show); }
      else if (trip && (S.live.running || (S.live.done && S.showDone))) { setScreen('run', 'tests'); renderRun(root, show); }
      else await renderSetup(root, (t, o) => show(typeof t === 'string' ? t : 'tests', o));
    }
    else if (t === 'results') await renderResults(root);
    else if (t === 'generator') await renderGenerator(root, show);
    else if (t === 'sniffer') await renderSniffer(root, show);
    else if (t === 'config') await renderConfig(root, sub, show);
    if (t !== 'results') S.openRun = null;
    updateBadge(); paintTransport();
  } catch (e) { if (e.message !== 'Password required') root.innerHTML = `<div class="card"><h2>Something went wrong</h2><p>${esc(e.message)}</p></div>`; }
}
$$('#tabs button').forEach(b => b.onclick = () => show(b.dataset.tab));
// One Setup button, two homes: the gear at the right of the lamp strip on a phone, the foot of the left rail on a wide screen.
const PHONE = matchMedia('(max-width:820px), (max-height:480px) and (max-width:1000px)');
function placeSetup() { const b = $('#setupBtn'); if (PHONE.matches) { $('.top-right').appendChild(b); b.classList.add('setup-ico'); b.classList.toggle('rail-setup', false); } else { $('#tabs').appendChild(b); b.classList.toggle('setup-ico', false); b.classList.add('rail-setup'); } }
placeSetup(); PHONE.addEventListener('change', placeSetup);
let lastHash = '';
window.addEventListener('hashchange', () => { const x = parseHash(location.hash); const cur = location.hash.replace(/^#/, ''); if (x && (x.t !== tab || (x.t === 'config' && cur !== lastHash))) show(x.t, { sub: x.sub }); lastHash = cur; });   // browser Back / Forward and typed links

function loginScreen() {
  main.innerHTML = `<div class="card" style="max-width:380px;margin:60px auto"><h2>Password</h2><p class="muted">This Pi Automation Traffic Tester is password-protected.</p>
    <label class="f">Password<input id="pw" type="password" autofocus></label><div class="row" style="margin-top:12px"><button class="btn primary" id="pwGo">Unlock</button></div></div>`;
  const go = async () => { try { await api('/auth/login', { method: 'POST', body: { password: $('#pw').value } }); start(); } catch (e) { toast('Wrong password', true); } };
  $('#pwGo').onclick = go; $('#pw').onkeydown = e => { if (e.key === 'Enter') go(); };
}
document.addEventListener('need-login', loginScreen);

onLive(e => {
  if (e.ev === '_conn') { $('#conn').className = 'dot' + (e.ok ? '' : ' off'); document.body.dataset.ws = e.ok ? 'ok' : 'down'; }
  if (e.ev === 'started' || e.ev === 'done') refreshLive();
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
  $('#piName').title = (hw.model ? `Running on: ${hw.model}${hw.is_pi5 ? '' : ' — built for the Raspberry Pi 5; this works but is not the target'}` : 'Not a Raspberry Pi (development PC). Built for the Raspberry Pi 5.') + ' · tap for Setup';
  await initSetup();
  initAiGuide(); initTransport(show);
  initLamps();
  $('#appVer').addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); show('config/help'); });   // the version label opens Help (About)
  connectWS();
  const h = location.hash.slice(1);
  const px = parseHash(h);
  const first = px ? (px.sub ? `${px.t}/${px.sub}` : px.t) : (st.run.running ? (st.run.kind === 'generator' && !st.run.testset ? 'generator' : st.run.kind === 'sniffer' ? 'sniffer' : 'tests') : 'home');
  if (st.run.running) { S.live.running = true; S.live.runId = st.run.run_id; S.live.thr = st.run.threshold_ms || 60; }
  show(first);
  maybeUpdatedBanner(st);
}
start().catch(e => { main.innerHTML = `<div class="card"><h2>Cannot reach the Pi Automation Traffic Tester</h2><p>${esc(e.message)}</p></div>`; });
