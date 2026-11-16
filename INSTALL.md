# Install guide: Pi Automation Traffic Tester

This guide assumes **no experience with GitHub or Linux**. Every step says what to click or type and what you should see.
Words in *italics* are explained the first time they appear. Do the same install on **every** Pi: there is no "main" Pi, every Pi is the same.

**Pick one way to install (A is the easiest):**

| | Way | Needs internet on the Pi? | Time |
|---|---|---|---|
| **A** | Ready-made SD-card image: flash it, power on, done | No | 10 min |
| **B** | Fresh Raspberry Pi OS card + download this code + one command | Yes (once) | 20 min |
| **C** | Offline package (a site with no internet) | No | 15 min |

**You need:** a Raspberry Pi 5 per test point (two or more), a **microSD card (32 GB or bigger) or SSD**, the official **5 V / 5 A USB-C power supply** (a weaker supply makes USB serial adapters misbehave), and a PC, Mac or phone. Everything runs on **64-bit Raspberry Pi OS** (*Bookworm* or *Trixie*; the Pi 5 does not run older ones).

---

## Way A: ready-made SD-card image

The image is a complete card: Raspberry Pi OS plus the app, already installed. You do not need a monitor, keyboard or terminal.

**Where to get the image.** If the project's **Releases** page (right-hand side of the GitHub page) lists a file ending in `.img.xz`, download it. If there is none, ask the person who gave you this project for the file, or build it yourself (needs a Linux PC with Docker, about 12 GB free and 15 minutes): `scripts/pkg/build-image.sh`, see "Making a new card image" in [docs/DETAILS.md](docs/DETAILS.md).

1. On your PC or Mac, install **Raspberry Pi Imager** from <https://www.raspberrypi.com/software/> and open it. (*Imager* is the free tool that writes an operating system onto a memory card.)
2. Put the microSD card in your PC (card reader or built-in slot).
3. In Imager: **Choose Device** → *Raspberry Pi 5*. **Choose OS** → scroll down to **Use custom** → pick the `.img.xz` file. **Choose Storage** → your card. Click **Next**.
4. Imager asks about OS customisation. You can click **No**. (Optional: **Edit settings** lets you add your own Wi-Fi name and password, and turn SSH on.)
5. Click **Yes**/**Write**, wait until it says it is finished, then take the card out.
6. Put the card in the Pi, plug in the power. Wait **2 minutes** for the first start.
7. On your **phone**, open the Wi-Fi list and join the network **`pi-setup`**, password **`password`**.
8. A page called **Set up this Pi** opens by itself. If not, open a browser and go to **http://10.42.0.1:8080**.
9. Type a name for this Pi (for example `pi1`) and tap **Name this Pi**. The Pi's Wi-Fi network is renamed to that name (the password stays `password`) and the page reloads.
10. Repeat for the second Pi with a different name (`pi2`). Go to **"Open the app"** below.

---

## Way B: fresh Raspberry Pi OS + this code

### B1. Put Raspberry Pi OS on the card
1. Install and open **Raspberry Pi Imager** (<https://www.raspberrypi.com/software/>), put the card in your PC.
2. **Choose Device** → *Raspberry Pi 5*. **Choose OS** → *Raspberry Pi OS (64-bit)* (the "Lite" version is fine and smaller). **Choose Storage** → your card. **Next**.
3. When asked about customisation click **Edit Settings** and fill in:
   - **Hostname:** `pi1` (the second Pi: `pi2`). This becomes the Pi's name on the network.
   - **Username and password:** choose them and write them down. (You need them to log in.)
   - **Wi-Fi:** your site's Wi-Fi name and password, or skip it and use an Ethernet cable instead.
   - **Services tab → Enable SSH → Use password authentication.** (*SSH* lets you type commands on the Pi from your PC.)
4. **Save**, **Yes**, wait for the write to finish. Put the card in the Pi and power it on. Wait **2 to 3 minutes**.

### B2. Log in to the Pi from your PC
1. Open a terminal: on Windows press the Windows key, type `powershell`, Enter; on a Mac open *Terminal*.
2. Type (replace `<user>` and `pi1` with what you chose): `ssh <user>@pi1.local`
3. Answer `yes` to the question about a key, then type the password (nothing shows while you type; that is normal). You should see a prompt like `<user>@pi1:~ $`. You are now typing commands **on the Pi**.
   - *"Could not resolve hostname"*: the Pi is not on the network yet or `.local` names are blocked. Find its address (next section, "Find the Pi's address") and use `ssh <user>@<address>`.

### B3. Get the code onto the Pi
Pick **one** of these. Each one ends with a folder named something like `pi-traffic-tester` on the Pi.

**Option 1: `git clone` (copy the project with `git`).** *Git* is the tool GitHub is built on; `git clone` downloads a copy of the whole project.
```
sudo apt update && sudo apt install -y git
git clone https://github.com/TedCracker/pi-automation-traffic-tester.git
cd pi-automation-traffic-tester
```
You should see "Cloning into 'pi-automation-traffic-tester'..." and end up inside the folder.


**Option 2: download a ZIP on your PC and copy it over.**
1. On the GitHub page of this project click the green **Code** button → **Download ZIP**. (Or open **Releases** and download "Source code (zip)".) You get `pi-automation-traffic-tester-main.zip` .
2. In a terminal **on your PC**, go to the folder where it was saved (usually `cd Downloads`) and copy it to the Pi:
   `scp pi-automation-traffic-tester-main.zip <user>@pi1.local:~/`
3. Back in the SSH window on the Pi:
```
sudo apt update && sudo apt install -y unzip
unzip pi-automation-traffic-tester-main.zip
cd pi-automation-traffic-tester-main
```
(`scp` copies a file to the Pi; `unzip` opens the ZIP.)

### B4. Run the installer
In the project folder on the Pi:
```
sudo bash scripts/install-pi.sh
```
(`sudo` = "run as administrator". `bash` is used so it also works when a ZIP download lost the file's "executable" flag.)

It needs the internet **once** (it downloads a few packages) and takes **3 to 10 minutes**. At the end you should see **"Installed. Pi name: ..."** and how to open the app.

> **Important: the Wi-Fi changes.** On a fresh install the Pi's own Wi-Fi becomes a **hotspot** (named after the Pi, password `password`, address `10.42.0.1`). If you are logged in over *Wi-Fi*, the connection drops at that point. Either plug in an **Ethernet cable** before you start, or add `--wifi-mode keep` to leave the Wi-Fi as it is: `sudo bash scripts/install-pi.sh --wifi-mode keep`.

Other options (all optional): `--ntp-server <IP>` take this Pi's clock from another Pi or a time server · `--port 9000` use another web port · `--help` list everything. Running the installer again is safe: it updates the app and restarts it.

Repeat B1 to B4 for the second Pi (hostname `pi2`).

---

## Way C: offline package (no internet at the site)

For a site where the Pi has no internet. You prepare **one file** at home or in the office, then carry it.

1. **Get the package** (a `.zip` named `pi-traffic-tester-offline-<version>-<date>.zip`) from whoever maintains the project, or build it (Linux PC with Docker and internet): `scripts/pkg/build-offline-package.sh <workdir> <nexmon-workdir>`; the header of that script lists what it needs. Alternatively `scripts/build-usb-package.sh <folder>` writes a USB-stick folder with `install.sh`; on the Pi run `bash /media/$USER/*/install.sh` (this one needs internet for system packages once).
2. Follow the picture-by-picture steps in **[docs/INSTALL-OFFLINE.md](docs/INSTALL-OFFLINE.md)**. In short: unzip on a laptop, connect the laptop to the Pi with an Ethernet cable, copy the `.tar.gz` over with `scp`, log in with `ssh`, then:
```
tar xzf pi-traffic-tester-offline-*.tar.gz
cd pi-traffic-tester-offline-*
sudo ./install-offline.sh
```
You should see "Installed. Pi name: ...". The package works on a Pi 4 or Pi 5 with 64-bit Raspberry Pi OS Bookworm or Trixie.

---

## Find the Pi's address

You usually do not need the address: `http://pi1.local:8080` works on most networks (use your Pi's name). If it does not:
- **On the Pi's own hotspot:** it is always **http://10.42.0.1:8080** (join the Pi's Wi-Fi first).
- **Router page:** log in to your router (often `192.168.1.1`) and look for a device called `pi1` in its list of connected devices.
- **From the Pi itself:** log in by SSH (or keyboard and monitor) and type `hostname -I`. The first number (like `192.168.1.57`) is the address.

## Open the app

1. Join the Pi's Wi-Fi hotspot with your phone or laptop (name = the Pi's name, or `pi-setup` on a brand-new image card; password `password`), **or** use any browser on the network the Pi's Ethernet cable is plugged into.
2. Open **http://10.42.0.1:8080** (hotspot) or **http://pi1.local:8080** / **http://<address>:8080** (cable or router). You should see the **Tests** page with the Pi's name at the top right and a green dot.
3. **Change the default Wi-Fi password** (it is `password` on every Pi!): **Settings → Network**. Optionally set a **web password** in **Settings → Web password** so nobody else on the network can start tests.

## Set up the second Pi and run the first test

1. Install the second Pi the same way, with a **different name**. Plug both Pis into the same network (or the same switch), or join them over the Pi hotspot. They find each other automatically (the other Pi shows up in the app; the full list is in **Settings**). If your network blocks that, use **Add a Pi by IP address** there.
2. Open **Tests**, pick **Quick link check**, press **▶ Run**, choose the other Pi, then press **Start**. The **Run** page shows live numbers and a chart, then a verdict (pass/fail). Everything is kept under **Results**.
3. First time with a new serial cable? Run **Back-to-back baseline** with a null-modem cable between the two Pis to see how much delay the Pis and adapters add by themselves.

## Optional: Tailscale (reach the Pis from anywhere)

*Tailscale* is a free service that puts all your devices on one private network over the internet. On each Pi (needs internet): `curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up` and open the link it prints to log in. Afterwards the Pi is reachable at the address Tailscale shows (also `http://<pi-name>:8080` if MagicDNS is on). The app never needs it.

## Update

- **From the app:** **Settings → Software update → Upload a file** works on any copy: if the maintainer gave you a `pi-traffic-tester-<version>.ptt` file, open **Choose update file** → **Install**. The app checks a digital signature first and rolls back by itself if the new version does not start. The **Check for updates** button needs the maintainer's private update channel (a read-only key that is **not** part of this public project), so on a copy installed from this page it will say it cannot reach an update source. That is normal; use the next option.
- **From the code (works for everyone):** download the newest code again (green **Code** button → **Download ZIP**, or `git pull` inside your clone), copy it to the Pi as in B3, and run `sudo bash scripts/install-pi.sh` again. Your saved tests and results are kept. **Offline site:** build or request a new offline package (Way C) and run its installer again.

## Uninstall

`sudo /opt/traffic-tester/install-pi.sh --uninstall` removes the app. Add `--purge` to also delete saved tests and results.

## Common problems

| What you see | Try this |
|---|---|
| Page does not open | Is your phone on the Pi's Wi-Fi (or the same network)? Try `http://10.42.0.1:8080`. On the Pi: `sudo systemctl status traffic-tester`, then `sudo systemctl restart traffic-tester`. Logs: `journalctl -u traffic-tester -n 50`. |
| `pi1.local` not found | Use the address instead (see "Find the Pi's address"). Windows needs *Bonjour*/iTunes for `.local` names on some PCs; the address always works. |
| Cannot log in with SSH | SSH must be enabled (Imager → Edit Settings → Services). With a monitor and keyboard: `sudo raspi-config nonint do_ssh 0`. Check user name and password; current Raspberry Pi OS has no default `pi` user. |
| Installer says it needs 64-bit / Bookworm or Trixie | The card has 32-bit or an old OS. Re-flash with **Raspberry Pi OS (64-bit)**. Check with `uname -m` (must print `aarch64`). |
| Installer fails on `apt` / "could not resolve" | The Pi has no internet. Plug in Ethernet or join a Wi-Fi network, then run the installer again; or use Way C. |
| The SSH connection dropped during the install | The Wi-Fi became a hotspot (see the note in B4). Reconnect through Ethernet or the Pi's hotspot (`10.42.0.1`), run the installer again. |
| No Wi-Fi hotspot appears | Set the Wi-Fi country once: `sudo raspi-config nonint do_wifi_country US` (use your country code), then reboot. |
| The other Pi is not listed | Both on the same network? Some switches/routers block name discovery: use **Add a Pi by IP address**. |
| Serial test shows zero bytes | Check the cable: two Pis back-to-back need a **null-modem** cable (TX and RX crossed). Both ends must use the same baud rate. Details: [docs/INSTALL-PI.md](docs/INSTALL-PI.md) (serial ports, FTDI). |
| USB serial adapter acts strangely | Use the official 5 V / 5 A power supply; avoid unpowered USB hubs. |
| One-way delay looks wrong | The two Pis' clocks differ. Use round-trip results, or set a time source in Settings. |
| Update fails | The app rolls back by itself. Look at **Settings → Software update**, or run the installer again from the newest code. |

More help: [docs/INSTALL-PI.md](docs/INSTALL-PI.md) (serial ports, sniffer wiring), [docs/AI-GUIDE.md](docs/AI-GUIDE.md), and the **AI guide** link inside the app.
