#!/bin/bash
# reflash-via-pinn.sh -- turn a running Raspbian (e.g. Stretch) Pi 3B+ into a
# PINN Lite network installer, with no computer and no second SD card.
#
#   sudo bash reflash-via-pinn.sh
#
# What it does:
#   1. checks: root, Pi model, /boot is the SD card's FAT partition, free space
#   2. downloads PINN Lite (SourceForge, procount/pinn), verifies sha256
#   3. trims files the Pi 3B+ never uses so PINN fits a small (~42 MB) /boot
#   4. adds your current Wi-Fi (wpa_supplicant.conf) so PINN is online at once
#   5. backs up /boot to /root, swaps PINN in, verifies every file, reboots
# Anything failing BEFORE /boot is touched aborts with nothing changed; a
# failure while copying restores the old /boot from the backup.
#
# After reboot PINN repartitions the WHOLE card: everything on it is erased
# (including the /root backup). Then pick an OS and it installs over Wi-Fi.
#
# NOOBS cards (Stretch /boot on p6): PINN replaces NOOBS on p1 instead.
# Test knobs: BOOT_DIR=/some/dir DRY_RUN=1 (no reboot, no model check) FORCE=1 (skip model check)
#   NOOBS dry run: DRY_RUN=1 FAKE_BOOTDEV=/dev/mmcblk0p6 P1_DIR=/some/dir
set -u
PINN_URL="https://sourceforge.net/projects/pinn/files/pinn-lite.zip/download"
PINN_SHA256="9c87d16a8092981296e426503e99e539463951796c1c3e408f111e4e2d8684d0"  # v3.8.8, checked 2026-11-04
BOOT="${BOOT_DIR:-/boot}"
DRY="${DRY_RUN:-0}"
STAGE=/root/pinn-stage
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=/root/boot-backup-$STAMP.tar.gz

say()  { echo -e "\e[1;36m==>\e[0m $*"; }
warn() { echo -e "\e[1;33m!!\e[0m $*"; }
die()  { echo -e "\e[1;31mABORTED:\e[0m $*  (nothing on /boot was changed)"; exit 1; }

# ---------- 1. safety checks ----------
[ "$(id -u)" = 0 ] || die "run it with sudo:  sudo bash $0"

if [ "$DRY" != 1 ] && [ "${FORCE:-0}" != 1 ]; then
  MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null)
  case "$MODEL" in
    *"Raspberry Pi 3 Model B Plus"*|*"Raspberry Pi 3 Model B"*) say "Model: $MODEL";;
    *) die "this is '$MODEL', not a Pi 3B/3B+ (the PINN file trim is for Pi 3). FORCE=1 to override.";;
  esac
fi

# Layout: normal card boots /boot = p1. A NOOBS card boots p1 (NOOBS recovery),
# which hands over to Stretch's /boot on p6 -- then PINN goes onto p1 instead.
if [ "$DRY" != 1 ]; then
  mountpoint -q "$BOOT" || die "$BOOT is not mounted"
  BOOTDEV=$(findmnt -n -o SOURCE "$BOOT")
else
  BOOTDEV=${FAKE_BOOTDEV:-/dev/mmcblk0p1}
fi
case "$BOOTDEV" in
  /dev/mmcblk0p1)
    LAYOUT=plain
    if [ "$DRY" != 1 ]; then
      [ "$(findmnt -n -o FSTYPE "$BOOT")" = vfat ] || die "$BOOT is not vfat"
      mount -o remount,rw "$BOOT" || die "cannot make $BOOT writable"
    fi
    say "Normal layout: PINN goes onto $BOOT (partition 1)";;
  /dev/mmcblk0p[5-9]|/dev/mmcblk0p[1-9][0-9])
    LAYOUT=noobs
    P1=/dev/mmcblk0p1
    if [ "$DRY" != 1 ]; then
      [ -b "$P1" ] || die "$BOOT is on $BOOTDEV but there is no $P1"
      [ "$(blkid -o value -s TYPE "$P1")" = vfat ] || die "$P1 is not a FAT partition -- not the NOOBS layout I expected"
      BOOT=/mnt/pinn-p1; mkdir -p "$BOOT"
      if ! mountpoint -q "$BOOT"; then mount -t vfat -o rw "$P1" "$BOOT" || die "cannot mount $P1"; fi
    else
      BOOT=${P1_DIR:?set P1_DIR for a NOOBS dry run}
    fi
    if ls "$BOOT" | grep -qiE '^(recovery\.elf|recovery\.img|RECOVERY_FILES_DO_NOT_EDIT)$'; then
      say "NOOBS layout: Stretch boots from $BOOTDEV, NOOBS recovery on $P1 -> PINN replaces NOOBS on $P1"
    else
      die "$P1 has no NOOBS recovery files -- unknown layout, not touching it"
    fi
    [ -f "$BOOT/autoboot.txt" ] && say "(its autoboot.txt goes too, so the Pi boots PINN, not Stretch)";;
  *) die "$BOOT is on $BOOTDEV -- unknown SD layout, not touching it";;
esac

FREE_ROOT=$(df -Pk /root | awk 'NR==2{print $4}')
[ "${FREE_ROOT:-0}" -gt 200000 ] || die "need ~200 MB free on the root filesystem, have $((FREE_ROOT/1024)) MB"

# clock: TLS fails if the Pi thinks it is 2018
if [ "$(date +%Y)" -lt 2025 ]; then
  warn "clock says $(date); fetching time over HTTP"
  D=$(wget -qS --spider http://www.google.com 2>&1 | sed -n 's/^ *[Dd]ate: //p' | head -1)
  [ -n "$D" ] && date -s "$D" >/dev/null && say "clock set to $(date)"
fi

cat <<'EOF'

 #####################################################################
 #  WARNING: after the reboot PINN ERASES THE WHOLE SD CARD.          #
 #  Raspbian Stretch, /home, every file on this Pi will be gone.     #
 #  Copy anything you need off the Pi BEFORE you continue.           #
 #####################################################################

EOF
if [ "$DRY" != 1 ]; then
  read -r -p "Type WIPE (capitals) to continue, anything else to quit: " ANS </dev/tty
  [ "$ANS" = WIPE ] || die "you did not type WIPE"
fi

# ---------- 2. download + verify ----------
rm -rf "$STAGE"; mkdir -p "$STAGE" || die "cannot create $STAGE"
ZIP="$STAGE/pinn-lite.zip"
SECURE=1
say "Downloading PINN Lite (~50 MB) ..."
fetch() {  # $1 = extra flags
  if command -v wget >/dev/null; then wget $1 -q --show-progress -O "$ZIP" "$PINN_URL" 2>&1 || wget $1 -q -O "$ZIP" "$PINN_URL"
  else curl $([ "$1" ] && echo -k) -fL -o "$ZIP" "$PINN_URL"; fi
}
if ! fetch "" || [ ! -s "$ZIP" ]; then
  warn "HTTPS certificate check failed (old Stretch certificates?) -- retrying without it; the sha256 check below still protects you"
  SECURE=0
  fetch "--no-check-certificate" && [ -s "$ZIP" ] || \
    { command -v curl >/dev/null && curl -kfL -o "$ZIP" "$PINN_URL"; } || die "download failed -- is the hotspot connected?  try: ping -c3 8.8.8.8"
fi
GOT=$(sha256sum "$ZIP" | cut -d' ' -f1)
if [ "$GOT" != "$PINN_SHA256" ]; then
  if [ "$SECURE" = 1 ]; then
    warn "sha256 differs from v3.8.8 (PINN probably released a newer build); download was over verified HTTPS, continuing"
  else
    die "sha256 mismatch ($GOT) on an unverified download -- refusing. Fix the clock / certificates (sudo apt-get install ca-certificates) and retry"
  fi
fi
say "Download OK (sha256 ${GOT:0:12}...)"

# ---------- 3. unpack + trim for Pi 3B+ ----------
mkdir -p "$STAGE/files"
if command -v unzip >/dev/null; then unzip -q "$ZIP" -d "$STAGE/files" || die "unzip failed (corrupt download?)"
else python3 -m zipfile -e "$ZIP" "$STAGE/files" || die "unpack failed (no unzip, python3 failed)"; fi
F="$STAGE/files"
[ -f "$F/recovery.rfs" ] && [ -f "$F/recovery7.img" ] && [ -f "$F/recovery.cmdline" ] || die "zip does not look like PINN Lite"

# Pi 3B+ boots 32-bit recovery7.img + recovery.elf; drop Pi 1/Zero/Pi 4 files
rm -f "$F"/recovery.img "$F"/recovery7l.img "$F"/recover4.elf "$F"/fixup4*.dat \
      "$F"/bcm2708-*.dtb "$F"/bcm2709-*.dtb "$F"/bcm2711-*.dtb "$F"/riscos-boot.bin

# Wi-Fi: PINN reads wpa_supplicant.conf from its own partition
WPA=/etc/wpa_supplicant/wpa_supplicant.conf
if [ -f "$WPA" ] && grep -q 'ssid' "$WPA"; then
  cp "$WPA" "$F/wpa_supplicant.conf" && say "Wi-Fi settings copied ($(grep -o 'ssid="[^"]*"' "$WPA" | head -3 | tr '\n' ' '))"
  grep -q '^country=' "$F/wpa_supplicant.conf" || sed -i '1i country=US' "$F/wpa_supplicant.conf"
else
  warn "no Wi-Fi config found -- in PINN click the Wi-Fi button and join the hotspot by hand"
fi

# reformat without asking, even if it thinks an OS is present
grep -q forceruninstaller "$F/recovery.cmdline" || sed -i '1s/^runinstaller/runinstaller forceruninstaller/' "$F/recovery.cmdline"

# ---------- size check (FAT cluster rounding) ----------
if [ "$DRY" != 1 ]; then BS=$(stat -f -c %S "$BOOT"); TOTAL=$(( $(stat -f -c %b "$BOOT") * BS ))
else BS=${FAKE_BS:-2048}; TOTAL=${FAKE_TOTAL:-$((42*1024*1024))}; fi
need() { find "$F" -type f -printf '%s\n' | awk -v bs="$BS" '{n+=int(($1+bs-1)/bs)*bs} END{print n + 1048576}'; }
NEED=$(need)
say "PINN needs $((NEED/1024/1024)) MB, $BOOT holds $((TOTAL/1024/1024)) MB"
if [ "$NEED" -gt "$TOTAL" ]; then
  warn "too tight -- dropping wallpaper, slideshow and unused overlays"
  rm -f "$F/wallpaper.jpg"; rm -rf "$F/defaults/slides" "$F/overlays"; mkdir -p "$F/overlays"
  NEED=$(need)
  say "now needs $((NEED/1024/1024)) MB"
  [ "$NEED" -le "$TOTAL" ] || die "PINN ($((NEED/1024/1024)) MB) cannot fit in the $((TOTAL/1024/1024)) MB boot partition"
fi
( cd "$F" && find . -type f -exec md5sum {} + | sort -k2 ) > "$STAGE/manifest.md5"

# ---------- 4. backup /boot ----------
say "Backing up $BOOT to $BACKUP (survives only until PINN wipes the card)"
if [ "$LAYOUT" = noobs ]; then
  # skip NOOBS's stored OS images (can be >1 GB); NOOBS still boots Stretch without them
  tar -C "$BOOT" --exclude='./os/*/*.tar.xz' --exclude='./os/*/*.tar' -czf "$BACKUP" . || die "backup of $BOOT failed"
else
  tar -C "$BOOT" -czf "$BACKUP" . || die "backup of $BOOT failed"
fi

# ---------- 5. swap in PINN ----------
restore() {
  warn "copy failed -- restoring the old $BOOT"
  find "$BOOT" -mindepth 1 -delete 2>/dev/null
  tar -C "$BOOT" -xzf "$BACKUP" --no-same-owner && sync && \
    echo "Old /boot restored; Stretch will still boot. Nothing else changed." || \
    echo "RESTORE FAILED: do NOT reboot; run:  sudo tar -C $BOOT -xzf $BACKUP --no-same-owner"
  exit 1
}
say "Replacing $BOOT with PINN ..."
find "$BOOT" -mindepth 1 -delete || restore
cp -r "$F"/. "$BOOT"/ || restore
sync
( cd "$BOOT" && md5sum --quiet -c "$STAGE/manifest.md5" ) || restore
say "All $(wc -l <"$STAGE/manifest.md5") PINN files verified on $BOOT"

cat <<'EOF'

 Next on the screen:
  - rainbow square, then "PINN" with "Resizing FAT partition / Formatting settings partition"
  - the PINN menu: Wi-Fi joins the hotspot by itself (else click the Wi-Fi button)
  - open the "Trixie" tab, tick  raspios_arm64  (desktop) or  raspios_arm64_lite
    (no desktop), click Install, confirm. ~20-40 min on a hotspot.
  - if the screen stays black: press 2 (HDMI safe mode)

EOF
if [ "$DRY" = 1 ]; then say "DRY_RUN: not rebooting"; exit 0; fi
for i in 10 9 8 7 6 5 4 3 2 1; do printf '\rRebooting into PINN in %2s s (Ctrl-C to stop; then restore with: sudo tar -C %s -xzf %s) ' $i "$BOOT" "$BACKUP"; sleep 1; done
echo; sync; reboot
