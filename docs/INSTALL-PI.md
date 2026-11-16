# Pi Automation Traffic Tester — install on the two Raspberry Pis

For the **Raspberry Pi 5** with **Raspberry Pi OS 64-bit** (Bookworm or newer; the Pi 5 does not run Bullseye or older). Do the same steps on every Pi (there are no fixed roles: every Pi is the same). The Pi needs internet **once** (to download a few packages); after that it works offline.

> **Easiest route: the ready-made card image** (no terminal at all): flash `pi-traffic-tester-<version>.img.xz` with Raspberry Pi Imager, power on, join the Wi-Fi `pi-setup` (password `password`), open `http://10.42.0.1:8080` and name the Pi. See docs/DETAILS.md "Ready-made SD-card image" and START-HERE.pdf. The steps below are for installing onto a Pi you already run.

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
Both Pis update from the web page; the installer sets everything up (a root-owned helper + one polkit rule; the web app is never root). Details and the security model: docs/DETAILS.md "Mobile + software updates".
- **Upload a file**: open the Pi's address on a phone or PC on the same network, Settings → Software update → *Choose update file…* → pick `pi-traffic-tester-<version>.ptt` → *Install*. Works with no internet. The `.ptt` files are on the USB stick (`pi-usb`) and are made by `scripts/publish-update.sh`.
- **Check online**: needs Wi-Fi internet and the update key. `install-pi.sh` installs the key when the package contains `update/deploy_key` (the offline package and the USB tar.gz do). Without it the button stays greyed out and says why.
- Install keeps the old app as `app.prev` (`/opt/traffic-tester/app.prev`); if the new version does not answer within 60 s the old one is restored automatically. *Roll back to previous version* swaps them back.
- **What changed**: the version number sits under the logo (tap it) and **About** shows the installed version, update status and *What's new*, all from `CHANGELOG.md`. *Check online* shows the notes of every newer version before *Install*; after an update the first page load shows a one-time "Updated to vX — what's new" banner. To publish: add the new version's entry to `CHANGELOG.md` (the script refuses without it), then `./scripts/publish-update.sh`; refresh the install stick with `scripts/build-usb-package.sh`.
- A new Python package (this release added `websockets`, without which the live charts never connect) travels inside the bundle as aarch64 wheels and is installed offline by the helper before the swap; if it cannot, the Pi says what is missing and changes nothing.
- An update needs the full installer again only when it says "needs a newer installer" (rare: when the helper itself changes).
- Rootless installs (no sudo, for example a shared lab machine): set `TT_UPDATE_MODE=user` (and optionally `TT_UPDATE_SERVICE=traffic-tester.service`) in the user unit; the app folder must sit next to its venv, e.g. `~/pi-traffic-tester/app` + `venv`. Put the key at `<data dir>/update/deploy_key` (chmod 600).

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

## Network: Wi-Fi / Ethernet, Settings → Network
Fresh installs: Wi-Fi = the Pi's own access point, Ethernet = DHCP (internet and testing); label each Ethernet port **Internet**, **Test** or both in Settings → Network (*Test* = never the default route, the old "Ethernet = test traffic only" split). Installs from before this change keep what they have: the installer's old split (Ethernet never the default route, Wi-Fi = internet) stays unless you change it in the app or run with `--no-net-split`. Check it any time in the app: **Settings → Network**, or `python3 -m traffic_tester.netcheck` (exit 0 = fine, 1 = warning, 2 = problem). If the check shows a problem it prints the exact command to fix it (`sudo /opt/traffic-tester/app/scripts/net-split.sh --apply`). Undo with `--undo`.

## SSH from your Windows PC into the Pi
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

## Removed: Ask Claude and Remote Control

Versions up to 1.6 had an optional "Ask Claude" chat panel and Claude Code Remote Control on the Pi. They were removed in 1.7.0 (installer `--with-claude` is now ignored). Updating or re-running the installer on a Pi that had them stops and removes the Remote Control units, uninstalls the `claude-code` package and Anthropic apt repository, and deletes the saved sign-in (`/var/lib/traffic-tester/claude/`); saved tests and results are never touched. For AI help with this app, use the [AI guide](AI-GUIDE.md) with any assistant.

## Offline install (no internet on the Pi)
For a customer site with only a locked-down Windows laptop: **`docs/INSTALL-OFFLINE.md`** (also shipped as `INSTALL-OFFLINE.txt`) is the step-by-step guide: download ONE zip from Google Drive, connect laptop and Pi with an Ethernet cable, `scp` the file over, run `sudo ./install-offline.sh`, join the Pi's Wi-Fi (password `password`) and open `http://10.42.0.1:8080` (or `http://raspberrypi.local:8080` over the Ethernet cable).
The package (`scripts/pkg/build-offline-package.sh`) covers **Raspberry Pi 4 and Pi 5, Raspberry Pi OS 64-bit Bookworm and Trixie**: app, all arm64 `.deb`s with dependencies as one local apt repository per release (`apt-bookworm/`, `apt-trixie/`), Python wheels for 3.11 and 3.13, the built-in Wi-Fi (Nexmon) driver for 23 Pi 5 kernels, install script, checksums. `install-pi.sh --offline <folder>` is the same installer with no internet; the normal online install is unchanged. Claude Code is not bundled (licence); it is added once when the Pi has Wi-Fi internet (`--with-claude`).
Verified in arm64 containers with the network OFF and real systemd (Bookworm and Trixie, Pi 5 and Pi 4 model strings): the maintainer's private notes "Phase 6 (offline Drive package)".

## Built-in Wi-Fi sniffing (Nexmon, experimental, Pi 5)
- Sniffer → *Wi-Fi monitor* → adapter **Built-in Wi-Fi of the Pi (Nexmon)**. **Wi-Fi, and internet over Wi-Fi, go off while it sniffs** — open the web page over Ethernet. Tick the confirmation; start. **Restore normal Wi-Fi** (same page) brings the stock driver/firmware, NetworkManager and the link back; it also happens when the capture stops, if the app stops (watchdog, ≤ about a minute) and at boot.
- The installer step needs a driver for your exact kernel: `uname -r` must be one of the folders in `nexmon/modules/` (Pi 5 Bookworm kernels 6.12.19–6.12.109, 6.6.20–6.6.74). Otherwise use a USB adapter, or build once on the Pi with internet: `sudo apt install linux-headers-$(uname -r) build-essential bc && sudo /opt/traffic-tester/nexmon/build/build-on-pi.sh`.
- Always works as a rescue: `sudo systemctl start tt-wifi-helper@restore.service`, or reboot.
- Recommended and more reliable: a USB Wi-Fi adapter with monitor mode (Alfa AWUS036ACM, AWUS036AXML).
- Details and what is still to be proven on a real Pi: SPEC §14.6 and the maintainer's private notes
