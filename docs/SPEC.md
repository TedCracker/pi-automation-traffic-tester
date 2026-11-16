# Pi Automation Traffic Tester — Specification & Architecture

Version 1.0 · 2026-11-03, rewritten for 1.2.0 (no fixed roles) 2026-11-12 · Source: docs/DETAILS.md spec items + the maintainer's private notes

## 1. Purpose

**Raspberry Pi 5**s (target hardware, Bookworm 64-bit), two or more, all running the same app, measure whether a communications link (e.g. 5G router / FirstNet radios) can carry a protection-style trip message fast enough — default target **60 ms** — by emulating the *timing* of SEL Mirrored Bits: small fixed frames, sent continuously, 8 status bits each. Users are **network engineers, not automation engineers**: the app speaks in links, ports, delay and pass/fail, never in protocol jargon.

### 1.1 Goals
- Round-trip timing (send → reflect → back) between any two Pis over serial, UDP and TCP, in either direction or both at once; one-way (up/down) when clocks are provably synced.
- Fixed-interval trip messages with p50/p95/p99/max, jitter, loss, duplicates, out-of-order, over-threshold count; pass/fail on a percentile.
- First-message-after-idle statistics (radio wake-up delay) reported separately from steady state.
- USB-serial adapter self-check (FTDI `latency_timer`) and local baseline, so adapter faults are separated from network faults.
- Saved test profiles, JSON export/import, read-only built-in presets ("Duplicate to edit").
- Sessions with history; CSV/JSON export; copy to a flash drive.
- Modern, guided web UI (Setup / Run / Results, plus Generator, Sniffer, Settings) with live charts.
- Runs fully offline on a Raspberry Pi 5 (4 GB or 8 GB; the app itself stays under ~150 MB RAM).

### 1.2 Non-goals (DECISIONS 2026-11-03)
- No line-rate traffic generation (TRex/MoonGen class). Throughput is a light check only; iperf3 covers bandwidth.
- No full DNP3/Modbus protocol simulators. Later phases add only enough protocol to time a request/response over a real session.
- No GPS timing. One-way timing relies on NTP over a path that is **not** the link under test.

## 2. Pis, names, discovery and endpoints

**One app, no roles (1.2.0).** The same app is installed on every Pi. There is no "Pi A / Pi B" and no "Tester / Responder": every Pi can start tests, answer the tests of other Pis, and be controlled from another Pi's web page. Each **test** names where it sends from, where it sends to and in which direction. The model is iperf3 / a Cisco IP SLA responder: identical endpoints, an always-on reflector, the test decides who plays which part. Design notes: the maintainer's private notes, DECISIONS 2026-11-12.

### 2.1 Name and identity
- Each Pi has a stable **id** (8 hex characters, created at first start, never changes), a friendly **name** (setting `pi_name`; default = the host name, e.g. `pi1`; 1-32 characters `A-Z a-z 0-9 space . _ -`, first one a letter or digit; editable in Settings → This Pi; `PUT /api/pi`, empty = host name) and the time it was first set up (`setup_at`, UTC; used by the time-source rule, §2.6). The name is also the Wi-Fi name (SSID) when the Pi is an access point.
- The top bar shows only this Pi's name and, when a web password is set, "Signed in" with a Sign out link; results, reports and the AI guide name Pis by name, never by letter.
- `GET /api/status` (open) carries `pi: {id, name}`; it has **no `role`**.

### 2.2 Finding the other Pis ("Pis on this network")
- **Discovery:** every Pi announces itself over mDNS as service `_pitester._tcp.local.` (python-zeroconf, a pure-Python wheel, listed in `requirements.txt` and carried in the offline package) and browses for the others. The announcement's port is the web port; its TXT properties are `app=pi-traffic-tester`, `id`, `name`, `version` and `ips` (comma-separated addresses). If `zeroconf` cannot start (missing package, no multicast) Settings shows the reason (`discovery_error`) and manual adding still works. UDP 5353 is used only for this.
- **Add a Pi by IP** (`POST /api/peers {host, password?}`): for networks that do not pass multicast (some radios, routers, VPNs). `host` is an IP address or name with an optional `:port` (default 8080). The Pi must answer `GET /api/peer/info`; otherwise a plain 422 ("Cannot reach … / No Pi Automation Traffic Tester answered at …"). Manually added Pis can be removed (`DELETE /api/peers/{id}`); Pis found by mDNS come back by themselves, so removing one only forgets a hand-added entry.
- **The list** (`GET /api/peers`) is polled every 10 s by the app (and on `POST /api/peers/refresh`): per Pi `{id, name, host, port, source: mdns|manual|both, ips, reachable, version, clock, busy, password_required, auth: ok|needs_password|unknown, has_password, error, last_seen, setup_at, hardware, clock_source}`. `GET /api/peer/info` is open (like `/api/status`) and carries the same facts about one Pi. Settings shows name, address, version, "clock synced ±0.3 ms" or "not synced", reachable ✓/✗, busy, "needs password"; actions: Open its page, Set password, Remove, Use this Pi as its time source, Update it too.
- A test or the generator can name a Pi by **id or name** (picked from the list) or by a typed address; an address that is not in the list can only be the answering end, because this Pi cannot control it.

### 2.3 Every Pi always answers (the reflector)
- From the moment the app starts every Pi answers **UDP and TCP** trip messages on port **20500** (setting `reflector_ports`, 1-8 ports; Settings → This Pi → Advanced). Nothing has to be set up on the Pi that is sent to. The reflector is the old Responder's echo engine, started once, so the trip wire format is unchanged.
- What cannot be shared is **claimed per test**, by the Pi that controls the test, and released when the test ends (also on failure or Stop): a serial port, a raw Layer-2 interface / GOOSE L2 subscription, the GOOSE L3 mirror, Mirrored Bits mode, a non-default port, test hooks (delay / drop, `TT_TEST_HOOKS=1`). A claim on something in use fails with a plain "… is busy: it is being used by …" (HTTP 409 / 422). A port cannot listen and send at once, which is why serial and GOOSE are claimed rather than always on.
- The GOOSE L3 mirror is deliberately **not** always on (DECISIONS 2026-11-12). `POST /api/responder/start|stop` remain as an alias that claims / releases the same service, so old scripts keep working.
- Between Pis: `POST /api/peer/reflect/ensure {cfg, owner}`, `POST /api/peer/reflect/release {owner}`, `GET /api/peer/reflect/status`.

### 2.4 Source, Destination and Direction (per test)
Every test has a source (`source`, plus `sources` for many → one), a destination (`dest`, plus `dests` for one → many) and a **Direction**; the New test page (§6.4) shows them as From → To with a tick list, One way / Round trip / Both ways and Swap. A source or destination is an Endpoint `{pi, host, name, port, serial_port, iface}`: a Pi from the list (id or name), a typed address, an optional test port, which serial port or Ethernet port to use on that Pi. Blank source = this Pi; blank destination = `net.peer_host` (how older tests were written, so every saved test still opens).

| Direction (UI words) | `direction` | What happens |
|---|---|---|
| **Round trip** (default) | `round_trip` | the source sends, the destination reflects each message back, the source times the round trip |
| **One way: from → to** | `forward` | the same messages, judged on the delay source → destination (the destination stamps its receive time) |
| **One way: to → from** | `reverse` | the same the other way round: the destination sends, the source reflects |
| **Both ways at once** | `both` | two tests at the same time, one each way |

- A **leg** is "one Pi sends trip messages, another Pi reflects them" (`plan.plan_trip`). Round trip and forward are one leg (sender = source), reverse is one leg (sender = destination), both is two legs run concurrently. Each leg is stored as its **own run** (`runs.group_id` ties the two of one test together, `runs.leg` holds `{label, kind, basis, tester, reflector, controller}`); `summary.endpoints = {source, dest, kind, basis, controller, address, label}` names, **by name**, which Pi sent and which received. The live events of the first leg are the normal ones (tagged `leg: 0`), the others arrive as `{ev: "legstate", leg: n}`; one `done` event ends the test with `legs: [{index, run_id, label, status, pass}]`. `POST /api/runs` returns `{run_id, run_ids}`.
- **Refused with a plain reason, never silently changed:** `both` for serial and GOOSE (a port cannot send and reflect at once); `forward` / `reverse` when the two clocks do not agree (§2.6); a Pi that is off, busy, or password-protected without a saved password; a serial port or interface already in use.
- `POST /api/probe <profile>` checks the whole plan (including preparing the answering end for a moment) and returns `{ok, peer, one_way_ok, legs}` or `{ok: false, error}`; the Setup screen shows "<name> answered ✓".
- Serial tests: pick which Pi's serial port sends and which one reflects (`GET /api/peers/{id}/ports`, `/interfaces`).

### 2.5 The controller
The **controller** is the Pi whose web page you started the test on. It plans the legs, checks that every Pi taking part answers, prepares the reflecting ends (itself, or through that Pi's own API), starts each sender (itself, or `remote.RemoteTester` through the other Pi's API, which relays the live numbers and copies the finished result back), collects the results and stores the run(s). State machine (`orchestrate`): planning → checking → preparing → running → collecting → done | failed | stopped; a failure in any step releases whatever was prepared before it, so a refused or failed start never leaves a reflector or a serial port claimed on another Pi. One test at a time per controller (409 if busy). Pairing is still in-band: before a run the sender sends `HELLO` frames on the chosen port and the answering Pi's reply carries its clock state, shown with the name the controller asked ("<name> answered ✓, clock synced ±0.3 ms"); if nobody answers the run fails with "<name> did not answer".

### 2.6 Clock (time source)
- One-way delay numbers are only meaningful when both Pis are on **the same clock**, so exactly one Pi serves time and the others follow it. Setting `clock_source` (Settings → Time source; `GET/PUT /api/clock/source`, `POST /api/peers/{id}/clock-source` for another Pi): **auto** (default: the Pi that was set up first, earliest `setup_at` among the Pis it can see, serves time and the others follow it), **self** (this Pi serves), **a Pi** (follow that Pi), **keep** (leave the installer's time setup alone: this is what installs from before 1.2.0 get, see §2.8).
- The decision is applied through the narrowly scoped privileged helper (chrony); if the helper is not installed the Settings card says "needs the one-time installer step" instead of pretending. The app never syncs over the link under test.
- **Agreement check:** two Pis that both say "synced" may each be synced to a different source and seconds apart, so before a one-way test the controller also checks that both **follow the same time source** (the same reference address, or one follows the other). Not the same → the one-way test refuses to start with the plain reason ("… are not following the same time source; set one Pi as the time source in Settings → Time source"). The optional "Measure each direction" on a round-trip test needs the same agreement or falls back to round trip only, saying why.

### 2.7 Security and ports
- Remote control honours the **app password of the Pi being controlled**: the controller keeps one saved password per peer (`POST /api/peers/{id}/password`, or when adding), logs in once per peer and keeps that session; passwords are never written into exports or shareable files. A Pi that needs a password nobody entered shows "needs password" and tests that involve it refuse to start. Only `GET /api/status` (login state, name) and `GET /api/peer/info` are open; everything else, including the reflector hand-over and the generator node, needs the sign-in.
- Ports this app uses: **TCP 8080** (web UI and control API), **UDP + TCP 20500** (always-on reflector; `reflector_ports`), **UDP 5353** (mDNS, to announce and browse), generator data ports **20600+** and GOOSE (**UDP 102**, raw EtherType **0x88B8**) **only while a test runs**. Nothing else is opened.

### 2.8 Migration from role A / role B installs
Done once at the first start of 1.2.0 (`migrate_roles`, recorded in the settings): `clock_source = keep` (the installer's time setup stays: the old Pi A keeps serving, the old Pi B keeps following it), an old Responder port other than 20500 becomes an extra always-on port, an old Responder on serial or GOOSE is no longer left listening (the Pi that starts a test now claims it), old saved tests, sessions and results open as they are (empty source / destination = this Pi → `net.peer_host`). The `--role A|B` flag and `TT_ROLE` are read once and then ignored (a note is printed); the installer still accepts `--role`. Existing installs never change their network mode on update.

## 3. Trip message wire format

All integers big-endian. CRC = CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over every byte after the sync/magic, excluding the CRC itself.

### 3.1 Compact frame — 7 bytes (serial default, Mirrored-Bits-like)

| Off | Size | Field |
|---|---|---|
| 0 | 1 | Sync `0xA5` |
| 1 | 1 | Type (high nibble: 1=TRIP, 2=ECHO, 3=HELLO, 4=HELLO_ACK) · flags (low nibble: bit0 = idle-probe) |
| 2 | 2 | Sequence (u16, wraps; the sender unwraps to 64-bit by nearest-expected) |
| 4 | 1 | Payload: 8 status bits (TB1–TB8 equivalent) |
| 5 | 2 | CRC-16 |

- 7 bytes at 19200 8N1 = 3.6 ms on the wire (at 9600: 7.3 ms) — same order as a real Mirrored Bits message.
- The sender keeps its own send timestamp per sequence locally; the frame carries no timestamps, so a round trip needs nothing from the reflecting Pi but an echo.
- Receiver resync: scan for `0xA5`, take 7 bytes, verify CRC; on failure drop one byte and rescan. CRC failures are counted ("corrupted").

### 3.2 Extended frame — 40 bytes (UDP/TCP default; serial when one-way mode is on)

| Off | Size | Field |
|---|---|---|
| 0 | 2 | Magic `0xA5 0x5B` |
| 2 | 1 | Version (1) |
| 3 | 1 | Type (as above, full byte) |
| 4 | 1 | Flags (bit0 idle-probe, bit1 reflector-clock-synced) |
| 5 | 1 | Payload bits |
| 6 | 4 | Sequence (u32) |
| 10 | 8 | t1: sender's send time, CLOCK_REALTIME ns |
| 18 | 8 | t2: reflector's receive time, CLOCK_REALTIME ns (0 in TRIP) |
| 26 | 8 | t3: reflector's send time, CLOCK_REALTIME ns (0 in TRIP) |
| 34 | 4 | Reflector's NTP offset estimate, µs, signed (from chrony) |
| 38 | 2 | CRC-16 |

- UDP: one frame per datagram. TCP: frames back-to-back on the stream, resync by magic + CRC.
- HELLO_ACK answers a HELLO: the extended form carries t1/t2/t3 and the reflecting Pi's NTP offset, both forms carry the clock-synced flag. The sender already knows whom it asked (the controller names the Pi), so the reply only proves that it is there and tells its clock state.

### 3.3 Timing computed by the sender
- `RTT = sender_rx_mono − sender_tx_mono` (CLOCK_MONOTONIC, `time.monotonic_ns()`), always valid.
- `reflector_proc = t3 − t2` (the reflecting Pi's turnaround), reported; RTT is **not** corrected for it (it is real path delay).
- One-way (extended frames only): `up = t2 − t1`, `down = sender_rx_real − t3`. Shown **only** when both Pis' offsets are ≤ `max_clock_offset_ms` (default 2 ms) at run start, stay within it, and both follow the same time source (§2.6); otherwise a round-trip test with "Measure each direction" falls back to round trip only and the UI says why ("Clocks not synced: pi2 offset 7.4 ms"). A test whose Direction is one-way refuses to start instead. The offset is printed beside every one-way number.

## 4. Test engine

### 4.1 Run phases
1. **Pre-check**: open port, FTDI self-check (serial), HELLO pairing (timeout 3 s → "<name> did not answer").
2. **Steady state**: send one TRIP every `interval_ms` for `duration_s`. Payload bits rotate a walking-one pattern so the bit view visibly moves.
3. **Idle probes**: for each gap in `idle_gaps_s` (e.g. 5 / 30 / 120 / 600), repeat `idle_repeats` times: send nothing for the gap, then send one TRIP (flag idle-probe) and time it. Order steady→idle by default; `idle_mode = interleaved` instead inserts probes during a long soak.
4. **Finish**: compute summary + verdict, store, push final result to UI.

### 4.2 Message classification (A side)
- **ok**: reply within threshold.
- **late**: reply after `threshold_ms` but before `timeout_ms` (default 1000 ms, must be > threshold).
- **lost**: no reply within `timeout_ms`. A reply arriving afterwards is counted **stale** and does not un-lose the message.
- **duplicate**: second reply for an already-answered sequence.
- **out-of-order**: reply whose sequence is lower than the highest already received.
- **corrupted**: CRC failure (serial/TCP stream).

### 4.3 Metrics
- Percentiles p50/p95/p99 and max, nearest-rank method, over received replies (ok + late). Mean, min.
- **Jitter**: mean |RTT[n] − RTT[n−1]| over consecutive received messages (RFC 3550-style IPDV); also standard deviation.
- **Loss** count and %, **over-threshold** count and % (late count), duplicates, out-of-order, stale, corrupted.
- **Idle-first**: per gap — count, min/median/max RTT, lost; and overall idle-first max.
- **Per-interval summary** every `summary_interval_s` (default 1 s; 10 s auto for runs > 1 h): sent, received, lost, late, p50, p95, p99, max.
- One-way metrics (up/down p50/p95/p99/max) only when §3.3 conditions hold.

### 4.4 Pass/fail rule (configurable per profile)
- `rule.percentile` (50/95/99/max, default 99) ≤ `threshold_ms` (default 60), **and**
- loss % ≤ `rule.max_loss_pct` (default 0.1), **and**
- if `rule.include_idle` (default true): every idle-first reply ≤ `rule.idle_threshold_ms` (default = threshold) and none lost.
- Verdict shows one plain sentence per failed criterion: "99 % of messages arrived within 74 ms — limit is 60 ms."

### 4.5 Timing accuracy
- All latency from `time.monotonic_ns()`; realtime clock only for one-way fields.
- **Serial**: a dedicated reader thread doing blocking `read()` (pyserial, `timeout` 50 ms), timestamping immediately after `read` returns; the writer timestamps immediately before `write()` + `flush()`. Avoids asyncio loop latency.
- **UDP/TCP**: dedicated socket thread per run (blocking `recv` with timeout) — same reason. The asyncio side (FastAPI) only consumes results from a queue.
- B echoes in its reader thread without touching the event loop: read → stamp → write.
- Expected overhead on a Raspberry Pi 5 (4× Cortex-A76 @ 2.4 GHz, ~2–3× a Pi 4): ~0.05–0.3 ms Python scheduling + USB-serial 1–3 ms per side with `latency_timer=1` (the FTDI USB poll dominates; 16 ms per side if untuned). With an RP1 GPIO UART it is well under 1 ms plus the character time on the wire. Ethernet is real gigabit (~940 Mb/s). The serial baseline (§5.1) measures this so it can be subtracted mentally from link results; the app shows "local overhead ≈ x ms" next to results.
- Optional: run the engine thread with `SCHED_FIFO` via `os.sched_setscheduler` if permitted (CAP_SYS_NICE); silently skipped otherwise.

## 5. Transports

### 5.1 Serial (MVP)
- pyserial; 8N1 default; baud 9600/19200/38400/57600/115200; no flow control.
- **Auto-detect** (Pi 5, RP1): list `/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/ttyAMA*` (ttyAMA0-4 = GPIO UARTs, **ttyAMA10 = 3-pin JST debug connector**, also `/dev/serial0` by default), `/dev/ttyS*` (only those that open), labelled in plain words ("Pi 5 debug connector (3-pin JST)", "GPIO 14/15 UART0, pins 8/10 (needs dtparam=uart0=on)"); sorted USB adapter → GPIO UART → ACM → debug connector → ttyS, and Auto picks the first openable in that order (never the debug connector while a better port exists). Aliasing differs between sources: verify on the real Pi (`ls -l /dev/serial* /dev/ttyAMA*`, `pinctrl get 14,15`). GPIO UARTs need a MAX3232 for RS-232 levels. Also plus `pty` paths entered manually (used by local tests). Show USB vendor/model/serial from sysfs, e.g. "FTDI FT232R USB UART (A10K3XYZ)".
- **Self-check**: read `/sys/bus/usb-serial/devices/<tty>/latency_timer`; if > 1 ms, show "Adapter adds up to 16 ms per side — Fix now" → writes 1 (needs root/udev permission; app explains when it can't) and offers the persistent udev rule:
  `ACTION=="add", SUBSYSTEM=="usb-serial", DRIVER=="ftdi_sio", ATTR{latency_timer}="1"` (installed by install.sh).
- **Baseline**: "Back-to-back baseline" preset with a null-modem cable between the two Pis (or a loopback plug on one adapter: TX→RX, A sees its own frames — local self-test).

### 5.2 UDP (MVP)
- Every Pi's reflector listens on UDP port 20500 (§2.3); the sending Pi sends extended frames. DSCP configurable (default EF/46).

### 5.3 TCP (MVP)
- Every Pi's reflector listens on TCP port 20500 (§2.3); the sending Pi connects and keeps the session open for the whole run. `TCP_NODELAY` on.
- Measures **session setup time** (connect duration) and, on a drop, **reconnect timing**: the sender retries every 200 ms; reports outage = last good reply → first good reply after reconnect, and the connect time of the new session.
- **Silent-drop detection** (TODO item): `SO_KEEPALIVE` with configurable idle/interval/count, plus app-level detection: no reply for `timeout_ms` while sending → mark "session stalled", count it, force reconnect.

### 5.4 Later phases
- **DNP3** (serial/TCP/UDP, outstation on 20000): time a class-0 poll/response over a real session — only with a solid pure-Python library; otherwise not built.
- **Modbus RTU/TCP**: time read-holding-registers via pymodbus.
- **IEC 61850 GOOSE**: full L2 GOOSE and L3 multicast (R-GOOSE) testers — see §5.5 (Alex 2026-11-03; built in phase 2).
- **Impairment presets (tc/netem)**: applied to a chosen interface (or a veth pair for local tests); needs root/CAP_NET_ADMIN, otherwise greyed out with the reason.

  | Preset | netem |
  |---|---|
  | Good cell | delay 25ms 5ms distribution normal |
  | Bad cell | delay 60ms 25ms distribution normal loss 1% |
  | Handover | delay 40ms 10ms loss gemodel 0.5% 30% (burst) |
  | Burst loss | loss gemodel 1% 25% |
  | Saturated | delay 120ms 40ms rate 1mbit |

  Fixed seed via netem `seed` where kernel/iproute2 support it; otherwise the run is marked "not repeatable". Always removed at run end (and at app start, for any qdisc the app left behind).
- **Latency-under-load** (Flent RRUL-style): background UDP/TCP bulk streams to B (B runs a sink) while the trip test runs; report latency loaded vs unloaded.
- Serial-to-IP converter tests (serial on A ↔ UDP/TCP on B) and carrier failover tests (link drop/recovery timing) reuse the same engine.


### 5.5 GOOSE testing — L2 GOOSE and L3 multicast R-GOOSE (Alex 2026-11-03, phase 2)
**No tunnelling in the app.** The Pi hands GOOSE to the cellular router on Ethernet; the router builds the GRE tunnels over cellular. The app only needs correct, fully configurable GOOSE publishers/subscribers on both ends. Both modes are full-fledged and selectable per test/profile.

**Common test model**
- The sending Pi = publisher of the "trip" GOOSE + subscriber of the reply; the Pi it sends to = subscriber + **mirror publisher** (republishes the sender's state in its own GOOSE with its own APPID/gocbRef/goID) → loopback event round trip. The mirror is started **on demand** for the length of a test (an always-on mirror would republish this Pi's own multicast test frames; DECISIONS 2026-11-12). One-way mode (NTP offset check) also supported: the receiving Pi timestamps reception. Direction `both` is refused for GOOSE.
- **Event test**: the sender changes a boolean (stNum+1, sqNum=0) at a set interval / idle gap; latency = A's state change → the other Pi's reception (one-way) or → the sender receives the mirrored change (round trip). Pass/fail on percentile vs threshold (default 60 ms).
- **Retransmission profile** (IEC 61850-8-1): after a change send at T1 (min, default 4 ms), doubling (configurable multiplier) up to T0 heartbeat (default 1000 ms); timeAllowedToLive = 2×current interval (configurable). Fixed-interval-only mode also available.
- **Metrics**: event latency p50/p95/p99/max, heartbeat jitter, lost events, sqNum gaps (lost retransmissions), duplicates, out-of-order, **TAL expiries** (subscriber saw no frame within timeAllowedToLive = stale/link-down, with duration), stNum jumps, confRev/field mismatches, frames from unexpected sources.
- **Pass-through check**: the receiving Pi reports what actually arrived vs what the sender sent (VLAN tag present?, priority, APPID, dest MAC/IP, frame size, test/simulation flag) — shows whether the router/GRE path altered anything.

**L2 GOOSE parameters (both ends, all editable, sane defaults)**
- Interface (auto-detect wired NICs), source MAC (auto), destination multicast MAC `01:0C:CD:01:00:00–01:FF` (default `01:0C:CD:01:00:01`).
- 802.1Q VLAN tag on/off (default on), VLAN ID (default 0 = priority-tagged; configurable 1–4094), priority PCP (default 4).
- EtherType 0x88B8, APPID (default 0x0001 for the sending Pi / 0x0002 for the mirror), reserved1/reserved2 (simulation bit).
- gocbRef, datSet, goID, confRev, ndsCom, test flag, simulation flag, numDatSetEntries; dataset = N booleans (default 8, Mirrored-Bits-like) + optional quality + optional timestamp entry.
- Subscriber filters: dest MAC, APPID, gocbRef, goID, confRev check (on/off), VLAN match.
- Raw socket (AF_PACKET) → needs CAP_NET_RAW (systemd `AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN`); greyed out with reason if missing.

**L3 multicast GOOSE parameters (R-GOOSE, IEC 61850-90-5 / 8-1 Ed2.1 routable GOOSE over UDP)**
- Interface, IPv4 multicast group (default 239.192.0.1 sender → mirror, 239.192.0.2 mirror → sender), UDP port 102 (configurable), source IP (auto), multicast TTL (default 16), DSCP (default 46/EF), IGMP join on subscriber (v2/v3 selectable; source-specific option).
- Session header: SPDU ID, SPDU number (increments), version, key ID / security = none (default); HMAC-SHA256 authentication optional later — the field and UI toggle exist, greyed "not in MVP" unless built.
- Same GOOSE PDU fields as L2 (APPID, gocbRef, datSet, goID, confRev, stNum/sqNum, TAL, test/simulation, dataset).
- Unicast-UDP fallback option (some carrier paths drop multicast) — clearly labelled non-standard.

**Implementation**
- Own small BER encoder/decoder for the GOOSE PDU + R-GOOSE session/SPDU headers (no heavy deps; arm64-safe). A dedicated sender thread with monotonic timing for T1 bursts.
- Frames must decode correctly in Wireshark: tests run `tshark` on captured frames and assert it dissects them as GOOSE / R-GOOSE with the right fields. **Verified in phase 7** (`scripts/e2e-sniff.sh`, tshark 4.0.17): L2 GOOSE and R-GOOSE from both Pis dissect with no malformed packets. The R-GOOSE payload is `CLTP header (01 40)` + the fixed-layout IEC 61850-90-5 session header + payload (length, APDU tag 0x81, simulation, APPID, APDU length, GOOSE PDU) - phase 2's first guess (TLV session header, no CLTP) was wrong and tshark did not recognise it.
- Keep frames small (default < 300 bytes) — GRE adds ~24–28 bytes; show frame size and warn above 1400 bytes.
- UI: Setup wizard step "GOOSE mode: Layer 2 (Ethernet) / Layer 3 (multicast IP)", plain-language labels with the IEC term in a tooltip; presets "GOOSE trip test – L2" and "GOOSE trip test – L3 multicast" (read-only, 60 ms, p99 rule).
- **Local tests**: L2 over a veth pair in two network namespaces (raw frames, VLAN tag on/off), L3 multicast over veth (IGMP join, TTL), retransmission timing asserted (T1 burst then T0), TAL expiry by pausing the publisher, netem delay flips verdict to FAIL; tshark dissection check.

## 6. Profiles

### 6.1 Schema (JSON, `schema_version: 1`)
```json
{
  "name": "DER trip test",
  "description": "Plain-words purpose shown in the preset card",
  "builtin": true,
  "transport": "serial",                 // serial | udp | tcp
  "serial": {"port": "auto", "baud": 19200, "bytesize": 8, "parity": "N", "stopbits": 1},
  "net":    {"peer_host": "", "port": 20500, "dscp": 46},
  "frame": "compact",                    // compact | extended
  "interval_ms": 50,
  "duration_s": 600,
  "timeout_ms": 1000,
  "threshold_ms": 60,
  "idle_gaps_s": [5, 30, 120],
  "idle_repeats": 3,
  "idle_mode": "after",                  // after | interleaved
  "one_way": false,
  "max_clock_offset_ms": 2,
  "rule": {"percentile": 99, "max_loss_pct": 0.1, "include_idle": true, "idle_threshold_ms": 60},
  "impairment": null,                    // netem preset name or null
  "summary_interval_s": 1
}
```

### 6.2 Built-in presets (read-only, "Duplicate to edit")

| Preset | Transport | Key settings |
|---|---|---|
| **DER trip test** | Serial 19200 8N1, compact | 10 ms interval, 10 min, 60 ms threshold, p99, loss ≤ 0.1 %, idle gaps 5 s/30 s/2 min ×3 |
| DER trip test (IP) | UDP, extended | same timing over the radio's Ethernet port |
| Quick link check | UDP | 20 ms interval, 60 s, no idle probes |
| TCP session test | TCP | 50 ms, 10 min, reconnect timing, keepalive on |
| Overnight soak | Serial | 100 ms interval, 12 h, idle interleaved incl. 10 min gap, 10 s summaries |
| Back-to-back baseline | Serial | null-modem, 20 ms, 60 s, threshold 10 ms — measures local Pi + adapter overhead |

19200 baud / 10 ms (release 1.5.6; was 50 ms): 10 ms gives 60,000 samples in 10 min (~700 B/s each way) and leaves radio headroom; on the real Pis latency is set by the baud rate, not the period (see the maintainer's private notes). Mirrored Bits runs 9600–38400 baud and sends continuously. Users can change it in a duplicate.

### 6.3 Rules
- Built-ins live in code, are seeded into the DB with `builtin=1` on start, cannot be edited/deleted (API returns 403), only duplicated ("DER trip test (copy)").
- **Export**: one or many profiles → `traffic-tester-profiles-YYYYMMDD.json`:
  `{"format":"traffic-tester-profiles","schema_version":1,"exported_at":"…","app_version":"…","profiles":[…]}`.
- **Import**: validates format, schema version and every field (unknown fields ignored, missing → defaults, out-of-range → reject with message). Imported profiles are never built-in; name clash → "Name (imported)".

### 6.4 New test: one page in test-set order (1.4.0; replaces the 1.1.2 custom form and the three-step wizard; the maintainer's private notes, Alex 2026-11-12)
- Setup is the list of tests. **＋ New test**, **▶ Run** and **Edit** open one page (`static/newtest.js`) set up from the Pi you are on, four rows that open and close, each closed to a one-line summary:
  ① **Port** — chips for this Pi's real ports (`GET /api/testports`: Ethernet, USB-Ethernet, Wi-Fi, serial; link, address, speed, "you are connected here") plus **Automatic** (default for IP: the port on the far Pi's network, else the Pi's own routing). Ports without a link sit behind "＋ N more ports". Serial adds speed chips 9600 / 19200 / 38400 / Other.
  ② **Protocol** — filtered by the port: serial → Serial, Mirrored Bits; IP → UDP, TCP, Routed GOOSE, Mirrored Bits over UDP / TCP; wired Ethernet also GOOSE (L2). Unavailable ones are greyed out with the reason.
  ③ **From → To** — From is this Pi; To is a tick list of the Pis on this network (radio buttons for serial, GOOSE and Mirrored Bits: one pair only), "Type an IP address" for UDP / TCP, "Add a Pi by IP". Direction: One way / Round trip (default) / Both ways; **⇅ Swap** makes the far Pis send here (many → one). Two or more ticked = one test, one link per far Pi (`Profile.dest` + `dests`, `source` + `sources`, plan.py). Mirrored Bits is round trip automatically. A sentence states the test. The only other Pi (or the one used last) is ticked already.
  ④ **Traffic** — rate, length, pass limit, percentile, loss allowed; **More options** holds the rest (test port, DSCP, frame size, quiet gaps, serial framing, GOOSE fields, bad-link simulation, background load, Mirrored Bits settings).
- **Start bar** (always at the bottom): the automatic check result in one line (re-run on every change; red rows above the bar when something blocks Start), check chips, the optional **Job** line, **← Tests**, **💾 Save** (name + description sheet), **▶ Start**. No "two Pis" wording anywhere.
- Ranges and validation as before (`GET /api/profile-limits`, `custom.js` helpers); a saved test is an ordinary profile.

### 6.5 One list the user controls (1.1.3, Alex 2026-11-12)
Setup step 1 is ONE list of trip tests (built-in and own) that the user fully controls: **edit, rename, hide, reorder, create, delete**.
- **Storage.** A built-in test is a row with a stable `preset_key` (slug of its ORIGINAL name). Defaults live in code (`profiles.builtin_presets`); what the user changed is stored as an **override** (`override_json`: only the fields that differ, deep) next to the row. At every start the row is rebuilt as default + override, so an app update never wipes an edit, untouched fields follow improved defaults, **Reset to default** deletes the override (name and description included) and an override that no longer validates is kept but not applied (`override_error`). Columns `hidden` and `position` (one order for the whole list; a built-in added by an update lands after its predecessor in the default order). Own tests are ordinary rows. Old databases are migrated in place.
- **API.** `PUT /api/profiles/{id}` on a built-in TRIP test stores an override (generator/sniffer presets stay read-only, 403); `POST .../reset`, `.../hide`, `.../unhide`, `POST /api/profiles/restore-hidden`, `POST /api/profiles/order {ids}`; `DELETE` is real only for own tests (built-ins: 403, hide them) and returns the deleted test so `POST /api/profiles/undelete` can put it back in the same place. Names are unique per Pi (built-in rows included, hidden or not); history keeps the name used at run time (`runs.profile_json`).
- **Export / import.** "Export all" adds `test_state` (`hidden` keys, `order`) and exports edited built-ins with their `preset_key`; import turns a built-in entry into an override of the local built-in (never a second copy), applies hidden/order, and still reads old exports (no `preset_key`/`test_state`: built-in entries are skipped, own tests are added).
- **UI.** Tap a card = plain-English info sheet (What it tests / How it works / What you need / What "pass" means / Typical use; text lives with the preset in `profiles.TRIP_INFO`, one source, also in the AI guide; an edited or own test shows its own description and, for an edited built-in, a fold with the original). Card buttons: **▶ Run**, **Edit** (the guided form of 6.4), name tap = inline rename, ⋮ menu (Rename, Duplicate, Export, Move up/down, Reset to default, Hide/Delete; on wide screens Hide/Delete are clear buttons instead, never both). Hide/Delete ask to confirm; both give an Undo toast. "Show hidden tests (n)" lists hidden ones with Show again / Restore all hidden tests. "＋ Create a new test" sits at the top and reuses the 6.4 form.

### 6.6 Mirrored Bits emulator (1.3.0; replaces the 1.1.3 "Mirrored-Bits-style" mode; Alex 2026-11-12)
Source of truth: **`docs/MIRRORED-BITS-SPEC.md`** (his words, verbatim). This section only says how it was built. Code: `traffic_tester/mbemu/` (pure logic + engine), `mbrun.py` (glue to the run manager / reflector), `static/mblive.js` + `mb.css` (live view), `static/mbsettings.js` (form fields).
- **What it is.** One emulator per end of the channel (SEL-2505 style): 8 transmit bits TMB1-8 (= contact inputs IN1-8) and 8 receive bits RMB1-8 (= outputs OUT1-8), no logic between. Both ends send a FULL message every send cycle (never on change only) and check every message they receive. Test name unchanged ("DER trip — Mirrored Bits mode"); `mode: "mirrored"`.
- **Codec (`codec.py`).** A pluggable interface (`encode(bits, id) -> bytes`, `decode(chars) -> bits x copies + id + per-character errors`). Only **SimCodec** ships: 4 characters, char 0 = `101` + 5-bit TX_ID, chars 1-3 = the 8 bits three times. Labelled everywhere "SimCodec — emulator-to-emulator only, not SEL wire-compatible". The SEL layout is NOT in the spec; the file explains how to add it (from SEL's application guide or real captures) as another `Codec` without touching anything else. Nothing claims SEL compatibility.
- **Stream.** Back to back at the baud rate (message time = 4 chars x bits/char / baud: 2.08 ms at 19200, 1.04 ms at 38400, 4.17 ms at 9600) or capped at N messages per power-system cycle (50/60 Hz). Bauds 2400-115200, data/parity/stop configurable (2 stop bits = the extra stop bit), RTS/CTS option.
- **Receive checks (spec 3).** Per message: character errors (termios `PARMRK|INPCK` markers `FF 00 c`, counted as parity when parity is on, else framing: Linux cannot tell them apart in this form; overrun from `TIOCGICOUNT`, "not available" where the driver has no counter), the three copies agree, ID = RX_ID (own TX_ID = loopback, rejected and flagged), inter-arrival <= K x nominal (K default 1.5, never tighter than a floor of 5 ms, a setting: a non-real-time host cannot honour 1 ms). Any bad, late or missing message puts **ROK to 0 at once**.
- **Channel status (spec 4, `status.py`).** ROK re-asserts after N good (default 2, marked **ASSUMPTION** in the UI); lost-channel rule (no valid message for 3 messages' time); **RBAD** after RBAD_PU s of ROK=0; **CBAD** when ROK=0 for more than CBAD_PU % of a rolling window; **LBOK** in loopback test mode. Event log with ms timestamps of every ROK/RBAD/CBAD/LOST/LBOK and every TMB/RMB change; the echo bit is counted, not logged.
- **Received bits (spec 5).** Per-bit PU/DO counts, per-bit default while ROK=0, resume through the counts. The message that re-asserts ROK also feeds the counts.
- **Timing of our own loop.** Time our own loop was late (a stall of the host, not of the link) is not charged to the link (`host_stalls`, `tx_late_max_ms` are reported). A stall of the SENDING host cannot be told from a link gap by the receiver and is charged to the link: run on a quiet host.
- **Impairment injector (`impair.py`).** Between codec and link, per sending end: fixed latency, +-jitter (never reorders: bunching, not overtaking), random drop %, burst outage (length / interval), bit errors, break link. Works on serial and IP; separate from the Linux netem bad-link simulator and usable without it.
- **Echo bit (spec 8).** Default TMB8, reserved (not toggleable, shown as "echo"). Far end: TMB8 = RMB8. Near end: TMB8 = NOT RMB8. Each RMB8 transition = one round trip (only counted while ROK was already 1 and the TMB change was made while the link was up); one way ~ RTT/2; the RTT alarm fires when no transition came within RTT_MAX or the last round trip was longer. PROTO A is the near end by default.
- **Bit transfer time.** The starting Pi flips the test bit (TMB1) every `trip_every_s` (and any IN flip is recorded); with both clocks synced and following the same source, the time to the matching RMB change at the other Pi is measured ONE WAY (`/api/mb/reflect`); otherwise it is estimated as RTT/2 of the echo bit and the result says so.
- **Pass.** Bit-transfer-time percentile (default p99) <= `threshold_ms` (default 60), lost bit changes <= `rule.max_loss_pct`, ROK=0 time <= `max_down_pct` (default 0.1 %), no RBAD, at least one bit made it.
- **Pairing.** The Pi that starts a test is PROTO A with `mirrored.settings`; the Pi that answers is given the mirror image (PROTO B, TX_ID/RX_ID swapped, no scheduled flips, no injected impairment) by the planner (`mbemu/pairing.far_settings`). A PROTO or ID conflict shows a warning on the live screen.
- **I/O (`io.py`).** IN/OUT virtual by default (squares in the UI, `POST /api/mb/tmb`); optional Pi GPIO mapping (`POST /api/mb/io/gpio`, BCM pins, gpiozero) with the 3.3 V warning: real contacts need an opto / relay board.
- **Transports.** Serial (`MBSerialLink`), UDP and TCP serial-over-IP (every message sent at once, TCP_NODELAY), and an in-process pair (`InternalLink`, `Registry.start_selftest`, `engine.selftest`) for the internal back-to-back self-test (Run screen button).
- **API.** `GET /api/mb/live` (this end + the other end through ITS API + warnings), `GET /api/mb/reflect`, `POST /api/mb/tmb|impair|settings|selftest`, `GET /api/mb/events.csv`, `GET|POST /api/mb/io[/gpio]`, `GET /api/runs/{id}/export.csv?kind=mbevents`.
- **Live view.** Two end cards (tap a "Sends" square to flip that bit on that Pi), two lanes of message chips driven by the real traffic (sampled for display; rejected ones red with the reason on tap, missing ones as gaps, the lane greys out when ROK=0), Break link and impairment chips, then Numbers / Charts / Event log / Settings / How it works. Phone portrait stacks the cards with vertical lanes. `requestAnimationFrame`, paused while the tab is hidden; the far end is polled once per 250 ms by the Pi that runs the test.
- **Old results.** Tests saved by the 1.1.3 mode (no `mirrored.emulator`) keep their old result card and PDF section.
- **Load.** See the maintainer's private notes (arm64 / QEMU figures at 38400 both ways).

## 7. Storage, sessions, export

- SQLite (WAL) at `<data_dir>/traffic-tester.db`. Data dir default `/var/lib/traffic-tester` (Pi), `--data-dir` for tests.
- Tables:
  - `profiles(id, name UNIQUE, builtin, json, created_at, updated_at)`
  - `sessions(id, name, created_at, notes)` — a session groups runs (e.g. "Utility site 4, 2026-11-10"). "New session" starts a fresh one; "Clear session" deletes its runs after confirmation.
  - `runs(id, session_id, profile_json, role, transport, port, started_at, ended_at, status, summary_json, verdict, note, group_id, leg)` (the legacy `role` column now holds the name of the Pi that sent; `group_id` ties the directions of one test, `leg` is a JSON label)
  - `messages(run_id, seq, phase, idle_gap_s, t_send_mono_ns, rtt_us, up_us, down_us, status)` — batched inserts once per second.
  - `intervals(run_id, t_offset_s, sent, received, lost, late, p50_us, p95_us, p99_us, max_us)`
  - `settings(key, value)` — `pi_id`, `pi_name`, `setup_at`, `clock_source`, `reflector_ports`, saved peers and their passwords, password hash, SMTP (later).
- Overnight 12 h at 100 ms ≈ 430 k message rows (~25 MB): fine on a Pi SD card.
- **Export**: per run CSV (messages), CSV (intervals), JSON (summary + profile + intervals); per session ZIP of all.
- **Flash drive**: detect mounted removable filesystems under `/media/*` and `/media/*/*` (from `/proc/mounts`); "Save to USB drive" writes the ZIP there and syncs. No drive → button disabled with "Plug in a USB drive".
- **Report builder (PDF built in phase 4; e-mail still later)**: pick any saved sessions/runs → PDF (reportlab: cover, summary table, per-run verdict, tables and charts for trip tests and generator runs; `GET /api/report.pdf?run_ids=…|session_id=…`, "PDF report" buttons in Results); email via SMTP settings entered once (password stored in the DB, readable only by the service user).


#### 6.6.1 Trip frame codec + "DER transfer trip — Orbit radio (2 s budget)" (1.6.0; Alex 2026-11-15, his Claude-AI design "DER Transfer Trip 2-Second Budget")
- **Codec `trip`** (`codec.TripCodec`, chosen per test by `mirrored.codec`; SimCodec stays the default and unchanged). 19 characters: sync+ID (`101`+5-bit ID, shared resync logic), 8 status bits, flags (bit0 = echo valid), 16-bit sequence (wraps 65535 -> 0), send time (sender's monotonic clock, **microseconds, 32 bit**, wraps every 71.6 min; only differences are used), echo of the last good frame heard (seq + its send time) + **hold time** (us, 16 bit, 65535 = too long, no sample), CRC-16/CCITT-FALSE over the first 17 characters. Emulator-to-emulator only, labelled not SEL wire-compatible. One frame at 19200 8N1 = 9.9 ms (about 100/s each way); 4.95 ms at 38400.
- **Receive.** Bad CRC -> reject reason `crc` (counted, ROK drops like any bad frame). Sequence gaps = exact lost frames (also counts frames that arrived damaged); repeats and old frames are counted and ignored (not a sign of life); after 10 min of silence the 16-bit number is ambiguous, so the stream is simply rejoined. After a bad frame the framer assumes a flipped bit; if the next one is bad too it slides one character at a time to the next frame that passes its CRC (no storm of rejects after a lost character).
- **Round trip, no clock sync:** rtt = (now - echoed send time) - hold time, on every new echo, at both ends. Kept in a fixed-size histogram (`LatHist`, 0.05 ms buckets to 2 s, exact min/max/mean, percentiles rounded up): p50 / p95 / p99 / **p99.9** / max for any run length. Echo-bit method is not used with this codec, so TMB8 is free.
- **Security:** RMB PU/DO default 2 in the preset (a bit changes only after two matching good frames in a row); ROK / RBAD / CBAD unchanged.
- **Trip:** the substation Pi flips TMB1 every 2 s; the DER Pi logs when it sees each change (`rmb_changes`). One-way only when the clocks are trustworthy, else round trip / 2, labelled. **GPS one-way: not built** (`Emulator._one_way_hook`; UI/PDF line "one-way needs GPS time").
- **Pass rule:** `rule.percentile` accepts 99.9; with this codec the verdict judges the **round trip** at that percentile against `threshold_ms` (200), plus frames lost % (`max_loss_pct`), ROK down % and RBAD.
- **Long runs.** In memory: histogram + capped lists (event log 100 000, flips/changes 50 000, thinned samples 200 000) = bounded, under about 50 MB. On the SD card nothing is stored per frame: one summary (event log capped at 5000 lines, under 1 MB) + about 1500 chart rows + one roll-up row per minute on runs over an hour (1440 rows a day, about 0.1 MB a day). At 19200 baud about 8.6 million frames a day are summarised into that. The run lives in the app service, not the browser.
- **Preset** `DER transfer trip — Orbit radio (2 s budget)`: serial 19200, mirrored, `trip` codec, PU/DO 2, trip bit every 2 s, RTT_MAX 200 ms, limit 200 ms round trip at p99.9, 24 h; ends named "Substation relay (SEL-351/751/651R class)" and "DER relay (SEL-700GT class)" (`Settings.role_names`). The older "DER trip — Mirrored Bits mode" preset is unchanged.
### 7.5 Mark (1.4.0)
- **⚑ Mark** left of Stop on every test kind: `POST /api/runs/{rid}/mark {name?}` (any link's run id; 409 when nothing runs; at most `marks.MAX_MARKS`). Stored as `summary.marks` `[{n, t_s, name, at}]` on every link; a `mark` event goes to the live screen.
- From a mark on, each state event carries `since` (sent, lost, late, now / p50 / p99 / max) counted from the mark; the Run screen switches **Since ⚑N / Whole test** (the other figure in small print). The verdict is always the whole test; nothing is deleted.
- Shown as a dashed flag on the live and result delay charts, a line on the result and the PDF, and a last `mark` column in the messages / per-second / generator CSVs (the mark in force on that row).

### 7.6 Tester lamp (1.4.0)
- `health.py`: one line under the Run banner — **Pi OK** / **Check the Pi** / **The Pi is the limit** — naming the Pi and the reason: sent < 93 % of the asked rate (judged on the test's own clock, not in its last 1.5 s), sending late, processor > 85 %, receive drops, clocks apart on a one-way test, Wi-Fi power saving, USB adapter hold time 16 ms. Far Pis report theirs (`GET /api/peer/health`). Periods are kept in `summary.tester` and printed on the result and the PDF.

### 7.7 Many links (1.4.0)
- A test with two or more links shows ONE table on Run and at the top of the result: a row per link (Link · Now · p99 · Lost), state dot, worst in red, banner "1 of 2 links over the limit"; tap a row for its tiles (`GET /api/runs/{rid}/live` for any link). A single round trip keeps the tiles.

## 8. Web application architecture

```
Browser (any device on the Pi's network / hotspot)
   │ HTTP + WebSocket /ws/live
FastAPI + uvicorn (asyncio)            ── static SPA (vendored, no CDN)
   ├── REST API  /api/*
   ├── RunManager ── engine thread ── transport (serial / udp / tcp)
   ├── Reflector  ── always-on echo ── transport (UDP/TCP 20500; serial / GOOSE claimed per test)
   ├── Peers      ── mDNS + manual list, HTTP clients to the other Pis
   ├── Store (SQLite)
   └── System helpers: port scan, FTDI sysfs, chronyc, tc/netem, /media
```

- **Stack**: Python ≥ 3.10 (Bookworm ships 3.11), FastAPI, uvicorn, pyserial, pydantic. All pure-Python or arm64 wheels available. Memory target < 150 MB RSS.
- **Engine ↔ UI**: engine thread puts events (per message, per interval, state changes) on a thread-safe queue; an asyncio task drains it, stores batches and broadcasts to WebSocket clients, throttled to ≤ 20 UI updates/s (messages are coalesced).
- **One test at a time** per controlling Pi (409 if busy; the two directions of a `both` test are one test). The reflector always runs alongside, so a Pi can run its own test and answer other Pis' tests at the same time (except where a serial port or raw interface is claimed).

### 8.1 REST API (MVP)
| Method & path | Purpose |
|---|---|
| `GET /api/status` | open: version, `pi {id, name}`, login state; after sign-in also the current run (with `legs`), reflector state, clock, interfaces, hardware, `migrated` (no `role`) |
| `GET /api/peer/info` | open: who this Pi is (id, name, version, ips, clock, busy, password_required, hardware, clock source) |
| `GET/PUT /api/pi`, `PUT /api/reflector/ports` | this Pi's name / always-on answering ports |
| `GET/POST /api/peers`, `DELETE /api/peers/{id}`, `POST /api/peers/refresh`, `POST /api/peers/{id}/password` | Pis on this network: list, add by IP, forget, look again, save its password |
| `GET /api/peers/{id}/ports`, `GET /api/peers/{id}/interfaces` | another Pi's serial ports / Ethernet ports (for the Send from / Send to pickers) |
| `GET/PUT /api/clock/source`, `POST /api/peers/{id}/clock-source` | time source (§2.6) |
| `POST /api/probe` | check a whole test plan before starting |
| `POST /api/peer/reflect/ensure`, `POST /api/peer/reflect/release`, `GET /api/peer/reflect/status` | what another Pi's controller asks of this Pi's reflector |
| `GET /api/ports` | detected serial ports + FTDI latency_timer |
| `POST /api/selfcheck` | adapter check (+ `fix: true`) |
| `GET /api/time` | chrony/timedatectl sync state and offset |
| `GET/POST /api/profiles`, `GET/PUT/DELETE /api/profiles/{id}` | profiles (403 on built-in edit/delete) |
| `POST /api/profiles/{id}/duplicate` | copy to editable profile |
| `GET /api/profiles/export?ids=…`, `POST /api/profiles/import` | JSON file exchange |
| `GET/POST /api/sessions`, `DELETE /api/sessions/{id}/runs` | sessions, clear |
| `POST /api/runs` (profile id or inline profile + overrides; returns `{run_id, run_ids}`, one id per direction), `POST /api/runs/{id}/stop`, `GET /api/runs/{id}` | runs (rows carry `group_id` and `leg`; the summary carries `endpoints`) |
| `GET /api/runs/{id}/export.{csv,json}`, `GET /api/sessions/{id}/export.zip` | export (trip CSVs end with `group_id, leg, sender, answering_pi, basis`; generator CSV with `from_pi, to_pi`) |
| `GET /api/usb`, `POST /api/usb/save` | flash-drive export |
| `POST /api/responder/start`, `POST /api/responder/stop` | alias of the reflector claim (older scripts) |
| `POST /api/update/partner` | send the ready update to another Pi: `{peer}` (from the list) or `{host, port?, password?}` |
| `WS /ws/live` | live events |

### 8.2 Frontend
- No build step: vanilla ES modules + small reactive helpers; **uPlot** (MIT, ~45 KB) vendored under `static/vendor/` for charts. System font stack, no web fonts → works with no internet.
- **Design**: clean card layout, CSS custom properties, automatic dark/light (prefers-color-scheme + toggle), 8-px spacing grid, large touch targets, responsive down to a phone screen (engineers in the field use a phone on the Pi hotspot). Status colours: green pass, amber warning, red fail — always paired with an icon/word, never colour alone.
- **Tabs**:
  - **Setup** — 3-step guided wizard (streamlined 2026-11; was 4 steps with a separate Pre-flight): ① Choose a test (your saved tests first, then the ready-made cards; one click picks and moves on) → ② Connect & check (port or address, auto-detected list; the link check — adapter latency, clock sync, "<name> answered ✓" — runs by itself and again on every change; **Start test** is on this step once the check is clean) → ③ Review & start (optional: "Advanced" disclosure for every profile field, "Save as profile"). Import/Export sit under the cards. The link details of the last started test are remembered in the browser. Every Pi shows the same Setup: step 2 also asks **Send from**, **Send to** and **Direction** (default: this Pi → the Pi you pick, round trip) and the answering Pi needs nothing set up.
  - **Run** — one live screen: big progress (phase, elapsed/remaining), live headline numbers (now / p99 / loss / over-limit), Stop button; when the test ends the verdict appears here with **See full results**, **PDF report**, **Run again** and **Change setup**. Below it (the former Monitor tab; an old `#monitor` link opens Run): live delay chart with red dashed threshold line; scrolling bit view for A→B and B→A (8 lanes, current payload bits); link diagram "Pi A ⇄ link ⇄ Pi B" whose arrows turn amber on late and red on lost, with last-event text.
  - **Results** — session picker with PDF / ZIP / USB export and New session (Rename and Clear under "More"), run list with PASS/FAIL badges on top (the open test is highlighted); run detail below it: huge verdict banner with plain-sentence reasons, metric cards, an export row for this test (PDF, CSV, JSON), latency histogram + percentile chart, idle-first table, interval chart, export buttons, "Save to USB".
- Every technical term has a ⓘ tooltip in plain words (p99: "99 out of 100 messages were at least this fast").
- Next action is always the one primary button on the screen.

## 9. Install & deploy (Raspberry Pi 5, Raspberry Pi OS 64-bit, Bookworm or newer)

- Pi 5 only supports Bookworm or newer; 32-bit is out of scope. Power: official 5 V / 5 A USB-C (27 W) — on 3 A the USB ports are capped at 600 mA total and USB-serial adapters may misbehave. `config.txt` lives in `/boot/firmware/`. The installer checks `/proc/device-tree/model` for "Raspberry Pi 5" (warn, never block, on other models) and refuses Bullseye or older.

- `scripts/install-pi.sh [--port 8080] [--uninstall]`, idempotent (the same installer on every Pi; the old `--role A|B` is still accepted and ignored):
  - apt: `python3-venv python3-pip chrony iproute2 socat`.
  - App to `/opt/traffic-tester`, venv `/opt/traffic-tester/venv`, `pip install -r requirements.txt`.
  - System user `traffic-tester` in `dialout`; data in `/var/lib/traffic-tester`; config `/etc/traffic-tester/config.env` (`TT_PORT`; an old `TT_ROLE` line is ignored).
  - udev rule for FTDI `latency_timer=1` (`/etc/udev/rules.d/99-traffic-tester-ftdi.rules`), reload + trigger.
  - systemd `traffic-tester.service` (`Restart=on-failure`, `AmbientCapabilities=CAP_NET_ADMIN CAP_SYS_NICE` for netem + realtime priority).
  - chrony `allow` for local subnets on every Pi, so any Pi can serve time; which Pi does is decided in Settings → Time source (§2.6).
  - Prints `Open http://<pi-ip>:8080`.
- Default web port **8080**. Optional simple local password (Settings; scrypt hash; cookie session). Off by default; UI shows a hint when off.

## 10. MVP scope vs later phases

(Traffic generator: built in phase 4, see §13.)

| MVP (phase 2) | Later |
|---|---|
| Serial loopback trip test, compact + extended frames | DNP3, Modbus RTU/TCP |
| UDP + TCP transports, TCP setup/reconnect timing | PDF e-mail (SMTP) (PDF itself: phase 4) |
| Metrics, idle-first stats, pass/fail | Latency-under-load (if not reached in MVP) |
| Web GUI 4 tabs, live WebSocket | Serial-to-IP converter + failover wizards |
| Sessions, profiles, presets, import/export, CSV/JSON, USB save | Password protection (if not reached in MVP) |
| FTDI self-check + baseline preset | |
| GOOSE L2 + L3 multicast (R-GOOSE) testers, §5.5 | |
| One-way mode with offset check; netem presets (if sudo) | |

## 11. Local test plan (this PC, before any Pi)

- **Unit (pytest)**: frame encode/decode/CRC/resync, seq unwrap, percentiles/jitter/classification, pass/fail rule, profile validation + export/import round-trip, built-in read-only.
- **E2E** `scripts/e2e-local.sh` (rerunnable, cleans up):
  1. Two instances: A on port 18080 (`--data-dir /tmp/tt-a`), B on 18081 (`/tmp/tt-b`).
  2. Serial: `socat -d -d pty,raw,echo=0 pty,raw,echo=0` → the second app instance is told to answer on pty 1, the first runs a DER-style test on pty 2 via the HTTP API; assert results stored, ~0 loss, idle-first rows present, verdict PASS.
  3. UDP and TCP on 127.0.0.1 (ports 18500+); TCP: kill/restart B's listener mid-run → reconnect time recorded.
  4. Impairment: network namespaces + veth pair (`sudo -n ip netns …`), netem delay 80 ms on the veth → over-threshold count > 0 and verdict flips to FAIL; then removed. If netem is impossible, an artificial-delay hook in B (enabled only by env `TT_TEST_HOOKS=1`) is used and the substitution is recorded.
  5. Headless browser screenshots of each tab during a live run → `docs/screenshots/`.
- **arm64**: QEMU binfmt + `debian:bookworm --platform linux/arm64`: run install steps, start app directly, hit the API, run unit tests and a pty loopback test; evidence includes `uname -m` = `aarch64`.
- **Real Pi** (after delivery): back-to-back baseline over null-modem, then the radio link.
- Results recorded in the maintainer's private notes (requirement | pass/fail/unresolved | evidence).

## 12. Open questions (need Alex)
- 5G radio RJ45 serial pinout (affects cable choice; not app code).


## 13. Traffic generator (phase 4 — built 2026-11-03)

**Purpose.** Ostinato-style load for the same Pis: custom streams between any two Pis (or several Pis at once, N → M) with set rate, size and bursts, measuring throughput, loss, latency and jitter with a plain PASS/FAIL. Used to answer "does the 5G / FirstNet link carry my traffic, and what does it do to latency when it is busy?" (and with the trip test running at the same time, latency under load).

**Why our own generator (not Ostinato Drone).** Ostinato went closed source in 2024 (repo archived 14 Apr 2024); work use needs a paid subscription (Pro $79 / Business $249 per month, per Drone), Linux support is listed only for RHEL/Ubuntu LTS (no Raspberry Pi OS arm64), the last GPL code would force us to open our app if bundled, and an external agent cannot share our NTP-synced timestamps, sessions and reports. See DECISIONS 2026-11-03. iperf3 (BSD) is an optional cross-check only.

**Streams** (several per test; each has a name and a stream id 0-255):
| Type | What is sent | Needs |
|---|---|---|
| `l2` | raw Ethernet frame: dst/src MAC (src auto), EtherType (default 0x88B5), optional 802.1Q VLAN id/priority | CAP_NET_RAW |
| `ip` | IPv4 with a custom protocol number (default 253), DSCP, TTL | CAP_NET_RAW |
| `udp` | UDP datagrams to dst port (default 20600+stream id), DSCP, TTL | none |
| `tcp` | one TCP connection, length-framed records, DSCP | none |

Per stream: **rate** (`pps` or `mbps`, counted on the Ethernet frame without FCS), **size** (`fixed`, `range` min-max seeded random, or `imix` 7×64 / 4×594 / 1×1514 B (the classic 64/594/1518 mix counted without the 4-byte frame check; 1514 is the largest frame a 1500-byte MTU carries — larger frames get a warning)), **bursts** (`burst_packets` N and optional `burst_gap_ms`: N packets back to back, then the gap; with gap 0 the rate is smooth), **duration or packet count**, **start offset**, and optional **echo probes** (`rtt_probe_pps`, default 10) for round-trip time.

**Frame.** 24-byte header — magic `0x5447`, version, flags (bit0 echo request, bit1 echo reply), stream id, length (u16, TCP framing), sequence (u64 per stream), tx time (u64 CLOCK_REALTIME ns) — padded with zero bytes to the chosen size (the padding carries no pattern and is not checked; TCP records of mixed sizes can hold left-over header bytes there). Minimum size: 60 B for raw Ethernet and raw IP, 66 B for UDP (Ethernet + IP + UDP headers + our 24), 24 B for a TCP record.

**Orchestration.** The Pi you are on (the controller) runs the test and drives every other Pi taking part through that Pi's own HTTP API (`/api/gen/node/...`). Each stream has its own **sender** (`src`) and **receiver** (`dst`), each an Endpoint (§2.4): a Pi from the list, this Pi, or a typed address. A stream left blank takes its ends from the test's Send from / Send to and the older `direction` (`a_to_b` = the test's from Pi sends, `b_to_a` = the to Pi sends; a two-way test is two streams). Per Pi the controller learns its address, MACs and clock, then starts every receiver, then every sender, records per-second counters on each Pi, polls their live counters, and at the end stops senders first (so the packets still in flight are not counted as lost), then everything, and fetches each Pi's results. A user Stop ends the senders everywhere before the receivers. Data-plane addresses are chosen per pair from the Pis' own addresses (the same subnet first); a typed address that is not in the list is only ever a receiver and needs its web address in `gen.peer_api` (and `gen.peer_password` if it has one). Pi passwords come from the saved per-peer passwords (§2.7).

**Measured.** Per stream (with its `src` and `dst` names), per direction and **per pair of Pis** (`summary.pairs`: from, to, streams, sent / received packets, lost, Mb/s, jitter, delay; `summary.nodes` lists the Pis taking part and `clocks.pairs` says for which pairs the one-way delay is valid): tx/rx packets and bytes, pps, Mb/s (frame bits), **loss** (sequence gaps; final loss = sent − unique received), duplicates, out-of-order, **one-way latency** min/avg/p50/p95/p99/max (only for a pair whose two clocks are synced within `max_clock_offset_ms` and follow the same time source, §2.6; the offset is shown with the numbers), **jitter** (RFC 3550 transit-time variation; independent of clock offset), **round trip** from echoes (valid on the sender's own clock). Percentiles come from a 1 µs–10 s log histogram (≈3 % resolution). Per-second rows feed live charts, the stored run and the PDF.

**Pass/fail** (`gen.rule`, each optional): max loss %, max latency (ms, on a chosen percentile, default p99), max jitter ms, min throughput (Mb/s, total received). Plain-sentence reasons like the trip test.

**Presets** (read-only, duplicate to edit): *Mirrored-Bits-like small frames*, *SCADA / DNP3 polling*, *Video / CCTV stream*, *Fill a 5G link* (max UDP), *IMIX*, *Burst stress*, *L2 priority frames*.

**Performance.** Fast path = batched `sendmmsg` / `recvmmsg` through ctypes (GIL released in the syscall), pre-built packets with in-place header patching, batch ≈ 1 ms of packets, sleeps (never spins) between batches so receiver threads keep the GIL, large receive buffers (`SO_RCVBUFFORCE`, installer raises `net.core.rmem_max`). Measured on the development PC (13th-gen Core i7, two separate app processes over a veth pair, UDP A→B, 8 s runs): **1,400-byte frames 2,500 Mb/s requested = 2,500 Mb/s sent and received (223k pps, no loss)**; **66-byte frames 300k pps sent = received (158 Mb/s, no loss)**; at 200-byte frames the Python ceiling shows: 538k pps sent, 434k pps received (19 % loss at the receiver). That is a PC, not a Pi 5: a Pi 5 core is several times slower, so expect roughly an order of magnitude less for small frames and a few hundred Mb/s to ~1 Gb/s for large frames — **must be measured on the real Pi 5** (TODO). Small frames at gigabit line rate (1.49 Mpps) are beyond a pure-Python generator; the follow-up would be a small C helper. Frame sizes exclude the 4-byte FCS, so the largest frame for a 1500-byte MTU is 1514 bytes (IMIX uses 1514; larger frames warn).

**API.** `POST /api/gen/node/rx/start`, `POST /api/gen/node/tx/start`, `POST /api/gen/node/senders/stop` (stops only the senders, so receivers keep counting what is still in flight), `POST /api/gen/node/stop` (one stop for senders and receivers; it returns the node's results), `GET /api/gen/node/live?since=N` (live counters and the per-second rows from row N), `GET /api/gen/node/status` (node control, used by the other Pi); runs start through the normal `POST /api/runs` with a `kind: "generator"` profile; results are normal runs (`transport: generator`) with per-second rows in `gen_intervals`; `GET /api/runs/{id}/export.csv?kind=gen` and `GET /api/report.pdf?run_ids=…|session_id=…` (summary table + charts for trip and generator runs).

**UI.** A fifth tab **Traffic generator**: preset cards → stream table editor → "Start", live throughput / loss / latency charts, result banner, and the run is listed under Results like any other.

**Not in this phase.** iperf3 wrapping, line-rate small frames (needs a C helper), PDF e-mail (SMTP), per-stream VLAN stacks / IPv6 / custom payload editors.


## 14. Traffic sniffer (phase 5 — started 2026-11-03)

**Purpose.** Look at what really crosses the link: a passive "wire tap" built from the same Pi 5. Four sources, one UI tab, captures saved as **pcapng** that open in Wireshark, plus a decoded DNP3 / Modbus RTU view for serial lines. Never part of the traffic path unless the Pi is deliberately placed inline as a transparent bridge.

**Sources**
| Mode | How it is wired | Capture |
|---|---|---|
| **Ethernet inline tap** | Pi sits between two devices: onboard `eth0` ⇄ USB-3 gigabit adapter, joined by a Linux bridge (`br0`: no IP, STP off, forwards LLDP/other link-local frames, offloads off). | `dumpcap` on `br0` (each frame once) or on both legs (pcapng interface ids per leg). |
| **Ethernet SPAN / mirror port** | A managed switch mirrors a port to one Pi interface (no bridge). | `dumpcap` on that interface. |
| **Serial RS-232 / RS-485** | Passive tap: one USB-serial adapter per line direction, each listening only (RS-485 2-wire: one adapter hears both directions; 4-wire: two). | Python reader, per-byte timestamps, framing, decoders; pcapng + raw + CSV. |
| **Wi-Fi monitor** | USB adapter with an in-kernel monitor-mode driver (the Pi's built-in chip cannot); channel / width select, optional channel hop. | `dumpcap` on the monitor interface (radiotap pcapng). |

**Capture engine.** Ethernet and Wi-Fi are captured by **`dumpcap`** (Wireshark's capture engine: libpcap, pcapng, BPF `-f`, ring buffer `-b filesize/duration/files`) as a child process — never per packet in Python. The web app only supervises (start/stop, stats, disk-space guard). A second best-effort `dumpcap` stream with a short snaplen feeds the **live view** (the app decodes at most a few hundred packets per second and shows "showing 1 in N" when sampling; the file capture is never affected). Fallback: if `dumpcap` is missing, `tcpdump` (pcap, not pcapng, plain ring) is used and the UI says so. A capture without a ring buffer stops itself (`dumpcap -a filesize`) before the free-space guard would be crossed; a ring buffer larger than the free space is refused with a plain message.

**Serial tap details**
- Each adapter is read with non-blocking I/O; every chunk is stamped with the host clock when `read` returns. **Timestamp accuracy (honest):** a USB-serial adapter delivers data in USB frames (1 ms) after its `latency_timer` (set to 1 ms by the installer) — expect ±1–2 ms on when a byte was *delivered*, not when it was on the wire; the position of bytes inside one chunk is not known, so inter-character gaps below ~2 ms cannot be seen. Modbus RTU t3.5 is 4 ms at 9600 baud but only 1.75 ms (fixed by the standard) above 19200 baud, so framing uses gap **and** validity: a candidate frame must pass its CRC; the framer also re-synchronises by CRC scan inside a chunk.
- **DNP3 CRC:** the link-header CRC covers all 8 header bytes **including** the `05 64` start bytes (an earlier version left them out and would have rejected real devices; `tshark` as an independent dissector caught it). Data-block CRCs cover the 16-byte blocks.
- **Framing:** Modbus RTU = gap (default `max(3.5 characters, 4 ms)`, adjustable) + CRC-16 validation / resync; DNP3 = `0x05 0x64` start, length field, header CRC and a CRC per 16-byte data block (CRC-16/DNP), resync on the next start bytes.
- **Decoders:** *DNP3* link header (direction, primary/secondary, function, addresses), transport byte (FIR/FIN/sequence), application header (FIR/FIN/CON/UNS/SEQ, function code such as read/write/response/unsolicited, IIN bits) and a list of object headers (group, variation, qualifier, range). *Modbus RTU* address, function (1-6, 15, 16, 23 and exceptions), start address / quantity / byte count, values, exception code, request vs response (from the direction channel; heuristic for single-channel RS-485), CRC status. Everything that fails a CRC is kept, flagged and counted.
- **Wireshark compatibility.** The pcapng wraps each decoded serial frame so Wireshark decodes it with no setup: DNP3 frames as TCP port 20000 (Wireshark's DNP3-over-UDP dissector is off by default, found with tshark), Modbus RTU frames (CRC stripped) as Modbus/TCP on port 502 (MBAP header), raw bytes as UDP 20001, inside a synthetic Ethernet/IPv4 header with fixed private addresses per channel; the packet comment (`opt_comment`) carries the channel, original CRC status and byte timestamps. A second optional file uses `LINKTYPE_USER0` (147) with the raw RTU bytes for people who map DLT_USER0 → `mbrtu` in Wireshark. Raw bytes (`.bin` with timestamps) and a CSV (time, channel, hex, decoded text) are always written.

**Common**
- **BPF capture filters** with a helper: presets "only DNP3" (tcp or udp port 20000), "only Modbus TCP" (tcp port 502), "only GOOSE" (ether proto 0x88B8), "this IP", "this MAC", "ARP", "VLAN n", "not the Pi's own web page", or any custom expression; every filter is compiled (`tcpdump -d`) before the capture starts and errors come back in plain words.
- **Ring buffer / rotation:** rotate by file size, by time, keep the newest N files (dumpcap native; the same three rules in the app's own pcapng writer for serial captures).
- **USB flash drive:** auto-detected (`/media/*`); a capture can be written straight to it; free-space guard (refuse below 50 MB, stop gracefully when the free space falls under the guard); "Copy to USB" for finished captures.
- **Sessions / reports:** a capture is a normal stored run (`transport: sniffer`, files listed in its summary), appears in Results and the PDF report: summary, duration, packets/bytes, rates, top talkers, protocol counts, DNP3 / Modbus counts, CRC failures and malformed frames, timing figures (first/last packet, max gap), dropped-packet counters from dumpcap.
- **Presets:** *Inline Ethernet tap*, *SPAN port*, *RS-485 Modbus listen*, *RS-232 DNP3 tap*, *Wi-Fi channel scan* (hops 2.4 GHz / 5 GHz channels and lists the networks seen: SSID, BSSID, channel, signal). Presets are read-only; saved profiles export/import like all others.
- **Privileges.** The web app never runs as root. Capture needs `CAP_NET_RAW` and the bridge/Wi-Fi setup needs `CAP_NET_ADMIN`: the systemd service already carries them as *ambient* capabilities (inherited by `dumpcap`), and the installer additionally `setcap`s `dumpcap` and adds the service user to `wireshark` and `dialout`. Packages: `tshark` (dumpcap + tshark), `tcpdump`, `iw`, `ethtool`, `iproute2`.

**API.** `POST /api/sniff/preflight` (tools, interfaces, adapters, BPF compile); runs start through `POST /api/runs` with a `kind: "sniffer"` profile; live: events over `/ws/live` (`sstate`, `spkt` batches, `sserial` byte chunks) and `GET /api/sniff/live` (sampled packet list), `GET /api/sniff/packet/{n}` (decoded detail); downloads `GET /api/runs/{id}/capture/{name}`; `POST /api/runs/{id}/capture/usb` (copy to flash drive).

**UI.** A sixth tab **Sniffer**: preset cards → source settings (interface / adapters / channel) with a plain "how to connect" description → filter helper → storage (ring, USB) → Start; live view with counters and a pps / Mb/s chart, packet list + decoded detail (DNP3 / Modbus) and a hex-bytes view for serial lines; Stop → result card → Results / PDF.

**Extra hardware needed (checked 2026-11-03; the prices are listings, not quotes):**
| Item | Model (starting point) | Rough price | Why / caveat |
|---|---|---|---|
| USB 3 gigabit Ethernet adapter | any RTL8153-based (e.g. TP-Link UE300; ASIX AX88179 also works) | $10–25 | second NIC for the inline bridge; in-kernel `r8152` / `ax88179_178a` drivers (generic RTL8153 adapters seen at about $10–16). Plug into a blue USB 3 port. |
| Monitor-mode Wi-Fi adapter | Alfa AWUS036ACM (MT7612U, in-kernel `mt76x2u`) | $41–47 plus shipping | the Pi 5 built-in Wi-Fi cannot monitor with the stock driver (experimental Nexmon option: §14.6); reviews report out-of-the-box use on Pi/Kali. Optional: AWUS036AXML (MT7921AU, Wi-Fi 6E): in-kernel `mt7921u` needs kernel 5.18 or newer (Bookworm 6.1 is fine) and firmware, but only passive monitor works (active monitor reported broken) and one community list dropped it in Nov 2024 over Bluetooth issues — not the first choice. |
| RS-232 tap | 2 x USB-RS232 adapters with real RS-232 levels (FTDI chip) + 1 DB9 breakout / gender changer with screw terminals | about $25–35 each, about $10 | one adapter hears each direction (wire each line's data pin to pin 2 of its adapter, grounds common); **never** feed RS-232 levels (±3–15 V) into a 3.3 V / 5 V TTL FTDI board. FTDI because the installer's `latency_timer=1` fix applies. |
| RS-485 listener | 1 x FTDI USB-RS485 adapter (e.g. USB-RS485-WE-1800-BT) | about $30–37 | 2-wire half-duplex: one adapter hears both directions; 4-wire needs two. An isolated version is recommended in substations. |
| Switch with port mirroring | Netgear GS305E (5-port) or GS308E (8-port) | about $22–40 | Netgear's datasheet lists port mirroring (many-to-one: 4 source ports on the GS305E, 7 on the GS308E) for the SPAN mode. |
| Power / USB | official 5 V / 5 A USB-C supply; a powered USB 3 hub if the 4 ports run short | — | USB-serial + NIC + Wi-Fi draw from the Pi's USB budget; the Pi 5 caps USB at 600 mA on a 3 A supply. |

**Hardware-only verification (cannot be done on a PC):** real USB-NIC inline throughput and offload behaviour, link-state passthrough on real PHYs, Wi-Fi monitor mode / channel hopping, FTDI timestamp accuracy on the wire, passive RS-232/RS-485 tap electrical behaviour, SPAN mirroring on a real switch.

**Built and checked (2026-11-03).** Ethernet/Wi-Fi capture = `dumpcap`; inline tap = Linux bridge (`ttbr0`: no IP, STP off, `group_fwd_mask 0xfff8` so LLDP and other link-local frames cross, offloads off, optional link-state passthrough by taking the opposite port down/up and probing); serial tap = Python; all of it driven as an unprivileged user with ambient capabilities. Checked with the real tools in an isolated Debian Bookworm container (dumpcap/tshark 4.0.17, veth + namespaces) — see the maintainer's private notes Not verifiable without hardware: see the list above.

**Mind the management connection.** The inline tap uses two wired ports; open the Pi's web page over Wi-Fi (or a third port). Pre-flight refuses to bridge the port your browser is using.

### 14.6 Built-in Wi-Fi monitor mode with Nexmon + Ethernet L2 capture (phase 6 — built 2026-11-03)
**Goal.** Sniff 802.11 with the Pi 5's own chip (Cypress/Infineon CYW43455, Linux driver `brcmfmac`) as an *experimental* option next to the reliable USB adapter (Alfa AWUS036ACM / AWUS036AXML stay the recommended hardware). Output is the same radiotap pcapng as the USB path, so presets, filters, ring buffer, Results and PDF work unchanged.

**What was verified (2026-11-03, arm64 containers against the real Raspberry Pi OS Pi 5 kernel headers) — and what was not.**
- Kali's `brcmfmac-nexmon-dkms` 6.12.2 (contrib) and `firmware-nexmon` 0.2 (non-free-firmware) *do not build as shipped* on Raspberry Pi OS Bookworm `6.12.109+rpt-rpi-2712`: (1) the `.set_monitor_channel` callback has a different signature than the source assumes (its version test, `< 6.13`, is wrong for Pi kernels), (2) `SDIO_DEVICE_ID_BROADCOM_CYPRESS_43752` is missing from the Pi headers. With our small patch (`scripts/pkg/nexmon-patches/0001-pi-os-6.12-build-fixes.patch`: the Makefile detects the callback signature from the kernel's own `cfg80211.h`, and defines the missing ID) the module builds cleanly with the right `vermagic` for **18 Pi 5 kernels**: Bookworm 6.12.19 … 6.12.109 (11) and 6.6.20 … 6.6.74 (7) — table in `docs/nexmon-build-matrix.txt`. Not built: Trixie kernels 6.18.x.
- The Kali firmware package's `cyfmac43455-sdio-standard.bin` differs from Raspberry Pi OS's stock file (Nexmon-patched; the `minimal` one is the stock one); we ship it as `cyfmac43455-sdio-nexmon.bin`.
- **Not verifiable without a real Pi 5 (listed in TODO):** module load and firmware start on the real chip, creating the monitor interface (the stock/unpatched path returns error −524 `ENOTSUPP`; with Nexmon `iw dev` must show `type monitor`), channel setting on 2.4/5 GHz, capture quality, injection, and the restore path on real hardware. Unit tests use a fake-tool harness for every helper branch; the install was tested offline in an arm64 container; units pass `systemd-analyze verify`; the permission rule was evaluated in a JS engine (12 cases).
- Fallback if it does not work on a given Pi/kernel: use the USB adapter, or a Kali Linux Pi 5 image (where this driver/firmware pair is native), or build once on the Pi (`nexmon/build/build-on-pi.sh`).

**Design (safety first: the Pi must never be left without Wi-Fi/network).**
- The **stock driver and firmware are never replaced on disk.** The Nexmon module sits in `/opt/traffic-tester/nexmon/<kernel>/brcmfmac.ko` and is loaded with `insmod` only while sniffing (stock modules `brcmfmac*` are unloaded first). The Nexmon firmware is `cyfmac43455-sdio-nexmon.bin`, registered as a *priority-5 alternative* of `cyfmac43455-sdio.bin`, selected with `update-alternatives --set` only while sniffing; `--auto` returns the stock file. (Kali's own DKMS package would install a driver into `/updates` that shadows the stock one for every boot — deliberately not used; deviation from the job text, DECISIONS.)
- **Privilege.** The web app never runs as root. A root-owned Python helper (`/opt/traffic-tester/sbin/tt-wifi-helper`, stdlib only, own copy so an app edit can never run as root) is started by systemd as `tt-wifi-helper@<command>.service`; a polkit rule lets *only* the service user start *only* `status|monitor|channel|restore|inject-test`. Requests are a small JSON file (strict validation: `wlanN`, channel digits, fixed widths); the helper's state/results live in root-owned `/run/tt-wifi`, the app's request + heartbeat in `/run/traffic-tester` (so an app-owned symlink can never redirect a root write).
- **Switching on** needs the explicit toggle (`wifi.builtin_ack`) and the pre-flight refuses if the browser is connected over Wi-Fi. Sequence: select Nexmon firmware → unload stock → `insmod` → wait for `wlan0` → check the kernel log (marker) for firmware errors → `iw dev wlan0 interface add wlan0mon type monitor` (fallback `airmon-ng`) → set channel/width → capture with `dumpcap` on the monitor interface (radiotap).
- **Failure detection** (each gives a plain message and an automatic restore): module missing for this kernel, module load error, firmware crash/timeout lines in the kernel log, interface never appears, monitor interface not created, timeouts (120 s).
- **Restore** (`tt-wifi-helper restore`, idempotent): remove the monitor interface, unload the Nexmon module, select the stock firmware, load the stock modules, restart NetworkManager/`wpa_supplicant`, bring `wlan0` up. Triggers: the one-click **Restore normal Wi-Fi** button, capture stop/crash, a **watchdog** timer every 10 s (restores when the app's heartbeat file is older than 45 s while in monitor mode), and a boot-time unit (`tt-wifi-boot-restore.service`, before NetworkManager) that resets the firmware choice if the Pi rebooted while sniffing.
- **Injection** is optional and experimental: *Test packet injection* runs `aireplay-ng --test` on the monitor interface and reports plainly; never part of a capture.
- **Prefer USB:** the UI's default "Automatic" uses a USB adapter with monitor mode if present; built-in is chosen only when selected.

**API.** `GET /api/wifi/builtin/status`, `POST /api/wifi/builtin/restore`, `POST /api/wifi/builtin/inject-test` (409 with a message when it fails); profiles: `sniff.wifi.interface: "builtin"`, `sniff.wifi.builtin_ack`. **UI:** Sniffer → Wi-Fi: adapter list has *Built-in Wi-Fi of the Pi (Nexmon) — experimental*; selecting it shows the warning, the confirmation tick, the status line, *Restore normal Wi-Fi* and *Test packet injection*; preset *Wi-Fi monitor, Pi's built-in Wi-Fi (experimental)*.

**Ethernet at layer 2 (confirmed complete).** *Inline tap* (Linux bridge `ttbr0`, no IP, link-local frames forwarded, VLAN tags kept, optional link-state passthrough, capture on the bridge or both legs) and *SPAN/mirror* both write pcapng with full Ethernet frames, are selectable as presets next to Wi-Fi (cards at the top of the Sniffer tab; the *Connection* section changes per mode) and are covered by the real-tool e2e (the maintainer's private notes: SNIFF rows).

**Offline package (Google Drive) - DONE 2026-11-03.** `scripts/pkg/build-offline-package.sh` assembles `pi-traffic-tester-offline-<version>-<date>.zip` = `INSTALL-OFFLINE.txt/.md` + `SHA256SUMS.txt` + the `.tar.gz` (Windows opens the zip natively; the tar.gz keeps Linux permissions and needs no `unzip` on the Pi). Inside: `app/`, `apt-bookworm/` + `apt-trixie/` (local flat apt repos: every arm64 `.deb` of the installer's packages with a transitive dependency closure, plus the base upgrades that version-locked packages need), `wheels/` (aarch64, Python 3.11 + 3.13), `nexmon/` (prebuilt drivers for 23 Pi 5 kernels: Bookworm 6.6/6.12, Trixie 6.18; firmware; patched source + `build-on-pi.sh`), `install-offline.sh`, `SHA256SUMS`. `install-pi.sh --offline <folder>` picks the repo by `VERSION_CODENAME`, refuses 32-bit OS and unknown releases with a plain message. Pi 4 installs the tester (Nexmon is Pi 5 only). Claude Code is not bundled (proprietary licence, updates): installed once over Wi-Fi with `--with-claude`.


## 15. Claude Code integration (phase 8 — built 2026-11-03)

**Purpose.** Put Claude Code on the Pi so an engineer can ask "why did this fail?", have the app operated by chat, evolve the app, and reach the Pi from a phone or a Windows PC — **without ever making the tester depend on it**. The tester works 100 % without it and 100 % offline.

**Hard rules**
1. **Default OFF.** After install nothing is signed in and every Claude endpoint answers `403 "Claude Code is off…"`. State is `<data>/claude/state.json` (`enabled`, `remote_control`).
2. **No API key, ever.** Sign-in is the user's own claude.ai subscription (Max) through `claude auth login --claudeai`; the app shows the link, the user pastes back the code. Process environment strips `ANTHROPIC_*` / `CLAUDE_CODE_*`.
3. **Credentials live in one folder** (`<data>/claude/{config,home}`, mode 700, service user) — never in the app folder, git, installer package or config file.
4. **Log-out wipes and verifies.** CLI `auth logout` → `enabled=false` first (endpoints refuse from that instant) → Remote Control off → delete config/home (and, for the service user, `~/.claude*`) → strip token lines from `/etc/traffic-tester/config.env` → **filesystem scan** for `sk-ant-(oat|ort|api|sid)…`, OAuth JSON keys and token env lines. Result `{verified, leftovers}` is shown in the UI, logged, and unverified leftovers are listed rather than hidden. Log-out needs **no** password; log-in needs the web password (and refuses when none is set).
5. **Never blocks or slows tests.** `claude -p` runs in its own session, `nice 19` + `SCHED_IDLE`, only when the user sends a message; internet is probed only then (2 s timeout). Offline / not signed in / not installed / busy / timeout each give one plain message.

**Pieces** (`traffic_tester/claude/`): `env` (paths, binary lookup, contained process env), `state` (on/off, status), `auth` (pty login flow, log-out), `scan` (leftover finder), `runner` (headless chat → events), `tools` + `mcp_server` (what Claude can see/do), `workspace` (scratch git copy, diff/apply/revert), `remote` (Remote Control switch), `cli` (status/login/logout). Plus `applog.py`, `netcheck.py`, `scripts/net-split.sh`, `scripts/install-claude.sh`, `scripts/tt-rc-run`, `scripts/tt-rc-sync`.

**Ask Claude (sidebar).** Resizable **right sidebar**, not a modal: header button opens it, the main screen stays on the left and fully usable. Default ≈30 % of the window (320–480 px), drag handle on its left edge (also ←/→ keys), min 300 px, max min(760 px, 62 % of the window), width and open/closed remembered; under 700 px wide it becomes a full-width overlay. Chat messages, **action lines** (✓/✕ + result for every tool call), a **diff card** with Apply / Discard, and an off-state ("Claude Code is off" + Log in).
- *Read:* `get_screen` returns a structured snapshot the page reports (`POST /api/ui/state`: tab, selected profile, unsaved overrides, running/link/last event, finished status, visible text) plus live state; also `get_status`, `list_profiles`, `get_profile`, `list_runs`, `get_run` (readable digest), `get_log`, `network_check`.
- *Operate:* `open_screen`, `start_test`, `stop_test`, `save_profile` — same validation as the UI. Each pushes a `uicmd` WebSocket event so the left screen switches to the live Run view / refreshes the profile list immediately.
- *Change the app:* Claude may edit only the **scratch git clone** (`<data>/claude/work`, tools Read/Grep/Glob/Edit/Write only, `--restricted`, no shell). The user sees the diff; **Apply** commits it to the live app's git repo as "Pi Automation Traffic Tester (Claude)" and restarts the service (exit 75 → systemd restarts); refused while a test runs. **Undo** = `git revert` of that commit. Protected paths (`traffic_tester/claude/`, `scripts/`, `docs/INSTALL-PI.md`, `.git/`) cannot be applied, and are root-owned on the Pi.
- *Tool endpoint:* `POST /api/claude/tool/<name>` accepts only localhost + a per-process random token held by the spawned Claude process, and only while enabled.

**Network split.** Wi-Fi = management/internet (web UI, Claude, apt); Ethernet test ports never the default route: NetworkManager `ipv4.never-default yes` / `ipv6.never-default yes` on wired profiles, or dhcpcd `nogateway` per `eth*/en*`. `netcheck` reads `ip route`: PROBLEM if a default route or the route to the internet leaves via a test port or a test peer is routed via Wi-Fi. No default route at all is OK (offline field use). API `GET /api/net/check`, Settings → Network, CLI `python -m traffic_tester.netcheck` (exit 0/1/2).

**Remote Control.** `claude remote-control` in tmux as `traffic-tester-claude-rc.service` (off by default). The app has **no** privilege to run it: it writes `remote_control` to `state.json`; a root-owned path unit runs `tt-rc-sync` which starts/stops the one service; `tt-rc-run` also stops itself within seconds when the flag clears. Outbound HTTPS only.

**Settings tab.** Web password (set/change/remove), Network check, Claude Code (state, version, signed-in, internet, Remote Control, log in/out with verification result, change history with Undo).

**Install.** `install-pi.sh [--with-claude] [--no-net-split]` → `scripts/install-claude.sh`: records the app in git (always), net split (always), and with `--with-claude`: Anthropic signed apt repo (fingerprint `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE` checked), tmux, contained dirs, ownership, Remote Control units. Uninstall forgets the sign-in and undoes the split. Re-install keeps the git history.

**Not verified without real hardware/account** (see the maintainer's private notes): the live link-and-code sign-in with a real account, Remote Control's one-time prompts on the Pi's Claude Code version, the systemd path-unit behaviour, NetworkManager/dhcpcd behaviour on the Pi OS image, and a real chat round trip.


## 16. Full end-to-end test of the whole app (phase 7 — 2026-11-03)
`scripts/e2e-full.sh` (add `--arm64` for the QEMU runs) runs every suite and prints one summary (the maintainer's private notes). Two real app instances - two identical peers, told apart only by name and port - are exercised **in both directions**: by default the tests are started on the first and name the second as destination, `E2E_REVERSE=1` starts them on the second and names the first, for the whole serial/UDP/TCP/netem/GOOSE/generator suite; the suite also runs a both-ways-at-once test and a one-way test in each direction (clocks assumed synced on the same host).
| Suite | What it proves | Log |
|---|---|---|
| `pytest tests` | units: frames, metrics, engine, profiles, GOOSE (incl. R-GOOSE layout), generator, sniffer decoders, Wi-Fi helper, Claude, network split | docs/unit-tests-last.log |
| `scripts/e2e-local.sh` | tests started on the first instance, the second as destination: serial (socat pty), UDP incl. one-way, TCP drop/reconnect, profiles/export/import, USB save, netem presets, GOOSE L2 + L3, generator (all stream types, impairment accuracy), PDF | the maintainer's private notes |
| `E2E_REVERSE=1 scripts/e2e-local.sh` | the same, started on the second instance | the maintainer's private notes |
| `scripts/e2e-sniff.sh` | real dumpcap/tshark/bridge in a Debian container: SPAN, inline tap, serial DNP3/Modbus tap, ring buffer, USB guard, PDF; **both Pis send UDP, GOOSE L2, R-GOOSE and a 2-stream generator both ways through the tap and tshark dissects every frame** | the maintainer's private notes |
| `scripts/e2e-netsplit.sh` | Wi-Fi = internet / Ethernet = test: 2 x 40 Mb/s of Claude-like traffic out of wlan0 changes nothing on the test port (bytes, delay, loss) A->B and B->A; a default route on a test port is flagged; offline works | the maintainer's private notes |
| `scripts/e2e-claude-ui.sh` (both directions) | Ask Claude sidebar (open/resize/remember), reads state, starts/stops tests, saves settings, change card + undo, off/offline/not-signed-in messages, log out wipes everything and everything refuses, log in again | the maintainer's private notes*.log |
| `scripts/e2e-ui-sweep.sh` | every read endpoint, all 7 tabs in a browser (no console errors), sessions, password, profiles, restart persistence - on both Pis | the maintainer's private notes |
| `scripts/arm64-check.sh`, `arm64-claude-check.sh` | install/uninstall on arm64, unit tests, the e2e suites under QEMU (netem and VLAN auxdata cannot run in qemu-user), the real Claude CLI | docs/arm64/ |
**Only on real hardware** (listed honestly in the maintainer's private notes): gigabit rates and real NIC offloads, USB-serial/RS-232/RS-485 adapters and taps, link-state passthrough on real PHYs, Wi-Fi monitor mode (USB adapter and built-in Nexmon), real radios/cellular link, chrony clock sync between two Pis, systemd service/boot/polkit, NetworkManager behaviour, shared-CPU effects of the Claude traffic on test timing, real Claude sign-in.
