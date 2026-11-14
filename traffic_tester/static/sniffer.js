// Sniffer tab: presets -> connection (per mode) -> filter -> storage -> pre-flight -> live view -> result.
import { $, $$, esc, api, toast, confirmBox, promptBox, tip, ms, num, dur, download, bindOnce, fitPlot } from './ui.js';
import { S, onLive, resetLive } from './state.js';

let G = { profiles: [], selId: null, p: null, pre: null, checks: null, off: null, plots: [], sel: null };
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
  navigate = nav; if (G.off) G.off(); G.plots.forEach(p => p.destroy()); G.plots = [];
  G.profiles = (await api('/profiles')).filter(p => p.kind === 'sniffer');
  if (!G.p && G.profiles.length) { G.selId = G.profiles[0].id; G.p = clone(G.profiles[0]); }
  try { G.pre = await api('/sniff/preflight', { method: 'POST', body: {} }); } catch { G.pre = null; }
  try { G.bw = await api('/wifi/builtin/status'); } catch { G.bw = null; }
  draw(root);
  G.off = onLive(e => { if (['done', 'started'].includes(e.ev)) draw(root); else if (e.ev === 'sstate' || e.ev === 'spkt' || e.ev === 'sserial') liveUpdate(); });
  bindOnce(root, 'click', ev => click(ev, root));
  bindOnce(root, 'change', ev => change(ev, root));
}
const running = () => S.live.profile ? (S.live.running && S.live.profile.kind === 'sniffer') : (S.status.run.running && S.status.run.kind === 'sniffer');
const hasResult = () => S.live.done && S.live.profile && S.live.profile.kind === 'sniffer';

function draw(root) {
  const run = running(), res = hasResult() && !run;
  root.innerHTML = `<h1>Traffic sniffer</h1>
    <p class="muted" style="max-width:760px">See what really crosses the link: Ethernet (inline or from a switch mirror port), serial lines, or Wi-Fi. Captures are saved as <b>.pcapng</b> files that open in Wireshark; DNP3 and Modbus are decoded here too.</p>
    <div id="liveBox">${run ? liveHtml() : res ? resultHtml() : ''}</div>${run ? '' : editorHtml()}`;
  if (run) liveInit();
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
  const filt = m === 'serial' ? '' : `<div class="card"><h2>3 · What to capture (optional filter)</h2><p class="muted">Leave empty to capture everything, or pick a quick filter.</p>
    <div class="row" style="flex-wrap:wrap;gap:8px">${(G.pre ? G.pre.presets : []).map(f => `<button class="btn small ${c.filter.preset === f.id ? 'primary' : ''}" data-act="preset" data-id="${esc(f.id)}" title="${esc(f.about)}">${esc(f.name)}</button>`).join('')}</div>
    <div class="grid g3" style="margin-top:10px">${field('Filter expression', inp('data-p="filter.bpf" id="bpf" placeholder="e.g. tcp port 502 and host 10.0.0.5"', c.filter.bpf), 'A standard capture filter (BPF), as used by tcpdump and Wireshark capture filters.')}
      <div class="row qf" style="align-items:flex-end">${sel('id="fk"', [['host', 'This IP'], ['mac', 'This MAC'], ['vlan', 'VLAN id'], ['port', 'Port']], 'host')}<input id="fv" placeholder="value"><button class="btn small" data-act="addf">Add</button><button class="btn small" data-act="checkf">Check</button></div></div>
    <p id="fmsg" class="muted"></p></div>`;
  // The two things people change (how long, USB drive) are in view; file rotation and the free-space guard are folded.
  const stAdv = st.ring_mb !== 100 || st.ring_files !== 10 || st.ring_minutes > 0 || st.min_free_mb !== 50;
  const storage = `<div class="card"><h2>${m === 'serial' ? 3 : 4} · How long, and where to keep it</h2><div class="grid g3">
      ${field('Stop after (minutes, 0 = until I stop it)', inp('data-p="duration_min" data-t="num"', (c.duration_s || 0) / 60, 'number', 'min="0" step="any"'))}
      <label class="chk"><input type="checkbox" data-p="storage.usb" ${st.usb ? 'checked' : ''} ${usb ? '' : 'disabled'}> Save straight to the USB drive ${tip('Needs a USB flash drive plugged in.')}</label></div>
      ${usb ? `<p class="muted">USB drive: ${esc(G.pre.usb[0].path)}</p>` : '<p class="muted">No USB drive plugged in: captures are kept on this Pi.</p>'}
      <details id="sStoreAdv" ${stAdv ? 'open' : ''}><summary>Advanced (file size, how many files to keep, free space)</summary><div class="grid g3" style="margin-top:8px">
      ${field('Start a new file every (MB)', inp('data-p="storage.ring_mb" data-t="num"', st.ring_mb, 'number', 'min="0"'), 'Files rotate by size. 0 = one growing file.')}${field('Keep the newest (files)', inp('data-p="storage.ring_files" data-t="num"', st.ring_files, 'number', 'min="0"'), 'Older files are deleted (ring buffer). 0 = keep all.')}
      ${field('…or a new file every (minutes)', inp('data-p="storage.ring_minutes" data-t="num"', st.ring_minutes, 'number', 'min="0" step="any"'))}
      ${field('Always keep free space (MB)', inp('data-p="storage.min_free_mb" data-t="num"', st.min_free_mb, 'number', 'min="1"'), 'The capture stops before the storage gets fuller than this. At gigabit speed keep at least 100 MB.')}
      </div></details></div>`;
  return `<div class="card"><div class="row sp"><div><h2>1 · Pick a setup</h2><p class="muted">Start from a preset and change anything below.</p></div></div><div class="grid g3">${G.profiles.map(pcard).join('')}</div></div>
    <div class="card"><h2>2 · ${MODES[m]}</h2><div class="check ok" style="margin-top:6px"><div class="ic">i</div><div><b>How to connect it</b><small class="muted">${HOW[m]}</small></div></div>${conn}</div>${filt}${storage}
    <div class="card"><div class="row sp"><div><h2>${m === 'serial' ? 4 : 5} · Check, then start</h2></div><button class="btn small" data-act="preflight">✓ Run pre-flight check</button></div>
      <div id="checks">${(G.checks || []).map(checkRow).join('')}</div>
      <div class="row sp" style="margin-top:14px"><button class="btn" data-act="save">💾 Save as profile…</button><button class="btn primary" data-act="start">▶ Start capture</button></div></div>`;
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
  if (['serial.protocol', 'wifi.hop'].includes(el.dataset.p)) draw(root);
}
async function click(ev, root) {
  const a = ev.target.closest('[data-act]'); if (!a) return;
  const act = a.dataset.act, id = +a.dataset.id, i = a.dataset.i !== undefined ? +a.dataset.i : null;
  const reload = async () => { G.profiles = (await api('/profiles')).filter(p => p.kind === 'sniffer'); draw(root); };
  const c = G.p && G.p.sniff;
  try {
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
    else if (act === 'again') { S.live.done = null; draw(root); }
    else if (act === 'results') { S.openRun = S.live.runId; navigate('results'); }
    else if (act === 'row') { openDetail(+a.dataset.n); }
    else if (act === 'usb') { const r = await api(`/runs/${S.live.runId}/capture/usb`, { method: 'POST' }); toast(`Copied ${r.files} file(s) to ${r.path}`); }
  } catch (e) { toast(e.message, true, 8000); }
}
function effProfile() { const p = clone(G.p); delete p.id; return p; }

// ------------------------------------------------------------------ live view
const stat = (id, label, val, sub) => `<div class="stat"><small>${label}</small><b id="${id}">${val}</b><span id="${id}s">${sub}</span></div>`;
function liveHtml() {
  const m = S.live.profile ? S.live.profile.sniff.mode : (S.sniff.serial ? 'serial' : '');
  return `<div class="banner run"><div class="big">● CAPTURING</div><div><b>${esc(MODES[m] || 'Capture')} in progress</b> — you can leave this page; it keeps going.</div></div>
  <div class="card"><div class="row sp"><div><b id="sPhase">Starting…</b><div class="muted" id="sTime"></div></div><button class="btn danger" data-act="stop">■ Stop capture</button></div></div>
  <div class="grid g4" style="margin-bottom:16px">${stat('sPk', 'Packets', '–', '')}${stat('sBy', 'Data', '–', '')}${stat('sRate', 'Rate now', '–', '')}${stat('sFree', 'Free space', '–', '')}</div>
  <div class="card"><h2>Traffic per second</h2><div id="sC1" class="chart"></div></div>
  <div class="card"><div class="row sp"><h2 id="sListTitle">Packets</h2><span class="muted" id="sNote"></span></div><div class="scroll" style="max-height:340px;overflow:auto"><table><thead id="sHead"></thead><tbody id="sRows"></tbody></table></div></div>
  <div class="card" id="sDetail" hidden></div>`;
}
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function liveInit() {
  if (window.uPlot && $('#sC1')) {
    const axis = { stroke: css('--muted'), grid: { stroke: css('--line'), width: 1 }, ticks: { stroke: css('--line') } };
    const el = $('#sC1');
    G.c1 = new uPlot({ width: Math.max(200, el.clientWidth), height: 220, padding: [8, 8, 0, 0], scales: { x: { time: false }, y: { range: (u, a, b) => [0, Math.max(1, (b || 0) * 1.15)] } }, axes: [{ ...axis, label: 'seconds' }, { ...axis, label: 'packets/s', size: 62 }],
      series: [{ label: 'Time' }, { label: 'Packets/s', stroke: css('--accent'), width: 1.8, points: { show: false } }] }, [[], []], el);
    fitPlot(G.c1, el); G.plots.push(G.c1);
  }
  liveUpdate();
}
const bytes = n => n == null ? '–' : n > 1e9 ? (n / 1e9).toFixed(2) + ' GB' : n > 1e6 ? (n / 1e6).toFixed(1) + ' MB' : n > 1e3 ? (n / 1e3).toFixed(1) + ' KB' : n + ' B';
function liveUpdate() {
  const g = S.sniff, e = g.last; if (!e || !$('#sPk')) return;
  $('#sPhase').textContent = e.mode === 'serial' ? 'Listening on the serial line(s)' : 'Capturing frames'; $('#sTime').textContent = `${dur(e.elapsed_s)}${e.total_s ? ' of ' + dur(e.total_s) : ''}${e.channel ? ' · channel ' + e.channel : ''}`;
  $('#sPk').textContent = num(e.packets); $('#sPks').textContent = e.mode === 'serial' ? `frames${e.crc_fail ? ` · ${e.crc_fail} CRC failed` : ''}` : 'seen by the capture';
  $('#sBy').textContent = bytes(e.bytes); $('#sRate').textContent = e.mode === 'serial' ? `${num(e.pps)}/s` : `${num(e.pps)}/s`; $('#sRates').textContent = e.mode === 'serial' ? 'frames per second' : `${e.mbps} Mb/s`;
  $('#sFree').textContent = e.free_mb != null ? num(e.free_mb) + ' MB' : '–';
  $('#sNote').textContent = e.viewer && e.viewer.sampling ? `Live view shows a sample (${num(e.viewer.decoded)} of ${num(e.viewer.seen)}). The saved file has everything.` : '';
  if (G.c1) G.c1.setData([g.xs, g.pps]);
  const serial = e.mode === 'serial';
  $('#sHead').innerHTML = serial ? '<tr><th>#</th><th>Time</th><th>Channel</th><th>Len</th><th>CRC</th><th>Decoded</th></tr>' : '<tr><th>#</th><th>Time</th><th>Source</th><th>Destination</th><th>Protocol</th><th>Len</th><th>Info</th></tr>';
  const t0 = g.rows.length ? g.rows[0].t_ns : 0;
  $('#sRows').innerHTML = g.rows.slice(-120).reverse().map(r => serial
    ? `<tr class="click" data-act="row" data-n="${r.n}"><td>${r.n}</td><td>${new Date(r.t_ns / 1e6).toLocaleTimeString([], { hour12: false })}.${String(Math.floor(r.t_ns / 1e6) % 1000).padStart(3, '0')}</td><td>${esc(r.channel_name)}</td><td>${r.len}</td><td style="${r.crc_ok ? '' : 'color:var(--fail);font-weight:700'}">${r.crc_ok ? 'ok' : 'FAILED'}</td><td>${esc(r.info)}</td></tr>`
    : `<tr class="click" data-act="row" data-n="${r.n}"><td>${r.n}</td><td>${((r.t_ns - t0) / 1e9).toFixed(3)}</td><td>${esc(r.src)}</td><td>${esc(r.dst)}</td><td><b>${esc(r.proto)}</b></td><td>${r.len}</td><td ${r.errors ? 'style="color:var(--fail)"' : ''}>${esc(r.info)}</td></tr>`).join('');
}
async function openDetail(n) {
  try {
    const d = await api(`/sniff/packet/${n}`); const box = $('#sDetail'); box.hidden = false;
    const fields = f => Object.entries(f || {}).map(([k, v]) => `<tr><td style="width:38%"><b>${esc(k)}</b></td><td>${Array.isArray(v) ? v.map(x => esc(String(x))).join('<br>') : esc(String(v))}</td></tr>`).join('');
    const hex = (d.hex || '').match(/.{1,2}/g) || [];
    box.innerHTML = `<h2>Packet ${n}: ${esc(d.proto || '')}</h2>${(d.layers || []).map(l => `<h3>${esc(l.name)}</h3><table><tbody>${fields(l.fields)}</tbody></table>`).join('')}
      <h3>Bytes</h3><pre style="white-space:pre-wrap;word-break:break-all;font-size:.8rem;background:var(--panel2);padding:10px;border-radius:10px">${hex.map(h => h).join(' ')}</pre>`;
    box.scrollIntoView({ behavior: 'smooth' });
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ result card
function resultHtml() {
  const d = S.live.done;
  if (d.status === 'failed') return `<div class="banner fail"><div class="big">✕ COULD NOT CAPTURE</div><div>${esc(d.error || 'The capture failed.')}</div></div><div class="row" style="margin-bottom:16px"><button class="btn" data-act="again">Change settings</button></div>`;
  return `<div class="banner pass"><div class="big">✓ SAVED</div><div><b>The capture finished and the files are saved.</b>${d.error ? `<br><small>${esc(d.error)}</small>` : ''}</div></div>
    <div class="row" style="margin-bottom:16px"><button class="btn primary" data-act="results">See summary and download files →</button><button class="btn" data-act="usb">Copy to USB drive</button><button class="btn" data-act="again">Capture again</button></div>`;
}
