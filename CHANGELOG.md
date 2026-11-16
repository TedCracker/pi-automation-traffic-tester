# Pi Automation Traffic Tester — what's new

One entry per version, newest first. The About page, "Check online" and the release notes are all built from this file.

## 1.7.0 — 2026-11-15
- **"Ask Claude" and Remote Control are gone.** The chat panel, the Claude Code card in Settings, the sign-in and Remote Control are removed from the app, the installer and the update. The tester itself works exactly as before, and the AI guide is still there for any AI assistant you want to use with it.
- **Updating cleans up older installs.** A Pi that had Ask Claude gets its Remote Control units, the Claude Code package and Anthropic's apt repository, the saved sign-in and the app's change history removed once, on the first start of 1.7.0. Saved tests and results are never touched.
- The installer still accepts `--with-claude` so old commands keep working; it now prints a note and does nothing.

## 1.6.1 — 2026-11-16
- **Radio serial packetization is recorded with every run.** Serial tests have a new "Radio serial packetization" section (character timeout, packet size, serial mode, note), always visible on the DER transfer trip — Orbit radio test with the advice to set the radio to one message per packet (character timeout about 2 character times, packet size = the 19-byte frame). It shows on Results, the PDF, the CSV and JSON exports and the compare view; when empty, Results says "radio packetization not recorded". The Pi never changes the radio.
- **Warns when the radio bundles messages.** If frames arrive together in bursts, or the round trips sit at fixed steps, Results says "the radio seems to be bundling messages — check its character timeout".
- **Data use shown before and after the run.** The setup page and Results show the payload figure (about 2 KB/s each way, about 5 GB per 30 days) and the over-the-air figure with the IP/UDP or IP/TCP header per packet (about 2.5 to 3 times more at one frame per packet), per second, day and 30 days, plus the bytes the run really sent and received.
- **Time of day.** Every run is tagged with its start and end in local time and which busy hours it covered (weekdays 7-9 am and 4-7 pm, weekend). Runs of an hour or more break delay (p50, p99, p99.9, slowest), lost frames and outages down per hour of day and weekday vs weekend, with a small chart.
- **Scheduled repeats.** "🔁 Repeat…" runs a test every N hours, or at fixed times of day (every day, weekdays or weekends) for N days. Schedules survive a reboot, a slot that cannot start is counted as missed, and Results shows one combined summary (worst value of any run, by time of day, busy hours covered) with a PDF.

## 1.6.0 — 2026-11-15
- **New test: "DER transfer trip — Orbit radio (2 s budget)".** It follows the transfer-trip design: one Pi plays the substation relay (SEL-351 / 751 / 651R class), the other the DER relay (SEL-700GT class), and frames run nonstop in both directions over the radio's serial port at 19200 baud. Every frame carries eight status bits, a frame number, the send time, the echo of the last frame heard and a CRC, so each Pi measures the **round trip of every frame with no clock sync**, counts lost frames exactly, counts damaged frames (CRC errors) and records every outage. It passes when 99.9 % of round trips are within 200 ms, no more than 0.1 % of frames are lost and the channel stays up. A bit counts only after two good frames in a row, and the substation Pi flips the trip bit every 2 s. It runs 24 hours by default; run it for days to catch the worst case. A frame is 19 bytes, 9.9 ms at 19200 baud (about 100 a second each way). Not wire-compatible with SEL relays, and one-way times need GPS time, which is not built in (one-way is shown as round trip / 2 and says so).
- **New "Message format" choice on Mirrored Bits tests:** SimCodec (as before) or Trip frame. The Trip frame frees TMB8, because the echo travels inside the frame.
- **New pass rule percentile: p99.9** (999 out of every 1000), next to p50, p95, p99 and slowest.
- Long runs are lighter: round trips are kept as a fixed-size summary, the results chart keeps one row per minute on runs over an hour (about 0.1 MB a day on the SD card, under 1 MB for the rest of the run) and the run keeps going if the browser is closed. The results screen and PDF show round-trip percentiles up to p99.9, frames lost, CRC errors, number of outages and the longest outage.
- The existing "DER trip — Mirrored Bits mode" test is unchanged.

## 1.5.6 — 2026-11-15
- **The serial "DER trip test" now sends a message every 10 ms instead of 50 ms.** Real-Pi runs at 20, 10 and 5 ms (19200 and 38400 baud) all passed with 0 lost, and the round-trip time depends on the baud rate, not on the period. A 10-minute run is now 60,000 messages. Tests you already saved keep their own period; the IP DER trip test, TCP session test, Overnight soak and Mirrored Bits tests are unchanged.

## 1.5.5 — 2026-11-15
- **Mirrored Bits over serial now starts at 38400 baud.** The built-in "DER trip — Mirrored Bits mode" test, a new Mirrored Bits test on a serial port, and the self-test all default to 38400 (it was 19200). On the two real Pis over a null-modem cable, 38400 gave a bit change in 2.7 ms (median) against 5.7 ms at 9600, the fewest dropouts, and passed 2 of 2 runs. Plain serial tests stay at 19200, Mirrored Bits over UDP / TCP keeps its own rate, and saved tests, and any speed you pick yourself, are not changed.

## 1.5.4 — 2026-11-15
- **Clearer link picture on the Run screen.** The top line is blue with an arrow pointing to the far Pi (request going out); the bottom line is purple with an arrow pointing back (reply coming back). Both still turn amber when late and red when lost.

## 1.5.3 — 2026-11-14
- **New name: Pi Automation Traffic Tester.** Neutral wording throughout; no behaviour change.

## 1.5.2 — 2026-11-14
- **A capture that lost packets now says so, loudly.** On the real Pis a SPAN capture of 450 Mb/s of large frames lost about 9 % of the packets and still ended with "Pi OK" and one small grey line. The Results page now opens with a warning ("The capture is not complete: the system dropped N packets"), the PDF has the same sentence, and the result's Pi lamp reads "The Pi is the limit: its receive buffer dropped N packets". The cause on the Pi 5 is the SD card: it takes about 20 MB/s (160 Mb/s) of steady writing, so a longer or faster capture than that needs a filter, a short snap length (headers only) or a USB drive.

## 1.5.1 — 2026-11-13
- **A Pi that is capturing can still be the receiving end of a generator test.** Starting "Send traffic" to a Pi that was running a capture used to fail with "pi2 is already running a test", so you could not watch a generator's traffic on the far Pi. A running capture only listens and no longer counts as busy for that.
- **VPN devices are no longer offered as Ethernet ports.** `tail0` showed up as a "wired port" next to the real one, and on a Pi with one Ethernet port the inline tap picked it as its second port and failed half way through building the bridge. Now it says "needs two different network ports" before it touches anything.
- **"Simulate a bad link" on a generator or capture test is refused, not silently ignored.** A generator test that carried a bad-link preset ran perfectly clean and said nothing. It now stops with "works in trip tests only".
- **A generator test you stop early shows how long it really ran** (the result, the PDF banner and Compare's Length said the planned length, for example 60 s after a stop at 10 s).
- Found by running the generator and the sniffer on the two real Pis over a direct cable (the maintainer's private notes).

## 1.5.0 — 2026-11-13
- **Ready-made SD-card image.** Flash `pi-traffic-tester-<version>.img.xz` with Raspberry Pi Imager, power the Pi on, join the Wi-Fi **pi-setup** (password `password`) on a phone and open `http://10.42.0.1:8080`: the only screen is **Set up this Pi**. No monitor, keyboard or terminal. Software updates reach a card Pi like any other Pi (Settings → Software update).
- **Name this Pi.** The first screen of a new card asks for a name (`pi1` is offered; the next card finds the others on the network and offers `pi2`, `pi3` ...). The Pi's address becomes `<name>.local`, its own Wi-Fi network is renamed to the name, and the page reopens by itself. Pis you already run never see this screen.
- **Imager's own settings still work** (user, Wi-Fi, hostname, SSH). If you gave Imager a Wi-Fi, the Pi joins it and keeps its own network off; SSH stays off unless you switched it on.
- **No password on the card.** No account has a login password and nothing is shared between cards: every card makes its own Pi id and first-setup time.

## 1.4.0 — 2026-11-13
- **New test is one page, in test-set order:** Port (the ports this Pi really has: Ethernet, USB-Ethernet, Wi-Fi, serial; or Automatic) → Protocol (only what that port can carry) → From → To → Traffic, with the Start bar always at the bottom. Every row has a sensible default, so a first test is: tick the far Pi, ▶ Start. ▶ Run and Edit on a test card open the same page with each row closed to one line. The three-step wizard is gone.
- **Send to several Pis at once.** Tick two or more Pis under To: one test, one link per Pi. ⇅ Swap makes the far Pis send to this Pi (many to one). One way, Round trip or Both ways is one switch; the page says the test in a sentence.
- **One table for many links** on the Run screen and at the top of the result: a row per link (Now, p99, Lost), the worst in red, "1 of 2 links over the limit". Tap a row for its own numbers.
- **⚑ Mark** next to Stop: flag a moment ("antenna moved"). The flag shows on the charts, the result, the PDF and a new `mark` column in the CSV; a switch shows the numbers since the mark next to the whole test. The verdict is always the whole test.
- **Tester lamp** under the Run banner: Pi OK / Check the Pi / The Pi is the limit, naming the Pi and the reason (could not send at the asked rate, processor busy, receive drops, Wi-Fi power saving, USB adapter delay, clocks apart on a one-way test). Far Pis report theirs. Kept with the result and printed on the PDF.
- **Throughput in steps** (Generator): finds the highest clean rate in 5 steps instead of one flood; result "Clean up to N Mb/s" with one row per step.
- **Compare results:** Select 2-4 results of the same kind on Results → Compare: a table with the change against the first, one overlaid chart, one sentence, the setup differences, a one-page PDF. **Set as baseline** makes later runs of that test show "vs baseline".
- **Job info** (optional Site, Link / radio, By, Note) in the Start bar: shown in the results list, the result, compare, PDF and exports. Never blocks Start.
- Fixes: a Pi that restarted no longer leaves the other Pis stuck on "asks for a password"; serial / GOOSE / far generator ends left behind by a lost controller free themselves; the automatic time source no longer flips for one missed check or during a test; Stop and Delete work from any link of a test; Mirrored Bits refuses TX_ID = RX_ID, needs N consecutive good messages for ROK and raises CBAD when the channel never comes up; a far Pi's own generator test counts as busy for updates; names announced by other devices are cleaned before they reach exports. Details: the maintainer's private notes

## 1.3.2 — 2026-11-13
- **Mirrored Bits: the echo alarm default is 40 ms again.** It had been tied to twice the bit-transfer limit (120 ms), so a steady +50 ms delay, which ROK cannot see, raised no alarm. RTT_MAX is a setting you can change.

## 1.3.1 — 2026-11-13
- **Mirrored Bits: bit transfer times stay correct when the link keeps dropping in and out.** After an outage the bits sit at their defaults, so a bit flipped back to its default while the channel was down never "changed" at the output and the time to cross was reported as several seconds too long. The far end now logs what each good message actually delivered. Found on the real Pis over Wi-Fi.

## 1.3.0 — 2026-11-13
- **Mirrored Bits is now a full emulator of a pair of SEL-2505-style units.** Each end has 8 transmit bits and 8 receive bits and sends a complete message nonstop (never only on change). Every message is checked: characters (parity, framing, overrun), the three copies of each bit, the ID, and how late it came. A bad, late or missing message drops **ROK** at once and the received bits fall back to their defaults; **RBAD** and **CBAD** show a sustained outage or too much unavailability, and the event log (with milliseconds, CSV export) records every change. Per-bit PU/DO counts, per-bit defaults, PROTO A/B (a warning if both ends match), TX_ID/RX_ID, baud 2400–115200, parity / extra stop bit, RTS/CTS and a configurable message rate (back to back or per power-system cycle). The message format is our own **SimCodec** — emulator-to-emulator only, **not SEL wire-compatible**; a real SEL format can be added later.
- **See whether Mirrored Bits survives your link.** The live screen shows both ends (flip a bit on either Pi with a tap), the message stream in both directions (rejected messages in red with the reason), ROK / RBAD / CBAD / LBOK lamps, a **Break link** button and quick problems (+50 ms, jitter, 10 % drop, bit errors) that work over serial and over IP. An echo bit times the round trip and raises an alarm for a steady delay that ROK alone cannot see; the time for a bit change to cross the link is measured one way when both clocks are synced. Results, the PDF report and the CSV export include availability, longest outage, rejects by reason, RBAD / CBAD events, the echo round trip and an inter-arrival histogram.
- **Try it without a second Pi:** Run → "Try Mirrored Bits (internal self-test)". Inputs and outputs are on-screen switches and lamps; they can also be mapped to Pi GPIO pins (3.3 V: real contacts need an opto or relay board).
- Tests made with the first Mirrored Bits mode (1.1.3) still open as they were.

## 1.2.1 — 2026-11-12
- **One-way tests recognise a Pi that follows another Pi's clock.** When the clock service shows the time source by its name instead of its address, the app could not tell that two Pis shared a clock and refused a one-way test that was fine. Fixed.

## 1.2.0 — 2026-11-12
- **No more Pi A and Pi B: install the same app on every Pi.** Every Pi can start tests, answer tests and be controlled from another Pi's page. For each test you pick **Send from**, **Send to** and a **Direction**: *Round trip* (the usual), *One way from → to*, *One way to → from* or *Both ways at once*. The Pi you send to needs nothing set up.
- **Pis on this network** (Settings): other Pis running the app appear by themselves, with their name, address, version, clock state and whether they answer. If your network does not pass discovery messages, **Add a Pi by IP address**. From the list you can open a Pi's page, save its password, make this Pi its time source or update it too.
- **Pi names.** Give each Pi a name (pi1, lab-east, …) in Settings → This Pi. The top bar shows only the Pi's name and who is signed in, and results, PDF reports and exports say which Pi sent and which received, by name.
- **Every Pi always answers** UDP and TCP test messages on port 20500, so you never have to start a "Responder". Serial ports, Layer-2 GOOSE and Mirrored Bits are taken only while a test runs, with a plain "busy" message if something else is using them.
- **Two-way and many-Pi tests.** Run a test in both directions at the same time and get one result per direction. The traffic generator can send from several Pis to several Pis in one run (each stream picks its own from and to) and shows the numbers per pair of Pis.
- **Time source** (Settings): choose which Pi serves the clock (automatic, this Pi, another Pi or "leave as the installer set it"). One-way delay numbers need both Pis on the same clock; a one-way test now refuses to start, and says why, instead of showing numbers that cannot be trusted.
- **Wi-Fi access point and Network settings.** New installs start as a Wi-Fi access point (the Wi-Fi name is the Pi's name) with Ethernet for internet and testing, and Settings → Network lets you change it. Existing Pis are never switched automatically.
- **Updating from an older version keeps everything working.** Pis that were Pi A or Pi B keep their tests, results and time setup; the old "Responder" simply becomes the always-on answering. The `--role` installer option is still accepted and ignored.

## 1.1.3 — 2026-11-12
- **Live charts now update on real Pis.** The live view could not connect on a Pi installed from the standard packages (a missing WebSocket component), so charts and the Run screen only changed when you reloaded. Fixed, and the update installs the missing piece by itself.
- **Version number under the logo** on every page (tap it to open About).
- **About page** (top menu, or tap the version): version and date, which Pi this is, the update status, and this list of what changed in each version. After an update the app shows "Updated to … — what's new" once.
- **Full control of the tests on Setup.** Tap a test to read what it checks, how it works, what you need, what "pass" means and where it is used. **Edit** any of the built-in tests (an edited one shows "Edited" and has **Reset to default**), **rename** any test, **hide** built-in tests you do not use (bring them back with "Show hidden tests"), **delete** your own tests (with Undo), **reorder** the list and **create a new test** from the top of the list. Your edits are kept when the app updates. Export and import include your edits, hidden tests and order.
- **New test: "DER trip — Mirrored Bits mode".** Both Pis send continuously, back to back, at the chosen speed (9600 / 19200 / 38400 baud) and check every message they receive, the way a pair of protection relays does. A trip is one bit changing on the sending Pi; the other Pi changes its own bit when it sees it, and the sending Pi times the whole trip. Results show messages received, bad and lost messages, time the channel was OK or failed, longest outage and how fast a failure was detected. It copies Mirrored Bits' timing and load but is **not** wire-compatible with SEL relays. Serial first, also over UDP and TCP.
- **Run and Results refresh by themselves.** A small status shows "● Live" or "↻ Updating every 2 s", with a Refresh button and an on/off switch. If the live connection is down, the pages keep themselves up to date by polling.
- **Fixes from the first IP tests on real Pis.** A traffic-generator run that someone stops now says who asked (the result shows it and the log keeps the address), because the 10-second early stop seen on the first Pis could not be reproduced and had no trace. The generator uses the other Pi's address for the data address when you only give one. Before a test over Wi-Fi the app warns if Wi-Fi power saving is on (it adds wake-up delay that spoils idle-gap results) and offers a one-click "Turn off Wi-Fi power saving"; results say when the path was Wi-Fi.

## 1.1.2 — 2026-11-12
- **Create custom test** on the Setup page: a guided form (link type, message rate and length, round trip or each direction, pass limits, optional bad-link simulation and background load) with the allowed ranges shown as you type and a plain sentence saying what the test will do. Saved under **My tests** with Edit and Duplicate.
- Options this Pi cannot run (GOOSE without raw network access, bad-link simulation without permission) are greyed out with the reason.
- The AI guide describes the new form and has a step-by-step recipe for it.

## 1.1.1 — 2026-11-12
- Test release that proved the update channel: Settings → Software update → Check online found, downloaded, verified and installed a signed update from the private release site.

## 1.1.0 — 2026-11-12
- **Software updates** (Settings → Software update): upload a file or check online; every update is signed and checked before it installs; the previous version is kept and the Pi rolls back by itself if the new one does not start. Your tests, results and settings are never touched. One Pi can also update another.
- **Mobile-friendly screens**: bottom tab bar, large tap targets, sheets instead of pop-ups, charts that follow phone rotation, "Add to Home Screen".

## 1.0 — 2026-11-11
- The original Pi Automation Traffic Tester: **DER trip test** (a small message every 50 ms between two Pis with round-trip and one-way timing, percentile pass/fail against a 60 ms limit, "first message after quiet" checks for cellular radios), over serial, UDP, TCP and GOOSE (Layer 2 and routed).
- **Traffic generator** (rate, size and mix of packets, bursts, loss, delay and jitter) and **traffic sniffer** (Ethernet tap or SPAN port, RS-232/485 with DNP3 and Modbus decoding, Wi-Fi) that saves captures for Wireshark.
- Saved tests, sessions and results, PDF reports, CSV/JSON export, bad-link simulation, USB-serial adapter self-check and fix.
- Simplified 3-step Setup (pick a test, automatic link check, start) with the live chart on the Run screen; **AI guide** that lets an AI assistant in the browser read the app and answer questions; optional **Ask Claude** sidebar.
