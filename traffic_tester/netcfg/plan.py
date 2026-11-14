"""A dry run of a network change, in plain words: what will happen, what could go wrong, and whether it touches the connection you are using."""
from __future__ import annotations

from typing import List, Optional

from ..update import netops

LABEL_WORDS = {"internet": "Internet", "test": "Test", "both": "Internet + Test"}
REVERT_SECONDS = netops.REVERT_DEFAULT


def _ports_by_name(config: dict) -> dict:
    return {p["iface"]: p for p in config.get("ports", [])}


def wifi_changed(w: dict, cur: dict, raw_wifi: Optional[dict]) -> bool:
    raw_wifi = raw_wifi or {}
    if w["mode"] != cur.get("mode"):
        return True
    if w["mode"] == "off":
        return False
    if w["mode"] == "ap":
        a = cur.get("ap") or {}
        return bool(raw_wifi.get("password")) or w["ssid"] != a.get("ssid") or w["band"] != a.get("band") or w["channel"] != a.get("channel")
    return bool(raw_wifi.get("password")) or w["ssid"] != cur.get("joined")


def port_changed(spec: dict, cur: Optional[dict]) -> bool:
    if not cur or not cur.get("saved"):
        return True
    return any(spec.get(k) != cur.get(k) for k in ("method", "address", "gateway", "vlan", "label")) or (spec.get("dns") or []) != (cur.get("dns") or [])


def port_words(spec: dict) -> str:
    if spec["method"] == "static":
        how = f"a fixed address {spec['address']}" + (f", gateway {spec['gateway']}" if spec.get("gateway") else ", no gateway") + (f", DNS {', '.join(spec['dns'])}" if spec.get("dns") else "")
    else:
        how = "an automatic address (DHCP)"
    return f"{spec['iface']}: {how}" + (f" on VLAN {spec['vlan']}" if spec.get("vlan") else "") + f", labelled {LABEL_WORDS[spec['label']]}."


def build_plan(norm: dict, config: dict, raw_changes: Optional[dict] = None) -> dict:
    raw_changes = raw_changes or {}
    through = config.get("connected_through") or {}
    dev = through.get("iface")
    base = dev.split(".")[0] if dev else None
    known = bool(dev)
    wifi_now = config.get("wifi") or {}
    ports_now = _ports_by_name(config)
    steps: List[str] = []
    warnings: List[str] = []
    reasons: List[str] = []
    summary: List[str] = []
    w = norm.get("wifi")
    wifi_touched = False
    if w and wifi_changed(w, wifi_now, raw_changes.get("wifi")):
        wifi_touched = True
        if w["mode"] == "ap":
            line = (f"Wi-Fi: the Pi becomes an access point named '{w['ssid']}' (WPA2, {'channel ' + str(w['channel']) if w['channel'] else 'automatic channel'}, "
                    f"{'5' if w['band'] == 'a' else '2.4'} GHz). Phones and laptops join it and open {config.get('ap_url', 'http://10.42.0.1:8080')}.")
            if wifi_now.get("mode") == "client":
                line += f" It leaves '{wifi_now.get('joined') or wifi_now.get('ssid') or 'its Wi-Fi network'}'."
            if w["psk"] == netops.DEFAULT_PSK:
                warnings.append("The access point password is the factory default ('password'). Anyone in range can join; choose your own.")
            summary.append(f"Wi-Fi: access point '{w['ssid']}'")
        elif w["mode"] == "client":
            line = f"Wi-Fi: the Pi joins '{w['ssid']}' and takes its address from that network. It stops being an access point, so phones can no longer join it directly."
            summary.append(f"Wi-Fi: join '{w['ssid']}'")
        else:
            line = "Wi-Fi: switched off. The Pi can then only be reached over an Ethernet cable."
            summary.append("Wi-Fi: off")
            if not any(p.get("link") for p in config.get("ports", [])):
                warnings.append("No Ethernet cable has a link right now. With Wi-Fi off the Pi cannot be reached at all until you plug one in (the emergency access point starts after 3 minutes without any network).")
        steps.append(line)
        if through.get("kind") == "wifi":
            reasons.append(f"You are connected through Wi-Fi ({dev}). Changing the Wi-Fi will disconnect this page; reconnect to the new network and press Keep.")
    touched_ports = []
    for iface, spec in (norm.get("ports") or {}).items():
        cur = ports_now.get(iface)
        if not port_changed(spec, cur):
            continue
        touched_ports.append(iface)
        steps.append(port_words(spec))
        summary.append(f"{iface}: {'DHCP' if spec['method'] == 'dhcp' else spec['address']}, {LABEL_WORDS[spec['label']]}")
        if base == iface:
            reasons.append(f"You are connected through {dev}. Changing it may disconnect this page; reconnect on the new address and press Keep.")
        if spec["label"] == "test" and spec["method"] == "dhcp":
            warnings.append(f"{iface} is labelled Test: it will never be used as the Pi's way to the internet.")
        if spec["method"] == "static" and spec["label"] != "test" and not spec.get("gateway"):
            warnings.append(f"{iface} is labelled Internet but has no gateway, so it cannot reach the internet.")
    if touched_ports:
        after = dict(ports_now)
        for i in touched_ports:
            after[i] = {**(after.get(i) or {}), **norm["ports"][i], "link": (after.get(i) or {}).get("link", True)}
        wifi_net = (w["mode"] if w else wifi_now.get("mode")) == "client"
        if not wifi_net and not any(p.get("label") in ("internet", "both") for p in after.values()):
            warnings.append("After this no port is labelled Internet, so the Pi cannot reach the internet (software updates, Ask Claude). Label one port Internet or Internet + Test.")
    if norm.get("fallback") is not None:
        steps.append("Emergency access point: " + ("on. If the Pi has no network at all for 3 minutes it starts its own Wi-Fi." if norm["fallback"] else "off. The Pi will not start its own Wi-Fi when it loses the network."))
    affects = bool(reasons)
    if not known and (wifi_touched or touched_ports):
        warnings.append("The Pi cannot tell which port you are using (you may be on the Pi itself or behind a proxy), so the change will be undone after "
                        f"{REVERT_SECONDS} seconds unless you press Keep.")
    auto = (affects or not known) and (wifi_touched or bool(touched_ports))
    return {"steps": steps, "warnings": warnings, "reasons": reasons, "affects_current_connection": affects, "needs_confirm": affects, "will_auto_revert": auto,
            "revert_seconds": REVERT_SECONDS, "summary": summary[:10], "nothing_to_do": not steps, "connected_through": through or None}
