#!/usr/bin/env bash
# Push a Pi update: ONE command.   ./scripts/publish-update.sh ["commit title"]  [--minor|--major|--version X.Y.Z] [--no-tests] [--no-usb]
# The release notes are the CHANGELOG.md entry of the new version: the script REFUSES to publish if that entry is missing or empty.
# Bumps the version, runs the unit tests, builds + signs the bundle (~/.config/pi-traffic-tester/release-signing.key), pushes it to the private releases
# repo (branch "stable", always ONE commit so the repo stays small) and copies the .ptt to /mnt/d/pi-usb/ for the "upload a file" route.
# Field Pis then see it under Settings -> Software update -> Check online. (Or tell Claude: "push the Pi update".)
# Overrides (tests): TT_RELEASES_REMOTE=<git url or path>  TT_RELEASE_USB=<dir>  TT_RELEASE_KEY=<key file>
set -euo pipefail
cd "$(dirname "$0")/.."
REMOTE="${TT_RELEASES_REMOTE:-https://github.com/OWNER/pi-traffic-tester-releases.git}"
USB="${TT_RELEASE_USB-/mnt/d/pi-usb}"
NOTES=""; PART=patch; FORCEV=""; TESTS=1; DOUSB=1
while [ $# -gt 0 ]; do
  case "$1" in
    --minor) PART=minor; shift;; --major) PART=major; shift;; --version) FORCEV="$2"; shift 2;;
    --no-tests) TESTS=0; shift;; --no-usb) DOUSB=0; shift;;
    -h|--help) sed -n 2,8p "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) if [ -z "$NOTES" ]; then NOTES="$1"; shift; else echo "unexpected argument: $1" >&2; exit 2; fi;;
  esac
done
PY=python3; [ -x venv/bin/python ] && PY=venv/bin/python
INIT=traffic_tester/__init__.py
OLD="$(sed -n 's/.*__version__ *= *"\(.*\)".*/\1/p' $INIT)"
if [ -n "$FORCEV" ]; then NEW="$FORCEV"; else NEW="$($PY -c "from traffic_tester.update.bundle import bump_version;print(bump_version('$OLD','$PART'))")"; fi
# the release notes ARE the changelog entry: no entry, no release
$PY - "$NEW" <<'PYEOF' || { echo "CHANGELOG.md has no entry (with at least one bullet) for version $NEW: write what changed for users, then run this again. Nothing was published." >&2; exit 1; }
import sys
from traffic_tester import changelog as C
e = C.entry(C.load("."), sys.argv[1])
sys.exit(0 if e and e["notes"] else 1)
PYEOF
[ -n "$NOTES" ] || NOTES="Release $NEW"
$PY scripts/release-keys.py init >/dev/null      # makes the key if missing; keeps pubkey.py in step
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
restore() { sed -i "s/__version__ = \"$NEW\"/__version__ = \"$OLD\"/" $INIT; }
echo "==> version $OLD -> $NEW"
sed -i "s/__version__ = \"$OLD\"/__version__ = \"$NEW\"/" $INIT
trap 'restore; rm -rf "$W"' ERR
if [ "$TESTS" = 1 ]; then
  echo "==> unit tests"; $PY -m pytest -q -p no:cacheprovider -x tests 2>&1 | tail -3
  [ "${PIPESTATUS[0]}" = 0 ] || { echo "Unit tests FAILED: nothing was published." >&2; restore; exit 1; }
fi
echo "==> fetching the releases repo"
GIT=(git -c user.name="$(git log -1 --format=%an)" -c user.email="$(git log -1 --format=%ae)")
git clone -q --depth 1 --branch stable "$REMOTE" "$W/rel" 2>/dev/null || { git clone -q "$REMOTE" "$W/rel"; }
PREV=""; [ -f "$W/rel/latest.json" ] && PREV="$($PY -c "import json;print(json.load(open('$W/rel/latest.json')).get('requirements_sha256',''))")"
echo "==> building and signing"
python3 scripts/build-update.py --out "$W/out" --notes "$NOTES" --prev-requirements-sha "$PREV" > "$W/path"
BUNDLE="$(cat "$W/path")"; FN="$(basename "$BUNDLE")"
$PY - "$BUNDLE" "$W/rel/latest.json" <<'PYEOF'
import hashlib, json, sys, tarfile
b, out = sys.argv[1], sys.argv[2]
m = json.loads(tarfile.open(b).extractfile("manifest.json").read())
json.dump({"version": m["version"], "date": m["date"], "file": b.split("/")[-1], "sha256": hashlib.sha256(open(b, "rb").read()).hexdigest(), "size": len(open(b, "rb").read()),
           "notes": m["notes"], "requirements_changed": m["requirements_changed"], "requirements_sha256": m.get("requirements_sha256", ""), "min_installer": m["min_installer"]}, open(out, "w"), indent=1)
PYEOF
echo "==> pushing to the releases repo (one commit, old bundles dropped)"
cd "$W/rel"
git checkout -q --orphan new 2>/dev/null; git rm -rq --cached . 2>/dev/null || true; find . -maxdepth 1 -name '*.ptt' -delete
cp "$BUNDLE" "./$FN"
printf '# Pi Automation Traffic Tester releases\n\nSigned update bundles (.ptt). The Pi checks the signature, so only bundles from the release key install. Latest: %s\n' "$NEW" > README.md
"${GIT[@]}" add -A; "${GIT[@]}" commit -q -m "Release $NEW: $NOTES"
git branch -M stable; git push -q --force origin stable
cd - >/dev/null
if [ "$DOUSB" = 1 ] && [ -n "$USB" ] && [ -d "$USB" ]; then
  find "$USB" -maxdepth 1 -name 'pi-traffic-tester-*.ptt' -delete; cp "$BUNDLE" "$USB/$FN"; echo "==> copied to $USB/$FN (for the upload route)"
fi
trap 'rm -rf "$W"' ERR
git add $INIT && git -c user.name="$(git log -1 --format=%an)" -c user.email="$(git log -1 --format=%ae)" commit -q -m "Release $NEW: $NOTES

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -- $INIT
echo "==> published $NEW ($(( $(stat -c %s "$BUNDLE") / 1024 )) KB). Field Pis: Settings -> Software update -> Check for updates."
[ -x "$(dirname "$0")/dev/sync-public.sh" ] && { echo "==> syncing the public repo (non-blocking)"; timeout 600 "$(dirname "$0")/dev/sync-public.sh" || echo "(public sync failed; the release itself is fine)"; } || true
