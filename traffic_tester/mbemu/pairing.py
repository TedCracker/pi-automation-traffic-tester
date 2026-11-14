"""The two ends of a channel are opposite: PROTO A <-> PROTO B, my TX_ID is your RX_ID.  The Pi that starts a test is PROTO A by default;
the Pi that answers is given the mirror image of the settings, so one form configures both ends."""
from __future__ import annotations

import copy
from typing import Optional

from .status import Settings


def far_settings(near: dict) -> dict:
    d = copy.deepcopy(near or {})
    base = Settings.from_dict(d)
    d["proto"] = "B" if base.proto == "A" else "A"
    d["tx_id"], d["rx_id"] = base.rx_id, base.tx_id
    d["trip_every_s"] = 0                      # only the near end flips the test bit
    d["loopback_test"] = False
    if base.echo in ("near", "far"):
        d["echo"] = "far" if base.echo == "near" else "near"
    return d


def proto_conflict(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if a and b and a == b:
        return f"Both ends are set to PROTO {a}. The two ends of a Mirrored Bits channel must be opposite: one A and one B."
    return None


def id_conflict(a: dict, b: dict) -> Optional[str]:
    """My TX_ID must be your RX_ID and the other way round, otherwise every message is rejected as 'address'."""
    try:
        if a.get("tx_id") != b.get("rx_id") or a.get("rx_id") != b.get("tx_id"):
            return (f"The IDs do not match: this end sends ID {a.get('tx_id')} and expects {a.get('rx_id')}, the other end sends {b.get('tx_id')} and expects "
                    f"{b.get('rx_id')}. Each end's TX_ID must be the other end's RX_ID.")
    except AttributeError:
        pass
    return None
