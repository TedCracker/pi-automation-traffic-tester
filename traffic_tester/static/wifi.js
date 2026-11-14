// Wi-Fi power saving (the Raspberry Pi OS default) puts the radio to sleep between packets, which adds wake-up delay of 30-170 ms to the first
// message after a quiet period. When a test's traffic leaves this Pi over Wi-Fi the pre-flight says so and offers one click to turn it off.
import { $, esc, api, toast } from './ui.js';

// -> a pre-flight row {s, title, text, fix?, fixAct?, iface?} for the path to `host`, or null when it does not go over Wi-Fi / cannot be told
export async function wifiCheck(host) {
  if (!host) return null;
  let r; try { r = await api('/wifi/powersave?host=' + encodeURIComponent(host)); } catch { return null; }
  if (!r.wifi) return null;
  if (r.power_save) return { s: 'warn', title: `Wi-Fi power saving is on (${r.interface})`, text: 'This test goes over Wi-Fi. With power saving on, the Pi itself adds a wake-up delay to the first message after a quiet period (30-170 ms seen on real Pis), which can fail an idle-gap test that the link would have passed.', fix: 'Turn off Wi-Fi power saving', fixAct: 'wifioff', iface: r.interface };
  return { s: 'ok', title: `The test path is Wi-Fi (${r.interface})`, text: r.power_save === false ? 'Power saving is off, so the Pi adds no wake-up delay. The result will say the path was Wi-Fi.' : 'The result will say the path was Wi-Fi.' };
}

export async function turnOff(iface) {
  try { const r = await api('/wifi/powersave/off', { method: 'POST', body: { interface: iface } }); toast(r.message, !r.ok, 9000); return r.ok; }
  catch (e) { toast(e.message, true, 7000); return false; }
}

export const rowHtml = c => `<div class="check ${c.s}"><div class="ic">${c.s === 'ok' ? '✓' : '!'}</div><div style="flex:1"><b>${esc(c.title)}</b><small class="muted">${esc(c.text)}</small>${c.fix ? `<div style="margin-top:6px"><button type="button" class="btn small" data-wifi-off="${esc(c.iface)}" data-testid="wifi-power-off">${esc(c.fix)}</button></div>` : ''}</div></div>`;

// Fill `box` with the row for the path to hostFn(); clicking the button turns power saving off and checks again.
export async function mountWifi(box, hostFn) {
  if (!box) return;
  const c = await wifiCheck(hostFn());
  box.innerHTML = c ? rowHtml(c) : '';
  const b = $('[data-wifi-off]', box);
  if (b) b.onclick = async () => { b.disabled = true; await turnOff(b.dataset.wifiOff); mountWifi(box, hostFn); };
}
