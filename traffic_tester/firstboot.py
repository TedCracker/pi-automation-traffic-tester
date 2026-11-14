"""First boot of a ready-made card image: "Name this Pi".

The image carries a marker file (`first-boot` in the data folder). While it exists and the Pi has no name yet, the web page shows only a big
"Name this Pi" screen (default pi1, pi2, ... the next name not used by a Pi found on the network). Naming sets the hostname (<name>.local),
renames the Pi's own Wi-Fi network from `pi-setup` to the name, and deletes the marker. Installs made with install-pi.sh have no marker and never
see any of this, so updating a Pi that is already in use changes nothing.

Also here: a tiny port-80 listener that redirects every request to the app (phones probe port 80 to decide whether a Wi-Fi network has a "sign in"
page; the card image's setup network answers every DNS name with the Pi so the phone opens the setup page by itself)."""
from __future__ import annotations

import http.server
import logging
import os
import re
import threading
from typing import Iterable, List, Optional

log = logging.getLogger("tt")
MARKER = "first-boot"
NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,30}[A-Za-z0-9])?$")


def marker_path(data_dir: str) -> str:
    return os.path.join(data_dir, MARKER)


def is_first_boot(data_dir: str, store) -> bool:
    if not os.path.exists(marker_path(data_dir)):
        return False
    n = store.get_setting("pi_name")
    return not (isinstance(n, str) and n.strip())


def finish(data_dir: str) -> None:
    try:
        os.unlink(marker_path(data_dir))
    except FileNotFoundError:
        pass


def clean_name(name) -> str:
    n = str(name or "").strip()
    if not NAME_RE.match(n):
        raise ValueError("Use 1 to 32 letters, digits or dashes (no spaces). It becomes the Pi's address (for example pi1.local) and the name of its Wi-Fi network.")
    return n.lower()


def suggest_name(taken: Iterable[str], prefix: str = "pi") -> str:
    """pi1, pi2, ...: the first one no Pi on the network uses (case ignored)."""
    used = {str(t).strip().lower() for t in taken if t}
    i = 1
    while f"{prefix}{i}" in used:
        i += 1
    return f"{prefix}{i}"


def taken_names(peers: List[dict]) -> List[str]:
    out: List[str] = []
    for p in peers or []:
        for k in ("name", "hostname"):
            v = p.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip().split(".")[0])
    return out


# ---------------------------------------------------------------- port 80: send phones (and anyone typing http://pi) to the app
class _Redirect(http.server.BaseHTTPRequestHandler):
    port = 8080

    def _go(self):
        host = self.connection.getsockname()[0]
        self.send_response(302)
        self.send_header("Location", f"http://{host}:{self.port}/")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_HEAD = do_POST = do_PUT = _go

    def log_message(self, *a):                  # quiet
        pass


def start_redirect(app_port: int, listen_port: int = 80, host: str = "0.0.0.0") -> Optional[http.server.ThreadingHTTPServer]:
    """Best effort: needs CAP_NET_BIND_SERVICE (the service has it). Returns the server, or None when the port is taken / not allowed."""
    handler = type("H", (_Redirect,), {"port": app_port})
    try:
        srv = http.server.ThreadingHTTPServer((host, listen_port), handler)
    except OSError as e:
        log.info("First boot: port %s redirect not started (%s)", listen_port, e)
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="tt-firstboot-redirect").start()
    return srv
