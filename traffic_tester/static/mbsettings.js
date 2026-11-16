// The Mirrored Bits channel settings (spec section 6) as form fields, shared by Setup -> Advanced settings (data-path / data-type)
// and the custom-test form (data-k / data-t).  Everything lives in profile.mirrored.settings; an empty field means "use the default"
// (the default is shown as the placeholder), so a saved test only carries what was changed.
export const MB_DEF = { proto: 'A', tx_id: 1, rx_id: 2, rok_n: 2, rbad_pu_s: 10, cbad_pu_pct: 10, cbad_window_s: 60, k_timing: 1.5, late_floor_ms: 5, rate_mode: 'max', line_hz: 60, per_cycle: 4,
  echo: 'auto', echo_bit: 8, rtt_max_ms: 40, trip_bit: 1, rmb_pu: [1, 1, 1, 1, 1, 1, 1, 1], rmb_do: [1, 1, 1, 1, 1, 1, 1, 1], rmb_default: [0, 0, 0, 0, 0, 0, 0, 0] };
export const MB_TYPES = ['snum', 'sstr', 'sbool', 'sints'];
const e = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const get = (o, path) => path.split('.').reduce((a, k) => a == null ? a : a[k], o);

// attr(path, type) -> the attribute text that makes the host form bind the field (e.g. ` data-path="x" data-type="snum"`)
export function mbFieldsHtml(p, attr, serial) {
  const S = (p.mirrored && p.mirrored.settings) || {}, I = (p.mirrored && p.mirrored.impair) || {};
  const num = (k, label, hint, ph = MB_DEF[k], base = 'settings', src = S) => `<label class="f"><span>${label}</span><input type="number" step="any" inputmode="decimal" placeholder="${e(ph)}" value="${e(src[k] ?? '')}"${attr(`mirrored.${base}.${k}`, 'snum')}><small class="muted">${hint}</small></label>`;
  const sel = (k, label, opts, hint = '') => `<label class="f"><span>${label}</span><select${attr(`mirrored.settings.${k}`, 'sstr')}><option value="" ${S[k] == null ? 'selected' : ''}>Default (${e(opts.find(o => o[0] === MB_DEF[k])?.[1] ?? MB_DEF[k])})</option>${opts.map(([v, l]) => `<option value="${e(v)}" ${S[k] === v ? 'selected' : ''}>${e(l)}</option>`).join('')}</select>${hint ? `<small class="muted">${hint}</small>` : ''}</label>`;
  const ints = (k, label, hint) => `<label class="f"><span>${label}</span><input placeholder="${MB_DEF[k].join(', ')}" value="${e((S[k] || []).join(', '))}"${attr(`mirrored.settings.${k}`, 'sints')}><small class="muted">${hint}</small></label>`;
  return `<details class="mbadv" data-testid="mb-advanced"><summary>Mirrored Bits channel settings <span class="muted">(PROTO, IDs, ROK, RBAD, CBAD, per-bit counts, echo, impairment)</span></summary>
    <p class="muted">Both ends of the channel are set up from this one form: the Pi that answers gets the mirror image (PROTO B, TX_ID and RX_ID swapped). Names are the ones SEL uses; they vary by model, and can be renamed on the live screen. Message format: SimCodec — emulator-to-emulator only, not SEL wire-compatible.</p>
    <h4>Channel</h4><div class="grid g3">
      ${sel('proto', 'PROTO (this end)', [['A', 'A'], ['B', 'B']], 'The two ends must be opposite. The answering Pi is set to the other letter.')}
      ${num('tx_id', 'TX_ID', 'Sender ID in every message (0–31).')}${num('rx_id', 'RX_ID', 'The ID this end accepts (0–31). Must be the other end’s TX_ID.')}
      ${num('rok_n', 'ROK after N good messages <span class="pill warn">ASSUMPTION</span>', 'ROK comes back after this many good messages in a row. Our assumption (2): verify against a real device.')}
      ${num('rbad_pu_s', 'RBAD_PU (s)', 'RBAD asserts when ROK has been 0 this long.')}${num('cbad_pu_pct', 'CBAD_PU (%)', 'CBAD asserts when ROK was 0 for more than this share of the window.')}
      ${num('cbad_window_s', 'CBAD window (s)', 'The rolling window for the unavailability percentage.')}${num('k_timing', 'K (timing)', 'A message later than K × the nominal message time is rejected.')}
      ${num('late_floor_ms', 'Timing floor (ms)', 'K × nominal is never tighter than this: a normal Linux host cannot honour 1 ms.')}</div>
    <h4>Message rate</h4><div class="grid g3">
      ${sel('rate_mode', 'Rate', [['max', 'Back to back at the baud rate'], ['per_cycle', 'Capped: messages per power-system cycle']], 'Real relays send 4–8 messages per cycle.')}
      ${sel('line_hz', 'Power system', [[50, '50 Hz'], [60, '60 Hz']], 'Used by the cap.').replace('<select', '<select data-num="1"')}${num('per_cycle', 'Messages per cycle', 'With the cap (4–8 like a relay).')}
      ${serial ? `<label class="chk"><input type="checkbox"${attr('mirrored.settings.rtscts', 'sbool')} ${S.rtscts ? 'checked' : ''}> RTS/CTS hardware handshake</label><small class="muted">Data bits, parity and the extra stop bit (2 stop bits for 10-bit characters) are in the serial settings above.</small>` : ''}</div>
    <h4>Echo bit (round trip)</h4><p class="muted" style="margin:0 0 6px">Not used with the Trip frame format: there the echo travels inside every frame and all 8 bits are yours.</p><div class="grid g3">
      ${sel('echo', 'Echo', [['auto', 'Automatic (PROTO A near, B far)'], ['near', 'Near end (flips it back)'], ['far', 'Far end (copies it)'], ['off', 'Off (all 8 bits are yours)']], 'One end copies the bit, the other flips it each time it returns: every flip is one round trip.')}
      ${num('echo_bit', 'Echo bit (1–8)', 'Reserved: not available for your own signals.')}${num('rtt_max_ms', 'RTT_MAX (ms)', 'The echo alarm fires when a round trip takes longer.')}
      ${num('trip_bit', 'Test bit (1–8)', 'The bit the test flips every few seconds to time a bit change.')}</div>
    <h4>Each received bit</h4><div class="grid g3">
      ${ints('rmb_pu', 'RMB1–8 PU', '8 numbers: good messages before 0→1.')}${ints('rmb_do', 'RMB1–8 DO', '8 numbers: good messages before 1→0.')}${ints('rmb_default', 'RMB1–8 default', '8 values (0/1): where each bit sits while ROK=0.')}</div>
    <h4>Impairment injector at the start <span class="muted">(can be changed live)</span></h4><div class="grid g3">
      ${num('latency_ms', 'Fixed latency (ms)', 'Added to every message sent by the starting Pi.', 0, 'impair', I)}${num('jitter_ms', '± jitter (ms)', 'Random extra delay.', 0, 'impair', I)}${num('drop_pct', 'Random drop (%)', '', 0, 'impair', I)}
      ${num('burst_len_ms', 'Burst outage (ms)', 'Drop everything this long ...', 0, 'impair', I)}${num('burst_every_s', '... every (s)', '', 0, 'impair', I)}${num('bit_error_pct', 'Bit errors (%)', 'Share of messages with a flipped bit.', 0, 'impair', I)}</div></details>`;
}

// the value a bound field holds, or undefined = "remove the setting (default)"
export function parseMb(el, type) {
  if (type === 'sbool') return el.checked ? true : undefined;
  const v = el.value.trim();
  if (v === '') return undefined;
  if (type === 'snum') { const n = Number(v); return Number.isFinite(n) ? n : undefined; }
  if (type === 'sints') { const a = v.split(/[,\s]+/).filter(Boolean).map(Number); return a.length === 8 && a.every(Number.isFinite) ? a : undefined; }
  if (el.dataset.num) return Number(v);
  return v;
}
export function putMb(obj, path, v) {
  const k = path.split('.'); let t = obj;
  for (const x of k.slice(0, -1)) t = (t[x] ??= {});
  if (v === undefined) delete t[k.at(-1)]; else t[k.at(-1)] = v;
}
