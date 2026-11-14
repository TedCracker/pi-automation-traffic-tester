#!/usr/bin/env python3
"""Sniff GOOSE frames (L2 or L3) on an interface for N seconds; print JSON lines. Needs root (raw socket). Used by e2e tests."""
import json, os, socket, struct, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from traffic_tester import goose as G
iface, secs = sys.argv[1], float(sys.argv[2])
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)); s.bind((iface, 3)); s.settimeout(0.2)
end, out = time.monotonic() + secs, []
while time.monotonic() < end:
    try: d, a = s.recvfrom(2048)
    except socket.timeout: continue
    t = time.monotonic()
    env = G.l2_parse(d)
    if env is None and len(d) > 42 and d[12:14] == b"\x08\x00" and d[23] == 17:          # IPv4/UDP -> R-GOOSE
        ihl = (d[14] & 15) * 4; e3 = G.l3_parse(d[14 + ihl + 8:])
        if e3: env = {**e3, "vlan": None, "prio": None, "dst": d[30:34], "size": len(d)}
    if env is None: continue
    p = G.pdu_decode(env["pdu"])
    out.append({"t": t, "appid": env["appid"], "st": p["st"], "sq": p["sq"], "tal_ms": p["tal_ms"], "size": env["size"], "vlan": env["vlan"], "prio": env["prio"], "gocb": p["gocb"], "out": a[2] == 4})
print(json.dumps(out))
