// Tag Along's real page-reading code, COPIED VERBATIM (not edited) from /mnt/c/Claude Projects/ai-sidebar-extension (v2.59.1) so tests can run it in Chromium.
// scripts/e2e_aiguide.py compares these copies with the live source when it is present and reports drift.
//   readPage()/Watch  -> chrome.scripting.executeScript(func: extractPageContent)            background.js:1133
//   Interactive page text -> extractRichestText(limit)                                   sidepanel/panel.js:1930
//   Interactive element map -> the func passed to executeScript in buildElementMap()      sidepanel/panel.js:1836
window.__ta = {
extractPageContent: function() {
  const clone = document.cloneNode(true);
  clone.querySelectorAll('script,style,nav,header,footer,aside,[aria-hidden="true"],noscript').forEach(el => el.remove());
  const main = clone.querySelector('main, article, [role="main"], #content, .content, .main-content');
  const target = main || clone.body;
  return target ? target.innerText.replace(/\s+/g, ' ').trim().slice(0, 15000) : '';
},
extractRichestText: function(limit) {
  // (2.44.0) VISIBLE-ONLY harvest from the LIVE DOM.
  // Why not cloneNode: a detached clone has no layout, so `innerText` degrades to
  // textContent and returns HIDDEN UI too. On Facebook that made the (closed)
  // notifications flyout the "richest" candidate — every read returned notification
  // chatter and ZERO listing text (debug log 71f46661: 0/41 observations contained a
  // price or GPU token). Walking the live DOM and rejecting invisible nodes fixes that.
  const NOISE = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, IFRAME: 1, SVG: 1, CANVAS: 1 };
  // (2.47.0) Site chrome — nav rails, filter sidebars, headers, footers. On eBay the left
  // filter rail ("Windows 11 Pro (344)", "AMD Ryzen 7 (120)"…) ate the entire 10,000-char
  // budget and the actual result cards never reached the model. Stripping it is usually
  // right, but some SPAs put real content inside these tags — so we only keep the stripped
  // version when it retains a meaningful share of the text (see the 40% check below).
  const CHROME_SEL = 'nav,header,footer,aside,[role="navigation"],[role="complementary"],[role="banner"],[role="contentinfo"],[role="search"]';
  const visibleText = (root, skipChrome) => {
    if (!root) return '';
    let out = '';
    try {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode(n) {
          const v = n.nodeValue;
          if (!v || !v.trim()) return NodeFilter.FILTER_REJECT;
          const p = n.parentElement;
          if (!p) return NodeFilter.FILTER_REJECT;
          if (NOISE[p.tagName]) return NodeFilter.FILTER_REJECT;
          // Rendered? (no boxes => display:none / detached / collapsed)
          if (!p.getClientRects().length) return NodeFilter.FILTER_REJECT;
          if (p.closest('[aria-hidden="true"]')) return NodeFilter.FILTER_REJECT;
          if (skipChrome && p.closest(CHROME_SEL)) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
      });
      const seen = new Set();
      let n;
      while ((n = walker.nextNode())) {
        const t = n.nodeValue.replace(/\s+/g, ' ').trim();
        if (!t) continue;
        // Collapse the repeated boilerplate SPAs sprinkle through the tree.
        const k = t.length > 12 ? t : t + '|' + (n.parentElement.tagName || '');
        if (seen.has(k)) continue;
        seen.add(k);
        out += t + '\n';
      }
    } catch { return ''; }
    return out.replace(/\n{3,}/g, '\n\n').trim();
  };
  const cands = [];
  // An OPEN modal is what the user sees — but only if it is actually rendered.
  document.querySelectorAll('[role="dialog"]').forEach(d => { if (d.getClientRects().length) cands.push(d); });
  ['main', 'article', '[role="main"]', '#content', '.content'].forEach(s => {
    const el = document.querySelector(s); if (el) cands.push(el);
  });
  if (document.body) cands.push(document.body);
  let best = '', bestNoChrome = '';
  for (const r of cands) {
    const t = visibleText(r, false); if (t.length > best.length) best = t;
    const c = visibleText(r, true);  if (c.length > bestNoChrome.length) bestNoChrome = c;
  }
  // Prefer the chrome-stripped text whenever it still yields a substantive page (>=500
  // chars) — that is the content the user cares about, and it keeps giant filter rails out
  // of the budget. If stripping leaves almost nothing, the site keeps its real content
  // inside nav/aside-ish tags, so fall back to everything rather than starve the model.
  const pick = (bestNoChrome.length >= 500) ? bestNoChrome : best;
  return { text: pick.slice(0, limit), truncated: pick.length > limit };
},
elementMap: () => {
        const sel = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [role=menuitem], [tabindex]:not([tabindex="-1"])';
        const els = Array.from(document.querySelectorAll(sel)).filter(el => {
          const r = el.getBoundingClientRect();
          return r.width > 0 && r.height > 0 && r.top < window.innerHeight && r.bottom > 0;
        });
        // (2.34.0) Cap + dedupe: dense pages (feeds, marketplaces) emit 150–300 lines
        // (3–5K tokens) per observation, mostly repeats ("Like" ×40). Every element still
        // gets a data-agent-id, so ids beyond the listing remain valid targets.
        // (2.36.0 fix #3) STABLE ids: an element keeps its data-agent-id for the life of the
        // page instead of being renumbered by DOM position on every mapping — renumbering
        // made ids reshuffle between turns (the model kept re-clicking "moved" controls).
        // New elements get fresh ids from a monotonically increasing per-page counter.
        if (window.__taAgentSeq == null) window.__taAgentSeq = 10000;
        // (2.47.0) Two-tier cap: page-CONTENT elements are listed before site chrome (nav
        // rails, filter sidebars, headers/footers). On eBay the 150-line cap was consumed by
        // filter checkboxes — "Windows 11 Pro (344)", "AMD Ryzen 7 (120)" — while the actual
        // result links sat below the cut. Chrome elements still get ids and still fill any
        // remaining room, so nothing becomes unreachable.
        const CHROME_SEL = 'nav,header,footer,aside,[role="navigation"],[role="complementary"],[role="banner"],[role="contentinfo"],[role="search"]';
        const content = [], chromeLines = [];
        const seen = {};
        const CAP = 150, DUP = 3;
        let skipped = 0;
        els.forEach(el => {
          if (!el.dataset.agentId) el.dataset.agentId = String(window.__taAgentSeq++);
          const i = el.dataset.agentId;
          const tag = el.tagName.toLowerCase();
          const label = (
            el.getAttribute('aria-label') ||
            el.getAttribute('title') ||
            el.getAttribute('placeholder') ||
            el.textContent?.trim().slice(0, 40) ||   // (2.38.0 F9) was 60 — 40 still disambiguates
            el.value?.slice(0, 40) ||
            ''
          ).replace(/\s+/g, ' ').trim();
          const key = tag + '|' + label;
          seen[key] = (seen[key] || 0) + 1;
          if ((label && seen[key] > DUP) || (content.length + chromeLines.length) >= CAP * 2) { skipped++; return; }
          // (2.38.0 O4) Terse line format — "10234 btn Label" / "10235 input:text Label" —
          // ~25% fewer tokens than the old `id=N <tag type=…> label` on dense pages.
          const short = { a: 'link', button: 'btn', select: 'sel', textarea: 'txt' }[tag] || tag;
          // (2.49.0) Skip unlabelled elements — ad iframes and image links produced rows like
          // "10286 link" with nothing to act on; they crowded out real results on eBay.
          if (!label && !['input', 'select', 'textarea'].includes(tag)) { skipped++; return; }
          // (2.50.0) Include each link's URL. Without it the only way to reach a result was to
          // click it, which forced: click -> read -> navigate back to the list -> re-scroll ->
          // hope the ids still match -> click the next. ~3 wasted steps per item, every item
          // (measured on Craigslist). With URLs present the agent harvests every link in ONE
          // observation and navigates straight to each detail page.
          let href = '';
          if (el.tagName === 'A' && el.href && !/^javascript:|^#/i.test(el.getAttribute('href') || '')) {
            try {
              const u = new URL(el.href);
              // (2.53.0) Strip tracking junk (ref, referral_*, utm_*, fbclid, tracking …).
              // Facebook's listing links ran past the 90-char cap and arrived TRUNCATED
              // ("…referral_story_type=pos"), which is a broken URL if copied literally.
              // Dropping the noise makes them ~40 chars, so they survive intact — and costs
              // fewer tokens on every observation.
              [...u.searchParams.keys()].forEach(k => {
                if (/^(ref|referral_|utm_|fbclid|tracking|_trk|si|igshid|__tn__|__cft__)/i.test(k)) u.searchParams.delete(k);
              });
              const rel = u.pathname + (u.searchParams.toString() ? '?' + u.searchParams.toString() : '');
              href = (u.origin === location.origin ? rel : u.origin + rel).slice(0, 110);
            } catch { href = ''; }
          }
          const line = `${i} ${short}${el.type ? ':' + el.type : ''} ${label}${href ? ' -> ' + href : ''}`;
          (el.closest(CHROME_SEL) ? chromeLines : content).push(line);
        });
        const lines = content.slice(0, CAP);
        const room = CAP - lines.length;
        if (room > 0) lines.push(...chromeLines.slice(0, room));
        const dropped = skipped + Math.max(0, content.length - CAP) + Math.max(0, chromeLines.length - Math.max(0, room));
        if (dropped) lines.push(`(… ${dropped} more elements not listed — repeats, site chrome (nav/filters), or beyond the ${CAP}-line cap. Unlisted elements still carry valid ids for click_id/type_id.)`);
        return lines.join('\n');
      },
};
