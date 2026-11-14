"""The update bundle: pi-traffic-tester-<version>.ptt = tar.gz holding
     manifest.json   version, date, notes, min installer level, requirements info, sha256 + size + mode of EVERY other file
     manifest.sig    "ed25519:<hex>" detached signature over the exact bytes of manifest.json
     app/...         what install-pi.sh deploys into <install>/app (traffic_tester/, requirements.txt, scripts/...)
     extra/...       installer scripts the system install keeps next to the app
     wheels/<tag>/*.whl   aarch64 wheels for the Python versions of Raspberry Pi OS (only when requirements changed)
A bundle is accepted only if the signature verifies against the public key built into the app, every file matches its hash,
and the archive holds nothing the manifest does not list. Nothing here ever executes bundle content."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
import time
from typing import Callable, Dict, Optional, Tuple

from . import APP_NAME, BUNDLE_FORMAT, INSTALLER_LEVEL, ed25519

MAX_BYTES = 400 * 1024 * 1024          # unpacked size cap (a bundle with wheels is ~30 MB)
MAX_FILES = 5000
MAX_MANIFEST = 4 * 1024 * 1024
_NAME_OK = re.compile(r"^[A-Za-z0-9._+@=~ ,()\-/]+$")


class BundleError(Exception):
    """A bundle that must not be installed. `code` is for tests/UI, `message` is plain language for the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ------------------------------------------------------------------ versions
def parse_version(v: str) -> Tuple[int, ...]:
    m = re.match(r"^\s*v?(\d+(?:\.\d+){0,3})\s*$", str(v or ""))
    if not m:
        raise BundleError("malformed", f"'{v}' is not a version number (expected like 1.2.3).")
    return tuple(int(x) for x in m.group(1).split("."))


def cmp_version(a: str, b: str) -> int:
    pa, pb = parse_version(a), parse_version(b)
    n = max(len(pa), len(pb))
    pa, pb = pa + (0,) * (n - len(pa)), pb + (0,) * (n - len(pb))
    return (pa > pb) - (pa < pb)


def bump_version(v: str, part: str = "patch") -> str:
    p = list(parse_version(v)) + [0, 0, 0]
    p = p[:3]
    if part == "major":
        return f"{p[0] + 1}.0.0"
    if part == "minor":
        return f"{p[0]}.{p[1] + 1}.0"
    return f"{p[0]}.{p[1]}.{p[2] + 1}"


def bundle_filename(version: str) -> str:
    return f"{APP_NAME}-{version}.ptt"


# ------------------------------------------------------------------ names
def safe_name(name: str) -> str:
    """Normalised archive path, or BundleError. Relative, no '..', no backslashes, plain characters only."""
    if not name or "\x00" in name or "\\" in name or name.startswith("/") or not _NAME_OK.match(name):
        raise BundleError("unsafe", f"The bundle holds a file name that is not allowed ({name[:60]!r}). Nothing was installed.")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise BundleError("unsafe", f"The bundle holds a file name that is not allowed ({name[:60]!r}). Nothing was installed.")
    return "/".join(parts)


# ------------------------------------------------------------------ build (release machine)
def build_bundle(out_path: str, files: Dict[str, "bytes | str"], seed: bytes, *, version: str, notes: str = "", date: Optional[str] = None,
                 requirements_changed: bool = False, min_installer: int = INSTALLER_LEVEL, extra_fields: Optional[dict] = None) -> dict:
    """files: archive path -> bytes, or a filesystem path (str) to read. Returns the manifest written into the bundle."""
    parse_version(version)
    entries, blobs = {}, {}
    for name in sorted(files):
        n = safe_name(name)
        src = files[name]
        if isinstance(src, str):
            with open(src, "rb") as f:
                data = f.read()
            mode = 0o755 if os.stat(src).st_mode & stat.S_IXUSR else 0o644
        else:
            data, mode = bytes(src), (0o755 if name.endswith((".sh", "tt-rc-run", "tt-rc-sync")) else 0o644)
        entries[n] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": mode}
        blobs[n] = data
    manifest = {"format": BUNDLE_FORMAT, "name": APP_NAME, "version": version, "date": date or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "notes": notes, "min_installer": min_installer, "requirements_changed": bool(requirements_changed),
                "wheel_tags": sorted({n.split("/")[1] for n in entries if n.startswith("wheels/") and n.count("/") >= 2}),
                "files": entries, **(extra_fields or {})}
    mbytes = json.dumps(manifest, indent=1, sort_keys=True).encode()
    sig = ("ed25519:" + ed25519.sign(seed, mbytes).hex() + "\n").encode()
    tmp = out_path + ".tmp"
    with tarfile.open(tmp, "w:gz", compresslevel=9) as tf:
        def add(name: str, data: bytes, mode: int):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.mtime = len(data), mode, 0
            ti.uname = ti.gname = ""
            tf.addfile(ti, io.BytesIO(data))
        add("manifest.json", mbytes, 0o644)
        add("manifest.sig", sig, 0o644)
        for n in sorted(blobs):
            add(n, blobs[n], entries[n]["mode"])
    os.replace(tmp, out_path)
    return manifest


# ------------------------------------------------------------------ verify (+ optional extract) on the Pi
def verify_bundle(path: str, pubkey: "bytes | str", dest: Optional[str] = None, progress: Optional[Callable[[str], None]] = None) -> dict:
    """Check `path` against `pubkey` (32 bytes or hex). If `dest` is given the files are also written there (hash-checked while writing;
    on any failure `dest` is left to the caller to delete). Returns the verified manifest. Raises BundleError."""
    if isinstance(pubkey, str):
        pubkey = bytes.fromhex(pubkey)
    try:
        tf = tarfile.open(path, "r:gz")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise BundleError("malformed", "This file is not an update bundle (it is not a valid .ptt archive).") from e
    manifest = None
    seen: Dict[str, str] = {}
    total = count = 0
    try:
        with tf:
            it = iter(tf)
            first = next(it, None)
            second = next(it, None)
            if not first or first.name != "manifest.json" or not first.isreg():
                raise BundleError("malformed", "This file is not an update bundle (no manifest.json at the start).")
            if first.size > MAX_MANIFEST:
                raise BundleError("malformed", "The bundle's manifest is too large.")
            mbytes = tf.extractfile(first).read()
            if not second or second.name != "manifest.sig" or not second.isreg() or second.size > 4096:
                raise BundleError("unsigned", "This bundle is not signed (manifest.sig is missing). Only bundles signed by the release key are accepted. Nothing was installed.")
            sigtxt = tf.extractfile(second).read().decode("ascii", "replace").strip()
            if not sigtxt.startswith("ed25519:") or not re.fullmatch(r"[0-9a-f]{128}", sigtxt[8:]):
                raise BundleError("unsigned", "The bundle's signature is not in the expected format. Nothing was installed.")
            if not ed25519.verify(pubkey, mbytes, bytes.fromhex(sigtxt[8:])):
                raise BundleError("bad_signature", "The signature does not match: this bundle was not signed by the release key (or was changed after signing). Nothing was installed.")
            try:
                manifest = json.loads(mbytes)
                files = manifest["files"]
                assert manifest.get("name") == APP_NAME and isinstance(files, dict) and isinstance(manifest["version"], str)
                parse_version(manifest["version"])
            except BundleError:
                raise
            except Exception as e:
                raise BundleError("malformed", "The bundle's manifest is not valid.") from e
            if manifest.get("format") != BUNDLE_FORMAT:
                raise BundleError("malformed", f"This bundle uses format {manifest.get('format')}; this app understands format {BUNDLE_FORMAT}. Update the app first with a full install.")
            for name in files:
                safe_name(name)
            if len(files) > MAX_FILES or sum(int(v.get("size", 0)) for v in files.values()) > MAX_BYTES:
                raise BundleError("too_big", "The bundle is larger than allowed. Nothing was installed.")
            for m in it:
                count += 1
                if count > MAX_FILES + 8:
                    raise BundleError("too_big", "The bundle holds too many files. Nothing was installed.")
                if m.isdir():
                    safe_name(m.name.rstrip("/"))
                    continue
                if not m.isreg():
                    raise BundleError("unsafe", f"The bundle holds something that is not a plain file ({m.name[:60]!r}). Nothing was installed.")
                name = safe_name(m.name)
                want = files.get(name)
                if want is None or name in seen:
                    raise BundleError("tampered", f"The bundle holds a file the signed manifest does not list ({name[:60]}). Nothing was installed.")
                total += m.size
                if total > MAX_BYTES or m.size != want.get("size"):
                    raise BundleError("tampered", f"A file in the bundle ({name[:60]}) has the wrong size. Nothing was installed.")
                h = hashlib.sha256()
                src = tf.extractfile(m)
                out = None
                if dest is not None:
                    target = os.path.join(dest, *name.split("/"))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    out = open(target, "wb")
                try:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        h.update(chunk)
                        if out:
                            out.write(chunk)
                finally:
                    if out:
                        out.close()
                if h.hexdigest() != want.get("sha256"):
                    raise BundleError("tampered", f"A file in the bundle ({name[:60]}) does not match its signed checksum: the bundle was damaged or changed. Nothing was installed.")
                if dest is not None:
                    os.chmod(target, 0o755 if int(want.get("mode", 0o644)) & 0o100 else 0o644)
                seen[name] = want["sha256"]
                if progress:
                    progress(name)
            missing = [n for n in files if n not in seen]
            if missing:
                raise BundleError("tampered", f"The bundle is missing {len(missing)} file(s) its manifest lists (e.g. {missing[0][:60]}). Nothing was installed.")
    except BundleError:
        raise
    except (tarfile.TarError, OSError, EOFError, UnicodeError) as e:
        raise BundleError("malformed", f"The bundle could not be read ({type(e).__name__}). It may be incomplete. Nothing was installed.") from e
    return manifest


# ------------------------------------------------------------------ rules about WHEN a verified bundle may be installed
def check_installable(manifest: dict, current_version: str, installer_level: int = INSTALLER_LEVEL, allow_older: bool = False) -> None:
    new = manifest["version"]
    c = cmp_version(new, current_version)
    if c < 0 and not allow_older:
        raise BundleError("downgrade", f"This update is version {new}, older than the installed {current_version}. Tick \"Allow an older version\" to install it anyway.")
    if c == 0 and not allow_older:
        raise BundleError("same_version", f"Version {new} is already installed. Tick \"Allow an older version\" to reinstall it.")
    need = int(manifest.get("min_installer", 1))
    if need > installer_level:
        raise BundleError("installer_too_old", f"This update needs a newer installer (level {need}; this Pi has {installer_level}). Run install-pi.sh from the new full package once, then updates work again. Nothing was changed.")


def wheel_tag(version_info) -> str:
    return f"cp{version_info[0]}{version_info[1]}"
