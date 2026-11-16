# AI guide — how an unmodified Tag Along (or any AI sidebar) understands and operates the Pi Automation Traffic Tester

Decision (Alex, 2026-11-09): **no changes to Tag Along**. Everything lives in the Pi app; Tag Along is used exactly as it is today
(Tag Along 2.59.1, source read-only at `/mnt/c/Claude Projects/ai-sidebar-extension`).

## How to use it (two sentences each)

- **Ask questions** ("how do I run a 60 ms trip test over serial?", "why did the last test fail?"): open the Pi app in Chrome
  (`http://<pi>:8080`), click **Watch** in Tag Along (turns it on and reads the page), then ask. If you move to another screen inside the app,
  click **Watch** off and on once (see "Re-reading" below). For the full manual, open the **AI guide** link (footer, Settings, or the small note at the bottom of
  every screen): Watch reads that page too.
- **Have it click for you**: switch Tag Along to **Interactive** mode and say what you want ("start a Quick link check against 192.168.1.50"). It finds the
  buttons by their exact labels / `data-testid` selectors printed in the guide.
- Anything that can fetch a URL (ChatGPT/Claude with browsing, `curl`): `http://<pi>:8080/ai-guide.txt` (also `/llms.txt`), plain text.
- With a web password set, sign in once in the browser first (the guide follows the same login rule; signed out it still teaches the app but hides the live state).

## Create custom test (2026-11-12)
The guide has the screen "Setup: Create custom test (guided form)" (every field with its exact label and data-testid, greyed-out rules), the card and the Edit / Duplicate buttons on step 1, `GET /api/profile-limits`, a glossary entry and the recipe `create-custom-test`; `scripts/e2e_aiguide.py` follows that recipe from the published guide.

## No fixed roles (2026-11-12, version 1.2.0)
There is ONE guide for every Pi: no "Pi A / Pi B", "Tester" or "Responder" anywhere (a unit test and the e2e scan the guide, the page note, `/api/ai-context` and every screen for those words; the two older API aliases
`/api/responder/start|stop` are real route paths and may be listed). The title is "Pi Automation Traffic Tester - AI guide (<Pi name>, <host name>, v<version>)".
- **Live facts** (`build.live_facts`): this Pi's name and id, the test running (with its directions: `runs.status()["legs"]`), the last result, the always-on reflector (`reflectors.status()`: ports, messages answered, ports held for a test),
  the other Pis (`peers.list()`: name, address, reachable, version, clock synced, needs password, busy; never a password or MAC), the clock and the time-source setting/decision, serial ports, interfaces, what is unavailable.
- **Screens**: Setup step 2 describes `Send from` / `Send to` / `Direction` (`setup-from`, `setup-to`, `setup-to-host`, `setup-direction-round_trip|forward|reverse|both`, `setup-clocks`, per-end serial / network port pickers, `setup-check*`),
  the Add-a-Pi sheet (`add-pi-*`), the custom form's `custom-endpoints`, Run's `run-leg-card` (one per direction), Results' `results-group-<n>` / `results-row-direction-<n>` / `result-ends*` / `result-open-direction-<n>` / `result-pdf-both`,
  the Generator's per-stream `gen-stream-<n>-from|to`, `gen-add-pi`, `gen-advanced`, `gen-pairs`, and Settings' `thispi-*`, `peers-*` / `peer-*`, `clock-*`, `net-*` (Wi-Fi access point / join / off, Ethernet DHCP / static, keep-or-revert bar) and `upd-other-*`.
- **Recipes** added: Add another Pi to the list · Run a test from this Pi to another (round trip) · One way or both ways at once · Run the generator between several Pis (N → M) · Name this Pi / set the time source ·
  Network: access point, join a Wi-Fi network, static Ethernet address (keep or revert within 90 s). The "Pair the two Pis" / Responder recipes and the role-B variant of the guide are gone.
- `scripts/e2e_aiguide.py` (98/98) now starts TWO identical apps (pi1, pi2) and follows those recipes from the published guide only (add pi2 by IP, run to it, both ways at once, the generator pi1 ⇄ pi2, a custom test, rename + time source, the network forms).
  The apply / Keep / revert flow of the network card needs the privileged helper and is covered by `scripts/e2e_netcfg_ui.py`; the guide's `net-*` ids are checked against `static/network.js`.

## What was built

| Piece | Where |
|---|---|
| `/ai-guide` (HTML, no JS, no CDN, dark/light), `/ai-guide.txt` and `/llms.txt` (same document as text) | `traffic_tester/server.py` (routes), `traffic_tester/aiguide/` |
| Document model + both renderers | `aiguide/model.py` |
| Generated content: tabs from `static/index.html`, presets from `profiles.builtin_presets()`, test kinds / link types from the `Profile` model, routes from the live router, glossary from `static/ui.js`, live state from the running app | `aiguide/build.py` |
| Hand-written content: screens and controls (exact labels + ids), recipes, how to read results, failures, route notes | `aiguide/content.py` |
| "For AI assistants" note in every page, server-rendered into `index.html` (first thing inside `<main>`, shown at the foot of the page) | `build.context_html`, `static/index.html`, `static/style.css` |
| Stable ids: `data-testid` on every actionable element, `aria-label` on every form field / preset card / tip, no visible change | `static/aiguide.js` |
| `GET /api/ai-context` (one-line live state for the note; behind the password) | `server.py` |

### Sections of the guide (most important first, so a cut-off reader keeps the essentials)
1 What this is (the name of THIS Pi, version, the other Pis it knows) · 2 **Live state** (test running + progress, last result with p50/p95/p99/slowest/lost/late and the FAIL reasons,
whether this Pi is answering tests, clock sync and time source, serial adapters, interfaces, USB drives, and **what is unavailable on this install and why**) · 3 **Quick start** (the two trip-test recipes, how to read a result) ·
4 every screen and control with exact label + selector + what it does, plus the preset table · 5 recipes for every task ·
6 how to read every number, pass rule, common failures and fixes · 7 glossary · 8 API summary (method, path, changes state yes/no, one line).
Every Pi gets the same guide (no roles since 1.2.0). Typical size: 16k tokens.

## Single source, never stale
- Presets, kinds, link types, tabs, routes and tooltips are **read from the code**; `build.check_coverage()` raises `GuideCoverageError` when something has no words.
  `tests/test_aiguide.py` fails if a tab, test kind, link type, preset or route (even one added at runtime) is not covered, and checks every recipe step uses a control that is in the control tables.
- `scripts/e2e_aiguide.py` (real Chromium) fails if a control the guide names is missing or has a different label on the real screen, if a screen has an actionable element without `data-testid`, or if any
  id on a swept screen is not covered by the guide.

## Facts about Tag Along's page reading (the design constraints), with file:line

**Watch / "read page"** (`sidepanel/panel.js:4017 readPage`, `:4029 readPageWatch`, `:4039 enablePageWatch`; message handler `background.js:337 GET_PAGE_CONTENT`; extractor `background.js:1133 extractPageContent`):
- Takes `document.cloneNode(true)` (a *detached copy*), removes `script, style, nav, header, footer, aside, [aria-hidden="true"], noscript`,
  picks the **first** element matching `main, article, [role="main"], #content, .content, .main-content` (else `<body>`), reads its `innerText`,
  collapses all whitespace to single spaces and **keeps the first 15 000 characters** (`.slice(0, 15000)`: head kept, **tail cut**).
- So: header (tab names, the Pi's name, theme button) and footer are **not** read; only what is inside `<main>` is. Structure is flattened to one line (no newlines).
  Because the copy is not rendered, `innerText` falls back to `textContent`: hidden (`display:none`, closed `<details>`) text *would* be included, but the guide does not rely on that:
  everything the AI needs is plain visible text.
- **When it re-reads:** only on `chrome.tabs.onActivated` (switching tabs) and `chrome.tabs.onUpdated` with `status === 'complete'` (a real page load) for the context tab (`panel.js:4041-4052`), and when
  Watch is switched on (`panel.js:4762`). A hash/SPA screen change inside the Pi app (Setup → Run) is **not** a page load, so the AI's copy of the page is not refreshed by it.
  Mitigations: the part that matters on every screen (this Pi's name, live state, screen list, link to the guide) is in the page source and stays valid; the AI guide is its own page (a load, so it is re-read when opened);
  and the user can click Watch off/on to refresh.
- The text is read at the moment the page finishes loading. The single-page app renders its screen a moment later, so the note is **server-rendered** (in the HTML), not added by script, and sits first in `<main>`.

**Interactive mode** (`panel.js:2010 buildObservation`, `:1836 buildElementMap`, `:1930 extractRichestText`, `:2420 read_page`):
- Observation page text: *visible-only* walk of the live DOM (`getClientRects().length`), skipping `script/style/noscript/iframe/svg/canvas`, `[aria-hidden="true"]` and (when ≥500 chars remain) `nav, header, footer, aside` and landmark roles; picks the richest of: open `[role=dialog]`, `main`, `article`, `[role=main]`, `#content`, `.content`, `<body>`;
  **first observation 2 000 chars, repeats on the same URL 1 200** (`panel.js:2027`), `read_page` tool 10 000 (`panel.js:2420`). One retry after 900 ms if the page text is under 400 chars (`:2040`).
- Element map (`panel.js:1841-1862`): selector `a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [role=menuitem], [tabindex]:not([tabindex="-1"])`;
  only elements **currently inside the viewport** (`r.top < innerHeight && r.bottom > 0`, non-zero size), max 150 lines, ≤3 repeats of the same label, page content listed before nav/header/footer/aside.
  Line format `id tag[:type] label -> href`; **label = `aria-label` || `title` || `placeholder` || first 40 chars of `textContent` || `value`** (`panel.js:1869-1875`).
  Consequences: form fields have no visible text of their own in the map, so every field got an `aria-label` equal to its visible label; the preset card (a `div role=button`) got an `aria-label`; the labelled controls are visible only after scrolling when below the fold (the guide tells the agent to scroll).
- Actions: `click_id/type_id/select_id` by map id (`data-agent-id`), `find_click` by visible text (first element whose text, value, aria-label or title *contains* it, `panel.js:2321`), or by CSS selector, `find_type` by selector/placeholder (`:2357`), `navigate` only to URLs that appeared in a map (`:2300`).
  Hence the guide gives, for every control, the exact label **and** a unique `[data-testid="..."]` selector (selectors are the unambiguous way: "Run" would match the tab "② Run" first).
- Page content is wrapped as untrusted (`panel.js:1487 wrapUntrusted`, system prompt line 1313 / 1343): embedded instructions are ignored. So the guide is written as documentation of the app ("To run X, click ..."), never as commands addressed to the AI.

The copies of these functions used by the tests: `scripts/tagalong_extractors.js` (verbatim from the source above; `scripts/e2e_aiguide.py` compares them with the live source and prints a warning if Tag Along changed them).

## The per-screen "For AI assistants" note
Placed by the server as the first child of `<main id="content">` and moved to the foot of the page by CSS (`order:99`), so a human sees a small muted footer note while a reader that truncates still sees it first
(it is in the page's real text, `display:none`/`aria-hidden` is **not** used and nothing differs from what a person could read). ~550 characters:

> For AI assistants: this is the Pi Automation Traffic Tester v1.2.0, Pi "lab-one". Screens: ① Setup, ② Run, … ⚙ Settings. <one-line description of the screen> <live line: "No test is running. Last result: 'Quick link check' PASS."> Full instructions, exact button labels, click-by-click recipes and live state: link AI guide (/ai-guide, plain text /ai-guide.txt).

The screen sentence and live line are refreshed by `static/aiguide.js` when the screen changes or a test starts / ends (`GET /api/ai-context`). Links named **AI guide**: as a small link in the top bar (right side, before the Pi name) (a separate header button was tried and dropped: it crowded the bar; this one adds no width), in the footer, in the note, and on the Settings screen. The top-bar one matters for Interactive mode: its element map only lists elements inside the viewport, and the note/footer are at the bottom of the page.

## Safety
- Read-only: only `GET /ai-guide`, `/ai-guide.txt`, `/llms.txt`, `/api/ai-context` were added; none changes state (unit test snapshots the stores before and after, and checks POST/PUT/DELETE are refused).
- No secrets: no password hash, token, Wi-Fi key, MAC address (unit test seeds fake secrets and greps every output). Only the *fact* that a password is set.
- Login rule: with a web password set, `/ai-guide*` and `/` serve the how-to part to everyone but replace the live state with "hidden" unless the browser has the login cookie; `/api/ai-context` is behind the password like every `/api` route.
- Every Pi has the same guide (there are no roles since 1.2.0).

## Maintaining it
Add a feature → run `pytest tests/test_aiguide.py` (tells you what to describe), edit `aiguide/content.py`, run `scripts/e2e-aiguide.sh` (proves the labels match the real screens).
New route → one line in `RN`. New tab → `VIEWS` + `VIEWS_BY_TAB_SUMMARY`. New preset → nothing if its link type already has a recipe (it is listed automatically).

## 2026-11-11: brought in line with the streamlined UI
- Six tabs (Setup, Run, Results, Generator, Sniffer, Settings); the Monitor tab is part of Run. Setup has 3 steps: a click on a test card opens "Connect & check", the link check runs by itself, "▶ Start test" is on that step; "Review settings" opens the optional step 3.
- Recipes and control tables in `traffic_tester/aiguide/content.py` follow that; `scripts/e2e_aiguide.py` proves every label and selector in a real browser (78/78).
- The **AI guide** link stays in the top bar: Tag Along's Interactive element map only lists the first controls of a page, so a footer-only link fell outside it on the long screens (found by the e2e when the review moved the link).
- Quick start now also says how to get a PDF and how to start the Responder on Pi B (both were beyond the 15 000 characters Watch reads).
- "Permission missing" messages no longer end with "The installed service has it." A small model read that as "available" (model check Q3); they now say this copy of the app was started without the permission.
- Model check, fed only what Watch extracts: qwen3-vl:8b 6/6 (`docs/ai-guide-model-check.md`); qwen2.5:7b-instruct 4/6 by the keyword rubric (`docs/ai-guide-model-check-qwen2.5.md`: Q2 is right in substance but lacks the rubric's word "delay"; Q1 does not name the Connect step). Before these fixes qwen3-vl scored 2/6 in this review's first run (two answers blank: all output tokens spent on hidden thinking). The rubric was not loosened, apart from Q1 and Q5 accepting the new screen names.
