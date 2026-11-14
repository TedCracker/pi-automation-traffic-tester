"""HTTP API for the first-boot "Name this Pi" screen (see firstboot.py).

  GET  /api/setup/info   {first_boot, suggested, taken, ssid, password, hostname}   (also: the current Wi-Fi name of this Pi, for the Setup/Settings screens)
  POST /api/setup/name   {"name": "pi1"}  only while the Pi is still unnamed (409 afterwards); names the Pi (hostname, Wi-Fi name, mDNS) -> {name, ssid, password, urls}
"""
from __future__ import annotations

import socket
import threading

from fastapi import Body, HTTPException

from . import firstboot as FB
from .netcfg import client as ncl
from .update import netops


def register(app, st) -> None:
    def ap_ssid() -> str:
        try:
            prof = (ncl.read_state().get("profiles") or {}).get(netops.PROFILE_AP) or {}
            return prof.get("ssid") or ""
        except Exception:
            return ""

    def info() -> dict:
        taken = FB.taken_names(st.peers.list()) + [st.me.name]
        first = FB.is_first_boot(st.data_dir, st.store)
        return {"first_boot": first, "suggested": FB.suggest_name([t for t in taken if not (first and t == st.me.name)]), "taken": sorted(set(taken)),
                "ssid": ap_ssid(), "password": netops.DEFAULT_PSK, "hostname": socket.gethostname().split(".")[0]}

    @app.get("/api/setup/info")
    def setup_info():
        return info()

    @app.post("/api/setup/name")
    def setup_name(body: dict = Body(...)):
        if not FB.is_first_boot(st.data_dir, st.store):
            raise HTTPException(409, "This Pi already has a name. Rename it in Settings.")
        try:
            name = FB.clean_name(body.get("name"))
        except ValueError as e:
            raise HTTPException(422, str(e))
        if name in {t.lower() for t in FB.taken_names(st.peers.list())}:
            raise HTTPException(409, f"Another Pi on this network is already called {name}. Choose a different name.")
        old = socket.gethostname().split(".")[0]
        try:
            res = ncl.call("set_name", {"name": name, "old_name": old}, 40)
        except ncl.NetClientError as e:
            raise HTTPException(e.status, e.message)
        if not res.get("ok"):
            raise HTTPException(422 if res.get("code") in ("bad_name", "bad_ssid") else 503, res.get("message") or "The Pi could not be renamed.")
        st.me.set_name(name)
        st.discovery.rename()
        FB.finish(st.data_dir)
        srv = getattr(st, "fb_redirect", None)
        if srv:                                                      # the port-80 "open the setup page" helper is only for the unnamed Pi
            st.fb_redirect = None
            threading.Thread(target=srv.shutdown, daemon=True).start()
        ssid = res.get("ssid") or ap_ssid() or name
        return {"ok": True, "name": name, "ssid": ssid, "password": netops.DEFAULT_PSK, "ap_renamed": bool(res.get("ap_renamed")),
                "urls": {"local": f"http://{name}.local:{st.port}", "ap": f"http://{netops.AP_GATEWAY}:{st.port}"},
                "ips": [f"http://{ip}:{st.port}" for ip in st.me.ips() if not ip.startswith("10.42.0.")]}
