// Setup (the gear): a short menu, each item its own page. Pis on this network, Network, Time, Software update, Password, Help, Demo.
// The pieces are the same cards Settings used to hold; they moved, nothing was dropped.
import { $, esc, api, toast, confirmBox } from './ui.js';
import { S } from './state.js';
import { renderUpdate } from './update.js';
import { renderPeersCards } from './peers.js';
import { renderAbout } from './about.js';

export const PAGES = [
  ['pis', 'Pis on this network', 'Names, add a Pi by address, the other Pis'],
  ['network', 'Network', 'Wi-Fi and Ethernet settings, routing check'],
  ['time', 'Time', 'Clock source and clock state'],
  ['update', 'Software update', 'Check for updates, install'],
  ['password', 'Password', 'Protect this Pi'],
  ['help', 'Help', 'AI guide, About, what is new'],
  ['demo', 'Demo, no cable', 'Mirrored Bits self-test inside this Pi'],
];
export const pageTitle = sub => (PAGES.find(p => p[0] === sub) || [])[1] || 'Setup';

export async function renderConfig(root, sub, nav) {
  if (!sub || !PAGES.some(p => p[0] === sub)) {
    root.innerHTML = `<h1 class="pgh">Setup</h1><section class="panel" data-testid="setup-menu"><header><span>Setup</span><small>${esc((S.status && S.status.pi && S.status.pi.name) || '')}</small></header>
      <div class="menu">${PAGES.map(([k, t, d]) => `<a class="mrow" href="#config/${k}" data-testid="setup-${k}"><span><b>${t}</b><small>${d}</small></span><i>›</i></a>`).join('')}</div></section>`;
    return;
  }
  root.innerHTML = `<div class="crumb"><a href="#config" data-testid="setup-back">‹ Setup</a><h1 class="pgh">${esc(pageTitle(sub))}</h1></div><div id="cfgBody"></div>`;
  const body = $('#cfgBody', root);
  if (sub === 'pis') {
    body.innerHTML = '<div class="card" id="setThisPi"></div><div class="card" id="setPeers"></div><div class="card" id="setClock" hidden></div>';
    await renderPeersCards({ pi: $('#setThisPi'), peers: $('#setPeers'), clock: $('#setClock') });
  } else if (sub === 'time') {
    body.innerHTML = '<div class="card" id="setThisPi" hidden></div><div class="card" id="setPeers" hidden></div><div class="card" id="setClock"></div>';
    await renderPeersCards({ pi: $('#setThisPi'), peers: $('#setPeers'), clock: $('#setClock') });
  } else if (sub === 'network') {
    body.innerHTML = '<div id="netCard" data-testid="network-card"></div><div class="card" id="setNet"></div>';
    import('./network.js').then(m => m.renderNetworkCard($('#netCard'))).catch(() => { });
    await network();
  } else if (sub === 'update') { body.innerHTML = '<div class="card" id="setUpdate"></div>'; await renderUpdate($('#setUpdate')); }
  else if (sub === 'password') { body.innerHTML = '<div class="card" id="setSec"></div>'; await security(); }
  else if (sub === 'help') {
    body.innerHTML = `<div class="card" data-testid="help-ai"><h2>AI guide</h2><p>Browser AI assistants (Tag Along, a ChatGPT sidebar) can read a plain-text manual of this app. <a class="btn small" href="/ai-guide" data-testid="help-ai-guide">Open the AI guide</a></p></div><div id="aboutBody"></div>`;
    await renderAbout($('#aboutBody'));
    const h1 = $('#aboutBody h1'); if (h1) h1.remove();
  } else if (sub === 'demo') {
    body.innerHTML = '<div class="card"><h2>Mirrored Bits self-test</h2><p class="muted">Two Mirrored Bits emulators inside this Pi, joined by an in-process wire. Try the screen without a second Pi or a cable.</p></div><div id="mbSelfHost"></div>';
    (await import('./run.js')).renderDemo(root, nav);
  }
}

// ---------------------------------------------------------------- web password
async function security() {
  const host = $('#setSec'); const st = S.status || await api('/status');
  host.innerHTML = `<h2>🔒 Web password</h2>
    <p class="muted">${st.password_enabled ? 'A password is set. It protects this app.' : 'No password is set. Anyone who can reach this Pi can use the app.'}</p>
    <div class="row"><label class="f" style="min-width:240px">${st.password_enabled ? 'New password' : 'Password'}<input id="pwNew" type="password" autocomplete="new-password" placeholder="at least 4 characters"></label>
      <button class="btn" id="pwSave" style="align-self:flex-end">${st.password_enabled ? 'Change password' : 'Set password'}</button>
      ${st.password_enabled ? '<button class="btn ghost" id="pwOff" style="align-self:flex-end">Remove password</button>' : ''}</div>`;
  const save = async pw => {
    try { await api('/auth/set', { method: 'POST', body: { password: pw } }); toast(pw ? 'Password saved.' : 'Password removed.'); if (pw) { try { await api('/auth/login', { method: 'POST', body: { password: pw } }); } catch { } } S.status = await api('/status'); await security(); }
    catch (e) { toast(e.message, true); }
  };
  $('#pwSave').onclick = () => save($('#pwNew').value);
  if ($('#pwOff')) $('#pwOff').onclick = async () => { if (await confirmBox('Remove the password?', 'Anyone who can reach this Pi will be able to use the app.', 'Remove', true)) save(''); };
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
