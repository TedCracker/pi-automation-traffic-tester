"""Shared fixtures for the network tests: a sandbox with a fake /sys/class/net, a fake NetworkManager (JSON file), a fake scheduler and a fake clock."""
import os

from traffic_tester.update import netfake, netops


class Env:
    def __init__(self, tmp):
        self.tmp = str(tmp)
        self.sysfs = os.path.join(self.tmp, "sys")
        self.state = os.path.join(self.tmp, "state")
        self.data = os.path.join(self.tmp, "data")
        self.nm_path = os.path.join(self.tmp, "nm.json")
        for n in ("wlan0", "eth0", "eth1"):
            os.makedirs(os.path.join(self.sysfs, n), exist_ok=True)
        os.makedirs(os.path.join(self.sysfs, "wlan0", "wireless"), exist_ok=True)
        for n in ("eth0", "eth1"):
            for f, v in (("type", "1"), ("carrier", "1")):
                with open(os.path.join(self.sysfs, n, f), "w") as fh:
                    fh.write(v)
        self.nm = netfake.FakeNM(self.nm_path)
        self.sched = netops.FakeScheduler()
        self.t = [1_000_000.0]
        self.runner = netops.FakeRunner(self.nm_path)
        self.ctx = netops.Ctx(self.state, runner=self.runner, scheduler=self.sched, now=lambda: self.t[0], sysfs=self.sysfs,
                              chrony_conf=os.path.join(self.tmp, "chrony", "conf.d", "traffic-tester.conf"), chrony_main=os.path.join(self.tmp, "chrony", "chrony.conf"),
                              keyfile_dir=os.path.join(self.tmp, "nmkeys"), data_dir=self.data, lock_timeout=2.0, etc_dir=os.path.join(self.tmp, "etc"))
        self.ctx.probe = netops.FakeProbe(self.ctx)
        self.seed_home()

    def seed_home(self, with_ap=False):
        """An 'existing install': home Wi-Fi joined + a plain wired profile, radio on."""
        d = self.nm.load()
        d["profiles"] = [
            {"name": "HomeNet", "uuid": "11111111-0000-0000-0000-000000000001", "type": "wifi",
             "props": {"connection.interface-name": "wlan0", "connection.autoconnect": "yes", "connection.autoconnect-priority": "0", "802-11-wireless.ssid": "HomeNet", "802-11-wireless.mode": "infrastructure"}},
            {"name": "Wired connection 1", "uuid": "11111111-0000-0000-0000-000000000002", "type": "ethernet",
             "props": {"connection.interface-name": "eth0", "connection.autoconnect": "yes", "connection.autoconnect-priority": "0"}}]
        d["active"] = [{"name": "HomeNet", "uuid": "x", "device": "wlan0", "type": "wifi", "mode": "infrastructure"},
                       {"name": "Wired connection 1", "uuid": "y", "device": "eth0", "type": "ethernet", "mode": ""}]
        d["scan"] = [{"ssid": "HomeNet", "signal": 80, "security": "WPA2", "chan": 6}, {"ssid": "Cafe", "signal": 40, "security": "", "chan": 11}, {"ssid": "HomeNet", "signal": 30, "security": "WPA2", "chan": 1}]
        self.nm.save(d)

    def view(self):
        """Everything NetworkManager holds, without the uuids (they are fresh after a recreate)."""
        d = self.nm.load()
        profs = sorted((p["name"], p["type"], tuple(sorted(p["props"].items()))) for p in d["profiles"])
        act = sorted((a["name"], a["device"]) for a in d["active"])
        return {"profiles": profs, "active": act, "radio": d["radio"]}

    def files(self):
        out = {}
        for f in (netops.F_STATE, netops.F_PRIVATE):
            d = netops.read_json(os.path.join(self.state, f))
            if isinstance(d, dict):
                d = {k: v for k, v in d.items() if k not in ("last_event", "updated")}               # these change on purpose (the revert notes itself)
            out[f] = d
        return out

    def names(self):
        return sorted(p["name"] for p in self.nm.load()["profiles"])


def kv(argv):
    """The property/value pairs of an `nmcli connection add ...` argument list."""
    a = argv[argv.index("add") + 1:]
    return {a[i]: a[i + 1] for i in range(0, len(a), 2)}
