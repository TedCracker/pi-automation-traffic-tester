// Sniffer: set up the tap and the capture filter, then work in the workbench: filter bar on top, packet list, decode, bytes, flows.
// The same workbench shows a live capture and a saved one (opened from the list below the setup, from Results, or right after Stop).
import { $, $$, esc, api, toast, confirmBox, promptBox, tip, ms, num, dur, when, bindOnce } from './ui.js';
import { S, onLive, resetLive } from './state.js';

let G = { profiles: [], selId: null, p: null, pre: null, checks: null, off: null, sel: null, runs: [], open: null };
let W = null;                                                  // workbench state (null on the setup screen)
let navigate = () => { };
const clone = o => JSON.parse(JSON.stringify(o));
const MODES = { eth_bridge: 'Inline Ethernet tap', eth_span: 'SPAN / mirror port', serial: 'Serial tap (RS-232 / RS-485)', wifi: 'Wi-Fi monitor' };
const PROTOS = { modbus_rtu: 'Modbus RTU', dnp3: 'DNP3', raw: 'Raw bytes (no decoding)' };
const HOW = {
  eth_bridge: 'Put the Pi between two devices: device A → <strong>built-in port</strong> · Pi · <strong>USB Ethernet adapter</strong> → device B. Traffic passes straight through (the Pi has no address on it). <strong>Open this page over Wi-Fi</strong> — the two wired ports are busy.',
  eth_span: "Plug a switch's mirror (SPAN) port into one of the Pi's network ports. The Pi only listens; it never sends.",
  serial: 'Wire each direction of the line to its own USB-serial adapter, listening only (RS-232: data pin → pin 2 of the adapter, grounds joined). RS-485 two-wire: one adapter hears both directions.',
  wifi: "Best: a USB Wi-Fi adapter that supports monitor mode (e.g. Alfa AWUS036ACM or AWUS036AXML) - reliable, and the Pi keeps its normal Wi-Fi. Experimental alternative: the Pi 5's <strong>built-in Wi-Fi</strong> with Nexmon (see below). The adapter listens on one channel, or hops through a list.",
};

export async function renderSniffer(root, nav) {
  navigate = nav; if (G.off) G.off(); stopWb();
  G.profiles = (await api('/profiles')).filter(p => p.kind === 'sniffer');
  if (!G.p && G.profiles.length) { G.selId = G.profiles[0].id; G.p = clone(G.profiles[0]); }
  if (S.openCapture) { G.open = { rid: S.openCapture, fromDone: false }; S.openCapture = null; }
  if (G.open && G.open.fromDone && !S.live.done) G.open = null;                 // Clear took us back to the setup
  if (!running() && hasResult() && S.live.done.status !== 'failed' && !G.open) G.open = { rid: S.live.runId, fromDone: true };
  if (!running() && !G.open) {
    try { G.pre = await api('/sniff/preflight', { method: 'POST', body: {} }); } catch { G.pre = null; }
    try { G.bw = await api('/wifi/builtin/status'); } catch { G.bw = null; }
    try { G.runs = (await api('/runs?limit=60')).filter(r => r.kind === 'sniffer' && r.status !== 'running' && ((r.headline || {}).sent || 0) > 0).slice(0, 8); } catch { G.runs = []; }
  }
  draw(root);
  G.off = onLive(e => {
    if (['done', 'started'].includes(e.ev)) { if (e.ev === 'done' && W && W.live && e.status !== 'failed') G.open = { rid: W.rid, fromDone: true }; draw(root); }
    else if (e.ev === 'sstate') wbCounts();
  });
  bindOnce(root, 'click', ev => click(ev, root));
  bindOnce(root, 'change', ev => change(ev, root));
  bindOnce(root, 'input', ev => wbInput(ev));
}
const running = () => S.live.profile ? (S.live.running && S.live.profile.kind === 'sniffer') : (S.status.run.running && S.status.run.kind === 'sniffer');
const hasResult = () => S.live.done && S.live.profile && S.live.profile.kind === 'sniffer';

function draw(root) {
  const run = running();
  stopWb();
  if (run) { root.innerHTML = wbHtml({ src: 'live', live: true }); wbInit(root); return; }
  if (G.open) { root.innerHTML = wbHtml({ src: 'run:' + G.open.rid, live: false, rid: G.open.rid }); wbInit(root); return; }
  const failed = hasResult() && S.live.done.status === 'failed';
  root.innerHTML = `${failed ? resultHtml() : ''}${editorHtml()}`;
}

function tapLabel(p) {
  const c = (p && p.sniff) || {}, pre = (G.pre && G.pre.eth) || [];
  if (c.mode === 'eth_bridge') return `inline ${c.eth && c.eth.leg_a !== 'auto' ? c.eth.leg_a : 'eth0'} ⇄ ${c.eth && c.eth.leg_b !== 'auto' ? c.eth.leg_b : 'USB'}`;
  if (c.mode === 'eth_span') return `mirror port ${c.eth && c.eth.interface !== 'auto' ? c.eth.interface : ''}`.trim();
  if (c.mode === 'serial') { const ch = (c.serial && c.serial.channels || []).filter(x => x.port); return `serial ${ch.map(x => x.port.replace('/dev/', '')).join(' + ') || ''} ${ch[0] ? ch[0].baud : ''}`.trim(); }
  if (c.mode === 'wifi') return `Wi-Fi ${c.wifi && c.wifi.hop ? 'hopping' : 'channel ' + (c.wifi ? c.wifi.channel : '')}`;
  return MODES[c.mode] || 'capture';
}
function savedHtml() {
  if (!G.runs.length) return '';
  return `<section class="panel" data-testid="sniff-saved"><header><span>Saved captures</span><small>open one to filter and decode it again</small></header><div class="pbody" style="padding:0">${G.runs.map(r => {
    const secs = r.ended_at ? Math.max(0, (new Date(r.ended_at) - new Date(r.started_at)) / 1000) : 0;
    return `<div class="svrow"><div><b>${esc(r.name || (r.profile || {}).name || 'Capture')}</b><small>${when(r.started_at)} · ${num(r.headline.sent)} packets · ${dur(secs)}</small></div><button class="btn small" data-act="openrun" data-id="${r.id}" data-testid="sniff-open-${r.id}">Open</button></div>`;
  }).join('')}</div></section>`;
}

// ------------------------------------------------------------------ editor
const field = (label, inner, t = '') => `<label class="f"><span>${label} ${t ? tip(t) : ''}</span>${inner}</label>`;
const inp = (attrs, v, type = 'text', extra = '') => `<input type="${type}" ${attrs} value="${esc(v ?? '')}" ${extra}>`;
const sel = (attrs, opts, cur) => `<select ${attrs}>${opts.map(([v, l]) => `<option value="${esc(v)}" ${String(cur) === String(v) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;

function pcard(p) {
  return `<div class="pcard ${p.id === G.selId ? 'sel' : ''}" data-act="pick" data-id="${p.id}" tabindex="0" role="button"><h3>${esc(p.name)} <span class="tag ${p.builtin ? 'preset' : ''}">${p.builtin ? 'Preset' : 'Saved'}</span></h3><p>${esc(p.description || '')}</p>
    <div class="meta">${MODES[p.sniff.mode]}</div><div class="acts"><button class="btn small ghost" data-act="dup" data-id="${p.id}">Duplicate to edit</button>${p.builtin ? '' : `<button class="btn small ghost" data-act="del" data-id="${p.id}">Delete</button>`}</div></div>`;
}

function serialPorts() { return (G.pre && G.pre.serial) || []; }
function portSelect(path, cur, attrs) {
  const ports = serialPorts();
  const opts = [['', 'Choose…'], ...ports.map(x => [x.path, `${x.label} — ${x.path}`])];
  if (cur && !ports.some(x => x.path === cur)) opts.push([cur, cur]);
  return sel(attrs, opts, cur);
}

function editorHtml() {
  if (!G.p) return '<div class="card empty"><h2>No presets found</h2></div>';
  const c = G.p.sniff, m = c.mode, st = c.storage, usb = G.pre && G.pre.usb && G.pre.usb.length;
  const eth = (G.pre && G.pre.eth || []).filter(x => x.wired && !x.is_bridge);
  const ports = [['auto', 'Automatic'], ...eth.map(x => [x.name, `${x.name}${x.usb ? ' (USB)' : ' (built-in)'}${x.carrier ? '' : ' — no cable'}`])];
  let conn = '';
  if (m === 'eth_bridge') conn = `<div class="grid g3">${field('Port A', sel('data-p="eth.leg_a"', ports, c.eth.leg_a), 'The built-in Ethernet port.')}${field('Port B', sel('data-p="eth.leg_b"', ports, c.eth.leg_b), 'The USB Ethernet adapter. Automatic picks the first USB adapter.')}
      ${field('Capture on', sel('data-p="eth.capture_on"', [['bridge', 'The bridge (each frame once)'], ['both_legs', 'Both ports (shows which side a frame came from)']], c.eth.capture_on))}
      <label class="chk"><input type="checkbox" data-p="eth.link_passthrough" ${c.eth.link_passthrough ? 'checked' : ''}> Pass link loss through ${tip('If the cable on one side is unplugged, the other port is switched off too, so the far device notices (useful for failover tests).')}</label></div>`;
  else if (m === 'eth_span') conn = `<div class="grid g3">${field('Port connected to the mirror port', sel('data-p="eth.interface"', ports, c.eth.interface))}</div>`;
  else if (m === 'serial') {
    const sr = c.serial;
    conn = `<div class="grid g3">${field('Protocol to decode', sel('data-p="serial.protocol"', Object.entries(PROTOS), sr.protocol), 'DNP3 and Modbus RTU frames are checked (CRC) and decoded. Raw keeps the bytes only.')}
      ${field('Frame gap (ms, blank = automatic)', inp('data-p="serial.gap_ms" data-t="nul"', sr.gap_ms, 'number', 'step="any" min="0" placeholder="automatic"'), 'Quiet time that ends a Modbus frame. Automatic = 3.5 characters, at least 4 ms (USB adapters cannot resolve less).')}
      <label class="chk"><input type="checkbox" data-p="serial.also_user_dlt" ${sr.also_user_dlt ? 'checked' : ''}> Also save raw frames (DLT_USER0) ${tip('A second pcapng with the plain serial frames, for Wireshark users who map DLT_USER0 to a serial dissector.')}</label></div>
      ${sr.channels.map((ch, i) => `<div class="card" style="background:var(--panel2);margin:10px 0"><div class="row sp"><b>Channel ${i + 1}</b>${sr.channels.length > 1 ? `<button class="btn small ghost" data-act="rmch" data-i="${i}">Remove</button>` : ''}</div>
        <div class="grid g4w">${field("Name", inp(`data-ch="${i}" data-k="name"`, ch.name))}${field('Serial adapter', portSelect('', ch.port, `data-ch="${i}" data-k="port"`), 'The USB-serial adapter wired to this direction.')}
        ${field('Speed (baud)', sel(`data-ch="${i}" data-k="baud" data-t="num"`, [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200].map(b => [b, b]), ch.baud))}
        ${field('Parity', sel(`data-ch="${i}" data-k="parity"`, [['N', 'None'], ['E', 'Even'], ['O', 'Odd']], ch.parity))}${field('Stop bits', sel(`data-ch="${i}" data-k="stopbits" data-t="num"`, [[1, '1'], [2, '2']], ch.stopbits))}</div></div>`).join('')}
      ${sr.channels.length < 2 ? '<button class="btn small" data-act="addch">＋ Add second direction (RS-232 / 4-wire)</button>' : ''}`;
  } else if (m === 'wifi') {
    const w = c.wifi, wl = (G.pre && G.pre.wifi) || [];
    const bw = G.bw, isB = w.interface === 'builtin';
    conn = `<div class="grid g3">${field('Wi-Fi adapter', sel('data-p="wifi.interface"', [['auto', 'Automatic (first USB adapter that supports monitor mode)'], ['builtin', 'Built-in Wi-Fi of the Pi (Nexmon) — experimental'], ...wl.filter(x => !x.built_in).map(x => [x.name, `${x.name} (${x.driver})${x.monitor_capable ? '' : ' — cannot monitor'}`])], w.interface))}
      ${field('Channel', inp('data-p="wifi.channel" data-t="num"', w.channel, 'number', 'min="1"'), '2.4 GHz: 1-13. 5 GHz: 36, 40, 44, 48, 149…')}${field('Width', sel('data-p="wifi.width"', ['HT20', 'HT40+', 'HT40-', '80MHz'].map(x => [x, x]), w.width))}
      <label class="chk"><input type="checkbox" data-p="wifi.hop" ${w.hop ? 'checked' : ''}> Hop through channels ${tip('Listens on each channel in the list for a few seconds, one after another. Good for finding which networks are around.')}</label>
      ${w.hop ? field('Channels to visit', inp('data-p="wifi.channels" data-t="list"', w.channels.join(', ')), 'Comma-separated.') + field('Seconds per channel', inp('data-p="wifi.dwell_s" data-t="num"', w.dwell_s, 'number', 'min="0.2" step="any"')) : ''}</div>`;
    if (isB) conn += builtinPanel(w, bw);
  }
  const filt = m === 'serial' ? '' : `<div class="card"><h2>Capture filter</h2><p class="muted">What is saved to the file. Leave it empty to save everything. (The display filter in the workbench only changes what you see.)</p>
    <div class="row" style="flex-wrap:wrap;gap:8px">${(G.pre ? G.pre.presets : []).map(f => `<button class="btn small ${c.filter.preset === f.id ? 'primary' : ''}" data-act="preset" data-id="${esc(f.id)}" title="${esc(f.about)}">${esc(f.name)}</button>`).join('')}</div>
    <div class="grid g3" style="margin-top:10px">${field('Filter expression', inp('data-p="filter.bpf" id="bpf" placeholder="e.g. tcp port 502 and host 10.0.0.5"', c.filter.bpf), 'A standard capture filter (BPF), as used by tcpdump and Wireshark capture filters.')}
      <div class="row qf" style="align-items:flex-end">${sel('id="fk"', [['host', 'This IP'], ['mac', 'This MAC'], ['vlan', 'VLAN id'], ['port', 'Port']], 'host')}<input id="fv" placeholder="value"><button class="btn small" data-act="addf">Add</button><button class="btn small" data-act="checkf">Check</button></div></div>
    <p id="fmsg" class="muted"></p></div>`;
  // The two things people change (how long, USB drive) are in view; file rotation and the free-space guard are folded.
  const stAdv = st.ring_mb !== 100 || st.ring_files !== 10 || st.ring_minutes > 0 || st.min_free_mb !== 50;
  const storage = `<div class="card"><h2>How long, and where to keep it</h2><div class="grid g3">
      ${field('Stop after (minutes, 0 = until I stop it)', inp('data-p="duration_min" data-t="num"', (c.duration_s || 0) / 60, 'number', 'min="0" step="any"'))}
      <label class="chk"><input type="checkbox" data-p="storage.usb" ${st.usb ? 'checked' : ''} ${usb ? '' : 'disabled'}> Save straight to the USB drive ${tip('Needs a USB flash drive plugged in.')}</label></div>
      ${usb ? `<p class="muted">USB drive: ${esc(G.pre.usb[0].path)}</p>` : '<p class="muted">No USB drive plugged in: captures are kept on this Pi.</p>'}
      <details id="sStoreAdv" ${stAdv ? 'open' : ''}><summary>Advanced (file size, how many files to keep, free space)</summary><div class="grid g3" style="margin-top:8px">
      ${field('Start a new file every (MB)', inp('data-p="storage.ring_mb" data-t="num"', st.ring_mb, 'number', 'min="0"'), 'Files rotate by size. 0 = one growing file.')}${field('Keep the newest (files)', inp('data-p="storage.ring_files" data-t="num"', st.ring_files, 'number', 'min="0"'), 'Older files are deleted (ring buffer). 0 = keep all.')}
      ${field('…or a new file every (minutes)', inp('data-p="storage.ring_minutes" data-t="num"', st.ring_minutes, 'number', 'min="0" step="any"'))}
      ${field('Always keep free space (MB)', inp('data-p="storage.min_free_mb" data-t="num"', st.min_free_mb, 'number', 'min="1"'), 'The capture stops before the storage gets fuller than this. At gigabit speed keep at least 100 MB.')}
      </div></details></div>`;
  return `<div class="card"><div class="row sp"><div><h2>Setups</h2><p class="muted">Start from a preset and change anything below.</p></div></div><div class="grid g3">${G.profiles.map(pcard).join('')}</div></div>
    <div class="card"><h2>Tap</h2><div class="grid g3" style="margin:6px 0">${field('Where the Pi listens', sel('data-p="mode" data-testid="sniff-tap"', Object.entries(MODES), m), 'Inline between two devices, on a switch mirror port, on a serial line, or on Wi-Fi.')}</div><div class="check ok" style="margin-top:6px"><div class="ic">i</div><div><b>How to connect it</b><small class="muted">${HOW[m]}</small></div></div>${conn}</div>${filt}${storage}
    <div class="card"><div class="row sp"><div><h2>Check, then start</h2></div><button class="btn small" data-act="preflight">✓ Run pre-flight check</button></div>
      <div id="checks">${(G.checks || []).map(checkRow).join('')}</div>
      <div class="row sp" style="margin-top:14px"><button class="btn" data-act="save">💾 Save as profile…</button><button class="btn primary tb-src" data-act="start" data-testid="sniff-start">▶ Start capture</button></div></div>${savedHtml()}`;
}
function builtinPanel(w, bw) {
  const st = bw ? (bw.available ? `<span style="color:var(--pass)">✓ Nexmon is installed for kernel ${esc(bw.kernel || '')}.</span>` : `<span style="color:var(--fail)">✕ ${esc(bw.reason || 'Not available.')}</span>`) : 'Status unknown.';
  const mon = bw && bw.state === 'monitor';
  return `<div class="card" style="background:var(--panel2);margin:12px 0"><h3 style="margin:0 0 6px">Built-in Wi-Fi sniffing (Nexmon) <span class="tag">Experimental</span></h3>
    <div class="check warn"><div class="ic">!</div><div><b>Wi-Fi (and internet over Wi-Fi) goes off while this runs</b><small class="muted">${esc((bw && bw.warning) || 'Use an Ethernet cable for this page and the internet, or a USB Wi-Fi adapter for sniffing.')} The Pi restores normal Wi-Fi by itself when the capture stops, if the app stops, or after a reboot.</small></div></div>
    <p style="margin:8px 0">${st}${mon ? ' <b>Currently in monitor mode.</b>' : ''}</p>
    <label class="chk"><input type="checkbox" data-p="wifi.builtin_ack" ${w.builtin_ack ? 'checked' : ''}> I understand normal Wi-Fi goes off while sniffing with the built-in chip</label>
    <div class="row" style="gap:8px;margin-top:8px"><button class="btn small" data-act="bw-restore">↺ Restore normal Wi-Fi</button><button class="btn small ghost" data-act="bw-inject" ${mon ? '' : 'disabled title="Only while sniffing with the built-in chip"'}>Test packet injection (experimental)</button></div>
    <p class="muted" id="bwmsg"></p></div>`;
}
const checkRow = c => `<div class="check ${{ ok: 'ok', warn: 'warn', bad: 'bad' }[c.s]}"><div class="ic">${{ ok: '✓', warn: '!', bad: '✕' }[c.s]}</div><div><b>${esc(c.title)}</b><small class="muted">${esc(c.text)}</small></div></div>`;

// ------------------------------------------------------------------ events
function setp(o, path, v) { const k = path.split('.'); let t = o; k.slice(0, -1).forEach(x => t = (t[x] ??= {})); t[k.at(-1)] = v; }
function change(ev, root) {
  const el = ev.target, c = G.p && G.p.sniff; if (!c) return;
  if (el.dataset.ch !== undefined) { const ch = c.serial.channels[+el.dataset.ch]; let v = el.value; if (el.dataset.t === 'num') v = Number(v); ch[el.dataset.k] = v; return; }
  if (!el.dataset.p) return;
  let v = el.type === 'checkbox' ? el.checked : el.value;
  if (el.dataset.t === 'num') v = Number(v); if (el.dataset.t === 'nul') v = v === '' ? null : Number(v);
  if (el.dataset.t === 'list') v = v.split(/[,\s]+/).filter(Boolean).map(Number).filter(x => x > 0);
  if (el.dataset.p === 'duration_min') { c.duration_s = Math.round(v * 60); return; }
  setp(c, el.dataset.p, v);
  if (['serial.protocol', 'wifi.hop', 'mode'].includes(el.dataset.p)) draw(root);
}
async function click(ev, root) {
  const a = ev.target.closest('[data-act]'); if (!a) return;
  const act = a.dataset.act, id = +a.dataset.id, i = a.dataset.i !== undefined ? +a.dataset.i : null;
  const reload = async () => { G.profiles = (await api('/profiles')).filter(p => p.kind === 'sniffer'); draw(root); };
  const c = G.p && G.p.sniff;
  try {
    if (await wbClick(a, ev, root)) return;
    if (act === 'pick') { G.selId = id; G.p = clone(G.profiles.find(x => x.id === id)); G.checks = null; draw(root); }
    else if (act === 'dup') { const p = await api(`/profiles/${id}/duplicate`, { method: 'POST' }); G.selId = p.id; G.p = clone(p); toast(`Created “${p.name}”. It's yours to edit.`); await reload(); }
    else if (act === 'del') { if (await confirmBox('Delete this profile?', 'Past captures are kept.', 'Delete', true)) { await api(`/profiles/${id}`, { method: 'DELETE' }); G.selId = null; G.p = null; await renderSniffer(root, navigate); } }
    else if (act === 'addch') { c.serial.channels.push({ name: 'Outstation to master', port: '', baud: c.serial.channels[0].baud, bytesize: 8, parity: 'N', stopbits: 1 }); draw(root); }
    else if (act === 'rmch') { c.serial.channels.splice(i, 1); draw(root); }
    else if (act === 'preset') { const f = G.pre.presets.find(x => x.id === a.dataset.id); c.filter = { preset: f.id, bpf: f.expr }; draw(root); }
    else if (act === 'addf') {
      const r = await api('/sniff/filter/check', { method: 'POST', body: { kind: $('#fk').value, value: $('#fv').value } });
      const cur = $('#bpf').value.trim(); c.filter.bpf = cur ? `(${cur}) and ${r.expr}` : r.expr; c.filter.preset = ''; draw(root);
    }
    else if (act === 'checkf') {
      const r = await api('/sniff/filter/check', { method: 'POST', body: { expr: $('#bpf').value } }); c.filter.bpf = $('#bpf').value;
      $('#fmsg').innerHTML = r.ok ? '<span style="color:var(--pass)">✓ The filter is valid.</span>' : `<span style="color:var(--fail)">${esc(r.error)}</span>`;
    }
    else if (act === 'bw-restore') {
      a.disabled = true; $('#bwmsg').textContent = 'Restoring normal Wi-Fi… (up to a minute)';
      try { const r = await api('/wifi/builtin/restore', { method: 'POST' }); $('#bwmsg').innerHTML = `<span style="color:var(--pass)">✓ ${esc(r.message || 'Normal Wi-Fi is back.')}</span>`; }
      catch (e) { $('#bwmsg').innerHTML = `<span style="color:var(--fail)">${esc(e.message)}</span>`; }
      try { G.bw = await api('/wifi/builtin/status'); } catch { /* keep old */ } a.disabled = false;
    }
    else if (act === 'bw-inject') {
      $('#bwmsg').textContent = 'Testing injection…';
      try { const r = await api('/wifi/builtin/inject-test', { method: 'POST' }); $('#bwmsg').innerHTML = `<span style="color:var(--pass)">✓ ${esc(r.message || 'Injection works.')}</span>`; }
      catch (e) { $('#bwmsg').innerHTML = `<span style="color:var(--fail)">${esc(e.message)}</span>`; }
    }
    else if (act === 'preflight') { const r = await api('/sniff/preflight', { method: 'POST', body: effProfile() }); G.pre = r; G.checks = r.checks; draw(root); }
    else if (act === 'save') {
      const name = await promptBox('Save as profile', 'Name for this capture setup', G.p.builtin ? `${G.p.name} (mine)` : G.p.name); if (!name) return;
      const p = effProfile(); p.name = name; p.builtin = false;
      const cur = G.profiles.find(x => x.id === G.selId && !x.builtin && x.name === name);
      const r = cur ? await api(`/profiles/${cur.id}`, { method: 'PUT', body: p }) : await api('/profiles', { method: 'POST', body: p });
      G.selId = r.id; G.p = clone(r); toast('Profile saved.'); await reload();
    }
    else if (act === 'start') {
      const p = effProfile(); a.disabled = true;
      try { const r = await api('/runs', { method: 'POST', body: { profile: p } }); resetLive(p, r.run_id); S.status = await api('/status'); draw(root); }
      catch (e) { toast(e.message, true, 9000); a.disabled = false; }
    }
    else if (act === 'stop') { if (await confirmBox('Stop the capture?', 'The files so far are kept.', 'Stop', true)) await api(`/runs/${S.live.runId}/stop`, { method: 'POST' }); }
    else if (act === 'again') { S.live.done = null; G.open = null; await renderSniffer(root, navigate); }
    else if (act === 'results') { S.openRun = G.open ? G.open.rid : S.live.runId; navigate('results'); }
    else if (act === 'usb') { const r = await api(`/runs/${G.open ? G.open.rid : S.live.runId}/capture/usb`, { method: 'POST' }); toast(`Copied ${r.files} file(s) to ${r.path}`); }
  } catch (e) { toast(e.message, true, 8000); }
}

// ------------------------------------------------------------------ workbench
const clock = ns => { const d = new Date(ns / 1e6); return d.toLocaleTimeString([], { hour12: false }) + '.' + String(Math.floor(ns / 1e6) % 1000).padStart(3, '0'); };
const f1 = x => x == null ? '–' : Number(x).toFixed(1);
const size = n => n == null ? '–' : n > 1e9 ? (n / 1e9).toFixed(2) + ' GB' : n > 1e6 ? (n / 1e6).toFixed(1) + ' MB' : n > 1e3 ? Math.round(n / 1e3) + ' kB' : n + ' B';
const stopWb = () => { if (W && W.timer) clearInterval(W.timer); W = null; };
function fit() {                                                     // the packet list fills the screen under the filter bar; on a phone the sheet below takes its share
  const l = $('#pkList'); if (!l) return; const phone = matchMedia('(max-width:820px)').matches;
  const top = l.getBoundingClientRect().top + window.scrollY, sheet = $('#wbSide'), open = sheet && sheet.dataset.open === '1';
  const bars = ($('#tabs') ? $('#tabs').offsetHeight : 0) + ($('#transport') ? $('#transport').offsetHeight : 0);
  const reserve = phone ? bars + (open ? Math.round(innerHeight * 0.44) : 46) + 6 : 16;
  l.style.height = Math.max(phone ? 120 : 360, innerHeight - top - reserve) + 'px';
}
window.addEventListener('resize', () => W && fit());
const effFilter = () => W.errsOnly ? (W.filter.trim() ? `(${W.filter}) and errors` : 'errors') : W.filter;
const TABS = [['decode', 'Decode'], ['bytes', 'Bytes'], ['flows', 'Flows'], ['protocols', 'Protocols'], ['timing', 'Timing']];

function wbHtml(o) {
  return `<div class="wb" data-testid="sniff-wb" data-src="${esc(o.src)}">
  <div class="wbbar"><span class="tapchip" id="wbTap" data-testid="sniff-tapchip">Tap: ${o.live ? esc(tapLabel(S.live.profile)) : '…'}</span><span class="capchip" id="wbCap" title="The capture filter: what is saved to the file">capture: …</span>
    <input id="dfilter" class="dfilter" data-testid="sniff-dfilter" placeholder="display filter, for example host 192.0.2.90 and dnp3" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Display filter">
    <div class="wbcount" id="wbCount" data-testid="sniff-counts"></div>
    <div class="wbacts"><button class="btn small" data-act="errs" id="wbErrs" data-testid="sniff-errors-only" aria-pressed="false">Errors only</button>
    <button class="btn small" data-act="savemenu" data-testid="sniff-save">Save pcapng ▾</button></div></div>
  <div class="dfmsg" id="dfmsg" data-testid="sniff-dfmsg" hidden></div>
  ${o.live ? '' : `<div class="wbdone" id="wbDone" data-testid="sniff-done"></div>`}
  <div class="wbgrid">
    <section class="panel pk"><header><span>Packets</span><button class="lnk" data-act="follow" id="wbFollow" data-testid="sniff-follow">following · tap a row to hold</button></header>
      <div class="pkh" aria-hidden="true"><span class="c-n">No.</span><span class="c-t">Time</span><span class="c-d" title="Δ ms: time since the previous packet of the same conversation. On a response it is the time since its request.">Δ ms</span><span class="c-s">Source</span><span class="c-x">Destination</span><span class="c-p">Proto</span><span class="c-l">Len</span><span class="c-i">Info</span><span class="c-mh">Time · Δ ms</span></div>
      <div class="pklist" id="pkList" data-testid="sniff-list" role="grid" aria-label="Packets"><div class="pkempty" id="pkEmpty">Waiting for packets…</div></div></section>
    <div class="wbside" id="wbSide" data-tab="decode" data-open="0">
      <section class="panel dec" data-p="decode"><header><span id="decT">Decode</span></header><div class="pbody" id="decBody" data-testid="sniff-decode"><p class="muted">Tap a packet to decode it.</p></div></section>
      <section class="panel byt" data-p="bytes"><header><span>Bytes</span><small id="bytNote"></small></header><div class="pbody" id="bytBody" data-testid="sniff-bytes"></div></section>
      <section class="panel flw" data-p="flows"><header><span class="ptabs" id="ptabs"></span></header><div class="pbody" id="flwBody" data-testid="sniff-flows"></div></section>
      <nav class="wbtabs" id="wbTabs" aria-label="Packet details">${TABS.map(([k, l]) => `<button type="button" data-act="wbtab" data-tab="${k}" ${k === 'decode' ? 'aria-selected="true"' : ''}>${l}</button>`).join('')}</nav>
    </div>
  </div></div>`;
}

async function wbInit(root) {
  const o = $('.wb', root).dataset.src, live = o === 'live';
  const w = W = { root, src: o, live, rid: live ? (S.live.runId || (S.status.run || {}).run_id) : +o.slice(4), rows: [], total: 0, shown: 0, errors: 0, filter: '', bad: false, floor: 0, after: 0, sel: null, follow: true, tab: 'decode', ptab: 'flows',
    gen: 0, loading: false, more: false, detail: null, hl: null, errsOnly: false, meta: null, flows: [], summ: null, tick: 0, retry: 0, serial: false };
  for (let i = 0; i < 10 && W === w; i++) {                       // right after Stop the result is stored a moment later: wait for it
    try { w.meta = await api(`/runs/${w.rid}`); setCap(w.meta.profile); if (!live) paintDone(); if (live || (w.meta.summary && w.meta.status !== 'running')) break; } catch { /* the chips stay as they are */ }
    await new Promise(r => setTimeout(r, 600));
  }
  if (W !== w) return;
  paintPtabs(); fit(); setTimeout(fit, 300); pull(); w.timer = setInterval(tick, live ? 1000 : 2500);
  const list = $('#pkList'); list.addEventListener('scroll', () => { if (!W) return; const atEnd = list.scrollTop + list.clientHeight >= list.scrollHeight - 30; if (!atEnd && W.follow && W.live && !W.programmatic) { W.follow = false; paintFollow(); } if (W.more && atEnd) loadMore(); });
}
function setCap(p) {
  const c = p && p.sniff; $('#wbTap').textContent = 'Tap: ' + tapLabel(p);
  const e = c && c.filter && c.filter.bpf ? c.filter.bpf : ''; const el = $('#wbCap'); el.textContent = 'capture: ' + (c && c.mode === 'serial' ? 'every frame' : e || 'everything'); el.classList.toggle('on', !!e);
}
function paintDone() {
  const r = W.meta, el = $('#wbDone'); if (!el || !r) return; const s = r.summary || {}, a = s.analysis || {}, e = s.engine || {};
  const lost = e.kernel_dropped > 0 ? ` · <b class="red">${num(e.kernel_dropped)} dropped by the system</b>` : '';
  el.innerHTML = `<span><b>${r.status === 'failed' ? 'Could not capture' : 'Capture saved'}</b> · ${esc((r.profile || {}).name || '')} · ${when(r.started_at)} · ${dur(s.elapsed_s || 0)} · ${num(a.packets || 0)} packets${lost}</span>
    <span class="wbdacts"><button class="btn small" data-act="results" data-testid="sniff-summary">Summary and files →</button><button class="btn small" data-act="usb">Copy to USB</button>${G.open && G.open.fromDone ? '<button class="btn small" data-act="again" data-testid="sniff-again">Capture again</button>' : '<button class="btn small" data-act="closewb" data-testid="sniff-close">Close</button>'}</span>`;
}
function tick() {
  if (!W) return;
  if (!document.body.contains(W.root.querySelector('.wb'))) return stopWb();
  if (W.live) { pull(); if (++W.tick % 2 === 0) pullSide(); }
}
async function pull(reset = false) {
  if (!W || W.loading || W.bad) return; const w = W, gen = w.gen; w.loading = true;
  try {
    const q = new URLSearchParams({ src: w.src, filter: effFilter(), after: w.after, limit: 400 });
    const v = await api('/sniff/view?' + q);
    if (W !== w || w.gen !== gen) return;
    w.total = v.total; w.shown = v.shown; w.errors = v.errors; w.seen = v.seen; w.sampling = v.sampling; w.serial = v.mode === 'serial'; w.retry = 0;
    if (v.rows.length) { w.after = v.rows[v.rows.length - 1].n; w.rows.push(...v.rows); appendRows(v.rows); }
    w.more = !w.live && v.rows.length >= 400;
    $('#pkEmpty') && (($('#pkEmpty').textContent = w.shown || w.total === 0 ? (w.live ? 'Waiting for packets…' : 'This capture has no packets.') : 'No packet matches the display filter.'), ($('#pkEmpty').hidden = !!w.rows.length));
    wbCounts(); if (w.tick === 0 || !w.live) pullSide();
  } catch (e) {
    if (W !== w || w.gen !== gen) return;
    if (/removed/i.test(e.message) || w.retry >= 8) { $('#pkEmpty').textContent = e.message; $('#pkEmpty').hidden = false; }
    else { w.retry++; setTimeout(() => W === w && pull(), 1000); }
  } finally { w.loading = false; }
}
function loadMore() { if (W && W.more && !W.loading) pull(); }

function rowHtml(r) {
  const cls = ['pr', r.errors ? 'err' : '', !r.errors && r.retrans ? 'retr' : '', r.resp_ms != null ? 'resp' : '', W.sel === r.n ? 'sel' : ''].filter(Boolean).join(' ');
  const serial = r.channel_name !== undefined;
  const sTxt = serial ? r.channel_name : r.src, dTxt = serial ? '' : r.dst;
  const ends = (r.dir === '←' ? r.src : r.dst) || '', shortEnd = serial ? r.channel_name : ends.replace(/^(\d+\.\d+\.\d+)\.(\d+)/, '.$2');
  return `<div class="${cls}" data-act="row" data-n="${r.n}" role="row" tabindex="0"><span class="c-n">${r.n}</span><span class="c-t">${clock(r.t_ns).replace(/^(\d+:)/, '<i class="th">$1</i>')}</span><span class="c-d ${r.resp_ms != null ? 'rt' : ''}">${f1(r.delta_ms)}</span>
    <span class="c-s">${esc(sTxt || '')}</span><span class="c-x">${esc(dTxt || '')}</span><span class="c-p"><b>${esc(r.proto)}</b></span><span class="c-l">${r.len}</span><span class="c-i">${esc(r.info)}${r.crc_ok === false || (serial && r.crc_bad) ? ' · CRC FAILED' : ''}</span>
    <span class="c-e">${esc(r.dir || '')} ${esc(shortEnd)}</span><span class="c-m">${r.len} B · no. ${r.n}</span></div>`;
}
function appendRows(rows) {
  const list = $('#pkList'); if (!list) return; const empty = $('#pkEmpty'); if (empty) empty.hidden = true;
  list.insertAdjacentHTML('beforeend', rows.map(rowHtml).join(''));
  if (W.live) { const over = W.rows.length - 3000; if (over > 0) { W.rows.splice(0, over); for (let i = 0; i < over; i++) list.querySelector('.pr').remove(); } }
  if (W.follow && W.live) { W.programmatic = true; list.scrollTop = list.scrollHeight; setTimeout(() => { if (W) W.programmatic = false; }, 60); }
}
function resetList() {
  W.gen++; W.rows = []; W.after = W.floor; W.loading = false; W.more = false;
  const list = $('#pkList'); if (list) list.innerHTML = '<div class="pkempty" id="pkEmpty">Waiting for packets…</div>';
  pull(); pullSide();
}
function paintFollow() { const b = $('#wbFollow'); if (!b) return; b.textContent = !W.live ? (W.more ? 'scroll for more' : 'tap a row to decode') : W.follow ? 'following · tap a row to hold' : 'held · tap to follow ↓'; b.classList.toggle('held', W.live && !W.follow); }
function wbCounts() {
  const el = $('#wbCount'); if (!el || !W) return; const e = S.sniff.last || {};
  const captured = W.live ? (e.packets != null ? e.packets : W.total) : W.total;
  const dropped = W.live ? '–' : ((W.meta && W.meta.summary && W.meta.summary.engine || {}).kernel_dropped ?? 0);
  const extra = W.live ? `${e.file_bytes != null ? 'file ' + size(e.file_bytes) : ''}${e.pps != null ? ` · ${num(e.pps)} pkt/s` : ''}` : ((W.meta && W.meta.summary && W.meta.summary.files || []).length ? `${W.meta.summary.files.length} file(s) · ${size(W.meta.summary.files.reduce((a, f) => a + f.bytes, 0))}` : '');
  const note = W.sampling ? ` · <span title="At a high rate the list shows a sample; the saved file has every packet.">list shows a sample (${num(W.seen)} seen)</span>` : '';
  el.innerHTML = `<b>${num(captured)}</b> captured <b>${num(W.shown)}</b> shown <b class="${W.errors ? 'red' : ''}">${num(W.errors)}</b> errors <span class="wbdrop"><b>${dropped === '–' ? '–' : num(dropped)}</b> dropped</span> <span class="muted wbx">${esc(extra)}</span><span class="wbx">${note}</span>`;
  paintFollow();
}

// ---- side panes
async function select(n) {
  W.sel = n; W.follow = false; paintFollow(); $('#wbSide').dataset.open = '1'; fit();
  $$('#pkList .pr.sel').forEach(x => x.classList.remove('sel')); const row = $(`#pkList .pr[data-n="${n}"]`); if (row) row.classList.add('sel');
  if (matchMedia('(max-width:820px)').matches && W.tab !== 'bytes') setTab('decode');
  try { W.detail = await api(`/sniff/packet/${n}?src=${encodeURIComponent(W.src)}`); W.hl = null; paintDetail(); }
  catch (e) { W.detail = null; $('#decBody').innerHTML = `<p class="muted">${esc(e.message)}</p>`; $('#bytBody').innerHTML = ''; }
}
const laySum = l => {
  const f = l.fields || {}, g = k => f[k];
  if (l.name === 'Frame') return `${esc(g('Number'))} · ${esc(g('Length'))}${g('Response time') || g('Timing') ? ' · <b>' + esc(g('Response time') || g('Timing')) + '</b>' : ''}`;
  if (l.name === 'Ethernet') return `${esc(g('Source'))} → ${esc(g('Destination'))}`;
  if (l.name === 'IPv4' || l.name === 'IPv6') return `${esc(g('Source'))} → ${esc(g('Destination'))}${g('TTL') != null ? ' · TTL ' + g('TTL') : ''}`;
  if (l.name === 'TCP') return `${g('Source port')} → ${g('Destination port')} · ${esc(g('Flags'))} · seq ${g('Sequence')}`;
  if (l.name === 'UDP') return `${g('Source port')} → ${g('Destination port')} · ${g('Payload bytes')} bytes`;
  return esc(g('Summary') || '');
};
function paintDetail() {
  const d = W.detail; if (!d) return; $('#decT').textContent = `Decode · packet ${d.n}`;
  const ls = d.layers || [], last = ls.length - 1;
  const val = v => Array.isArray(v) ? v.map(x => esc(String(x))).join('<br>') : esc(String(v));
  $('#decBody').innerHTML = `<div class="tree">${ls.map((l, i) => {
    const rows = Object.entries(l.fields || {}).filter(([k]) => k !== 'Summary');
    return `<details class="lay" ${i === last || ls.length === 1 ? 'open' : ''} data-i="${i}"><summary data-act="layer" data-i="${i}"><b>${esc(l.name)}</b>${laySum(l) ? ' · <span>' + laySum(l) + '</span>' : ''}</summary>
      <div class="lf">${rows.map(([k, v]) => `<div class="lr"><span class="lk">${esc(k)}</span><span class="lv">${val(v)}</span></div>`).join('')}</div></details>`;
  }).join('')}</div>`;
  if (ls[last] && ls[last].range) W.hl = ls[last].range; paintBytes();
}
function paintBytes() {
  const d = W.detail; if (!d) return; const hex = (d.hex || '').match(/.{1,2}/g) || [], hl = W.hl;
  let out = '';
  for (let i = 0; i < hex.length; i += 16) {
    const chunk = hex.slice(i, i + 16);
    out += `<div class="hx"><span class="ho">${i.toString(16).padStart(4, '0')}</span><span class="hb">${chunk.map((h, j) => `<span class="${hl && i + j >= hl[0] && i + j < hl[1] ? 'h' : ''}">${h}</span>`).join(' ')}</span>
      <span class="ha">${chunk.map(h => { const c = parseInt(h, 16); return c >= 32 && c < 127 ? esc(String.fromCharCode(c)) : '·'; }).join('')}</span></div>`;
  }
  $('#bytBody').innerHTML = out; $('#bytNote').textContent = d.len > hex.length ? `first ${hex.length} of ${d.len} bytes` : `${d.len} bytes · tap a layer to highlight it`;
}
function setTab(t) {
  W.tab = t; const side = $('#wbSide'); side.dataset.tab = t;
  $$('#wbTabs button').forEach(b => b.setAttribute('aria-selected', b.dataset.tab === t ? 'true' : 'false'));
  if (['flows', 'protocols', 'timing'].includes(t)) { W.ptab = t; paintPtabs(); pullSide(); }
}
function paintPtabs() {
  const p = $('#ptabs'); if (!p) return;
  p.innerHTML = [['flows', 'Flows'], ['protocols', 'Protocols'], ['timing', 'Timing']].map(([k, l]) => `<button type="button" class="ptab" data-act="ptab" data-tab="${k}" aria-selected="${W.ptab === k}">${l}</button>`).join('');
}
async function pullSide() {
  if (!W) return; const w = W, t = w.ptab;
  try {
    if (t === 'flows') { w.flows = (await api(`/sniff/flows?src=${encodeURIComponent(w.src)}`)).flows; }
    else w.summ = await api(`/sniff/summary?${new URLSearchParams({ src: w.src, filter: effFilter() })}`);
    if (W === w && w.ptab === t) paintSide();
  } catch { /* the panel keeps what it had */ }
}
const sparkline = b => { if (!b || !b.length) return ''; const m = Math.max(1, ...b); return `<div class="spark" aria-label="Packets over the captured time">${b.map(v => `<i style="height:${Math.max(2, Math.round(100 * v / m))}%"></i>`).join('')}</div>`; };
function paintSide() {
  const el = $('#flwBody'); if (!el) return;
  if (W.ptab === 'flows') {
    const f = W.flows, anyR = f.some(x => x.retrans);
    el.innerHTML = !f.length ? '<p class="muted">No conversations yet.</p>' : `<div class="fl"><table class="ptable flt"><thead><tr><th>Flow</th><th>Proto</th><th class="r">Pkts</th><th class="r">Bytes</th><th class="r" title="Average time from a request to its response">Resp ms</th><th class="r">${anyR ? 'Err · retr' : 'Errors'}</th></tr></thead><tbody>${f.slice(0, 60).map(x =>
      `<tr class="click" data-act="flowf" data-a="${esc(x.a)}" data-b="${esc(x.b)}" data-proto="${esc(x.proto)}" tabindex="0"><td class="mono">${esc(x.label)}</td><td><b>${esc(x.proto)}</b></td><td class="r">${num(x.packets)}</td><td class="r">${size(x.bytes)}</td>
      <td class="r" title="${x.responses ? `${x.responses} responses · max ${f1(x.resp_max_ms)} ms · p95 ${f1(x.resp_p95_ms)} ms` : 'no request/response pairs'}">${x.resp_avg_ms != null ? f1(x.resp_avg_ms) : '–'}</td><td class="r ${x.errors ? 'red' : ''}">${x.errors}${anyR ? ` · <span class="${x.retrans ? 'amb' : ''}">${x.retrans}</span>` : ''}</td></tr>`).join('')}</tbody></table></div>`;
  } else if (W.ptab === 'protocols') {
    const p = (W.summ && W.summ.protocols) || [], tot = p.reduce((a, x) => a + x.packets, 0) || 1;
    el.innerHTML = !p.length ? '<p class="muted">Nothing to count yet.</p>' : `<table class="ptable"><thead><tr><th>Protocol</th><th class="r">Pkts</th><th class="r">Bytes</th><th>Share</th></tr></thead><tbody>${p.map(x =>
      `<tr class="click" data-act="protof" data-proto="${esc(x.proto)}" tabindex="0"><td><b>${esc(x.proto)}</b></td><td class="r">${num(x.packets)}</td><td class="r">${size(x.bytes)}</td><td><span class="share"><i style="width:${Math.round(100 * x.packets / tot)}%"></i></span> ${(100 * x.packets / tot).toFixed(0)}%</td></tr>`).join('')}</tbody></table>`;
  } else {
    const t = (W.summ && W.summ.timing) || {};
    el.innerHTML = !t.span_s && !t.responses ? '<p class="muted">Timing appears once packets have arrived.</p>' : `<div class="kv"><span>Request to response</span><b>${t.responses ? `${t.responses} pairs` : 'none seen'}</b></div>
      <div class="tmg">${[['min', t.resp_min_ms], ['average', t.resp_avg_ms], ['95th %', t.resp_p95_ms], ['max', t.resp_max_ms]].map(([l, v]) => `<div><small>${l}</small><b>${v != null ? f1(v) : '–'}<em> ms</em></b></div>`).join('')}</div>
      <div class="kv"><span>Time covered</span><b>${t.span_s != null ? t.span_s.toFixed(1) + ' s' : '–'}</b></div>${sparkline(t.buckets)}
      ${(t.longest_gaps || []).length ? `<small class="muted">Longest quiet times (tap to jump)</small>${t.longest_gaps.map(g => `<div class="kv gap click" data-act="gap" data-n="${g.n}" tabindex="0"><span>packet ${g.n}</span><b>${f1(g.delta_ms)} ms</b></div>`).join('')}` : ''}`;
  }
}

// ---- events
function wbInput(ev) {
  const el = ev.target; if (!W || el.id !== 'dfilter') return;
  clearTimeout(W.dt); W.dt = setTimeout(async () => {
    const v = el.value; W.filter = v; const msg = $('#dfmsg');
    try { const r = await api('/sniff/display/check', { method: 'POST', body: { expr: v } }); W.bad = !r.ok; el.classList.toggle('bad', !r.ok); el.classList.toggle('on', !!v.trim() && r.ok); msg.hidden = r.ok; msg.textContent = r.ok ? '' : r.error; }
    catch { W.bad = false; }
    if (!W.bad) resetList();
  }, 350);
}
async function download(src, body) {
  const r = await fetch('/api/sniff/save-shown', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) { let m = 'Could not save'; try { m = (await r.json()).detail || m; } catch { /* keep */ } throw new Error(m); }
  const blob = await r.blob(), a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `capture-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.pcapng`; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 3000);
  return +r.headers.get('X-Packets') || 0;
}
async function wbClick(a, ev, root) {
  const act = a.dataset.act;
  if (act === 'openrun') { G.open = { rid: +a.dataset.id, fromDone: false }; draw(root); return true; }
  if (act === 'closewb') { G.open = null; await renderSniffer(root, navigate); return true; }
  if (!W) return false;
  if (act === 'row') { select(+a.dataset.n); return true; }
  if (act === 'follow') { if (W.live) { W.follow = true; const l = $('#pkList'); W.programmatic = true; l.scrollTop = l.scrollHeight; setTimeout(() => { if (W) W.programmatic = false; }, 60); } else loadMore(); paintFollow(); return true; }
  if (act === 'errs') { W.errsOnly = !W.errsOnly; a.setAttribute('aria-pressed', W.errsOnly); a.classList.toggle('primary', W.errsOnly); resetList(); return true; }
  if (act === 'wbtab') { const sd = $('#wbSide'); if (sd.dataset.open === '1' && sd.dataset.tab === a.dataset.tab) sd.dataset.open = '0'; else { sd.dataset.open = '1'; setTab(a.dataset.tab); } fit(); return true; }
  if (act === 'ptab') { W.ptab = a.dataset.tab; paintPtabs(); pullSide(); return true; }
  if (act === 'layer') { const l = (W.detail && W.detail.layers || [])[+a.dataset.i]; if (l && l.range) { W.hl = l.range; paintBytes(); } return true; }
  if (act === 'gap') { const n = +a.dataset.n; const row = $(`#pkList .pr[data-n="${n}"]`); if (row) row.scrollIntoView({ block: 'center' }); select(n); return true; }
  if (act === 'protof' || act === 'flowf') {
    let f = '';
    if (act === 'protof') f = a.dataset.proto.toLowerCase().replace(/\s+/g, '').replace('modbus/tcp', 'modbus').replace('modbusrtu', 'modbus');
    else { const ip = s => s.replace(/:\d+$/, ''), port = s => (s.match(/:(\d+)$/) || [])[1]; const A = a.dataset.a, B = a.dataset.b; f = B ? `host ${ip(A)} and host ${ip(B)}` : a.dataset.proto.toLowerCase(); const pp = [port(A), port(B)].filter(Boolean).map(Number).sort((x, y) => x - y)[0]; if (pp && pp < 32768) f += ` and port ${pp}`; }
    const el = $('#dfilter'); el.value = f; el.dispatchEvent(new Event('input', { bubbles: true })); return true;
  }
  if (act === 'savemenu') {
    const eff = effFilter().trim(), canKeep = !W.live;
    const c = await sheetBoxLazy('Save pcapng', `<p>Opens in Wireshark. The capture filter decided what was recorded; this saves from that.</p>
      <p><b>All packets</b> · the whole capture${W.live ? ' so far' : ''}.<br><b>Only what is shown</b> · ${eff ? `packets that match <code>${esc(eff)}</code>` : 'no display filter is set, so this is the same as all packets'}.</p>${canKeep ? '<p class="muted">“Keep with the result” adds the shown packets as a file of this capture, so it downloads from Results.</p>' : ''}`,
      [{ id: 'all', label: 'All packets', cls: '' }, { id: 'shown', label: 'Only what is shown', cls: 'primary' }, ...(canKeep ? [{ id: 'keep', label: 'Keep shown with the result' }] : [])], 'sniff-savesheet');
    if (!c) return true;
    try {
      const body = { src: W.src, filter: c === 'all' ? '' : effFilter(), keep: c === 'keep' };
      if (c === 'keep') { const r = await api('/sniff/save-shown', { method: 'POST', body }); toast(`Kept ${num(r.packets)} packets with this capture as ${r.name}.`); }
      else { const n = await download(W.src, body); toast(`Saved ${num(n)} packets.`); }
    } catch (e) { toast(e.message, true, 7000); }
    return true;
  }
  return false;
}
const sheetBoxLazy = async (...a) => (await import('./ui.js')).sheetBox(...a);
document.addEventListener('tt-sniff-clear', () => { if (!W || !W.live) return; W.floor = W.total; resetList(); toast('List cleared. The capture keeps recording everything.'); });

function effProfile() { const p = clone(G.p); delete p.id; return p; }

// ------------------------------------------------------------------ a capture that could not start
function resultHtml() {
  const d = S.live.done;
  return `<div class="banner fail"><div class="big">✕ COULD NOT CAPTURE</div><div>${esc(d.error || 'The capture failed.')}</div></div><div class="row" style="margin-bottom:16px"><button class="btn" data-act="again">Change settings</button></div>`;
}
