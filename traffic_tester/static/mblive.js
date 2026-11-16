// Mirrored Bits live view (docs/MIRRORED-BITS-SPEC.md section 9; look and feel from docs/design/mirrored_bits_stream_simple.html, own code, no CDN).
// Two end cards (8 sent bits you can tap, 8 received bits, ROK / RBAD / CBAD / LBOK lamps), two lanes of message chips driven by the real traffic
// (sampled for display), Break link + quick impairment chips, then numbers / charts / event log / settings / how it works.
// One poll of /api/mb/live every 250 ms (paused while the tab is hidden); the drawing runs on requestAnimationFrame only while something moves.
import { $, $$, esc, api, toast, tip } from './ui.js';

const POLL_MS = 250, TRAVEL_MS = 1500, MAX_CHIPS = 16;
const REASON_TEXT = { parity: 'parity error', framing: 'framing error / not a message', overrun: 'receive overrun', consistency: 'the 3 copies of a bit disagreed', crc: 'CRC did not match (damaged frame)', address: 'wrong ID (not our RX_ID)',
  loopback: 'our own TX_ID: hearing ourselves (loopback)', timing: 'arrived too late (inter-arrival over K x nominal)', missing: 'a message was missing' };
const REJECT_ORDER = ['parity', 'framing', 'overrun', 'consistency', 'crc', 'address', 'loopback', 'timing', 'missing'];
const CHIPS = [['lat', '+50 ms', { latency_ms: 50 }, { latency_ms: 0 }, c => c.latency_ms > 0 && !c.jitter_ms], ['jit', '±10 ms jitter', { jitter_ms: 10 }, { jitter_ms: 0 }, c => c.jitter_ms > 0],
  ['drop', '10 % drop', { drop_pct: 10 }, { drop_pct: 0 }, c => c.drop_pct > 0], ['err', 'Bit errors', { bit_error_pct: 5 }, { bit_error_pct: 0 }, c => c.bit_error_pct > 0]];
const f1 = v => v == null ? '–' : (+v).toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2);
const n0 = v => v == null ? '–' : Number(v).toLocaleString();
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

export function mountMB(root, opts = {}) {
  const st = { alive: true, tx: 0, rx: 0, rtx: 0, rrx: 0, data: null, lanes: { ab: [], ba: [] }, tab: opts.tab || 'numbers', raf: 0, timer: 0, hits: [], busy: false, err: '' };
  root.innerHTML = `<div class="mbv" data-testid="mb-live">
    <div class="mbwarn" id="mbWarn" role="alert" hidden></div>
    <div class="mbhead"><b>Mirrored Bits channel</b><small class="muted" id="mbCodec">SimCodec — emulator-to-emulator only, not SEL wire-compatible</small></div>
    <div class="mbstage" id="mbStage">
      <section class="mbcard" data-end="A" data-testid="mb-card-a"></section>
      <div class="mblanes">
        <div class="mblane" data-lane="ab" data-testid="mb-lane-a-to-b"><div class="mblabel"></div><div class="mbtrack"><canvas></canvas><div class="mbtip" hidden></div></div><div class="mbrate muted"></div></div>
        <div class="mblane" data-lane="ba" data-testid="mb-lane-b-to-a"><div class="mblabel"></div><div class="mbtrack"><canvas></canvas><div class="mbtip" hidden></div></div><div class="mbrate muted"></div></div>
      </div>
      <section class="mbcard" data-end="B" data-testid="mb-card-b"></section>
    </div>
    <div class="mbctl" data-testid="mb-controls"><button class="btn" id="mbBreak" data-testid="mb-break-link">Break link</button>
      <span class="mbchips" id="mbChips">${CHIPS.map(([k, l]) => `<button type="button" class="mbchip" data-chip="${k}" aria-pressed="false">${l}</button>`).join('')}</span>
      <span class="muted mbhint">Tap a “Sends” square to flip that bit. The chips add the problem on both directions, right now.</span></div>
    <div class="mbtabs" role="tablist" id="mbTabs">${[['numbers', 'Numbers'], ['charts', 'Charts'], ['events', 'Event log'], ['settings', 'Settings'], ['about', 'How it works']].map(([k, l]) => `<button role="tab" data-tab="${k}">${l}</button>`).join('')}</div>
    <div class="mbpane" id="mbPane"></div></div>`;
  const q = s => $(s, root);
  const cardEl = { A: q('[data-end=A]'), B: q('[data-end=B]') };
  const laneEl = { ab: q('[data-lane=ab]'), ba: q('[data-lane=ba]') };

  // ------------------------------------------------------------ which end is which
  function ends(d) {
    const L = d.local, R = d.remote;
    let a = L, b = R, aLoc = true, bLoc = false;
    if (R && R.proto === 'A' && L.proto !== 'A') { a = R; b = L; aLoc = false; bLoc = true; }
    return { a, b, aLoc, bLoc };
  }
  const feeds = d => {            // newest messages seen by each end (rx) and sent by each end (tx)
    const { aLoc } = ends(d);
    const loc = { rx: d.local.rx_chips || [], tx: d.local.tx_chips || [] }, rem = { rx: d.remote_rx_chips || [], tx: d.remote_tx_chips || [] };
    return aLoc ? { a: loc, b: rem } : { a: rem, b: loc };
  };

  // ------------------------------------------------------------ polling
  async function poll() {
    if (!st.alive || !root.isConnected) { st.alive = false; return; }
    if (!document.hidden && !st.busy) {
      st.busy = true;
      try {
        const d = await api(`/mb/live?since_tx=${st.tx}&since_rx=${st.rx}&r_since_tx=${st.rtx}&r_since_rx=${st.rrx}`);
        st.err = '';
        if (d && d.running_any) { ingest(d); } else if (!st.data) { showIdle(); }
      } catch (e) { st.err = e.message; }
      st.busy = false;
    }
    st.timer = setTimeout(poll, POLL_MS);
  }
  function showIdle() {
    q('#mbStage').classList.add('idle');
    cardEl.A.innerHTML = `<div class="muted">No Mirrored Bits channel is running on this Pi right now.</div>`; cardEl.B.innerHTML = '';
  }
  function ingest(d) {
    q('#mbStage').classList.remove('idle');
    st.tx = d.local.tx_last ?? st.tx; st.rx = d.local.rx_last ?? st.rx; st.rtx = d.remote_tx_last ?? st.rtx; st.rrx = d.remote_rx_last ?? st.rrx;
    const now = performance.now(), { a, b } = ends(d), fd = feeds(d);
    // lane A -> B: what B received (with its verdict per message); if B is not visible, what A sent. Lane B -> A the other way round.
    const spawn = (lane, arr, ok) => {
      const n = arr.length; if (!n) return;
      // sampled for display: at most 2 rejected / missing ones plus the newest good one per poll, so chips never pile up and a red one is never skipped
      const bads = arr.filter(c => c.bad).slice(-2), pick = [...bads, ...(bads.length < 2 ? [arr[n - 1]] : [])].filter((c, i, a) => a.indexOf(c) === i);
      pick.forEach((c, i) => st.lanes[lane].push({ t0: now - i * 120, bits: c.bits, bad: ok ? c.bad : null, ok }));
      st.lanes[lane] = st.lanes[lane].slice(-MAX_CHIPS);
    };
    if (b) spawn('ab', fd.b.rx, true); else spawn('ab', fd.a.tx, false);
    if (a) spawn('ba', fd.a.rx, true); else spawn('ba', fd.b.tx, false);
    st.data = d;
    paint(d);
    kick();
  }

  // ------------------------------------------------------------ cards
  const lamp = (on, label, cls = '', t = '') => `<span class="mbl ${on ? 'on ' + cls : ''}" title="${esc(t || label)}"><i></i>${label}</span>`;
  function endHtml(e, loc, lbl) {
    if (!e) return `<div class="mbname"><b>${esc(lbl || 'The other Pi')}</b></div><p class="muted">Its side is not visible from here (this Pi does not control it, or it is not answering).</p>`;
    const S = e.settings || {}, L = S.labels || {}, nm = k => L[k] || ({ proto: 'PROTO', tx_id: 'TX_ID', rx_id: 'RX_ID', baud: 'BAUD' }[k]);
    const echo = e.echo_bit, tmb = e.tmb | 0, tx = e.tx_bits | 0, fr = (e.counters || {}).frames;
    const sq = (i, bits, tap) => { const on = (bits >> i) & 1, reserved = tap && echo === i + 1; return `<button type="button" class="mbsq ${on ? 'on' : ''} ${reserved ? 'echo' : ''}" ${tap && !reserved ? `data-flip="${i + 1}" data-side="${loc ? 'local' : 'remote'}" aria-pressed="${on}"` : 'disabled tabindex="-1"'} aria-label="${tap ? 'TMB' : 'RMB'}${i + 1} ${on ? 1 : 0}${reserved ? ' (echo)' : ''}" title="${tap ? 'TMB' : 'RMB'}${i + 1}${reserved ? ' - reserved for the round-trip echo' : ''}">${i + 1}${reserved ? '<small>echo</small>' : ''}</button>`; };
    const row = (bits, tap) => [0, 1, 2, 3, 4, 5, 6, 7].map(i => sq(i, bits, tap)).join('');
    return `<div class="mbname"><b>${esc(e.label || e.name)}</b> <span class="pill na mbproto">${esc(nm('proto'))} ${esc(e.proto)}</span>${e.role ? ` <small class="muted" data-testid="mb-role">${esc(e.role)}</small>` : ''}</div>
      ${fr ? `<div class="muted" data-testid="mb-frames" style="font-size:.85em">Frames: ${fr.good} good · ${fr.lost} lost · ${fr.crc_errors} CRC errors · ${fr.duplicates + fr.out_of_order} repeated / out of order${e.rtt && e.rtt.p999 != null ? ` · round trip p50 ${e.rtt.p50} / p99.9 ${e.rtt.p999} / max ${e.rtt.max} ms` : ''}</div>` : ''}
      <div class="mbrowl">Sends TMB 1–8 <small class="muted">${loc ? 'tap to flip' : 'tap flips it on that Pi'}</small></div><div class="mbsqs" data-testid="mb-tmb">${row(echo ? tx : tmb, true)}</div>
      <div class="mbrowl">Receives RMB 1–8</div><div class="mbsqs" data-testid="mb-rmb">${row(e.rmb | 0, false)}</div>
      <div class="mblamps"><span class="mbrok ${e.rok ? 'ok' : e.health === 'waiting' ? 'wait' : 'bad'}" data-testid="mb-rok"><i></i>ROK</span>
        ${lamp(e.rbad, 'RBAD', 'bad', 'Channel down longer than RBAD_PU')}${lamp(e.cbad, 'CBAD', 'bad', 'Unavailability above CBAD_PU over the window')}${lamp(e.lbok, 'LBOK', 'ok', 'Loopback test passing')}${e.loopback ? lamp(1, 'LOOPBACK', 'bad', 'We hear our own TX_ID') : ''}${e.rtt_alarm ? lamp(1, 'RTT alarm', 'warn', 'No echo transition within RTT_MAX') : ''}</div>
      <div class="mbid muted">${esc(nm('tx_id'))} ${e.tx_id} · ${esc(nm('rx_id'))} ${e.rx_id} · ${n0(e.baud)} ${esc(nm('baud'))}</div>`;
  }
  function paint(d) {
    const { a, b, aLoc, bLoc } = ends(d), far = d.remote_label || '';
    cardEl.A.innerHTML = endHtml(a, aLoc, a ? a.label : ''); cardEl.B.innerHTML = endHtml(b, bLoc, d.far_label || 'The other Pi');
    cardEl.A.classList.toggle('down', a && !a.rok); cardEl.B.classList.toggle('down', !!b && !b.rok);
    const w = q('#mbWarn'); const ws = [...(d.warnings || [])]; if (st.err) ws.push('Lost contact with this Pi: ' + st.err);
    w.hidden = !ws.length; w.innerHTML = ws.map(x => '⚠ ' + esc(x)).join('<br>');
    q('#mbCodec').textContent = d.local.codec || 'SimCodec — emulator-to-emulator only, not SEL wire-compatible';
    // lanes: labels, real rate, one-way delay (RTT/2 from the echo), failed state
    const nameA = a ? (a.label || a.name) : 'A', nameB = b ? (b.label || b.name) : (d.far_label || 'B');
    const near = [a, b].find(x => x && x.rtt && x.rtt.n && x.echo_role === 'near') || null;
    const ow = near ? near.rtt.last / 2 : null;
    const lane = (key, from, to, snd, rcv) => {
      const el = laneEl[key], down = rcv ? !rcv.rok : (snd ? !snd.rok : false);
      el.classList.toggle('grey', !!down);
      $('.mblabel', el).textContent = `${from} → ${to}: nonstop messages`;
      const rate = snd ? snd.tx_rate : null, nom = snd ? snd.nominal_ms : null;
      $('.mbrate', el).textContent = (rate != null ? `${n0(Math.round(rate))} msg/s · ${f1(nom)} ms each` : '') + (ow != null ? ` · ≈ ${f1(ow)} ms one way` : '') + (down ? ' · channel failed' : '');
    };
    lane('ab', nameA, nameB, a, b); lane('ba', nameB, nameA, b, a);
    const imp = (d.local.impair || {}), brk = !!imp.break_link;
    const bb = q('#mbBreak'); bb.textContent = brk ? 'Restore link' : 'Break link'; bb.classList.toggle('on', brk);
    $$('.mbchip', root).forEach(c => { const def = CHIPS.find(x => x[0] === c.dataset.chip); const on = def[4](imp); c.classList.toggle('on', on); c.setAttribute('aria-pressed', on); });
    paintPane(d);
  }

  // ------------------------------------------------------------ lanes (canvas)
  function drawLane(key, now) {
    const el = laneEl[key], cv = $('canvas', el), box = cv.parentElement;
    const W = box.clientWidth, H = box.clientHeight; if (!W || !H) return false;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (cv.width !== W * dpr || cv.height !== H * dpr) { cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + 'px'; cv.style.height = H + 'px'; }
    const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    const vert = H > W * 1.2, len = vert ? H : W, thick = vert ? W : H, forward = key === 'ab';
    const grey = el.classList.contains('grey'), ok = css('--accent'), bad = css('--fail'), line = css('--line'), mut = css('--muted'), panel = css('--panel');
    const cw = vert ? Math.min(thick - 6, 46) : 64, ch = vert ? 22 : Math.min(thick - 6, 26);
    g.save(); g.strokeStyle = mut; g.globalAlpha = grey ? .35 : .6; g.setLineDash([3, 5]); g.lineWidth = 1.5; g.beginPath();
    const mid = thick / 2; if (vert) { g.moveTo(mid, 0); g.lineTo(mid, len); } else { g.moveTo(0, mid); g.lineTo(len, mid); } g.stroke(); g.restore();
    const arr = st.lanes[key], keep = [];
    st.hits[key === 'ab' ? 0 : 1] = [];
    for (const c of arr) {
      const p = (now - c.t0) / TRAVEL_MS; if (p > 1.05) continue; keep.push(c);
      if (p < 0) continue;
      const pos = (forward ? p : 1 - p) * (len - (vert ? ch : cw));
      const x = vert ? (thick - cw) / 2 : pos, y = vert ? pos : (thick - ch) / 2, w = vert ? cw : cw, h = ch;
      const missing = c.bad === 'missing', isBad = !!c.bad && !missing;
      g.save(); g.globalAlpha = grey && !c.bad ? .45 : 1;
      g.beginPath(); g.roundRect ? g.roundRect(x, y, w, h, 5) : g.rect(x, y, w, h);
      g.fillStyle = isBad ? 'rgba(220,60,60,.16)' : panel; g.fill(); g.lineWidth = 1.3; g.strokeStyle = isBad ? bad : line; if (missing) g.setLineDash([4, 3]); g.stroke(); g.setLineDash([]);
      if (!missing && c.bits != null) {
        const slot = (vert ? h : w) / 9;
        for (let i = 0; i < 8; i++) if ((c.bits >> i) & 1) { g.fillStyle = isBad ? bad : ok; if (vert) g.fillRect(x + 4, y + slot * (i + .6), w - 8, Math.max(2, slot * .45)); else g.fillRect(x + slot * (i + .6), y + 4, Math.max(2, slot * .45), h - 8); }
      }
      g.restore();
      st.hits[key === 'ab' ? 0 : 1].push({ x, y, w, h, c });
    }
    st.lanes[key] = keep;
    return keep.length > 0;
  }
  function frame(now) {
    st.raf = 0; if (!st.alive || document.hidden) return;
    const a = drawLane('ab', now), b = drawLane('ba', now);
    if (a || b || !st.data) st.raf = requestAnimationFrame(frame);
  }
  const kick = () => { if (!st.raf && st.alive && !document.hidden) st.raf = requestAnimationFrame(frame); };
  document.addEventListener('visibilitychange', () => { if (!document.hidden) kick(); });
  // tap / hover a chip: why a red one was rejected
  $$('.mbtrack', root).forEach((tr, idx) => {
    const tip = $('.mbtip', tr);
    const show = ev => { const r = tr.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top; const h = (st.hits[idx] || []).find(h => x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + h.h);
      if (h && (h.c.bad || h.c.bits != null)) { const bits = h.c.bits == null ? '' : ` bits ${[0, 1, 2, 3, 4, 5, 6, 7].map(i => (h.c.bits >> i) & 1).join('')}`; tip.textContent = h.c.bad ? `Rejected: ${REASON_TEXT[h.c.bad] || h.c.bad}${bits}` : `Good message${bits}`; tip.hidden = false; tip.style.left = Math.min(r.width - 150, Math.max(0, x - 40)) + 'px'; tip.style.top = Math.max(0, y - 34) + 'px'; } else tip.hidden = true; };
    tr.addEventListener('pointermove', show); tr.addEventListener('pointerdown', show); tr.addEventListener('pointerleave', () => { tip.hidden = true; });
  });

  // ------------------------------------------------------------ actions
  root.addEventListener('click', async ev => {
    const flip = ev.target.closest('[data-flip]');
    if (flip) {
      const on = flip.getAttribute('aria-pressed') === 'true';
      try { await api('/mb/tmb', { method: 'POST', body: { bit: +flip.dataset.flip, value: on ? 0 : 1, side: flip.dataset.side } }); flip.classList.toggle('on', !on); flip.setAttribute('aria-pressed', !on); } catch (e) { toast(e.message, true); }
      return;
    }
    if (ev.target.closest('#mbBreak')) {
      const brk = !!(st.data && st.data.local.impair && st.data.local.impair.break_link);
      try { await api('/mb/impair', { method: 'POST', body: { scope: 'both', cfg: { break_link: !brk } } }); } catch (e) { toast(e.message, true); }
      return;
    }
    const chip = ev.target.closest('[data-chip]');
    if (chip) {
      const def = CHIPS.find(x => x[0] === chip.dataset.chip), imp = (st.data && st.data.local.impair) || {}, on = def[4](imp);
      try { await api('/mb/impair', { method: 'POST', body: { scope: 'both', cfg: on ? def[3] : def[2] } }); } catch (e) { toast(e.message, true); }
      return;
    }
    const tab = ev.target.closest('#mbTabs [data-tab]'); if (tab) { st.tab = tab.dataset.tab; if (st.data) paintPane(st.data, true); return; }
    const csv = ev.target.closest('[data-csv]'); if (csv) { window.location = `/api/mb/events.csv?side=${csv.dataset.csv}`; return; }
    const sv = ev.target.closest('[data-save-settings]'); if (sv) saveSettings(sv.dataset.saveSettings);
  });

  // ------------------------------------------------------------ the panes below
  let paneSig = '';
  function paintPane(d, force) {
    $$('#mbTabs button', root).forEach(b => b.setAttribute('aria-selected', b.dataset.tab === st.tab));
    const pane = q('#mbPane');
    if (st.tab === 'settings' || st.tab === 'about') {           // forms must not be rebuilt under the user's fingers: only when the tab was just opened
      if (force || paneSig !== st.tab) { pane.innerHTML = st.tab === 'settings' ? settingsHtml(d) : aboutHtml(); paneSig = st.tab; }
      return;
    }
    paneSig = st.tab;
    pane.innerHTML = st.tab === 'numbers' ? numbersHtml(d) : st.tab === 'charts' ? chartsHtml() : eventsHtml(d);
    if (st.tab === 'charts') drawCharts(d);
  }
  function tile(l, v, sub, cls = '') { return `<div class="stat ${cls}"><small>${l}</small><b>${v}</b><span>${sub || ''}</span></div>`; }
  function numbersHtml(d) {
    const { a, b } = ends(d), cols = [a, b].filter(Boolean), near = cols.find(x => x.echo_role === 'near') || cols[0];
    const R = near.rtt || {}, c = near.channel || {};
    const rejRows = REJECT_ORDER.map(k => `<tr><td>${REASON_TEXT[k] ? esc(k) : k}</td>${cols.map(e => `<td>${n0((e.counters.rejects || {})[k])}</td>`).join('')}</tr>`).join('');
    const stat = (e, k) => e.channel[k];
    return `<div class="tiles t5" data-testid="mb-tiles">${tile('Bit transfer time (≈ RTT/2)', R.n ? f1(R.last / 2) + ' ms' : '–', R.n ? `p50 ${f1(R.p50 / 2)} · p99 ${f1(R.p99 / 2)} · max ${f1(R.max / 2)} ms` : 'waiting for the echo', R.alarm ? 'bad' : 'good')}
      ${tile('Round trip (echo bit)', R.n ? f1(R.last) + ' ms' : '–', R.n ? `jitter ${f1(R.jitter_ms)} ms · limit ${f1(R.rtt_max_ms)} ms` : '', R.alarm ? 'bad' : '')}
      ${tile('ROK availability', f1(c.availability_pct) + ' %', `unavailable ${f1(c.unavail_pct)} % · window ${f1(c.window_unavail_pct)} %`, c.unavail_pct > 0.1 ? 'warn' : 'good')}
      ${tile('Longest outage', f1(c.longest_outage_ms) + ' ms', `${n0(c.outage_count)} outage(s) · ${n0(c.rok_drops)} ROK drops`, c.longest_outage_ms ? 'warn' : 'good')}
      ${tile('RBAD / CBAD events', `${n0(c.rbad_events)} / ${n0(c.cbad_events)}`, `lost-channel ${n0(c.lost_events)}`, c.rbad_events || c.cbad_events ? 'bad' : 'good')}</div>
      <div class="scroll"><table class="mbtbl"><thead><tr><th>Messages</th>${cols.map(e => `<th>${esc(e.label || e.name)} (${esc(e.proto)})</th>`).join('')}</tr></thead><tbody>
        <tr><td>sent</td>${cols.map(e => `<td>${n0(e.counters.tx_msgs)}</td>`).join('')}</tr><tr><td>received good</td>${cols.map(e => `<td>${n0(e.counters.rx_good)}</td>`).join('')}</tr>
        <tr class="mbsub"><td colspan="${cols.length + 1}">rejected, by reason ${tip('Every message is checked: characters (parity / framing / overrun), the 3 copies of each bit, the ID, and how late it came. A rejected or missing message puts ROK to 0 at once.')}</td></tr>${rejRows}
        <tr><td>ROK drops (1→0)</td>${cols.map(e => `<td>${n0(stat(e, 'rok_drops'))}</td>`).join('')}</tr>
        <tr><td>unavailability</td>${cols.map(e => `<td>${f1(stat(e, 'unavail_pct'))} %</td>`).join('')}</tr></tbody></table></div>
      ${cols.some(e => e.overrun_note) ? `<p class="muted">Overrun counting: ${esc(cols.find(e => e.overrun_note).overrun_note)}.</p>` : ''}`;
  }
  const chartsHtml = () => `<div class="mbcharts"><div class="card flat"><div class="row sp"><b>Round trip of the echo bit</b><span class="muted">ms, newest on the right · red line = RTT_MAX</span></div><canvas id="cRtt" height="170"></canvas></div>
    <div class="card flat"><div class="row sp"><b>Jitter of the round trip</b><span class="muted">change from one trip to the next, ms</span></div><canvas id="cJit" height="120"></canvas></div>
    <div class="card flat"><div class="row sp"><b>Gap between good messages</b><span class="muted">how bunched the radio delivers them (histogram, both ends)</span></div><canvas id="cHist" height="170"></canvas></div></div>`;
  function line(cv, xs, ys, color, ref) {
    const W = cv.clientWidth || 600, H = +cv.getAttribute('height'), dpr = Math.min(2, window.devicePixelRatio || 1); cv.width = W * dpr; cv.height = H * dpr;
    const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H); g.font = '11px system-ui'; g.fillStyle = css('--muted'); g.strokeStyle = css('--line');
    const mx = Math.max(1, ...ys, ref || 0) * 1.15; g.beginPath(); g.moveTo(34, 4); g.lineTo(34, H - 14); g.lineTo(W - 4, H - 14); g.stroke();
    g.fillText(f1(mx), 2, 12); g.fillText('0', 22, H - 16);
    if (!ys.length) { g.fillText('waiting for data…', 50, H / 2); return; }
    const px = i => 34 + (W - 40) * (i / Math.max(1, ys.length - 1)), py = v => H - 14 - (H - 20) * v / mx;
    if (ref) { g.strokeStyle = css('--fail'); g.setLineDash([6, 4]); g.beginPath(); g.moveTo(34, py(ref)); g.lineTo(W - 4, py(ref)); g.stroke(); g.setLineDash([]); }
    g.strokeStyle = color; g.lineWidth = 1.6; g.beginPath(); ys.forEach((v, i) => i ? g.lineTo(px(i), py(v)) : g.moveTo(px(i), py(v))); g.stroke();
  }
  function drawCharts(d) {
    const { a, b } = ends(d), near = [a, b].find(x => x && x.echo_role === 'near') || d.local, ch = (d.local.rtt_chart || []);
    const src = near === d.local ? ch : [];
    const ys = src.map(r => r[1]);
    line($('#cRtt', root), [], ys, css('--accent'), (near.rtt || {}).rtt_max_ms);
    line($('#cJit', root), [], ys.slice(1).map((v, i) => Math.abs(v - ys[i])), css('--warn'));
    const cv = $('#cHist', root), W = cv.clientWidth || 600, H = 170, dpr = Math.min(2, window.devicePixelRatio || 1); cv.width = W * dpr; cv.height = H * dpr;
    const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.font = '10px system-ui'; g.fillStyle = css('--muted');
    const hs = [a, b].filter(Boolean).map(e => e.histogram), mx = Math.max(1, ...hs.flatMap(h => h.counts));
    const labels = (hs[0] || { labels: [] }).labels, bw = (W - 20) / Math.max(1, labels.length);
    hs.forEach((h, k) => h.counts.forEach((c, i) => { const hh = (H - 34) * Math.log10(1 + c) / Math.log10(1 + mx); g.fillStyle = k ? css('--warn') : css('--accent'); g.fillRect(10 + i * bw + k * bw / 2.2, H - 20 - hh, bw / 2.4, hh); }));
    g.fillStyle = css('--muted'); labels.forEach((l, i) => { if (i % 2 === 0) g.fillText(l, 10 + i * bw, H - 6); });
    g.fillText('ms between good messages (log scale) · teal = ' + ((a && a.label) || 'A') + ', amber = ' + ((b && b.label) || 'B'), 10, 11);
  }
  function eventsHtml(d) {
    const rows = [...(d.local.events || [])].reverse().map(e => `<tr><td>${e.t_s.toFixed(3)} s</td><td>${esc(e.name)}</td><td>${esc(e.value)}</td><td class="muted">${esc(e.note || '')}</td></tr>`).join('');
    return `<div class="row sp"><div><b>Event log</b> <span class="muted">every TMB, RMB, ROK, RBAD, CBAD change with its time (ms), like a relay's SER. The echo bit is counted, not logged.</span></div>
      <div class="row"><button class="btn small" data-csv="local" data-testid="mb-csv">⬇ CSV, this Pi</button>${d.remote ? '<button class="btn small" data-csv="remote">⬇ CSV, other Pi</button>' : ''}</div></div>
      <div class="scroll" style="max-height:340px"><table class="mbtbl" data-testid="mb-events"><thead><tr><th>Time</th><th>Element</th><th>Value</th><th>Note</th></tr></thead><tbody>${rows || '<tr><td colspan="4" class="muted">Nothing yet.</td></tr>'}</tbody></table></div>`;
  }
  function settingsHtml(d) {
    const { a, b } = ends(d), e = d.local, S = e.settings, im = e.impair || {}, nm = k => (S.labels && S.labels[k]) || { rok_n: 'ROK after N good', rbad_pu_s: 'RBAD_PU', cbad_pu_pct: 'CBAD_PU', cbad_window_s: 'CBAD window', k_timing: 'K (timing)', rtt_max_ms: 'RTT_MAX' }[k];
    const num = (k, label, hint, extra = '') => `<label class="f"><span>${esc(label)}${k === 'rok_n' ? ' <span class="pill warn">ASSUMPTION</span>' : ''}</span><input type="number" step="any" data-set="${k}" value="${S[k]}" ${extra}><small class="muted">${hint}</small></label>`;
    const bit = (k, i) => `<input type="number" min="${k === 'rmb_default' ? 0 : 1}" max="${k === 'rmb_default' ? 1 : 999}" data-bit="${k}:${i}" value="${S[k][i]}" aria-label="${k} bit ${i + 1}">`;
    const imp = (k, label, hint) => `<label class="f"><span>${label}</span><input type="number" step="any" min="0" data-imp="${k}" value="${im[k] || 0}"><small class="muted">${hint}</small></label>`;
    return `<div class="mbsets"><h3>Channel settings of ${esc(e.label || e.name)} <small class="muted">(this end; they apply at once)</small></h3><div class="grid g3">
      ${num('rok_n', nm('rok_n'), 'ROK comes back after this many good messages in a row. Our assumption (default 2): check it against a real device.')}
      ${num('rbad_pu_s', nm('rbad_pu_s') + ' (s)', 'RBAD asserts when ROK has been 0 this long.')}
      ${num('cbad_pu_pct', nm('cbad_pu_pct') + ' (%)', 'CBAD asserts when ROK=0 for more than this share of the window.')}
      ${num('cbad_window_s', nm('cbad_window_s') + ' (s)', 'The rolling window for CBAD.')}
      ${num('k_timing', nm('k_timing'), 'A message later than K × the nominal message time is rejected (timing).')}
      ${num('rtt_max_ms', nm('rtt_max_ms') + ' (ms)', 'The echo alarm fires when a round trip takes longer than this.')}</div>
      <h3>Each received bit</h3><p class="muted">PU = good messages in a row before RMBn goes 0→1; DO = before it goes 1→0; Default = where it sits while ROK=0.</p>
      <div class="scroll"><table class="mbtbl mbbits"><thead><tr><th></th>${[1, 2, 3, 4, 5, 6, 7, 8].map(i => `<th>${i}</th>`).join('')}</tr></thead><tbody>
        ${[['rmb_pu', 'PU'], ['rmb_do', 'DO'], ['rmb_default', 'Default']].map(([k, l]) => `<tr><td>${l}</td>${[0, 1, 2, 3, 4, 5, 6, 7].map(i => `<td>${bit(k, i)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
      <div class="row" style="margin:10px 0"><button class="btn primary" data-save-settings="local" data-testid="mb-save-settings">Apply to ${esc(e.label || e.name)}</button></div>
      <h3>Impairment injector <small class="muted">between the codec and the link; works on serial and IP; separate from the Linux bad-link simulator</small></h3><div class="grid g3">
        ${imp('latency_ms', 'Fixed latency (ms)', 'Every message is held this long.')}${imp('jitter_ms', '± jitter (ms)', 'Random extra delay, never reordering.')}${imp('drop_pct', 'Random drop (%)', 'Share of messages thrown away.')}
        ${imp('burst_len_ms', 'Burst outage length (ms)', 'Everything is dropped for this long ...')}${imp('burst_every_s', '... every (s)', 'How often the burst repeats (0 = off).')}${imp('bit_error_pct', 'Bit errors (%)', 'Share of messages with one flipped bit.')}</div>
      <div class="row" style="margin-top:10px"><select id="mbImpScope"><option value="both">both directions</option><option value="local">${esc(e.label || e.name)} sending only</option>${d.remote ? `<option value="remote">${esc(d.remote.label || d.remote.name)} sending only</option>` : ''}</select>
        <button class="btn" data-save-settings="imp" data-testid="mb-apply-impair">Apply impairment</button><button class="btn" data-save-settings="imp0">Clear all</button></div></div>`;
  }
  async function saveSettings(what) {
    try {
      if (what === 'local') {
        const s = {}; $$('[data-set]', root).forEach(i => { if (i.value !== '') s[i.dataset.set] = +i.value; });
        for (const k of ['rmb_pu', 'rmb_do', 'rmb_default']) s[k] = $$(`[data-bit^="${k}:"]`, root).map(i => +i.value);
        await api('/mb/settings', { method: 'POST', body: { settings: s } }); toast('Settings applied');
      } else {
        const cfg = {}; $$('[data-imp]', root).forEach(i => { cfg[i.dataset.imp] = what === 'imp0' ? 0 : +i.value || 0; });
        if (what === 'imp0') cfg.break_link = false;
        await api('/mb/impair', { method: 'POST', body: { scope: what === 'imp0' ? 'both' : $('#mbImpScope', root).value, cfg } }); toast(what === 'imp0' ? 'Impairment cleared' : 'Impairment applied');
        paneSig = '';
      }
    } catch (e) { toast(e.message, true, 7000); }
  }
  const aboutHtml = () => `<div class="mbabout" data-testid="mb-about"><h3>What you are looking at</h3>
    <p>Two Mirrored Bits units, one at each end of the link (like a pair of SEL-2505s). Each end has <b>8 transmit bits (TMB1–8)</b> and <b>8 receive bits (RMB1–8)</b>. Whatever you flip at one end shows up at the other end, over and over, whether or not anything changed.</p>
    <ul><li><b>Messages</b> are sent nonstop, back to back, in both directions. Each carries all 8 bits three times, plus an ID.</li>
    <li><b>ROK</b> (receive OK) is 1 while good messages keep arriving. A bad, late or missing message drops it to 0 at once, and the received bits fall back to their defaults. It comes back after a few good messages in a row (the “N” setting, our assumption).</li>
    <li><b>RBAD</b> asserts when ROK has been 0 for a while (RBAD_PU). <b>CBAD</b> asserts when ROK was 0 too much of a rolling window (CBAD_PU). <b>LBOK</b> is the loopback test passing.</li>
    <li><b>Echo bit</b> (default TMB8): one end copies it back, the other flips it each time it returns. The time per flip is the round trip; one way ≈ half. This catches a steady delay that ROK alone cannot see.</li>
    <li><b>Trip frame format</b> (the DER transfer-trip test): each message is a 19-byte frame with a frame number, a send time, the echo of the last frame heard and a CRC. Every frame gives a round trip with no clock sync, lost frames are counted exactly from the numbers, damaged ones by their CRC, and TMB8 is free. One-way needs GPS time (not built in): until then one-way = round trip / 2.</li></ul>
    <p>The question it answers: <b>does Mirrored Bits survive this link?</b> Break the link, add delay, jitter, drops or bit errors with the chips, and watch what a real unit pair would do.</p>
    <p class="muted"><b>SimCodec</b> is our own message format for emulator-to-emulator tests. It is <b>not</b> the SEL wire format: it will not talk to a real SEL relay. The real layout can be added later as another codec without changing anything else.</p></div>`;

  st.timer = setTimeout(poll, 0);
  return () => { st.alive = false; clearTimeout(st.timer); if (st.raf) cancelAnimationFrame(st.raf); };
}
