"""The installer/rollback helper. Standard library only. Runs
   - system install: as root, started by the oneshot unit traffic-tester-update.service (the web app, which is NOT root, only asks systemd to start it);
                     it reads its paths from the root-owned /etc/traffic-tester/update.conf and trusts only its own copy of the public key.
   - rootless install: as the same user, detached from the app via `systemd-run --user`, paths from the layout file the app wrote.
It never trusts the bundle: copy -> verify signature + every hash -> unpack to a staging dir -> (new Python packages if needed) -> keep the current
app as app.prev -> swap -> restart -> health check (page + /api/status within 60 s) -> otherwise automatic rollback. Data (tests, results,
settings) lives outside the app directory and is never touched."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, List, Optional

if __package__ in (None, ""):          # run as a script from a copied folder: make `traffic_tester.update` importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    __package__ = "traffic_tester.update"

from . import INSTALLER_LEVEL  # noqa: E402
from .bundle import BundleError, check_installable, verify_bundle, wheel_tag  # noqa: E402

SYSTEM_CONF = "/etc/traffic-tester/update.conf"
SYSTEM_PUBKEY = "/etc/traffic-tester/update-pubkey.hex"
STAGED_NAME = "staged.ptt"
HEALTH_SECONDS = 60


@dataclass
class Layout:
    mode: str                                   # "system" | "user"
    app_dir: str                                # .../app  (the folder holding traffic_tester/)
    data_dir: str                               # saved tests/results/settings (never touched); the app drops the bundle in <data>/update/inbox
    state_dir: str                              # where status.json is written (root-owned for the system install)
    work_dir: str                               # private scratch (copy of the bundle, staging)
    pubkey: bytes
    service: str = "traffic-tester.service"
    port: int = 8080
    venv_python: Optional[str] = None
    svc_user: Optional[str] = None
    installer_level: int = INSTALLER_LEVEL
    health_seconds: float = HEALTH_SECONDS

    @property
    def parent(self) -> str:
        return os.path.dirname(os.path.abspath(self.app_dir))

    @property
    def prev_dir(self) -> str:
        return self.app_dir + ".prev"

    @property
    def inbox(self) -> str:
        return os.path.join(self.data_dir, "update", "inbox")

    @property
    def request_path(self) -> str:
        return os.path.join(self.data_dir, "update", "request.json")


class Refused(Exception):
    """The update was refused BEFORE anything was changed."""


# ------------------------------------------------------------------ status file
def status_path(state_dir: str) -> str:
    return os.path.join(state_dir, "status.json")


def read_status(state_dir: str) -> dict:
    try:
        with open(status_path(state_dir)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"state": "idle"}


class Status:
    def __init__(self, lay: Layout, action: str, to_version: str = "", from_version: str = ""):
        self.lay = lay
        self.d = {"state": "working", "action": action, "phase": "starting", "message": "", "from_version": from_version, "to_version": to_version,
                  "started": time.time(), "ts": time.time(), "log": []}

    def update(self, **kw):
        msg = kw.pop("log", None)
        self.d.update(kw)
        self.d["ts"] = time.time()
        line = msg or kw.get("message")
        if line:
            self.d["log"] = (self.d["log"] + [time.strftime("%H:%M:%S ") + line])[-40:]
        os.makedirs(self.lay.state_dir, exist_ok=True)
        tmp = os.path.join(self.lay.state_dir, f".status.{os.getpid()}.tmp")
        try:
            os.unlink(tmp)                                                                      # a planted link is removed, never followed
        except OSError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(self.d, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, status_path(self.lay.state_dir))


# ------------------------------------------------------------------ helpers
def installed_version(app_dir: str) -> str:
    try:
        with open(os.path.join(app_dir, "traffic_tester", "__init__.py")) as f:
            m = re.search(r'__version__\s*=\s*"([^"]+)"', f.read())
        return m.group(1) if m else "0"
    except OSError:
        return "0"


def _run(cmd: List[str], timeout: float = 120, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def systemctl_service(lay: Layout, action: str) -> None:
    cmd = ["systemctl"] + (["--user"] if lay.mode == "user" else []) + [action, lay.service]
    r = _run(cmd, timeout=90)
    if r.returncode and action != "stop":
        raise RuntimeError(f"systemctl {action} failed: {(r.stderr or r.stdout).strip()[:200]}")


def http_health(lay: Layout, expect_version: str, seconds: Optional[float] = None) -> Optional[str]:
    """None when healthy, else why not. Healthy = the main page and /api/status answer 200 (and report the expected version) within the time."""
    deadline = time.time() + (lay.health_seconds if seconds is None else seconds)
    why = "no answer yet"
    while True:
        try:
            base = f"http://127.0.0.1:{lay.port}"
            with urllib.request.urlopen(base + "/", timeout=5) as r:
                if r.status != 200:
                    raise RuntimeError(f"main page answered {r.status}")
            with urllib.request.urlopen(base + "/api/status", timeout=5) as r:
                st = json.loads(r.read() or b"{}")
            if expect_version and st.get("version") != expect_version:
                raise RuntimeError(f"the service reports version {st.get('version')}, not {expect_version}")
            return None
        except Exception as e:
            why = str(e)[:160]
        if time.time() >= deadline:
            return why
        time.sleep(1.0)


def _need_packages(lay: Layout, req_file: str) -> Optional[bool]:
    """True = new packages are needed, False = the venv already satisfies the requirements, None = cannot tell."""
    if not lay.venv_python or not os.path.isfile(req_file):
        return None
    r = _run([lay.venv_python, "-m", "pip", "install", "--dry-run", "--no-index", "--disable-pip-version-check", "-q", "-r", req_file], timeout=120)
    if r.returncode == 0:
        return False
    out = (r.stderr + r.stdout)
    if "no such option" in out.lower() or "dry-run" in out.lower() and "usage" in out.lower():
        return None
    return True


def _py_tag(lay: Layout) -> Optional[str]:
    if not lay.venv_python:
        return None
    r = _run([lay.venv_python, "-c", "import sys;print(sys.version_info[0],sys.version_info[1])"], timeout=20)
    try:
        a, b = r.stdout.split()
        return wheel_tag((int(a), int(b)))
    except Exception:
        return None


def _rm(path: str) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _mv(src: str, dst: str) -> None:
    """Rename; where the filesystem refuses (overlayfs/containers: EXDEV on directories) fall back to copy + delete."""
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != 18:                               # EXDEV
            raise
        shutil.copytree(src, dst, symlinks=True)
        shutil.rmtree(src)


def _copy_in(src: str, dst: str) -> None:
    """Copy the bundle to a private place. The source is a path the (unprivileged) web app controls: no symlinks, regular files only."""
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fi:
        import stat as _st
        if not _st.S_ISREG(os.fstat(fi.fileno()).st_mode):
            raise Refused("The staged update is not a plain file.")
        with open(dst, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)


# ------------------------------------------------------------------ the two operations
def apply_update(lay: Layout, bundle_path: str, allow_older: bool = False, *, restart: Optional[Callable[[str], None]] = None,
                 health: Optional[Callable[[str], Optional[str]]] = None, log: Callable[[str], None] = print) -> dict:
    """Install a bundle. Returns the final status dict (state: ok | rolled_back | failed | refused)."""
    restart = restart or (lambda a: systemctl_service(lay, a))
    health = health or (lambda v: http_health(lay, v))
    cur = installed_version(lay.app_dir)
    st = Status(lay, "install", from_version=cur)
    st.update(phase="verifying", message="Checking the update's signature…")
    stage = os.path.join(lay.work_dir, "stage")
    local = os.path.join(lay.work_dir, "in.ptt")
    new_dir = lay.app_dir + ".new"
    try:
        shutil.rmtree(lay.work_dir, ignore_errors=True)
        os.makedirs(stage, exist_ok=True)
        os.chmod(lay.work_dir, 0o700)
        try:
            need = os.path.getsize(bundle_path) * 4
            if shutil.disk_usage(lay.parent).free < need + 50 * 1024 * 1024:
                raise Refused("Not enough free space on the Pi to install this update. Nothing was changed.")
            _copy_in(bundle_path, local)
            manifest = verify_bundle(local, lay.pubkey, dest=stage)
            check_installable(manifest, cur, lay.installer_level, allow_older)
        except BundleError as e:
            raise Refused(e.message)
        except OSError as e:
            raise Refused(f"The staged update could not be read ({e.strerror or e}). Nothing was changed.")
        to = manifest["version"]
        st.update(to_version=to, phase="checking", message=f"Update {to} is signed correctly. Checking what it needs…")
        # --- new Python packages (offline, from the bundle's wheels)
        req = os.path.join(stage, "app", "requirements.txt")
        needs = _need_packages(lay, req)
        if needs:
            tag = _py_tag(lay)
            wheels = os.path.join(stage, "wheels", tag) if tag else ""
            if not wheels or not os.path.isdir(wheels):
                raise Refused(f"This update needs new Python packages, but the bundle has none for this Pi's Python ({tag or 'unknown'}; it carries: "
                              f"{', '.join(manifest.get('wheel_tags') or []) or 'none'}). Nothing was changed. Update with a full install package, or connect the Pi to the internet and run install-pi.sh.")
            st.update(phase="packages", message="Installing the new Python packages from the bundle…")
            r = _run([lay.venv_python, "-m", "pip", "install", "--no-index", "--find-links", wheels, "--disable-pip-version-check", "-q", "-r", req], timeout=600)
            if r.returncode:
                raise Refused("The new Python packages could not be installed: " + (r.stderr or r.stdout).strip().splitlines()[-1][:200] + " Nothing else was changed.")
        # --- build the new app folder next to the old one
        st.update(phase="staging", message="Preparing the new version…")
        _rm(new_dir)
        shutil.copytree(os.path.join(stage, "app"), new_dir)
    except Refused as e:
        _rm(new_dir)
        shutil.rmtree(lay.work_dir, ignore_errors=True)
        st.update(state="refused", phase="done", message=str(e))
        return st.d
    except Exception as e:                                      # anything unexpected before the swap: nothing was changed
        _rm(new_dir)
        shutil.rmtree(lay.work_dir, ignore_errors=True)
        st.update(state="failed", phase="done", message=f"The update could not be prepared ({type(e).__name__}: {str(e)[:150]}). Nothing was changed; the Pi still runs {cur}.")
        return st.d

    # --- swap
    swapped = False
    try:
        st.update(phase="installing", message="Stopping the service and switching to the new version…")
        restart("stop")
        _rm(lay.prev_dir)
        _mv(lay.app_dir, lay.prev_dir)
        _mv(new_dir, lay.app_dir)
        swapped = True
        st.update(phase="starting", message="Starting the new version…")
        err = None
        try:
            restart("start")
        except Exception as e:
            err = str(e)
        err = err or health(to)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    if err:
        return _roll_back(lay, st, cur, restart, health, reason=err, to=to, new_dir=new_dir)
    # --- success
    _refresh_extras(lay, stage)
    shutil.rmtree(lay.work_dir, ignore_errors=True)
    st.update(state="ok", phase="done", to_version=to, message=f"Updated to version {to}. The previous version ({cur}) is kept for \"Roll back\".")
    return st.d


def _refresh_extras(lay: Layout, stage: str) -> None:
    """System install: keep the installer scripts next to the app (and the root-owned helper copy) in step with the new version."""
    if lay.mode != "system":
        return
    try:
        base = os.path.dirname(os.path.abspath(lay.app_dir))
        ex = os.path.join(stage, "extra")
        for name in ("install-pi.sh", "install-wifi-nexmon.sh"):
            s, d = os.path.join(ex, name), os.path.join(base, name)
            if os.path.isfile(s) and os.path.isfile(d):
                shutil.copyfile(s, d + ".new")
                os.chmod(d + ".new", 0o755)
                os.replace(d + ".new", d)
        hs = os.path.join(stage, "app", "traffic_tester", "update")
        hd = os.path.join(base, "update-helper", "traffic_tester")
        if os.path.isdir(hs) and os.path.isdir(os.path.join(base, "update-helper")):
            tmp = hd + ".new"
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp + "/update")
            shutil.copyfile(os.path.join(stage, "app", "traffic_tester", "__init__.py"), tmp + "/__init__.py")
            for fn in os.listdir(hs):
                if fn.endswith(".py"):
                    shutil.copyfile(os.path.join(hs, fn), os.path.join(tmp, "update", fn))
            _run(["chown", "-R", "root:root", tmp])
            _run(["chmod", "-R", "go-w", tmp])
            shutil.rmtree(hd + ".old", ignore_errors=True)
            if os.path.isdir(hd):
                _mv(hd, hd + ".old")
            _mv(tmp, hd)
            shutil.rmtree(hd + ".old", ignore_errors=True)
            _run(["systemctl", "try-restart", "traffic-tester-netwatch.service"], timeout=30)        # the network dead-man loop runs this code: pick up the new copy (no-op where it is not installed)
    except Exception:
        pass                                    # the app itself is updated; a stale copy of an installer script is not worth failing for


def _roll_back(lay: Layout, st: Status, cur: str, restart, health, reason: str, to: str, new_dir: str = "") -> dict:
    st.update(phase="rolling_back", message=f"The new version did not start properly ({reason}). Going back to {cur}…")
    try:
        try:
            restart("stop")
        except Exception:
            pass
        failed = lay.app_dir + ".failed"
        _rm(failed)
        if not os.path.isdir(lay.app_dir):                       # the swap stopped half way: the old app is still in app.prev
            _mv(lay.prev_dir, lay.app_dir)
        elif installed_version(lay.app_dir) != cur and os.path.isdir(lay.prev_dir):      # the new app is in place: put the old one back
            _mv(lay.app_dir, failed)
            _mv(lay.prev_dir, lay.app_dir)
        if new_dir:
            _rm(new_dir)
        restart("start")
        bad = health(cur)
        _rm(failed)
        if bad:
            st.update(state="failed", phase="done", message=f"The update to {to} failed ({reason}) and the old version ({cur}) did not answer either ({bad}). Check the Pi: journalctl -u traffic-tester.")
        else:
            st.update(state="rolled_back", phase="done", message=f"Update to {to} failed ({reason}). Rolled back automatically: the Pi runs {cur} again. Your tests and results are untouched.")
    except Exception as e:
        st.update(state="failed", phase="done", message=f"The update to {to} failed ({reason}) and the automatic rollback did not finish ({type(e).__name__}: {str(e)[:120]}). The previous version is in {lay.prev_dir}.")
    shutil.rmtree(lay.work_dir, ignore_errors=True)
    return st.d


def rollback(lay: Layout, *, restart: Optional[Callable[[str], None]] = None, health: Optional[Callable[[str], Optional[str]]] = None) -> dict:
    """Swap app <-> app.prev (so the button can be pressed again to go forward)."""
    restart = restart or (lambda a: systemctl_service(lay, a))
    health = health or (lambda v: http_health(lay, v))
    cur = installed_version(lay.app_dir)
    prev = installed_version(lay.prev_dir) if os.path.isdir(lay.prev_dir) else ""
    st = Status(lay, "rollback", to_version=prev, from_version=cur)
    if not prev or prev == "0":
        st.update(state="refused", phase="done", message="There is no previous version to go back to.")
        return st.d
    swap = lay.app_dir + ".swap"
    try:
        st.update(phase="installing", message=f"Going back to version {prev}…")
        restart("stop")
        _rm(swap)
        _mv(lay.app_dir, swap)
        _mv(lay.prev_dir, lay.app_dir)
        _mv(swap, lay.prev_dir)
        restart("start")
        bad = health(prev)
    except Exception as e:
        bad = f"{type(e).__name__}: {e}"
    if bad:                                     # the older version does not come up: put things back as they were
        try:
            restart("stop")
            _rm(swap)
            if os.path.isdir(lay.prev_dir) and installed_version(lay.prev_dir) == cur:
                _mv(lay.app_dir, swap)
                _mv(lay.prev_dir, lay.app_dir)
                _mv(swap, lay.prev_dir)
            restart("start")
            ok2 = health(cur)
        except Exception as e:
            ok2 = str(e)
        st.update(state="rolled_back" if not ok2 else "failed", phase="done",
                  message=f"Version {prev} did not start properly ({bad}). Kept version {cur}." if not ok2 else f"Version {prev} did not start ({bad}) and {cur} did not answer afterwards ({ok2}).")
        return st.d
    st.update(state="ok", phase="done", message=f"Rolled back to version {prev}. Version {cur} is kept: press the button again to go forward.")
    return st.d


# ------------------------------------------------------------------ entry point
def _load_system_layout() -> Layout:
    conf = {}
    with open(SYSTEM_CONF) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    with open(SYSTEM_PUBKEY) as f:
        pub = bytes.fromhex(f.read().strip())
    return Layout(mode="system", app_dir=conf["APP_DIR"], data_dir=conf["DATA_DIR"], state_dir=conf.get("STATE_DIR", "/var/lib/traffic-tester-update"),
                  work_dir=conf.get("WORK_DIR", "/var/lib/traffic-tester-update/work"), pubkey=pub, service=conf.get("SERVICE", "traffic-tester.service"),
                  port=int(conf.get("PORT", "8080")), venv_python=conf.get("VENV_PYTHON") or None, svc_user=conf.get("SVC_USER") or None)


def _load_user_layout(path: str) -> Layout:
    with open(path) as f:
        d = json.load(f)
    d["pubkey"] = bytes.fromhex(d["pubkey"])
    return Layout(**d)


def _peek_request(lay: Layout) -> Optional[dict]:
    try:
        with open(lay.request_path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _run_net_requests(lay: Layout) -> int:
    """Network and clock requests ({"action": "net" | "clock"}) are handled by netops.py with their own lock, so they never wait for (or block) a software update.
    Returns how many were handled. The request file is only deleted when it is still the one that was handled (a newer one is picked up next)."""
    from . import netops
    done = 0
    for _ in range(6):
        req = _peek_request(lay)
        if not req or req.get("action") not in ("net", "clock"):
            break
        if lay.mode != "system" and os.environ.get("TT_NET_FAKE") != "1":
            netops.write_json(os.path.join(lay.state_dir, netops.F_RESULT), {"ok": False, "code": "needs_service", "message": "Network changes need the installed service (install-pi.sh); this rootless copy cannot change the network.",
                                                                            "id": str(req.get("id", ""))[:64], "time": time.time()})
        else:
            netops.handle(netops.make_ctx(lay.state_dir, data_dir=lay.data_dir), req)
        done += 1
        if _peek_request(lay) == req:
            try:
                os.unlink(lay.request_path)
            except OSError:
                pass
    return done


def run(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "net-revert":                           # fired by the systemd timer armed in netops.op_apply (the 90 s "Keep these settings?" countdown)
        from . import netops
        return netops.revert_main(None, "--force" in argv)
    layout_file = None
    if "--layout" in argv:
        i = argv.index("--layout")
        layout_file = argv[i + 1]
        del argv[i:i + 2]
    lay = _load_user_layout(layout_file) if layout_file else _load_system_layout()
    os.makedirs(lay.work_dir, exist_ok=True)
    handled = _run_net_requests(lay)
    if handled and _peek_request(lay) is None:
        return 0
    lock = open(os.path.join(os.path.dirname(lay.work_dir.rstrip("/")) if lay.mode == "user" else lay.state_dir, ".update.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another update is running")
        return 3
    try:
        with open(lay.request_path) as f:
            req = json.load(f)
    except (OSError, ValueError):
        print("no valid request")
        return 2
    action = req.get("action")
    if action == "apply":
        res = apply_update(lay, os.path.join(lay.inbox, STAGED_NAME), bool(req.get("allow_older") is True))
    elif action == "rollback":
        res = rollback(lay)
    else:
        print("unknown action")
        return 2
    try:
        os.unlink(lay.request_path)
    except OSError:
        pass
    print(res.get("state"), res.get("message"))
    return 0 if res.get("state") in ("ok",) else 1


if __name__ == "__main__":
    sys.exit(run())
