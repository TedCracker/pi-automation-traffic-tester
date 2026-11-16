// Radio facts (Orbit and similar): the packetization fields of a test, the data-use estimate, the Results block (radio, when it ran, bundling warning,
// data used, delay by hour of day) and the scheduled repeats (sheet + combined summary).  The Pi only records what the radio was set to; it never changes it.
import { $, $$, esc, api, toast, sheetBox, confirmBox } from './ui.js';

const num = v => (v == null ? '–' : Number(v).toLocaleString());
const f2 = v => (v == null ? '–' : +Number(v).toFixed(2));
const MODES = [['', 'Not set'], ['tcp_client', 'TCP client'], ['tcp_server', 'TCP server'], ['udp', 'UDP'], ['udp_multicast', 'UDP multicast'], ['other', 'Other']];

// ---------------------------------------------------------------- the form (Setup)
let usageCache = { key: '', data: null };
export const usageKey = p => `${(p.serial || {}).baud}|${p.interval_ms}|${(p.radio || {}).serial_mode || ''}`;
export async function loadUsage(p) {
  const key = usageKey(p); if (usageCache.key === key && usageCache.data) return usageCache.data;
  const q = new URLSearchParams({ baud: (p.serial || {}).baud || 19200, interval_ms: Number.isFinite(p.interval_ms) ? p.interval_ms : 10, serial_mode: (p.radio || {}).serial_mode || '' });
  const d = await api('/radio/usage?' + q); usageCache = { key, data: d }; return d;
}
export const usageBlock = (u, p) => {
  if (!u) return '<p class="muted">Working out the data use…</p>';
  const o = (u.over_the_air || {})[u.selected] || {}, py = u.payload || {};
  return `<div class="usage" data-testid="nt-usage"><b>Data use if this runs nonstop</b> <span class="muted">(each way; the radio carries both directions)</span>
    <div class="scroll"><table><thead><tr><th></th><th>per second</th><th>per day</th><th>per 30 days</th><th>both ways, 30 days</th></tr></thead><tbody>
    <tr><td>Payload only (serial bytes) — the “about 2 KB/s, 5 GB a month” figure</td><td>${num(py.bytes_per_s)} B</td><td>${num(py.mb_per_day)} MB</td><td>${f2(py.gb_per_30_days)} GB</td><td>${f2(py.both_ways_gb_per_30_days)} GB</td></tr>
    <tr><td><b>Over the air</b> — one frame per packet (${f2(u.frames_per_s)} packets/s) + ${o.header_bytes} B ${u.selected === 'tcp' ? 'IP/TCP' : 'IP/UDP'} header + 5 % cellular overhead</td><td>${num(o.bytes_per_s)} B</td><td>${num(o.mb_per_day)} MB</td><td><b>${f2(o.gb_per_30_days)} GB</b></td><td><b>${f2(o.both_ways_gb_per_30_days)} GB</b></td></tr></tbody></table></div>
    <p class="muted">At about ${u.frame_bytes} bytes per frame the packet headers are most of the traffic: the over-the-air figure is ${o.x_payload}× the payload figure. Check the data plan against the over-the-air figure. The Pi cannot see your FirstNet plan.</p></div>`;
};
// `field` and `select` are the form helpers of custom.js, passed in so this file does not import the form code.
export function radioFormHtml(c, field, select, errEl) {
  const p = c.p; if (p.transport !== 'serial') return '';
  const trip = p.mode === 'mirrored' && (p.mirrored || {}).codec === 'trip', baud = (p.serial || {}).baud || 19200, ch = 10000 / baud;
  const advice = trip ? `<p class="muted" data-testid="nt-radio-advice">Set each radio to send <b>one message per packet</b>: character timeout about 2 character times (≈ ${f2(2 * ch)} ms at ${baud} baud) and packet size = the frame (19 bytes). The Pi does not change the radio: type what you set, so every result records it.</p>` : '';
  const body = `<div class="grid g3">${field(c, 'radio.char_timeout_ms', 'Character timeout (ms)', { hint: 'How long the radio waits after the last serial character before it sends the packet.' })}
    ${field(c, 'radio.max_packet_bytes', 'Packet size (bytes)', { hint: 'The most serial bytes it puts in one packet.' })}
    ${select(c, 'radio.serial_mode', 'Serial mode', MODES)}
    ${field(c, 'radio.note', 'Note (optional)', { type: 'str' })}</div>${advice}${trip ? `<div id="ntUsage">${usageBlock(usageCache.data, p)}</div>` : ''}`;
  return trip ? `<div class="card ntradio" data-testid="nt-radio"><h3 style="margin:0 0 8px">Radio serial packetization</h3>${body}</div>`
    : `<details class="ntmore" id="ntRadio" data-testid="nt-radio"><summary>Radio serial packetization (optional)</summary>${body}</details>`;
}
export const radioErrors = (p, inRange) => {
  const e = {}, r = p.radio || {};
  if (r.char_timeout_ms != null && Number.isFinite(r.char_timeout_ms) && !inRange('radio.char_timeout_ms', r.char_timeout_ms)) e['radio.char_timeout_ms'] = 'Enter 0 to 10000 ms, or leave empty.';
  if (r.max_packet_bytes != null && Number.isFinite(r.max_packet_bytes) && (!inRange('radio.max_packet_bytes', r.max_packet_bytes) || !Number.isInteger(Number(r.max_packet_bytes)))) e['radio.max_packet_bytes'] = 'Enter a whole number of bytes (1 to 65535), or leave empty.';
  return e;
};

// ---------------------------------------------------------------- the Results block
const hourBars = hod => {
  const mx = Math.max(1, ...hod.map(h => h.max || 0)), by = Object.fromEntries(hod.map(h => [h.hour, h]));
  return `<div class="hrbars" data-testid="hour-chart" role="img" aria-label="Delay by hour of day">${Array.from({ length: 24 }, (_, i) => { const h = by[i]; return h ? `<div title="${String(i).padStart(2, '0')}:00 — p99.9 ${h.p999} ms, slowest ${h.max} ms, ${h.n} round trips"><i style="height:${Math.round(56 * (h.max || 0) / mx)}px;opacity:.35"></i><i style="height:${Math.round(56 * (h.p999 || 0) / mx)}px;margin-top:-${Math.round(56 * (h.p999 || 0) / mx)}px"></i><small>${i}</small></div>` : `<div><i style="height:0"></i><small>${i}</small></div>`; }).join('')}</div>
    <p class="muted" style="margin:2px 0 0;font-size:.8rem">Dark bar = p99.9, light bar = slowest, per hour of day (0–23, local time). Highest: ${mx} ms.</p>`;
};
export const hourlyHtml = hr => {
  if (!hr || !(hr.by_hour_of_day || []).length) return '';
  const row = (lab, h) => `<tr><td>${esc(lab)}</td><td>${num(h.n)}</td><td>${f2(h.p50)}</td><td>${f2(h.p99)}</td><td>${f2(h.p999)}</td><td>${f2(h.max)}</td><td>${num(h.lost)}</td><td>${num(h.outages)}</td></tr>`;
  return `<h3 style="margin:14px 0 6px">Delay by hour of day and weekday / weekend (ms, local time)</h3>
    <div class="scroll"><table data-testid="hourly-table"><thead><tr><th>Hour</th><th>Round trips</th><th>p50</th><th>p99</th><th>p99.9</th><th>Slowest</th><th>Lost frames</th><th>Outages</th></tr></thead><tbody>
    ${hr.by_hour_of_day.map(h => row(String(h.hour).padStart(2, '0') + ':00', h)).join('')}
    ${['weekday', 'weekend'].filter(k => (hr.kinds || {})[k]).map(k => row(k === 'weekday' ? 'Weekdays' : 'Weekends', hr.kinds[k]).replace('<tr>', '<tr style="font-weight:700">')).join('')}</tbody></table></div>
    ${hourBars(hr.by_hour_of_day)}<p class="muted" style="font-size:.8rem">${esc(hr.note || '')}</p>`;
};
export function radioResultHtml(s) {
  const rd = s.radio, w = s.when, m = s.mirrored || {}, pk = m.packetization, u = s.usage;
  if (!rd && !w && !pk) return '';
  const a = (u && u.actual) || m.bytes || {};
  return `<div class="card" data-testid="radio-result"><h2>Radio, time of day and data use</h2>
    <p data-testid="radio-line" style="margin:0 0 6px">${rd && rd.recorded ? '📻 ' + esc(rd.text) : '<span class="muted">📻 radio packetization not recorded</span>'}</p>
    ${w ? `<p style="margin:0 0 6px" data-testid="when-line">🕒 Ran <b>${esc(w.start_local)}</b> to <b>${esc(w.end_local)}</b> (${esc(w.tag)}). <span class="muted">${esc(w.note)}</span></p>` : ''}
    ${pk ? `<p class="${pk.suspected ? 'banner fail' : 'muted'}" style="${pk.suspected ? 'padding:8px 12px;border-radius:8px;margin:6px 0' : 'margin:0 0 6px'}" data-testid="bundling-line">${pk.suspected ? '⚠ ' : ''}${esc(pk.message)}${pk.burst_pct != null ? ` <small class="muted">(${pk.burst_pct} % of frames arrived in bursts${pk.mean_burst ? ' of ' + pk.mean_burst : ''})</small>` : ''}</p>` : ''}
    ${a.tx_bytes != null ? `<p style="margin:0 0 6px" data-testid="bytes-line">📶 This run sent <b>${num(a.tx_bytes)}</b> bytes and received <b>${num(a.rx_bytes)}</b> bytes of frames (about ${num(a.tx_over_air_bytes)} / ${num(a.rx_over_air_bytes)} bytes over the air).</p>` : ''}
    ${u ? usageBlock(u, null) : ''}${hourlyHtml(m.hourly)}</div>`;
}

// ---------------------------------------------------------------- scheduled repeats
const tzNow = () => new Date().toLocaleString();
export async function repeatSheet(profile) {
  const name = profile.name;
  const html = `<p class="muted" style="margin-top:0">Run “${esc(name)}” again and again, so the result covers busy hours and weekends, not one quiet window. It starts by itself, survives a reboot, and one combined summary collects the runs. A run that cannot start (Pi busy, port in use) is counted as a missed slot, never run late.</p>
    <label class="f"><span>Schedule</span><select id="rpKind" data-testid="rp-kind"><option value="every">Every N hours</option><option value="times">At these times of day</option></select></label>
    <div id="rpEvery" class="row" style="gap:12px;margin-top:8px"><label class="f"><span>Every (hours)</span><input id="rpHours" type="number" step="any" value="6" data-testid="rp-hours"></label></div>
    <div id="rpTimes" hidden style="margin-top:8px"><label class="f"><span>Times of day (24 h, comma-separated)</span><input id="rpTimeList" value="08:00, 12:00, 17:30, 22:00" data-testid="rp-times"></label>
      <label class="f" style="margin-top:8px"><span>On</span><select id="rpDays" data-testid="rp-days"><option value="all">Every day, including weekends</option><option value="weekdays">Weekdays only</option><option value="weekend">Weekends only</option></select></label></div>
    <label class="f" style="margin-top:8px"><span>For how many days</span><input id="rpDaysN" type="number" step="any" value="7" data-testid="rp-fordays"></label>
    <p class="muted" id="rpNote" style="margin-top:8px"></p>`;
  const pr = sheetBox('Repeat this test on a schedule', html, [{ id: 'no', label: 'Cancel' }, { id: 'go', label: 'Start schedule', cls: 'primary' }], 'repeat-sheet');
  const sync = () => { const k = $('#rpKind').value; $('#rpEvery').hidden = k !== 'every'; $('#rpTimes').hidden = k !== 'times'; const d = +$('#rpDaysN').value, h = +$('#rpHours').value; $('#rpNote').textContent = k === 'every' && h > 0 ? `About ${Math.floor(d * 24 / h) + 1} runs of ${Math.round(profile.duration_s / 60)} min each, the first one now (the Pi's clock: ${tzNow()}).` : `Times are the Pi's local time (${tzNow()}).`; };
  ['rpKind', 'rpHours', 'rpDaysN', 'rpTimeList', 'rpDays'].forEach(i => { const e = $('#' + i); if (e) e.oninput = e.onchange = sync; }); sync();
  if (await pr !== 'go') return null;
  const kind = $('#rpKind').value, schedule = { kind, for_days: +$('#rpDaysN').value };
  if (kind === 'every') schedule.every_h = +$('#rpHours').value; else { schedule.times = $('#rpTimeList').value; schedule.days = $('#rpDays').value; }
  try { const r = await api('/repeats', { method: 'POST', body: { profile, schedule } }); toast(`Scheduled: ${r.text}. Next: ${r.next_local || '—'}.`, false, 8000); return r; }
  catch (e) { toast(e.message, true, 8000); return null; }
}
export const combinedHtml = c => {
  const t = c.totals || {}, cov = c.coverage || {};
  return `<div class="card" data-testid="repeat-summary"><h2>Combined summary — ${esc(c.name)}</h2><p class="muted" style="margin:0 0 8px">${esc(c.schedule_text)} · ${esc(c.coverage_note)}</p>
    <div class="tiles"><div class="tile"><b>${num(t.runs)}</b><small>runs (${num(t.passed)} passed, ${num(t.failed)} failed, ${num(t.missed_slots)} missed slots)</small></div>
    <div class="tile"><b>${num(t.frames_lost)}</b><small>frames lost of ${num(t.frames_sent)}</small></div>
    <div class="tile"><b>${f2(t.worst_p999)} ms</b><small>worst p99.9 of any run</small></div><div class="tile"><b>${f2(t.worst_max)} ms</b><small>slowest frame of any run</small></div></div>
    ${t.bundling_runs ? `<p class="banner fail" style="padding:8px 12px;border-radius:8px">⚠ The radio seemed to be bundling messages in ${t.bundling_runs} run(s) — check its character timeout.</p>` : ''}
    ${(c.by_tag || []).length ? `<div class="scroll"><table><thead><tr><th>Time of day</th><th>Runs</th><th>Failed</th><th>Lost</th><th>Worst p99</th><th>Worst p99.9</th><th>Slowest</th></tr></thead><tbody>${c.by_tag.map(x => `<tr><td>${esc(x.tag)}</td><td>${x.runs}</td><td>${x.fails}</td><td>${num(x.lost)}</td><td>${f2(x.worst_p99)}</td><td>${f2(x.worst_p999)}</td><td>${f2(x.worst_max)}</td></tr>`).join('')}</tbody></table></div>` : ''}
    <div class="scroll"><table><thead><tr><th>Run</th><th>Started</th><th>When</th><th>Result</th><th>Lost</th><th>p99</th><th>p99.9</th><th>Slowest</th></tr></thead><tbody>${(c.rows || []).map(x => `<tr><td><a href="#" data-open-rpt-run="${x.run_id}">#${x.run_id}</a></td><td>${esc(x.start || '')}</td><td>${esc(x.tag)}</td><td>${esc(x.verdict || x.status)}${x.bundling ? ' ⚠' : ''}</td><td>${num(x.lost)}</td><td>${f2(x.p99)}</td><td>${f2(x.p999)}</td><td>${f2(x.max)}</td></tr>`).join('')}</tbody></table></div>
    <p class="muted" style="font-size:.8rem">${esc(c.basis)}</p>${(c.errors || []).length ? `<p class="muted">Could not start: ${esc(c.errors.join(' · '))}</p>` : ''}
    <div class="row"><a class="btn small" href="/api/report.pdf?repeat_id=${esc(c.id)}" download data-testid="repeat-pdf">📄 PDF with combined summary</a></div></div>`;
};
export async function mountRepeats(box, onOpenRun) {
  if (!box) return;
  let list = []; try { list = await api('/repeats'); } catch (e) { return; }
  if (!list.length) { box.innerHTML = ''; return; }
  box.innerHTML = `<div class="card" data-testid="repeats-card"><h2>Scheduled repeats</h2>${list.map(r => `<div class="row sp" style="padding:6px 0;border-top:1px solid var(--line)" data-testid="repeat-row"><div><b>${esc(r.name)}</b> <span class="tag">${esc(r.status)}</span><br><span class="muted">${esc(r.text)} · ${num((r.runs || []).length)} of ${r.total_slots} runs done · ${num(r.missed)} missed${r.next_local ? ' · next ' + esc(r.next_local) : ''}</span></div>
    <div class="tools"><button class="btn small" data-rp-sum="${esc(r.id)}">Combined summary</button>${r.status === 'waiting' ? `<button class="btn small" data-rp-stop="${esc(r.id)}">Stop</button>` : ''}<button class="btn small ghost" data-rp-del="${esc(r.id)}">Remove</button></div></div>`).join('')}<div id="rpSum"></div></div>`;
  $$('[data-rp-sum]', box).forEach(b => b.onclick = async () => { const c = await api(`/repeats/${b.dataset.rpSum}/summary`); $('#rpSum', box).innerHTML = combinedHtml(c); $$('[data-open-rpt-run]', box).forEach(a => a.onclick = ev => { ev.preventDefault(); onOpenRun && onOpenRun(+a.dataset.openRptRun); }); });
  $$('[data-rp-stop]', box).forEach(b => b.onclick = async () => { await api(`/repeats/${b.dataset.rpStop}/stop`, { method: 'POST' }); toast('Schedule stopped. Runs already done are kept.'); mountRepeats(box, onOpenRun); });
  $$('[data-rp-del]', box).forEach(b => b.onclick = async () => { if (await confirmBox('Remove this schedule?', 'It stops. The runs it already made stay in Results.', 'Remove', true)) { await api(`/repeats/${b.dataset.rpDel}`, { method: 'DELETE' }); mountRepeats(box, onOpenRun); } });
}
