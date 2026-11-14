// Settings -> "Software update": upload a signed .ptt from this phone/PC, or check the online channel; install with automatic rollback; roll back; optionally send the update to another Pi.
import { $, esc, api, toast, confirmBox } from './ui.js';
import { listPeers } from './peers.js';

const FINAL = ['ok', 'failed', 'rolled_back', 'refused'];
let pollTimer = null, host = null;

const fmtDate = d => { if (!d) return 'unknown'; const t = new Date(d); return isNaN(t) ? String(d) : t.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }); };
const fmtSize = n => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
const notesHtml = t => t ? `<div class="upd-notes">${esc(t).replace(/\n/g, '<br>')}</div>` : '';

export async function renderUpdate(el) {
  host = el; await paint();
}

async function paint(info) {
  if (!host || !host.isConnected) return;
  try { info = info || await api('/update/status'); } catch (e) { host.innerHTML = `<h2>⬆ Software update</h2><p>${esc(e.message)}</p>`; return; }
  const c = info.current, st = info.status || { state: 'idle' }, on = info.online || {}, last = on.last, sg = info.staged;
  const working = st.state === 'working' && !st.stale;
  const can = info.can_install && !working;
  const pl = await listPeers().catch(() => ({ peers: [] }));
  host.innerHTML = `<h2>⬆ Software update</h2>
    <dl class="kv"><dt>Installed</dt><dd><b>${esc(c.version)}</b> · ${esc(fmtDate(c.date))}</dd>
      ${c.previous_version ? `<dt>Previous</dt><dd>${esc(c.previous_version)} (kept so you can go back)</dd>` : ''}</dl>
    <div id="updStatus">${statusHtml(st)}</div>
    ${info.can_install ? '' : `<div class="check warn"><div class="ic">!</div><div><b>Updates are not available here</b><small>${esc(info.reason)}</small></div></div>`}
    <div class="grid g2 upd-opts">
      <div class="upd-opt"><h3>1 · Upload a file</h3>
        <p class="muted">Pick the update file (<b>.ptt</b>) from this phone or PC. It works with no internet, over Wi-Fi or Ethernet.</p>
        <input type="file" id="updFile" accept=".ptt,application/octet-stream" hidden>
        <button class="btn" id="updPick" ${can ? '' : 'disabled'}>Choose update file…</button>
        <div id="updUp" class="upd-prog" hidden><div class="bar"><div id="updBar" style="width:0%"></div></div><small id="updUpTxt" class="muted"></small></div>
        <div id="updStaged">${sg ? stagedHtml(sg) : ''}</div></div>
      <div class="upd-opt"><h3>2 · Check online</h3>
        <p class="muted">Looks for the newest version on the update channel (${esc(on.repo || 'GitHub')}). Needs internet on this Pi.</p>
        <button class="btn" id="updCheck" ${can && on.can_check ? '' : 'disabled'}>Check for updates</button>
        ${on.can_check ? '' : `<p class="upd-why">${esc(on.reason || '')}</p>`}
        <div id="updOnline">${last ? onlineHtml(last, c.version, can) : ''}</div></div>
    </div>
    <div class="row upd-roll"><button class="btn" id="updRoll" ${can && c.previous_version ? '' : 'disabled'}>Roll back to previous version${c.previous_version ? ' (' + esc(c.previous_version) + ')' : ''}</button>
      <small class="muted">${c.previous_version ? 'Your tests, results and settings are never touched.' : 'Available after the first update.'}</small></div>
    <label class="chk upd-daily"><input type="checkbox" id="updDaily" ${info.daily_check ? 'checked' : ''}> Check for updates daily</label>
    <small class="muted">Only tells you here (a dot on Settings); it never installs by itself.</small>
    ${otherHtml(info, pl.peers)}`;
  bind(info, pl.peers);
  if (working) poll(); else if (info.partner && info.partner.state === 'working') pollPartner();
}

function statusHtml(st) {
  if (!st || st.state === 'idle') return '';
  const cls = st.state === 'ok' ? 'ok' : st.state === 'working' ? 'wait' : st.state === 'refused' ? 'warn' : 'bad';
  const ic = { ok: '✓', wait: '…', warn: '!', bad: '✕' }[cls];
  const title = { working: st.action === 'rollback' ? 'Rolling back…' : 'Installing the update…', ok: 'Update finished', rolled_back: 'Update failed: rolled back automatically', failed: 'Update failed', refused: 'Update not installed' }[st.state] || st.state;
  const phase = { verifying: 'Checking the signature', checking: 'Checking what it needs', packages: 'Installing packages', staging: 'Preparing', installing: 'Switching versions', starting: 'Starting and checking it works', rolling_back: 'Going back to the old version' }[st.phase] || '';
  return `<div class="check ${cls}" data-state="${esc(st.state)}"><div class="ic">${ic}</div><div><b>${esc(title)}</b><small>${esc(st.message || '')}</small>${st.state === 'working' && phase ? `<small class="muted">${esc(phase)} · the app restarts for a few seconds; this page reconnects by itself.</small>` : ''}</div></div>`;
}

function stagedHtml(s) {
  const tag = s.newer ? '<span class="pill pass">newer</span>' : s.same ? '<span class="pill na">same version</span>' : '<span class="pill run">older</span>';
  return `<div class="upd-card"><div class="row sp"><b>Version ${esc(s.version)}</b>${tag}</div><small class="muted">${esc(fmtDate(s.date))} · signature checked ✓</small>
    ${notesHtml(s.notes)}${s.requirements_changed ? '<small class="muted">Includes new Python packages (installed from the file, no internet needed).</small>' : ''}
    ${s.installer_ok === false ? '<p class="upd-why">This update needs the full installer package first (run install-pi.sh from the new package once).</p>' : ''}
    ${s.newer ? '' : `<label class="chk"><input type="checkbox" id="updOlder"> Allow an older version (or reinstall the same one)</label>`}
    <div class="row"><button class="btn primary" id="updInstallFile">Install ${esc(s.version)}</button><button class="btn ghost" id="updDiscard">Discard</button></div></div>`;
}

function onlineHtml(l, cur, can) {
  const when = l.checked ? new Date(l.checked * 1000).toLocaleString() : '';
  const body = l.newer
    ? `<div class="upd-card"><div class="row sp"><b>Version ${esc(l.version)} is available</b><span class="pill pass">newer</span></div><small class="muted">${esc(fmtDate(l.date))} · ${esc(fmtSize(l.size))} · signature checked ✓</small>
        ${notesHtml(l.notes)}${l.requirements_changed ? '<small class="muted">Includes new Python packages.</small>' : ''}
        <div class="row"><button class="btn primary" id="updInstallOnline" ${can ? '' : 'disabled'}>Install ${esc(l.version)}</button></div></div>`
    : `<div class="upd-card"><b>You have the newest version (${esc(cur)}).</b><small class="muted">The channel offers ${esc(l.version)}.</small></div>`;
  return body + (when ? `<small class="muted">Last checked ${esc(when)}</small>` : '');
}

// "Also update another Pi": pick one of the Pis on this network (its saved password is used) or type an address.
function otherHtml(info, peers) {
  const p = info.partner || {};
  const want = sessionStorage.getItem('tt_update_peer') || '';
  const reach = peers.filter(x => x.reachable);
  const pick = reach.some(x => x.id === want) ? want : reach.length === 1 ? reach[0].id : reach.length ? reach[0].id : '__addr';
  const opts = reach.map(x => `<option value="${esc(x.id)}" ${x.id === pick ? 'selected' : ''}>${esc(x.name)} (${esc(x.host)}${x.version ? ', version ' + esc(x.version) : ''})</option>`).join('')
    + `<option value="__addr" ${pick === '__addr' ? 'selected' : ''}>Another address (type it)…</option>`;
  return `<details class="more2 upd-partner upd-other" data-testid="upd-other" ${want || p.state === 'working' ? 'open' : ''}><summary>Also update another Pi</summary>
    <p class="muted">Sends the update that is ready on this Pi (uploaded or downloaded above) to the other Pi, installs it there and waits for it to come back. It must already have software updates. Pick it from the Pis on this network, or type its address.</p>
    <div class="row"><label class="f" style="min-width:230px">Which Pi<select id="updPSel" data-testid="upd-other-select">${opts}</select></label></div>
    <div class="row" id="updPAddr" ${pick === '__addr' ? '' : 'hidden'}><label class="f" style="min-width:200px">Its address<input id="updPHost" data-testid="upd-other-host" inputmode="decimal" autocomplete="off" placeholder="e.g. 192.168.1.20" value="${esc(p.host && !p.name ? p.host : localStorage.getItem('tt_partner') || '')}"></label></div>
    <div class="row" id="updPPwRow"><label class="f" style="min-width:160px">Its password (only if it has one that is not saved)<input id="updPPw" data-testid="upd-other-password" type="password" autocomplete="off"></label></div>
    <div class="row"><button class="btn" id="updPGo" data-testid="upd-other-go" aria-label="Update the other Pi" ${info.staged ? '' : 'disabled'}>Update that Pi</button>${info.staged ? '' : '<small class="muted">Upload or download an update first.</small>'}</div>
    <div id="updPOut" data-testid="upd-other-status">${otherOut(p)}</div></details>`;
}
const otherOut = p => !p || !p.state || p.state === 'idle' ? '' : `<div class="check ${p.state === 'done' ? 'ok' : p.state === 'working' ? 'wait' : 'bad'}"><div class="ic">${p.state === 'done' ? '✓' : p.state === 'working' ? '…' : '✕'}</div><div><b>${p.state === 'done' ? esc(p.name || 'The other Pi') + ' updated' : p.state === 'working' ? 'Updating ' + esc(p.name || p.host || 'the other Pi') + '…' : 'Updating ' + esc(p.name || p.host || 'the other Pi') + ' failed'}</b><small>${esc(p.message || '')}</small></div></div>`;

function bind(info, peers) {
  const c = info.current;
  const pick = $('#updPick', host), file = $('#updFile', host);
  pick.onclick = () => file.click();
  file.onchange = () => file.files[0] && upload(file.files[0]);
  $('#updCheck', host).onclick = async e => {
    const b = e.target; b.disabled = true; b.textContent = 'Checking…';
    try { const l = await api('/update/check', { method: 'POST' }); toast(l.newer ? `Version ${l.version} is available.` : 'You have the newest version.'); }
    catch (err) { toast(err.message, true); }
    paint();
  };
  const io = $('#updInstallOnline', host); if (io) io.onclick = () => install('online', false, io);
  const ifile = $('#updInstallFile', host); if (ifile) ifile.onclick = () => install('upload', !!($('#updOlder', host) || {}).checked, ifile);
  const dis = $('#updDiscard', host); if (dis) dis.onclick = async () => { await api('/update/discard', { method: 'POST' }); paint(); };
  $('#updRoll', host).onclick = async () => {
    if (!await confirmBox('Go back to version ' + c.previous_version + '?', 'The app restarts for a few seconds. Your tests, results and settings are not touched.', 'Roll back', true)) return;
    try { await api('/update/rollback', { method: 'POST' }); paint(); } catch (e) { toast(e.message, true); }
  };
  $('#updDaily', host).onchange = async e => { try { await api('/update/settings', { method: 'POST', body: { daily_check: e.target.checked } }); toast(e.target.checked ? 'Daily check on.' : 'Daily check off.'); } catch (err) { toast(err.message, true); } };
  const pg = $('#updPGo', host), psel = $('#updPSel', host);
  if (psel) {
    const sync = () => {
      const typed = psel.value === '__addr', pe = peers.find(x => x.id === psel.value);
      $('#updPAddr', host).hidden = !typed;
      $('#updPPwRow', host).hidden = !typed && !!pe && (pe.has_password || !pe.password_required);   // a saved password is used by itself
    };
    psel.onchange = () => { if (psel.value !== '__addr') sessionStorage.setItem('tt_update_peer', psel.value); sync(); };
    sync();
  }
  if (pg) pg.onclick = async () => {
    const typed = psel.value === '__addr', h = typed ? $('#updPHost', host).value.trim() : '';
    if (typed) localStorage.setItem('tt_partner', h);
    const body = typed ? { host: h, password: $('#updPPw', host).value } : { peer: psel.value, password: $('#updPPw', host).value };
    try { await api('/update/partner', { method: 'POST', body }); pollPartner(); } catch (e) { toast(e.message, true); }
  };
}

function upload(f) {
  if (!/\.ptt$/i.test(f.name)) { toast('That is not an update file (it should end in .ptt).', true); return; }
  const box = $('#updUp', host), bar = $('#updBar', host), txt = $('#updUpTxt', host);
  box.hidden = false; $('#updStaged', host).innerHTML = '';
  const x = new XMLHttpRequest();
  x.open('POST', '/api/update/upload?name=' + encodeURIComponent(f.name));
  x.setRequestHeader('Content-Type', 'application/octet-stream');
  x.upload.onprogress = e => { if (e.lengthComputable) { const p = Math.round(100 * e.loaded / e.total); bar.style.width = p + '%'; txt.textContent = `Uploading ${fmtSize(e.loaded)} of ${fmtSize(e.total)} (${p}%)`; if (p >= 100) txt.textContent = 'Checking the signature…'; } };
  x.onload = () => {
    let d = {}; try { d = JSON.parse(x.responseText); } catch { }
    if (x.status === 200) { box.hidden = true; paint(); toast(`Version ${d.version} is ready to install.`); }
    else { box.hidden = true; if (x.status === 401) document.dispatchEvent(new Event('need-login')); $('#updStaged', host).innerHTML = `<div class="check bad"><div class="ic">✕</div><div><b>This file was not accepted</b><small>${esc(d.detail || 'Upload failed (' + x.status + ')')}</small></div></div>`; }
  };
  x.onerror = () => { box.hidden = true; toast('The upload did not finish. Check the connection and try again.', true); };
  x.send(f);
}

async function install(source, older, btn) {
  const txt = source === 'online' ? 'The app restarts for a few seconds. If the new version does not start, the Pi goes back to this one automatically.' : 'The app restarts for a few seconds. If the new version does not start, the Pi goes back to this one automatically.';
  if (!await confirmBox('Install the update?', txt, 'Install')) return;
  btn.disabled = true;
  try { await api('/update/install', { method: 'POST', body: { source, allow_older: older } }); toast('Update started.'); paint(); }
  catch (e) { toast(e.message, true); btn.disabled = false; }
}

// While the helper works the app restarts: a failed request is expected, so keep asking until the final state (or a version change) shows up.
function poll() {
  clearTimeout(pollTimer);
  let misses = 0; const t0 = Date.now();
  const tick = async () => {
    if (!host || !host.isConnected) return;
    try {
      const r = await fetch('/api/update/status', { credentials: 'same-origin', cache: 'no-store' });
      if (r.status === 401) { document.dispatchEvent(new Event('need-login')); return; }
      const info = await r.json(); misses = 0;
      const st = info.status || {};
      if (FINAL.includes(st.state)) {
        if (st.state === 'ok') { toast(st.message || 'Updated.'); await new Promise(r => setTimeout(r, 800)); location.reload(); return; }   // new code: reload the page itself
        await paint(info); if (st.state !== 'refused') toast(st.message, st.state === 'failed'); return;
      }
      $('#updStatus', host).innerHTML = statusHtml(st);
    } catch { misses++; $('#updStatus', host).innerHTML = `<div class="check wait"><div class="ic">…</div><div><b>Restarting…</b><small>Waiting for the Pi to come back${misses > 3 ? ' (' + misses + 's)' : ''}.</small></div></div>`; }
    if (Date.now() - t0 > 240000) { $('#updStatus', host).innerHTML = `<div class="check bad"><div class="ic">✕</div><div><b>No answer from the Pi</b><small>It did not come back within 4 minutes. Check that it is powered; it keeps or restores the previous version.</small></div></div>`; return; }
    pollTimer = setTimeout(tick, 1500);
  };
  pollTimer = setTimeout(tick, 1200);
}

function pollPartner() {
  clearTimeout(pollTimer);
  const tick = async () => {
    if (!host || !host.isConnected) return;
    try {
      const info = await api('/update/status'); const p = info.partner || {};
      const o = $('#updPOut', host); if (o) o.innerHTML = otherOut(p);
      if (p.state === 'working') pollTimer = setTimeout(tick, 2000);
    } catch { pollTimer = setTimeout(tick, 3000); }
  };
  tick();
}

// A small dot on the Settings tab when the daily check found something newer (the check only notes it; nothing installs by itself).
export async function updateBadge() {
  try {
    const info = await api('/update/status'); const l = info.online && info.online.last;
    const tab = document.querySelector('[data-tab="settings"]'); if (!tab) return;
    const show = !!(info.daily_check && l && l.newer);
    let d = tab.querySelector('.updot');
    if (show && !d) { d = document.createElement('span'); d.className = 'updot'; d.title = `Version ${l.version} is available (Settings → Software update)`; tab.appendChild(d); }
    if (!show && d) d.remove();
  } catch { }
}

// "Update it too" on a Pi in the list: open this section with that Pi chosen.
document.addEventListener('tt-update-peer', e => { if (host && host.isConnected && e.detail && e.detail.id) { sessionStorage.setItem('tt_update_peer', e.detail.id); paint(); } });
