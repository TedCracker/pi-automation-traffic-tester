"""The Pis this Pi can talk to (docs/SPEC.md section 2).

Every Pi runs the same app. It has a friendly name (default = hostname), announces itself on the network as an mDNS service
`_pitester._tcp` (name / id / version / ip), listens for the other Pis' announcements, and also lets the user add a Pi by IP address
(networks without multicast). A background poll keeps "reachable / version / clock" fresh for the "Pis on this network" list.

Talking to another Pi is plain HTTP to its normal web API (`PeerClient`). When that Pi has an app password set, the controller logs in
with the password stored for that peer (one login per peer, cached in memory as that peer's session token)."""
from __future__ import annotations

import http.cookiejar
import json
import logging
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from . import __version__, system

log = logging.getLogger("tt")
APP_ID = "pi-traffic-tester"
SERVICE_TYPE = "_pitester._tcp.local."
DEFAULT_PORT = 8080
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}$")


class PeerError(Exception):
    pass


class PeerAuthError(PeerError):
    pass


# ---------------------------------------------------------------- addresses
def parse_address(text: str) -> Tuple[str, Optional[int]]:
    """"http://10.0.0.5:8080/", "10.0.0.5:8080", "pi2.local", "10.0.0.5" -> (host, port or None)."""
    a = (text or "").strip()
    if not a:
        raise PeerError("Enter the other Pi's IP address.")
    u = urlparse(a if "//" in a else "//" + a)
    host = u.hostname or ""
    if not host or not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", host):
        raise PeerError(f"'{a}' is not an IP address or Pi name.")
    try:
        port = u.port
    except ValueError:
        raise PeerError(f"'{a}' has a bad port number.")
    return host, port


def same_subnet24(a: str, b: str) -> bool:
    pa, pb = a.split("."), b.split(".")
    return len(pa) == 4 == len(pb) and pa[:3] == pb[:3]


def choose_address(from_ips: List[str], to_ips: List[str], fallback: str = "") -> str:
    """Which of `to_ips` should a Pi that owns `from_ips` use to reach it: one on the same /24, else the fallback (the address we already
    reach it at), else the first one."""
    for t in to_ips:
        if any(same_subnet24(f, t) for f in from_ips):
            return t
    if fallback:
        return fallback
    return to_ips[0] if to_ips else ""


# ---------------------------------------------------------------- this Pi's identity
class Identity:
    """Stable id, friendly name, first-setup time of this Pi (kept in the settings table)."""

    def __init__(self, store, port: int = DEFAULT_PORT):
        self.store, self.port = store, port
        pid = store.get_setting("pi_id")
        if not pid:
            pid = secrets.token_hex(4)
            store.set_setting("pi_id", pid)
        self.id = pid
        sa = store.get_setting("setup_at")
        if not sa:
            sa = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            store.set_setting("setup_at", sa)
        self.setup_at = sa

    @property
    def name(self) -> str:
        n = self.store.get_setting("pi_name")
        return n if isinstance(n, str) and n.strip() else (socket.gethostname().split(".")[0] or "pi")

    def set_name(self, name: str) -> str:
        n = (name or "").strip()
        if not n:                                    # empty = back to the hostname
            self.store.set_setting("pi_name", None)
            return self.name
        if not NAME_RE.fullmatch(n):
            raise ValueError("Use 1-32 letters, digits, spaces, dots, dashes or underscores (it is also the Wi-Fi name of this Pi).")
        self.store.set_setting("pi_name", n)
        return n

    @staticmethod
    def ips() -> List[str]:
        from .gen import node as gnode
        out, seen = [], set()
        ifs = sorted(system.interfaces(), key=lambda i: (not i["wired"], i["name"]))
        for i in ifs:
            if not i["up"]:
                continue
            ip = None
            try:
                ip = gnode.iface_ipv4(i["name"])
            except Exception:
                ip = None
            if ip and ip not in seen and not ip.startswith("127."):
                seen.add(ip)
                out.append(ip)
        return out

    def info(self, extra: Optional[dict] = None) -> dict:
        d = {"app": APP_ID, "id": self.id, "name": self.name, "version": __version__, "port": self.port,
             "hostname": socket.gethostname(), "ips": self.ips(), "setup_at": self.setup_at,
             "clock_source": self.store.get_setting("clock_source", "auto")}
        d.update(extra or {})
        return d


# ---------------------------------------------------------------- talking to another Pi
class PeerClient:
    """HTTP to another Pi's web API. Logs in once with the peer's password when it asks (cookie kept for the life of this object)."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, password: str = "", label: str = ""):
        self.host, self.port, self.password = host, int(port or DEFAULT_PORT), password or ""
        self.label = label or host
        self.base = f"http://{host}:{self.port}"
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.logged_in = False

    def call(self, method: str, path: str, body: Any = None, timeout: float = 8.0, raw: bool = False, _retry: bool = True):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, method=method, data=data, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                d = r.read()
                if raw:
                    return d
                return json.loads(d) if d else None
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("detail")
            except Exception:
                detail = None
            if e.code == 401:
                # The other Pi keeps its sessions in memory: after it restarts (every software update) or its password is saved again, the cookie
                # we hold is worthless. So a 401 always means "log in again", once per call, whether or not we were logged in before.
                if self.password and _retry:
                    try:
                        self.call("POST", "/api/auth/login", {"password": self.password}, timeout=timeout, _retry=False)
                    except PeerAuthError:
                        self.logged_in = False
                        raise PeerAuthError(f"{self.label} did not accept the saved password. Enter it again in Settings → Pis on this network.")
                    self.logged_in = True
                    return self.call(method, path, body, timeout, raw, _retry=False)
                raise PeerAuthError(f"{self.label} asks for a password. Enter it in Settings → Pis on this network.")
            err = PeerError(f"{self.label} refused: {detail or e.code}")
            err.status = e.code                                              # type: ignore[attr-defined]
            err.detail = detail                                              # type: ignore[attr-defined]
            raise err
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PeerError(f"Cannot reach {self.label} at {self.host}:{self.port}: {getattr(e, 'reason', e)}")

    def get(self, path: str, timeout: float = 8.0):
        return self.call("GET", path, None, timeout)

    def post(self, path: str, body: Any = None, timeout: float = 8.0):
        return self.call("POST", path, body if body is not None else {}, timeout)


# ---------------------------------------------------------------- what other devices say about themselves is not trusted
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_SETUP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def clean_name(v: Any, fallback: str = "") -> str:
    """A Pi name as another device announced it -> only the characters a Pi name may have (it ends up in results, CSV files and reports)."""
    n = re.sub(r"[^A-Za-z0-9 ._-]", "", v if isinstance(v, str) else "").strip()[:32]
    n = n.lstrip(" ._-")
    return n or fallback


def clean_id(v: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", v if isinstance(v, str) else "")[:40]


def clean_ips(v: Any) -> List[str]:
    out = []
    for x in (v if isinstance(v, (list, tuple)) else []):
        if isinstance(x, str) and _IPV4_RE.match(x) and all(int(p) <= 255 for p in x.split(".")) and x not in out:
            out.append(x)
    return out[:16]


def clean_info(d: dict) -> dict:
    """/api/peer/info of another Pi, cleaned: text fields cut to plain characters, addresses to IPv4, the set-up time to a timestamp or nothing."""
    out = dict(d)
    out["id"] = clean_id(d.get("id"))
    out["name"] = clean_name(d.get("name"))
    out["version"] = re.sub(r"[^0-9A-Za-z.+-]", "", str(d.get("version") or ""))[:24]
    out["ips"] = clean_ips(d.get("ips"))
    out["hostname"] = re.sub(r"[^A-Za-z0-9._-]", "", str(d.get("hostname") or ""))[:64]
    out["hardware"] = re.sub(r"[^A-Za-z0-9 ._()/+-]", "", str(d.get("hardware") or ""))[:64] or None
    sa = d.get("setup_at")
    out["setup_at"] = sa if isinstance(sa, str) and _SETUP_RE.match(sa) else None
    cs = d.get("clock_source")
    out["clock_source"] = clean_id(cs) if isinstance(cs, str) else None
    if not isinstance(out.get("clock"), dict):
        out["clock"] = None
    try:
        out["port"] = int(d.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        out["port"] = DEFAULT_PORT
    return out


# ---------------------------------------------------------------- mDNS announcement parsing (pure, unit-tested)
def peer_from_service(name: str, properties: Dict[Any, Any], addresses: List[str], port: int, my_ips: Optional[List[str]] = None) -> Optional[dict]:
    """What an mDNS `_pitester._tcp` announcement says about a Pi -> a peer dict, or None when it is not ours / has no usable address."""
    def val(k: str) -> str:
        v = properties.get(k.encode()) if k.encode() in properties else properties.get(k)
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        return v or ""
    if val("app") and val("app") != APP_ID:
        return None
    ips = [a for a in clean_ips(list(addresses)) if not a.startswith(("127.", "169.254."))]
    for x in clean_ips([x for x in val("ips").split(",") if x]):
        if x not in ips and not x.startswith(("127.", "169.254.")):
            ips.append(x)
    if not ips:
        return None
    host = choose_address(my_ips or [], ips)
    nm = clean_name(val("name")) or clean_name(name.split("._pitester.")[0]) or host
    return {"id": clean_id(val("id")) or f"mdns-{host}", "name": nm, "host": host, "port": int(port or DEFAULT_PORT), "ips": ips,
            "version": re.sub(r"[^0-9A-Za-z.+-]", "", val("version"))[:24], "source": "mdns"}


# ---------------------------------------------------------------- the list
class PeerRegistry:
    """Peers = manual entries (saved in settings) + mDNS finds (memory) + live probe results. Thread-safe."""

    def __init__(self, store, identity: Identity, poll_s: float = 10.0, probe_timeout: float = 2.0):
        self.store, self.me = store, identity
        self.poll_s, self.probe_timeout = poll_s, probe_timeout
        self.lock = threading.RLock()
        self.manual: List[dict] = [dict(p) for p in (store.get_setting("peers") or []) if isinstance(p, dict) and p.get("host")]
        self.mdns: Dict[str, dict] = {}
        self.state: Dict[str, dict] = {}
        self.clients: Dict[str, PeerClient] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()

    # ---- persistence
    def _save(self) -> None:
        self.store.set_setting("peers", self.manual)

    def _key(self, p: dict) -> str:
        return p["id"]

    # ---- building the view
    def _entry(self, base: dict) -> dict:
        st = self.state.get(base["id"], {})
        info = st.get("info") or {}
        return {"id": base["id"], "name": info.get("name") or base.get("name") or base["host"], "host": base["host"], "port": base.get("port", DEFAULT_PORT),
                "source": base.get("source", "manual"), "ips": info.get("ips") or base.get("ips") or [base["host"]],
                "reachable": bool(st.get("reachable")), "version": info.get("version") or base.get("version") or "",
                "clock": info.get("clock"), "busy": bool(info.get("busy")), "password_required": bool(info.get("password_required")),
                "auth": st.get("auth", "unknown"), "has_password": bool(base.get("password")), "error": st.get("error") or "",
                "last_seen": st.get("last_seen"), "setup_at": info.get("setup_at"), "hardware": info.get("hardware"),
                "clock_source": info.get("clock_source")}

    def _bases(self) -> List[dict]:
        with self.lock:
            out = {m["id"]: dict(m, source="manual") for m in self.manual}
            for k, d in self.mdns.items():
                if k == self.me.id:
                    continue
                if k in out:
                    out[k]["source"] = "both"
                    out[k]["ips"] = d.get("ips")
                else:
                    out[k] = dict(d)
            return list(out.values())

    def list(self) -> List[dict]:
        return sorted((self._entry(b) for b in self._bases()), key=lambda e: e["name"].lower())

    def get(self, pid: str) -> Optional[dict]:
        for b in self._bases():
            if b["id"] == pid:
                return self._entry(b)
        return None

    def resolve(self, ref: str) -> Optional[dict]:
        """A Pi by id, then by name (any case), then by address."""
        ref = (ref or "").strip()
        if not ref:
            return None
        lst = self.list()
        for key in ("id",):
            for e in lst:
                if e[key] == ref:
                    return e
        for e in lst:
            if e["name"].lower() == ref.lower():
                return e
        for e in lst:
            if ref == e["host"] or ref in e["ips"]:
                return e
        return None

    # ---- changes
    def add_mdns(self, d: dict) -> None:
        if not d or d.get("id") == self.me.id:
            return
        with self.lock:
            d = dict(d, seen=time.time())
            self.mdns[d["id"]] = d
        self._wake.set()

    def remove_mdns(self, pid: str) -> None:
        with self.lock:
            self.mdns.pop(pid, None)

    def add_manual(self, address: str, password: str = "") -> dict:
        host, port = parse_address(address)
        port = port or DEFAULT_PORT
        info, err = self._fetch_info(host, port)
        if info is None:
            raise PeerError(err or f"No Pi Automation Traffic Tester answered at {host}:{port}. Check the address, that it is powered on, and that you are on the same network.")
        if info.get("id") == self.me.id:
            raise PeerError("That is this Pi.")
        with self.lock:
            pid = info["id"]
            entry = {"id": pid, "name": info.get("name") or host, "host": host, "port": port, "password": password or "", "added_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            self.manual = [m for m in self.manual if m["id"] != pid and not (m["host"] == host and m.get("port") == port)] + [entry]
            self._save()
            self.clients.pop(pid, None)
        self._record(pid, info, None, host, port, password)
        self.probe(pid)                                              # also finds out whether its password is needed and known
        return self.get(pid) or {}

    def remove(self, pid: str) -> bool:
        with self.lock:
            n = len(self.manual)
            self.manual = [m for m in self.manual if m["id"] != pid]
            self.mdns.pop(pid, None)
            self.clients.pop(pid, None)
            self.state.pop(pid, None)
            if len(self.manual) != n:
                self._save()
                return True
        return False

    def set_password(self, pid: str, password: str) -> dict:
        with self.lock:
            m = next((m for m in self.manual if m["id"] == pid), None)
            if m is None:                                    # a Pi found by mDNS becomes a saved one once it needs a password
                b = next((b for b in self._bases() if b["id"] == pid), None)
                if b is None:
                    raise PeerError("That Pi is not in the list.")
                m = {"id": pid, "name": b.get("name"), "host": b["host"], "port": b.get("port", DEFAULT_PORT), "added_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                self.manual.append(m)
            m["password"] = password or ""
            self._save()
            self.clients.pop(pid, None)
        self.probe(pid)
        return self.get(pid) or {}

    # ---- talking
    def client(self, peer: dict) -> PeerClient:
        """A logged-in-on-demand client for this peer (cached: its session token is the 'token per peer')."""
        with self.lock:
            pid = peer["id"]
            c = self.clients.get(pid)
            m = next((m for m in self.manual if m["id"] == pid), None)
            pw = (m or {}).get("password", "")
            if c is None or c.host != peer["host"] or c.port != peer["port"] or c.password != pw:
                c = PeerClient(peer["host"], peer["port"], pw, label=peer.get("name") or peer["host"])
                self.clients[pid] = c
            return c

    def _fetch_info(self, host: str, port: int) -> Tuple[Optional[dict], str]:
        try:
            d = PeerClient(host, port).call("GET", "/api/peer/info", timeout=self.probe_timeout)
        except PeerAuthError:
            return None, f"{host} asks for a password before it can be added: this Pi's app needs updating to the same version first."
        except PeerError as e:
            return None, str(e)
        if not isinstance(d, dict) or d.get("app") != APP_ID or not d.get("id"):
            return None, f"{host}:{port} answered, but it is not a Pi Automation Traffic Tester."
        d = clean_info(d)
        if not d["id"]:
            return None, f"{host}:{port} answered, but it is not a Pi Automation Traffic Tester."
        return d, ""

    def _record(self, pid: str, info: Optional[dict], err: Optional[str], host: str, port: int, password: str = "") -> None:
        with self.lock:
            st = self.state.setdefault(pid, {})
            if info:
                st["info"] = info
                st["reachable"] = True
                st["error"] = ""
                st["last_seen"] = time.time()
                st["auth"] = "unknown"
            else:
                st["reachable"] = False
                st["error"] = err or "no answer"

    def probe(self, pid: str) -> Optional[dict]:
        b = next((b for b in self._bases() if b["id"] == pid), None)
        if not b:
            return None
        info, err = self._fetch_info(b["host"], int(b.get("port", DEFAULT_PORT)))
        if info is None:                                            # a Pi with several addresses (Ethernet + Wi-Fi): the one we picked may not be reachable from here, try the others
            for alt in b.get("ips") or []:
                if alt == b["host"]:
                    continue
                i2, _ = self._fetch_info(alt, int(b.get("port", DEFAULT_PORT)))
                if i2 is not None and i2.get("id") == pid:
                    with self.lock:
                        if pid in self.mdns:
                            self.mdns[pid]["host"] = alt
                    info, err, b = i2, "", {**b, "host": alt}
                    break
        if info is not None and info["id"] != pid:                  # another Pi now answers at this address
            self._record(pid, None, f"A different Pi ({info.get('name')}) answers at {b['host']} now.", b["host"], b.get("port", DEFAULT_PORT))
            return self.get(pid)
        self._record(pid, info, err, b["host"], int(b.get("port", DEFAULT_PORT)))
        if info is not None and info.get("password_required"):
            self._check_auth(b, pid)
        elif info is not None:
            with self.lock:
                self.state[pid]["auth"] = "ok"
        return self.get(pid)

    def _check_auth(self, b: dict, pid: str) -> None:
        try:
            c = self.client(self._entry(b))
            c.call("GET", "/api/ports", timeout=self.probe_timeout)
            ok = "ok"
        except PeerAuthError:
            ok = "needs_password"
        except PeerError:
            ok = "unknown"
        with self.lock:
            self.state.setdefault(pid, {})["auth"] = ok

    def refresh(self, wait: bool = True) -> List[dict]:
        threads = []
        for b in self._bases():
            t = threading.Thread(target=self.probe, args=(b["id"],), daemon=True)
            t.start()
            threads.append(t)
        if wait:
            for t in threads:
                t.join(self.probe_timeout + 2)
        return self.list()

    def fresh(self, ref: str, max_age: float = 4.0) -> Optional[dict]:
        """The peer, re-probed when its last answer is older than `max_age` seconds (used right before a test)."""
        p = self.resolve(ref)
        if not p:
            return None
        ls = p.get("last_seen") or 0
        if time.time() - ls > max_age:
            self.probe(p["id"])
            p = self.get(p["id"])
        return p

    # ---- background poll
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="peer-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh(wait=True)
            except Exception as e:  # noqa
                log.warning("peer poll: %s", e)
            self._wake.wait(self.poll_s)
            self._wake.clear()


# ---------------------------------------------------------------- mDNS (python-zeroconf; optional)
class Discovery:
    """Announces this Pi and listens for the others. Needs the `zeroconf` package (pure Python, in requirements.txt); without it the list
    still works through "Add a Pi by IP"."""

    def __init__(self, identity: Identity, registry: PeerRegistry, port: int = DEFAULT_PORT, info_extra: Optional[Callable[[], dict]] = None):
        self.me, self.reg, self.port = identity, registry, port
        self.zc = None
        self.browser = None
        self.info = None
        self.error = ""
        self._lock = threading.Lock()

    def _service_info(self):
        from zeroconf import ServiceInfo
        ips = self.me.ips()
        host = socket.gethostname().split(".")[0]
        props = {"app": APP_ID, "id": self.me.id, "name": self.me.name, "version": __version__, "ips": ",".join(ips)}
        # The mDNS instance name carries the Pi id: two Pis with the same friendly name (fresh installs all start as their host name) must not collide.
        inst = re.sub(r"[^A-Za-z0-9 _-]", "-", self.me.name) + "-" + self.me.id
        return ServiceInfo(SERVICE_TYPE, f"{inst}.{SERVICE_TYPE}", port=self.port, properties=props,
                           addresses=[socket.inet_aton(i) for i in ips], server=f"{host}.local.")

    def start(self) -> None:
        threading.Thread(target=self._start, name="mdns-start", daemon=True).start()

    def _start(self) -> None:
        try:
            from zeroconf import ServiceBrowser, Zeroconf
        except ImportError:
            self.error = "mDNS discovery needs the 'zeroconf' package; use Add a Pi by IP."
            log.info(self.error)
            return
        try:
            with self._lock:
                self.zc = Zeroconf()
                try:
                    self.info = self._service_info()
                    self.zc.register_service(self.info, allow_name_change=True)
                except Exception as e:  # noqa
                    self.error = f"Could not announce this Pi on the network: {e}"
                    log.warning(self.error)
                disc = self

                class _L:
                    def add_service(self, zc, type_, name):
                        threading.Thread(target=disc._found, args=(zc, type_, name), daemon=True).start()

                    def update_service(self, zc, type_, name):
                        threading.Thread(target=disc._found, args=(zc, type_, name), daemon=True).start()

                    def remove_service(self, zc, type_, name):
                        disc._gone(name)
                self.browser = ServiceBrowser(self.zc, SERVICE_TYPE, _L())
        except Exception as e:  # noqa
            self.error = f"mDNS discovery could not start: {e}"
            log.warning(self.error)

    def _found(self, zc, type_, name) -> None:
        try:
            info = zc.get_service_info(type_, name, timeout=3000)
            if not info:
                return
            addrs = []
            for a in info.addresses:
                if len(a) == 4:
                    addrs.append(socket.inet_ntoa(a))
            d = peer_from_service(name, info.properties or {}, addrs, info.port, self.me.ips())
            if d:
                d["mdns_name"] = name
                self.reg.add_mdns(d)
        except Exception as e:  # noqa
            log.debug("mDNS resolve %s: %s", name, e)

    def _gone(self, name: str) -> None:
        for k, d in list(self.reg.mdns.items()):
            if d.get("mdns_name") == name:
                self.reg.remove_mdns(k)

    def rename(self) -> None:
        """The Pi's name changed: announce the new one."""
        with self._lock:
            if not self.zc:
                return
            try:
                if self.info:
                    self.zc.unregister_service(self.info)
                self.info = self._service_info()
                self.zc.register_service(self.info, allow_name_change=True)
            except Exception as e:  # noqa
                log.warning("mDNS rename: %s", e)

    def stop(self) -> None:
        with self._lock:
            try:
                if self.zc:
                    if self.info:
                        self.zc.unregister_service(self.info)
                    self.zc.close()
            except Exception:
                pass
            self.zc = None
