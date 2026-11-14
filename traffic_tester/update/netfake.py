"""A fake `nmcli` (and a recording stand-in for every other command) for tests and containers. Standard library only.
It keeps NetworkManager's profiles, the active connections, the Wi-Fi radio switch and a canned scan list in ONE JSON file, so the very same
operations can run in-process (TT_NET_FAKE=1), through a shim script in a Docker container (`python3 netfake.py <nmcli arguments>` with
TT_FAKE_NM_STATE=<file>), or in a unit test. It is NOT a full NetworkManager: it understands exactly the commands netops.py issues.

Test hooks stored in the same file:  "fail": {"<profile name>": "message"} makes `connection up` of that profile fail;
"scan": [{"ssid","signal","security","chan"}]; "calls": every non-nmcli command that was run (systemctl, chronyc ...)."""
from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Dict, List, Optional, Tuple


def _blank() -> dict:
    return {"profiles": [], "active": [], "radio": "enabled", "scan": [], "fail": {}, "calls": [], "nmcli_calls": [], "seq": 0, "running": True}


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:")


TYPES = {"wifi": "802-11-wireless", "ethernet": "802-3-ethernet", "vlan": "vlan"}


class FakeNM:
    def __init__(self, path: str):
        self.path = path

    # ------------------------------------------------------------ storage
    def load(self) -> dict:
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        b = _blank()
        b.update(d)
        return b

    def save(self, d: dict) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + f".{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def update(self, **kw) -> None:
        d = self.load()
        d.update(kw)
        self.save(d)

    def profiles(self) -> List[dict]:
        return self.load()["profiles"]

    # ------------------------------------------------------------ commands
    def run(self, args: List[str]) -> Tuple[int, str, str]:
        d = self.load()
        d["nmcli_calls"].append(list(args))
        d["nmcli_calls"] = d["nmcli_calls"][-400:]
        a = list(args)
        fields = ""
        while a and a[0].startswith("-"):                              # global options: -t, -g <fields>, -f <fields>, --wait <n>
            o = a.pop(0)
            if o in ("-f", "--fields", "-g", "--get-values", "--wait", "-w") and a:
                v = a.pop(0)
                if o not in ("--wait", "-w"):
                    fields = v
        try:
            rc, out, err = self._dispatch(d, a, fields)
        except _Fail as e:
            rc, out, err = e.rc, "", e.msg
        self.save(d)
        return rc, out, err

    def _dispatch(self, d: dict, a: List[str], fields: str) -> Tuple[int, str, str]:
        if not a:
            return 2, "", "Error: missing arguments"
        if a[0] in ("general", "g"):
            return 0, ("running\n" if d["running"] else ""), ""
        if a[0] in ("radio", "r"):
            if a[1:2] == ["wifi"]:
                if len(a) == 2:
                    return 0, d["radio"] + "\n", ""
                if a[2] not in ("on", "off"):
                    return 2, "", "Error: bad radio value"
                d["radio"] = "enabled" if a[2] == "on" else "disabled"
                if a[2] == "off":
                    d["active"] = [x for x in d["active"] if x["type"] != "wifi"]
                return 0, "", ""
            return 2, "", "Error: unsupported radio command"
        if a[0] in ("device", "d"):
            return self._device(d, a[1:], fields)
        if a[0] in ("connection", "c", "con"):
            return self._connection(d, a[1:], fields)
        return 2, "", f"Error: fake nmcli does not know '{a[0]}'"

    # ------------------------------------------------------------ device
    def _device(self, d: dict, a: List[str], fields: str):
        if not a:
            return 2, "", "Error: missing device command"
        if a[0] == "status":
            lines = []
            devs: Dict[str, dict] = {}
            for x in d["active"]:
                devs[x["device"]] = x
            for name in sorted(devs):
                x = devs[name]
                lines.append(":".join(_esc(v) for v in (name, x["type"], "connected", x["name"])))
            return 0, "\n".join(lines) + ("\n" if lines else ""), ""
        if a[0] == "wifi":
            if a[1:2] == ["rescan"]:
                return 0, "", ""
            if d["radio"] != "enabled":
                return 1, "", "Error: Wi-Fi is disabled."
            if any(x["type"] == "wifi" and (x.get("mode") == "ap") for x in d["active"]):
                return 1, "", "Error: Scanning not allowed while Wi-Fi is in access point mode."
            rows = []
            for s in d["scan"]:
                rows.append(":".join(_esc(str(s.get(k, ""))) for k in ("ssid", "signal", "security", "chan")))
            return 0, "\n".join(rows) + ("\n" if rows else ""), ""
        if a[0] == "connect" and len(a) >= 2:
            dev = a[1]
            best = None
            for p in d["profiles"]:
                if p["props"].get("connection.interface-name") not in (dev, None, ""):
                    continue
                if p["props"].get("connection.autoconnect", "yes") == "no":
                    continue
                if dev.startswith("wl") and p["type"] != "wifi" or (not dev.startswith("wl") and p["type"] == "wifi"):
                    continue
                pr = int(p["props"].get("connection.autoconnect-priority", "0"))
                if best is None or pr > best[0]:
                    best = (pr, p)
            if not best:
                return 10, "", f"Error: Failed to add/activate new connection: Device '{dev}' has no suitable connection."
            return self._up(d, best[1]["name"], dev)
        return 2, "", f"Error: unsupported device command {a[0]}"

    # ------------------------------------------------------------ connection
    def _find(self, d: dict, name: str) -> Optional[dict]:
        for p in d["profiles"]:
            if p["name"] == name:
                return p
        return None

    def _connection(self, d: dict, a: List[str], fields: str):
        if not a:
            return 2, "", "Error: missing connection command"
        cmd = a[0]
        if cmd in ("show", "s"):
            rest = a[1:]
            active_only = "--active" in rest
            rest = [x for x in rest if x != "--active"]
            if rest and rest[0] in ("id", "uuid"):
                p = self._find(d, rest[1]) if rest[0] == "id" else next((x for x in d["profiles"] if x["uuid"] == rest[1]), None)
                if not p:
                    return 10, "", f"Error: {rest[1]} - no such connection profile."
                want = [f for f in fields.split(",") if f] or sorted(p["props"])
                return 0, "\n".join(p["props"].get(f, "") for f in want) + "\n", ""
            cols = [c for c in (fields or "NAME,UUID,TYPE,DEVICE").split(",") if c]
            rows = []
            act = {x["name"]: x for x in d["active"]}
            for p in d["profiles"]:
                if active_only and p["name"] not in act:
                    continue
                dev = act[p["name"]]["device"] if p["name"] in act else ""
                vals = {"NAME": p["name"], "UUID": p["uuid"], "TYPE": TYPES.get(p["type"], p["type"]), "DEVICE": dev, "ACTIVE": "yes" if p["name"] in act else "no"}
                rows.append(":".join(_esc(vals.get(c, "")) for c in cols))
            return 0, "\n".join(rows) + ("\n" if rows else ""), ""
        if cmd in ("add", "a"):
            props, typ, ifname, name = {}, None, "", None
            kv = a[1:]
            i = 0
            while i < len(kv):
                k = kv[i]
                if i + 1 >= len(kv):
                    return 2, "", f"Error: value for '{k}' is missing."
                v = kv[i + 1]
                i += 2
                if k == "type":
                    typ = v
                elif k == "ifname":
                    ifname = v
                elif k == "con-name":
                    name = v
                elif k == "dev":
                    props["vlan.parent"] = v
                elif k == "id":
                    props["vlan.id"] = v
                else:
                    props[k] = v
            if typ not in TYPES or not name:
                return 2, "", "Error: type and con-name are required."
            if ifname:
                props["connection.interface-name"] = ifname
            props.setdefault("connection.autoconnect", "yes")
            props.setdefault("connection.autoconnect-priority", "0")
            if typ == "wifi":
                props.setdefault("802-11-wireless.mode", "infrastructure")
            d["seq"] += 1
            d["profiles"].append({"name": name, "uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{name}-{d['seq']}")), "type": typ, "props": props})
            return 0, f"Connection '{name}' successfully added.\n", ""
        if cmd in ("delete", "d"):
            if a[1:2] != ["id"] or len(a) < 3:
                return 2, "", "Error: usage: connection delete id <name>"
            n = len(d["profiles"])
            d["profiles"] = [p for p in d["profiles"] if p["name"] != a[2]]
            d["active"] = [x for x in d["active"] if x["name"] != a[2]]
            if len(d["profiles"]) == n:
                return 10, "", f"Error: unknown connection '{a[2]}'."
            return 0, f"Connection '{a[2]}' successfully deleted.\n", ""
        if cmd in ("modify", "m"):
            if a[1:2] != ["id"] or len(a) < 3:
                return 2, "", "Error: usage: connection modify id <name> <prop> <val>..."
            p = self._find(d, a[2])
            if not p:
                return 10, "", f"Error: unknown connection '{a[2]}'."
            kv = a[3:]
            for i in range(0, len(kv) - 1, 2):
                p["props"][kv[i]] = kv[i + 1]
            return 0, "", ""
        if cmd in ("up", "u"):
            if a[1:2] != ["id"] or len(a) < 3:
                return 2, "", "Error: usage: connection up id <name>"
            dev = a[a.index("ifname") + 1] if "ifname" in a[3:] and a.index("ifname") + 1 < len(a) else None
            return self._up(d, a[2], dev)
        if cmd == "down":
            if a[1:2] != ["id"] or len(a) < 3:
                return 2, "", "Error: usage: connection down id <name>"
            before = len(d["active"])
            d["active"] = [x for x in d["active"] if x["name"] != a[2]]
            if len(d["active"]) == before:
                return 10, "", f"Error: '{a[2]}' is not an active connection."
            return 0, f"Connection '{a[2]}' successfully deactivated.\n", ""
        return 2, "", f"Error: unsupported connection command {cmd}"

    def _up(self, d: dict, name: str, dev: Optional[str]):
        p = self._find(d, name)
        if not p:
            return 10, "", f"Error: unknown connection '{name}'."
        if name in d["fail"]:
            return 4, "", f"Error: Connection activation failed: {d['fail'][name]}"
        if p["type"] == "wifi" and d["radio"] != "enabled":
            return 4, "", "Error: Connection activation failed: the Wi-Fi radio is switched off."
        device = dev or p["props"].get("connection.interface-name") or ""
        if not device:
            return 4, "", "Error: Connection activation failed: no device."
        d["active"] = [x for x in d["active"] if x["device"] != device and x["name"] != name]
        d["active"].append({"name": name, "uuid": p["uuid"], "device": device, "type": p["type"], "mode": p["props"].get("802-11-wireless.mode", "")})
        return 0, f"Connection successfully activated (D-Bus active path: /org/freedesktop/NetworkManager/ActiveConnection/{len(d['active'])})\n", ""


class _Fail(Exception):
    def __init__(self, rc: int, msg: str):
        super().__init__(msg)
        self.rc, self.msg = rc, msg


def record_call(path: str, argv: List[str]) -> None:
    fm = FakeNM(path)
    d = fm.load()
    d["calls"].append(list(argv))
    d["calls"] = d["calls"][-400:]
    fm.save(d)


def main(argv: List[str]) -> int:
    path = os.environ.get("TT_FAKE_NM_STATE")
    if not path:
        print("TT_FAKE_NM_STATE is not set", file=sys.stderr)
        return 2
    rc, out, err = FakeNM(path).run(argv)
    sys.stdout.write(out)
    sys.stderr.write(err + ("\n" if err and not err.endswith("\n") else ""))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
