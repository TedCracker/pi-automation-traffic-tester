"""Generator run orchestration: the Pi you are on (the controller) drives every Pi's generator node (its own directly, the others over HTTP), each stream
with its own sender and receiver, then judges the result (docs/SPEC.md 13)."""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse
from typing import Callable, Dict, List, Optional

from .. import orchestrate as OR, plan as PL, system
from ..peers import PeerAuthError, PeerClient, PeerError
from . import node as G
from . import proto as P


class RemoteError(Exception):
    pass


def api_host(peer_api: str) -> str:
    """Host part of the other Pi's web address ("http://10.0.0.5:8080/", "10.0.0.5:8080", "pi2.local") -> "10.0.0.5"."""
    from urllib.parse import urlparse
    a = (peer_api or "").strip()
    if not a:
        return ""
    return urlparse(a if "//" in a else "//" + a).hostname or ""




class RemoteNode:
    """Another Pi's generator node, over its normal web API (logs in with that Pi's password when it asks)."""

    def __init__(self, base: str = "", password: str = "", client: Optional[PeerClient] = None, label: str = ""):
        if client is None:
            base = base.strip().rstrip("/")
            if not base:
                raise RemoteError("Enter the other Pi's web address (for example http://192.168.1.51:8080).")
            u = urlparse(base if "//" in base else "//" + base)
            client = PeerClient(u.hostname or "", u.port or 8080, password, label=label or (u.hostname or ""))
        self.client = client
        self.base = client.base
        self.password = password

    def _call(self, method: str, path: str, body: Optional[dict] = None, timeout: float = 15):
        try:
            return self.client.call(method, path, body, timeout=timeout)
        except PeerAuthError as e:
            raise RemoteError(str(e))
        except PeerError as e:
            raise RemoteError(str(e))

    def status(self) -> dict:
        return self._call("GET", "/api/gen/node/status")

    def rx_start(self, streams: List[dict], iface: str) -> None:
        self._call("POST", "/api/gen/node/rx/start", {"streams": streams, "interface": iface})

    def tx_start(self, streams: List[dict], iface: str, start_in_s: float) -> None:
        self._call("POST", "/api/gen/node/tx/start", {"streams": streams, "interface": iface, "start_in_s": start_in_s})

    def live(self, since: int) -> dict:
        return self._call("GET", f"/api/gen/node/live?since={since}")

    def stop_senders(self) -> None:
        self._call("POST", "/api/gen/node/senders/stop", {}, timeout=10)

    def stop(self) -> dict:
        return self._call("POST", "/api/gen/node/stop", {}, timeout=30)


def local_status() -> dict:
    """What this Pi tells a controller about itself (the generator node status)."""
    ifs = {}
    for i in system.interfaces():
        ifs[i["name"]] = {"mac": i["mac"], "ip": G.iface_ipv4(i["name"]), "wired": i["wired"], "up": i["up"]}
    try:
        default_if = G._first_wired()
    except G.GenError:
        default_if = ""
    return {"interfaces": ifs, "default_iface": default_if, "clock": system.clock_state(0), "hardware": system.pi_model(),
            "caps": {"steps": True}}                 # what this Pi's generator node can do beyond the first version (a controller checks before it asks)


class _N:
    """One Pi taking part in a generator test."""

    def __init__(self, key: str, label: str, handle, node: Optional[PL.Node], iface: str = "auto"):
        self.key, self.label, self.handle, self.node, self.iface = key, label, handle, node, iface
        self.status: dict = {}
        self.tx: List[dict] = []
        self.rx: List[dict] = []
        self.since = 0

    @property
    def is_self(self) -> bool:
        return self.key == "self"


def _mbps(bytes_: int, dur: float) -> float:
    return round(bytes_ * 8 / max(dur, 1e-3) / 1e6, 3)


class GeneratorRun:
    LOST_POLLS = 5            # this many failed status calls in a row ...
    LOST_GRACE_S = 10.0       # ... and this long past the planned end: the other Pi is gone, the run ends with an error
    SETTLE_S = 1.0            # throughput in steps: wait this long after a step's senders end before it is judged

    def __init__(self, profile: dict, node: G.Node, emit: Callable[[dict], None] = lambda e: None,
                 on_interval: Callable[[dict], None] = lambda r: None, remote=None, services: Optional[OR.Services] = None):
        self.p, self.cfg = profile, profile["gen"]
        self.node, self.emit, self.on_interval = node, emit, on_interval
        self.remote = remote
        self.services = services or OR.Services()
        self.stop_evt = threading.Event()
        self.error: Optional[str] = None
        self.status = "created"
        self.total_s = 0.0
        self.nodes: Dict[str, _N] = {}
        self.streams: List[dict] = []
        self.sum_cfg: List[dict] = self.cfg["streams"]     # the settings each entry of self.streams was made from (the same list, unless the test runs in steps)
        self._mon_last: dict = {}
        # Throughput in steps (docs/SPEC.md 13.9)
        self.steps_cfg: dict = self.cfg.get("steps") or {}
        self.stepping = bool(self.steps_cfg.get("enabled"))
        self.phases: List[dict] = []
        self.step_rows: List[dict] = []                    # judged as each step ends (live); the stored result is worked out again from the final counts
        self.step_now: Optional[dict] = None
        self.top_mbps: Optional[float] = None
        self.flood_live: Optional[dict] = None
        self.fail_stop = False
        self.sent_s: Optional[float] = None

    def stop(self) -> None:
        self.stop_evt.set()

    # ------------------------------------------------------------ who takes part
    def _endpoint(self, s: dict, which: str) -> dict:
        """The Pi that sends (`src`) or receives (`dst`) a stream: its own setting, else taken from the test's From / To and the stream's direction."""
        ep = s.get(which) or {}
        named = lambda e: bool((e or {}).get("pi") or (e or {}).get("host"))
        if named(ep) or named(s.get("src")) or named(s.get("dst")):       # the stream names its own ends: an end left blank is this Pi
            return ep
        frm, to = dict(self.p.get("source") or {}), dict(self.p.get("dest") or {})
        if not (to.get("pi") or to.get("host")):            # older tests: the other Pi is named by its web address / data address
            to = {"host": api_host(self.cfg["peer_api"]) or self.p["net"]["peer_host"]}
            if not to["host"] and self.remote is None:
                raise G.GenError("Choose the Pi to send to (pick one from the list), or enter its web address (for example http://192.168.1.51:8080).")
        fwd = s.get("direction", "a_to_b") == "a_to_b"
        return (frm if (which == "src") == fwd else to)

    def _node(self, ep: dict, ctx: PL.Context, what: str) -> _N:
        cfg = self.cfg
        if self.remote is not None:                         # one other Pi, handed in (tests, older callers)
            is_self = not (ep.get("pi") or ep.get("host")) or ep.get("pi") == "self"
            return self.nodes["self"] if is_self else self.nodes["peer"]
        pn = PL.node_for(ep, ctx, what)
        if pn.kind == "self":
            return self.nodes["self"]
        key = pn.id or f"addr:{pn.host}"
        if key in self.nodes:
            return self.nodes[key]
        if pn.kind == "peer":
            handle = RemoteNode(client=self.services.client(pn))
        else:                                               # a typed address: its web address is the one in the settings, or http://<host>:8080
            base = cfg["peer_api"] if (cfg["peer_api"] and api_host(cfg["peer_api"]) == pn.host) else f"http://{pn.host}:8080"
            handle = RemoteNode(base, cfg["peer_password"], label=pn.host)
        n = _N(key, pn.label(), handle, pn, ep["iface"] if ep.get("iface") not in (None, "", "auto") else (cfg["peer_interface"] or "auto"))
        self.nodes[key] = n
        return n

    # ------------------------------------------------------------ setup
    def _plan(self) -> None:
        """Decide, for every stream, which node sends and which receives, and fill in addresses / MACs."""
        cfg = self.cfg
        svc = self.services
        me = svc.me()
        ctx = svc.context()
        self.nodes = {"self": _N("self", me["name"], self.node, PL.Node("self", id=me["id"], name=me["name"], host="127.0.0.1", ips=list(me.get("ips") or [])), "auto")}
        if self.remote is not None:
            self.nodes["peer"] = _N("peer", "the other Pi", self.remote, PL.Node("addr", name="the other Pi", host=api_host(cfg["peer_api"]) or self.p["net"]["peer_host"]),
                                    cfg["peer_interface"])
        ends = []
        for i, s in enumerate(cfg["streams"]):
            a = self._node(self._endpoint(s, "src"), ctx, "The Pi to send from")
            b = self._node(self._endpoint(s, "dst"), ctx, "The Pi to send to")
            if a is b:
                raise G.GenError(f"Stream '{s['name']}' sends from {a.label} to itself: pick two different Pis.")
            ends.append((a, b))
        # every Pi tells its address, MACs and clock (the starting check: is everyone there and idle?)
        for n in self.nodes.values():
            if n.is_self:
                if self.node.busy():
                    raise G.GenError("This Pi is already running a generator test.")
                n.status = local_status()
            else:
                n.status = n.handle.status()
                if n.status.get("busy"):
                    raise G.GenError(f"{n.label} is already running a test.")
        # the port each Pi uses for raw (Layer 2) streams; "auto" for a Pi with no raw stream
        for n in self.nodes.values():
            n.iface = cfg["interface"] if n.is_self else n.iface
        local_l2 = any(s["type"] == "l2" for s in cfg["streams"])
        for n in self.nodes.values():
            if n.is_self:
                n.iface = G.resolve_iface(n.iface) if (local_l2 or n.iface != "auto") else "auto"
            else:
                n.iface = n.iface if n.iface != "auto" else n.status.get("default_iface", "")
        self.streams = []
        for i, (s, (a, b)) in enumerate(zip(cfg["streams"], ends)):
            r = dict(s)
            r["id"] = i
            r["duration_s"] = s["duration_s"] or cfg["duration_s"]
            if s["packets"]:
                r["duration_s"] = None
            r["dst_port"] = s["dst_port"] or 20600 + i
            peer_if = b.status["interfaces"].get(b.iface or "", {}) if b.status.get("interfaces") else {}
            if s["type"] in ("udp", "tcp", "ip"):
                r["dst_ip"] = self._data_ip(a, b, ctx)
                if not r["dst_ip"]:
                    raise G.GenError("Enter the other Pi's address (its web address, or a data address) for the streams." if not b.is_self else
                                     "Streams to this Pi from another Pi need this Pi's data address: enter it in the generator settings.")
            else:
                r["dst_ip"] = ""
            if b.is_self:
                try:
                    my_mac = ":".join("%02x" % x for x in __import__("traffic_tester.gen.net", fromlist=["x"]).iface_mac(b.iface))
                except Exception:
                    my_mac = ""
                r["dst_mac"] = s["dst_mac"] or my_mac
            else:
                r["dst_mac"] = s["dst_mac"] or peer_if.get("mac", "")
            if s["type"] == "l2" and not r["dst_mac"]:
                raise G.GenError("Could not find the other Pi's MAC address for the raw Ethernet stream: enter it.")
            a.tx.append(r)
            b.rx.append(r)
            r["_src"], r["_dst"] = a.key, b.key
            self.streams.append(r)
        if self.stepping:
            self._plan_steps()

    # ------------------------------------------------------------ throughput in steps
    def _plan_steps(self) -> None:
        """Turn the planned streams into phases: an optional flood that measures the top rate, then one phase per step. Every phase has its
        own stream ids, so each step keeps its own exact counters on both Pis; every receiver listens from the start."""
        st = self.steps_cfg
        for n in self.nodes.values():
            if not n.is_self and not (n.status.get("caps") or {}).get("steps"):
                ver = n.status.get("version")
                raise G.GenError(f"{n.label} runs an older version of the app{(' (' + str(ver) + ')') if ver else ''} that cannot run Throughput in steps. "
                                 "Update it (Settings → Software update), or switch the method to Flood.")
        base = self.streams
        nb = len(base)
        fr = [float(f) for f in st["fractions"]]
        if any(s.get("packets") for s in self.cfg["streams"]):
            raise G.GenError("Throughput in steps runs every stream for the length of a step: clear \"Or stop after (packets)\", or switch the method to Flood.")
        mb = [stream_mbps(s) for s in self.cfg["streams"]]
        tot = sum(mb) or 1.0
        self.step_share = [m / tot for m in mb]
        self.top_mbps = float(st["top_mbps"]) if st.get("top_mbps") else None
        multi = nb > 1

        def mk(i: int, sid: int, name: str, dur: float, phase: str) -> dict:
            q = dict(base[i])
            q.update(id=sid, name=name, duration_s=float(dur), packets=None, start_offset_s=0.0, _phase=phase, _base=i)
            return q

        self.phases = []
        if self.top_mbps is None:                          # Auto: a short flood shows what the link carries
            fl = [mk(i, len(fr) * nb + i, "Flood" + (f" · {base[i]['name']}" if multi else ""), st["flood_s"], "flood") for i in range(nb)]
            for i, q in enumerate(fl):
                q["rate"] = {"pps": None, "mbps": max(0.001, float(st["flood_mbps"]) * self.step_share[i])}
            self.phases.append({"kind": "flood", "streams": fl})
        for k, f in enumerate(fr):
            self.phases.append({"kind": "step", "n": k + 1, "of": len(fr), "share": f,
                                "streams": [mk(i, k * nb + i, f"Step {k + 1}" + (f" · {base[i]['name']}" if multi else ""), st["step_s"], "step") for i in range(nb)]})
        self.streams = [q for ph in self.phases for q in ph["streams"]]
        self.sum_cfg = self.streams
        for n in self.nodes.values():                      # every receiver listens from the start; the senders are started step by step
            n.tx = []
            n.rx = [q for q in self.streams if q["_dst"] == n.key]

    def _steps_total_s(self) -> float:
        st = self.steps_cfg
        gap = 0.4 + self.SETTLE_S + 0.6                    # start delay, settling time, polling slack
        return (st["flood_s"] + gap if self.top_mbps is None else 0.0) + len(st["fractions"]) * (st["step_s"] + gap)

    def _set_rates(self, ph: dict) -> None:
        for q in ph["streams"]:
            q["rate"] = {"pps": None, "mbps": max(0.001, round(self.top_mbps * ph["share"] * self.step_share[q["_base"]], 6))}
        ph["asked_mbps"] = round(self.top_mbps * ph["share"], 4)

    def _step_ev(self) -> dict:
        """What the screen shows about the steps while the test runs (rides on every "gstate" event)."""
        return {"step": self.step_now, "steps": {"of": len(self.steps_cfg["fractions"]), "auto": not self.steps_cfg.get("top_mbps"), "top_mbps": self.top_mbps,
                                                 "step_s": self.steps_cfg["step_s"], "fractions": list(self.steps_cfg["fractions"]), "rows": list(self.step_rows), "flood": self.flood_live,
                                                 "limit": step_limit(self.cfg["rule"])}}

    def _poll(self, tries: int = 4) -> Dict[str, dict]:
        lives: Dict[str, dict] = {}
        for n in self.nodes.values():
            for k in range(tries):
                try:
                    lives[n.key] = self.node.live(0) if n.is_self else n.handle.live(n.since)
                    break
                except RemoteError:
                    if k == tries - 1:
                        raise G.GenError(f"Lost contact with {n.label} during the test.")
                    time.sleep(0.5)
        return lives

    def _phase(self, ph: dict, t0: float, valid_lat: bool, started: list) -> Optional[List[dict]]:
        """Send one phase and wait for it. -> what each of its streams sent and what arrived (None = Stop was pressed)."""
        by: Dict[str, List[dict]] = {}
        for q in ph["streams"]:
            by.setdefault(q["_src"], []).append(q)
        for n in sorted(self.nodes.values(), key=lambda n: not n.is_self):
            n.tx = by.get(n.key, [])
            if n.tx:
                if n not in started:
                    started.append(n)
                n.handle.tx_start(n.tx, n.iface, 0.4)
        self._monitor(t0, valid_lat)
        if self.stop_evt.is_set() or self.stop_evt.wait(self.SETTLE_S):       # packets still on their way arrive before the step is judged
            return None
        lives = self._phase_lives = self._poll()
        for n in self.nodes.values():
            if not n.is_self:
                n.since = lives[n.key]["n_rows"]
        errs = [e for lv in lives.values() for e in lv.get("errors", [])]
        if errs:
            raise G.GenError("; ".join(errs[:2]))
        key = delay_key(self.cfg["rule"])
        out = []
        for q in ph["streams"]:
            tx = lives[q["_src"]]["tx"].get(str(q["id"]))
            rx = lives[q["_dst"]]["rx"].get(str(q["id"]))
            if tx is None or rx is None:
                raise G.GenError(f"{self.nodes[q['_src'] if tx is None else q['_dst']].label} has no numbers for {q['name']}: the test cannot go on.")
            d_us = rx.get(key + "_us") if valid_lat else None
            out.append({"name": self.cfg["streams"][q["_base"]]["name"], "src": self.nodes[q["_src"]].label, "tx_pkts": tx["pkts"], "tx_bytes": tx["bytes"],
                        "rx_pkts": rx["unique"], "rx_bytes": rx["bytes"], "delay_ms": None if d_us is None else round(d_us / 1000.0, 3), "jitter_ms": None,
                        "dur": q["duration_s"]})
        return out

    def _run_steps(self, t0: float, valid_lat: bool, started: list) -> None:
        st, rule = self.steps_cfg, self.cfg["rule"]
        fails = 0
        for ph in self.phases:
            if self.stop_evt.is_set():
                break
            if ph["kind"] == "step":
                if fails >= st["stop_after_fail"]:
                    self.fail_stop = True                  # two failed steps in a row: higher rates would only burn data
                    break
                self._set_rates(ph)
                self.step_now = {"phase": "step", "n": ph["n"], "of": ph["of"], "mbps": ph["asked_mbps"], "text": f"Step {ph['n']} of {ph['of']} · {fmt_mbps(ph['asked_mbps'])} Mb/s"}
            else:
                self.step_now = {"phase": "flood", "n": 0, "of": len(st["fractions"]), "mbps": None, "text": f"Measuring the top rate ({st['flood_s']:g} s flood)"}
            ph["ran"] = True
            got = self._phase(ph, t0, valid_lat, started)
            if got is None:
                ph["stopped"] = True
                break
            if ph["kind"] == "flood":
                sent = sum(x["tx_bytes"] for x in got) * 8 / st["flood_s"] / 1e6
                mbps = sum(x["rx_bytes"] for x in got) * 8 / st["flood_s"] / 1e6
                if mbps <= 0:
                    raise G.GenError("Nothing arrived during the flood that measures the top rate, so there is no rate to step up to. "
                                     "Check the address, the cable and any firewall, or type a top rate instead of Auto.")
                self.top_mbps = float(f"{mbps:.3g}")
                self.flood_live = {"sent_mbps": round(sent, 3), "got_mbps": round(mbps, 3), "duration_s": st["flood_s"]}
            else:
                row = step_row(ph, got, rule, valid_lat)
                self.step_rows.append(row)
                fails = 0 if row["clean"] else fails + 1
            self._state_ev(self._phase_lives, time.monotonic() - t0, valid_lat)      # the screen gets the judged step at once
        self.step_now = None

    def _data_ip(self, a: _N, b: _N, ctx: PL.Context) -> str:
        cfg = self.cfg
        if b.node is not None and b.node.data_host:
            return b.node.data_host
        if b.node is not None and b.node.kind == "addr":
            return self.p["net"]["peer_host"] or b.node.host
        if b.is_self:
            if a.is_self:
                return ""
            if cfg["local_host"]:
                return cfg["local_host"]
            mine = G.iface_ipv4(b.iface) if b.iface and b.iface != "auto" else ""
            tester = a.node
            return PL.data_address(tester, b.node, ctx) or mine or ""
        return PL.data_address(a.node, b.node, ctx)

    def _clock_state(self) -> dict:
        maxo = self.p["max_clock_offset_ms"]
        per: Dict[str, dict] = {}
        for n in self.nodes.values():
            c = n.status.get("clock") or {}
            per[n.key] = {"name": n.label, "synced": bool(c.get("synced")), "offset_ms": c.get("offset_ms"),
                          "ok": bool(c.get("synced")) and c.get("offset_ms") is not None and abs(c["offset_ms"]) <= maxo, "clock": c,
                          "ips": list(n.node.ips) if n.node else []}
        pairs: Dict[str, bool] = {}
        notes: Dict[str, str] = {}
        for r in self.streams:
            a, b = per[r["_src"]], per[r["_dst"]]
            ok = a["ok"] and b["ok"]
            note = ""
            if ok:
                agree, note = OR.clock_agree(a["clock"], b["clock"], a["ips"], b["ips"])
                ok = agree is not False
            pairs[f"{r['_src']}>{r['_dst']}"] = ok
            if note:
                notes[f"{r['_src']}>{r['_dst']}"] = note
        a = per.get("self", {})
        other = next((v for k, v in per.items() if k != "self"), {})
        return {"valid": bool(pairs) and all(pairs.values()), "a_offset_ms": a.get("offset_ms"), "b_offset_ms": other.get("offset_ms"), "limit_ms": maxo,
                "a_synced": bool(a.get("synced")), "b_synced": bool(other.get("synced")), "pairs": pairs, "notes": notes,
                "nodes": {k: {"name": v["name"], "synced": v["synced"], "offset_ms": v["offset_ms"]} for k, v in per.items()}}

    # ------------------------------------------------------------ run
    def run(self) -> dict:
        cfg = self.cfg
        results: Dict[str, dict] = {}
        clocks: dict = {}
        started: List[_N] = []
        try:
            if self.remote is None and not (cfg["streams"] and any(1 for _ in cfg["streams"])):
                raise G.GenError("Add at least one stream.")
            self._plan()
            clocks = self._clock_state()
            # a packet-count stream has no duration: it takes packets / rate
            if self.stepping:
                self.total_s = self._steps_total_s()
            else:
                self.total_s = max([(s["start_offset_s"] + (s["duration_s"] or s["packets"] / G.stream_pps(s)[1])) for s in self.streams] or [0])
            order = sorted(self.nodes.values(), key=lambda n: n.is_self)       # other Pis first, then this one: receivers first, then senders
            # receivers first (every Pi), then senders: nothing is sent before everything is listening
            for n in order:
                if n.rx:
                    started.append(n)                        # added before the call: if anything fails from here on, that Pi is stopped
                    n.handle.rx_start(n.rx, n.iface)
            for n in sorted(self.nodes.values(), key=lambda n: not n.is_self):
                if n.tx:
                    if n not in started:
                        started.append(n)
                    n.handle.tx_start(n.tx, n.iface, 0.4)
            self.status = "running"
            t0 = time.monotonic()
            self.emit({"ev": "started", "total_s": self.total_s, "transport": "traffic generator", "streams": len(cfg["streams"]), "one_way": clocks["valid"],
                       "peer": {"clock": (self.nodes.get("peer") or next((n for n in self.nodes.values() if not n.is_self), None) or _N("", "", None, None)).status.get("clock")},
                       **({"steps": self._step_ev()["steps"]} if self.stepping else {})})
            if self.stepping:
                self._run_steps(t0, clocks["valid"], started)
            else:
                self._monitor(t0, clocks["valid"])
            self.sent_s = time.monotonic() - t0              # how long the test really ran (a Stop ends it before total_s)
            for n in self.nodes.values():                    # user Stop: senders end first everywhere, or the packets they still send count as lost
                try:
                    if n.is_self:
                        self.node.stop_senders()
                    elif hasattr(n.handle, "stop_senders"):
                        n.handle.stop_senders()
                except Exception:
                    pass
            time.sleep(1.2)                                 # let in-flight packets arrive
            for n in sorted(self.nodes.values(), key=lambda n: n.is_self):
                results[n.key] = n.handle.stop()             # other Pis stop their senders, then their receivers; this Pi last
                if self.stop_evt.is_set() and not n.is_self:
                    time.sleep(0.1)
            started = []                                    # every Pi is stopped: nothing left for the clean-up below
            self.status = "stopped" if self.stop_evt.is_set() else "finished"
        except (G.GenError, RemoteError) as e:
            self.error, self.status = str(e), "failed"
        except Exception as e:  # noqa
            self.error, self.status = f"{type(e).__name__}: {e}", "failed"
        finally:
            for n in started:                               # a failed start: free every Pi that was already told to listen or send
                try:
                    results.setdefault(n.key, n.handle.stop())
                except Exception:
                    pass
        complete = bool(results) and all(n.key in results for n in self.nodes.values())
        summary = self._summarize(results, clocks) if complete else {"error": self.error, "kind": "generator"}
        if self.error:
            summary["error"] = self.error
        self.emit({"ev": "done", "status": self.status, "error": self.error, "summary": summary if "verdict" in summary else None})
        return summary

    def _monitor(self, t0: float, valid_lat: bool = True) -> None:
        misses = {n.key: 0 for n in self.nodes.values()}
        while not self.stop_evt.is_set():
            time.sleep(0.5)
            now = time.monotonic() - t0
            lives: Dict[str, dict] = {}
            for n in self.nodes.values():
                if n.is_self:
                    lives[n.key] = self.node.live(0)
                    continue
                try:
                    lives[n.key] = n.handle.live(n.since)
                    misses[n.key] = 0
                except RemoteError:
                    misses[n.key] += 1                         # a short outage of the link is part of the test; a Pi that stays away is not
                    if misses[n.key] >= self.LOST_POLLS and now > self.total_s + self.LOST_GRACE_S:
                        raise G.GenError(f"Lost contact with {n.label} during the test.")
                    lives[n.key] = {"rows": [], "n_rows": n.since, "done": False, "tx": {}, "rx": {}, "errors": []}
                n.since = lives[n.key]["n_rows"]
            errs = [e for lv in lives.values() for e in lv.get("errors", [])]
            if errs:
                raise G.GenError("; ".join(errs[:2]))
            self._state_ev(lives, now, valid_lat)
            if all(((not n.tx) or lives[n.key].get("done")) for n in self.nodes.values()) and now > 1.0:
                break

    def _state_ev(self, lives: Dict[str, dict], now: float, valid_lat: bool) -> None:
        """One "gstate" event for the screen from what every Pi reports."""
        last = self._mon_last
        vals = list(lives.values())
        tx_n = sum(v["pkts"] for lv in vals for v in lv["tx"].values())
        rx_n = sum(v["unique"] for lv in vals for v in lv["rx"].values())
        lost = sum(v["lost"] for lv in vals for v in lv["rx"].values())
        tx_b = sum(v["bytes"] for lv in vals for v in lv["tx"].values())
        rx_b = sum(v["bytes"] for lv in vals for v in lv["rx"].values())
        prev = last.get("t", 0.0)           # first reading covers the time since the run started
        dt = max(1e-3, now - prev)
        ev = {"ev": "gstate", "elapsed_s": round(now, 1), "total_s": self.total_s, "tx_pkts": tx_n, "rx_pkts": rx_n, "lost": lost,
              "tx_mbps": round((tx_b - last.get("tx_b", 0)) * 8 / dt / 1e6, 2), "rx_mbps": round((rx_b - last.get("rx_b", 0)) * 8 / dt / 1e6, 2),
              "tx_pps": round((tx_n - last.get("tx_n", 0)) / dt), "rx_pps": round((rx_n - last.get("rx_n", 0)) / dt)}
        rows = [r for lv in vals for r in (lv["rows"][-8:] if lv is lives.get("self") else lv["rows"]) if r["dir"] == "rx" and r.get("lat_avg_us") is not None]
        if rows:
            if valid_lat:                              # absolute delay only when both clocks agree
                ev["lat_ms"] = round(sum(r["lat_avg_us"] for r in rows) / len(rows) / 1000.0, 3)
            ev["jitter_ms"] = round(max((r["jitter_us"] or 0) for r in rows) / 1000.0, 3)
        if self.stepping:
            ev.update(self._step_ev())
        self._mon_last = {"t": now, "tx_b": tx_b, "rx_b": rx_b, "tx_n": tx_n, "rx_n": rx_n}
        self.emit(ev)

    # ------------------------------------------------------------ results
    def _summarize(self, results: Dict[str, dict], clocks: dict) -> dict:
        cfg = self.cfg
        pairs_ok = clocks.get("pairs", {})
        names = {n.key: n.label for n in self.nodes.values()}
        valid_of = {r["id"]: bool(pairs_ok.get(f"{r['_src']}>{r['_dst']}")) for r in self.streams}
        streams, rows_by = [], {}
        for res in results.values():
            for r in res["rows"]:
                sid = r["stream"]
                valid_lat = valid_of.get(int(sid), False) if str(sid).lstrip("-").isdigit() else False
                rows_by.setdefault((r["t"], sid), {}).update({("tx_" if r["dir"] == "tx" else "rx_") + k: v for k, v in r.items() if k in ("pkts", "bytes")})
                d = rows_by[(r["t"], sid)]
                if r["dir"] == "rx":                       # absolute delay is meaningless unless the clocks agree; jitter always is
                    d.update({"lost_cum": r["lost_cum"], "lat_avg_us": r["lat_avg_us"] if valid_lat else None, "lat_p95_us": r["lat_p95_us"] if valid_lat else None,
                              "lat_max_us": r["lat_max_us"] if valid_lat else None, "jitter_us": r["jitter_us"]})
                else:
                    d["rtt_avg_us"] = r["rtt_avg_us"]
        for i, s in enumerate(self.sum_cfg):
            r0 = self.streams[i]
            sid = str(r0["id"])
            valid_lat = valid_of.get(r0["id"], False)
            tx_res = results[r0["_src"]]["tx"].get(sid)
            rx_res = results[r0["_dst"]]["rx"].get(sid)
            if tx_res is None or rx_res is None:
                continue
            dur = tx_res["duration_s"]
            lost = max(0, tx_res["pkts"] - rx_res["unique"])
            lat = P.Hist.from_dict(rx_res["lat"])
            rtt = P.Hist.from_dict(tx_res["rtt"])
            sz = s["size"]
            size_desc = {"fixed": f"{sz['size']} B", "range": f"{sz['min']}-{sz['max']} B", "imix": "IMIX 64/594/1514 B"}[sz["mode"]]
            warn = []
            if tx_res["target_pps"] and tx_res["achieved_pps"] < 0.93 * tx_res["target_pps"] and not s["packets"]:
                warn.append(f"{names[r0['_src']]} could only send {tx_res['achieved_pps']:.0f} packets/s of the {tx_res['target_pps']:.0f} requested.")
            if s["type"] != "tcp" and max(P.size_table(sz["mode"], sz.get("size", 64), sz.get("min", 64), sz.get("max", 1518), s.get("seed", 1), s["type"], bool(s.get("vlan")))) > P.STD_MTU_FRAME:
                warn.append(f"Frames larger than {P.STD_MTU_FRAME} bytes need an MTU above 1500; on a standard link they are fragmented or dropped.")
            if tx_res["err"]:
                warn.append(f"{tx_res['err']} packets could not be sent (send buffer full).")
            streams.append({
                "id": r0["id"], "name": s["name"], "type": s["type"], "direction": s["direction"], "src": names[r0["_src"]], "dst": names[r0["_dst"]], "size": size_desc,
                "tx": {"pkts": tx_res["pkts"], "bytes": tx_res["bytes"], "pps": tx_res["achieved_pps"], "mbps": _mbps(tx_res["bytes"], dur), "target_pps": tx_res["target_pps"], "err": tx_res["err"], "duration_s": dur},
                "rx": {"pkts": rx_res["pkts"], "unique": rx_res["unique"], "bytes": rx_res["bytes"], "pps": round(rx_res["unique"] / max(dur, 1e-3), 1), "mbps": _mbps(rx_res["bytes"], dur), "dup": rx_res["dup"], "ooo": rx_res["ooo"]},
                "lost": lost, "loss_pct": round(100.0 * lost / tx_res["pkts"], 4) if tx_res["pkts"] else 0.0,
                "lat_ms": lat.stats_ms() if valid_lat else None, "lat_hist": lat.to_dict() if valid_lat else None,
                "jitter_ms": None if rx_res["jitter_us"] is None else round(rx_res["jitter_us"] / 1000.0, 3), "jitter_max_ms": round(rx_res["jitter_max_us"] / 1000.0, 3),
                "rtt_ms": rtt.stats_ms() if rtt.n else None, "warnings": warn})
        phase_of = {r["id"]: r.get("_phase") for r in self.streams}
        flood_streams = [x for x in streams if phase_of.get(x["id"]) == "flood"]
        if self.stepping:                                  # the flood only measured the top rate: it is reported with the steps, not as a stream of the test
            streams = [x for x in streams if x not in flood_streams]
        tx_p = sum(x["tx"]["pkts"] for x in streams)
        lost = sum(x["lost"] for x in streams)
        totals = {"tx_pkts": tx_p, "rx_pkts": sum(x["rx"]["unique"] for x in streams), "lost": lost, "loss_pct": round(100.0 * lost / tx_p, 4) if tx_p else 0.0,
                  "tx_mbps": round(sum(x["tx"]["mbps"] for x in streams), 3), "rx_mbps": round(sum(x["rx"]["mbps"] for x in streams), 3),
                  "tx_pps": round(sum(x["tx"]["pps"] for x in streams), 1)}
        if self.stepping and streams:
            per = max(1, len(self.cfg["streams"]))
            sent_s = sum(x["tx"]["duration_s"] for x in streams) / per
            totals["tx_mbps"] = _mbps(sum(x["tx"]["bytes"] for x in streams), sent_s)
            totals["rx_mbps"] = _mbps(sum(x["rx"]["bytes"] for x in streams), sent_s)
            totals["tx_pps"] = round(tx_p / max(sent_s, 1e-3), 1)
        timed = [x for x in streams if x["lat_hist"]]
        if timed and len(timed) == len(streams):
            tot = P.Hist()
            for x in timed:
                tot.merge(P.Hist.from_dict(x["lat_hist"]))
            totals["lat_ms"] = tot.stats_ms()
        # per pair of Pis (sender -> receiver): who sent how much to whom, and what arrived
        pairs: Dict[tuple, dict] = {}
        for x in streams:
            g = pairs.setdefault((x["src"], x["dst"]), {"src": x["src"], "dst": x["dst"], "streams": 0, "tx_pkts": 0, "rx_pkts": 0, "lost": 0, "tx_mbps": 0.0, "rx_mbps": 0.0, "jitter_ms": None, "_hist": P.Hist(), "_timed": 0,
                                                        "_txb": 0, "_rxb": 0, "_dur": 0.0})
            g["_txb"] += x["tx"]["bytes"]
            g["_rxb"] += x["rx"]["bytes"]
            g["_dur"] += x["tx"]["duration_s"]
            g["streams"] += 1
            g["tx_pkts"] += x["tx"]["pkts"]
            g["rx_pkts"] += x["rx"]["unique"]
            g["lost"] += x["lost"]
            g["tx_mbps"] = round(g["tx_mbps"] + x["tx"]["mbps"], 3)
            g["rx_mbps"] = round(g["rx_mbps"] + x["rx"]["mbps"], 3)
            if x["jitter_ms"] is not None:
                g["jitter_ms"] = max(g["jitter_ms"] or 0, x["jitter_ms"])
            if x["lat_hist"]:
                g["_hist"].merge(P.Hist.from_dict(x["lat_hist"]))
                g["_timed"] += 1
        pair_list = []
        for g in pairs.values():
            g["loss_pct"] = round(100.0 * g["lost"] / g["tx_pkts"], 4) if g["tx_pkts"] else 0.0
            g["lat_ms"] = g["_hist"].stats_ms() if g["_timed"] == g["streams"] else None
            if self.stepping:                              # steps run one after another: the rate is the average over the time spent sending, not a sum
                per = max(1, len(self.cfg["streams"]))
                g["tx_mbps"], g["rx_mbps"] = _mbps(g["_txb"], g["_dur"] / per), _mbps(g["_rxb"], g["_dur"] / per)
            del g["_hist"], g["_timed"], g["_txb"], g["_rxb"], g["_dur"]
            pair_list.append(g)
        intervals = [{"t_offset_s": t, "stream": sid, "tx_pkts": d.get("tx_pkts", 0), "tx_bytes": d.get("tx_bytes", 0), "rx_pkts": d.get("rx_pkts", 0), "rx_bytes": d.get("rx_bytes", 0),
                      "lost_cum": d.get("lost_cum"), "lat_avg_us": d.get("lat_avg_us"), "lat_p95_us": d.get("lat_p95_us"), "lat_max_us": d.get("lat_max_us"),
                      "jitter_us": d.get("jitter_us"), "rtt_avg_us": d.get("rtt_avg_us")} for (t, sid), d in sorted(rows_by.items())]
        for row in intervals:
            self.on_interval(row)
        other = next((n for n in self.nodes.values() if not n.is_self), None)
        summary = {"kind": "generator", "streams": streams, "pairs": pair_list, "totals": totals, "clocks": clocks, "nodes": [n.label for n in self.nodes.values()],
                   "peer": {"hardware": (other.status.get("hardware") if other else None), "clock": (other.status.get("clock") if other else None)},
                   "duration_s": round(min(self.total_s, self.sent_s) if (self.stop_evt.is_set() and self.sent_s is not None) else self.total_s, 1),   # a stopped test shows how long it really ran, not the planned length
                   "stopped_early": self.stop_evt.is_set()}
        for res in results.values():
            for e in res["errors"]:
                summary.setdefault("warnings", []).append(e)
        if self.stepping:
            summary["steps"] = self._steps_summary(streams, flood_streams, clocks)
            summary["duration_s"] = round(sum(x["tx"]["duration_s"] for x in streams + flood_streams) / max(1, len(cfg["streams"])), 1)
            summary["verdict"] = steps_verdict(summary, cfg["rule"])
        else:
            summary["verdict"] = verdict(summary, cfg["rule"])
        return summary

    def _steps_summary(self, streams: List[dict], flood_streams: List[dict], clocks: dict) -> dict:
        """The step table of the stored result, from the final counts of every Pi."""
        st, rule = self.steps_cfg, self.cfg["rule"]
        by_id = {x["id"]: x for x in streams}
        key = delay_key(rule)
        rows: List[dict] = []
        for ph in self.phases:
            if ph["kind"] != "step":
                continue
            xs = [by_id[q["id"]] for q in ph["streams"] if q["id"] in by_id]
            if not ph.get("ran") or len(xs) < len(ph["streams"]):
                rows.append({"n": ph["n"], "of": ph["of"], "share": ph["share"], "asked_mbps": round(self.top_mbps * ph["share"], 4) if self.top_mbps else None,
                             "state": "skipped", "clean": None, "why": "not run"})
                continue
            got = [{"name": self.cfg["streams"][q["_base"]]["name"], "src": x["src"], "tx_pkts": x["tx"]["pkts"], "tx_bytes": x["tx"]["bytes"], "rx_pkts": x["rx"]["unique"],
                    "rx_bytes": x["rx"]["bytes"], "delay_ms": (x["lat_ms"] or {}).get(key), "jitter_ms": x["jitter_ms"], "dur": x["tx"]["duration_s"]}
                   for q, x in zip(ph["streams"], xs)]
            row = step_row(ph, got, rule, bool(clocks.get("valid")))
            if ph.get("stopped"):                          # Stop was pressed during this step: its numbers are shown, it is not judged
                row.update(state="stopped", clean=None, why="stopped before the step ended")
            rows.append(row)
        flood = None
        if flood_streams:
            dur = max(1e-3, sum(x["tx"]["duration_s"] for x in flood_streams) / len(flood_streams))
            txp, lost = sum(x["tx"]["pkts"] for x in flood_streams), sum(x["lost"] for x in flood_streams)
            flood = {"sent_mbps": _mbps(sum(x["tx"]["bytes"] for x in flood_streams), dur), "got_mbps": _mbps(sum(x["rx"]["bytes"] for x in flood_streams), dur),
                     "duration_s": round(dur, 1), "lost": lost, "loss_pct": round(100.0 * lost / txp, 4) if txp else 0.0}
        return steps_result(rows, self.top_mbps, not st.get("top_mbps"), flood, st, rule, self.fail_stop, bool(clocks.get("valid")))


# ---------------------------------------------------------------- throughput in steps: pure helpers (docs/SPEC.md 13.9)
def stream_mbps(s: dict) -> float:
    """A stream's configured rate in Mb/s of whole frames (a packets/s rate is converted with the stream's mean frame size)."""
    r = s["rate"]
    if r.get("mbps"):
        return float(r["mbps"])
    sz = s["size"]
    mean = P.mean_size(P.size_table(sz["mode"], sz.get("size", 64), sz.get("min", 64), sz.get("max", 1518), s.get("seed", 1), s["type"], bool(s.get("vlan")) and s["type"] == "l2"))
    return float(r["pps"]) * mean * 8 / 1e6


def fmt_mbps(v: Optional[float]) -> str:
    if v is None:
        return "?"
    if v >= 100:
        return f"{v:.0f}"
    return (f"{v:.1f}" if v >= 10 else f"{v:.2f}" if v >= 0.1 else f"{v:.3f}").rstrip("0").rstrip(".")


def delay_key(rule: dict) -> str:
    return "max" if rule.get("latency_percentile") == 100 else f"p{rule.get('latency_percentile', 99)}"


def step_limit(rule: dict) -> dict:
    """The limits a step is judged against (the test's own pass limits)."""
    return {"max_loss_pct": rule.get("max_loss_pct"), "max_latency_ms": rule.get("max_latency_ms"), "latency_percentile": rule.get("latency_percentile", 99)}


def step_row(ph: dict, got: List[dict], rule: dict, delay_valid: bool) -> dict:
    """One row of the step table. `got` = per stream {"name", "src", "tx_pkts", "tx_bytes", "rx_pkts", "rx_bytes", "delay_ms", "jitter_ms", "dur"}.
    A step is CLEAN when every stream's loss and delay are inside the test's pass limits (a limit left empty is not checked; a delay limit
    can only be checked when the clocks agree)."""
    ml, mlat, key = rule.get("max_loss_pct"), rule.get("max_latency_ms"), delay_key(rule)
    label = "the slowest packet" if key == "max" else f"{key} delay"
    multi = len(got) > 1
    why: List[str] = []
    for x in got:
        who = f"{x['name']}: " if multi else ""
        lost = max(0, x["tx_pkts"] - x["rx_pkts"])
        lp = 100.0 * lost / x["tx_pkts"] if x["tx_pkts"] else 0.0
        if not x["tx_pkts"]:
            why.append(f"{who}nothing was sent")
        elif not x["rx_pkts"]:
            why.append(f"{who}nothing arrived")
        elif ml is not None and lp > ml:
            why.append(f"{who}{lp:.3g} % lost (limit {ml:g} %)")
        if mlat is not None and delay_valid and x.get("delay_ms") is not None and x["delay_ms"] > mlat:
            why.append(f"{who}{label} {x['delay_ms']:g} ms (limit {mlat:g} ms)")
    dur = max(1e-3, sum(float(x["dur"] or 0) for x in got) / max(1, len(got)))
    txp, rxp = sum(x["tx_pkts"] for x in got), sum(x["rx_pkts"] for x in got)
    lost = sum(max(0, x["tx_pkts"] - x["rx_pkts"]) for x in got)
    asked = ph.get("asked_mbps")
    sent = _mbps(sum(x["tx_bytes"] for x in got), dur)
    delays = [x["delay_ms"] for x in got if x.get("delay_ms") is not None]
    jit = [x["jitter_ms"] for x in got if x.get("jitter_ms") is not None]
    limited = bool(asked) and sent < 0.93 * asked          # the Pi, not the link, held the rate down: the step counts for what was really sent
    note = ""
    if limited:
        srcs = sorted({x["src"] for x in got if x.get("src")})
        note = f"{' and '.join(srcs) or 'The Pi'} could only send {fmt_mbps(sent)} of the {fmt_mbps(asked)} Mb/s asked"
    return {"n": ph["n"], "of": ph["of"], "share": ph["share"], "asked_mbps": asked, "sent_mbps": sent, "got_mbps": _mbps(sum(x["rx_bytes"] for x in got), dur),
            "tx_pkts": txp, "rx_pkts": rxp, "lost": lost, "loss_pct": round(100.0 * lost / txp, 4) if txp else 0.0,
            "delay_ms": (max(delays) if delays else None) if delay_valid else None, "delay_pct": 100 if key == "max" else int(key[1:]),
            "jitter_ms": max(jit) if jit else None, "duration_s": round(dur, 1), "limited": limited, "note": note,
            "clean_mbps": round(min(asked, sent), 3) if limited else asked,
            "state": "done", "clean": not why, "why": "; ".join(why)}


def steps_result(rows: List[dict], top_mbps: Optional[float], auto: bool, flood: Optional[dict], st: dict, rule: dict, fail_stop: bool, delay_valid: bool) -> dict:
    """The step table with its headline: the highest rate with every step up to it clean."""
    clean_mbps, clean_step = 0.0, None
    for r in rows:
        if r["clean"] is not True:
            break
        clean_mbps, clean_step = float(r["clean_mbps"] or 0), r["n"]
    judged = [r for r in rows if r["clean"] is not None]
    if clean_step is not None:
        headline = f"Clean up to {fmt_mbps(clean_mbps)} Mb/s"
    elif judged:
        headline = f"No clean rate found: even {fmt_mbps(judged[0]['asked_mbps'])} Mb/s was not clean"
    else:
        headline = "No step was finished"
    notes: List[str] = []
    if clean_step is not None and judged and clean_step == rows[-1]["n"]:
        notes.append("Every step was clean: the link may carry more than the top rate tested." if not auto else "Every step was clean, up to the rate the flood measured.")
    if fail_stop:
        notes.append(f"Stopped after {st['stop_after_fail']} failed steps in a row; the higher rates were not sent.")
    if rule.get("max_latency_ms") is not None and not delay_valid:
        notes.append("Delay was not checked (the Pis' clocks are not synced): the steps were judged on loss only.")
    return {"method": "steps", "auto": auto, "top_mbps": top_mbps, "step_s": st["step_s"], "of": len(st["fractions"]), "flood": flood, "rows": rows,
            "clean_mbps": round(clean_mbps, 3), "clean_step": clean_step, "limit": step_limit(rule), "stopped_after_fails": bool(fail_stop),
            "headline": headline, "notes": notes}


def steps_csv(summary: dict) -> str:
    """The step table of a stored result as CSV (one row per step; a first row for the flood that measured the top rate)."""
    import csv
    import io
    sp = (summary or {}).get("steps") or {}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["step", "of", "share_of_top", "asked_mbps", "sent_mbps", "got_mbps", "sent_packets", "got_packets", "lost_packets", "lost_pct",
                "delay_ms", "delay_percentile", "duration_s", "state", "clean", "why", "note"])
    f = sp.get("flood")
    if f:
        w.writerow(["flood", sp.get("of"), "", "", f.get("sent_mbps"), f.get("got_mbps"), "", "", f.get("lost"), f.get("loss_pct"), "", "", f.get("duration_s"), "done", "", "measures the top rate", ""])
    v = lambda x: "" if x is None else x
    for r in sp.get("rows") or []:
        w.writerow([r["n"], r["of"], r.get("share"), v(r.get("asked_mbps")), v(r.get("sent_mbps")), v(r.get("got_mbps")), v(r.get("tx_pkts")), v(r.get("rx_pkts")), v(r.get("lost")),
                    v(r.get("loss_pct")), v(r.get("delay_ms")), v(r.get("delay_pct")), v(r.get("duration_s")), r.get("state", ""),
                    {True: "yes", False: "no"}.get(r.get("clean"), ""), r.get("why", ""), r.get("note", "")])
    w.writerow([])
    w.writerow(["headline", sp.get("headline", ""), "clean_mbps", sp.get("clean_mbps", ""), "top_mbps", v(sp.get("top_mbps")), "top_rate", "auto (flood)" if sp.get("auto") else "typed"])
    return buf.getvalue()


def steps_verdict(summary: dict, rule: dict) -> dict:
    """PASS/FAIL of a throughput-in-steps run: is there a clean rate, and is it at least the rate the test needs? (Loss at the higher steps is
    the finding, not a failure.)"""
    sp, tot = summary["steps"], summary["totals"]
    reasons: List[str] = []
    judged = [r for r in sp["rows"] if r["clean"] is not None]
    if not judged:
        return {"pass": False, "reasons": ["The test was stopped before the first step ended, so there is no clean rate." if summary.get("stopped_early")
                                           else "No step was finished, so there is no clean rate."]}
    if sp["clean_step"] is None:
        r0 = judged[0]
        reasons.append(f"No clean rate: the lowest step ({fmt_mbps(r0['asked_mbps'])} Mb/s) already failed - {r0['why']}.")
    need = rule.get("min_clean_mbps")
    if need is not None and sp["clean_step"] is not None and sp["clean_mbps"] < need:
        reasons.append(f"Clean up to {fmt_mbps(sp['clean_mbps'])} Mb/s - at least {need:g} Mb/s clean is required.")
    if rule.get("max_latency_ms") is not None and any(x["lat_ms"] is None for x in summary["streams"]):
        c = summary["clocks"]
        offs = ", ".join(f"{v['name']} {v['offset_ms']}" for v in (c.get("nodes") or {}).values())
        reasons.append(f"Delay could not be checked because the Pis' clocks are not synced within {c.get('limit_ms', 0):g} ms{(' (' + offs + ')') if offs else ''}: the steps were judged on loss only.")
    return {"pass": not reasons, "reasons": reasons}


def verdict(summary: dict, rule: dict) -> dict:
    """Plain-sentence PASS/FAIL for a generator run."""
    reasons: List[str] = []
    streams, tot = summary["streams"], summary["totals"]
    if not streams or tot["tx_pkts"] == 0:
        return {"pass": False, "reasons": ["Nothing was sent. Check the addresses, ports and that the other Pi is running."]}
    if tot["rx_pkts"] == 0:
        reasons.append("Nothing arrived at the other Pi - check the cable, address and any firewall.")
    ml = rule.get("max_loss_pct")
    if ml is not None:
        for x in streams:
            if x["loss_pct"] > ml:
                reasons.append(f"{x['name']}: {x['loss_pct']:g} % of packets were lost - the limit is {ml:g} %.")
    mj = rule.get("max_jitter_ms")
    if mj is not None:
        for x in streams:
            if x["jitter_ms"] is not None and x["jitter_ms"] > mj:
                reasons.append(f"{x['name']}: jitter was {x['jitter_ms']:g} ms - the limit is {mj:g} ms.")
    mlat = rule.get("max_latency_ms")
    if mlat is not None:
        key = "max" if rule.get("latency_percentile") == 100 else f"p{rule.get('latency_percentile', 99)}"
        label = "the slowest packet" if key == "max" else f"{key[1:]} % of packets"
        for x in streams:
            if x["lat_ms"] is None:
                c = summary["clocks"]
                nn = c.get("nodes") or {}
                offs = ", ".join(f"{v['name']} {v['offset_ms']}" for v in nn.values()) or f"this Pi {c.get('a_offset_ms')}, other Pi {c.get('b_offset_ms')}"
                note = (c.get("notes") or {}).get(next(iter(c.get("notes") or {}), ""), "")
                reasons.append(f"{x['name']}: delay could not be checked because the two Pis' clocks are not synced within {c['limit_ms']:g} ms ({offs}).{(' Also: ' + note) if note else ''}")
            elif x["lat_ms"][key] is not None and x["lat_ms"][key] > mlat:
                reasons.append(f"{x['name']}: {label} arrived within {x['lat_ms'][key]:g} ms - the limit is {mlat:g} ms.")
    mt = rule.get("min_throughput_mbps")
    if mt is not None and tot["rx_mbps"] < mt:
        reasons.append(f"Throughput was {tot['rx_mbps']:g} Mb/s - at least {mt:g} Mb/s is required.")
    for x in streams:
        for w in x["warnings"]:
            if "could only send" in w:
                reasons.append(f"{x['name']}: {w}")
    return {"pass": not reasons, "reasons": reasons}
