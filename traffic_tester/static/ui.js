// Shared helpers: API, toast, modal, formatting, glossary tooltips.
export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export const GLOSSARY = {
  p99: '99 out of 100 messages were at least this fast. The slowest 1 in 100 are ignored.',
  p95: '95 out of 100 messages were at least this fast.',
  p50: 'The typical (median) message: half were faster, half slower.',
  max: 'The single slowest message in the test.',
  jitter: 'How much the delay changes from one message to the next. Low is steady.',
  loss: 'Messages that never came back within the timeout.',
  threshold: 'The delay limit. A message slower than this counts as late. Default 60 ms.',
  idle: 'The first message after the link has been quiet. Cellular radios go to sleep and can take much longer to wake up than to carry traffic.',
  oneway: 'Delay in each direction separately. Needs both Pis to have synced clocks.',
  rtt: 'Round trip: the first Pi sends, the other Pi sends it straight back, and the first Pi stops the clock.',
  interval: 'How often a new trip message is sent.',
  baud: 'Serial speed. Must match the radio / relay port setting.',
  reflector: 'The other Pi. It always answers test messages so the first Pi can time them.',
  direction: 'Round trip: the far end sends each message back. One way: the delay in a single direction (needs synced clocks). Both ways: each link runs in both directions at the same time.',
  latency_timer: 'USB-serial adapters hold data for up to 16 ms by default. Setting it to 1 ms removes that hidden delay.',
  timeout: 'How long to wait before counting a message as lost.',
  netem: 'Adds artificial delay and loss on this Pi so you can see how the link behaves when it is bad.',
  rule: 'What has to be true for the test to pass.',
  dscp: 'A priority label on IP packets. 46 (EF) is the usual "fast lane" for protection traffic.',
  goose: 'GOOSE: IEC 61850 protection messages. Layer 2 = plain Ethernet frames; Layer 3 = routed IP multicast (R-GOOSE).',
  load: 'Extra UDP traffic sent alongside the test, to see whether the delay grows when the link is busy. Only on Ethernet (UDP or TCP) links.',
  vlan: 'A tag on Ethernet frames. The priority (0-7) tells switches which frames go first; GOOSE normally uses 4. VLAN ID 0 means priority only.',
  tal: 'Time Allowed to Live: how long a receiver trusts a GOOSE message before treating the sender as lost.',
};
// Screens re-render into the same root element: attach each delegated listener once per root, or every click acts twice.
export function bindOnce(root, type, fn) { const k = '_tt_' + type; if (root[k]) root.removeEventListener(type, root[k]); root[k] = fn; root.addEventListener(type, fn); }
export const tip = k => `<i class="tip" tabindex="0" data-tip="${esc(GLOSSARY[k] || k)}">i</i>`;

export async function api(path, opts = {}) {
  const o = { credentials: 'same-origin', ...opts };
  if (o.body && typeof o.body !== 'string') { o.body = JSON.stringify(o.body); o.headers = { 'Content-Type': 'application/json', ...(o.headers || {}) }; }
  const r = await fetch('/api' + path, o);
  if (r.status === 401) { document.dispatchEvent(new Event('need-login')); throw new Error('Password required'); }
  let data = null; const txt = await r.text();
  try { data = txt ? JSON.parse(txt) : null; } catch { data = txt; }
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map(x => x.msg).join('; ') : `Request failed (${r.status})`);
  }
  return data;
}

// `action` = { label, fn }: a button inside the toast (e.g. Undo). It removes the toast and runs fn once.
export function toast(msg, err = false, ms = 4000, action = null) {
  const d = document.createElement('div'); if (err) d.className = 'err';
  const t = document.createElement('span'); t.textContent = msg; d.appendChild(t);
  if (action) { const b = document.createElement('button'); b.type = 'button'; b.className = 'toast-act'; b.textContent = action.label; b.onclick = () => { d.remove(); action.fn(); }; d.appendChild(b); }
  $('#toast').appendChild(d); setTimeout(() => d.remove(), ms);
}

// A sheet (bottom sheet on phones, centred box on desktop) with an HTML body and a row of buttons. Resolves to the id of the button pressed, or null when closed (X, Esc, tap outside).
export function sheetBox(title, html, buttons = [], testid = 'info-sheet') {
  return new Promise(res => {
    const m = $('#modal');
    m.innerHTML = `<div class="box wide" role="dialog" aria-modal="true" aria-label="${esc(title)}" data-testid="${esc(testid)}"><div class="row sp sheet-head"><h2>${esc(title)}</h2><button type="button" class="icon sheet-x" id="mX" aria-label="Close" data-testid="sheet-close">✕</button></div>
      <div class="sheet-body">${html}</div>
      <div class="row sheet-acts">${buttons.map(b => `<button type="button" class="btn ${b.cls || ''}" id="sb-${esc(b.id)}" data-testid="sheet-${esc(b.id)}">${esc(b.label)}</button>`).join('')}</div></div>`;
    m.hidden = false;
    const done = v => { document.removeEventListener('keydown', key, true); m.onclick = null; m.hidden = true; m.innerHTML = ''; res(v); };
    const key = e => { if (e.key === 'Escape') { e.stopPropagation(); done(null); } };
    document.addEventListener('keydown', key, true);
    m.onclick = e => { if (e.target === m) done(null); };
    $('#mX').onclick = () => done(null);
    buttons.forEach(b => { $('#sb-' + b.id).onclick = () => done(b.id); });
    ($('#sb-' + (buttons.find(b => b.cls === 'primary') || buttons[0] || {}).id) || $('#mX')).focus();
  });
}

export function confirmBox(title, text, okLabel = 'OK', danger = false) {
  return new Promise(res => {
    const m = $('#modal');
    m.innerHTML = `<div class="box" role="dialog" aria-modal="true"><h2>${esc(title)}</h2><p>${esc(text)}</p>
      <div class="row" style="justify-content:flex-end"><button class="btn" id="mNo">Cancel</button><button class="btn ${danger ? 'danger' : 'primary'}" id="mYes">${esc(okLabel)}</button></div></div>`;
    m.hidden = false;
    const done = v => { m.hidden = true; m.innerHTML = ''; res(v); };
    $('#mNo').onclick = () => done(false); $('#mYes').onclick = () => done(true); $('#mYes').focus();
  });
}

export function promptBox(title, label, value = '', okLabel = 'Save') {
  return new Promise(res => {
    const m = $('#modal');
    m.innerHTML = `<div class="box" role="dialog" aria-modal="true"><h2>${esc(title)}</h2>
      <label class="f">${esc(label)}<input id="mIn" value="${esc(value)}" maxlength="80"></label>
      <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn" id="mNo">Cancel</button><button class="btn primary" id="mYes">${esc(okLabel)}</button></div></div>`;
    m.hidden = false;
    const done = v => { m.hidden = true; m.innerHTML = ''; res(v); };
    $('#mNo').onclick = () => done(null); $('#mYes').onclick = () => done($('#mIn').value.trim() || null);
    $('#mIn').onkeydown = e => { if (e.key === 'Enter') done($('#mIn').value.trim() || null); };
    $('#mIn').focus(); $('#mIn').select();
  });
}

// Charts follow their box: phone rotation, a resized window. (uPlot draws at a fixed pixel width.)
export function fitPlot(p, el, h) {
  if (p && el && window.ResizeObserver) new ResizeObserver(() => { const w = Math.max(200, el.clientWidth); if (Math.abs(w - p.width) > 1) p.setSize({ width: w, height: h || p.height }); }).observe(el);
  return p;
}

// Eight on/off lamps for the 8 Mirrored Bits (TMB1-8 sent, RMB1-8 received)
export const lamps = (bits, label, prefix = 'TMB') => `<span class="lamps" role="img" aria-label="${esc(label)}">${[0, 1, 2, 3, 4, 5, 6, 7].map(i => `<i class="lamp ${(bits >> i) & 1 ? 'on' : ''}" title="${prefix}${i + 1}">${i + 1}</i>`).join('')}</span>`;

export const ms = v => v == null ? '–' : (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)) + ' ms';
export const num = v => v == null ? '–' : Number(v).toLocaleString();
export const pct = v => v == null ? '–' : (v < 10 ? v.toFixed(2) : v.toFixed(1)) + ' %';
export function dur(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return h ? `${h} h ${m} min` : m ? `${m} min ${x ? x + ' s' : ''}`.trim() : `${x} s`;
}
export const when = iso => iso ? new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';

export const TRANSPORT_NAMES = { serial: 'Serial (RS-232/485)', udp: 'Ethernet · UDP', tcp: 'Ethernet · TCP', goose_l2: 'GOOSE · Layer 2', goose_l3: 'GOOSE · Layer 3 multicast', generator: 'Traffic generator', sniffer: 'Traffic sniffer' };

// Names of the Pis (filled in by endpoints.js from GET /api/peers): "pi1", not "Pi A".
export const NAMES = { me: '', byId: {} };
export const myName = () => NAMES.me || 'this Pi';
// Who an Endpoint ({pi, host, name}) is, in words. Blank = the Pi you are using.
export function epName(ep, blank) {
  const e = ep || {};
  if (e.host) return NAMES.byId[e.host] || e.host;
  if (!e.pi || e.pi === 'self') return blank === undefined ? myName() : blank;
  return NAMES.byId[e.pi] || e.name || e.pi;
}
export const DIRECTION_NAMES = { round_trip: 'Round trip', forward: 'One way: from → to', reverse: 'One way: to → from', both: 'Both ways at once' };
// The two ends of a trip test: [from, to] as words. A typed address in net.peer_host (older tests) is the "to".
export function endsOf(p) {
  const t = p.transport, ip = t === 'udp' || t === 'tcp';
  const from = epName(p.source);
  const hasTo = p.dest && (p.dest.pi || p.dest.host);
  const to = hasTo ? epName(p.dest) : ip ? (p.net.peer_host || '') : '';
  return [from, to];
}

export function describeProfile(p, port) {
  const t = p.transport, dir = p.direction || 'round_trip';
  const [from, to0] = endsOf(p), to = to0 || (t === 'udp' || t === 'tcp' ? '(address needed)' : 'the other end');
  const where = t === 'serial' ? `serial port ${port || p.serial.port} at ${p.serial.baud} baud`
    : t === 'udp' || t === 'tcp' ? `${t.toUpperCase()} port ${p.net.port}`
      : t === 'goose_l2' ? `Layer-2 GOOSE on ${p.goose.interface}` : `Layer-3 multicast GOOSE on ${p.goose.interface}`;
  if (p.mode === 'mirrored') {
    const baud = t === 'serial' ? p.serial.baud : p.mirrored.rate_baud;
    const rule = p.rule.percentile === 100 ? 'the slowest bit change' : `${p.rule.percentile} % of bit changes`;
    return `${from} and ${to} both send a full Mirrored Bits message nonstop over ${where.replace(/ at \d+ baud/, '')} at ${baud} baud and check every message (ROK, RBAD, CBAD); every ${p.mirrored.trip_every_s} s a test bit is flipped and timed to the other end, for ${dur(p.duration_s)}. Pass if ${rule} cross within ${p.threshold_ms} ms, no more than ${p.mirrored.max_down_pct} % of the time has ROK at 0, and RBAD never asserts.`;
  }
  const gaps = p.idle_gaps_s.length ? ` plus quiet-period checks (${p.idle_gaps_s.map(g => dur(g)).join(', ')})` : '';
  const rule = p.rule.percentile === 100 ? 'the slowest message' : `${p.rule.percentile} % of messages`;
  const how = { round_trip: `${from} sends, ${to} sends each message straight back and ${from} times the round trip`,
    forward: `${from} sends to ${to} and the one-way delay is timed`, reverse: `${to} sends to ${from} and the one-way delay is timed`,
    both: `${from} → ${to} and ${to} → ${from} at the same time, each timed as a round trip` }[dir] || '';
  return `Send a message over ${where} every ${p.interval_ms} ms for ${dur(p.duration_s)}${gaps}: ${how}. Pass if ${rule} ${dir === 'forward' || dir === 'reverse' ? 'arrive' : 'come back'} within ${p.threshold_ms} ms and no more than ${p.rule.max_loss_pct} % are lost.`;
}

export function download(name, text, type = 'application/json') {
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([text], { type })); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
