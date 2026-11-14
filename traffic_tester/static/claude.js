// "Ask Claude": resizable right sidebar chat with Claude Code running on this Pi. The main screen stays usable on the left.
// Off by default. When off the panel only says so and offers a (password-protected) Log in. Nothing here is needed to run tests.
import { $, esc, api, toast, confirmBox } from './ui.js';
import { S, onLive, resetLive } from './state.js';

const C = { open: false, status: null, msgs: [], busy: false, ctrl: null, hooks: { snapshot: () => ({}), nav: () => { }, reload: () => { }, tab: () => null }, acts: {}, draft: '' };
export const claudeState = C;

const PHONE_Q = '(max-width:700px), (max-height:480px) and (max-width:1000px)';      // the panel is full screen there (CSS), so its width is not ours to set
const isPhone = () => matchMedia(PHONE_Q).matches;
const MIN_W = 300;
const maxW = () => Math.max(MIN_W + 40, Math.min(760, Math.round(innerWidth * 0.62)));
const defW = () => Math.max(320, Math.min(480, Math.round(innerWidth * 0.3)));
const clampW = w => Math.max(MIN_W, Math.min(maxW(), Math.round(w)));
const savedW = () => { const v = +localStorage.getItem('tt-ask-w'); return v ? clampW(v) : defW(); };

function setWidth(px, save = true) {
  const w = clampW(px);
  document.documentElement.style.setProperty('--ask-w', w + 'px');
  if (save) localStorage.setItem('tt-ask-w', String(w));
  dispatchEvent(new Event('resize'));                 // charts re-fit to the narrower main area
  return w;
}

export async function refreshStatus() {
  try { C.status = await api('/claude/status'); } catch (e) { C.status = { enabled: false, message: 'Claude Code is off.' }; }
  return C.status;
}

export function initClaude(hooks) {
  Object.assign(C.hooks, hooks);
  setWidth(savedW(), false);
  $('#askBtn').onclick = () => (C.open ? closePanel() : openPanel());
  const h = $('#askHandle');
  h.addEventListener('pointerdown', e => {
    e.preventDefault(); h.setPointerCapture(e.pointerId); document.body.classList.add('ask-resizing');
    const move = ev => setWidth(innerWidth - ev.clientX, false);
    const up = () => { h.removeEventListener('pointermove', move); h.removeEventListener('pointerup', up); h.removeEventListener('pointercancel', up); document.body.classList.remove('ask-resizing'); setWidth(parseInt(getComputedStyle($('#askPanel')).width), true); };
    h.addEventListener('pointermove', move); h.addEventListener('pointerup', up); h.addEventListener('pointercancel', up);
  });
  h.addEventListener('keydown', e => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); setWidth(parseInt(getComputedStyle($('#askPanel')).width) + (e.key === 'ArrowLeft' ? 24 : -24)); }
  });
  addEventListener('resize', () => { if (C.open && !isPhone()) { const w = parseInt(getComputedStyle($('#askPanel')).width); if (w > maxW()) setWidth(maxW(), false); } });
  onLive(onUiCommand);
  const main = $('#main'); let t = null;
  const soon = () => { clearTimeout(t); t = setTimeout(report, 500); };
  main.addEventListener('click', soon); main.addEventListener('change', soon); main.addEventListener('input', soon);
  setInterval(() => { if (C.open) report(); }, 5000);
  // On a phone the panel is full screen and keeps the soft keyboard in view (visualViewport); it never re-opens by itself after a reload.
  if (window.visualViewport) { const fit = () => document.documentElement.style.setProperty('--vvh', visualViewport.height + 'px'); visualViewport.addEventListener('resize', fit); fit(); }
  if (localStorage.getItem('tt-ask-open') === '1' && !isPhone()) openPanel(false);
}

export async function openPanel(focus = true) {
  C.open = true; localStorage.setItem('tt-ask-open', '1');
  document.body.classList.add('ask-open'); $('#askPanel').hidden = false; $('#askBtn').setAttribute('aria-expanded', 'true');
  setWidth(savedW(), false);
  await refreshStatus(); draw();
  if (focus && $('#askIn')) $('#askIn').focus();
  report();
}
export function closePanel() {
  C.open = false; localStorage.setItem('tt-ask-open', '0');
  document.body.classList.remove('ask-open'); $('#askPanel').hidden = true; $('#askBtn').setAttribute('aria-expanded', 'false');
  dispatchEvent(new Event('resize'));
}

// ---------------------------------------------------------------- what Claude can "see": a structured snapshot of the screen
export async function report() {
  if (!C.status || !C.status.enabled) return;
  try { await api('/ui/state', { method: 'POST', body: C.hooks.snapshot() }); } catch { }
}

// ---------------------------------------------------------------- the app reacts to what Claude does (the left screen updates live)
async function onUiCommand(e) {
  if (e.ev !== 'uicmd') return;
  const { nav, reload } = C.hooks;
  if (e.cmd === 'open') {
    if (e.profile_id) { const p = S.profiles.find(x => x.id === e.profile_id); if (p) S.sel = p; }
    nav(e.tab);
  } else if (e.cmd === 'run_started') {
    resetLive(e.profile, e.run_id); try { S.status = await api('/status'); } catch { }
    nav(e.tab || 'run');
  } else if (e.cmd === 'profile_saved') {
    try { S.profiles = await api('/profiles'); } catch { }
    if (['setup', 'generator', 'sniffer'].includes(C.hooks.tab())) reload();
    toast('Claude saved a test profile.');
  }
}

// ---------------------------------------------------------------- drawing
function head(title, extra = '') {
  return `<div class="ask-head"><b>${esc(title)}</b>${extra}<button class="btn small ghost" id="askClose" title="Close the panel" aria-label="Close">✕<span class="only-phone"> Close</span></button></div>`;
}

export function draw() {
  const el = $('#askInner'); if (!el) return;
  const s = C.status || { enabled: false };
  if (!s.enabled) { drawOff(el, s); return; }
  el.innerHTML = head('✦ Ask Claude', '<button class="btn small" id="askNew" title="Start a fresh conversation">New chat</button><button class="btn small ghost" id="askSet" title="Claude Code settings">⚙</button>') +
    `<div class="ask-body" id="askBody" aria-live="polite"></div>
     <div class="ask-foot"><textarea id="askIn" rows="1" placeholder="Ask Claude…" aria-label="Message to Claude"></textarea><button class="btn primary" id="askSend" style="padding:10px 16px;font-size:1rem">Send</button></div>`;
  $('#askClose').onclick = closePanel;
  $('#askNew').onclick = async () => { try { await api('/claude/new', { method: 'POST' }); } catch { } C.msgs = []; C.acts = {}; draw(); };
  $('#askSet').onclick = () => C.hooks.nav('settings');
  const inp = $('#askIn');
  inp.value = C.draft; inp.oninput = () => { C.draft = inp.value; inp.style.height = 'auto'; inp.style.height = Math.min(140, inp.scrollHeight) + 'px'; };
  inp.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
  $('#askSend').onclick = () => (C.busy ? stopChat() : send());
  setBusy(C.busy);
  if (!C.msgs.length) C.msgs.push({ k: 'note', text: 'Ask what a result means, why a test failed, or tell me what to do ("start the 60 ms trip test for 30 seconds"). I can see this screen.' });
  C.msgs.forEach(m => paint(m));
  scrollDown();
}

function drawOff(el, s) {
  el.innerHTML = head('✦ Ask Claude') + `<div class="ask-body"><div class="ask-off" id="askOff">
      <div style="font-size:2rem">⏻</div><h3>Claude Code is off</h3>
      <p class="muted">${esc(s.message && s.message !== 'Claude Code is off.' ? s.message : 'It is switched off on this Pi and cannot see or change anything. The app works fully without it.')}</p>
      <button class="btn primary" id="askLogin" style="padding:10px 22px;font-size:1rem">Log in</button>
      <small class="muted">Signing in takes about a minute. You finish it yourself with your Claude account, and it is protected by this app's web password.</small></div>
      <div class="ask-login" id="askLoginHost"></div></div>`;
  $('#askClose').onclick = closePanel;
  $('#askLogin').onclick = () => loginUI($('#askLoginHost'), async () => { await refreshStatus(); draw(); });
}

function setBusy(b) {
  C.busy = b;
  const btn = $('#askSend'); if (btn) { btn.textContent = b ? 'Stop' : 'Send'; btn.classList.toggle('danger', b); btn.classList.toggle('primary', !b); }
}

const scrollDown = () => { const b = $('#askBody'); if (b) b.scrollTop = b.scrollHeight; };

function paint(m) {
  const body = $('#askBody'); if (!body) return;
  let el = m.el && body.contains(m.el) ? m.el : document.createElement('div');
  if (!m.el || !body.contains(m.el)) { body.appendChild(el); m.el = el; }
  if (m.k === 'user') { el.className = 'msg user'; el.textContent = m.text; }
  else if (m.k === 'bot') { el.className = 'msg bot'; el.textContent = m.text; }
  else if (m.k === 'err') { el.className = 'msg err'; el.textContent = m.text; }
  else if (m.k === 'note') { el.className = 'msg note'; el.textContent = m.text; }
  else if (m.k === 'typing') { el.className = 'typing'; el.textContent = 'Claude is working…'; }
  else if (m.k === 'act') {
    el.className = 'act ' + (m.state || 'wait');
    el.innerHTML = `<span class="ai">${m.state === 'ok' ? '✓' : m.state === 'bad' ? '✕' : '…'}</span><div><b>${esc(ACTION_LABEL[m.name] || m.name)}</b> ${esc(actArgs(m))}<span class="res">${esc(m.result || (m.state === 'wait' ? 'working…' : ''))}</span></div>`;
  } else if (m.k === 'chg') paintChange(el, m);
}

const ACTION_LABEL = { get_screen: 'Looked at the screen', get_status: 'Checked app status', list_profiles: 'Listed test profiles', get_profile: 'Read a profile', list_runs: 'Listed recent tests', get_run: 'Read test results', get_log: 'Read the app log', network_check: 'Checked the network', open_screen: 'Opened a screen', start_test: 'Started a test', stop_test: 'Stopped the test', save_profile: 'Saved a profile', Read: 'Read a file', Grep: 'Searched the code', Glob: 'Looked for files', Edit: 'Edited a file (not applied yet)', Write: 'Wrote a file (not applied yet)' };
function actArgs(m) {
  const i = m.input || {};
  if (m.name === 'start_test') return [i.profile_id != null ? `profile ${i.profile_id}` : i.profile?.name, i.overrides && Object.keys(i.overrides).length ? JSON.stringify(i.overrides).slice(0, 90) : ''].filter(Boolean).join(' · ');
  if (m.name === 'open_screen') return i.tab || '';
  if (m.name === 'save_profile') return i.profile?.name || '';
  if (m.name === 'get_run' || m.name === 'get_profile') return '#' + i.id;
  if (i.file_path || i.path || i.pattern) return String(i.file_path || i.path || i.pattern).split('/').slice(-2).join('/');
  return '';
}
function summarize(text) {
  try { const j = JSON.parse(text); if (j.error) return j.error; if (j.note) return j.note; if (j.run_id) return `test #${j.run_id} started`; if (j.opened) return 'opened ' + j.opened; if (j.stopping) return 'stopping test #' + j.stopping; if (j.id && j.name) return `saved “${j.name}”`; return 'done'; } catch { return String(text).slice(0, 160); }
}

function add(m) { C.msgs.push(m); paint(m); scrollDown(); return m; }

// ---------------------------------------------------------------- chat
async function stopChat() { try { await api('/claude/stop', { method: 'POST' }); } catch { } if (C.ctrl) C.ctrl.abort(); }

async function send() {
  const inp = $('#askIn'); const text = (inp.value || '').trim();
  if (!text || C.busy) return;
  inp.value = ''; C.draft = ''; inp.style.height = 'auto';
  C.msgs = C.msgs.filter(m => m.k !== 'note' || m.text.indexOf('Ask what') !== 0);
  add({ k: 'user', text }); setBusy(true);
  const typing = add({ k: 'typing' });
  await report();
  C.ctrl = new AbortController();
  let sawChange = false;
  try {
    const r = await fetch('/api/claude/chat', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: text }), signal: C.ctrl.signal });
    if (r.status === 401) { document.dispatchEvent(new Event('need-login')); throw new Error('Password required'); }
    if (!r.ok) {
      let d = ''; try { d = (await r.json()).detail; } catch { }
      if (r.status === 403) { await refreshStatus(); drop(typing); draw(); return; }
      throw new Error(d || `Request failed (${r.status})`);
    }
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';
    for (; ;) {
      const { value, done } = await rd.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl; while ((nl = buf.indexOf('\n')) >= 0) { const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1); if (line) { const ev = JSON.parse(line); if (await onEvent(ev, typing)) sawChange = true; } }
    }
  } catch (e) {
    if (e.name !== 'AbortError' && e.message !== 'Password required') add({ k: 'err', text: 'Could not reach Claude: ' + e.message });
    else if (e.name === 'AbortError') add({ k: 'note', text: 'Stopped.' });
  } finally {
    drop(typing); setBusy(false); C.ctrl = null;
    try { S.status = await api('/status'); } catch { }
    if (!sawChange) report();
  }
}

function drop(m) { const i = C.msgs.indexOf(m); if (i >= 0) C.msgs.splice(i, 1); if (m.el) m.el.remove(); }

async function onEvent(ev, typing) {
  if (ev.t === 'text') { drop(typing); add({ k: 'bot', text: ev.text }); C.msgs.push(typing); paint(typing); scrollDown(); }
  else if (ev.t === 'action') { const m = add({ k: 'act', name: ev.name, input: ev.input, state: 'wait', id: ev.id }); C.acts[ev.id] = m; keepTypingLast(typing); }
  else if (ev.t === 'action_result') {
    const m = C.acts[ev.id]; if (m) { m.state = ev.ok ? 'ok' : 'bad'; m.result = summarize(ev.text) || (ev.ok ? 'done' : 'failed'); paint(m); }
  } else if (ev.t === 'error') {
    drop(typing); add({ k: 'err', text: ev.message });
    if (ev.code === 'not_signed_in') await refreshStatus();
  } else if (ev.t === 'done') {
    drop(typing);
    if (ev.changes) { await showChange(); return true; }
  }
  return false;
}
function keepTypingLast(typing) { const b = $('#askBody'); if (b && typing.el && b.contains(typing.el)) b.appendChild(typing.el); scrollDown(); }

// ---------------------------------------------------------------- feature / UI changes: show a diff, the user decides, and every change can be undone
async function showChange() {
  let d; try { d = await api('/claude/changes'); } catch { return; }
  if (d.empty) return;
  add({ k: 'chg', diff: d });
}

function paintChange(el, m) {
  const d = m.diff; el.className = 'chg';
  if (m.state === 'applied') { el.innerHTML = `<b>✓ Change applied.</b> <small class="muted">${esc(m.msg || '')}</small><div class="row" style="margin-top:6px"><button class="btn small" data-a="undo">Undo this change</button></div>`; el.querySelector('[data-a=undo]').onclick = () => undo(m); return; }
  if (m.state === 'discarded') { el.className = 'msg note'; el.textContent = 'Change discarded.'; return; }
  if (m.state === 'undone') { el.className = 'msg note'; el.textContent = 'Change undone.'; return; }
  const lines = d.patch.split('\n').slice(0, 400).map(l => `<span class="${l.startsWith('+') && !l.startsWith('+++') ? 'a' : l.startsWith('-') && !l.startsWith('---') ? 'd' : l.startsWith('@@') || l.startsWith('diff') ? 'h' : ''}">${esc(l)}</span>`).join('\n');
  el.innerHTML = `<b>Claude changed ${d.files.length} file${d.files.length === 1 ? '' : 's'}</b> <small class="muted">${esc(d.stat)}</small>
    <div class="muted" style="margin-top:2px">${d.files.map(f => esc(f.path)).join(', ')}</div>
    ${d.blocked.length ? `<p style="color:var(--fail);margin:6px 0 0">These parts keep Claude Code safe and cannot be applied from here: ${esc(d.blocked.join(', '))}. Discard this change.</p>` : '<p style="margin:6px 0 0">Nothing is live yet. Apply it to use it (the app restarts for a few seconds).</p>'}
    <details><summary>Show the exact changes</summary><pre>${lines}</pre></details>
    <div class="row"><button class="btn small primary" data-a="apply" ${d.blocked.length ? 'disabled' : ''} style="padding:6px 14px;font-size:.9rem">Apply</button><button class="btn small" data-a="discard">Discard</button></div>`;
  el.querySelector('[data-a=apply]').onclick = () => applyChange(m);
  el.querySelector('[data-a=discard]').onclick = async () => { try { await api('/claude/changes/discard', { method: 'POST' }); m.state = 'discarded'; paint(m); } catch (e) { toast(e.message, true); } };
}

async function applyChange(m) {
  if (!await confirmBox('Apply this change?', 'The app restarts for a few seconds. A test cannot be running. You can undo the change afterwards.', 'Apply')) return;
  try {
    const r = await api('/claude/changes/apply', { method: 'POST', body: { message: 'Change made by Claude: ' + m.diff.files.map(f => f.path.split('/').pop()).slice(0, 3).join(', ') } });
    m.state = 'applied'; m.commit = r.commit; m.msg = 'Commit ' + r.commit.slice(0, 8); paint(m);
    if (r.restarting) waitRestart();
  } catch (e) { toast(e.message, true); }
}
async function undo(m) {
  if (!await confirmBox('Undo this change?', 'The app goes back to how it was before. It restarts for a few seconds.', 'Undo', true)) return;
  try { const r = await api('/claude/changes/revert', { method: 'POST', body: { commit: m.commit } }); m.state = 'undone'; paint(m); if (r) waitRestart(); } catch (e) { toast(e.message, true); }
}
export async function waitRestart() {
  toast('Restarting the app with the new code…', false, 8000);
  await new Promise(r => setTimeout(r, 2500));
  for (let i = 0; i < 40; i++) { try { const r = await fetch('/api/status'); if (r.ok) { location.reload(); return; } } catch { } await new Promise(r => setTimeout(r, 1000)); }
}

// ---------------------------------------------------------------- sign-in (shared by the sidebar and Settings)
export async function loginUI(host, onDone) {
  host.innerHTML = `<p class="muted">Starting the sign-in…</p>`;
  let info;
  try { info = await api('/claude/login/start', { method: 'POST' }); }
  catch (e) {
    host.innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
    return;
  }
  const show = i => {
    if (i.status === 'success') { host.innerHTML = `<div class="msg bot">✓ Signed in. Claude Code is on.</div>`; toast('Claude Code is on.'); onDone && onDone(); return; }
    if (i.status === 'failed') { host.innerHTML = `<div class="msg err">${esc(i.error || 'Sign-in did not complete.')}</div><button class="btn small" id="lgAgain">Try again</button>`; $('#lgAgain', host).onclick = () => loginUI(host, onDone); return; }
    host.innerHTML = `<ol style="margin:0;padding-left:1.2em;font-size:.9rem"><li>Open this link on your phone or PC and sign in to your Claude account:</li></ol>
      ${i.url ? `<a class="url" href="${esc(i.url)}" target="_blank" rel="noopener noreferrer">${esc(i.url)}</a>` : '<p class="muted">Waiting for the link…</p>'}
      <ol start="2" style="margin:0;padding-left:1.2em;font-size:.9rem"><li>It shows a code. Paste it here:</li></ol>
      <input id="lgCode" placeholder="Paste the code" autocomplete="off" spellcheck="false" aria-label="Sign-in code"><div class="row"><button class="btn primary small" id="lgGo" style="padding:8px 18px;font-size:.95rem">Finish sign-in</button><button class="btn small" id="lgCancel">Cancel</button></div>`;
    $('#lgCode', host).onkeydown = e => { if (e.key === 'Enter') $('#lgGo', host).click(); };
    $('#lgGo', host).onclick = async () => {
      const code = $('#lgCode', host).value.trim(); if (!code) return;
      host.innerHTML = '<p class="muted">Checking the code…</p>';
      try { show(await api('/claude/login/code', { method: 'POST', body: { code } })); } catch (e) { host.innerHTML = `<div class="msg err">${esc(e.message)}</div>`; }
    };
    $('#lgCancel', host).onclick = async () => { await api('/claude/login/cancel', { method: 'POST' }).catch(() => { }); host.innerHTML = ''; };
  };
  show(info);
}

export async function logoutUI(afterDone) {
  if (!await confirmBox('Log out of Claude Code?', 'This deletes the saved sign-in from this Pi, stops Remote Control and turns Claude Code off. Nobody can use your account from this Pi afterwards. Tests are not affected.', 'Log out', true)) return null;
  try {
    const r = await api('/claude/logout', { method: 'POST' });
    C.msgs = []; C.acts = {}; await refreshStatus(); draw();
    toast(r.verified ? 'Logged out. No saved sign-in is left on this Pi.' : 'Logged out, but check the list in Settings.', !r.verified, 6000);
    if (afterDone) afterDone(r);
    return r;
  } catch (e) { toast(e.message, true); return null; }
}
