import { $, esc, api, tip, ms, lamps } from './ui.js';
import { markPlugin } from './results.js';
import { S, onLive } from './state.js';
import { mountMB } from './mblive.js';

let off = null, plot = null, raf = 0, bitCtx = null, mirrored = false, mbOff = null;

export function renderMonitor(root, opts = {}) {
  mirrored = !!opts.mirrored;
  if (off) off(); if (plot) { plot.destroy(); plot = null; } if (mbOff) { mbOff(); mbOff = null; }
  if (mirrored) { off = null; root.innerHTML = '<div id="mbHost"></div>'; mbOff = mountMB($('#mbHost')); return; }      // Mirrored Bits: the live emulator view replaces the delay-chart screen
  root.innerHTML = `
    <div class="card"><div class="row sp"><h2>Link</h2><span class="muted" id="lastEv"></span></div>
      <div class="link"><div class="node" id="nodeA"><b>Sender</b><small class="muted">sends</small></div>
        <div class="wire" id="wire"><i></i><i class="rev"></i></div>
        <div class="node" id="nodeLink"><b>Link</b><small class="muted" id="linkTxt">idle</small></div>
        <div class="wire" id="wire2"><i></i><i class="rev"></i></div>
        <div class="node" id="nodeB"><b>Other end</b><small class="muted">answers</small></div></div>
      <div class="wire-label">Green = on time · amber = late · red = lost or session down</div></div>
    <div class="card"><div class="row sp"><h2>Delay of each message ${tip('rtt')}</h2><span class="muted">dashed red line = your limit</span></div><div id="chart" class="chart"></div></div>
    <div class="card"><div class="row sp"><h2>Message bits ${tip('Each message carries 8 on/off bits (like Mirrored Bits). Top block: sent. Bottom block: coming back.')}</h2><span class="muted">newest on the right</span></div>
      <canvas id="bits" class="bits" width="1100" height="190"></canvas></div>`;
  buildChart();
  bitCtx = $('#bits').getContext('2d');
  off = onLive(e => { if (e.ev === '_batch' || e.ev === '_conn') schedule(); });
  restore(); draw();
}

async function restore() {   // page opened mid-run: pull the recent window from the server
  const L = S.live;
  if (L.xs.length || !S.status.run.running) return;
  try { const r = await api(`/runs/${S.status.run.run_id}/live`); L.running = true; L.runId = S.status.run.run_id; L.thr = S.status.run.threshold_ms || L.thr || 60;
    for (const e of r.recent) { L.xs.push(e.t_s); L.rtt.push(e.status === 'lost' ? null : e.rtt_ms); L.lost.push(e.status === 'lost' ? 1 : null); if (e.bits_rx != null) L.rxBits.push(e.bits_rx); L.txBits.push(e.bits_tx); }
    schedule(); } catch { }
}

const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function buildChart() {
  const el = $('#chart'); if (!el || !window.uPlot) return;
  const w = Math.max(200, el.clientWidth);
  const axis = { stroke: css('--muted'), grid: { stroke: css('--line'), width: 1 }, ticks: { stroke: css('--line') } };
  plot = new uPlot({
    plugins: [markPlugin(() => S.live.marks)], width: w, height: 280, padding: [8, 8, 0, 0], cursor: { drag: { x: false, y: false } }, legend: { live: false },
    scales: { x: { time: false }, y: { range: (u, mn, mx) => [0, Math.max(S.live.thr * 1.6, (mx || 0) * 1.15)] } },
    axes: [{ ...axis, label: 'seconds since start' }, { ...axis, label: 'ms', size: 56 }],
    series: [{ label: 'Time' }, { label: 'Delay', stroke: css('--accent'), width: 1.6, points: { show: false }, spanGaps: false },
      { label: 'Limit', stroke: css('--fail'), width: 2, dash: [8, 6], points: { show: false } },
      { label: 'Lost', stroke: css('--fail'), width: 0, points: { show: true, size: 9, fill: css('--fail'), stroke: css('--fail') } }],
  }, [[], [], [], []], el);
  new ResizeObserver(() => { if (plot) plot.setSize({ width: Math.max(200, el.clientWidth), height: 280 }); }).observe(el);
}

const schedule = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; draw(); }); };

function draw() {
  const L = S.live; if (!$('#wire')) return;
  if (plot) {
    const thr = L.xs.map(() => L.thr), lostY = L.lost.map(v => v ? L.thr * 1.5 : null);
    plot.setData([L.xs, L.rtt, thr, lostY]);
  }
  const cls = { ok: '', late: 'late', lost: 'lost', down: 'down', idle: 'idle' }[L.link] || '';
  for (const id of ['wire', 'wire2']) $('#' + id).className = 'wire ' + cls;
  const nc = L.link === 'lost' || L.link === 'down' ? 'lost' : L.link === 'late' ? 'late' : '';
  $('#nodeLink').className = 'node ' + nc;
  $('#linkTxt').textContent = { ok: 'healthy', late: 'late message', lost: 'message lost', down: 'session down', idle: L.running ? 'quiet' : 'idle' }[L.link];
  $('#lastEv').textContent = L.lastEvent;
  drawBits();
}

function drawBits() {
  const c = $('#bits'), g = bitCtx; if (!c) return;
  const W = c.width, H = c.height, cols = 100, cw = (W - 60) / cols, rows = 8, gap = 14;
  const blockH = (H - gap - 16) / 2, rh = blockH / rows;
  g.clearRect(0, 0, W, H); g.font = '11px system-ui'; g.textBaseline = 'middle';
  const on = css('--accent'), off_ = css('--line'), txt = css('--muted');
  const blocks = [[S.live.txBits, 'sent', 4], [S.live.rxBits, 'back', 4 + blockH + gap]];
  for (const [arr, label, y0] of blocks) {
    g.fillStyle = txt; g.fillText(label, 6, y0 + blockH / 2);
    const data = arr.slice(-cols);
    for (let r = 0; r < rows; r++) for (let i = 0; i < cols; i++) {
      const idx = data.length - cols + i; const v = idx >= 0 ? data[idx] : null;
      g.fillStyle = v != null && (v >> r) & 1 ? on : off_;
      g.fillRect(50 + i * cw, y0 + r * rh + 1, Math.max(1, cw - 1.5), Math.max(1, rh - 2));
    }
  }
}
