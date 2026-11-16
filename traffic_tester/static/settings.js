// Settings tab: this Pi (name, answering), the other Pis, time source, network settings, software update, web password, routing self-check.
import { $, esc, api, toast, confirmBox } from './ui.js';
import { S } from './state.js';
import { renderUpdate } from './update.js';
import { renderPeersCards } from './peers.js';

export async function renderSettings(root) {
  root.innerHTML = `<div class="card" id="setThisPi"></div><div class="card" id="setPeers"></div><div class="card" id="setClock"></div><div id="netCard" data-testid="network-card"></div>
    <div class="card" id="setUpdate"></div><div class="card" id="setSec"></div><div class="card" id="setNet"></div>
    <p class="muted" style="margin:4px 6px">Browser AI assistants (Tag Along, a ChatGPT sidebar) can read a plain-text manual of this app: <a href="/ai-guide">AI guide</a>.</p>`;
  // The network settings card (Wi-Fi access point, Ethernet ports) is its own module; show nothing when it is not there.
  import('./network.js').then(m => m.renderNetworkCard($('#netCard'))).catch(() => { });
  await Promise.all([renderPeersCards({ pi: $('#setThisPi'), peers: $('#setPeers'), clock: $('#setClock') }), security(), renderUpdate($('#setUpdate')), network()]);
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
