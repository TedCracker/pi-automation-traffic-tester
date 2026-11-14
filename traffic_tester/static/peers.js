// Settings cards that make every Pi a peer: "This Pi" (name, id, addresses, always-on answering), "Pis on this network" (found by themselves or added by IP)
// and "Time source" (which Pi serves the clock). Plain words, phone first. Contract: docs/design/ROLES-DESIGN.md.
import { $, esc, api, toast, confirmBox } from './ui.js';
import { S } from './state.js';

const REFRESH_MS = 10000;
const timers = new Map();

export const listPeers = () => api('/peers');

export const clockText = c => !c ? 'clock unknown' : c.synced ? `clock synced${c.offset_ms != null ? ' ±' + Math.abs(c.offset_ms).toFixed(1) + ' ms' : ''}` : 'not synced';

// Runs fn every REFRESH_MS while the card is on screen and the tab is visible; stops by itself when the card is gone.
function autoRefresh(key, el, fn) {
  clearInterval(timers.get(key));
  timers.set(key, setInterval(() => {
    if (!el.isConnected) { clearInterval(timers.get(key)); timers.delete(key); return; }
    if (document.hidden) return;
    fn();
  }, REFRESH_MS));
}

const pill = (cls, txt, tid) => `<span class="pill ${cls}"${tid ? ` data-testid="${tid}"` : ''}>${esc(txt)}</span>`;

export function renderPeersCards({ pi, peers, clock }) {
  const els = { pi, peers, clock };
  const again = () => Promise.all([paintClockInfo(els), paintPeerList(els)]);
  els.again = again;
  return Promise.all([paintPi(els), paintPeers(els), paintClock(els)]);
}

// ---------------------------------------------------------------- This Pi
async function paintPi(els) {
  const host = els.pi; if (!host) return;
  let pi, info;
  try { [pi, info] = await Promise.all([api('/pi'), api('/peer/info').catch(() => ({}))]); }
  catch (e) { host.innerHTML = `<h2>🥧 This Pi</h2><p>${esc(e.message)}</p>`; return; }
  const ports = pi.reflector_ports || [20500];
  const hw = info.hardware || (S.status && S.status.hardware && S.status.hardware.model) || 'not a Raspberry Pi (development PC)';
  host.setAttribute('data-testid', 'thispi-card');
  host.innerHTML = `<h2>🥧 This Pi</h2>
    <label class="f" style="max-width:420px">Pi name
      <input id="piName" data-testid="thispi-name-input" maxlength="32" autocomplete="off" placeholder="${esc(pi.default_name)}" value="${esc(pi.name === pi.default_name ? '' : pi.name)}"></label>
    <small class="muted" data-testid="thispi-name-help">1 to 32 letters, digits, spaces, dots, dashes or underscores. Leave it empty to use the Pi's own host name (${esc(pi.default_name)}). The other Pis and your results show this name. It is also the Wi-Fi name when this Pi is an access point.</small>
    <div class="row" style="margin-top:8px"><button class="btn" id="piNameSave" data-testid="thispi-name-save">Save name</button><span id="piNameMsg" class="muted" data-testid="thispi-name-msg" role="status"></span></div>
    <dl class="kv" style="margin-top:14px">
      <dt>Pi id</dt><dd data-testid="thispi-id"><code>${esc(pi.id)}</code> <small class="muted">(never changes, even if you rename it)</small></dd>
      <dt>Addresses</dt><dd data-testid="thispi-addresses">${(pi.ips || []).length ? pi.ips.map(a => `<code>${esc(a)}</code>`).join(', ') : '<span class="muted">none yet (no network cable or Wi-Fi connected)</span>'}</dd>
      <dt>Web page</dt><dd><code>port ${esc(pi.port)}</code></dd>
      <dt>Hardware</dt><dd data-testid="thispi-hardware">${esc(hw)}</dd>
      <dt>Software</dt><dd>${esc(pi.version)}</dd>
      <dt>Answers tests</dt><dd data-testid="thispi-reflector">Always answers UDP and TCP test messages on port ${ports.map(esc).join(', ')}. Nothing to switch on: other Pis can send to this one at any time.</dd>
      <dt>Finding other Pis</dt><dd data-testid="thispi-mdns-status">${pi.discovery_error
        ? `<b style="color:var(--warn)">Automatic finding is off:</b> ${esc(pi.discovery_error)}. You can still add Pis by their IP address below.`
        : `This Pi announces itself on the network as <b>${esc(pi.name)}</b> (mDNS, service <code>_pitester._tcp</code>) and finds the others the same way.`}</dd></dl>
    ${pi.migrated && pi.migrated.note ? `<div class="check ok" data-testid="thispi-migrated"><div class="ic">i</div><div><b>Updated from an older version</b><small>${esc(pi.migrated.note)} Your tests and results are unchanged.</small></div></div>` : ''}
    <details class="more2" style="margin-top:10px"><summary data-testid="thispi-advanced">Advanced: test ports</summary>
      <p class="muted">The ports this Pi always answers on, for UDP and TCP. 20500 is the default and is what the tests use unless you change a test's port. Add another port here only if a firewall or radio needs it. Give 1 to 8 port numbers, separated by commas.</p>
      <div class="row"><label class="f" style="min-width:200px">Ports<input id="piPorts" data-testid="thispi-ports-input" inputmode="numeric" autocomplete="off" value="${esc(ports.join(', '))}"></label>
        <button class="btn" id="piPortsSave" data-testid="thispi-ports-save" style="align-self:flex-end">Save ports</button></div>
      <div id="piPortsMsg" class="muted" data-testid="thispi-ports-msg" role="status"></div></details>`;
  const msg = (id, t, bad) => { const m = $(id, host); m.textContent = t; m.style.color = bad ? 'var(--fail)' : ''; };
  $('#piNameSave', host).onclick = async () => {
    try {
      const r = await api('/pi', { method: 'PUT', body: { name: $('#piName', host).value.trim() } });
      msg('#piNameMsg', 'Saved: this Pi is now called ' + r.name + '.');
      if (S.status) S.status.pi = { ...(S.status.pi || {}), name: r.name };
      document.dispatchEvent(new CustomEvent('tt-pi-renamed', { detail: { name: r.name } }));
      toast('This Pi is now called ' + r.name + '.');
      paintPi(els); if (els.again) els.again();
    } catch (e) { msg('#piNameMsg', e.message, true); }
  };
  $('#piName', host).onkeydown = e => { if (e.key === 'Enter') $('#piNameSave', host).click(); };
  $('#piPortsSave', host).onclick = async () => {
    const raw = $('#piPorts', host).value.split(/[\s,;]+/).filter(Boolean);
    const list = raw.map(x => /^\d+$/.test(x) ? parseInt(x, 10) : NaN);
    if (!list.length || list.some(n => !(n >= 1 && n <= 65535))) { msg('#piPortsMsg', 'Give 1 to 8 port numbers between 1 and 65535, like 20500.', true); return; }
    try { const r = await api('/reflector/ports', { method: 'PUT', body: { ports: list } }); msg('#piPortsMsg', 'Saved. This Pi now answers on port ' + r.ports.join(', ') + '.'); toast('Test ports saved.'); }
    catch (e) { msg('#piPortsMsg', e.message, true); }
  };
}

// ---------------------------------------------------------------- Pis on this network
async function paintPeers(els) {
  const host = els.peers; if (!host) return;
  host.setAttribute('data-testid', 'peers-card'); host._els = els;
  host.innerHTML = `<div class="row sp"><h2>📡 Pis on this network</h2>
      <button class="btn small" id="peersRefresh" data-testid="peers-refresh">Look again</button></div>
    <p class="muted">Other Pis running this app show up here by themselves when they are on the same network. You can then tick them under <b>From → To</b> in a test. This list refreshes every 10 seconds.</p>
    <div id="peersWarn"></div>
    <div id="peerList" data-testid="peers-list" aria-live="polite"><p class="muted">Looking…</p></div>
    <h3 style="margin-top:16px">Add a Pi by IP address</h3>
    <p class="muted">For networks that do not pass discovery messages (some radios, routers and VPNs). Type its IP address or name, like <code>192.168.1.20</code> or <code>pi2.local</code>; add <code>:port</code> if it does not use 8080.</p>
    <div class="row"><label class="f" style="min-width:210px">Address<input id="peerAddHost" data-testid="peer-add-host" inputmode="url" autocomplete="off" autocapitalize="off" placeholder="192.168.1.20"></label>
      <label class="f" style="min-width:170px">Its password (if it has one)<input id="peerAddPw" data-testid="peer-add-password" type="password" autocomplete="off"></label>
      <button class="btn" id="peerAddGo" data-testid="peer-add-go" style="align-self:flex-end">Add this Pi</button></div>
    <div id="peerAddErr" class="check bad" data-testid="peer-add-error" role="alert" hidden><div class="ic">✕</div><div><b id="peerAddErrT"></b></div></div>`;
  $('#peersRefresh', host).onclick = async e => {
    const b = e.target; b.disabled = true; b.textContent = 'Looking…';
    try { await api('/peers/refresh', { method: 'POST' }); } catch (err) { toast(err.message, true); }
    await els.again(); b.disabled = false; b.textContent = 'Look again';
  };
  const go = async () => {
    const hostv = $('#peerAddHost', host).value.trim(), err = $('#peerAddErr', host);
    err.hidden = true;
    if (!hostv) { $('#peerAddErrT', host).textContent = 'Type the IP address of the Pi first (like 192.168.1.20).'; err.hidden = false; return; }
    const b = $('#peerAddGo', host); b.disabled = true; b.textContent = 'Looking for it…';
    try {
      const r = await api('/peers', { method: 'POST', body: { host: hostv, password: $('#peerAddPw', host).value } });
      toast('Added ' + (r.name || hostv) + '.'); $('#peerAddHost', host).value = ''; $('#peerAddPw', host).value = '';
      await els.again();
    } catch (e) { $('#peerAddErrT', host).textContent = e.message; err.hidden = false; }
    b.disabled = false; b.textContent = 'Add this Pi';
  };
  $('#peerAddGo', host).onclick = go;
  $('#peerAddHost', host).onkeydown = e => { if (e.key === 'Enter') go(); };
  await paintPeerList(els);
  autoRefresh('peers', host, () => els.again());
}

function peerRow(p, me) {
  const how = p.source === 'manual' ? 'added by hand' : p.source === 'both' ? 'found automatically and added by hand' : 'found automatically';
  const needsPw = p.auth === 'needs_password';
  const tags = [
    p.reachable ? pill('pass', '✓ reachable', 'peer-reachable') : pill('fail', '✗ not reachable', 'peer-reachable'),
    p.reachable ? pill(p.clock && p.clock.synced ? 'pass' : 'na', clockText(p.clock), 'peer-clock') : '',
    p.busy ? pill('run', 'busy with a test', 'peer-busy') : '',
    needsPw ? pill('fail', 'needs password', 'peer-needs-password') : p.password_required && p.reachable ? pill('na', p.has_password ? 'password saved' : 'has a password', 'peer-password-state') : '',
  ].join(' ');
  const url = `${location.protocol}//${p.host.includes(':') && !p.host.startsWith('[') ? '[' + p.host + ']' : p.host}:${p.port}`;
  return `<div class="peer" data-testid="peer-row" data-peer-id="${esc(p.id)}" data-peer-name="${esc(p.name)}">
    <div class="row sp"><b data-testid="peer-name">${esc(p.name)}</b><small class="muted">${esc(p.version ? 'version ' + p.version : '')}</small></div>
    <small class="muted" data-testid="peer-address">${esc(p.host)}:${esc(p.port)}${p.hardware ? ' · ' + esc(p.hardware) : ''} · ${esc(how)}</small>
    <div class="peer-tags">${tags}</div>
    ${!p.reachable && p.error ? `<small class="peer-err" data-testid="peer-error">${esc(p.error)}</small>` : ''}
    ${needsPw ? '<small class="peer-err">This Pi has a web password. Set it here so this Pi can control it.</small>' : ''}
    <div class="peer-acts">
      <a class="btn small" href="${esc(url)}" target="_blank" rel="noopener" data-testid="peer-open">Open its page</a>
      <button class="btn small ${needsPw ? 'primary' : ''}" data-act="pw" data-testid="peer-password">Set password</button>
      <button class="btn small" data-act="clock" data-testid="peer-clock-source" ${p.reachable ? '' : 'disabled'} title="Makes ${esc(me.name)} the clock this Pi follows">Use this Pi as its time source</button>
      <button class="btn small" data-act="update" data-testid="peer-update" ${p.reachable ? '' : 'disabled'}>Update it too</button>
      ${p.source !== 'mdns' ? '<button class="btn small ghost" data-act="remove" data-testid="peer-remove">Remove</button>' : ''}</div>
    <div class="peer-pw row" hidden data-testid="peer-password-form"><label class="f" style="min-width:200px">Password of ${esc(p.name)}<input type="password" autocomplete="off" data-testid="peer-password-input"></label>
      <button class="btn small" data-act="pwsave" data-testid="peer-password-save" style="align-self:flex-end">Save</button><span class="muted" data-role="pwmsg" role="status"></span></div>
    ${p.source === 'mdns' ? '<small class="muted">Found by itself, so it cannot be removed: it comes back by itself while it is on the network.</small>' : p.source === 'both' ? '<small class="muted">Remove only forgets the hand-added entry. It is found automatically, so it comes back by itself.</small>' : ''}</div>`;
}

async function paintPeerList(els) {
  const host = els.peers; if (!host || !host.isConnected) return;
  const list = $('#peerList', host); if (!list) return;
  const open = $('.peer-pw:not([hidden])', list);
  if (open && ($('input', open).value || open.contains(document.activeElement))) return;     // do not wipe a password someone is typing
  let d;
  try { d = await listPeers(); } catch (e) { list.innerHTML = `<p>${esc(e.message)}</p>`; return; }
  els.last = d;
  const w = $('#peersWarn', host);
  if (w) w.innerHTML = d.discovery_error ? `<div class="check warn" data-testid="peers-discovery-error"><div class="ic">!</div><div><b>Automatic finding is off</b><small>${esc(d.discovery_error)}. Add Pis by their IP address below.</small></div></div>` : '';
  if (!d.peers.length) {
    list.innerHTML = `<div class="check wait" data-testid="peers-empty"><div class="ic">…</div><div><b>No other Pi found yet.</b><small>They appear here by themselves when they are on the same network; or add one by its IP address.</small></div></div>`;
    return;
  }
  list.innerHTML = d.peers.map(p => peerRow(p, d.me)).join('');
  $$peer(list, d);
}

function $$peer(list, d) {
  list.querySelectorAll('[data-peer-id]').forEach(row => {
    const id = row.dataset.peerId, p = d.peers.find(x => x.id === id), name = p ? p.name : id;
    const pwBox = $('.peer-pw', row), msg = $('[data-role=pwmsg]', row);
    row.querySelectorAll('[data-act]').forEach(b => b.onclick = async () => {
      const act = b.dataset.act;
      try {
        if (act === 'pw') { pwBox.hidden = !pwBox.hidden; if (!pwBox.hidden) $('input', pwBox).focus(); }
        else if (act === 'pwsave') {
          const r = await api(`/peers/${encodeURIComponent(id)}/password`, { method: 'POST', body: { password: $('input', pwBox).value } });
          msg.textContent = r && r.auth === 'ok' ? 'Saved: it accepted the password.' : 'Saved.'; toast('Password saved for ' + name + '.');
          setTimeout(() => { pwBox.hidden = true; $('input', pwBox).value = ''; document.dispatchEvent(new CustomEvent('tt-peers-changed')); }, 900);
        } else if (act === 'remove') {
          if (!await confirmBox('Remove ' + name + '?', p && p.source === 'both' ? 'Only the hand-added entry goes. It is also found automatically, so it comes back by itself.' : 'It is only removed from this list. The Pi itself is not touched.', 'Remove', true)) return;
          await api('/peers/' + encodeURIComponent(id), { method: 'DELETE' }); toast(name + ' removed.'); document.dispatchEvent(new CustomEvent('tt-peers-changed'));
        } else if (act === 'clock') {
          if (!await confirmBox('Use this Pi as the time source for ' + name + '?', name + ' will follow this Pi\'s clock from now on. One-way delay numbers need both Pis on the same clock.', 'Do it')) return;
          b.disabled = true;
          await api(`/peers/${encodeURIComponent(id)}/clock-source`, { method: 'POST', body: { source: 'self' } });
          toast(name + ' will follow this Pi for time (it can take a minute to settle).'); document.dispatchEvent(new CustomEvent('tt-peers-changed'));
        } else if (act === 'update') {
          sessionStorage.setItem('tt_update_peer', id);
          document.dispatchEvent(new CustomEvent('tt-update-peer', { detail: { id } }));
          const u = document.getElementById('setUpdate'); if (u) u.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      } catch (e) { toast(e.message, true); if (act === 'pwsave') msg.textContent = e.message; b.disabled = false; }
    });
  });
}
document.addEventListener('tt-peers-changed', () => { const a = [document.getElementById('setPeers')].filter(Boolean); if (a.length && a[0]._els) a[0]._els.again(); });

// ---------------------------------------------------------------- Time source
const CLOCK_HELP = 'One-way delay numbers (how long a message takes in each direction) are only meaningful when both Pis run on the same clock. One Pi serves the time and the others follow it.';

async function paintClock(els) {
  const host = els.clock; if (!host) return;
  host.setAttribute('data-testid', 'clock-card');
  host.innerHTML = `<h2>🕒 Time source</h2>
    <p class="muted" data-testid="clock-why-it-matters">${esc(CLOCK_HELP)}</p>
    <div id="clkInfo" data-testid="clock-info"></div>
    <div class="row" style="margin-top:6px"><label class="f" style="min-width:240px;flex:1">Which Pi serves the time
      <select id="clkSel" data-testid="clock-source-select"></select></label>
      <button class="btn" id="clkSave" data-testid="clock-save" style="align-self:flex-end">Use this</button></div>
    <small class="muted" id="clkHelp"></small>
    <div id="clkMsg" data-testid="clock-msg" role="status"></div>`;
  host._els = els;
  const sel = $('#clkSel', host);
  sel.onchange = () => { sel.dataset.dirty = '1'; helpFor(host); };
  $('#clkSave', host).onclick = async () => {
    const b = $('#clkSave', host); b.disabled = true;
    try { const d = await api('/clock/source', { method: 'PUT', body: { source: sel.value } }); delete sel.dataset.dirty; toast('Time source saved.'); fillInfo(host, d); setTimeout(() => paintClockInfo(els), 2500); }
    catch (e) { $('#clkMsg', host).innerHTML = `<div class="check bad"><div class="ic">✕</div><div><b>${esc(e.message)}</b></div></div>`; }
    b.disabled = false;
  };
  await paintClockInfo(els);
  autoRefresh('clock', host, () => paintClockInfo(els));
}

function helpFor(host) {
  const v = $('#clkSel', host).value;
  $('#clkHelp', host).textContent = v === 'auto' ? 'Automatic: the Pi that was set up first serves the time and the others follow it. Nothing to do when you add a Pi.'
    : v === 'self' ? 'This Pi serves the time. Make the other Pis follow it (use "Use this Pi as its time source" on each one above, or set them to Automatic).'
      : v === 'keep' ? 'Leaves the time setup exactly as the installer made it. Nothing is changed.'
        : 'This Pi follows the chosen Pi for time.';
}

function decisionText(d, me) {
  if (!d) return '';
  if (d.mode === 'serve') return `This Pi (${me}) serves the time.`;
  if (d.mode === 'client') return `This Pi follows ${d.source_name || d.server}${d.server ? ' (' + d.server + ')' : ''}.`;
  return d.source_name ? `Waiting for ${d.source_name}.` : 'Left as the installer set it up.';
}

function fillInfo(host, d, peers) {
  const info = $('#clkInfo', host); if (!info) return;
  const st = d.state || {}, dec = d.decision || {}, last = d.last || {}, res = last.result || {};
  const meName = (S.status && S.status.pi && S.status.pi.name) || 'this Pi';
  const want = { mode: dec.mode, server: dec.server || null }, ap = d.applied || {};
  const applied = dec.mode === 'keep' ? null : (ap.mode === want.mode && (ap.server || null) === want.server);
  let appliedHtml = '';
  if (dec.mode === 'keep') appliedHtml = 'Nothing to change: the installer\'s time setup stays.';
  else if (applied) appliedHtml = '<b style="color:var(--pass)">✓ Applied.</b>';
  else if (res && res.ok === false) {
    const m = String(res.message || '');
    appliedHtml = `<b style="color:var(--warn)">Not applied yet:</b> ${esc(m)}${/helper|install/i.test(m) ? ' This needs the one-time installer step: run the installer (install-pi.sh) from the new package once on this Pi.' : ''}`;
  } else appliedHtml = 'Applying… this can take a minute.';
  info.innerHTML = `<dl class="kv">
    <dt>This Pi's clock</dt><dd data-testid="clock-state">${st.synced ? `<b style="color:var(--pass)">${esc(clockText(st))}</b>` : `<b style="color:var(--warn)">${esc(clockText(st))}</b>`}${st.source ? ` <small class="muted">(${esc(st.source)})</small>` : ''}</dd>
    <dt>Follows</dt><dd data-testid="clock-decision">${esc(decisionText(dec, meName))}${dec.why ? `<br><small class="muted" data-testid="clock-why">${esc(dec.why)}</small>` : ''}</dd>
    <dt>Change applied</dt><dd data-testid="clock-applied">${appliedHtml}</dd></dl>`;
  const sel = $('#clkSel', host);
  if (sel && !sel.dataset.dirty) {
    const opts = [['auto', 'Automatic (the Pi that was set up first serves time)'], ['self', 'This Pi (' + meName + ')']];
    const seen = new Set();
    (peers || []).forEach(p => { opts.push([p.id, p.name + (p.reachable ? '' : ' (not reachable)')]); seen.add(p.id); });
    opts.push(['keep', 'Leave as the installer set it']);
    const cur = d.setting === 'self' || d.setting === (S.status && S.status.pi && S.status.pi.id) ? 'self' : d.setting;
    if (cur && !['auto', 'self', 'keep'].includes(cur) && !seen.has(cur)) opts.splice(2, 0, [cur, 'A Pi that is not in the list now (' + cur + ')']);
    sel.innerHTML = opts.map(([v, t]) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(t)}</option>`).join('');
    helpFor(host);
  }
}

async function paintClockInfo(els) {
  const host = els.clock; if (!host || !host.isConnected) return;
  try {
    const [d, pl] = await Promise.all([api('/clock/source'), listPeers().catch(() => ({ peers: [] }))]);
    if (!S.status || !S.status.pi) { try { S.status = await api('/status'); } catch { } }
    fillInfo(host, d, pl.peers);
  } catch (e) { const i = $('#clkInfo', host); if (i) i.innerHTML = `<p>${esc(e.message)}</p>`; }
}
