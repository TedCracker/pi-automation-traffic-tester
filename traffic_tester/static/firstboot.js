// First boot of a card image: the only screen is "Name this Pi". After naming, the phone loses the Wi-Fi (it is renamed), so the second screen tells what to join.
import { $, api, esc } from './ui.js';

export async function renderFirstBoot(main) {
  document.body.classList.add('fb');
  const info = await api('/setup/info');
  main.innerHTML = `<div class="card fb-card" data-testid="fb-card">
    <h1 class="fb-title">Set up this Pi</h1>
    <p class="fb-lead">Give this Pi a name. It becomes the name of its Wi-Fi network and its address (<b id="fbEx">${esc(info.suggested)}.local</b>).</p>
    <label class="f fb-f">Name<input id="fbName" data-testid="fb-name" value="${esc(info.suggested)}" maxlength="32" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" inputmode="text" enterkeyhint="go"></label>
    <p class="muted fb-hint">Letters, digits and dashes. ${info.taken.length > 1 ? 'Already used on this network: ' + esc(info.taken.filter(t => t !== info.hostname).join(', ')) + '.' : 'Pi 2 will be offered pi2, and so on.'}</p>
    <div id="fbErr" class="fb-err" role="alert" hidden></div>
    <button class="btn primary fb-go" id="fbGo" data-testid="fb-go">Name this Pi</button>
  </div>`;
  const go = $('#fbGo'), err = $('#fbErr'), inp = $('#fbName');
  inp.oninput = () => { $('#fbEx').textContent = (inp.value.trim().toLowerCase() || '…') + '.local'; };
  const submit = async () => {
    err.hidden = true; go.disabled = true; go.textContent = 'Naming…';
    try { done(main, await api('/setup/name', { method: 'POST', body: { name: inp.value } })); }
    catch (e) { err.textContent = e.message; err.hidden = false; go.disabled = false; go.textContent = 'Name this Pi'; }
  };
  go.onclick = submit; inp.onkeydown = e => { if (e.key === 'Enter') submit(); };
}

const bare = u => String(u).replace(/^https?:\/\//, '');

function done(main, r) {
  main.innerHTML = `<div class="card fb-card" data-testid="fb-done">
    <h1 class="fb-title">This Pi is now called <span class="fb-name">${esc(r.name)}</span></h1>
    <p class="fb-lead">${r.ap_renamed ? 'Its Wi-Fi network was just renamed, so this phone will lose the connection in a few seconds.' : 'It is ready.'}</p>
    <ol class="fb-steps">
      ${r.ap_renamed ? `<li>Join the Wi-Fi network <b data-testid="fb-ssid">${esc(r.ssid)}</b><br>password <b data-testid="fb-pass">${esc(r.password)}</b></li>
      <li>Open <a href="${esc(r.urls.ap)}" data-testid="fb-url"><b>${esc(bare(r.urls.ap))}</b></a><br><span class="muted">or ${esc(bare(r.urls.local))}</span></li>`
        : `<li>On the same network as this Pi, open <a href="${esc(r.urls.local)}" data-testid="fb-url"><b>${esc(bare(r.urls.local))}</b></a>${(r.ips || []).length ? `<br><span class="muted">or ${esc(r.ips.map(bare).join(', '))}</span>` : ''}</li>`}
    </ol>
    <p class="muted" id="fbWait" data-testid="fb-wait">Waiting for the Pi to come back…</p>
    <p class="muted">To use your own Wi-Fi network instead, open Setup → Network after you are in.</p>
  </div>`;
  let n = 0;
  const poll = async () => {
    n++;
    try {
      const r2 = await fetch('/api/status', { cache: 'no-store', credentials: 'same-origin' });
      const s = r2.ok ? await r2.json() : null;
      if (s && s.first_boot === false && n > 2) { location.replace('/'); return; }
    } catch { /* the Wi-Fi is being renamed */ }
    setTimeout(poll, 3000);
  };
  setTimeout(poll, 3000);
}
