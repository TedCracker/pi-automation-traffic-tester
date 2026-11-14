# Mirrored Bits emulator spec (SEL-2505-style remote I/O). Treat as source of truth.

## 1. What we're emulating
- SEL Mirrored Bits: point-to-point, full-duplex, relay-to-relay protocol over async serial (EIA-232, fiber, radio, serial-over-IP).
- Each end has 8 transmit bits (TMB1–8) and 8 receive bits (RMB1–8). Far end's TMBn appears as local RMBn.
- SEL-2505 mode: contact inputs IN1–8 drive TMB1–8; RMB1–8 drive contact outputs OUT1–8. No logic in between.
- Use case: put one emulator at each end of a wireless link (a cellular or private-LTE radio) to test whether Mirrored Bits survives it.

## 2. The stream
- Continuous: send a full message every send cycle whether or not any bit changed. Never send only on change.
- Each message is 4 characters. Each of the 8 bits is carried 3 times per message (redundancy), plus framing/parity and an address/ID pattern.
- Rate: back-to-back as fast as the baud allows, capped by device processing rate (SEL relays send 4–8 messages per power-system cycle). Configurable. Rough guide: ~1 ms/message at 38.4 kbps, ~4 ms at 9600.
- Baud options: 2400, 4800, 9600, 19200, 38400 (some devices also 115200).
- IMPORTANT: The exact on-the-wire bit layout (character format, bit positions, ID/inversion pattern, MB vs MB8 modes) is NOT defined in this spec. Build the codec as a pluggable module behind a clean interface. Do NOT invent a layout and claim SEL compatibility. Fill it in later from SEL's "Understanding and Effectively Using the SEL Mirrored Bits Protocol" application guide or from serial captures of a real device. Until then, ship a clearly labeled "SimCodec" (our own framing) for emulator-to-emulator testing only.

## 3. Receive validation (every message)
Reject the message if any check fails:
1. Character errors: parity, framing, overrun.
2. Bit consistency: all 3 copies of each bit must agree.
3. Address: received ID must match local RX_ID. If it matches our own TX_ID, we're hearing ourselves (loopback) → reject and flag loopback.
4. Timing: inter-arrival time must be within K × nominal message interval (K configurable).
Any rejected or missing message → ROK = 0 immediately.

## 4. Channel status bits (per channel)
- ROK: 1 while messages are good; 0 instantly on any bad, late or missing message. Re-asserts after N consecutive good messages (N configurable, default 2; ASSUMPTION, verify against a real device).
- Lost-channel rule: at least 1 valid message must be received for every 3 messages' worth of time; otherwise the channel is failed.
- RBAD: ROK continuously 0 for ≥ RBAD_PU seconds (setting).
- CBAD: unavailability (% of time ROK=0 over a rolling window) exceeds CBAD_PU (setting; window length configurable).
- LBOK: loopback test passing (loopback test mode only).

## 5. Received-bit handling
- Per-bit security counts: RMBn goes 0→1 only after PU consecutive good messages showing 1, and 1→0 only after DO consecutive good messages showing 0 (settings RMBn_PU / RMBn_DO, default 1).
- Per-bit default state (0 or 1): while ROK=0, each RMBn sits at its default. On recovery, resume normal updates through the counters.

## 6. Settings (per channel)
Mode/PROTO (A/B pairing; the two ends must be opposite), BAUD, TX_ID, RX_ID, RBAD_PU, CBAD_PU, per-bit default state, per-bit PU/DO counts, hardware handshaking on/off, character framing option (e.g., extra stop bit for equipment needing 10-bit characters). Keep setting names configurable labels; they vary by SEL model.

## 7. Transports (pluggable)
- Real serial port (COMx / /dev/ttyX).
- TCP client/server and UDP (raw serial-over-IP for radio / PLTE gateways). Send each message the moment it's built: TCP_NODELAY on, no batching or buffering.
- Internal back-to-back (two emulator instances in one process) for self-test.

## 8. Wireless test features
- Impairment injector (between codec and transport): fixed latency, jitter (± ms random), random drop %, burst outages (length / interval), bit-error injection.
- Latency measurement via echo on a reserved bit (default TMB8): far end sets TMB8 = RMB8; near end sets TMB8 = NOT RMB8. Each RMB8 transition = one round trip. Log RTT, show one-way ≈ RTT/2, alarm if no transition within RTT_MAX. Note: ROK alone will NOT catch a steady-but-high delay; this echo is what catches it.
- Inter-arrival histogram of valid messages (exposes jitter and bunching from radio packetization).
- Counters: messages TX/RX, rejects by reason (parity / framing / overrun / consistency / address / timing), ROK drop count, RBAD and CBAD events, unavailability %.
- Timestamped event log (ms resolution, SER-style) of every TMB, RMB, ROK, RBAD, CBAD change. CSV export.

## 9. UI
- Two panels (A and B): 8 TMB toggles, 8 RMB LEDs, ROK / RBAD / CBAD / LBOK LEDs, TX_ID / RX_ID / baud display.
- Live message stream view, impairment controls, RTT and jitter charts, event log.

## 10. Acceptance tests
1. Toggle TMBn at A → RMBn at B changes within (message time + transport latency + PU count).
2. Kill the link → ROK=0 instantly, RMBs go to defaults, RBAD asserts after RBAD_PU.
3. Loop A's TX back to A's RX, or mismatch IDs → messages rejected, loopback flagged.
4. Corrupt one copy of one bit → message rejected.
5. 10% random drop → ROK chatter; CBAD asserts per its threshold.
6. Steady +50 ms latency with zero jitter → ROK stays up AND the echo RTT alarm fires.
