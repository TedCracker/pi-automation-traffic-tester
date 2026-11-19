"""The application library: the traffic shapes a utility link carries (SCADA, NMS, video, AMI ...), used by test sets (docs/design/UX-INSTRUMENT-FABLE.md 5.2).

Every entry is one stream (what is sent) plus an EXAMPLE target (what a link should meet). The targets are sample values, not from a standard: a test
set shows them labelled "example target" and the user edits each one on its row. A test set holds copies of these entries, so editing a set never
changes the library.
"""
import copy
from typing import List, Optional

# group: where the entry is offered in "Add application". stream: the fields of a generator stream. target: the example target (GenTarget fields).
_LIB = [
    {"id": "trip", "name": "Transfer trip", "group": "DA",
     "about": "A small protection message 100 times a second (trip-like frames), with round-trip probes.",
     "stream": {"type": "udp", "rate": {"pps": 100}, "size": {"mode": "fixed", "size": 80}, "rtt_probe_pps": 20},
     "target": {"max_loss_pct": 0.1, "max_latency_ms": 60, "latency_percentile": 99, "max_jitter_ms": 10}},
    {"id": "scada_poll", "name": "SCADA poll (DNP3)", "group": "DA",
     "about": "A master polling a remote site: small requests over TCP, 20 a second.",
     "stream": {"type": "tcp", "rate": {"pps": 20}, "size": {"mode": "range", "min": 40, "max": 250}, "rtt_probe_pps": 10},
     "target": {"max_loss_pct": 0.5, "max_latency_ms": 150, "latency_percentile": 95}},
    {"id": "scada_unsol", "name": "SCADA unsolicited (DNP3)", "group": "DA",
     "about": "Event reports the outstation sends by itself: a few small bursts a second.",
     "stream": {"type": "udp", "rate": {"pps": 10}, "size": {"mode": "fixed", "size": 120}, "burst_packets": 5, "burst_gap_ms": 500, "rtt_probe_pps": 5},
     "target": {"max_loss_pct": 0.1, "max_latency_ms": 250, "latency_percentile": 99}},
    {"id": "goose", "name": "GOOSE-like messages", "group": "DA",
     "about": "Peer-to-peer protection events, 100 a second over UDP (the real GOOSE trip tests are on the DA list).",
     "stream": {"type": "udp", "rate": {"pps": 100}, "size": {"mode": "fixed", "size": 150}, "rtt_probe_pps": 20},
     "target": {"max_loss_pct": 0.1, "max_latency_ms": 60, "latency_percentile": 99, "max_jitter_ms": 10}},
    {"id": "nms", "name": "NMS (SNMP polling)", "group": "FAN",
     "about": "A network management system polling devices: 30 small requests a second, mixed sizes.",
     "stream": {"type": "udp", "rate": {"pps": 30}, "size": {"mode": "range", "min": 80, "max": 600}, "rtt_probe_pps": 5},
     "target": {"max_loss_pct": 1.0, "max_latency_ms": 500, "latency_percentile": 95}},
    {"id": "nms_file", "name": "NMS file transfer", "group": "FAN",
     "about": "A firmware or configuration push over TCP, 2 Mb/s.",
     "stream": {"type": "tcp", "rate": {"mbps": 2}, "size": {"mode": "fixed", "size": 1400}, "rtt_probe_pps": 2},
     "target": {"max_loss_pct": 1.0, "min_throughput_mbps": 1.5}},
    {"id": "video", "name": "Camera video", "group": "FAN",
     "about": "A camera stream: 4 Mb/s of 1,358-byte packets in small bursts.",
     "stream": {"type": "udp", "rate": {"mbps": 4}, "size": {"mode": "fixed", "size": 1358}, "burst_packets": 7, "rtt_probe_pps": 5},
     "target": {"max_loss_pct": 0.5, "max_jitter_ms": 30, "min_throughput_mbps": 3.8}},
    {"id": "ami", "name": "AMI meter reads", "group": "FAN",
     "about": "Many meters answering at once: a burst of 20 small packets every second.",
     "stream": {"type": "udp", "rate": {"pps": 20}, "size": {"mode": "fixed", "size": 200}, "burst_packets": 20, "burst_gap_ms": 1000, "rtt_probe_pps": 2},
     "target": {"max_loss_pct": 2.0, "max_latency_ms": 2000, "latency_percentile": 95}},
    {"id": "bulk", "name": "Bulk load", "group": "Fill",
     "about": "Large packets at a steady 20 Mb/s: fills the link to see what the others cost.",
     "stream": {"type": "udp", "rate": {"mbps": 20}, "size": {"mode": "fixed", "size": 1400}, "rtt_probe_pps": 2},
     "target": {"min_throughput_mbps": 0}},
]


def library() -> List[dict]:
    """A copy of the application library (safe to edit)."""
    return copy.deepcopy(_LIB)


def get(app_id: str) -> Optional[dict]:
    return next((copy.deepcopy(a) for a in _LIB if a["id"] == app_id), None)


def new_stream(app_id: str, name: Optional[str] = None) -> dict:
    """A generator stream for application `app_id`, with its example target."""
    a = get(app_id)
    if a is None:
        raise KeyError(app_id)
    return {"name": name or a["name"], "app": a["id"], "enabled": True, **a["stream"], "target": {"example": True, **a["target"]}}
