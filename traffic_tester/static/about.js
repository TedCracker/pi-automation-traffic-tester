// About: installed version, which Pi this is, update status and "What's new" (CHANGELOG.md is the one source: GET /api/about).
// Also: the one-time "Updated to vX — what's new" banner shown after an update.
import { $, esc, api } from './ui.js';
import { S } from './state.js';

const SEEN = 'tt-seen-version';
const bold = t => esc(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
const fmtDate = d => { if (!d) return ''; const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(d)); const t = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(d); return isNaN(t) ? String(d) : t.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' }); };

// entries = [{version, date, notes: [..]}] newest first
export function changelogHtml(entries, { limit = 0, open = true } = {}) {
  const list = limit ? entries.slice(0, limit) : entries;
  return list.map((e, i) => `<section class="cl-entry" data-version="${esc(e.version)}"><h3>Version ${esc(e.version)}${e.date ? ` <small class="muted">· ${esc(fmtDate(e.date))}</small>` : ''}${i === 0 && open ? ' <span class="tag preset">newest</span>' : ''}</h3>
    <ul>${e.notes.map(n => `<li>${bold(n)}</li>`).join('')}</ul></section>`).join('');
}

export async function renderAbout(root) {
  let a;
  try { a = await api('/about'); } catch (e) { root.innerHTML = `<div class="card"><h2>About</h2><p>${esc(e.message)}</p></div>`; return; }
  const hw = a.hardware || {};
  const u = a.update || {};
  const checked = u.checked ? new Date(u.checked * 1000).toLocaleString() : '';
  const upd = u.newest
    ? (u.newer ? `<b>Version ${esc(u.newest)} is available.</b> <a href="#config/update" data-testid="about-open-update">Open Setup → Software update to install it</a>.` : `You have the newest version${u.newest !== a.version ? ` (the channel offers ${esc(u.newest)})` : ''}.`)
    : 'Not checked yet.';
  localStorage.setItem(SEEN, a.version);
  root.innerHTML = `<h1>About</h1>
    <div class="card" data-testid="about-card"><div class="row sp"><div><h2 style="margin:0">${esc(a.name)} <span class="tag preset" style="text-transform:none" data-testid="about-version">v${esc(a.version)}</span></h2><p class="muted" style="margin:4px 0 0">${esc(a.product)}</p></div></div>
      <dl class="kv" style="margin-top:12px">
        <dt>Installed version</dt><dd>${esc(a.version)}${a.date ? ` <span class="muted">· installed ${esc(fmtDate(a.date))}</span>` : ''}</dd>
        ${a.previous_version ? `<dt>Previous version</dt><dd>${esc(a.previous_version)} <span class="muted">(kept for “Roll back”)</span></dd>` : ''}
        <dt>This Pi</dt><dd data-testid="about-pi-name">${esc((a.pi && a.pi.name) || a.hostname)}${a.pi && a.pi.name !== a.hostname ? ` <span class="muted">· host name ${esc(a.hostname)}</span>` : ''}${hw.model ? ` · ${esc(hw.model)}` : ''}</dd>
        <dt>Updates</dt><dd>${upd}${checked ? ` <span class="muted">· last checked ${esc(checked)}</span>` : ''}</dd></dl></div>
    <div class="card" data-testid="whats-new"><h2>What's new</h2>${changelogHtml(a.changelog) || '<p class="muted">No change log is included in this copy.</p>'}</div>`;
}

// After an update the first page load shows what changed, once per browser. A browser that has never opened this Pi but finds an older
// "previous version" next to the app counts as updated too.
export async function maybeUpdatedBanner(status) {
  const seen = localStorage.getItem(SEEN);
  if (seen === status.version) return;
  let a;
  try { a = await api('/about'); } catch { return; }
  const from = seen || a.previous_version;
  if (!from) { localStorage.setItem(SEEN, status.version); return; }       // a fresh install: nothing was "updated"
  const cmp = (x, y) => { const p = s => String(s).split('.').map(Number), A = p(x), B = p(y); for (let i = 0; i < Math.max(A.length, B.length); i++) { const d = (A[i] || 0) - (B[i] || 0); if (d) return d; } return 0; };
  if (cmp(status.version, from) <= 0) { localStorage.setItem(SEEN, status.version); return; }
  const news = a.changelog.filter(e => cmp(e.version, from) > 0 && cmp(e.version, status.version) <= 0);
  localStorage.setItem(SEEN, status.version);                                // shown once, even if the page is reloaded right now
  const host = document.createElement('div'); host.id = 'updBanner'; host.className = 'upd-banner'; host.setAttribute('role', 'status'); host.dataset.testid = 'updated-banner';
  // one line: the notes themselves are one tap away in About (kept in the page, hidden, for AI assistants reading it)
  host.innerHTML = `<span class="ub-line">Updated to v${esc(status.version)} · <a href="#config/help" id="updBannerMore" data-testid="updated-banner-about">what's new</a></span><button type="button" class="icon" id="updBannerX" aria-label="Dismiss" data-testid="updated-banner-dismiss">✕</button>
    <div class="upd-banner-body" hidden>${changelogHtml(news.length ? news : a.changelog.slice(0, 1), { open: false })}</div>`;
  $('#content').insertBefore(host, $('#main'));          // after the server-rendered AI note, which must stay the first child of <main>
  $('#updBannerX').onclick = () => host.remove();
  $('#updBannerMore').onclick = () => host.remove();
}
