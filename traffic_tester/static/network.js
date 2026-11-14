// Settings -> "Network": Wi-Fi (access point / join a Wi-Fi network / off) and every Ethernet port (DHCP or static, VLAN, label Internet / Test),
// live link + speed + address, which port YOU are connected through, and the safety net: a change that affects your own connection asks first,
// then reverts by itself after 90 s unless you press Keep (the countdown banner works after you reconnect: it only asks /api/net/pending).
import { $, $$, esc, api, toast } from './ui.js';

const LABELS = { internet: 'Internet', test: 'Test', both: 'Internet + Test' };
const CH_BG = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13], CH_A = [36, 40, 44, 48, 149, 153, 157, 161, 165];
let host = null, cfg = null, scanned = null, liveTimer = null, pollTimer = null, tickTimer = null, banner = null;
let pend = null, offline = false, hint = '', lastEventAt = 0, seenPending = false;

const bytes = s => new TextEncoder().encode(s).length;
const val = (id, root = host) => { const e = $('#' + id, root); return e ? e.value.trim() : ''; };
const linkText = p => p.link ? `<span class="net-up">link up${p.speed_mbps ? ' · ' + p.speed_mbps + ' Mb/s' : ''}</span>` : '<span class="net-down">no cable</span>';

// ------------------------------------------------------------------ the countdown banner (everywhere in the app, not only on Settings)
export function initNetworkBanner() { startBanner(); }

function ensureBanner() {
  if (banner && banner.isConnected) return;
  banner = document.createElement('div');
  banner.id = 'netBanner'; banner.className = 'net-banner'; banner.hidden = true;
  banner.setAttribute('data-testid', 'net-pending-banner'); banner.setAttribute('role', 'alert');
  document.body.insertBefore(banner, document.body.firstChild);                       // sticky at the top: pushes the page down instead of covering it
}

function startBanner() {
  ensureBanner();
  if (pollTimer) return;
  const go = async () => {
    try {
      const r = await fetch('/api/net/pending', { credentials: 'same-origin', cache: 'no-store' });
      if (r.ok) { offline = false; onPending(await r.json()); } else offline = r.status !== 401 && r.status !== 404 ? true : offline;
    } catch { offline = true; }
    renderBanner();
    pollTimer = setTimeout(go, 2000);
  };
  go();
  tickTimer = setInterval(renderBanner, 1000);
}

function onPending(j) {
  const p = j.pending;
  if (p && p.state === 'waiting') { pend = { ...p, deadlineMs: Date.now() + (p.seconds_left || 0) * 1000 }; seenPending = true; hint = ''; }
  else if (p && p.state === 'applying') { pend = { ...p, applying: true }; seenPending = true; }
  else {
    const was = pend; pend = null;
    const ev = j.last_event;
    if (ev && ev.at && ev.at * 1000 > lastEventAt) {
      if (was || seenPending) { toast(ev.message || 'The network settings were restored.', ev.type === 'reverted'); if (host && host.isConnected) load(); }
      lastEventAt = ev.at * 1000;
    }
    seenPending = false; if (!offline) hint = '';
  }
}

function renderBanner() {
  if (!banner) return;
  if (pend) {
    const left = pend.applying ? null : Math.max(0, Math.ceil((pend.deadlineMs - Date.now()) / 1000));
    const lines = (pend.summary || []).map(x => `<li>${esc(x)}</li>`).join('');
    banner.innerHTML = `<div class="net-banner-in"><div><b data-testid="net-countdown">${pend.applying ? 'Applying the network change…' : left > 0 ? `Keep these settings? Reverting in ${left} s` : 'Reverting to the previous settings…'}</b>
        ${lines ? `<ul>${lines}</ul>` : ''}${offline ? '<small>Connection lost: reconnect to the Pi; this banner finds it again by itself.</small>' : ''}</div>
      <div class="net-banner-btn"><button class="btn primary" id="netKeep" data-testid="net-keep" ${pend.applying ? 'disabled' : ''}>Keep</button><button class="btn" id="netRevertNow" data-testid="net-revert-now">Revert now</button></div></div>`;
    $('#netKeep', banner).onclick = keep; $('#netRevertNow', banner).onclick = revertNow;
    banner.hidden = false;
  } else if (hint && offline) {
    banner.innerHTML = `<div class="net-banner-in"><div><b>The Pi changed its network and this page lost contact</b><small>${esc(hint)}</small></div></div>`;
    banner.hidden = false;
  } else { banner.hidden = true; banner.innerHTML = ''; }
  document.body.classList.toggle('net-banner-on', !banner.hidden);
}

async function keep() {
  try { const r = await api('/net/keep', { method: 'POST' }); toast(r.message || 'Kept.'); pend = null; renderBanner(); if (host && host.isConnected) load(); } catch (e) { toast(e.message, true); }
}
async function revertNow() {
  try { await api('/net/revert', { method: 'POST' }); toast('Going back to the previous settings…'); pend = null; renderBanner(); setTimeout(() => host && host.isConnected && load(), 1500); } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ the card
export async function renderNetworkCard(root) {
  host = root; startBanner();
  host.setAttribute('data-testid', 'net-card');
  await load();
  clearInterval(liveTimer); liveTimer = setInterval(refreshLive, 8000);
}

async function load() {
  if (!host || !host.isConnected) return;
  try { cfg = await api('/net/config'); } catch (e) { host.innerHTML = `<h2>🌐 Network</h2><p>${esc(e.message)}</p>`; return; }
  paint();
}

async function refreshLive() {
  if (!host || !host.isConnected) { clearInterval(liveTimer); return; }
  try {
    const c = await api('/net/config'); cfg.connected_through = c.connected_through;
    cfg.ports.forEach(p => { const n = c.ports.find(x => x.iface === p.iface); if (n) Object.assign(p, { link: n.link, speed_mbps: n.speed_mbps, ip: n.ip }); });
    cfg.ports.forEach(p => { const e = $(`[data-live="${p.iface}"]`, host); if (e) e.innerHTML = liveHtml(p); });
    const t = $('#netThrough', host); if (t) t.innerHTML = throughHtml();
  } catch { /* offline: the banner says so */ }
}

const liveHtml = p => `${linkText(p)} · ${p.ip ? 'IP ' + esc(p.ip) : 'no address yet'}`;
function throughHtml() {
  const t = cfg.connected_through;
  return t ? `You are connected through: <b data-testid="net-through-iface">${esc(t.text)}</b>` : 'The Pi cannot tell which port you are using (you may be on the Pi itself).';
}

function wifiNow() {
  const w = cfg.wifi;
  if (!w.present) return '<span class="muted">This Pi has no Wi-Fi adapter.</span>';
  if (w.mode === 'fallback') return `<b>Emergency access point is on</b>: the Pi lost its network, so it started its own Wi-Fi <b>${esc(w.ap.ssid || cfg.pi_name)}</b>. Open ${esc(cfg.ap_url)}. It tries the normal settings again every few minutes.`;
  if (w.mode === 'ap') return `Now: <b>access point</b> '${esc(w.ssid || w.ap.ssid)}'. Phones and laptops join it and open <b>${esc(cfg.ap_url)}</b>.`;
  if (w.mode === 'client') return w.joined ? `Now: joined <b>${esc(w.joined)}</b>.` : 'Now: set to join a Wi-Fi network, but not connected.';
  if (w.mode === 'off') return 'Now: <b>off</b>.';
  return 'Now: not known yet.';
}

function paint() {
  const w = cfg.wifi, ev = cfg.last_event;
  const mode0 = w.mode === 'fallback' ? 'client' : (['ap', 'client', 'off'].includes(w.mode) ? w.mode : 'client');
  const evHtml = ev && ['reverted', 'fallback'].includes(ev.type) && Date.now() / 1000 - ev.at < 86400
    ? `<div class="check ${ev.type === 'reverted' ? 'warn' : 'bad'}" data-testid="net-last-event"><div class="ic">!</div><div><b>${ev.type === 'reverted' ? 'Settings were restored' : 'Emergency Wi-Fi'}</b><small>${esc(ev.message)}</small></div></div>` : '';
  host.innerHTML = `<div class="row sp"><h2>🌐 Network</h2><button class="btn small" id="netRefresh" data-testid="net-refresh">Refresh</button></div>
    <p id="netThrough" class="net-through" data-testid="net-through">${throughHtml()}</p>
    ${cfg.available ? '' : `<div class="check warn"><div class="ic">!</div><div><b>Changes are not available here</b><small>${esc(cfg.reason)}</small></div></div>`}
    ${evHtml}
    <div class="net-sec" data-testid="net-wifi"><h3>Wi-Fi</h3>
      <p class="muted" id="netWifiNow" data-testid="net-wifi-now">${wifiNow()}</p>
      <div class="net-seg" role="radiogroup" aria-label="Wi-Fi mode">
        ${[['ap', 'Access point'], ['client', 'Join a Wi-Fi network'], ['off', 'Off']].map(([k, t]) => `<label class="net-opt"><input type="radio" name="netWifiMode" value="${k}" data-testid="net-wifi-mode-${k}" ${k === mode0 ? 'checked' : ''}><span>${t}</span></label>`).join('')}</div>
      <div id="netWifiForm"></div>
      <div class="row"><button class="btn primary" id="netWifiApply" data-testid="net-wifi-apply" ${cfg.available && w.present ? '' : 'disabled'}>Apply Wi-Fi settings</button></div></div>
    <div class="net-sec" data-testid="net-ports"><h3>Ethernet ports</h3>
      ${cfg.ports.length ? cfg.ports.map(portHtml).join('') : '<p class="muted">No Ethernet port found.</p>'}
      <details class="net-help"><summary>What do the labels mean?</summary><p><b>Internet</b>: the Pi may use this port to reach the internet (software updates, Ask Claude).
        <b>Test</b>: test traffic only; it is never the Pi's way out to the internet, so management traffic cannot disturb a test.
        <b>Internet + Test</b>: both. A test that would go over the Pi's Wi-Fi or an Internet-only port is flagged before it starts.</p></details></div>
    <div class="net-sec"><label class="chk"><input type="checkbox" id="netFallback" data-testid="net-fallback" ${cfg.fallback.enabled ? 'checked' : ''} ${cfg.available ? '' : 'disabled'}> If the Pi has no network at all for 3 minutes, start its own Wi-Fi</label>
      <small class="muted">So you can always get back in. It tries the normal settings again every 5 minutes.</small></div>`;
  $('#netRefresh', host).onclick = () => load();
  $$('input[name=netWifiMode]', host).forEach(r => r.onchange = drawWifiForm);
  drawWifiForm();
  $('#netWifiApply', host).onclick = applyWifi;
  $$('.net-port', host).forEach(bindPort);
  $('#netFallback', host).onchange = async e => {
    try { await api('/net/fallback', { method: 'POST', body: { enabled: e.target.checked } }); toast(e.target.checked ? 'Emergency access point on.' : 'Emergency access point off.'); } catch (err) { toast(err.message, true); e.target.checked = !e.target.checked; }
  };
}

// ------------------------------------------------------------------ Wi-Fi form
function drawWifiForm() {
  const mode = ($('input[name=netWifiMode]:checked', host) || {}).value, w = cfg.wifi, el = $('#netWifiForm', host);
  if (mode === 'ap') {
    const a = w.ap, band = a.band === 'a' ? 'a' : 'bg';
    el.innerHTML = `<div class="grid g2 net-grid">
      <label class="f">Network name (SSID)<input id="netSsid" data-testid="net-ssid" maxlength="32" autocomplete="off" autocapitalize="off" value="${esc(a.ssid || cfg.pi_name)}"><small class="muted">Defaults to the Pi's name.</small></label>
      <label class="f">Password (8 to 63 characters)<span class="net-pw"><input id="netPw" data-testid="net-password" type="password" autocomplete="new-password" placeholder="${a.has_password ? 'unchanged' : 'password'}"><button type="button" class="btn small" id="netPwShow" data-testid="net-password-show" aria-label="Show password">Show</button></span></label>
      <label class="f">Band<select id="netBand" data-testid="net-band"><option value="bg" ${band === 'bg' ? 'selected' : ''}>2.4 GHz (works with everything)</option><option value="a" ${band === 'a' ? 'selected' : ''}>5 GHz (faster, shorter range)</option></select></label>
      <label class="f">Channel<select id="netChannel" data-testid="net-channel"></select></label></div>
      <small class="muted">Phones open <b>${esc(cfg.ap_url)}</b> after joining. WPA2.</small>`;
    const fill = () => { const b = val('netBand'); $('#netChannel', host).innerHTML = '<option value="auto">Automatic</option>' + (b === 'a' ? CH_A : CH_BG).map(c => `<option value="${c}" ${String(a.channel) === String(c) && b === band ? 'selected' : ''}>${c}</option>`).join(''); };
    $('#netBand', host).onchange = fill; fill();
    $('#netPwShow', host).onclick = () => { const i = $('#netPw', host); i.type = i.type === 'password' ? 'text' : 'password'; };
  } else if (mode === 'client') {
    el.innerHTML = `<div class="row"><button class="btn" id="netScan" data-testid="net-scan">Scan for networks</button><small class="muted">${w.mode === 'ap' ? 'While the Pi is an access point it cannot scan: type the name instead.' : ''}</small></div>
      <div id="netScanList" class="net-scan" data-testid="net-scan-list"></div>
      <div class="grid g2 net-grid"><label class="f">Network name (SSID)<input id="netSsid" data-testid="net-ssid" maxlength="32" autocomplete="off" autocapitalize="off" value="${esc(w.joined || '')}"></label>
        <label class="f">Password<span class="net-pw"><input id="netPw" data-testid="net-password" type="password" autocomplete="off" placeholder="empty = open network"><button type="button" class="btn small" id="netPwShow" data-testid="net-password-show" aria-label="Show password">Show</button></span></label></div>
      <label class="chk"><input type="checkbox" id="netHidden" data-testid="net-wifi-hidden"> Hidden network</label>`;
    $('#netScan', host).onclick = scan;
    $('#netPwShow', host).onclick = () => { const i = $('#netPw', host); i.type = i.type === 'password' ? 'text' : 'password'; };
    if (scanned) drawScan();
  } else el.innerHTML = '<p class="muted">Wi-Fi switched off. The Pi can then only be reached over an Ethernet cable.</p>';
}

async function scan() {
  const b = $('#netScan', host); b.disabled = true; b.textContent = 'Scanning…';
  try { scanned = (await api('/net/wifi/scan', { method: 'POST' })).networks; drawScan(); } catch (e) { $('#netScanList', host).innerHTML = `<div class="check warn"><div class="ic">!</div><div><b>Scan did not work</b><small>${esc(e.message)}</small></div></div>`; }
  b.disabled = false; b.textContent = 'Scan for networks';
}
function drawScan() {
  const el = $('#netScanList', host); if (!el) return;
  el.innerHTML = scanned.length ? scanned.map((n, i) => `<button class="net-ap" data-i="${i}" data-testid="net-scan-item"><b>${esc(n.ssid)}</b><small>${n.secure ? 'secured' : 'open'} · ${n.signal}% · ch ${esc(n.chan)}</small></button>`).join('') : '<p class="muted">No networks found.</p>';
  $$('.net-ap', el).forEach(b => b.onclick = () => { const n = scanned[+b.dataset.i]; $('#netSsid', host).value = n.ssid; host.dataset.sec = n.sae ? 'sae' : n.secure ? 'wpa-psk' : 'none'; $('#netPw', host).focus(); $$('.net-ap', el).forEach(x => x.classList.toggle('on', x === b)); });
}

function applyWifi() {
  const mode = $('input[name=netWifiMode]:checked', host).value;
  if (mode === 'off') return submit({ wifi: { mode: 'off' } });
  const ssid = val('netSsid'), pw = $('#netPw', host).value;
  if (!ssid) return toast('Enter the network name (SSID).', true);
  if (bytes(ssid) > 32) return toast('A Wi-Fi network name can be at most 32 bytes.', true);
  if (mode === 'ap') {
    if (pw && (pw.length < 8 || pw.length > 63)) return toast('The password must be 8 to 63 characters.', true);
    if (!pw && !cfg.wifi.ap.has_password) return toast('Choose a password of at least 8 characters for the access point.', true);
    const ch = val('netChannel');
    return submit({ wifi: { mode: 'ap', ssid, band: val('netBand'), channel: ch === 'auto' ? null : Number(ch), ...(pw ? { password: pw } : {}) } });
  }
  const sec = host.dataset.sec;
  if (pw && (pw.length < 8 || pw.length > 63)) return toast('The Wi-Fi password must be 8 to 63 characters.', true);
  submit({ wifi: { mode: 'client', ssid, hidden: $('#netHidden', host).checked, ...(pw ? { password: pw } : {}), ...(sec === 'sae' && pw ? { security: 'sae' } : {}) } });
}

// ------------------------------------------------------------------ Ethernet ports
function portHtml(p) {
  const i = p.iface, st = p.method === 'static';
  return `<div class="net-port" data-iface="${esc(i)}" data-testid="net-port-${esc(i)}">
    <div class="row sp net-port-h"><b>${esc(i)}${p.usb ? ' <span class="chip">USB adapter</span>' : ''}${p.you ? ' <span class="chip you" data-testid="net-you-' + esc(i) + '">you are here</span>' : ''}</b><span class="net-live" data-live="${esc(i)}" data-testid="net-live-${esc(i)}">${liveHtml(p)}</span></div>
    <div class="net-seg sm" role="radiogroup" aria-label="Address of ${esc(i)}">
      <label class="net-opt"><input type="radio" name="netM-${esc(i)}" value="dhcp" data-testid="net-port-method-${esc(i)}-dhcp" ${st ? '' : 'checked'}><span>DHCP (automatic)</span></label>
      <label class="net-opt"><input type="radio" name="netM-${esc(i)}" value="static" data-testid="net-port-method-${esc(i)}-static" ${st ? 'checked' : ''}><span>Static address</span></label></div>
    <div class="grid g2 net-grid net-static" ${st ? '' : 'hidden'}>
      <label class="f">IP address<input class="n-ip" data-testid="net-port-ip-${esc(i)}" inputmode="decimal" autocomplete="off" placeholder="192.168.1.20" value="${esc((p.address || '').split('/')[0])}"></label>
      <label class="f">Subnet mask<input class="n-mask" data-testid="net-port-mask-${esc(i)}" inputmode="decimal" autocomplete="off" placeholder="255.255.255.0" value="${esc(p.address ? prefixToMask(+p.address.split('/')[1]) : '255.255.255.0')}"></label>
      <label class="f">Gateway (optional)<input class="n-gw" data-testid="net-port-gw-${esc(i)}" inputmode="decimal" autocomplete="off" value="${esc(p.gateway || '')}"></label>
      <label class="f">DNS servers (optional, comma separated)<input class="n-dns" data-testid="net-port-dns-${esc(i)}" autocomplete="off" value="${esc((p.dns || []).join(', '))}"></label></div>
    <div class="grid g2 net-grid"><label class="f">VLAN id (optional)<input class="n-vlan" data-testid="net-port-vlan-${esc(i)}" inputmode="numeric" autocomplete="off" placeholder="none" value="${p.vlan || ''}"></label>
      <label class="f">This port is for<select class="n-label" data-testid="net-port-label-${esc(i)}">${Object.entries(LABELS).map(([k, t]) => `<option value="${k}" ${p.label === k ? 'selected' : ''}>${t}</option>`).join('')}</select></label></div>
    <div class="row"><button class="btn primary n-apply" data-testid="net-port-apply-${esc(i)}" ${cfg.available ? '' : 'disabled'}>Apply to ${esc(i)}</button></div></div>`;
}
const prefixToMask = n => { const m = n ? (0xFFFFFFFF << (32 - n)) >>> 0 : 0; return [24, 16, 8, 0].map(s => (m >>> s) & 255).join('.'); };

function bindPort(el) {
  const i = el.dataset.iface;
  $$(`input[name="netM-${i}"]`, el).forEach(r => r.onchange = () => { $('.net-static', el).hidden = r.value !== 'static' || !r.checked; });
  $('.n-apply', el).onclick = () => {
    const m = $(`input[name="netM-${i}"]:checked`, el).value, vlan = $('.n-vlan', el).value.trim();
    const p = { method: m, label: $('.n-label', el).value, vlan: vlan ? Number(vlan) : null };
    if (vlan && !/^\d+$/.test(vlan)) return toast('The VLAN id must be a number from 1 to 4094.', true);
    if (m === 'static') {
      p.address = $('.n-ip', el).value.trim(); p.mask = $('.n-mask', el).value.trim();
      const gw = $('.n-gw', el).value.trim(); if (gw) p.gateway = gw;
      p.dns = $('.n-dns', el).value.trim();
      if (!p.address) return toast('Enter the IP address.', true);
    }
    submit({ ports: { [i]: p } });
  };
}

// ------------------------------------------------------------------ apply with plan + confirm + revert countdown
function sheet(title, html, okLabel, danger) {
  return new Promise(res => {
    const m = $('#modal');
    m.innerHTML = `<div class="box" role="dialog" aria-modal="true" data-testid="net-confirm"><h2>${esc(title)}</h2><div class="net-plan">${html}</div>
      <div class="row" style="justify-content:flex-end"><button class="btn" id="mNo" data-testid="net-confirm-no">Cancel</button><button class="btn ${danger ? 'danger' : 'primary'}" id="mYes" data-testid="net-confirm-yes">${esc(okLabel)}</button></div></div>`;
    m.hidden = false;
    const done = v => { m.hidden = true; m.innerHTML = ''; res(v); };
    $('#mNo').onclick = () => done(false); $('#mYes').onclick = () => done(true); $('#mYes').focus();
  });
}

const web = h => `${location.protocol}//${h}:${location.port || 8080}`;      // (the front end carries no literal web addresses: a test forbids them)
function reconnectHint(changes) {
  const w = changes.wifi, out = [];
  if (w && w.mode === 'ap') out.push(`join the Wi-Fi network '${w.ssid}' and open ${cfg.ap_url}`);
  else if (w && w.mode === 'client') out.push(`the Pi gets a new address from '${w.ssid}': look on your router, or try ${web(cfg.hostname + '.local')}`);
  else if (w && w.mode === 'off') out.push('use an Ethernet cable');
  Object.entries(changes.ports || {}).forEach(([i, p]) => out.push(p.method === 'static' ? `reconnect to ${i} on ${web(p.address)} (your PC needs an address in the same network)` : `${i} gets a new address: try ${web(cfg.hostname + '.local')}`));
  return 'To get back to this page: ' + out.join('; ') + '. Then press Keep within 90 seconds.';
}

async function submit(changes) {
  let plan;
  try { plan = await api('/net/plan', { method: 'POST', body: { changes } }); } catch (e) { return toast(e.message, true); }
  if (plan.nothing_to_do) return toast('Nothing to change: these are already the settings.');
  const html = `<ul>${plan.steps.map(s => `<li>${esc(s)}</li>`).join('')}</ul>${plan.reasons.map(r => `<p class="net-warn">${esc(r)}</p>`).join('')}${plan.warnings.map(w => `<p class="net-note">${esc(w)}</p>`).join('')}
    ${plan.will_auto_revert ? `<p><b>Safety net:</b> if you do not press <b>Keep</b> within ${plan.revert_seconds} seconds, the Pi goes back to the old settings by itself. ${esc(reconnectHint(changes))}</p>` : ''}`;
  if (plan.needs_confirm || plan.warnings.length) {
    if (!await sheet(plan.needs_confirm ? 'This changes the connection you are using' : 'Apply these network settings?', html, plan.will_auto_revert ? `Apply (reverts in ${plan.revert_seconds} s unless kept)` : 'Apply', plan.needs_confirm)) return;
  }
  hint = plan.will_auto_revert ? reconnectHint(changes) : '';
  $$('button', host).forEach(b => b.disabled = true);
  try {
    const r = await api('/net/apply', { method: 'POST', body: { changes, confirm: true } });
    if (r.state === 'working') toast('Applying…'); else toast(r.message || 'Applied.');
    (r.warnings || []).forEach(w => toast(w, true, 9000));
    if (!plan.will_auto_revert) hint = '';
  } catch (e) {
    if (e instanceof TypeError || /fetch|network|load failed/i.test(e.message)) { offline = true; toast('Lost contact with the Pi: it changed its network. ' + hint, false, 12000); }   // expected when the change moves the Pi: the banner finds it again
    else { toast(e.message, true, 8000); hint = ''; }
  }
  await new Promise(r => setTimeout(r, 400));
  try { const j = await (await fetch('/api/net/pending', { credentials: 'same-origin', cache: 'no-store' })).json(); onPending(j); renderBanner(); } catch { /* offline */ }
  if (host && host.isConnected) load();
}
