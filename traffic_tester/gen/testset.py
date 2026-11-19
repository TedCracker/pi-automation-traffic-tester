"""A utility test set: the applications of a test (SCADA, NMS, camera ...) run alone, then all together, then together with fill traffic, and stored as
ONE result with a table per application against its own target (docs/design/UX-INSTRUMENT-FABLE.md 5.2).

It drives one GeneratorRun per step (the engine that already runs several streams at once on both Pis) and joins the steps: the "all together" step is the
stored result the rest of the app already knows (streams, totals, clocks, charts); this module adds the `testset` block and the verdict."""
from __future__ import annotations

import copy
import threading
import time
from typing import Callable, Dict, List, Optional

from . import node as G
from . import run as R

GAP_S = 4.0            # the time a step takes on top of its length (start delay, in-flight packets, stopping both Pis): used for the progress bar


def is_testset(profile: dict) -> bool:
    return profile.get("kind") == "generator" and bool(((profile.get("gen") or {}).get("testset") or {}).get("enabled"))


# ---------------------------------------------------------------- judging one application against its target
def _target(stream_cfg: dict, rule: dict) -> dict:
    t = stream_cfg.get("target")
    src = t if t else rule
    return {k: src.get(k) for k in ("max_loss_pct", "max_latency_ms", "latency_percentile", "max_jitter_ms", "min_throughput_mbps")} | {"example": bool(t and t.get("example"))}


def measures(x: dict) -> dict:
    """The numbers of one application in one step, from the engine's per-stream summary."""
    return {"tx_pkts": x["tx"]["pkts"], "rx_pkts": x["rx"]["unique"], "lost": x["lost"], "loss_pct": x["loss_pct"], "lat_ms": x["lat_ms"], "jitter_ms": x["jitter_ms"],
            "tx_mbps": x["tx"]["mbps"], "rx_mbps": x["rx"]["mbps"], "warnings": list(x.get("warnings") or [])}


def judge(name: str, m: dict, tg: dict) -> List[dict]:
    """One check per limit that is set: {key, label, limit, value, unit, pass (None = could not be checked), margin (limit minus value, or value minus limit), text}."""
    out: List[dict] = []
    ml = tg.get("max_loss_pct")
    if ml is not None:
        v = m["loss_pct"]
        out.append({"key": "loss", "label": "Loss", "limit": ml, "value": v, "unit": "%", "pass": v <= ml, "margin": round(ml - v, 4), "dir": "max",
                    "text": f"{name}: {v:g} % of packets were lost - the limit is {ml:g} %."})
    mlat = tg.get("max_latency_ms")
    if mlat is not None:
        pc = tg.get("latency_percentile") or 99
        key = "max" if pc == 100 else f"p{pc}"
        v = (m.get("lat_ms") or {}).get(key)
        label = "Slowest packet" if key == "max" else f"Latency p{pc}"
        if v is None:
            out.append({"key": "latency", "label": label, "limit": mlat, "value": None, "unit": "ms", "pass": None, "margin": None, "dir": "max",
                        "text": f"{name}: delay could not be checked because the two Pis' clocks are not synced."})
        else:
            out.append({"key": "latency", "label": label, "limit": mlat, "value": v, "unit": "ms", "pass": v <= mlat, "margin": round(mlat - v, 3), "dir": "max",
                        "text": f"{name}: {'the slowest packet' if key == 'max' else f'{pc} % of packets'} arrived within {v:g} ms - the limit is {mlat:g} ms."})
    mj = tg.get("max_jitter_ms")
    if mj is not None and m.get("jitter_ms") is not None:
        v = m["jitter_ms"]
        out.append({"key": "jitter", "label": "Jitter", "limit": mj, "value": v, "unit": "ms", "pass": v <= mj, "margin": round(mj - v, 3), "dir": "max",
                    "text": f"{name}: jitter was {v:g} ms - the limit is {mj:g} ms."})
    mt = tg.get("min_throughput_mbps")
    if mt:
        v = m["rx_mbps"]
        out.append({"key": "throughput", "label": "Throughput", "limit": mt, "value": v, "unit": "Mb/s", "pass": v >= mt, "margin": round(v - mt, 3), "dir": "min",
                    "text": f"{name}: it carried {v:g} Mb/s - at least {mt:g} Mb/s is required."})
    return out


def app_pass(checks: List[dict], m: dict) -> Optional[bool]:
    if m["tx_pkts"] == 0 or any("could only send" in w for w in m.get("warnings") or []):
        return False
    judged = [c for c in checks if c["pass"] is not None]
    if any(c["pass"] is False for c in judged):
        return False
    return True if judged else None


class TestSetRun:
    """Same interface as GeneratorRun (run, stop, set_stream, clear, status, error), so the run manager treats it as one test."""
    LOST_GRACE_S = R.GeneratorRun.LOST_GRACE_S

    def __init__(self, profile: dict, node: G.Node, emit: Callable[[dict], None] = lambda e: None,
                 on_interval: Callable[[dict], None] = lambda r: None, remote=None, services=None):
        self.p = profile
        self.cfg = profile["gen"]
        self.ts = self.cfg["testset"]
        self.node, self.emit, self.on_interval, self.remote, self.services = node, emit, on_interval, remote, services
        self.stop_evt = threading.Event()
        self.error: Optional[str] = None
        self.status = "created"
        self.total_s = 0.0
        self.sent_s: Optional[float] = None
        self.orig = [s for s in self.cfg["streams"] if s.get("enabled", True)]            # the applications that take part (the others are kept in the list, not run)
        self.idx = [i for i, s in enumerate(self.cfg["streams"]) if s.get("enabled", True)]
        self.steps = self._plan()
        self.cur: Optional[R.GeneratorRun] = None
        self.cur_step: Optional[dict] = None
        self.done_steps: List[dict] = []
        self._t0 = 0.0
        self._offset = 0.0

    # ------------------------------------------------------------ the steps
    def _plan(self) -> List[dict]:
        ts, n = self.ts, len(self.orig)
        steps: List[dict] = []
        if ts["alone"] and n > 1:
            for k in range(n):
                steps.append({"kind": "alone", "label": f"Alone · {self.orig[k]['name']}", "apps": [k], "dur": ts["alone_s"]})
        steps.append({"kind": "together", "label": "All together", "apps": list(range(n)), "dur": float(self.cfg["duration_s"])})
        if ts["fill"]:
            steps.append({"kind": "fill", "label": f"All together + fill {ts['fill_mbps']:g} Mb/s", "apps": list(range(n)) + [n], "dur": ts["fill_s"]})
        for k, s in enumerate(steps):
            s["n"], s["of"] = k + 1, len(steps)
        return steps

    def planned_total_s(self) -> float:
        return round(sum(s["dur"] + GAP_S for s in self.steps), 1)

    def _fill_stream(self) -> dict:
        from .. import apps as A
        base = self.orig[0]
        f = A.new_stream("bulk", "Fill traffic")
        f.pop("target", None)
        f.update(rate={"mbps": self.ts["fill_mbps"]}, src=copy.deepcopy(base["src"]), dst=copy.deepcopy(base["dst"]), direction=base.get("direction", "a_to_b"))
        # a complete stream dict: take the defaults of the model for everything the library entry leaves out
        from ..profiles import GenStream
        return GenStream.model_validate(f).model_dump()

    def _sub_profile(self, step: dict) -> dict:
        sub = copy.deepcopy(self.p)
        streams = [copy.deepcopy(self.orig[k]) if k < len(self.orig) else self._fill_stream() for k in step["apps"]]
        for s in streams:
            s["duration_s"], s["packets"], s["start_offset_s"] = None, None, 0.0
        g = sub["gen"]
        g["streams"], g["duration_s"] = streams, float(step["dur"])
        g["steps"] = {**g["steps"], "enabled": False}
        g["testset"]["enabled"] = False                                   # one step is a plain generator run
        return sub

    # ------------------------------------------------------------ while it runs
    def stop(self) -> None:
        self.stop_evt.set()
        r = self.cur
        if r is not None:
            r.stop()

    def set_stream(self, i: int, on: bool) -> None:
        st, r = self.cur_step, self.cur
        if r is None or st is None or r.status != "running":
            raise ValueError("The test is not running.")
        if st["kind"] == "alone":
            raise ValueError("Applications can be switched on and off in the 'all together' step.")
        if i not in self.idx:
            raise ValueError(f"There is no application number {i + 1} in this run.")
        r.set_stream(self.idx.index(i), on)

    def clear(self) -> None:
        r = self.cur
        if r is not None:
            r.clear()

    # ------------------------------------------------------------ events
    def _sub_emit(self, step: dict, first: bool) -> Callable[[dict], None]:
        def emit(e: dict) -> None:
            ev = e.get("ev")
            if ev == "done":
                return                                                       # one "done" for the whole test, sent by run()
            if ev == "started":
                if first:
                    self.emit({**e, "total_s": self.planned_total_s(), "testset": self._ts_ev(step, 0.0)})
                return
            if ev == "gstate":
                e = {**e, "elapsed_s": round(self._offset + float(e.get("elapsed_s") or 0), 1), "total_s": self.planned_total_s(), "until_stopped": False, "testset": self._ts_ev(step, e.get("elapsed_s") or 0)}
                for x in e.get("streams_now") or []:                          # the step's own numbering -> the application's place in the test
                    if x["i"] < len(step["apps"]):
                        k = step["apps"][x["i"]]
                        x["i"] = self.idx[k] if k < len(self.idx) else len(self.cfg["streams"])
            self.emit(e)
        return emit

    def _ts_ev(self, step: dict, step_elapsed: float) -> dict:
        return {"n": step["n"], "of": step["of"], "kind": step["kind"], "label": step["label"], "step_s": step["dur"], "step_elapsed_s": round(float(step_elapsed), 1),
                "apps": [self.idx[k] if k < len(self.idx) else len(self.cfg["streams"]) for k in step["apps"]], "done": self.done_steps}

    # ------------------------------------------------------------ run
    def _wait_idle(self) -> None:
        for _ in range(40):
            if not self.node.busy():
                return
            time.sleep(0.25)

    def run(self) -> dict:
        self.status = "running"
        self.total_s = self.planned_total_s()
        t0 = self._t0 = time.monotonic()
        subs: List[dict] = []                   # per step: {"step", "summary"}
        self.emit({"ev": "info", "text": f"Test set: {len(self.steps)} step(s), {len(self.orig)} application(s)"})
        try:
            if not self.orig:
                raise G.GenError("Switch on at least one application.")
            if self.cfg["duration_s"] <= 0:
                raise G.GenError("A test set needs a length for the 'all together' step.")
            for step in self.steps:
                if self.stop_evt.is_set():
                    break
                self._wait_idle()
                self._offset = time.monotonic() - t0
                self.cur_step = step
                run = self.cur = R.GeneratorRun(self._sub_profile(step), self.node, self._sub_emit(step, step["n"] == 1),
                                                self.on_interval if step["kind"] == "together" else (lambda r: None), remote=self.remote, services=self.services)
                if self.stop_evt.is_set():
                    break
                s = run.run()
                if run.status == "failed" or run.error:
                    raise G.GenError(f"Step {step['n']} of {step['of']} ({step['label']}): {run.error or s.get('error') or 'it failed'}")
                subs.append({"step": step, "summary": s, "stopped": run.status == "stopped"})
                self.done_steps = self.done_steps + [{"n": step["n"], "kind": step["kind"], "label": step["label"],
                                                      "apps": {str(self.idx[k] if k < len(self.idx) else len(self.cfg["streams"])): measures(x) for k, x in zip(step["apps"], s["streams"])}}]
                if run.status == "stopped":
                    break
                if step["n"] < step["of"] and not self.stop_evt.wait(1.0):
                    pass
            self.status = "stopped" if self.stop_evt.is_set() else "finished"
        except (G.GenError, ValueError) as e:
            self.error, self.status = str(e), "failed"
        except Exception as e:  # noqa
            self.error, self.status = f"{type(e).__name__}: {e}", "failed"
        finally:
            self.cur = None
        self.sent_s = time.monotonic() - t0
        summary = self._summarize(subs) if not self.error else {"error": self.error, "kind": "generator"}
        if self.error:
            summary["error"] = self.error
        self.emit({"ev": "done", "status": self.status, "error": self.error, "summary": summary if "verdict" in summary else None})
        return summary

    # ------------------------------------------------------------ the result
    def _summarize(self, subs: List[dict]) -> dict:
        tog = next((x for x in subs if x["step"]["kind"] == "together"), None)
        by_kind: Dict[str, dict] = {x["step"]["kind"] + (str(x["step"]["apps"][0]) if x["step"]["kind"] == "alone" else ""): x for x in subs}
        if tog is None:
            reason = "The test was stopped before the 'all together' step ran, so it is not judged." if self.stop_evt.is_set() else "The 'all together' step did not run."
            return {"kind": "generator", "streams": [], "totals": {"tx_pkts": 0, "rx_pkts": 0, "lost": 0, "loss_pct": 0.0, "tx_mbps": 0, "rx_mbps": 0, "tx_pps": 0},
                    "pairs": [], "clocks": {}, "nodes": [], "duration_s": round(self.sent_s or 0, 1), "stopped_early": self.stop_evt.is_set(),
                    "verdict": {"pass": False, "reasons": [reason]}, "testset": self._block([], subs, [])}
        base = copy.deepcopy(tog["summary"])
        rule = self.cfg["rule"]
        apps, reasons = [], []
        for k, sc in enumerate(self.orig):
            x = base["streams"][k]
            tg = _target(sc, rule)
            m = measures(x)
            checks = judge(sc["name"], m, tg)
            alone = by_kind.get(f"alone{k}")
            if len(self.orig) == 1:
                alone = tog
            fill = by_kind.get("fill")
            apps.append({"i": self.idx[k], "name": sc["name"], "app": sc.get("app", ""), "src": x["src"], "dst": x["dst"], "type": x["type"], "size": x["size"],
                         "target": tg, "alone": measures(alone["summary"]["streams"][0 if alone is not tog else k]) if alone else None, "together": m,
                         "fill": measures(fill["summary"]["streams"][k]) if fill and len(fill["summary"]["streams"]) > k else None,
                         "checks": checks, "pass": app_pass(checks, m)})
            for c in checks:
                if c["pass"] is False or c["pass"] is None:
                    reasons.append(c["text"])
            for w in m["warnings"]:
                if "could only send" in w:
                    reasons.append(f"{sc['name']}: {w}")
        if base["totals"]["tx_pkts"] == 0:
            reasons.insert(0, "Nothing was sent. Check the addresses, ports and that the other Pi is running.")
        elif base["totals"]["rx_pkts"] == 0:
            reasons.insert(0, "Nothing arrived at the other Pi - check the cable, address and any firewall.")
        if self.stop_evt.is_set() and tog["stopped"]:
            base["stopped_early"] = True
        base["testset"] = self._block(apps, subs, reasons)
        base["verdict"] = {"pass": not reasons and all(a["pass"] is not False for a in apps), "reasons": reasons}
        # not-judged steps that stopped part way are listed in the block; the overall run length is every step's
        base["duration_s"] = round(sum(x["summary"].get("duration_s") or 0 for x in subs), 1)
        base["together_s"] = tog["summary"].get("duration_s")
        for w in [w for x in subs if x is not tog for w in (x["summary"].get("warnings") or [])]:
            base.setdefault("warnings", []).append(w)
        return base

    def _block(self, apps: List[dict], subs: List[dict], reasons: List[str]) -> dict:
        done = {x["step"]["n"]: x for x in subs}
        steps = []
        for st in self.steps:
            x = done.get(st["n"])
            steps.append({"n": st["n"], "of": st["of"], "kind": st["kind"], "label": st["label"], "planned_s": st["dur"], "ran": x is not None,
                          "duration_s": x["summary"].get("duration_s") if x else None, "stopped": bool(x and x["stopped"]),
                          "judged": st["kind"] == "together"})
        fill = next((x for x in subs if x["step"]["kind"] == "fill"), None)
        return {"name": self.p.get("name"), "group": self.ts["group"], "steps": steps, "apps": apps, "length_s": self.cfg["duration_s"],
                "example_targets": any(a["target"].get("example") for a in apps), "fill_mbps": self.ts["fill_mbps"] if fill else None,
                "alone_s": self.ts["alone_s"] if self.ts["alone"] and len(self.orig) > 1 else None}
