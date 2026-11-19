// The lamp strip at the top of every screen: PEER · CLK · ETH · SER · PI · CAP. Grey = nothing to judge, green = fine, amber = look, red = broken.
// A lamp is a button: it opens one line that says what it means and, when there is one, the button that fixes or opens the place to fix it.
// Fed by what the app already knows (/api/status, /api/peers, /api/testports, the tester lamp of the running test); polled every 15 s and on every live event.
import { $, $$, esc, api } from './ui.js';
import { S, onLive } from './state.js';

let lastHtml = '', host = null, data = { peers: [], ports: [] }, timer = 0, openKey = null;

// -> [{key, name, level: off|ok|warn|bad, value, text, fix?: {label, hash}}]
export function lampList() {
  const st = S.status || {}, run = st.run || {}, L = S.live;
  const out = [];
  // PEER: the other Pi answers
  const peers = data.peers, up = peers.filter(p => p.reachable);
  out.push(!peers.length ? { key: 'peer', name: 'PEER', level: 'off', value: 'none', text: 'No other Pi is known yet. Find it by address in Setup.', fix: { label: 'Find the other Pi', hash: 'config/pis' } }
    : up.length ? { key: 'peer', name: 'PEER', level: 'ok', value: up.map(p => p.name).join(', '), text: `${up.map(p => p.name).join(', ')} ${up.length > 1 ? 'answer' : 'answers'}.` }
    : { key: 'peer', name: 'PEER', level: 'bad', value: peers[0].name, text: `${peers[0].name} does not answer. Check its power and the cable, or its address in Setup.`, fix: { label: 'Open Pis in Setup', hash: 'config/pis' } });
  // CLK: the clocks agree (one-way times are only valid then)
  const c = st.clock || {};
  out.push(c.synced ? { key: 'clk', name: 'CLK', level: 'ok', value: c.offset_ms != null ? `±${Math.abs(c.offset_ms).toFixed(1)} ms` : 'synced', text: 'The clock is in sync, so one-way times are valid.' }
    : { key: 'clk', name: 'CLK', level: 'warn', value: 'not synced', text: 'The clock is not in sync with the other Pi, so one-way times are not valid (round trip still is).', fix: { label: 'Open Time in Setup', hash: 'config/time' } });
  // ETH / SER: the test ports
  const ports = data.ports, eth = ports.filter(p => p.kind === 'eth' || p.kind === 'usb_eth'), ethUp = eth.filter(p => p.link), ser = ports.filter(p => p.kind === 'serial');
  out.push(!eth.length ? { key: 'eth', name: 'ETH', level: 'off', value: 'none', text: 'No Ethernet port found.' }
    : ethUp.length ? { key: 'eth', name: 'ETH', level: 'ok', value: ethUp.map(p => p.name + (p.speed_mbps ? ' ' + (p.speed_mbps >= 1000 ? p.speed_mbps / 1000 + 'G' : p.speed_mbps + 'M') : '')).join(', '), text: ethUp.map(p => `${p.name}: ${p.state}`).join(' · ') }
    : { key: 'eth', name: 'ETH', level: 'warn', value: 'no link', text: 'An Ethernet port is present but no cable is up. Check the cable.' });
  out.push(!ser.length ? { key: 'ser', name: 'SER', level: 'off', value: 'none', text: 'No serial adapter found. A serial trip test needs one.' }
    : { key: 'ser', name: 'SER', level: ser.some(p => p.adapter_slow) ? 'warn' : 'ok', value: ser.map(p => p.name).join(', '), text: ser.map(p => `${p.name}: ${p.state}`).join(' · ') });
  // PI: is the tester itself the limit? (the tester lamp of a running test)
  const lp = L.running ? L.lamp : null;
  out.push(lp && lp.level === 'limit' ? { key: 'pi', name: 'PI', level: 'bad', value: 'limit', text: `${lp.title || 'The Pi is the limit.'} ${lp.text || ''}`.trim() }
    : lp && lp.level === 'warn' ? { key: 'pi', name: 'PI', level: 'warn', value: 'check', text: `${lp.title || 'Check the Pi.'} ${lp.text || ''}`.trim() }
    : { key: 'pi', name: 'PI', level: 'ok', value: 'pi ok', text: 'This Pi keeps up (sending rate, processor, receive drops).' });
  // CAP: a capture is recording
  const cap = !!(run.running && run.kind === 'sniffer');
  out.push(cap ? { key: 'cap', name: 'CAP', level: 'bad', value: 'recording', text: 'A capture is recording.', fix: { label: 'Open the Sniffer', hash: 'sniffer' } } : { key: 'cap', name: 'CAP', level: 'off', value: 'off', text: 'No capture is recording.' });
  return out;
}

export function drawLamps() {
  if (!host) return;
  const ls = lampList();
  const html = ls.map(l => `<button type="button" class="lp ${l.level}${openKey === l.key ? ' open' : ''}" data-lamp="${l.key}" data-testid="lamp-${l.key}" aria-label="${l.name} ${esc(l.value)}: ${esc(l.text)}" aria-expanded="${openKey === l.key}"><i></i><b>${l.name}</b><span>${esc(l.value)}</span></button>`).join('');
  if (html === lastHtml) return drawNote();
  lastHtml = html; host.innerHTML = html;
  $$('button[data-lamp]', host).forEach(b => b.onclick = ev => { ev.stopPropagation(); openKey = openKey === b.dataset.lamp ? null : b.dataset.lamp; drawLamps(); drawNote(); });
  drawNote();
}

function drawNote() {
  let n = $('#lampNote');
  if (!openKey) { if (n) n.remove(); return; }
  const l = lampList().find(x => x.key === openKey); if (!l) return;
  if (!n) { n = document.createElement('div'); n.id = 'lampNote'; n.className = 'lamp-note'; n.setAttribute('role', 'status'); n.dataset.testid = 'lamp-note'; host.closest('header').appendChild(n); }
  n.className = 'lamp-note ' + l.level;
  n.innerHTML = `<i class="lp-dot ${l.level}"></i><span><b>${l.name}</b> ${esc(l.text)}</span>${l.fix ? `<a class="btn small" href="#${l.fix.hash}" data-testid="lamp-fix">${esc(l.fix.label)}</a>` : ''}<button type="button" class="x" aria-label="Close" data-testid="lamp-close">✕</button>`;
  $('.x', n).onclick = () => { openKey = null; drawLamps(); };
  const a = $('a', n); if (a) a.onclick = () => { openKey = null; setTimeout(drawLamps, 0); };
}

export async function pollLamps() {
  try {
    const [p, t] = await Promise.all([api('/peers').catch(() => null), api('/testports').catch(() => null)]);
    if (p) data.peers = p.peers || []; if (t) data.ports = t.ports || [];
    const st = await api('/status').catch(() => null); if (st && !st.login_required) S.status = st;
  } catch { }
  drawLamps();
}

export function initLamps() {
  host = $('#lamps'); if (!host) return;
  drawLamps(); pollLamps();
  clearInterval(timer); timer = setInterval(pollLamps, 15000);
  onLive(e => { if (['started', 'done', 'lamp', 'state'].includes(e.ev)) drawLamps(); if (e.ev === 'started' || e.ev === 'done') setTimeout(pollLamps, 800); });
  document.addEventListener('click', e => { if (openKey && !e.target.closest('#lamps, #lampNote')) { openKey = null; drawLamps(); } });
  document.addEventListener('tt-pi-renamed', () => pollLamps());
}
