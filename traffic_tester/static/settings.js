// Settings tab: this Pi (name, answering), the other Pis, time source, network settings, software update, web password, routing self-check, Claude Code.
import { $, esc, api, toast, confirmBox } from './ui.js';
import { S } from './state.js';
import { refreshStatus, claudeState, loginUI, logoutUI, draw as drawPanel, waitRestart } from './claude.js';
import { renderUpdate } from './update.js';
import { renderPeersCards } from './peers.js';

export async function renderSettings(root) {
  root.innerHTML = `<div class="card" id="setThisPi"></div><div class="card" id="setPeers"></div><div class="card" id="setClock"></div><div id="netCard" data-testid="network-card"></div>
    <div class="card" id="setUpdate"></div><div class="card" id="setSec"></div><div class="card" id="setNet"></div><div class="card" id="setClaude"></div>
    <p class="muted" style="margin:4px 6px">AI assistants (Tag Along, Claude, ChatGPT sidebars) can read a plain-text manual of this app: <a href="/ai-guide">AI guide</a>.</p>`;
  // The network settings card (Wi-Fi access point, Ethernet ports) is its own module; show nothing when it is not there.
  import('./network.js').then(m => m.renderNetworkCard($('#netCard'))).catch(() => { });
  await Promise.all([renderPeersCards({ pi: $('#setThisPi'), peers: $('#setPeers'), clock: $('#setClock') }), security(), renderUpdate($('#setUpdate')), network(), claude()]);
}

// ---------------------------------------------------------------- web password
async function security() {
  const host = $('#setSec'); const st = S.status || await api('/status');
  host.innerHTML = `<h2>🔒 Web password</h2>
    <p class="muted">${st.password_enabled ? 'A password is set. It protects this app, and it is required to sign in to Claude Code.' : 'No password is set. Anyone who can reach this Pi can use the app. Set one before turning on Claude Code.'}</p>
    <div class="row"><label class="f" style="min-width:240px">${st.password_enabled ? 'New password' : 'Password'}<input id="pwNew" type="password" autocomplete="new-password" placeholder="at least 4 characters"></label>
      <button class="btn" id="pwSave" style="align-self:flex-end">${st.password_enabled ? 'Change password' : 'Set password'}</button>
      ${st.password_enabled ? '<button class="btn ghost" id="pwOff" style="align-self:flex-end">Remove password</button>' : ''}</div>`;
  const save = async pw => {
    try { await api('/auth/set', { method: 'POST', body: { password: pw } }); toast(pw ? 'Password saved.' : 'Password removed.'); if (pw) { try { await api('/auth/login', { method: 'POST', body: { password: pw } }); } catch { } } S.status = await api('/status'); await security(); await claude(); }
    catch (e) { toast(e.message, true); }
  };
  $('#pwSave').onclick = () => save($('#pwNew').value);
  if ($('#pwOff')) $('#pwOff').onclick = async () => { if (await confirmBox('Remove the password?', 'Claude Code cannot be turned on without one.', 'Remove', true)) save(''); };
}

// ---------------------------------------------------------------- routing self-check (internet must leave over Wi-Fi, test traffic over Ethernet)
async function network() {
  const host = $('#setNet'); host.innerHTML = `<h2>🌐 Network check</h2><p class="muted">Checking…</p>`;
  let n; try { n = await api('/net/check'); } catch (e) { host.innerHTML = `<h2>🌐 Network check</h2><p>${esc(e.message)}</p>`; return; }
  const ic = { ok: '✓', warn: '!', bad: '✕' };
  host.innerHTML = `<div class="row sp"><h2>🌐 Network check</h2><button class="btn small" id="netAgain">Check again</button></div>
    <p class="muted">Wi-Fi carries the internet and this web page. The Ethernet test ports carry test traffic only and must never become the way out to the internet.</p>
    ${n.checks.map(c => `<div class="check ${c.s}"><div class="ic">${ic[c.s] || '?'}</div><div><b>${esc(c.title)}</b><small>${esc(c.text)}</small>${c.fix ? `<span class="fixbox">${esc(c.fix)}</span>` : ''}</div></div>`).join('')}`;
  $('#netAgain').onclick = network;
}

// ---------------------------------------------------------------- Claude Code
async function claude() {
  const host = $('#setClaude'); const s = await refreshStatus();
  if (!s.enabled) {
    host.innerHTML = `<h2>✦ Claude Code <span class="pill na">OFF</span></h2>
      <p>Claude Code is off. It cannot see or change anything on this Pi, and the app works fully without it.</p>
      ${s.password_set ? '' : '<p style="color:var(--warn)">Set a web password above first. It protects the sign-in.</p>'}
      <div class="row"><button class="btn primary" id="cLogin" ${s.password_set ? '' : 'disabled'}>Log in</button></div>
      <div id="cLoginHost" class="ask-login" style="padding:12px 0 0;max-width:520px"></div>
      <details><summary>How signing in works</summary><p>You sign in once with your own Claude account ((your Claude subscription; no API key)). The app shows a link and asks for a code: open the link on your phone or PC, approve, and paste the code back here. The Pi never sees your password.</p></details>`;
    $('#cLogin').onclick = () => loginUI($('#cLoginHost'), async () => { await claude(); drawPanel(); });
    return;
  }
  const r = s.remote || {};
  host.innerHTML = `<div class="row sp"><h2>✦ Claude Code <span class="pill pass">ON</span></h2><button class="btn danger" id="cLogout">Log out and turn off</button></div>
    <dl class="kv"><dt>Version</dt><dd>${esc(s.version || 'unknown')}</dd>
      <dt>Signed in</dt><dd>${s.logged_in ? 'Yes (your Claude subscription)' : '<b style="color:var(--warn)">No: the saved sign-in expired. Log out, then log in again.</b>'}</dd>
      <dt>Internet</dt><dd>${s.internet ? 'Reachable over Wi-Fi' : '<b style="color:var(--warn)">Not reachable. Ask Claude needs Wi-Fi internet; the app does not.</b>'}</dd>
      <dt>Remote Control</dt><dd>${r.active ? 'Running (you can continue this Pi’s Claude session from the Claude app on your phone)' : 'Off'}</dd></dl>
    <div class="row" style="margin-top:12px"><button class="btn" id="cRc">${r.active ? 'Stop Remote Control' : 'Start Remote Control'}</button>
      <small class="muted">Remote Control stays off unless you start it. Logging out stops it.</small></div>
    <div id="cOut"></div>
    <h3 style="margin-top:18px">Changes Claude made to this app</h3><div id="cHist" class="muted">Loading…</div>`;
  $('#cLogout').onclick = async () => { const r2 = await logoutUI(); if (r2) { await claude(); showLogoutResult(r2); } };
  $('#cRc').onclick = async () => {
    try { await api(r.active ? '/claude/remote/stop' : '/claude/remote/start', { method: 'POST' }); toast(r.active ? 'Remote Control stopped.' : 'Remote Control started.'); } catch (e) { toast(e.message, true); }
    claude();
  };
  history();
}

function showLogoutResult(r) {
  const host = $('#setClaude');
  const d = document.createElement('div');
  d.innerHTML = `<div class="check ${r.verified ? 'ok' : 'bad'}" style="margin-top:12px"><div class="ic">${r.verified ? '✓' : '✕'}</div><div><b>${esc(r.message)}</b><small>${r.steps.map(esc).join(' · ')}</small>${r.leftovers.length ? `<span class="fixbox">${r.leftovers.map(h => esc(h.path + ' (' + h.what + ')')).join('\n')}</span>` : ''}</div></div>`;
  host.appendChild(d);
}

async function history() {
  const host = $('#cHist'); if (!host) return;
  try {
    const h = await api('/claude/history'); const mine = h.filter(x => x.by_claude && !x.is_revert);
    host.innerHTML = mine.length ? `<table><thead><tr><th>When</th><th>What</th><th></th></tr></thead><tbody>${mine.map(x => `<tr><td>${esc(x.when)}</td><td>${esc(x.subject)}</td><td><button class="btn small" data-c="${esc(x.commit)}">Undo</button></td></tr>`).join('')}</tbody></table>` : 'Nothing yet. When you apply a change Claude suggested, it is listed here and can be undone.';
    host.querySelectorAll('[data-c]').forEach(b => b.onclick = async () => {
      if (!await confirmBox('Undo this change?', 'The app goes back to how it was before. It restarts for a few seconds.', 'Undo', true)) return;
      try { await api('/claude/changes/revert', { method: 'POST', body: { commit: b.dataset.c } }); waitRestart(); } catch (e) { toast(e.message, true); }
    });
  } catch (e) { host.textContent = e.message; }
}
