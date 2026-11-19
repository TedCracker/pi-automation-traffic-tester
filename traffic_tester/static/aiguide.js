// AI-guide support: gives every actionable control a stable data-testid (and an aria-label where the visible text is not enough),
// and keeps the small "For AI assistants" note at the foot of the page current. Pure additions: no behaviour or layout change.
// Why: the AI sidebars (e.g. Tag Along) find controls by aria-label / visible text / selector, and form fields have none of those by default.
// Details and the extraction facts this is designed for: docs/AI-GUIDE.md.
const ACTIONABLE = 'button, a[href], input:not([type=hidden]), select, textarea, summary, [role=button], [role=tab], [tabindex="0"], tr[data-run]';
const slug = s => String(s || '').normalize('NFKD').replace(/[^\x00-\x7f]/g, ' ').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 44);
const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
let screen = 'setup', scheduled = false, data = null;

function labelText(el) {
  if (el.dataset.tcard !== undefined) { const h = el.querySelector('h3'); if (h) { const c = h.cloneNode(true); c.querySelectorAll('.tag').forEach(x => x.remove()); return clean(c.textContent); } }      // a Setup test card: its name
  const al = el.dataset.ariaAuto ? '' : el.getAttribute('aria-label'); if (al) return clean(al);          // our own earlier aria-label must not feed back into the id
  if (el.matches('input, select, textarea')) {
    let box = el.closest('label');
    if (!box && el.id) box = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    let t = '';
    if (box) { const c = box.cloneNode(true); c.querySelectorAll('input, select, textarea, option, .tip, small').forEach(x => x.remove()); t = clean(c.textContent); }
    if (t && box && box.querySelectorAll('input, select, textarea').length > 1) t += el.matches('select') ? ' (unit)' : ' (value)';
    if (el.type === 'file') return 'Profile file to import';
    if (el.id === 'fk') return 'Quick filter: what to match';
    if (el.id === 'fv') return 'Quick filter: value';
    if (el.dataset.k === 'name') return el.dataset.ch !== undefined ? 'Channel name' : 'Stream name';
    return t || clean(el.getAttribute('placeholder')) || clean(el.getAttribute('title')) || el.id || el.name || '';
  }
  if (el.classList.contains('tip')) return 'Help';
  if (el.matches('tr[data-run]')) return `Open test: ${clean([...el.cells].slice(1, 3).map(c => c.textContent).join(' · '))}`;     // name + time: does not change when RUNNING becomes PASS
  if (el.matches('.step')) return `Step ${el.dataset.step}: ${clean((el.querySelector('b') || el).textContent)}`;
  if (el.tagName === 'SUMMARY') return clean(el.textContent).replace(/\s*\([^)]*\)\s*$/, '');
  if (el.classList.contains('pcard')) { const h = el.querySelector('h3'); if (h) { const c = h.cloneNode(true); c.querySelectorAll('.tag').forEach(x => x.remove()); return clean(c.textContent); } }
  const c = el.cloneNode(true); c.querySelectorAll('.tip, .live, .tag').forEach(x => x.remove());
  return clean(c.textContent);
}

function scopeOf(el) {
  if (el.closest('#askPanel')) return 'ask';
  if (el.closest('#modal')) return 'dialog';
  if (el.closest('header.top')) return 'top';
  if (el.closest('footer')) return 'foot';
  return screen;
}

function context(el) {
  const card = el.closest('.pcard'); if (card) { const h = card.querySelector('h3'); if (h) { const c = h.cloneNode(true); c.querySelectorAll('.tag').forEach(x => x.remove()); return slug(c.textContent); } }
  if (el.dataset.i !== undefined) return 's' + el.dataset.i;                   // a stream (generator) / channel (sniffer) number
  if (el.dataset.ch !== undefined) return 'ch' + el.dataset.ch;
  return '';
}

const autoLabel = (el, v) => { el.setAttribute('aria-label', v); el.dataset.ariaAuto = '1'; };

export function annotate(root = document) {
  const seen = new Map();
  root.querySelectorAll(ACTIONABLE).forEach(el => {
    if (el.closest('.uplot, .u-legend')) return;
    const sc = scopeOf(el), isForm = el.matches('input, select, textarea');
    if (el.matches('.step') && !el.getAttribute('aria-label')) autoLabel(el, `Step ${el.dataset.step}: ${clean((el.querySelector('b') || el).textContent)}`);
    const label = labelText(el);
    if (isForm && (!el.getAttribute('aria-label') || el.dataset.ariaAuto) && label) autoLabel(el, label);
    if (el.classList.contains('pcard') && (!el.getAttribute('aria-label') || el.dataset.ariaAuto)) autoLabel(el, `${label} (choose this test)`);
    if (el.matches('tr[data-run]')) {
      if (!el.hasAttribute('tabindex')) { el.setAttribute('tabindex', '0'); el.addEventListener('keydown', e => { if (e.key === 'Enter') el.click(); }); }
      if (!el.getAttribute('aria-label') || el.dataset.ariaAuto) autoLabel(el, labelText(el) + ` (${clean(el.cells[0].textContent)})`);
    }
    if (el.classList.contains('tip') && (!el.getAttribute('aria-label') || el.dataset.ariaAuto)) autoLabel(el, 'Help: ' + clean(el.dataset.tip).slice(0, 60));
    if (el.dataset.testidAuto === undefined && el.hasAttribute('data-testid')) return;   // hand-written id (index.html): keep
    let base = sc + '-' + (el.classList.contains('tip') ? 'help' : (el.classList.contains('pcard') ? (el.dataset.tcard !== undefined ? 'open-' : 'pick-') : '') + (slug(label) || slug(el.id) || el.tagName.toLowerCase()));
    const ctx = el.classList.contains('pcard') ? '' : context(el); if (ctx) base += '--' + ctx;
    const n = (seen.get(base) || 0) + 1; seen.set(base, n);
    el.setAttribute('data-testid', n > 1 ? `${base}-${n}` : base); el.dataset.testidAuto = '1';
  });
}

function schedule() { if (scheduled) return; scheduled = true; queueMicrotask(() => { scheduled = false; annotate(); }); }

// `t` = the scope that prefixes generated ids (kept from before the shell change: setup = test list, run = live screen, settings = Setup pages); `key` = the tab, for the one-line summary.
export function setScreen(t, key = t) {
  screen = t; document.documentElement.dataset.screen = key; annotate();
  const el = document.getElementById('aiScreen'); if (el && data && data.screens && data.screens[key]) el.textContent = data.screens[key];
  refreshLive();
}

export async function refreshLive() {
  const el = document.getElementById('aiLive'); if (!el) return;
  try { const r = await fetch('/api/ai-context', { credentials: 'same-origin' }); if (r.ok) el.textContent = (await r.json()).live; } catch { }
}

let inited = false;
export function initAiGuide() {
  if (inited) return; inited = true;
  const d = document.getElementById('aiData'); try { data = d ? JSON.parse(d.textContent) : null; } catch { data = null; }
  new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  annotate();
}
