"""FastAPI app: REST + WebSocket + static SPA (docs/SPEC.md section 8)."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import threading
import time
from typing import Any, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import __version__, changelog, engine, factory, mbrun, plan as PL, orchestrate as OR, profiles as P, system
from . import clock as CK, peers as PE
from . import compare as CMP
from . import radio as RADIO, repeats as RPT
from .netcfg import api as netapi, watchdog as netwd
from . import firstboot as FB, firstboot_api as fbapi
from .reflector import Busy as ReflectorBusy, PortClaims, ReflectorService, DEFAULT_PORT as REFLECT_PORT
from .gen import node as gnode
from .sniff import bpf as sbpf, preflight as spre
from . import netcheck
from .aiguide import build as aig
from .aiguide.model import to_html, to_text
from .applog import AppLog
import collections, logging
from .runmgr import Busy, RunManager
from .update.manager import UpdateError, UpdateManager, MAX_UPLOAD
from .store import BuiltinError, Store

STATIC = os.path.join(os.path.dirname(__file__), "static")


def app_dir() -> str:
    """Where the app lives (the folder an update replaces)."""
    return os.environ.get("TT_APP_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import mimetypes
mimetypes.add_type("application/manifest+json", ".webmanifest")      # the web app manifest ("Add to Home Screen"); not in every OS's mime table


class AppState:
    def __init__(self, data_dir: str, role: Optional[str] = None):
        # `role` (A / B) is only read once, to carry an install made before there were no roles over to the new setup (see migrate_roles)
        self.data_dir = data_dir
        self.store = Store(data_dir)
        self.fresh_install = self.store.get_setting("pi_id") is None and not self.store.list_runs() and self.store.get_setting("password_hash") is None \
            and self.store.get_setting("responder_cfg") is None
        self.port = int(os.environ.get("TT_PORT", "8080"))
        self.me = PE.Identity(self.store, self.port)
        self.peers = PE.PeerRegistry(self.store, self.me)
        self.discovery = PE.Discovery(self.me, self.peers, self.port)
        self.hooks = os.environ.get("TT_TEST_HOOKS") == "1"
        self.claims = PortClaims()
        self.reflectors = ReflectorService(self.claims, self.hooks, self.store.get_setting("reflector_ports") or [REFLECT_PORT])
        self.clock = CK.ClockManager(self.store, self.me, self.peers, self._apply_clock)
        self.gen_node = gnode.Node()
        self.gen_touched = time.monotonic()                       # when a controlling Pi last asked this Pi's generator node for anything
        self.services = OR.Services(self.me, self.peers, self.reflectors, self.claims)
        self.runs = RunManager(self.store, None, self.gen_node, self.services)
        self.repeats = RPT.RepeatManager(self.store, lambda prof: self.runs.start(prof, None, controller="schedule"), lambda: self.runs.busy() or self.gen_node.busy())
        self.migrated = migrate_roles(self, role)
        self.tokens: set = set()
        self.secret = secrets.token_bytes(16)
        self.applog = AppLog(data_dir)
        self.updates = UpdateManager(data_dir, app_dir(), __version__, busy=lambda: bool((self.runs.status() or {}).get("running")) or self.gen_node.busy(),
                                     get_setting=self.store.get_setting, set_setting=self.store.set_setting)
        logging.getLogger("tt").info("Pi Automation Traffic Tester %s started (Pi name %s)", __version__, self.me.name)

    def _apply_clock(self, mode: str, server: Optional[str]) -> dict:
        try:
            from .netcfg import client as ncl                      # the privileged helper (installed with the app)
            return ncl.clock(mode, server)
        except ImportError:
            return {"ok": False, "message": "The time-source helper is not installed on this Pi."}

    GEN_ORPHAN_S = 180.0

    def gen_orphan_check(self) -> bool:
        """This Pi as the far end of another Pi's generator test: that Pi stops us when its test ends. If it never does (it lost contact, crashed
        or was switched off) this Pi would say "busy" until the app is restarted. So: nothing sending any more, no test of our own, and no word
        from a controller for GEN_ORPHAN_S seconds -> stop the left-over test here. Returns True when it did."""
        n = self.gen_node
        if not n.busy() or self.runs.busy():
            return False
        if time.monotonic() - self.gen_touched < self.GEN_ORPHAN_S or (n.senders and not n.senders_done()):
            return False
        logging.getLogger("tt").warning("Generator: the controlling Pi has not asked for %.0f s; stopping the test it left behind.", time.monotonic() - self.gen_touched)
        try:
            n.stop()
        except Exception as e:  # noqa
            logging.getLogger("tt").warning("Generator: could not stop the left-over test: %s", e)
            return False
        return True

    # --- password (optional)
    def pw_hash(self) -> Optional[str]:
        return self.store.get_setting("password_hash")

    @staticmethod
    def hash_pw(pw: str, salt: Optional[bytes] = None) -> str:
        salt = salt or secrets.token_bytes(16)
        h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
        return salt.hex() + ":" + h.hex()

    @staticmethod
    def check_pw(pw: str, stored: str) -> bool:
        salt, h = stored.split(":")
        return hmac.compare_digest(hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex(), h)


def migrate_roles(st: "AppState", role_arg: Optional[str]) -> dict:
    """Installs made before there were no fixed roles: Pi A (role A) simply becomes a Pi whose tests default to "from this Pi"; Pi B (role B) keeps
    answering because every Pi now has the always-on reflector. Old tests and results open as they are. Nothing is switched off or reconfigured:
    the time setup stays as the installer made it ("keep"), and an old Responder port other than the default is kept as an extra always-on port."""
    if st.store.get_setting("roles_migrated") is not None:
        return st.store.get_setting("roles_migrated")
    role = (role_arg or os.environ.get("TT_ROLE") or "").upper()[:1] or None
    info = {"from_role": role, "existing_install": not st.fresh_install, "note": "", "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not st.fresh_install:
        if st.store.get_setting("clock_source") is None:
            st.store.set_setting("clock_source", "keep")             # the installer already set the clocks up (Pi A serves, Pi B follows it)
        cfg = st.store.get_setting("responder_cfg")
        if cfg:
            t, port = cfg.get("transport"), (cfg.get("net") or {}).get("port")
            if t in ("udp", "tcp") and cfg.get("mode", "echo") == "echo" and isinstance(port, int) and port != REFLECT_PORT:
                ports = sorted(set((st.store.get_setting("reflector_ports") or [REFLECT_PORT]) + [port]))
                st.store.set_setting("reflector_ports", ports)
                st.reflectors.ports = ports
            elif t in ("udp", "tcp") and cfg.get("mode", "echo") == "echo":
                info["note"] = "This Pi was the Responder: it still answers UDP and TCP on port %s, now always." % port
            else:
                info["note"] = ("This Pi was the Responder on %s. Serial ports and GOOSE are no longer left listening: the Pi that starts a test now "
                                "prepares this one." % (t or "its port"))
    st.store.set_setting("roles_migrated", info)
    return info


def create_app(data_dir: str, role: Optional[str] = None) -> FastAPI:
    st = AppState(data_dir, role)

    def _claude_leftovers():
        """A Pi that was updated from 1.6 or older may still carry the removed Ask Claude feature (units, package, sign-in folder). The privileged helper cleans it up.
        Right after an update the helper is still finishing the update (its health check waits for this app), so the first request can be missed: try again a few times."""
        try:
            from .netcfg import client as ncl
            from .update import claude_cleanup as cc
            if ncl.fake() or not ncl.available()[0]:
                return
            for attempt in range(6):
                if not cc.leftovers(st.data_dir, "/etc", app_dir()):
                    return
                r = ncl.call("claude_cleanup", wait=45)
                if r.get("ok") is not None:
                    logging.getLogger("tt").info("Ask Claude leftovers: %s", r.get("message"))
                    if r.get("ok"):
                        return
                time.sleep(30 if attempt else 60)
        except Exception as e:  # noqa
            logging.getLogger("tt").warning("Ask Claude leftovers could not be cleaned up: %s", e)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bg = asyncio.create_task(broadcaster())
        bg2 = asyncio.create_task(daily_update_check())
        if st.store.get_setting("wifi_powersave_off", False):       # the user chose "turn off Wi-Fi power saving": keep it that way after a reboot
            def _keep_off():
                for i in system.interfaces():
                    if system.is_wifi(i["name"]) and system.wifi_power_save(i["name"]):
                        system.wifi_powersave_off(i["name"])
            threading.Thread(target=_keep_off, daemon=True).start()
        threading.Thread(target=_claude_leftovers, daemon=True).start()
        if os.environ.get("TT_NO_DEFAULT_REFLECTOR") != "1":
            st.reflectors.start_defaults()                  # every Pi answers UDP and TCP on the test port from the moment the app starts
        st.peers.start()
        if os.environ.get("TT_NO_REPEATS") != "1":
            st.repeats.start()                              # scheduled repeats survive a reboot: they are read back from the settings table
        if os.environ.get("TT_NO_DISCOVERY") != "1":
            st.discovery.start()
        bg3 = asyncio.create_task(clock_loop())
        if FB.is_first_boot(st.data_dir, st.store) and os.environ.get("TT_NO_FIRSTBOOT_REDIRECT") != "1":
            st.fb_redirect = FB.start_redirect(st.port)     # card image, not named yet: phones that probe port 80 land on the setup page
        if os.environ.get("TT_NO_NET_WATCHDOG") != "1":
            netwd.start()                                   # no network at all for 3 minutes -> bring the access point up
        yield
        bg.cancel()
        bg2.cancel()
        bg3.cancel()
        if getattr(st, "fb_redirect", None):
            st.fb_redirect.shutdown()
        st.repeats.shutdown()
        st.runs.stop()
        st.runs.wait(5)
        try:
            st.gen_node.stop()
        except Exception:
            pass
        st.peers.stop()
        st.discovery.stop()
        st.reflectors.stop_all()

    app = FastAPI(title="Pi Automation Traffic Tester", version=__version__, lifespan=lifespan)
    app.state.tt = st
    clients: List[WebSocket] = []

    def _ints(csv: str) -> List[int]:
        try:
            return [int(x) for x in csv.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(422, "Give the ids as numbers separated by commas.")

    # ------------------------------------------------------------ auth
    @app.middleware("http")
    async def auth(request: Request, call_next):
        path = request.url.path
        if st.pw_hash() and path.startswith("/api/") and not path.startswith("/api/auth/") and path not in ("/api/status", "/api/peer/info"):
            if request.cookies.get("tt_token") not in st.tokens:
                return JSONResponse({"detail": "Password required"}, status_code=401)
        resp = await call_next(request)
        if path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/auth/login")
    def login(resp: Response, body: dict = Body(...)):
        h = st.pw_hash()
        if not h or not AppState.check_pw(str(body.get("password") or ""), h):
            raise HTTPException(401, "Wrong password")
        tok = secrets.token_urlsafe(24)
        st.tokens.add(tok)
        resp.set_cookie("tt_token", tok, httponly=True, samesite="strict")
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(request: Request, resp: Response):
        """Sign out of this browser: forget its token and clear the cookie (the password itself stays set)."""
        tok = request.cookies.get("tt_token")
        if tok:
            st.tokens.discard(tok)
        resp.delete_cookie("tt_token")
        return {"ok": True}

    @app.post("/api/auth/set")
    def set_password(request: Request, body: dict = Body(...)):
        if st.pw_hash() and request.cookies.get("tt_token") not in st.tokens:
            raise HTTPException(401, "Password required")
        pw = body.get("password") or ""
        if not isinstance(pw, str):
            raise HTTPException(422, "The password must be text.")
        if pw and len(pw) < 4:
            raise HTTPException(422, "Use at least 4 characters.")
        st.store.set_setting("password_hash", AppState.hash_pw(pw) if pw else None)
        st.tokens.clear()
        return {"ok": True, "enabled": bool(pw)}

    # ------------------------------------------------------------ status
    @app.get("/api/status")
    def status(request: Request):
        needs_login = bool(st.pw_hash()) and request.cookies.get("tt_token") not in st.tokens
        base = {"version": __version__, "hostname": socket.gethostname(), "pi": {"id": st.me.id, "name": st.me.name},
                "password_enabled": bool(st.pw_hash()), "login_required": needs_login,
                "first_boot": FB.is_first_boot(st.data_dir, st.store)}
        if needs_login:
            return base
        return {**base, "run": st.runs.status(),
                "reflector": st.reflectors.status(), "responder": st.reflectors.legacy_status(),
                "clock": system.clock_state(), "netem": system.can_netem(),
                "usb": system.usb_drives(), "session": st.store.current_session(),
                "hardware": system.pi_model(), "hooks": st.hooks, "goose_raw": _goose_raw_ok(), "interfaces": system.interfaces(),
                "migrated": st.migrated}

    # ------------------------------------------------------------ this Pi + the other Pis
    @app.get("/api/peer/info")
    def peer_info():
        """Open to anyone on the network (like /api/status): who this Pi is. The other Pis (and the discovery list) read it."""
        st.gen_orphan_check()
        r = st.runs.status()
        return st.me.info({"clock": system.clock_state(), "busy": bool(r.get("running")) or st.gen_node.busy(), "password_required": bool(st.pw_hash()),
                           "hardware": (system.pi_model() or {}).get("model")})

    @app.get("/api/pi")
    def pi_get():
        return {**st.me.info(), "default_name": socket.gethostname().split(".")[0], "discovery_error": st.discovery.error,
                "reflector_ports": st.reflectors.ports, "migrated": st.migrated}

    @app.put("/api/pi")
    def pi_put(body: dict = Body(...)):
        try:
            name = st.me.set_name(str(body.get("name", "")))
        except ValueError as e:
            raise HTTPException(422, str(e))
        st.discovery.rename()
        return {**st.me.info(), "name": name}

    @app.put("/api/reflector/ports")
    def reflector_ports(body: dict = Body(...)):
        ports = body.get("ports")
        if not isinstance(ports, list) or not ports or not all(isinstance(p, int) and 1 <= p <= 65535 for p in ports) or len(ports) > 8:
            raise HTTPException(422, "Give 1 to 8 port numbers (1-65535).")
        ports = sorted(set(ports))
        st.store.set_setting("reflector_ports", ports)
        old = st.reflectors.ports
        st.reflectors.ports = ports
        st.reflectors.apply_default_ports(old, ports)
        return {"ports": ports, "reflector": st.reflectors.status()}

    @app.get("/api/peers")
    def peers_list():
        return {"me": {"id": st.me.id, "name": st.me.name, "ips": st.me.ips(), "port": st.port}, "peers": st.peers.list(), "discovery_error": st.discovery.error}

    @app.post("/api/peers/refresh")
    def peers_refresh():
        return {"peers": st.peers.refresh()}

    @app.post("/api/peers")
    def peers_add(body: dict = Body(...)):
        try:
            return st.peers.add_manual(str(body.get("host") or body.get("address") or ""), str(body.get("password") or ""))
        except PE.PeerError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/peers/{pid}")
    def peers_del(pid: str):
        if not st.peers.remove(pid):
            raise HTTPException(404, "That Pi is not in your list (Pis found on the network come back by themselves; remove only works for ones you added).")
        return {"ok": True}

    @app.post("/api/peers/{pid}/password")
    def peers_password(pid: str, body: dict = Body(...)):
        try:
            return st.peers.set_password(pid, str(body.get("password") or ""))
        except PE.PeerError as e:
            raise HTTPException(422, str(e))

    def _peer_client(pid: str):
        p = st.peers.resolve(pid)
        if not p:
            raise HTTPException(404, "That Pi is not in your list.")
        return p, st.peers.client(p)

    def _via_peer(pid: str, path: str, method: str = "GET", body=None):
        p, c = _peer_client(pid)
        try:
            return c.call(method, path, body, timeout=10)
        except PE.PeerAuthError as e:
            raise HTTPException(409, str(e))
        except PE.PeerError as e:
            raise HTTPException(502, str(e))

    @app.get("/api/peers/{pid}/ports")
    def peers_ports(pid: str):
        """That Pi's serial ports (for the Send from / Send to pickers)."""
        return _via_peer(pid, "/api/ports")

    @app.get("/api/testports")
    def testports(request: Request):
        """Every physical port a test can use on this Pi (New test, row 1): serial, Ethernet, USB Ethernet, Wi-Fi, with live state."""
        return {"pi": {"id": st.me.id, "name": st.me.name}, "ports": system.test_ports(request.client.host if request.client else None),
                "goose_raw": _goose_raw_ok()}

    @app.get("/api/peers/{pid}/testports")
    def peers_testports(pid: str):
        return _via_peer(pid, "/api/testports")

    @app.get("/api/peer/health")
    def peer_health():
        """Tester lamp: what may be wrong with THIS Pi right now (asked by the Pi that controls a test this Pi takes part in)."""
        return {"pi": st.me.name, "reasons": st.runs.health_inputs()}

    @app.get("/api/peers/{pid}/interfaces")
    def peers_interfaces(pid: str):
        d = _via_peer(pid, "/api/gen/node/status")
        return {"interfaces": d.get("interfaces", {}), "default_iface": d.get("default_iface")}

    @app.post("/api/peers/{pid}/clock-source")
    def peers_clock(pid: str, body: dict = Body(...)):
        """Make `source` ("self" = this Pi, or another Pi's id) the time source of that Pi."""
        src = body.get("source")
        value = st.me.id if src in ("self", "", None) else src
        return _via_peer(pid, "/api/clock/source", "PUT", {"source": value})

    @app.get("/api/clock/source")
    def clock_get():
        d = st.clock.decision()
        return {"setting": st.clock.setting(), "decision": d, "state": system.clock_state(0), "applied": st.store.get_setting("clock_applied"),
                "last": st.clock.last}

    @app.put("/api/clock/source")
    def clock_put(body: dict = Body(...)):
        v = str(body.get("source") or "auto")
        try:
            st.clock.set_setting(v if v != st.me.id else "self")
        except ValueError as e:
            raise HTTPException(422, str(e))
        threading.Thread(target=st.clock.reconcile, daemon=True).start()
        return clock_get()

    async def clock_loop():
        await asyncio.sleep(20)
        while True:
            try:
                st.gen_orphan_check()
                busy = st.runs.busy() or st.gen_node.busy()
                await asyncio.to_thread(st.clock.reconcile, busy)   # never touch the time setup in the middle of a test
            except Exception:
                pass
            await asyncio.sleep(60)

    # ------------------------------------------------------------ the reflector (other Pis' tests ask this Pi to answer)
    @app.post("/api/peer/reflect/ensure")
    def reflect_ensure(body: dict = Body(...)):
        owner = str(body.get("owner") or "")[:80]
        cfg = body.get("cfg")
        if not owner or not isinstance(cfg, dict):
            raise HTTPException(422, "Give owner and cfg.")
        try:
            return st.reflectors.ensure(cfg, owner, float(body.get("delay_ms", 0) or 0), float(body.get("drop_pct", 0) or 0))
        except ReflectorBusy as e:
            raise HTTPException(409, str(e))
        except (factory.ConfigError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))
        except OSError as e:
            raise HTTPException(422, f"Could not open the port: {e}")

    @app.post("/api/peer/reflect/release")
    def reflect_release(body: dict = Body(...)):
        return {"released": st.reflectors.release(str(body.get("owner") or ""))}

    @app.get("/api/peer/reflect/status")
    def reflect_status():
        return st.reflectors.status()

    def _goose_raw_ok() -> dict:
        try:
            from .goose import raw_socket_ok
            return raw_socket_ok()
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    @app.get("/api/ports")
    def ports():
        return {"ports": system.list_ports(), "udev_rule": system.UDEV_RULE}

    @app.post("/api/selfcheck")
    def selfcheck(body: dict = Body(...)):
        port = body.get("port") or "auto"
        try:
            port = factory.resolve_serial_port(port)
        except factory.ConfigError as e:
            raise HTTPException(422, str(e))
        return system.selfcheck(port, bool(body.get("fix")))

    @app.get("/api/time")
    def time_state():
        return system.clock_state(0)

    # ------------------------------------------------------------ profiles
    @app.get("/api/profiles")
    def profiles():
        return st.store.list_profiles()

    @app.get("/api/profiles/export")
    def profiles_export(ids: Optional[str] = None):
        idl = _ints(ids) if ids else None
        doc = st.store.export_profiles(idl)
        name = time.strftime("traffic-tester-profiles-%Y%m%d.json")
        return Response(json.dumps(doc, indent=1), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/profiles/import")
    def profiles_import(doc: Any = Body(...)):
        try:
            out = st.store.import_profiles(doc)
            return {"imported": out, **st.store.last_import}
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/profiles")
    def profile_create(body: dict = Body(...)):
        try:
            return st.store.create_profile(body)
        except ValueError as e:
            raise HTTPException(422, str(e))

    # The Setup list is one list the user controls: edit / rename / hide / reorder / create / delete (built-in tests keep their defaults in code).
    @app.post("/api/profiles/order")
    def profiles_order(body: dict = Body(...)):
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise HTTPException(422, "Give ids as a list of test numbers.")
        return {"profiles": st.store.set_order(ids)}

    @app.post("/api/profiles/restore-hidden")
    def profiles_restore_hidden():
        return {"restored": st.store.restore_hidden()}

    @app.post("/api/profiles/undelete")
    def profile_undelete(body: dict = Body(...)):
        try:
            return st.store.undelete_profile(body)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/profiles/{pid}")
    def profile_get(pid: int):
        p = st.store.get_profile(pid)
        if not p:
            raise HTTPException(404, "No such profile")
        return p

    @app.put("/api/profiles/{pid}")
    def profile_put(pid: int, body: dict = Body(...)):
        try:
            return st.store.update_profile(pid, body)
        except KeyError:
            raise HTTPException(404, "No such profile")
        except BuiltinError as e:
            raise HTTPException(403, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/profiles/{pid}")
    def profile_del(pid: int):
        try:
            return {"ok": True, "deleted": st.store.delete_profile(pid)}
        except KeyError:
            raise HTTPException(404, "No such profile")
        except BuiltinError as e:
            raise HTTPException(403, str(e))

    @app.post("/api/profiles/{pid}/reset")
    def profile_reset(pid: int):
        try:
            return st.store.reset_profile(pid)
        except KeyError:
            raise HTTPException(404, "No such profile")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/profiles/{pid}/hide")
    def profile_hide(pid: int):
        try:
            return st.store.set_hidden(pid, True)
        except KeyError:
            raise HTTPException(404, "No such profile")

    @app.post("/api/profiles/{pid}/unhide")
    def profile_unhide(pid: int):
        try:
            return st.store.set_hidden(pid, False)
        except KeyError:
            raise HTTPException(404, "No such profile")

    @app.post("/api/profiles/{pid}/duplicate")
    def profile_dup(pid: int):
        try:
            return st.store.duplicate_profile(pid)
        except KeyError:
            raise HTTPException(404, "No such profile")

    @app.get("/api/profile-limits")
    def profile_limits():
        return P.form_limits()

    @app.get("/api/netem-presets")
    def netem_presets():
        return {k: v["about"] for k, v in P.NETEM_PRESETS.items()}

    # ------------------------------------------------------------ sessions
    @app.get("/api/sessions")
    def sessions():
        return st.store.list_sessions()

    @app.post("/api/sessions")
    def session_new(body: dict = Body(default={})):
        return st.store.new_session(body.get("name"))

    @app.put("/api/sessions/{sid}")
    def session_rename(sid: int, body: dict = Body(...)):
        if not st.store.get_session(sid):
            raise HTTPException(404, "No such session")
        st.store.rename_session(sid, str(body.get("name", ""))[:80] or "Session")
        return st.store.get_session(sid)

    @app.delete("/api/sessions/{sid}/runs")
    def session_clear(sid: int):
        if st.runs.busy() and set(st.runs.run_ids()) & {r["id"] for r in st.store.list_runs(sid)}:
            raise HTTPException(409, "A test in this session is running.")
        st.store.clear_session(sid)
        return {"ok": True}

    @app.delete("/api/sessions/{sid}")
    def session_delete(sid: int):
        if st.runs.busy():
            raise HTTPException(409, "A test is running.")
        st.store.delete_session(sid)
        return {"ok": True}

    # ------------------------------------------------------------ runs
    @app.get("/api/runs")
    def runs(session_id: Optional[int] = None):
        return st.store.list_runs(session_id)

    @app.post("/api/runs")
    def run_start(request: Request, body: dict = Body(...)):
        prof = body.get("profile")
        if body.get("profile_id") is not None:
            base = st.store.get_profile(int(body["profile_id"]))
            if not base:
                raise HTTPException(404, "No such profile")
            base.pop("id", None)
            prof = {**base, **(body.get("overrides") or {})}
        if not prof:
            raise HTTPException(422, "Give profile_id or profile.")
        try:
            pv = P.validate_profile(prof)
            if pv["kind"] == "sniffer" and pv["sniff"]["mode"] == "wifi" and pv["sniff"]["wifi"]["interface"] == "builtin":
                cp = spre.route_dev(request.client.host if request.client else None)
                if cp and cp.startswith("wlan"):
                    raise HTTPException(422, f"You are connected to this Pi over Wi-Fi ({cp}). Built-in Wi-Fi sniffing would cut you off. Open the Pi over Ethernet first.")
            if pv["kind"] == "sniffer" and pv["sniff"]["mode"] == "eth_bridge":
                cp = spre.route_dev(request.client.host if request.client else None)      # never cut off the browser that is controlling the Pi
                if cp and cp in [x for x in spre.bridge_legs(pv) if x]:
                    raise HTTPException(422, f"You are connected to this Pi through {cp}, which the inline tap would bridge. Open the Pi over Wi-Fi (or another port) first.")
        except ValueError as e:
            raise HTTPException(422, str(e))
        job = _job_in(body.get("job"))                               # job info is optional and never stops a start, but nonsense is refused before anything runs
        try:
            rid = st.runs.start(prof, body.get("session_id"), controller=str(body.get("controller") or "")[:60])
        except Busy as e:
            raise HTTPException(409, str(e))
        except (factory.ConfigError, ValueError) as e:
            raise HTTPException(422, str(e))
        except OSError as e:
            raise HTTPException(422, f"Could not open the port: {e}")
        if job:
            for i in st.runs.run_ids() or [rid]:
                st.store.set_job(i, job)
        return {"run_id": rid, "run_ids": st.runs.run_ids()}

    # ------------------------------------------------------------ radio facts + scheduled repeats
    @app.get("/api/radio/usage")
    def radio_usage(baud: int = 19200, interval_ms: float = 10.0, serial_mode: str = "", frames_per_packet: int = 1):
        """Data a nonstop stream uses each way (payload and over the air), plus what to set on the radio."""
        return {**RADIO.usage_estimate(baud, RADIO.FRAME_BYTES, interval_ms, serial_mode, frames_per_packet), "advice": RADIO.advice(baud)}

    @app.get("/api/repeats")
    def repeats_list():
        return st.repeats.list()

    @app.post("/api/repeats")
    def repeats_create(body: dict = Body(...)):
        prof = body.get("profile")
        if body.get("profile_id") is not None:
            base = st.store.get_profile(int(body["profile_id"]))
            if not base:
                raise HTTPException(404, "No such profile")
            base.pop("id", None)
            prof = {**base, **(body.get("overrides") or {})}
        if not prof:
            raise HTTPException(422, "Give profile_id or profile.")
        try:
            pv = P.validate_profile(prof)
            return st.repeats.create(pv, body.get("schedule") or {}, body.get("start_at"), str(body.get("name") or ""))
        except (ValueError, RPT.BadSchedule) as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/repeats/{rid}")
    def repeats_delete(rid: str):
        if not st.repeats.delete(rid):
            raise HTTPException(404, "No such schedule")
        return {"ok": True}

    @app.post("/api/repeats/{rid}/stop")
    def repeats_stop(rid: str):
        if not st.repeats.stop(rid):
            raise HTTPException(404, "No such schedule")
        return {"ok": True}

    @app.get("/api/repeats/{rid}/summary")
    def repeats_summary(rid: str):
        c = st.repeats.combined(rid)
        if c is None:
            raise HTTPException(404, "No such schedule")
        return c

    @app.get("/api/wifi/powersave")
    def wifi_powersave_status(host: str = ""):
        """Is the path to `host` a Wi-Fi link with power saving on? (Setup pre-flight.)"""
        w = system.wifi_path(host)
        return {"wifi": w is not None, **(w or {}), "keep_off": bool(st.store.get_setting("wifi_powersave_off", False))}

    @app.post("/api/wifi/powersave/off")
    def wifi_powersave_off(body: dict = Body(...)):
        iface = str(body.get("interface") or "")
        r = system.wifi_powersave_off(iface)
        if r["ok"]:
            st.store.set_setting("wifi_powersave_off", True)         # re-applied at start-up and before each test
        return r

    @app.post("/api/probe")
    def probe(body: dict = Body(...)):
        """Pre-flight: can the Pi that sends reach the Pi that answers? (Prepares the answering end for a moment, asks, and lets it go.)"""
        if st.runs.busy():
            raise HTTPException(409, "A test is running.")
        try:
            prof = P.validate_profile(body)
            if prof["kind"] != "trip":
                raise ValueError("Only trip tests can be checked this way.")
            legs = PL.plan_trip(prof, st.services.context())
            OR.check_nodes(st.services, legs)
            legs = PL.plan_trip(prof, st.services.context())
            OR.check_clocks(st.services, legs, prof)
        except (factory.ConfigError, ValueError) as e:
            raise HTTPException(422, str(e))
        leg = legs[0]
        owner = OR.new_owner(st.services) + "-probe"
        prepared = []
        try:
            try:
                prepared = OR.prepare_reflectors(st.services, [leg], owner)
            except PL.PlanError as e:
                return {"ok": False, "error": str(e)}
            lp = leg.profile
            if not leg.tester.is_self:                               # the Pi that sends does the check itself
                try:
                    r = st.services.client(leg.tester).post("/api/probe", lp, timeout=15)
                except PE.PeerError as e:
                    return {"ok": False, "error": str(e)}
                if isinstance(r, dict):
                    r.setdefault("legs", [l.label for l in legs])
                return r
            if mbrun.is_mirrored(lp):
                try:
                    return mbrun.probe(lp)
                except (factory.ConfigError, ValueError) as e:
                    raise HTTPException(422, str(e))
            tr = factory.make_transport(lp, "tester")
            t = engine.Tester(lp, tr)
            try:
                tr.open()
                t._hello()
                from .runmgr import path_warning
                w = path_warning(lp["net"]["peer_host"]) if lp["transport"] in ("udp", "tcp") else None
                return {"ok": True, "peer": t.peer_info, "one_way_ok": t.one_way_ok, "legs": [l.label for l in legs], "warnings": [w] if w else []}
            except engine.PairingError as e:
                return {"ok": False, "error": str(e)}
            except OSError as e:
                return {"ok": False, "error": f"Could not open the port: {e}"}
            finally:
                try:
                    tr.close()
                except Exception:
                    pass
        finally:
            OR.release_reflectors(st.services, prepared)

    @app.post("/api/runs/{rid}/stop")
    def run_stop(rid: int, request: Request):
        if rid in st.runs.run_ids():                              # a test with several links is several runs: any of their ids stops the test
            st.runs.stop(by=(request.client.host if request.client else "") or "unknown")
        return {"ok": True}

    @app.get("/api/runs/{rid}")
    def run_get(rid: int):
        r = st.store.get_run(rid)
        if not r:
            raise HTTPException(404, "No such run")
        r["intervals"] = st.store.run_gen_intervals(rid) if r["transport"] == "generator" else st.store.run_intervals(rid)
        r["kind"], r["job_line"], r["vs_baseline"] = CMP.kind_of(r), CMP.job_line(r["job"]), CMP.vs_baseline(st.store, r)
        return r

    @app.delete("/api/runs/{rid}")
    def run_delete(rid: int):
        if st.runs.busy() and rid in st.runs.run_ids():
            raise HTTPException(409, "This test is running.")
        st.store.delete_run(rid)
        return {"ok": True}

    # ------------------------------------------------------------ job info, compare, baseline (SPEC 7.4, 7.5)
    def _job_in(job) -> dict:
        try:
            return CMP.clean_job(job)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/job/last")
    def job_last():
        """What the Start bar pre-fills: Site, Link / radio and By of the newest run that had any (the note belongs to one run and is not carried over)."""
        j = st.store.last_job()
        return {"job": {k: v for k, v in j.items() if k != "note"}, "fields": [{"key": k, "label": label, "max": mx} for k, label, mx in CMP.JOB_FIELDS]}

    @app.put("/api/runs/{rid}/job")
    def run_job_put(rid: int, body: dict = Body(...)):
        if not st.store.get_run(rid):
            raise HTTPException(404, "No such run")
        job = _job_in(body.get("job", body))
        ids = st.store.set_job(rid, job)
        return {"ok": True, "job": job, "job_line": CMP.job_line(job), "run_ids": ids}

    @app.post("/api/runs/{rid}/baseline")
    def run_baseline_set(rid: int):
        try:
            return CMP.set_baseline(st.store, rid)
        except KeyError:
            raise HTTPException(404, "No such run")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/runs/{rid}/baseline")
    def run_baseline_clear(rid: int):
        try:
            return CMP.clear_baseline(st.store, rid)
        except KeyError:
            raise HTTPException(404, "No such run")

    @app.get("/api/compare")
    def compare_runs(run_ids: str = ""):
        try:
            return CMP.compare(st.store, _ints(run_ids))
        except CMP.CompareError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/compare.pdf")
    def compare_pdf(run_ids: str = ""):
        from .report import build_comparison
        try:
            pdf = build_comparison(st.store, _ints(run_ids), pi_name=st.me.name)
        except CMP.CompareError as e:
            raise HTTPException(422, str(e))
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="comparison-{time.strftime("%Y%m%d-%H%M")}.pdf"'})

    @app.get("/api/runs/{rid}/messages")
    def run_messages(rid: int, limit: int = Query(20000, le=200000)):
        return st.store.run_messages(rid, limit)

    @app.get("/api/runs/{rid}/live")
    def run_live(rid: int):
        d = st.runs.leg_live(rid)                 # any link of the running test (the table of links picks one)
        if d is None:
            raise HTTPException(404, "That test is not the one running now.")
        return d

    @app.post("/api/runs/{rid}/mark")
    def run_mark(rid: int, body: dict = Body(default={})):
        """Drop a flag on the running test ("antenna moved"). The "since mark" numbers start again from it; the verdict stays for the whole test."""
        if rid not in st.runs.run_ids():
            raise HTTPException(404, "That test is not the one running now.")
        try:
            return st.runs.mark(str((body or {}).get("name") or ""))
        except Busy as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))

    def _dl(content, media, name):
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/runs/{rid}/export.{fmt}")
    def run_export(rid: int, fmt: str, kind: str = "messages"):
        if not st.store.get_run(rid):
            raise HTTPException(404, "No such run")
        if fmt == "csv" and kind == "mbevents":
            run = st.store.get_run(rid)
            m = ((run.get("summary") or {}).get("mirrored") or {})
            if not m.get("emulator"):
                raise HTTPException(404, "This test has no Mirrored Bits event log")
            from .mbemu.engine import events_to_csv
            return _dl(events_to_csv(m.get("events_log") or [], (run.get("started_at") or "")), "text/csv", f"run-{rid}-mirrored-bits-events.csv")
        if fmt == "csv" and kind == "steps":
            sm = (st.store.get_run(rid).get("summary") or {})
            if not sm.get("steps"):
                raise HTTPException(404, "This test did not run in steps")
            from .gen.run import steps_csv
            return _dl(steps_csv(sm), "text/csv", f"run-{rid}-steps.csv")
        if fmt == "csv":
            body = st.store.gen_csv(rid) if kind == "gen" else st.store.intervals_csv(rid) if kind == "intervals" else st.store.messages_csv(rid)
            return _dl(body, "text/csv", f"run-{rid}-{kind}.csv")
        if fmt == "json":
            return _dl(json.dumps(st.store.run_json(rid), indent=1), "application/json", f"run-{rid}.json")
        raise HTTPException(404, "Unknown format")

    @app.get("/api/sessions/{sid}/export.zip")
    def session_export(sid: int):
        if not st.store.get_session(sid):
            raise HTTPException(404, "No such session")
        return _dl(st.store.session_zip(sid), "application/zip", f"session-{sid}.zip")

    @app.get("/api/report.pdf")
    def report_pdf(run_ids: Optional[str] = None, session_id: Optional[int] = None, title: str = "Link test report", repeat_id: Optional[str] = None):
        from .report import build_report
        comb = None
        if repeat_id:                                             # every run of a scheduled repeat, with the combined summary on top
            comb = st.repeats.combined(repeat_id)
            if comb is None:
                raise HTTPException(404, "No such schedule")
            run_ids = ",".join(str(x["run_id"]) for x in comb["rows"]) or None
            title = title if title != "Link test report" else f"Scheduled repeats: {comb['name']}"
        if run_ids:
            ids = _ints(run_ids)
        elif session_id:
            ids = [r["id"] for r in reversed(st.store.list_runs(session_id))]
        else:
            raise HTTPException(422, "Give run_ids or session_id.")
        if not ids:
            raise HTTPException(404, "No tests to put in the report.")
        pdf = build_report(st.store, ids, title[:80], pi_name=st.me.name, repeat=comb)
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="report-{time.strftime("%Y%m%d-%H%M")}.pdf"'})

    # ------------------------------------------------------------ USB
    @app.get("/api/usb")
    def usb():
        return system.usb_drives()

    @app.post("/api/usb/save")
    def usb_save(body: dict = Body(...)):
        drives = system.usb_drives()
        if not drives:
            raise HTTPException(409, "Plug in a USB drive first.")
        sid = body.get("session_id") or st.store.current_session()["id"]
        if not isinstance(sid, int) or not st.store.get_session(sid):
            raise HTTPException(422, "No such session.")
        d = drives[0]["path"]
        out = os.path.join(d, f"traffic-tester-session-{sid}-{time.strftime('%Y%m%d-%H%M%S')}.zip")
        try:
            with open(out, "wb") as f:
                f.write(st.store.session_zip(int(sid)))
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            raise HTTPException(500, f"Could not write to the USB drive: {e}")
        return {"ok": True, "path": out}

    # ------------------------------------------------------------ responder (older name: it is the reflector now)
    @app.post("/api/responder/start")
    def responder_start(body: dict = Body(...)):
        """Kept for older callers and tests: asks this Pi's reflector to answer on the given link until /api/responder/stop."""
        cfg = {k: body[k] for k in ("transport", "serial", "net", "goose", "frame", "mode", "mirrored", "timeout_ms", "threshold_ms", "rule") if k in body}
        try:
            st.reflectors.release("legacy")
            return st.reflectors.ensure(cfg, "legacy", float(body.get("delay_ms", 0)), float(body.get("drop_pct", 0)))
        except ReflectorBusy as e:
            raise HTTPException(409, str(e))
        except (factory.ConfigError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))
        except OSError as e:
            raise HTTPException(422, f"Could not open the port: {e}")

    # ------------------------------------------------------------ Mirrored Bits emulator (docs/MIRRORED-BITS-SPEC.md)
    def _mb():
        return st.services.mb

    def _mb_local():
        emu = _mb().local
        if emu is None:
            raise HTTPException(409, "No Mirrored Bits channel is running on this Pi.")
        return emu

    @app.get("/api/mb/live")
    def mb_live(since_tx: int = 0, since_rx: int = 0, r_since_tx: int = 0, r_since_rx: int = 0):
        """Everything the live screen needs: this Pi's end (lamps, counters, newest messages), the other end through ITS web API, warnings."""
        return _mb().view(since_tx, since_rx, r_since_tx, r_since_rx)

    @app.get("/api/mb/reflect")
    def mb_reflect():
        """What this end's outputs did (the starting Pi matches them with its own input flips to time each bit; needs synced clocks)."""
        ev = _mb_local().reflect_events()
        return {**ev, "clock": {**system.clock_state(0), "ips": []}}

    @app.get("/api/mirrored/events")
    def mirrored_events():
        """Older name of /api/mb/reflect."""
        emu = _mb().local
        if emu is None:
            return {"events": [], "clock": system.clock_state(0), "running": False}
        return {"events": emu.reflect_events()["rmb_changes"], "clock": system.clock_state(0), "running": emu.running}

    @app.post("/api/mb/tmb")
    def mb_tmb(body: dict = Body(...)):
        """Flip a contact input: IN<bit> drives TMB<bit>.  side 'remote' flips it on the other Pi."""
        try:
            bit, val = int(body["bit"]), 1 if body.get("value", 1) else 0
            if body.get("side") == "remote":
                return _mb_remote("POST", "/api/mb/tmb", {"bit": bit, "value": val})
            emu = _mb_local()
            emu.set_tmb(bit - 1, val)
            return {"ok": True, "tmb": emu.io.mask}
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(422, str(e))

    def _mb_remote(method: str, path: str, body=None):
        reg = _mb()
        if reg.far_local is not None:
            return None
        if reg.far_client is None:
            raise HTTPException(409, "The other end is not controlled from this Pi.")
        try:
            return reg.far_client.call(method, path, body, timeout=6)
        except PE.PeerAuthError as e:
            raise HTTPException(409, str(e))
        except PE.PeerError as e:
            raise HTTPException(502, str(e))

    @app.post("/api/mb/impair")
    def mb_impair(body: dict = Body(...)):
        """In-app impairment injector (between codec and transport) of this end's sending side.  scope: 'local' | 'remote' | 'both'."""
        scope, cfg = body.get("scope", "local"), body.get("cfg") or {}
        out = {}
        try:
            if scope in ("local", "both"):
                out["local"] = _mb_local().set_impairment(cfg)
            if scope in ("remote", "both"):
                reg = _mb()
                if reg.far_local is not None:
                    out["remote"] = reg.far_local.set_impairment(cfg)
                else:
                    out["remote"] = _mb_remote("POST", "/api/mb/impair", {"scope": "local", "cfg": cfg})
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True, **out}

    @app.post("/api/mb/settings")
    def mb_settings(body: dict = Body(...)):
        """Settings that can change while running (counts, defaults, thresholds, labels)."""
        try:
            _mb_local().apply_settings(body.get("settings") or {})
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "settings": _mb_local().s.to_dict()}

    @app.get("/api/mb/events.csv")
    def mb_events_csv(side: str = "local"):
        reg = _mb()
        emu = reg.far_local if (side == "remote" and reg.far_local is not None) else _mb_local()
        if side == "remote" and reg.far_local is None:
            try:
                raw = reg.far_client.call("GET", "/api/mb/events.csv?side=local", None, timeout=8, raw=True)
            except PE.PeerError as e:
                raise HTTPException(502, str(e))
            return Response(raw, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="mirrored-bits-events-far.csv"'})
        return Response(emu.events_csv(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="mirrored-bits-events.csv"'})

    @app.get("/api/mb/io")
    def mb_io():
        return _mb_local().io.state()

    @app.post("/api/mb/io/gpio")
    def mb_io_gpio(body: dict = Body(...)):
        """Map IN1..8 / OUT1..8 to Pi GPIO (BCM numbers); enable=false goes back to the on-screen switches and lamps."""
        io = _mb_local().io
        try:
            if not body.get("enable", True):
                io.disable_gpio()
                return io.state()
            return io.configure_gpio(body.get("in_pins") or [None] * 8, body.get("out_pins") or [None] * 8, bool(body.get("pull_up", True)), bool(body.get("active_low_in", False)))
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e))
        except RuntimeError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/mb/selftest")
    def mb_selftest(body: dict = Body(default={})):
        """Internal back-to-back self-test: two emulators in this process, PROTO A <-> PROTO B, watched live like a real channel."""
        try:
            return _mb().start_selftest(float(body.get("duration_s", 60)), body.get("a"), body.get("b"), body.get("impair_a"), body.get("impair_b"), int(body.get("baud", 38400)))
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e))
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/mb/selftest/stop")
    def mb_selftest_stop():
        _mb().stop_selftest()
        return {"ok": True}

    @app.post("/api/responder/stop")
    def responder_stop():
        st.reflectors.release("legacy")
        return {"ok": True}

    # ------------------------------------------------------------ traffic generator node (called by the other Pi)
    def _gen_err(e):
        return HTTPException(422, str(e))

    @app.get("/api/gen/node/status")
    def gen_status():
        from .gen import run as grun
        st.gen_orphan_check()
        return {**grun.local_status(), "busy": st.gen_node.busy() or st.runs.busy_except_capture(), "version": __version__, "pi": {"id": st.me.id, "name": st.me.name}}

    @app.post("/api/gen/node/rx/start")
    def gen_rx_start(body: dict = Body(...)):
        if st.runs.busy_except_capture():
            raise HTTPException(409, "This Pi is running a test.")
        st.gen_touched = time.monotonic()
        try:
            st.gen_node.rx_start(body["streams"], body.get("interface", "auto"))
        except (gnode.GenError, KeyError) as e:
            raise _gen_err(e)
        return {"ok": True}

    @app.post("/api/gen/node/tx/start")
    def gen_tx_start(body: dict = Body(...)):
        st.gen_touched = time.monotonic()
        try:
            st.gen_node.tx_start(body["streams"], body.get("interface", "auto"), float(body.get("start_in_s", 0)))
        except (gnode.GenError, KeyError) as e:
            raise _gen_err(e)
        return {"ok": True}

    @app.get("/api/gen/node/live")
    def gen_live(since: int = 0):
        st.gen_touched = time.monotonic()
        return st.gen_node.live(since)

    @app.post("/api/gen/node/senders/stop")
    def gen_stop_senders():
        """Stop only the senders (the receivers keep counting what is still on its way). The controller does this on every Pi first, then stops them all."""
        st.gen_touched = time.monotonic()
        st.gen_node.stop_senders()
        return {"ok": True}

    @app.post("/api/gen/node/stop")
    def gen_stop():
        return st.gen_node.stop()

    # ------------------------------------------------------------ sniffer
    @app.post("/api/sniff/preflight")
    def sniff_preflight(request: Request, body: dict = Body(default={})):
        prof = None
        if body:
            try:
                prof = P.validate_profile(body)
            except ValueError as e:
                raise HTTPException(422, str(e))
        return spre.preflight(prof, request.client.host if request.client else None)

    @app.get("/api/wifi/builtin/status")
    def wifi_builtin_status():
        from .sniff import builtin_wifi
        return builtin_wifi.status()

    @app.post("/api/wifi/builtin/restore")
    def wifi_builtin_restore():
        """One click: stock Wi-Fi driver + firmware back, NetworkManager and the Wi-Fi link restored."""
        from .sniff import builtin_wifi
        if st.runs.busy() and st.runs.sniff and st.runs.sniff.cfg["mode"] == "wifi":
            st.runs.stop()
            st.runs.wait(40)
        try:
            return builtin_wifi.restore()
        except builtin_wifi.BuiltinWifiError as e:
            raise HTTPException(500, e.message)

    @app.post("/api/wifi/builtin/inject-test")
    def wifi_builtin_inject():
        """EXPERIMENTAL packet-injection self-test (aireplay-ng --test); only while the built-in chip is in monitor mode."""
        from .sniff import builtin_wifi
        try:
            res = builtin_wifi.inject_test()
        except builtin_wifi.BuiltinWifiError as e:
            raise HTTPException(409, e.message)
        if not res.get("ok"):
            raise HTTPException(409, res.get("message", "Injection test failed."))
        return res

    @app.get("/api/sniff/filters")
    def sniff_filters():
        return sbpf.PRESETS

    @app.post("/api/sniff/filter/check")
    def sniff_filter_check(body: dict = Body(...)):
        try:
            expr = sbpf.build(body["kind"], body.get("value", "")) if body.get("kind") else body.get("expr", "")
        except ValueError as e:
            raise HTTPException(422, str(e))
        err = sbpf.validate(expr, body.get("linktype", "EN10MB"))
        return {"expr": expr, "ok": err is None, "error": err}

    @app.get("/api/sniff/live")
    def sniff_live(since: int = 0):
        s = st.runs.sniff
        return s.live(since) if s else {"n": 0, "rows": [], "running": False}

    @app.get("/api/sniff/packet/{n}")
    def sniff_packet(n: int):
        s = st.runs.sniff
        d = s.detail(n) if s else None
        if not d:
            raise HTTPException(404, "That packet is no longer in the live view.")
        return d

    def _cap_files(rid: int):
        r = st.store.get_run(rid)
        if not r or not r["summary"] or r["summary"].get("kind") != "sniffer":
            raise HTTPException(404, "No capture for this test")
        return r["summary"]

    @app.get("/api/runs/{rid}/capture")
    def capture_list(rid: int):
        s = _cap_files(rid)
        return {"location": s["location"], "files": s["files"]}

    @app.get("/api/runs/{rid}/capture/{name}")
    def capture_download(rid: int, name: str):
        from fastapi.responses import FileResponse
        s = _cap_files(rid)
        if name not in [f["name"] for f in s["files"]]:
            raise HTTPException(404, "No such file")
        path = os.path.join(s["location"], name)
        if not os.path.isfile(path):
            raise HTTPException(404, "The file was removed (ring buffer or drive unplugged).")
        return FileResponse(path, filename=name)

    @app.post("/api/runs/{rid}/capture/usb")
    def capture_to_usb(rid: int):
        s = _cap_files(rid)
        drives = system.usb_drives()
        if not drives:
            raise HTTPException(409, "Plug in a USB drive first.")
        files = [os.path.join(s["location"], f["name"]) for f in s["files"] if os.path.isfile(os.path.join(s["location"], f["name"]))]
        need = sum(os.path.getsize(f) for f in files)
        free = shutil.disk_usage(drives[0]["path"]).free
        if need > free - min(50 * 1024 * 1024, free // 5):
            raise HTTPException(409, f"Not enough room on the USB drive ({free // 1048576} MB free, {need // 1048576} MB needed).")
        dest = os.path.join(drives[0]["path"], "traffic-tester-captures", f"run-{rid}")
        os.makedirs(dest, exist_ok=True)
        for f in files:
            if os.path.abspath(os.path.dirname(f)) != os.path.abspath(dest):
                shutil.copy2(f, dest)
        os.sync()
        return {"ok": True, "path": dest, "files": len(files), "bytes": need}

    @app.get("/api/runs/{rid}/gen-intervals")
    def run_gen_intervals(rid: int):
        return st.store.run_gen_intervals(rid)

    # ------------------------------------------------------------ routing self-check (Wi-Fi = internet, Ethernet = test path)
    @app.get("/api/net/check")
    def net_check():
        return netcheck.check(netcheck.peers_from_profiles(st.store.list_profiles()))

    # ------------------------------------------------------------ websocket
    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        if st.pw_hash() and ws.cookies.get("tt_token") not in st.tokens:
            await ws.close(code=4401)
            return
        await ws.accept()
        clients.append(ws)
        try:
            await ws.send_json({"events": [], "snapshot": {"run": st.runs.status()}})
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            if ws in clients:
                clients.remove(ws)

    async def broadcaster():
        last_refl = [0.0]
        while True:
            await asyncio.sleep(0.1)
            if not clients:
                st.runs.ui_events.clear() if not st.runs.busy() else None
                continue
            evs = []
            q = st.runs.ui_events
            while q and len(evs) < 600:
                evs.append(q.popleft())
            if time.monotonic() - last_refl[0] > 1.0:
                last_refl[0] = time.monotonic()
                rs = st.reflectors.legacy_status()
                if rs.get("running"):
                    evs.append({"ev": "responder", **rs})
            if not evs:
                continue
            # coalesce: at most 120 'tx'/'msg' events per frame to keep phones smooth
            msgs = [e for e in evs if e["ev"] in ("tx", "msg")]
            if len(msgs) > 240:
                keep = set(id(e) for e in msgs[-240:])
                evs = [e for e in evs if e["ev"] not in ("tx", "msg") or id(e) in keep]
            dead = []
            for c in list(clients):
                try:
                    await c.send_json({"events": evs})
                except Exception:
                    dead.append(c)
            for c in dead:
                if c in clients:
                    clients.remove(c)

    # ------------------------------------------------------------ software updates (signed bundles; the install itself is done by a helper, never by this process)
    def _uerr(e: UpdateError):
        return HTTPException(e.status, e.message)

    @app.get("/api/about")
    def about():
        """About page: what is installed, which Pi this is, the update channel status and the change log (CHANGELOG.md is the one source)."""
        cur = st.updates.current()
        last = st.updates.last_online() or {}
        entries = changelog.load(st.updates.app_dir) or changelog.load(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return {"name": "Pi Automation Traffic Tester", "product": "Pi Automation Traffic Tester",
                "version": __version__, "date": cur.get("date"), "previous_version": cur.get("previous_version"),
                "pi": {"id": st.me.id, "name": st.me.name}, "hostname": socket.gethostname(), "hardware": system.pi_model(),
                "update": {"checked": last.get("checked"), "newest": last.get("version"), "newer": bool(last.get("newer")), "changelog": last.get("changelog") or []},
                "changelog": entries}

    @app.get("/api/update/status")
    def update_status():
        return st.updates.info()

    @app.post("/api/update/check")
    def update_check():
        try:
            return st.updates.check_online()
        except UpdateError as e:
            raise _uerr(e)

    @app.post("/api/update/upload")
    async def update_upload(request: Request, name: str = Query("update.ptt")):
        """The raw .ptt as the request body (no multipart dependency). It is verified before it is accepted."""
        try:
            n = int(request.headers.get("content-length") or 0)
        except ValueError:
            n = 0
        if n > MAX_UPLOAD:
            raise HTTPException(413, "That file is too large to be an update bundle.")
        os.makedirs(st.updates.inbox, exist_ok=True)
        tmp = os.path.join(st.updates.inbox, f".upload.{secrets.token_hex(6)}.ptt")       # one file per upload: two uploads at once must not write into each other
        total = 0
        staged = False
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise HTTPException(413, "That file is too large to be an update bundle.")
                    f.write(chunk)
            if total == 0:
                raise HTTPException(422, "The file is empty.")
            try:
                d = await asyncio.to_thread(st.updates.stage_file, tmp)
                staged = True
                return d
            except UpdateError as e:
                raise _uerr(e)
        finally:
            if not staged:                                        # refused, too large, empty, or the browser went away half way: nothing is left behind
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    @app.post("/api/update/discard")
    def update_discard():
        for fn in (st.updates.staged, os.path.join(st.updates.upd, "staged.json")):
            try:
                os.unlink(fn)
            except OSError:
                pass
        return {"ok": True}

    @app.post("/api/update/install")
    def update_install(body: dict = Body(default={})):
        try:
            if body.get("source") == "online":
                st.updates.stage_online()
            return JSONResponse(st.updates.install(bool(body.get("allow_older"))), status_code=202)
        except UpdateError as e:
            raise _uerr(e)

    @app.post("/api/update/rollback")
    def update_rollback():
        try:
            return JSONResponse(st.updates.rollback(), status_code=202)
        except UpdateError as e:
            raise _uerr(e)

    @app.post("/api/update/settings")
    def update_settings(body: dict = Body(...)):
        st.store.set_setting("update_daily_check", bool(body.get("daily_check")))
        return {"ok": True, "daily_check": bool(body.get("daily_check"))}

    @app.post("/api/update/partner")
    def update_partner(body: dict = Body(...)):
        host = str(body.get("host") or "").strip()
        pw = str(body.get("password") or "")
        pid = str(body.get("peer") or "").strip()
        name = ""
        if pid:                                                   # a Pi picked from "Pis on this network": its address, port and saved password
            p = st.peers.resolve(pid)
            if not p:
                raise HTTPException(404, "That Pi is not in your list.")
            host, name = p["host"], p.get("name") or ""
            body = {**body, "port": p.get("port")}
            pw = pw or st.peers.client(p).password
        if not host or any(c in host for c in " /\\?#@") or len(host) > 100:
            raise HTTPException(422, "Enter the IP address of the Pi to update (like 192.168.1.20).")
        try:
            port = int(body.get("port") or 8080)
        except (TypeError, ValueError):
            raise HTTPException(422, "The port must be a number.")
        if st.updates.partner.get("state") == "working":
            raise HTTPException(409, "An update of another Pi is already running.")
        if not os.path.isfile(st.updates.staged):
            raise HTTPException(409, "No update is ready on this Pi. Upload a file or check online first.")
        st.updates.partner = {"state": "working", "message": "Starting…", "host": host, "name": name, "ts": time.time()}
        threading.Thread(target=st.updates.push_partner, args=(host, port, pw), daemon=True).start()
        return JSONResponse({"started": True}, status_code=202)

    async def daily_update_check():
        """Optional (off by default): look for a new release once a day and only NOTE it in the UI. Never installs anything."""
        while True:
            await asyncio.sleep(1800)
            try:
                if st.updates.daily_due() and not st.updates.busy():
                    await asyncio.to_thread(st.updates.check_online)
            except Exception:
                pass

    # ------------------------------------------------------------ AI guide (read-only; same sign-in rule as the rest of the app)
    def _authed(request: Request) -> bool:
        return not st.pw_hash() or request.cookies.get("tt_token") in st.tokens

    def _guide_headers() -> dict:
        return {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}

    @app.get("/ai-guide", response_class=HTMLResponse)
    def ai_guide(request: Request):
        return HTMLResponse(to_html(aig.build_doc(st, app, _authed(request))), headers=_guide_headers())

    @app.get("/ai-guide.txt", response_class=PlainTextResponse)
    @app.get("/llms.txt", response_class=PlainTextResponse)
    def ai_guide_txt(request: Request):
        return PlainTextResponse(to_text(aig.build_doc(st, app, _authed(request))), headers=_guide_headers())

    @app.get("/api/ai-context")
    def ai_context():
        return {"live": aig.live_line(st, True), "guide": aig.GUIDE_PATH}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
    def index_page(request: Request):
        """The single-page app, with the small server-rendered 'For AI assistants' note filled in (it is in the page source, not added by script)."""
        html = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
        html = html.replace("<!--APP_VERSION-->", f"v{__version__}").replace("<!--AI_CONTEXT-->", aig.context_html(st, _authed(request))).replace("<!--AI_DATA-->", f'<script type="application/json" id="aiData">{aig.data_json(st)}</script>')
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    # JS/CSS compress about 4x: matters on first load over a slow Wi-Fi / cellular management link. Only the static files are
    # wrapped: API responses are never buffered by a compressor.
    fbapi.register(app, st)                                                     # first boot of a card image: Name this Pi
    netapi.register(app, st)                                                    # Settings -> Network (Wi-Fi access point, Ethernet ports, auto-revert)
    app.mount("/", GZipMiddleware(StaticFiles(directory=STATIC, html=True), minimum_size=1024, compresslevel=6), name="static")
    return app
