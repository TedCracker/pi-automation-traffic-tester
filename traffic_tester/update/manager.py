"""Web-app side of software updates: what is installed, stage an uploaded or downloaded bundle, check the online channel, start the
helper (which does the real install, outside the web app), report progress, and push the same bundle to another Pi.
The web app never installs anything itself and never runs as root."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from . import INSTALLER_LEVEL, helper
from .bundle import BundleError, check_installable, cmp_version, parse_version, verify_bundle
from .pubkey import PUBLIC_KEY_HEX

DEFAULT_REPO = "ssh://git@ssh.github.com:443/OWNER/pi-traffic-tester-releases.git"
DEFAULT_BRANCH = "stable"
MAX_UPLOAD = 300 * 1024 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))
KNOWN_HOSTS = os.path.join(HERE, "github_known_hosts")
SYSTEM_UNIT = "traffic-tester-update.service"


class UpdateError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path: str, d: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)


class UpdateManager:
    def __init__(self, data_dir: str, app_dir: str, current_version: str, *, busy=lambda: False, get_setting=lambda k, d=None: d, set_setting=lambda k, v: None):
        self.data_dir, self.app_dir, self.version = data_dir, os.path.abspath(app_dir), current_version
        self.busy, self.get_setting, self.set_setting = busy, get_setting, set_setting
        self.mode = os.environ.get("TT_UPDATE_MODE", "off").lower()
        self.upd = os.path.join(data_dir, "update")
        self.inbox = os.path.join(self.upd, "inbox")
        self.staged = os.path.join(self.inbox, helper.STAGED_NAME)
        self.online_dir = os.path.join(self.upd, "online")
        self.state_dir = os.environ.get("TT_UPDATE_STATE_DIR") or (os.path.join(self.upd, "state") if self.mode != "system" else "/var/lib/traffic-tester-update")
        self.repo = os.environ.get("TT_UPDATE_REPO") or DEFAULT_REPO
        self.branch = os.environ.get("TT_UPDATE_BRANCH") or DEFAULT_BRANCH
        self.pubkey = bytes.fromhex(os.environ.get("TT_UPDATE_PUBKEY") or PUBLIC_KEY_HEX)
        self.partner: dict = {"state": "idle"}
        self._lock = threading.Lock()
        self._net = (0.0, False)

    # ------------------------------------------------------------------ what is installed
    def current(self) -> dict:
        rel = _read_json(os.path.join(self.app_dir, "release.json")) or {}
        date = rel.get("date")
        if not date:
            try:
                date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(os.path.join(self.app_dir, "traffic_tester", "__init__.py"))))
            except OSError:
                date = None
        prev = None
        pd = self.app_dir + ".prev"
        if os.path.isdir(pd):
            pv = helper.installed_version(pd)
            prev = pv if pv != "0" else None
        return {"version": self.version, "date": date, "notes": rel.get("notes", ""), "previous_version": prev}

    def availability(self) -> dict:
        """Can this copy install updates here, and if not, exactly why."""
        if self.mode == "off":
            return {"can_install": False, "reason": "This copy of the app is not an installed Pi (it runs from a source folder), so it cannot update itself. Install it with install-pi.sh."}
        if self.mode == "system":
            if not os.path.exists(helper.SYSTEM_CONF) and not os.environ.get("TT_UPDATE_SYSTEM_CONF"):
                return {"can_install": False, "reason": "The update helper is not installed on this Pi. Run install-pi.sh from the newest package once."}
            return {"can_install": True, "reason": ""}
        if self.mode == "user":
            if not os.access(os.path.dirname(self.app_dir), os.W_OK):
                return {"can_install": False, "reason": "The app folder cannot be replaced by this user."}
            return {"can_install": True, "reason": ""}
        return {"can_install": False, "reason": f"Unknown update mode '{self.mode}'."}

    # ------------------------------------------------------------------ the online channel
    def _local_repo(self) -> bool:
        return self.repo.startswith("/") or self.repo.startswith("file://")

    def key_path(self) -> Optional[str]:
        for p in (os.environ.get("TT_UPDATE_KEY"), "/etc/traffic-tester/update/deploy_key", os.path.join(self.upd, "deploy_key")):
            if p and os.path.isfile(p):
                return p
        return None

    def _host_port(self):
        u = self.repo
        if "@" in u and "://" in u:
            hp = u.split("@", 1)[1].split("/", 1)[0]
        else:
            hp = u.split("://", 1)[-1].split("/", 1)[0]
        host, _, port = hp.partition(":")
        return host, int(port or 22)

    def internet(self, max_age: float = 30.0) -> bool:
        if self._local_repo():
            return True
        if os.environ.get("TT_UPDATE_FAKE_ONLINE") in ("0", "1"):
            return os.environ["TT_UPDATE_FAKE_ONLINE"] == "1"
        if time.monotonic() - self._net[0] < max_age:
            return self._net[1]
        try:
            with socket.create_connection(self._host_port(), timeout=3):
                ok = True
        except OSError:
            ok = False
        self._net = (time.monotonic(), ok)
        return ok

    def online_availability(self) -> dict:
        if not self._local_repo() and not self.key_path():
            return {"can_check": False, "reason": "No access key for the update channel is installed on this Pi (the installer package puts it there). Use \"Upload a file\" instead."}
        if not self.internet():
            return {"can_check": False, "reason": "No internet connection: the update channel (GitHub) cannot be reached. Connect the Pi to Wi-Fi or use \"Upload a file\"."}
        return {"can_check": True, "reason": ""}

    def _git_env(self) -> dict:
        env = dict(os.environ)
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "HOME": self.upd, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
        if self._local_repo():            # a repository on disk (tests / a USB stick): owned by someone else is fine, it is read-only data we verify anyway
            gc = os.path.join(self.upd, "gitconfig")             # safe.directory is honoured only from a global file, not from -c / environment
            with open(gc, "w") as f:
                f.write("[safe]\n\tdirectory = *\n")
            env["GIT_CONFIG_GLOBAL"] = gc
        else:
            ssh = ["ssh", "-i", self.key_path() or "/nonexistent", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=15", "-F", "/dev/null"]
            env["GIT_SSH_COMMAND"] = " ".join(ssh)
        return env

    def check_online(self) -> dict:
        """Fetch the newest release from the channel, verify its signature, remember it. Returns the info shown in the UI."""
        oa = self.online_availability()
        if not oa["can_check"]:
            raise UpdateError(oa["reason"], 409)
        os.makedirs(self.upd, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix="online.", dir=self.upd)
        try:
            r = subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", self.branch, "--single-branch", self.repo, os.path.join(tmp, "repo")],
                               capture_output=True, text=True, timeout=180, env=self._git_env())
            if r.returncode:
                raise UpdateError(self._friendly_git(r.stderr), 502)
            repo = os.path.join(tmp, "repo")
            latest = _read_json(os.path.join(repo, "latest.json"))
            if not latest or not isinstance(latest.get("file"), str):
                raise UpdateError("The update channel has no release yet (latest.json is missing).", 502)
            fn = os.path.basename(latest["file"])
            if fn != latest["file"] or not fn.endswith(".ptt"):
                raise UpdateError("The update channel points to an unexpected file name. Nothing was downloaded.", 502)
            path = os.path.join(repo, fn)
            if not os.path.isfile(path):
                raise UpdateError("The update channel lists a bundle that is not there.", 502)
            try:
                manifest = verify_bundle(path, self.pubkey)
            except BundleError as e:
                raise UpdateError(e.message, 422)
            newer = cmp_version(manifest["version"], self.version) > 0
            self._replace_dir(self.online_dir, tmp, os.path.join("repo", fn), fn)
            info = {"checked": time.time(), "version": manifest["version"], "date": manifest.get("date"), "notes": manifest.get("notes", ""), "file": fn,
                    "size": os.path.getsize(os.path.join(self.online_dir, fn)), "requirements_changed": bool(manifest.get("requirements_changed")), "newer": newer, "signature": "ok",
                    "changelog": self._new_entries(manifest)}
            _write_json(os.path.join(self.upd, "online.json"), info)
            return info
        except subprocess.TimeoutExpired:
            raise UpdateError("The update channel did not answer in time. Check the internet connection and try again.", 504)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _new_entries(self, manifest: dict) -> list:
        """What changed in every version between the installed one and the one in this (signed) bundle, newest first."""
        from . import bundle as _b
        ents = [e for e in (manifest.get("changelog") or []) if isinstance(e, dict) and isinstance(e.get("version"), str)]
        try:
            return [e for e in ents if _b.cmp_version(e["version"], self.version) > 0]
        except _b.BundleError:
            return []

    def _replace_dir(self, dest: str, tmp: str, rel: str, fn: str) -> None:
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest)
        shutil.copyfile(os.path.join(tmp, rel), os.path.join(dest, fn))

    @staticmethod
    def _friendly_git(err: str) -> str:
        e = err.lower()
        if "could not resolve" in e or "network is unreachable" in e or "temporary failure" in e or "timed out" in e or "no route" in e:
            return "The update channel could not be reached (no internet?). Try again, or use \"Upload a file\"."
        if "permission denied" in e or "publickey" in e or "access rights" in e:
            return "The update channel refused this Pi's access key. Ask whoever publishes updates for a new key, or use \"Upload a file\"."
        if "host key verification failed" in e or "remote host identification" in e:
            return "The update channel's identity did not match what this Pi expects, so nothing was downloaded. Use \"Upload a file\"."
        if "remote branch" in e and "not found" in e:
            return "The update channel has no release yet."
        return "The update channel could not be read: " + (err.strip().splitlines()[-1][:160] if err.strip() else "unknown error")

    def last_online(self) -> Optional[dict]:
        return _read_json(os.path.join(self.upd, "online.json"))

    # ------------------------------------------------------------------ staging a bundle
    def describe(self, manifest: dict) -> dict:
        v = manifest["version"]
        c = cmp_version(v, self.version)
        return {"version": v, "date": manifest.get("date"), "notes": manifest.get("notes", ""), "requirements_changed": bool(manifest.get("requirements_changed")),
                "changelog": self._new_entries(manifest), "wheel_tags": manifest.get("wheel_tags", []), "newer": c > 0, "same": c == 0, "older": c < 0,
                "min_installer": manifest.get("min_installer", 1), "installer_ok": int(manifest.get("min_installer", 1)) <= INSTALLER_LEVEL}

    def stage_file(self, src_path: str) -> dict:
        """Verify an uploaded file and make it the staged update. Raises UpdateError with the reason in plain language."""
        try:
            manifest = verify_bundle(src_path, self.pubkey)
        except BundleError as e:
            raise UpdateError(e.message, 422)
        os.makedirs(self.inbox, exist_ok=True)
        os.replace(src_path, self.staged)
        d = self.describe(manifest)
        _write_json(os.path.join(self.upd, "staged.json"), {**d, "source": "upload", "staged": time.time()})
        return d

    def stage_online(self) -> dict:
        info = self.last_online()
        if not info:
            raise UpdateError("Check online first.", 409)
        src = os.path.join(self.online_dir, info["file"])
        if not os.path.isfile(src):
            raise UpdateError("The downloaded update is gone. Check online again.", 409)
        os.makedirs(self.inbox, exist_ok=True)
        tmp = os.path.join(self.inbox, ".incoming.ptt")
        shutil.copyfile(src, tmp)
        d = self.stage_file(tmp)
        _write_json(os.path.join(self.upd, "staged.json"), {**d, "source": "online", "staged": time.time()})
        return d

    def staged_info(self) -> Optional[dict]:
        if not os.path.isfile(self.staged):
            return None
        return _read_json(os.path.join(self.upd, "staged.json"))

    # ------------------------------------------------------------------ install / roll back
    def _check_can(self) -> None:
        a = self.availability()
        if not a["can_install"]:
            raise UpdateError(a["reason"], 409)
        st = self.status()
        if st.get("state") == "working" and not st.get("stale"):
            raise UpdateError("An update is already being installed.", 409)
        if self.busy():
            raise UpdateError("A test is running. Stop it before updating: the app restarts during an update.", 409)

    def install(self, allow_older: bool = False) -> dict:
        self._check_can()
        if not os.path.isfile(self.staged):
            raise UpdateError("No update is ready. Upload a file or check online first.", 409)
        try:
            manifest = verify_bundle(self.staged, self.pubkey)
            check_installable(manifest, self.version, INSTALLER_LEVEL, allow_older)
        except BundleError as e:
            raise UpdateError(e.message, 422)
        self._start({"action": "apply", "allow_older": bool(allow_older), "to": manifest["version"]})
        return {"started": True, "to_version": manifest["version"]}

    def rollback(self) -> dict:
        self._check_can()
        if not self.current()["previous_version"]:
            raise UpdateError("There is no previous version to go back to.", 409)
        self._start({"action": "rollback"})
        return {"started": True, "to_version": self.current()["previous_version"]}

    def _layout_dict(self) -> dict:
        work = os.path.join(self.upd, "work")
        return {"mode": "user", "app_dir": self.app_dir, "data_dir": self.data_dir, "state_dir": self.state_dir, "work_dir": work, "pubkey": self.pubkey.hex(),
                "service": os.environ.get("TT_UPDATE_SERVICE", "traffic-tester.service"), "port": int(os.environ.get("TT_PORT", "8080")),
                "venv_python": os.environ.get("TT_UPDATE_VENV_PYTHON") or sys.executable, "svc_user": None, "installer_level": INSTALLER_LEVEL, "health_seconds": 60.0}

    def _start(self, req: dict) -> None:
        os.makedirs(self.upd, exist_ok=True)
        _write_json(os.path.join(self.upd, "request.json"), {k: v for k, v in req.items() if k in ("action", "allow_older")})
        if self.mode == "user":
            os.makedirs(self.state_dir, exist_ok=True)
        stale = {"state": "working", "action": req["action"], "phase": "starting", "message": "Starting…", "to_version": req.get("to", ""), "from_version": self.version,
                 "started": time.time(), "ts": time.time(), "log": []}
        if self.mode == "user":
            _write_json(helper.status_path(self.state_dir), stale)
        if self.mode == "system":
            r = subprocess.run(["systemctl", "start", "--no-block", SYSTEM_UNIT], capture_output=True, text=True, timeout=30)
            if r.returncode:
                raise UpdateError("The update helper could not be started: " + (r.stderr.strip()[:160] or "systemctl failed"), 500)
            return
        # rootless: copy the helper out of the app folder (the folder is about to be swapped), then run it detached from this service
        run = os.path.join(self.upd, "run")
        shutil.rmtree(run, ignore_errors=True)
        os.makedirs(os.path.join(run, "traffic_tester", "update"))
        shutil.copyfile(os.path.join(os.path.dirname(HERE), "__init__.py"), os.path.join(run, "traffic_tester", "__init__.py"))
        for fn in os.listdir(HERE):
            if fn.endswith(".py"):
                shutil.copyfile(os.path.join(HERE, fn), os.path.join(run, "traffic_tester", "update", fn))
        layout = os.path.join(self.upd, "layout.json")
        _write_json(layout, self._layout_dict())
        cmd = ["/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable, "-I", os.path.join(run, "traffic_tester", "update", "helper.py"), "run", "--layout", layout]
        unit = f"tt-update-{int(time.time())}"
        r = subprocess.run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}"] + cmd, capture_output=True, text=True, timeout=30) if shutil.which("systemd-run") else None
        if r is None or r.returncode:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    # ------------------------------------------------------------------ progress
    def status(self) -> dict:
        s = helper.read_status(self.state_dir)
        if s.get("state") == "working" and time.time() - float(s.get("ts", 0)) > 360:
            s = {**s, "stale": True, "message": "The last update did not report back for several minutes. Check the Pi."}
        return s

    def info(self, deep: bool = False) -> dict:
        av = self.availability()
        out = {"current": self.current(), "mode": self.mode, **av, "installer_level": INSTALLER_LEVEL, "status": self.status(), "staged": self.staged_info(),
               "online": {**self.online_availability(), "last": self.last_online(), "repo": "GitHub" if "github.com" in self.repo else ("local test repository" if self._local_repo() else self.repo)},
               "daily_check": bool(self.get_setting("update_daily_check", False)), "partner": self.partner}
        return out

    # ------------------------------------------------------------------ daily check (notify only)
    def daily_due(self) -> bool:
        if not self.get_setting("update_daily_check", False):
            return False
        last = (self.last_online() or {}).get("checked", 0)
        return time.time() - last > 23 * 3600

    # ------------------------------------------------------------------ another Pi (this Pi pushes the same verified bundle to it)
    def push_partner(self, host: str, port: int = 8080, password: str = "") -> None:
        """Runs in a thread. Progress is in self.partner."""
        def say(state, msg, **kw):
            self.partner = {"state": state, "message": msg, "host": host, "name": (self.partner or {}).get("name", ""), "ts": time.time(), **kw}
        base = f"http://{host}:{port}"
        jar = {}
        def req(method, path, body=None, ctype="application/json", timeout=60):
            data = body if isinstance(body, (bytes, bytearray)) else (None if body is None else json.dumps(body).encode())
            h = {"Content-Type": ctype}
            if jar.get("c"):
                h["Cookie"] = jar["c"]
            rq = urllib.request.Request(base + path, data=data, method=method, headers=h)
            try:
                with urllib.request.urlopen(rq, timeout=timeout) as r:
                    for k, v in r.headers.items():
                        if k.lower() == "set-cookie":
                            jar["c"] = v.split(";")[0]
                    raw = r.read()
                    return r.status, (json.loads(raw) if raw else {})
            except urllib.error.HTTPError as e:
                try:
                    return e.code, json.loads(e.read() or b"{}")
                except Exception:
                    return e.code, {}
        try:
            if not os.path.isfile(self.staged):
                return say("failed", "No update is ready on this Pi. Upload a file or check online first.")
            say("working", f"Connecting to the Pi at {host}…")
            c, st = req("GET", "/api/status", timeout=10)
            if c != 200:
                return say("failed", f"The Pi at {host}:{port} did not answer as a Pi Automation Traffic Tester.")
            if st.get("login_required"):
                if not password:
                    return say("failed", "That Pi has a web password. Type it in the box and try again.")
                c, _ = req("POST", "/api/auth/login", {"password": password}, timeout=15)
                if c != 200:
                    return say("failed", "That Pi did not accept that password.")
            c, info = req("GET", "/api/update/status", timeout=10)
            if c != 200 or "current" not in info:
                return say("failed", "That Pi runs a version without software updates. Update it once with a file, or reinstall it from the new package.")
            if not info.get("can_install"):
                return say("failed", "That Pi cannot update itself: " + info.get("reason", ""))
            say("working", "Sending the verified update to that Pi…")
            with open(self.staged, "rb") as f:
                blob = f.read()
            c, up = req("POST", "/api/update/upload?name=" + urllib.request.quote(os.path.basename(self.staged)), blob, "application/octet-stream", timeout=300)
            if c != 200:
                return say("failed", "That Pi refused the update: " + str(up.get("detail", c)))
            c, ins = req("POST", "/api/update/install", {"source": "upload", "allow_older": False}, timeout=30)
            if c != 202:
                return say("failed", "That Pi did not start the update: " + str(ins.get("detail", c)))
            say("working", "That Pi is installing and restarting…", to_version=up.get("version"))
            t0 = time.time()
            seen_down = False
            while time.time() - t0 < 240:
                time.sleep(3)
                try:
                    c, s2 = req("GET", "/api/update/status", timeout=8)
                    cur = s2.get("current", {}).get("version") if c == 200 else None
                    stt = (s2.get("status") or {}) if c == 200 else {}
                    if stt.get("state") in ("ok", "rolled_back", "failed", "refused") and (stt.get("to_version") == up.get("version") or stt.get("state") != "ok"):
                        return say("done" if stt["state"] == "ok" else "failed", stt.get("message", ""), version=cur)
                except Exception:
                    seen_down = True
            say("failed", "That Pi did not report back in time. Open it in a browser to check." + (" (it went offline while restarting)" if seen_down else ""))
        except Exception as e:
            say("failed", f"Could not reach that Pi at {host}:{port} ({type(e).__name__}). Check the address and that it is switched on.")
