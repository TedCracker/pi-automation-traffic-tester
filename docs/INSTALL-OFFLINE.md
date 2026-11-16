# Pi Automation Traffic Tester: install with NO internet (from a locked-down Windows laptop)

For: Raspberry Pi **4 or 5**, Raspberry Pi OS **64-bit**, **Bookworm or Trixie**. The Pi needs no internet. The laptop needs nothing installed: only the built-in Windows `ssh` and `scp`, an Ethernet port and a cable. No USB stick, no card reader.

You need: the Pi powered and booted, one Ethernet cable (laptop to Pi, direct; any cable works), the Pi's user name and password (set when the SD card was prepared).

## Part 1: On the laptop (once, with internet)
1. Download the `.zip` from Google Drive. Right-click it, **Extract All**. You get a folder with three files: `pi-traffic-tester-offline-...tar.gz` (everything), `INSTALL-OFFLINE.txt` (this guide) and `SHA256SUMS.txt`.
2. Check that Windows has `ssh`. Open **PowerShell** (Start menu, type `powershell`) and type:
   `ssh -V`
   It prints a version (e.g. `OpenSSH_for_Windows_9.5`). If it says "not recognized": Settings, Apps, Optional features, Add a feature, **OpenSSH Client** (needs admin rights; ask IT for "OpenSSH Client"). Without it you cannot copy files over Ethernet; the only other way is a keyboard and monitor on the Pi plus the file on a medium the Pi can read, which your laptop does not allow.

## Part 2: Connect and find the Pi
3. Plug the cable into the laptop's Ethernet port and the Pi's Ethernet port. Wait one minute (Windows shows "Unidentified network" or "No internet": that is normal).
4. In PowerShell type: `ping raspberrypi.local`
   - Replies come back: the Pi is at `raspberrypi.local`. Go to step 6. (If the Pi was given another host name, use that: `<name>.local`.)
   - "could not find host": do step 5.
5. Find the Pi's address without the name:
   - Type `ipconfig`. Under your Ethernet adapter look for an address starting `169.254.` (the laptop gave itself one). Then type `arp -a`; under that interface you will see other `169.254.x.x` addresses: the Pi is one of them (unplug the cable, run `arp -a` again, the one that disappears is the Pi).
   - Still nothing? The Pi has no address on the cable. Fix: give both ends fixed addresses.
     - Laptop (**may need admin rights, tell IT**): Settings, Network and Internet, Ethernet, your adapter, **IP assignment: Edit, Manual**, IPv4 on, IP address `192.168.77.1`, subnet prefix length `24`, leave the rest empty, Save.
     - Pi (needs a keyboard and monitor on the Pi, once): log in and type
       `sudo nmcli connection modify "Wired connection 1" ipv4.method manual ipv4.addresses 192.168.77.2/24`
       then `sudo nmcli connection up "Wired connection 1"`. (If the name differs: `nmcli connection show`.) The Pi is now `192.168.77.2`.
   - Cannot read the SD card on the laptop (card readers are usually blocked too)? Then the card cannot be edited at the site; use the keyboard-and-monitor way above.
6. Test the login (replace `<user>` with the Pi's user name; use the address you found instead of `raspberrypi.local` if needed):
   `ssh <user>@raspberrypi.local`
   Answer `yes` to the question about the key, type the password. You see a prompt like `<user>@raspberrypi:~ $`. Check the system: `uname -m` must say `aarch64` and `grep VERSION_CODENAME /etc/os-release` must say `bookworm` or `trixie`. Type `exit`.
   - **"Connection refused" / SSH is off:** with keyboard and monitor on the Pi type `sudo raspi-config nonint do_ssh 0` (or `sudo systemctl enable --now ssh`). If you can edit the SD card elsewhere: an empty file named `ssh` in the boot partition (the drive called `bootfs`) turns SSH on at the next start.
   - **No user / unknown password:** current Raspberry Pi OS has **no default user** (no `pi`/`raspberry`). The user is created when the card is made with Raspberry Pi Imager (gear/customisation), or by the on-screen first-start wizard on a Pi with keyboard and monitor. A Pi that has never been set up must be done with keyboard and monitor.
   - **`aarch64` is missing (it says `armv7l`):** that Pi runs 32-bit Raspberry Pi OS. This package is 64-bit only; the card must be re-imaged with "Raspberry Pi OS (64-bit)" (not possible at the site without a card reader).

## Part 3: Copy and install
7. In PowerShell go to the folder you extracted (example): `cd $HOME\Downloads\pi-traffic-tester-offline-1.0.0-2026-11-03`
   Copy the big file to the Pi (about 230 MB, one to two minutes on Ethernet):
   `scp .\pi-traffic-tester-offline-1.0.0-2026-11-03.tar.gz <user>@raspberrypi.local:~/`
8. Log in again: `ssh <user>@raspberrypi.local`. Then unpack and check:
   ```
   tar xzf pi-traffic-tester-offline-*.tar.gz
   cd pi-traffic-tester-offline-1.0.0
   ```
9. Install (every Pi is the same, there are no fixed roles any more):
   `sudo ./install-offline.sh`
   (`--role A` / `--role B` from older instructions is still accepted and ignored.) It checks the files, installs everything from the folder (no internet), and prints how to reach the Pi. It takes 2 to 5 minutes. It is safe to run again. Options: `--port N` (other web port), `--no-wifi-nexmon`, `--ntp-server <address of another Pi or time server>` (optional; the app decides the time source later in Settings), `--wifi-mode ap|keep`.
   **What the Pi does with its networks (fresh install):** the Wi-Fi becomes an **access point** named after the Pi (its hostname, or the name you gave it in the app) with the WPA2 password **`password`** (change it in Settings, Network). The Ethernet port is a normal DHCP client: plug it into the network for internet and updates, or into the equipment you test. Running the installer again on a Pi that is already installed leaves its Wi-Fi as it is (`--wifi-mode ap` forces the access point).
   - "Some files are damaged": copy the `.tar.gz` again (step 7).
   - "...is for Bookworm or Trixie": the Pi runs another OS release; re-image the card with Raspberry Pi OS (64-bit).
10. Open the web page. Easiest: join the Pi's own Wi-Fi network (name = the Pi's name, password `password`) with the laptop or a phone and open `http://10.42.0.1:8080`. Or over the Ethernet cable: `http://raspberrypi.local:8080` (or `http://<address>:8080`).
    - Page does not open (name not found)? Let SSH carry it: in a **second** PowerShell window type `ssh -L 8080:localhost:8080 <user>@raspberrypi.local`, leave it open, and browse to `http://localhost:8080`.
11. Do the same for a second Pi (steps 3 to 10; unplug the first Pi, or give it its own name first so the two Wi-Fi networks differ). Then follow "Pair the two Pis" in `docs/INSTALL-PI.md`.

Notes: the built-in Wi-Fi sniffing (Nexmon) is installed automatically on a **Pi 5** whose kernel is in the package (`ls nexmon/modules`); on a Pi 4 or another kernel use a USB Wi-Fi adapter. Remove the tester: `sudo /opt/traffic-tester/install-pi.sh --uninstall`.

## Wi-Fi and Ethernet: who does what
**Fresh install:** Wi-Fi = the Pi's own access point (you reach the Pi with a phone or laptop at `http://10.42.0.1:8080`); Ethernet = DHCP client for internet (updates) and for testing. **Existing install** (you ran an older installer before): nothing changes by itself; the Pi keeps the Wi-Fi it has (so a Pi you reach over Wi-Fi never drops off), and Ethernet test ports stay "never the default route".
Change any of it in the app: **Settings, Network**: Wi-Fi = access point / join a Wi-Fi network / off; each Ethernet port = DHCP or static address, optional VLAN, label *Internet*, *Test* or both. A change that affects the connection you are using asks to confirm and then undoes itself after 90 seconds unless you press **Keep**. If the Pi has no network at all for 3 minutes it starts its own Wi-Fi so you can always get back in. The older check is still there: `python3 -m traffic_tester.netcheck`. If the Pi has no internet nothing is wrong: the tester works fully offline.
Put the Pi on the site's Wi-Fi (only needed for online software updates): Settings, Network, *Join a Wi-Fi network*, or `sudo nmcli device wifi connect "<SSID>" password "<password>"`.

## If something fails
- Copy problems: use the same cable, try `scp` again; verify with `sha256sum` on the Pi against `SHA256SUMS.txt`.
- The installer log is on screen; the service log: `journalctl -u traffic-tester -n 50`. Open the web page again after `sudo systemctl restart traffic-tester`.
- Everything else: `docs/INSTALL-PI.md`.
