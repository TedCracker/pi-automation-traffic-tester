#!/usr/bin/env python3
"""Build + sign an update bundle (pi-traffic-tester-<version>.ptt) from this working tree.
   build-update.py --out DIR --notes "what changed" [--prev-requirements-sha SHA] [--wheels auto|always|never]
Signs with ~/.config/pi-traffic-tester/release-signing.key (never printed). Wheels (aarch64, Python 3.11 = Bookworm, 3.13 = Trixie) are added when
requirements.txt differs from the previous release (--prev-requirements-sha) or with --wheels always. Prints the bundle path."""
import argparse, hashlib, os, re, shutil, subprocess, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "scripts"))
from traffic_tester.update import bundle as B, ed25519
import importlib.util
spec = importlib.util.spec_from_file_location("release_keys", os.path.join(ROOT, "scripts", "release-keys.py")); RK = importlib.util.module_from_spec(spec); spec.loader.exec_module(RK)

APP_SCRIPTS = ["net-split.sh", "tt-rc-run", "tt-rc-sync", "claude_real_probe.py"]
EXTRA = ["install-pi.sh", "install-claude.sh", "install-wifi-nexmon.sh"]
CACHE = os.path.expanduser("~/.cache/pi-traffic-tester/wheels")


def version():
    return re.search(r'__version__\s*=\s*"([^"]+)"', open(os.path.join(ROOT, "traffic_tester", "__init__.py")).read()).group(1)


def fetch_wheels(tag: str, req: str) -> dict:
    pv = f"{tag[2]}.{tag[3:]}"
    d = os.path.join(CACHE, tag, hashlib.sha256(open(req, "rb").read()).hexdigest()[:12])
    if not os.path.isdir(d) or not os.listdir(d):
        os.makedirs(d, exist_ok=True)
        cmd = [sys.executable, "-m", "pip", "download", "-q", "-r", req, "-d", d, "--only-binary=:all:", "--python-version", pv, "--implementation", "cp"]
        for plat in ("manylinux2014_aarch64", "manylinux_2_17_aarch64", "manylinux_2_28_aarch64", "manylinux_2_34_aarch64", "linux_aarch64", "any"):
            cmd += ["--platform", plat]
        subprocess.run(cmd, check=True)
    return {f"wheels/{tag}/{fn}": os.path.join(d, fn) for fn in sorted(os.listdir(d)) if fn.endswith(".whl")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True); ap.add_argument("--notes", default="")
    ap.add_argument("--prev-requirements-sha", default=""); ap.add_argument("--wheels", default="auto", choices=["auto", "always", "never"])
    ap.add_argument("--key", default=RK.KEY)
    a = ap.parse_args()
    ver = version(); os.makedirs(a.out, exist_ok=True)
    date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    req = os.path.join(ROOT, "requirements.txt"); rsha = hashlib.sha256(open(req, "rb").read()).hexdigest()
    changed = a.wheels == "always" or (a.wheels == "auto" and rsha != a.prev_requirements_sha)
    files = {}
    for dp, dn, fn in os.walk(os.path.join(ROOT, "traffic_tester")):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if f.endswith((".pyc", ".pyo")): continue
            full = os.path.join(dp, f); files["app/" + os.path.relpath(full, ROOT).replace(os.sep, "/")] = full
    files["app/requirements.txt"] = req
    from traffic_tester import changelog as CL
    cl_path = os.path.join(ROOT, "CHANGELOG.md")
    entries = CL.parse(open(cl_path, encoding="utf-8").read()) if os.path.isfile(cl_path) else []
    if os.path.isfile(cl_path):
        files["app/CHANGELOG.md"] = cl_path
    for f in APP_SCRIPTS:
        if os.path.isfile(os.path.join(ROOT, "scripts", f)): files["app/scripts/" + f] = os.path.join(ROOT, "scripts", f)
    for f in EXTRA:
        files["extra/" + f] = os.path.join(ROOT, "scripts", f)
    import json
    cur = CL.entry(entries, ver)
    notes = CL.notes_text(cur) if cur and cur["notes"] else a.notes
    files["app/release.json"] = json.dumps({"version": ver, "date": date, "notes": notes}, indent=1).encode()
    if changed:
        for tag in ("cp311", "cp313"):
            files.update(fetch_wheels(tag, req))
    seed = RK.load_seed(a.key)
    assert ed25519.public_key(seed).hex() == __import__("traffic_tester.update.pubkey", fromlist=["x"]).PUBLIC_KEY_HEX, "the signing key does not match the public key built into the app (run scripts/release-keys.py init)"
    out = os.path.join(a.out, B.bundle_filename(ver))
    m = B.build_bundle(out, files, seed, version=ver, notes=notes, date=date, requirements_changed=changed, extra_fields={"requirements_sha256": rsha, "changelog": entries})
    B.verify_bundle(out, ed25519.public_key(seed))           # never publish something the Pi would refuse
    print(out); print(f"version {ver} · {len(m['files'])} files · {os.path.getsize(out)//1024} KB · wheels: {','.join(m['wheel_tags']) or 'none'}", file=sys.stderr)


if __name__ == "__main__":
    main()
