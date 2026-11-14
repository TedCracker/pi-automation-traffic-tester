#!/usr/bin/env python3
"""A tiny stand-in server for the Network card UI test (scripts/e2e_netcfg_ui.py): the real traffic_tester/static files + the real netcfg API (register(app, st)) on a FAKE NetworkManager
(fake sysfs, JSON profile store), and a loop that fires the revert the way the root watch loop does. Usage: netcfg_ui_harness.py <port> <tmpdir>"""
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
port, tmp = int(sys.argv[1]), sys.argv[2]
os.makedirs(tmp, exist_ok=True)

from netcfgfix import Env  # noqa: E402

env = Env(tempfile.mkdtemp(dir=tmp))
for k, v in (("TT_NET_FAKE", "1"), ("TT_NET_STATE_DIR", env.state), ("TT_NET_DATA_DIR", env.data), ("TT_NET_SYSFS", env.sysfs), ("TT_FAKE_NM_STATE", env.nm_path)):
    os.environ[k] = v

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from traffic_tester.netcfg import api as netapi, plan as planmod, state as statemod  # noqa: E402
from traffic_tester.update import netops  # noqa: E402

planmod.REVERT_SECONDS = 20                                                                   # a short countdown for the test
PORTS = {"eth0": {"carrier": True, "speed_mbps": 1000, "ip": "192.168.1.50"}, "eth1": {"carrier": False, "speed_mbps": None, "ip": None}}
statemod._live_ports = lambda: [{"name": n, "mac": "aa:bb", "wired": True, "usb": n == "eth1", "driver": "x", "is_bridge": False, **p} for n, p in PORTS.items()]
statemod._route_dev = lambda ip: "eth0" if ip else None                                      # the browser is "connected through eth0"

store = {}


class Store:
    def get_setting(self, k, d=None):
        return store.get(k, d)

    def set_setting(self, k, v):
        store[k] = v


app = FastAPI()
netapi.register(app, type("St", (), {"store": Store()})())


@app.get("/harness.html", response_class=HTMLResponse)
def harness():
    return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="stylesheet" href="style.css"><link rel="stylesheet" href="update.css"><link rel="stylesheet" href="network.css"></head>
<body><main id="main" style="padding:12px"><section class="card" id="setNet"></section></main><div id="toast" role="status" aria-live="polite"></div><div id="modal" class="modal" hidden></div>
<script type="module">import { renderNetworkCard } from './network.js'; renderNetworkCard(document.getElementById('setNet'));</script></body></html>"""


@app.get("/__ctl/profiles")
def ctl_profiles():
    return env.view()["profiles"] and {"names": env.names(), "active": env.view()["active"], "radio": env.view()["radio"]}


@app.post("/__ctl/carrier/{iface}/{up}")
def ctl_carrier(iface: str, up: int):
    PORTS[iface]["carrier"] = bool(up)
    PORTS[iface]["ip"] = "192.168.9.9" if up else None
    return {"ok": True}


def timer_loop():
    ctx = netops.make_ctx(env.state, data_dir=env.data)
    while True:
        time.sleep(1)
        try:
            with netops._Lock(ctx):
                netops.check_expired_revert(ctx)                                              # the same function the root watch loop uses when the systemd timer was lost
        except Exception:
            pass


threading.Thread(target=timer_loop, daemon=True).start()
app.mount("/", StaticFiles(directory=os.path.join(ROOT, "traffic_tester", "static"), html=True), name="static")
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
