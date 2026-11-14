// Run and Results keep themselves current. Normally the live connection (/ws/live) pushes every change; when it is down (a proxy that blocks
// WebSockets, a flaky Wi-Fi link) the screen POLLS instead: every 2 s while a test is running, every 10 s otherwise. A small chip says which
// mode is active, with a manual Refresh button and an on/off switch that is remembered in this browser.
import { $, $$, esc } from './ui.js';
import { S, onLive } from './state.js';

const KEY = 'tt-autorefresh';
export const autoOn = () => { try { return localStorage.getItem(KEY) !== 'off'; } catch { return true; } };
export function setAuto(v) { try { localStorage.setItem(KEY, v ? 'on' : 'off'); } catch { } paint(); if (v) tick(true); }
const running = () => !!(S.live.running || (S.status && S.status.run && S.status.run.running));
export const pollSeconds = () => running() ? 2 : 10;

let refreshFn = null, last = 0, busy = false, lastOk = 0;
export function setRefreshTarget(fn) { refreshFn = fn; last = fn && !S.wsOk ? 0 : Date.now(); }      // a screen opened while the live connection is down is brought up to date at once

export function chipText() {
  if (!autoOn()) return { t: 'Paused', c: 'paused', title: 'Auto-refresh is off: use Refresh to update this screen.' };
  if (S.wsOk) return { t: '● Live', c: 'live', title: 'Updates arrive by themselves over the live connection.' };
  return { t: `↻ Updating every ${pollSeconds()} s`, c: 'poll', title: 'The live connection is down, so this screen asks the Pi for news by itself.' + (lastOk ? ` Last update ${new Date(lastOk).toLocaleTimeString()}.` : '') };
}
export const barHtml = () => {
  const c = chipText();
  return `<div class="refbar"><span class="refchip ${c.c}" data-testid="refresh-chip" role="status" title="${esc(c.title)}">${esc(c.t)}</span>
  <button type="button" class="btn small" data-refresh="now" data-testid="refresh-now" title="Update this screen now">↻ Refresh</button>
  <label class="chk refauto"><input type="checkbox" data-refresh="auto" data-testid="refresh-auto" ${autoOn() ? 'checked' : ''}> Auto-refresh</label></div>`;
};

export function paint() {
  const c = chipText();
  $$('.refchip').forEach(e => { e.textContent = c.t; e.className = 'refchip ' + c.c; e.title = c.title; });
  $$('input[data-refresh=auto]').forEach(i => { i.checked = autoOn(); });
}

// Wire the bar that barHtml() put on the screen (call once per render).
export function bindBar(root) {
  $$('[data-refresh=now]', root).forEach(b => b.onclick = async () => { b.disabled = true; try { await tick(true); } finally { b.disabled = false; } });
  $$('input[data-refresh=auto]', root).forEach(i => i.onchange = () => setAuto(i.checked));
}

async function tick(force) {
  if (!refreshFn || busy) return;
  if (!force && (!autoOn() || S.wsOk || Date.now() - last < pollSeconds() * 1000)) return;
  busy = true;
  try { await refreshFn(); lastOk = Date.now(); } catch { } finally { busy = false; last = Date.now(); paint(); }
}
setInterval(() => tick(false), 1000);
onLive(e => { if (e.ev === '_conn') { paint(); if (e.ok) last = Date.now(); } });
