"""Routing self-check (docs/SPEC.md 15): Wi-Fi is the management/internet path, Ethernet ports are the test (data) path.
Internet / Claude traffic must never leave through a test port, or it skews jitter and latency."""
from __future__ import annotations

import json
import os
import subprocess
from typing import Dict, List, Optional

PROBE4, PROBE6 = "1.1.1.1", "2606:4700:4700::1111"


def _ip(*a: str, timeout: float = 4) -> Optional[object]:
    try:
        r = subprocess.run(["ip", "-j", *a], capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def iface_role(name: str) -> str:
    """'mgmt' (Wi-Fi), 'test' (Ethernet data ports, bridge/tap), 'other' (loopback, docker, VPN...)."""
    base = f"/sys/class/net/{name}"
    if name == "lo":
        return "other"
    if os.path.isdir(f"{base}/wireless") or os.path.isdir(f"{base}/phy80211") or name.startswith(("wl", "wlan")):
        return "mgmt"
    if name.startswith(("docker", "veth", "br-", "tun", "tap", "tail", "wg", "virbr", "zt")) and name != "ttbr0":
        return "other"
    if name.startswith(("eth", "en", "ttbr")) or os.path.isdir(f"{base}/device"):
        return "test"
    return "other"


def _default_routes(family: str) -> List[dict]:
    d = _ip(*(["-6"] if family == "6" else []), "route", "show", "default") or []
    return [{"dev": r.get("dev"), "gateway": r.get("gateway"), "metric": r.get("metric", 0)} for r in d]


def _route_get(target: str, v6: bool = False) -> Optional[str]:
    d = _ip(*(["-6"] if v6 else []), "route", "get", target)
    return d[0].get("dev") if d else None


def peers_from_profiles(profiles: List[dict]) -> List[str]:
    out: List[str] = []
    for p in profiles or []:
        for h in ((p.get("net") or {}).get("peer_host"),):
            if h and h not in out:
                out.append(h)
    return out


def check(peers: Optional[List[str]] = None) -> dict:
    """Plain-language routing check. status: ok | warn | bad."""
    checks: List[dict] = []

    def add(state: str, title: str, text: str, fix: str = "") -> None:
        checks.append({"s": state, "title": title, "text": text, "fix": fix})

    res = {"status": "ok", "checks": checks, "default_routes": {"v4": _default_routes("4"), "v6": _default_routes("6")}, "internet": {}, "peers": []}
    bad_default = []
    for fam in ("v4", "v6"):
        for r in res["default_routes"][fam]:
            if r["dev"] and iface_role(r["dev"]) == "test":
                bad_default.append(f"{r['dev']} (IP{fam})")
    for fam, tgt, v6 in (("IPv4", PROBE4, False), ("IPv6", PROBE6, True)):
        dev = _route_get(tgt, v6)
        res["internet"][fam.lower()] = dev
        if dev is None:
            continue
        role = iface_role(dev)
        if role == "test":
            add("bad", f"{fam} internet traffic would use a test port", f"The route to the internet ({tgt}) leaves through {dev}, which is a test (Ethernet) port. Claude and updates would share the test path and distort delay and jitter.",
                "Run: sudo scripts/net-split.sh --apply   (sets 'never default' on the Ethernet ports)")
        elif role == "mgmt":
            add("ok", f"{fam} internet goes out over Wi-Fi", f"Route to {tgt}: {dev} (management path), as intended.")
        else:
            add("warn", f"{fam} internet goes out over {dev}", f"{dev} is neither Wi-Fi nor an Ethernet test port; make sure it is not used for tests.")
    if not any(res["internet"].values()):
        add("ok", "No internet route", "There is no default route, so nothing leaves the Pi towards the internet. The app works fully offline; Ask Claude and updates need Wi-Fi internet.")
    if bad_default and not any(c["s"] == "bad" for c in checks):
        add("bad", "A default route is set on a test port", f"Default route via {', '.join(bad_default)}.", "Run: sudo scripts/net-split.sh --apply")
    for peer in peers or []:
        dev = _route_get(peer, ":" in peer)
        role = iface_role(dev) if dev else None
        res["peers"].append({"peer": peer, "dev": dev, "role": role})
        if dev is None:
            add("warn", f"No route to test peer {peer}", "The Pi has no route to this address. Check the cable and the address.")
        elif role == "mgmt":
            add("warn", f"Test peer {peer} is reached over Wi-Fi", f"Traffic to {peer} leaves through {dev}. Tests between the two Pis should use the Ethernet test ports.")
        else:
            add("ok", f"Test peer {peer} via {dev}", f"Test traffic to {peer} uses {dev}.")
    res["status"] = "bad" if any(c["s"] == "bad" for c in checks) else "warn" if any(c["s"] == "warn" for c in checks) else "ok"
    res["management"] = sorted({d for fam in ("v4", "v6") for r in res["default_routes"][fam] if (d := r["dev"]) and iface_role(d) == "mgmt"})
    res["test_ports"] = sorted(n for n in os.listdir("/sys/class/net") if iface_role(n) == "test") if os.path.isdir("/sys/class/net") else []
    return res


if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser(description="Routing self-check: internet over Wi-Fi, test traffic over Ethernet")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--peer", action="append", default=[])
    a = ap.parse_args()
    r = check(a.peer)
    if a.json:
        print(json.dumps(r, indent=1))
    else:
        print({"ok": "OK", "warn": "WARNING", "bad": "PROBLEM"}[r["status"]])
        for c in r["checks"]:
            print(f"  [{c['s']}] {c['title']}: {c['text']}" + (f"  -> {c['fix']}" if c["fix"] else ""))
    sys.exit({"ok": 0, "warn": 1, "bad": 2}[r["status"]])
