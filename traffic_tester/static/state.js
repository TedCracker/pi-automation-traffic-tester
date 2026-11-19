// Shared app state + live event bus.
export const S = {
  status: null, profiles: [], sel: null, wizard: { step: 1, overrides: {}, port: 'auto', checks: null },
  live: { running: false, state: null, xs: [], rtt: [], lost: [], thr: 60, txBits: [], rxBits: [], link: 'idle', lastEvent: '', done: null, runId: null, runIds: [], profile: null, info: null, legs: [], legsFor: '', legStates: {} },
  ws: null, wsOk: false,
  gen: { xs: [], tx: [], rx: [], lat: [], jit: [], sr: {}, sl: {}, last: null },
  sniff: { xs: [], pps: [], mbps: [], rows: [], last: null, serial: false },
};
// a test set = a generator test with `gen.testset.enabled` (applications with targets, run alone / together / plus fill)
export const isTestset = p => !!(p && p.kind === 'generator' && p.gen && p.gen.testset && p.gen.testset.enabled);
const subs = new Set();
export const onLive = f => { subs.add(f); return () => subs.delete(f); };
export const emitLive = e => subs.forEach(f => f(e));

export function resetLive(profile, runId, runIds) {
  S.mbA = null; S.mbB = null;
  S.gen = { xs: [], tx: [], rx: [], lat: [], jit: [], sr: {}, sl: {}, last: null };
  S.sniff = { xs: [], pps: [], mbps: [], rows: [], last: null, serial: false };
  Object.assign(S.live, { running: true, state: null, xs: [], rtt: [], lost: [], thr: profile ? profile.threshold_ms : 60, txBits: [], rxBits: [], link: 'ok', lastEvent: 'Test starting…', done: null, runId, runIds: runIds && runIds.length ? runIds : [runId], profile, legs: [], legsFor: '', legStates: {}, marks: [], lamp: null, sinceView: true, pickLeg: null });
  if (typeof document !== 'undefined') document.dispatchEvent(new Event('tt-run-started'));
}

export function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
  S.ws = ws;
  ws.onopen = () => { S.wsOk = true; emitLive({ ev: '_conn', ok: true }); };
  ws.onclose = () => { S.wsOk = false; emitLive({ ev: '_conn', ok: false }); setTimeout(connectWS, 1500); };
  ws.onmessage = m => {
    const d = JSON.parse(m.data);
    for (const e of d.events || []) handle(e);
    emitLive({ ev: '_batch' });
  };
}

// Pull the current run from the server (used when the live connection is down, and for the manual Refresh): the newest window of messages + the latest numbers.
// Who sends and who answers in each direction of the running (or just finished) test, by name: from the stored run rows ("pi1 → pi2").
export async function loadLegs() {
  const { api } = await import('./ui.js');
  const L = S.live; if (!L.runId) return [];
  const run = S.status && S.status.run;
  const ids = run && run.running && run.legs ? run.legs.map(l => l.run_id) : (L.runIds && L.runIds.length ? L.runIds : [L.runId]);
  if (L.legsFor === ids.join()) return L.legs;
  const legs = [];
  for (const [i, id] of ids.entries()) {
    const r = await api(`/runs/${id}`).catch(() => null), l = (r && r.leg) || {};
    legs.push({ index: i, run_id: id, label: l.label || '', tester: l.tester || '', reflector: l.reflector || '', kind: l.kind || 'round_trip', basis: l.basis || 'rtt' });
  }
  L.legs = legs; L.legsFor = ids.join();
  return legs;
}

export async function pullLive() {
  const { api } = await import('./ui.js');
  const st = await api('/status'); S.status = st;
  const L = S.live;
  if (st.run.running && st.run.kind !== 'generator' && st.run.kind !== 'sniffer') {
    if (!L.running || L.runId !== st.run.run_id) { L.done = null; L.runId = st.run.run_id; L.xs = []; L.rtt = []; L.lost = []; L.txBits = []; L.rxBits = []; }
    L.running = true; L.thr = st.run.threshold_ms || L.thr || 60;
    if (st.run.legs) for (const l of st.run.legs) if (l.index > 0 && l.state) L.legStates[l.index] = l.state;
    const r = await api(`/runs/${st.run.run_id}/live`);
    L.xs = []; L.rtt = []; L.lost = []; L.txBits = []; L.rxBits = [];
    for (const e of r.recent) { L.xs.push(e.t_s); L.rtt.push(e.status === 'lost' ? null : e.rtt_ms); L.lost.push(e.status === 'lost' ? 1 : null); if (e.bits_rx != null) L.rxBits.push(e.bits_rx); L.txBits.push(e.bits_tx); }
    if (r.state) { L.state = r.state; L.link = r.state.link === 'red' ? 'lost' : 'ok'; }
    if (r.marks) L.marks = r.marks;
    if (r.lamp) L.lamp = r.lamp;
    if (r.mb) S.mbA = r.mb;
    if (r.mbpeer) S.mbB = r.mbpeer;
  } else if (L.running && !st.run.running && L.runId) {            // it finished while we were not being told: show the verdict like the live event would
    const r = await api(`/runs/${L.runId}`);
    handle({ ev: 'done', status: r.status, error: (r.summary || {}).error || null, summary: r.summary });
  }
  emitLive({ ev: '_batch' });
}

function handle(e) {
  const L = S.live;
  switch (e.ev) {
    case 'started': L.running = true; L.link = 'ok'; L.info = e; L.lastEvent = `Connected to ${(e.peer && e.peer.name) || 'the other Pi'}` + (e.one_way ? ' · one-way timing on' : ''); break;
    case 'legstate': L.legStates[e.leg] = e; L.running = true; break;           // another direction of a two-way test: only its headline numbers
    case 'tx': L.txBits.push(e.bits); if (L.txBits.length > 400) L.txBits.shift(); break;
    case 'msg': {
      L.xs.push(e.t_s);
      L.rtt.push(e.status === 'lost' ? null : e.rtt_ms);
      L.lost.push(e.status === 'lost' ? 1 : null);
      if (e.bits_rx != null) { L.rxBits.push(e.bits_rx); if (L.rxBits.length > 400) L.rxBits.shift(); }
      if (L.xs.length > 3000) { L.xs.shift(); L.rtt.shift(); L.lost.shift(); }
      if (e.status === 'lost') { L.link = 'lost'; L.lastEvent = `Message #${e.seq} was lost`; }
      else if (e.status === 'late') { L.link = 'late'; L.lastEvent = `Message #${e.seq} was late: ${e.rtt_ms} ms`; }
      else if (L.link !== 'down') { L.link = 'ok'; L.lastEvent = `Message #${e.seq} · ${e.rtt_ms} ms`; }
      break;
    }
    case 'state': L.state = e; L.running = true; if (e.phase === 'idle') { L.link = 'idle'; } break;
    case 'idle_wait': L.link = 'idle'; L.lastEvent = `Quiet for ${e.gap_s} s, then one message to see how fast the link wakes up`; break;
    case 'link': L.link = e.state === 'down' ? 'down' : 'ok'; L.lastEvent = e.state === 'down' ? 'TCP session dropped — reconnecting' : `TCP session re-established (${e.connect_ms} ms)`; break;
    case 'stall': L.lastEvent = 'TCP session stalled — reconnecting'; L.link = 'down'; break;
    case 'gstate': {
      const g = S.gen; g.last = e; g.xs.push(e.elapsed_s); g.tx.push(e.tx_mbps); g.rx.push(e.rx_mbps); g.lat.push(e.lat_ms ?? null); g.jit.push(e.jitter_ms ?? null);
      for (const x of (e.streams_now || [])) { (g.sr[x.i] ||= []).push(x.on ? x.rx_mbps : null); (g.sl[x.i] ||= []).push(x.on ? (x.lat_ms ?? null) : null); }
      if (g.xs.length > 3000) { for (const k of ['xs', 'tx', 'rx', 'lat', 'jit']) g[k].shift(); for (const k in g.sr) { g.sr[k].shift(); g.sl[k].shift(); } }
      L.running = true; L.link = e.lost ? 'lost' : 'ok'; break;
    }
    case 'sstate': { const g = S.sniff; g.last = e; g.xs.push(e.elapsed_s); g.pps.push(e.pps); g.mbps.push(e.mbps); if (g.xs.length > 3000) { for (const k of ['xs', 'pps', 'mbps']) g[k].shift(); } L.running = true; break; }
    case 'spkt': case 'sserial': { const g = S.sniff; g.serial = e.ev === 'sserial'; g.rows.push(...e.rows); if (g.rows.length > 600) g.rows.splice(0, g.rows.length - 600); break; }
    case 'info': L.lastEvent = e.text; break;
    case 'done': if (S.status) S.status.run.running = false; L.running = false; L.done = e; L.link = 'idle'; L.lastEvent = e.status === 'failed' ? (e.error || 'Test failed') : 'Test finished'; break;
    case 'mark': (S.live.marks ||= []).push({ n: e.n, t_s: e.t_s, name: e.name, at: e.at }); S.live.lastEvent = `⚑ Mark ${e.n}${e.name ? ' · ' + e.name : ''}`; break;
    case 'lamp': S.live.lamp = e; break;
    case 'mb': S.mbA = e; break;
    case 'mbpeer': S.mbB = e; break;
  }
  emitLive(e);
}
