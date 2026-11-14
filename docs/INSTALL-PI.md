# Pi Automation Traffic Tester — install on the two Raspberry Pis

For the **Raspberry Pi 5** with **Raspberry Pi OS 64-bit** (Bookworm or newer; the Pi 5 does not run Bullseye or older). Do the same steps on every Pi (there are no fixed roles: every Pi is the same). The Pi needs internet **once** (to download a few packages); after that it works offline.

> **Easiest route: the ready-made card image** (no terminal at all): flash `pi-traffic-tester-<version>.img.xz` with Raspberry Pi Imager, power on, join the Wi-Fi `pi-setup` (password `password`), open `http://10.42.0.1:8080` and name the Pi. See README "Ready-made SD-card image" and START-HERE.pdf. The steps below are for installing onto a Pi you already run.

## 1. Copy the app to each Pi
On your laptop, from the `pi-traffic-tester` folder:
- **USB stick:** copy the whole `pi-traffic-tester` folder onto it, plug it into the Pi, then on the Pi: `cp -r /media/$USER/<stick>/pi-traffic-tester ~/`
- **or over the network:** `scp -r pi-traffic-tester pi@<pi-address>:~/`

## 2. Run the installer
```
cd ~/pi-traffic-tester
sudo ./scripts/install-pi.sh
```
(`--role A|B` from older instructions is still accepted and ignored.) Optional: `--ntp-server <IP>` takes this Pi's clock from another Pi or a time server; without it the app decides the time source later (Settings). Every install allows the private networks to read this Pi's clock.

**What the networks do after a fresh install.** The Pi's Wi-Fi becomes an **access point**: the name is the Pi's name (its hostname, or the "Pi name" you set in the app), the WPA2 password is **`password`**, and the app is at `http://10.42.0.1:8080` (also `http://<hostname>.local:8080`). The Ethernet port is a DHCP client for internet (software updates) and for testing. Settings, Network changes all of it. Running the installer again on a Pi that is already installed **never switches its Wi-Fi by itself** (it may be your only way in); add `--wifi-mode ap` to force the access point, or `--wifi-mode keep` to be explicit. If the Pi is installed over a Wi-Fi SSH session the access point starts at the next reboot, so the session survives.

The installer prints how to reach the Pi ("Installed. Pi name: ..."). It is safe to run again (it updates and restarts). To remove: `sudo /opt/traffic-tester/install-pi.sh --uninstall` (add `--purge` to also delete saved tests and results).

## 3. Open it in a browser
Join the Pi's Wi-Fi network with a phone or laptop (see above) and open `http://10.42.0.1:8080`, or use any browser on the network the Pi's Ethernet port is plugged into.

## 4. Pair the two Pis
1. **On the Pi that echoes:** choose the link type (Serial / Ethernet / GOOSE) and the port and start the echo side. It starts by itself after a reboot.
2. **On the Pi that measures:** click a test (e.g. **DER trip test**) and enter the port or the other Pi's address. The link check runs by itself; you should see the other Pi answer ✓.
3. Press **Start test**. The **Run** screen shows the live numbers and chart, then the verdict; results stay under **Results**.

First time with a new cable? Run **Back-to-back baseline** with a null-modem cable between the two Pis (no radio) to see how much delay the Pis and adapters add themselves.

## Notes
- **Port:** the web page is on port **8080** (change with `--port`). GOOSE multicast (R-GOOSE) uses UDP port 102.
- **USB-serial adapters:** the installer adds a rule that sets FTDI adapters to 1 ms latency every time they are plugged in. The Setup pre-flight check warns if one is still slow.
- **Permissions:** the service runs as a normal user with only the extra network permissions it needs (GOOSE, bad-link simulation, port 102).
- **Password (optional):** set one in the app; off by default.
- **Settings file:** `/etc/traffic-tester/config.env` (port; an old `TT_ROLE` line is ignored). After editing: `sudo systemctl restart traffic-tester`.
- **Logs:** `journalctl -u traffic-tester -n 50`.
- **Power (important on the Pi 5):** use the official **5 V / 5 A USB-C** supply (27 W). On a 5 V / 3 A supply the Pi 5 caps its USB ports at 600 mA in total, and USB-serial adapters may misbehave. Avoid unpowered USB hubs for the serial adapters.
- **Serial ports on the Pi 5** (they go through the RP1 chip):
  - USB-serial adapter: `/dev/ttyUSB0` (the app prefers it).
  - GPIO 14/15 (pins 8/10): `/dev/ttyAMA0` after adding `dtparam=uart0=on` to `/boot/firmware/config.txt` and rebooting.
  - More GPIO UARTs: overlays `uart1-pi5` … `uart4-pi5` give `/dev/ttyAMA1` … `/dev/ttyAMA4`.
  - The 3-pin JST **debug connector** is `/dev/ttyAMA10` (and `/dev/serial0` by default). The app lists it as "debug connector".
  - GPIO UARTs are 3.3 V logic: use a **MAX3232** board for RS-232 levels. Sources differ on exact port naming — check on your Pi with `ls -l /dev/serial* /dev/ttyAMA*` and `pinctrl get 14,15`.
- **Installer check:** it reads `/proc/device-tree/model`; on anything other than a Pi 5 it warns but still installs.
- **Names you will still see:** inside the Pi the service, folder `/opt/traffic-tester`, user and settings file keep the short technical name `traffic-tester` (the product is "Pi Automation Traffic Tester").

## Software updates (Settings → Software update)
Both Pis update from the web page; the installer sets everything up (a root-owned helper + one polkit rule; the web app is never root). Details and the security model: README "Mobile + software updates".
- **Upload a file**: open the Pi's address on a phone or PC on the same network, Settings → Software update → *Choose update file…* → pick `pi-traffic-tester-<version>.ptt` → *Install*. Works with no internet. The `.ptt` files are on the USB stick (`pi-usb`) and are made by `scripts/publish-update.sh`.
- **Check online**: needs Wi-Fi internet and the update key. `install-pi.sh` installs the key when the package contains `update/deploy_key` (the offline package and the USB tar.gz do). Without it the button stays greyed out and says why.
- Install keeps the old app as `app.prev` (`/opt/traffic-tester/app.prev`); if the new version does not answer within 60 s the old one is restored automatically. *Roll back to previous version* swaps them back.
- **What changed**: the version number sits under the logo (tap it) and **About** shows the installed version, update status and *What's new*, all from `CHANGELOG.md`. *Check online* shows the notes of every newer version before *Install*; after an update the first page load shows a one-time "Updated to vX — what's new" banner. To publish: add the new version's entry to `CHANGELOG.md` (the script refuses without it), then `./scripts/publish-update.sh`; refresh the install stick with `scripts/build-usb-package.sh`.
- A new Python package (this release added `websockets`, without which the live charts never connect) travels inside the bundle as aarch64 wheels and is installed offline by the helper before the swap; if it cannot, the Pi says what is missing and changes nothing.
- An update needs the full installer again only when it says "needs a newer installer" (rare: when the helper itself changes).
- Rootless installs (no sudo, like tvbrain): set `TT_UPDATE_MODE=user` (and optionally `TT_UPDATE_SERVICE=traffic-tester.service`) in the user unit; the app folder must sit next to its venv, e.g. `~/pi-traffic-tester/app` + `venv`. Put the key at `<data dir>/update/deploy_key` (chmod 600).

## Traffic sniffer: extra hardware and wiring
Everything below is optional; the tester and generator need none of it. Prices are rough listings (checked 2026-11-03) — verify when you buy.

| You want to… | Buy | About |
|---|---|---|
| Sit inline on an Ethernet link | USB 3 gigabit Ethernet adapter, RTL8153 chip (for example TP-Link UE300) — plug into a **blue** USB 3 port | $10–25 |
| Watch a switch's traffic | Switch with port mirroring: Netgear GS305E (5-port) or GS308E (8-port) | $22–40 |
| Listen to Wi-Fi | Alfa AWUS036ACM (monitor mode, in-kernel driver). The Pi's built-in Wi-Fi cannot do it. | $41–47 |
| Tap RS-232 | 2 × USB-RS232 adapters with real RS-232 levels (FTDI chip) + a DB9 breakout | $25–35 each, $10 |
| Listen to RS-485 | 1 × FTDI USB-RS485 adapter (USB-RS485-WE-1800-BT); 2 for 4-wire | $30–37 |

**Wiring**
- **Inline tap:** device A → Pi's built-in Ethernet · Pi's USB Ethernet → device B. The Pi passes everything through and has no address on those two ports, so **open the Pi's web page over Wi-Fi**. The app refuses to bridge the port your browser uses.
- **SPAN:** switch mirror port → any Pi network port. The Pi only listens.
- **RS-232 tap:** wire each direction's data pin to **pin 2** of its own adapter, join the grounds. Do not use 3.3 V/5 V "TTL" boards on RS-232 levels.
- **RS-485:** A/B (D+/D−) of the bus to the adapter; one adapter hears both directions on 2-wire.
- Keep the Pi on its 5 V / 5 A supply; four USB devices draw from the same budget (use a powered hub if needed).

**Using it.** Open the **Sniffer** tab, pick a preset, press **Run pre-flight check**, then **Start capture**. Captures are saved as `.pcapng` (open in Wireshark) under `/var/lib/traffic-tester/captures/`, or straight to a USB drive. The installer adds `tshark` (dumpcap), `tcpdump`, `iw` and `ethtool`, gives the service the capture permissions (it never runs as root) and keeps NetworkManager away from the tap bridge.

## Claude Code on the Pi (optional, OFF by default)

Claude Code is Anthropic's coding assistant. On the Pi it gives you **Ask Claude** (a chat panel on the right of the web app that can read the screen, explain a failed test, start/stop tests, change settings, and suggest changes to the app) and **Remote Control** (continue the Pi's Claude session from your phone). **The tester works 100 % without it and 100 % offline.** Claude is off until you log in, and logging out removes every trace.

### Install
```
sudo ./scripts/install-pi.sh --with-claude
```
This needs internet **once** (Wi-Fi is fine): it adds Anthropic's signed apt repository (the installer checks the key fingerprint `31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE` and stops if it differs) and installs `claude-code`. It does not sign in. You can add it later by running the installer again with `--with-claude`.

### Network: Wi-Fi / Ethernet, Settings → Network
Fresh installs: Wi-Fi = the Pi's own access point, Ethernet = DHCP (internet and testing); label each Ethernet port **Internet**, **Test** or both in Settings → Network (*Test* = never the default route, the old "Ethernet = test traffic only" split). Installs from before this change keep what they have: the installer's old split (Ethernet never the default route, Wi-Fi = internet) stays unless you change it in the app or run with `--no-net-split`. Check it any time in the app: **Settings → Network**, or `python3 -m traffic_tester.netcheck` (exit 0 = fine, 1 = warning, 2 = problem). If the check shows a problem it prints the exact command to fix it (`sudo /opt/traffic-tester/app/scripts/net-split.sh --apply`). Undo with `--undo`.

### Sign in (one time, about a minute, you finish it yourself)
No API key is used; you sign in with your own Claude subscription (Max).
1. In the app set a **web password** (**Settings → Web password**). Signing in is refused without one.
2. Open the **✦ Ask Claude** panel (top right) → **Log in**, or **Settings → Claude Code → Log in**.
3. The app shows a **link**. Open it on your phone or PC, sign in to your Claude account, approve. It shows a **code**.
4. Paste the code into the app → **Finish sign-in**. The panel now shows the chat. Remote Control is **not** started by this; it stays off until you switch it on.

Command-line alternative (same steps, in the SSH terminal):
`sudo -u traffic-tester env PYTHONPATH=/opt/traffic-tester/app /opt/traffic-tester/venv/bin/python -m traffic_tester.claude.cli login`

The sign-in is stored **only** in `/var/lib/traffic-tester/claude/` (never in the app folder, never in an installer package, never in git).

### Log out (do this before you leave the Pi at a customer site)
**✦ Ask Claude → ⚙ → Log out**, or **Settings → Claude Code → Log out and turn off** (no password needed to log out), or
`sudo -u traffic-tester env PYTHONPATH=/opt/traffic-tester/app /opt/traffic-tester/venv/bin/python -m traffic_tester.claude.cli logout`.
It signs out, deletes the saved sign-in, sessions and settings, stops and disables Remote Control, switches the panel off (the server then refuses every Claude request) and **scans the Pi for any leftover token**. The result is shown in the app (“No saved sign-in was found on this Pi”) and written to the app log. If it finds a leftover file it lists the path instead of claiming success. To check by hand:
`sudo grep -rIl --exclude-dir=proc --exclude-dir=sys -E 'sk-ant-(oat|ort|api)[0-9]*-|"(accessToken|refreshToken|claudeAiOauth)"' /var/lib /home /root /etc /opt /tmp 2>/dev/null` should print nothing.

### What Ask Claude can and cannot do
- **Reads** the screen as structured data (current tab, selected test, unsaved changes, live status, the visible text), recent test results, and the app log. Not just screenshots.
- **Operates** the app with the same actions you have: open a tab, start a test, stop a test, save a profile. **Every action appears as an action line** in the chat with its result, and the screen on the left updates live.
- **Suggests changes** to the app (a new setting, a layout change). Claude edits a **scratch copy**; you see the exact diff and press **Apply** or **Discard**. Applying commits it to git and restarts the app for a few seconds (refused while a test runs). **Every applied change can be undone** (chat card or **Settings → Claude Code → Changes**). Claude cannot change the parts that keep it safe (`traffic_tester/claude/`, `scripts/`, the installer): they are root-owned and the app refuses to apply changes to them.
- **Never** runs commands on the Pi, never touches a running test, runs at the lowest CPU priority. If the Pi has no internet, or you are not signed in, it says so in plain words and the tester carries on.

### SSH from your Windows PC into the Pi
Windows 10/11 already has OpenSSH. In PowerShell on the PC:
```
ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\id_ed25519_pi
type $env:USERPROFILE\.ssh\id_ed25519_pi.pub | ssh pi@<pi-address> "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```
(use the Pi's Wi-Fi address; it is the one that has internet and the web page. On the Pi, `sudo systemctl enable --now ssh` if SSH is not on yet.) Then add this to `C:\Users\<you>\.ssh\config`:
```
Host pi-tt
    HostName 192.168.1.50            # the Pi's Wi-Fi address, or pi-tt.local
    User pi
    IdentityFile ~/.ssh/id_ed25519_pi
    ServerAliveInterval 30
    ServerAliveCountMax 4
```
Now `ssh pi-tt` logs in with no password. For safety you can turn off password logins on the Pi once the key works: set `PasswordAuthentication no` in `/etc/ssh/sshd_config.d/99-tt.conf` and `sudo systemctl reload ssh`.

### Edit the app on the Pi from Claude Code on your PC
Claude Code on your PC can work on the Pi's app files using the same SSH key. Pick one (not yet tried on a real Pi, see checks.md):
1. **Mount the Pi's folder on the PC (keeps the Pi signed out).** Install SSHFS-Win + WinFsp on the PC, then in Explorer map a network drive to `\\sshfs.r\pi@pi-tt\opt\traffic-tester\app` (it reads your `~/.ssh/config` host `pi-tt`). Start Claude Code on the PC in that drive. Your PC's sign-in is used; the Pi stores nothing.
2. **Terminal over SSH.** `ssh pi-tt`, `cd /opt/traffic-tester/app`, run `claude` there. This signs in **on the Pi** (as whoever you are logged in as), so run the log-out check below when you leave.
3. **VS Code Remote-SSH.** Connect to `pi-tt`, open `/opt/traffic-tester/app`. The Claude Code extension then runs on the Pi and needs a sign-in on the Pi, like option 2.
The app folder is a git repository owned by the service user: after editing run `sudo -u traffic-tester git -C /opt/traffic-tester/app add -A && sudo -u traffic-tester git -C /opt/traffic-tester/app commit -m "what changed"` then `sudo systemctl restart traffic-tester`. Files in `traffic_tester/claude/` and `scripts/` are root-owned on purpose (edit them with `sudo`). A reinstall (`install-pi.sh`) overwrites the app files from the copied folder but keeps the git history, so your edits stay visible as a diff and can be reverted with `sudo -u traffic-tester git -C /opt/traffic-tester/app revert <id>`.

### Remote Control (use the Pi's Claude from your phone)
Off by default. Turn on: **Settings → Claude Code → Start Remote Control** (needs Claude signed in). The Pi then runs `claude remote-control` named “Pi Automation Traffic Tester <hostname>” as a background service (`traffic-tester-claude-rc.service`); open the Claude app on your phone or claude.ai/code and pick that session. It only makes **outbound** HTTPS connections (nothing listens for it), so it needs the Wi-Fi internet. It restarts itself if the connection drops for a while.
Stop it: **Settings → Claude Code → Stop Remote Control**, or **log out** (always stops it). The switch works without giving the web app any root rights: the app writes a flag file and a root-owned systemd path unit (`traffic-tester-claude-rc.path`) starts or stops the service; the service also stops itself within seconds when the flag is cleared.
By hand on the Pi: `sudo systemctl stop traffic-tester-claude-rc.service` (and `sudo systemctl disable --now traffic-tester-claude-rc.path` to disable the switch).
Security note: a Remote Control session is a full Claude Code session as the `traffic-tester` user. Anyone who can use your Claude account can use it, so keep it off unless you need it, and log out when you leave.

### What was verified, and what still needs a real Pi
See `checks.md` (“Phase 8”). Real Pi still to confirm: the link-and-code sign-in with your real account, the Remote Control prompts on the Pi's Claude Code version, and that NetworkManager/dhcpcd on your Pi OS image takes the “never default route” setting.

## Offline install (no internet on the Pi)
For a customer site with only a locked-down Windows laptop: **`docs/INSTALL-OFFLINE.md`** (also shipped as `INSTALL-OFFLINE.txt`) is the step-by-step guide: download ONE zip from Google Drive, connect laptop and Pi with an Ethernet cable, `scp` the file over, run `sudo ./install-offline.sh`, join the Pi's Wi-Fi (password `password`) and open `http://10.42.0.1:8080` (or `http://raspberrypi.local:8080` over the Ethernet cable).
The package (`scripts/pkg/build-offline-package.sh`) covers **Raspberry Pi 4 and Pi 5, Raspberry Pi OS 64-bit Bookworm and Trixie**: app, all arm64 `.deb`s with dependencies as one local apt repository per release (`apt-bookworm/`, `apt-trixie/`), Python wheels for 3.11 and 3.13, the built-in Wi-Fi (Nexmon) driver for 23 Pi 5 kernels, install script, checksums. `install-pi.sh --offline <folder>` is the same installer with no internet; the normal online install is unchanged. Claude Code is not bundled (licence); it is added once when the Pi has Wi-Fi internet (`--with-claude`).
Verified in arm64 containers with the network OFF and real systemd (Bookworm and Trixie, Pi 5 and Pi 4 model strings): checks.md "Phase 6 (offline Drive package)".

## Built-in Wi-Fi sniffing (Nexmon, experimental, Pi 5)
- Sniffer → *Wi-Fi monitor* → adapter **Built-in Wi-Fi of the Pi (Nexmon)**. **Wi-Fi, and internet over Wi-Fi, go off while it sniffs** — open the web page over Ethernet. Tick the confirmation; start. **Restore normal Wi-Fi** (same page) brings the stock driver/firmware, NetworkManager and the link back; it also happens when the capture stops, if the app stops (watchdog, ≤ about a minute) and at boot.
- The installer step needs a driver for your exact kernel: `uname -r` must be one of the folders in `nexmon/modules/` (Pi 5 Bookworm kernels 6.12.19–6.12.109, 6.6.20–6.6.74). Otherwise use a USB adapter, or build once on the Pi with internet: `sudo apt install linux-headers-$(uname -r) build-essential bc && sudo /opt/traffic-tester/nexmon/build/build-on-pi.sh`.
- Always works as a rescue: `sudo systemctl start tt-wifi-helper@restore.service`, or reboot.
- Recommended and more reliable: a USB Wi-Fi adapter with monitor mode (Alfa AWUS036ACM, AWUS036AXML).
- Details and what is still to be proven on a real Pi: SPEC §14.6 and TODO.md.
