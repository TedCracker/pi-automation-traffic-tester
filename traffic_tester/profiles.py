"""Test profiles: schema, validation, built-in presets, export/import (docs/SPEC.md section 6)."""
from __future__ import annotations

import copy
import datetime as dt
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import __version__

SCHEMA_VERSION = 1
FORMAT = "traffic-tester-profiles"
BAUD_MIN, BAUD_MAX = 300, 4_000_000
IDLE_GAPS_MAX_COUNT, IDLE_GAP_MAX_S = 12, 86400

NETEM_PRESETS: Dict[str, Dict[str, Any]] = {
    "Good cell": {"args": "delay 25ms 5ms distribution normal", "about": "Healthy 5G/LTE: ~25 ms added delay"},
    "Bad cell": {"args": "delay 60ms 25ms distribution normal loss 1%", "about": "Weak signal: 60 ms delay, wide jitter, 1 % loss"},
    "Handover": {"args": "delay 40ms 10ms loss gemodel 0.5% 30%", "about": "Tower hand-over: short bursts of loss"},
    "Burst loss": {"args": "loss gemodel 1% 25%", "about": "Clustered packet loss"},
    "Saturated": {"args": "delay 120ms 40ms rate 1mbit", "about": "Congested link: 120 ms delay, 1 Mbit/s"},
}


class Serial(BaseModel):
    port: str = "auto"
    baud: int = 19200
    bytesize: int = 8
    parity: Literal["N", "E", "O"] = "N"
    stopbits: Literal[1, 2] = 1

    @field_validator("baud")
    @classmethod
    def _baud(cls, v):
        if not BAUD_MIN <= v <= BAUD_MAX:
            raise ValueError(f"baud must be {BAUD_MIN}..{BAUD_MAX}")
        return v

    @field_validator("bytesize")
    @classmethod
    def _bs(cls, v):
        if v not in (5, 6, 7, 8):
            raise ValueError("bytesize must be 5-8")
        return v


class Net(BaseModel):
    peer_host: str = ""
    port: int = Field(20500, ge=1, le=65535)
    dscp: int = Field(46, ge=0, le=63)
    keepalive: bool = True
    keepalive_idle_s: int = Field(5, ge=1, le=600)
    iface: str = Field("", max_length=40)        # the sending Pi's network port for this test ("" / "auto" = the Pi's routing decides)


class Goose(BaseModel):
    """GOOSE parameters (docs/SPEC.md 5.5). Used when transport is goose_l2 / goose_l3."""
    interface: str = "auto"
    # L2
    dest_mac: str = "01:0c:cd:01:00:01"
    vlan: bool = True
    vlan_id: int = Field(0, ge=0, le=4094)
    vlan_prio: int = Field(4, ge=0, le=7)
    # L3
    group_a: str = "239.192.0.1"      # A -> B
    group_b: str = "239.192.0.2"      # B -> A (mirror)
    udp_port: int = Field(102, ge=1, le=65535)
    ttl: int = Field(16, ge=1, le=255)
    dscp: int = Field(46, ge=0, le=63)
    unicast_peer: str = ""            # non-standard fallback when multicast is dropped
    # common PDU fields
    appid_a: int = Field(1, ge=0, le=0xFFFF)
    appid_b: int = Field(2, ge=0, le=0xFFFF)
    gocb_ref_a: str = "TESTER_A/LLN0$GO$gcbTrip"
    gocb_ref_b: str = "TESTER_B/LLN0$GO$gcbMirror"
    dat_set_a: str = "TESTER_A/LLN0$TripBits"
    dat_set_b: str = "TESTER_B/LLN0$TripBits"
    go_id_a: str = "TesterA_Trip"
    go_id_b: str = "TesterB_Mirror"
    conf_rev: int = Field(1, ge=0)
    nds_com: bool = False
    test_flag: bool = False
    simulation: bool = False
    num_entries: int = Field(8, ge=1, le=64)
    # retransmission profile (IEC 61850-8-1): T1 burst doubling up to T0 heartbeat
    t1_ms: float = Field(4.0, gt=0)
    t0_ms: float = Field(1000.0, gt=0)
    multiplier: float = Field(2.0, ge=1.0)
    fixed_interval: bool = False
    # subscriber filters
    check_conf_rev: bool = True
    check_vlan: bool = False

    @field_validator("dest_mac")
    @classmethod
    def _mac(cls, v):
        parts = v.replace("-", ":").lower().split(":")
        if len(parts) != 6 or not all(len(p) == 2 and all(c in "0123456789abcdef" for c in p) for p in parts):
            raise ValueError("dest_mac must look like 01:0c:cd:01:00:01")
        return ":".join(parts)


DIRECTIONS = ("round_trip", "forward", "reverse", "both")
DIRECTION_NAMES = {"round_trip": "Round trip (the far end reflects each message back)", "forward": "One way: from \u2192 to",
                   "reverse": "One way: to \u2192 from", "both": "Both ways at once"}


class Endpoint(BaseModel):
    """One end of a test: a Pi from the list (id or name), or a typed address. Blank pi + blank host = the Pi you are using."""
    pi: str = Field("", max_length=64)           # "" / "self" = the Pi whose page you are on; otherwise a Pi id or name from Settings -> Pis
    host: str = Field("", max_length=253)        # an IP address or name typed by hand (for a Pi that is not in the list)
    name: str = Field("", max_length=64)         # last known name of that Pi (shown when it is offline)
    port: Optional[int] = Field(None, ge=1, le=65535)    # test port on that Pi (blank = the test's own port)
    serial_port: str = Field("", max_length=120)   # serial tests: that Pi's port ("" = the test's serial port, "auto" = first adapter)
    iface: str = Field("auto", max_length=40)     # network port used for raw / GOOSE / generator traffic ("auto" = first wired port)

    @field_validator("host")
    @classmethod
    def _host(cls, v):
        v = (v or "").strip()
        if v and not re.fullmatch(r"[A-Za-z0-9._:-]{1,253}", v):
            raise ValueError("use an IP address like 192.168.1.50 or a name like pi2.local")
        return v

    @property
    def is_self(self) -> bool:
        return not self.host and self.pi in ("", "self")


class Mirrored(BaseModel):
    """Mirrored Bits emulator (docs/MIRRORED-BITS-SPEC.md): both Pis send a full message every cycle and check every message they receive.
    `settings` holds the per-channel settings of THIS end (PROTO, TX_ID/RX_ID, counts, thresholds ... see mbemu.status.Settings); the answering Pi gets
    the mirror image automatically.  `impair` is the in-app impairment injector at the start of the test (it can be changed live)."""
    rate_baud: int = 19200            # message speed over UDP / TCP (over serial the serial baud is used): 9600 / 19200 / 38400 like a relay channel
    codec: str = "sim"                # SimCodec only: emulator-to-emulator, not SEL wire-compatible
    settings: Dict[str, Any] = Field(default_factory=dict)
    impair: Dict[str, Any] = Field(default_factory=dict)
    fail_after: int = Field(3, ge=1, le=1000)          # OLD (first Mirrored Bits mode): kept so older saved tests still open; no longer used
    trip_every_s: float = Field(2.0, ge=0.1, le=3600)  # the starting Pi flips the trip bit (TMB1) this often: the bit-transfer-time probe
    max_down_pct: float = Field(0.1, ge=0, le=100)     # pass limit: share of time ROK may be 0
    peer_api: str = ""                # optional: the other Pi's web address (older tests); the Pi picked as "Send to" is used otherwise
    peer_password: str = ""

    @field_validator("rate_baud")
    @classmethod
    def _rb(cls, v):
        if not BAUD_MIN <= v <= BAUD_MAX:
            raise ValueError(f"baud must be {BAUD_MIN}..{BAUD_MAX}")
        return v

    @field_validator("codec")
    @classmethod
    def _codec(cls, v):
        from .mbemu import CODECS
        if v not in CODECS:
            raise ValueError(f"unknown codec '{v}'")
        return v

    @field_validator("settings")
    @classmethod
    def _settings(cls, v):
        from .mbemu import Settings
        Settings.from_dict(v)               # raises ValueError with a plain message
        return v

    @field_validator("impair")
    @classmethod
    def _impair(cls, v):
        from .mbemu import Impairer
        Impairer().set(v)
        return v


class Rule(BaseModel):
    percentile: Literal[50, 95, 99, 100] = 99
    max_loss_pct: float = Field(0.1, ge=0, le=100)
    include_idle: bool = True
    idle_threshold_ms: Optional[float] = Field(None, gt=0)


class GenRate(BaseModel):
    pps: Optional[float] = Field(None, gt=0, le=5_000_000)
    mbps: Optional[float] = Field(None, gt=0, le=10_000)

    @model_validator(mode="after")
    def _one(self):
        if (self.pps is None) == (self.mbps is None):
            raise ValueError("give a rate as pps OR mbps")
        return self


class GenSize(BaseModel):
    mode: Literal["fixed", "range", "imix"] = "fixed"
    size: int = Field(1000, ge=24, le=9000)
    min: int = Field(64, ge=24, le=9000)
    max: int = Field(1518, ge=24, le=9000)


class GenStream(BaseModel):
    name: str = Field("Stream", min_length=1, max_length=60)
    type: Literal["l2", "ip", "udp", "tcp"] = "udp"
    direction: Literal["a_to_b", "b_to_a"] = "a_to_b"      # older tests: the test's "from" Pi sends (a_to_b) or the "to" Pi sends (b_to_a)
    src: Endpoint = Endpoint()                              # sending Pi; blank = taken from `direction`
    dst: Endpoint = Endpoint()                              # receiving Pi; blank = taken from `direction`
    rate: GenRate = GenRate(mbps=10)
    size: GenSize = GenSize()
    burst_packets: int = Field(1, ge=1, le=1000)
    burst_gap_ms: float = Field(0, ge=0, le=60_000)
    duration_s: Optional[float] = Field(None, gt=0, le=7 * 24 * 3600)
    packets: Optional[int] = Field(None, ge=1)
    start_offset_s: float = Field(0, ge=0, le=86400)
    rtt_probe_pps: float = Field(10, ge=0, le=1000)
    dscp: int = Field(0, ge=0, le=63)
    ttl: int = Field(64, ge=1, le=255)
    dst_port: Optional[int] = Field(None, ge=1, le=65535)
    dst_mac: str = ""
    src_mac: str = ""
    ethertype: int = Field(0x88B5, ge=0x0600, le=0xFFFF)
    vlan: bool = False
    vlan_id: int = Field(0, ge=0, le=4094)
    vlan_prio: int = Field(0, ge=0, le=7)
    ip_proto: int = Field(253, ge=0, le=255)
    seed: int = 1

    @field_validator("dst_mac", "src_mac")
    @classmethod
    def _mac(cls, v):
        if not v:
            return v
        p = v.replace("-", ":").lower().split(":")
        if len(p) != 6 or not all(len(x) == 2 and all(c in "0123456789abcdef" for c in x) for x in p):
            raise ValueError("MAC must look like 02:00:00:00:00:01")
        return ":".join(p)


class GenRule(BaseModel):
    max_loss_pct: Optional[float] = Field(0.1, ge=0, le=100)
    max_latency_ms: Optional[float] = Field(None, gt=0)
    latency_percentile: Literal[50, 95, 99, 100] = 99
    max_jitter_ms: Optional[float] = Field(None, gt=0)
    min_throughput_mbps: Optional[float] = Field(None, ge=0)
    min_clean_mbps: Optional[float] = Field(None, gt=0, le=10_000)      # Throughput in steps: "needs at least N Mb/s clean"


GEN_STEP_FRACTIONS = [0.125, 0.25, 0.5, 0.75, 1.0]


class GenSteps(BaseModel):
    """Throughput in steps (docs/SPEC.md 13.9): the same traffic at a rising share of a top rate, a fixed time per step; a step is
    CLEAN when its loss and delay are inside the test's pass limits. Off = one rate for the whole test (Flood / fixed rate), as before."""
    enabled: bool = False
    top_mbps: Optional[float] = Field(None, gt=0, le=10_000)            # None = Auto: a short flood measures what the link carries first
    fractions: List[float] = Field(default_factory=lambda: list(GEN_STEP_FRACTIONS), min_length=1, max_length=10)
    step_s: float = Field(10, ge=1, le=600)
    stop_after_fail: int = Field(2, ge=1, le=10)                        # stop after this many failed steps in a row (saves cellular data)
    flood_s: float = Field(5, ge=1, le=60)                              # Auto: how long the measuring flood lasts
    flood_mbps: float = Field(1000, gt=0, le=10_000)                    # Auto: the rate the flood is sent at (the Pi sends what it can, up to this)

    @field_validator("fractions")
    @classmethod
    def _fr(cls, v):
        if any(not (0 < f <= 1) for f in v) or any(b <= a for a, b in zip(v, v[1:])):
            raise ValueError("steps must be rising shares of the top rate, each above 0 and at most 1 (for example 0.125, 0.25, 0.5, 0.75, 1)")
        return v


class GenCfg(BaseModel):
    streams: List[GenStream] = Field(default_factory=lambda: [GenStream(name="Stream 1")], min_length=1, max_length=16)
    duration_s: float = Field(30, gt=0, le=7 * 24 * 3600)
    interface: str = "auto"                 # this Pi's port for raw (l2) streams
    peer_interface: str = "auto"            # the other Pi's port
    peer_api: str = ""                      # http://<address of the Pi you send to>:8080 (control channel)
    peer_password: str = ""
    local_host: str = ""                    # this Pi's data address (for streams B -> A); blank = from the port
    rule: GenRule = GenRule()
    steps: GenSteps = GenSteps()

    @model_validator(mode="after")
    def _steps_ok(self):
        if self.steps.enabled and any(s.burst_gap_ms > 0 for s in self.streams):
            raise ValueError("Throughput in steps needs a steady rate: set the gap between bursts to 0, or switch the method to Flood.")
        if self.steps.enabled and any(s.packets for s in self.streams):
            raise ValueError("Throughput in steps runs every stream for the length of a step: clear \"Or stop after (packets)\", or switch the method to Flood.")
        return self


class SniffEth(BaseModel):
    interface: str = "auto"                # SPAN / mirror port interface
    leg_a: str = "auto"                    # inline tap: first port (auto = first wired port)
    leg_b: str = "auto"                    # inline tap: second port (auto = first USB Ethernet adapter)
    capture_on: Literal["bridge", "both_legs"] = "bridge"
    link_passthrough: bool = False


class SniffChannel(BaseModel):
    name: str = Field("Serial line", min_length=1, max_length=40)
    port: str = ""
    baud: int = Field(9600, ge=300, le=4_000_000)
    bytesize: int = Field(8, ge=5, le=8)
    parity: Literal["N", "E", "O"] = "N"
    stopbits: Literal[1, 2] = 1


class SniffSerial(BaseModel):
    channels: List[SniffChannel] = Field(default_factory=lambda: [SniffChannel(name="Master to outstation"), SniffChannel(name="Outstation to master")], min_length=1, max_length=2)
    protocol: Literal["modbus_rtu", "dnp3", "raw"] = "modbus_rtu"
    gap_ms: Optional[float] = Field(None, gt=0, le=1000)
    also_user_dlt: bool = False


class SniffWifi(BaseModel):
    interface: str = "auto"                 # auto = first USB adapter with monitor mode; "builtin" = the Pi's own chip via Nexmon (experimental)
    builtin_ack: bool = False               # explicit OK: Wi-Fi (and internet over it) is off while sniffing with the built-in chip
    channel: int = Field(6, ge=1, le=233)
    width: Literal["HT20", "HT40+", "HT40-", "80MHz"] = "HT20"
    hop: bool = False
    channels: List[int] = Field(default_factory=lambda: [1, 6, 11, 36, 40, 44, 48, 149, 153, 157, 161])
    dwell_s: float = Field(2.0, ge=0.2, le=60)


class SniffFilter(BaseModel):
    bpf: str = Field("", max_length=600)
    preset: str = ""


class SniffStorage(BaseModel):
    ring_mb: int = Field(100, ge=0, le=100_000)
    ring_minutes: float = Field(0, ge=0, le=100_000)
    ring_files: int = Field(10, ge=0, le=10_000)
    usb: bool = False
    min_free_mb: int = Field(50, ge=1, le=1_000_000)


class SniffCfg(BaseModel):
    mode: Literal["eth_bridge", "eth_span", "serial", "wifi"] = "eth_span"
    eth: SniffEth = SniffEth()
    serial: SniffSerial = SniffSerial()
    wifi: SniffWifi = SniffWifi()
    filter: SniffFilter = SniffFilter()
    storage: SniffStorage = SniffStorage()
    duration_s: float = Field(0, ge=0, le=7 * 24 * 3600)      # 0 = until stopped
    snaplen: int = Field(0, ge=0, le=262144)


class Info(BaseModel):
    """Plain-English explanation shown when a test card is tapped (the same text feeds the AI guide)."""
    what: str = Field("", max_length=1200)          # What it tests
    how: str = Field("", max_length=1600)           # How it works
    need: str = Field("", max_length=1200)          # What you need
    pass_means: str = Field("", max_length=1200)    # What "pass" means
    typical: str = Field("", max_length=1200)       # Typical use


INFO_SECTIONS = [("what", "What it tests"), ("how", "How it works"), ("need", "What you need"), ("pass_means", "What \u201cpass\u201d means"), ("typical", "Typical use")]


class Profile(BaseModel):
    schema_version: int = SCHEMA_VERSION
    name: str = Field(..., min_length=1, max_length=80)
    description: str = Field("", max_length=2000)
    info: Optional[Info] = None
    builtin: bool = False
    kind: Literal["trip", "generator", "sniffer"] = "trip"
    gen: GenCfg = GenCfg()
    sniff: SniffCfg = SniffCfg()
    transport: Literal["serial", "udp", "tcp", "goose_l2", "goose_l3", "generator", "sniffer"] = "serial"
    source: Endpoint = Endpoint()                       # who sends (blank = the Pi you are using)
    dest: Endpoint = Endpoint()                         # who receives / reflects (blank = the address in net.peer_host, as older tests had it)
    dests: List[Endpoint] = Field(default_factory=list, max_length=7)      # MORE destinations (one -> many): every source sends to `dest` and to each of these
    sources: List[Endpoint] = Field(default_factory=list, max_length=7)    # MORE sources (many -> one): `source` and each of these send
    direction: Literal["round_trip", "forward", "reverse", "both"] = "round_trip"
    leg_info: Optional[Dict[str, Any]] = None           # set by the planner on the test a Pi actually runs (who sends, who reflects, what is judged); never edited by hand
    mode: Literal["echo", "mirrored"] = "echo"          # echo = the source sends and the destination reflects (all older tests); mirrored = both ends send continuously
    mirrored: Mirrored = Mirrored()
    serial: Serial = Serial()
    net: Net = Net()
    goose: Goose = Goose()
    frame: Literal["compact", "extended"] = "compact"
    interval_ms: float = Field(50, ge=1, le=60000)
    duration_s: float = Field(600, ge=1, le=7 * 24 * 3600)
    timeout_ms: float = Field(1000, ge=2)
    threshold_ms: float = Field(60, gt=0)
    idle_gaps_s: List[float] = Field(default_factory=list)
    idle_repeats: int = Field(3, ge=1, le=100)
    idle_mode: Literal["after", "interleaved"] = "after"
    one_way: bool = False
    max_clock_offset_ms: float = Field(2.0, gt=0)
    rule: Rule = Rule()
    impairment: Optional[str] = None
    impair_iface: str = ""                                # interface netem is applied to (blank = auto)
    summary_interval_s: float = Field(1.0, ge=0.1, le=3600)
    load_mbps: float = Field(0.0, ge=0, le=1000)       # latency-under-load background traffic (UDP)

    @field_validator("idle_gaps_s")
    @classmethod
    def _gaps(cls, v):
        if len(v) > IDLE_GAPS_MAX_COUNT or any(g <= 0 or g > IDLE_GAP_MAX_S for g in v):
            raise ValueError(f"idle gaps must be 1-{IDLE_GAPS_MAX_COUNT} values between 0 and {IDLE_GAP_MAX_S} s")
        return v

    @field_validator("impairment")
    @classmethod
    def _imp(cls, v):
        if v is not None and v not in NETEM_PRESETS:
            raise ValueError(f"unknown impairment preset '{v}'")
        return v

    @model_validator(mode="after")
    def _cross(self):
        if self.kind == "generator":
            self.transport = "generator"
            self.frame = "extended"
            return self
        if self.kind == "sniffer":
            self.transport = "sniffer"
            return self
        if self.timeout_ms <= self.threshold_ms:
            raise ValueError("timeout_ms must be larger than threshold_ms")
        if self.direction == "both" and self.transport in ("serial", "goose_l2", "goose_l3"):
            raise ValueError("A serial port or GOOSE interface cannot send and reflect at the same time: use Round trip or One way.")
        if self.direction in ("forward", "reverse"):
            self.one_way = True                         # judged on the one-way delay: needs the extended frame with timestamps
        if self.dests or self.sources:
            if self.mode == "mirrored":
                raise ValueError("Mirrored Bits is always one pair of Pis. For a second pair, start another test.")
            if self.transport == "serial":
                raise ValueError("A serial port reaches one far end. Pick one destination (use another serial port for a second link).")
            if self.transport in ("goose_l2", "goose_l3"):
                raise ValueError("A GOOSE test runs between two Pis. Pick one destination.")
        if self.mode == "mirrored":
            self.direction = "round_trip"
            if self.transport not in ("serial", "udp", "tcp"):
                raise ValueError("Mirrored Bits mode runs over a serial, UDP or TCP link (not GOOSE).")
            self.idle_gaps_s = []
        if self.one_way or self.transport in ("goose_l2", "goose_l3"):
            self.frame = "extended"
        return self


def validate_profile(data: dict) -> dict:
    """Return a normalised profile dict, or raise ValueError with a plain message."""
    try:
        return Profile.model_validate(data).model_dump()
    except ValidationError as e:
        msgs = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            msgs.append(f"{loc}: {err['msg']}")
        raise ValueError("; ".join(msgs))


def _preset(name, description, **kw) -> dict:
    if name in TRIP_INFO:
        kw.setdefault("info", TRIP_INFO[name])                 # the plain-English panel lives with the definition (one source: app, AI guide)
    return validate_profile({"name": name, "description": description, "builtin": True, **kw})


# ---------------------------------------------------------------- built-in tests can be edited: defaults stay in code, edits are an OVERRIDE
# An override is only the fields that differ from the default (deep), so an app update that improves a default still reaches every field
# the user did not touch, a reset always works, and an edit is never lost by an update.
def preset_key(default_name: str) -> str:
    """Stable id of a built-in test = its ORIGINAL name, so renaming it never breaks the link to its default."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", default_name.lower()).strip("-")


_NOT_SETTINGS = {"leg_info", "id", "builtin", "schema_version", "preset_key", "edited", "hidden", "position", "default_name", "override_error"}


def diff_profile(default: dict, eff: dict) -> dict:
    out: dict = {}
    for k, v in eff.items():
        if k in _NOT_SETTINGS:
            continue
        d = default.get(k)
        if isinstance(v, dict) and isinstance(d, dict):
            sub = diff_profile(d, v)
            if sub:
                out[k] = sub
        elif v != d:
            out[k] = copy.deepcopy(v)
    return out


def merge_profile(default: dict, override: Optional[dict]) -> dict:
    out = copy.deepcopy(default)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_profile(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def builtin_presets() -> List[dict]:
    return _trip_presets() + generator_presets() + sniffer_presets()


TRIP_INFO = {
 "DER trip test": {
  "what": "How quickly a small “trip” message gets across a serial link and back. It checks that a protection signal, like a breaker trip sent to a distributed energy resource (DER), arrives inside the time limit.",
  "how": "The sending Pi (the one you press Start on, unless you Swap in From → To) sends a 7-byte message every 50 ms down the serial cable or radio. The Pi you send to sends each one straight back and the sending Pi times every round trip. After the main run it also goes quiet for 5 s, 30 s and 2 min, then sends one message, to see how long the first message takes when the link has been idle (a radio can be slow to wake up). Runs 10 minutes at 19200 baud (bits per second).",
  "need": "A USB-serial adapter (or the Pi's serial pins) on each Pi, joined by a null-modem cable for a bench test or through the radios' serial ports. Tick the Pi that answers under From → To (⇅ Swap if it should send); the answering Pi needs nothing set up, but its serial port must use the same speed and is busy while the test runs. The clocks only matter for the one-way directions.",
  "pass_means": "Pass = 99 out of every 100 round trips took 60 ms or less, and no more than 0.1 % of messages were lost. The first message after each quiet period must also be inside the limit. One bad message in a thousand is allowed; a pattern of slow ones is not.",
  "typical": "Proving a serial path over an 5G router or FirstNet radio can carry a DER trip signal inside 60 ms before it is used in the field."
 },
 "DER trip test (IP)": {
  "what": "The same trip-message timing as the serial DER trip test, but over the radio's Ethernet port instead of a serial cable.",
  "how": "The sending Pi sends a small message every 50 ms as a UDP packet (a simple network message with no retries) to the Pi you send to. That Pi sends each one straight back and the sending Pi times the round trip. It also tests the first message after 5 s, 30 s and 2 min of quiet. Runs 10 minutes.",
  "need": "Both Pis on the same network or on each side of the radio link. Tick the Pi to send to under From → To (or type its IP address); tick several to test them all at once. The answering Pi needs nothing set up: every Pi always answers UDP and TCP on port 20500. Firewalls must let that port through. Pick One way, Round trip or Both ways, and ⇅ Swap to make the far Pi send.",
  "pass_means": "Pass = 99 out of every 100 round trips took 60 ms or less, no more than 0.1 % of messages were lost, and the first message after each quiet period was also inside the limit.",
  "typical": "Checking an IP path (Ethernet through a cellular router, a private LTE/5G link or a fibre ring) before relying on it for protection or SCADA traffic."
 },
 "Quick link check": {
  "what": "A one-minute first look at a link: is it up, how fast is it, does it drop anything.",
  "how": "The sending Pi sends a small UDP message every 20 ms for one minute. The Pi you send to sends each one back and the sending Pi times the round trip. It does not test quiet periods, so it finishes fast.",
  "need": "Another Pi on the network to send to (pick it from the list or type its IP address). It always answers UDP on port 20500, so nothing needs setting up on it.",
  "pass_means": "Pass = 99 out of every 100 round trips took 60 ms or less and no more than 0.1 % of messages were lost.",
  "typical": "The first thing to run after plugging in a new radio or changing a setting, before starting a long test."
 },
 "TCP session test": {
  "what": "How a long-lived TCP connection behaves: how fast it sets up, how it copes with quiet periods and how quickly it recovers when the connection drops. TCP is the “reliable” network protocol that SCADA and many relays use.",
  "how": "The sending Pi opens one TCP connection to the Pi you send to and sends a small message every 50 ms for 10 minutes, timing each round trip. It also times the connection set-up. After the main run it goes quiet for 5 s, 30 s and 2 min, then sends one message, to catch radios or firewalls that put idle connections to sleep. If the connection drops, it reconnects and records how long the outage lasted.",
  "need": "Another Pi to send to (pick it or type its IP address); every Pi always answers TCP on port 20500. For a fair result on Wi-Fi, switch off Wi-Fi power saving (the app offers a one-click fix).",
  "pass_means": "Pass = 99 out of every 100 round trips took 60 ms or less, no more than 0.1 % were lost, and the first message after each quiet period was also inside the limit. Reconnect times are shown but do not decide pass or fail.",
  "typical": "Checking that a SCADA or DNP3-over-TCP session will stay up and answer quickly across a cellular link that goes quiet between polls."
 },
 "Overnight soak": {
  "what": "A 12-hour serial test that shows rare problems a short test misses: occasional slow messages, drop-outs, radios that need to re-register overnight.",
  "how": "The sending Pi sends a small message every 100 ms for 12 hours over the serial link; the Pi you send to sends each back. During the night it pauses for 5 s, 30 s, 2 min and 10 min, three times each, then sends one message to time the wake-up. Results are summarised every 10 s so the report shows exactly when anything went wrong.",
  "need": "A serial link set up as for the DER trip test, a Pi that will not be unplugged or put to sleep for 12 hours, and a stable power supply. Start it in the evening.",
  "pass_means": "Pass = 99 out of every 100 round trips took 60 ms or less, no more than 0.1 % were lost, and every first message after a quiet period (up to 10 minutes) was inside the limit.",
  "typical": "Acceptance testing a new link: leave it running overnight and read the result in the morning."
 },
 "Back-to-back baseline": {
  "what": "How much delay the two Pis and their adapters add by themselves. You run it with a short cable and no radio, so the answer is the equipment's own overhead.",
  "how": "The two Pis are joined directly by a null-modem cable. The sending Pi sends a small message every 20 ms at 115200 baud for one minute; the other Pi sends each one back. Anything over a few milliseconds points at a slow adapter setting, not at the network.",
  "need": "One null-modem serial cable (or two USB-serial adapters wired to each other) between the Pis, with the same speed (115200 baud) on both. Tick the Pi that answers under From → To (⇅ Swap if it should send).",
  "pass_means": "Pass = 99 out of every 100 round trips took 10 ms or less and none were lost. This is a stricter limit than the real tests because there is no radio in the way.",
  "typical": "Run it once before the real link tests so you know the Pis are not the reason a later result looks slow."
 },
 "GOOSE trip test - L2": {
  "what": "How fast a Layer-2 GOOSE message (the IEC 61850 substation message used for fast protection signals) crosses the link and returns.",
  "how": "The sending Pi sends a GOOSE frame, a special Ethernet frame with VLAN priority 4, every 500 ms. The Pi you send to sends a matching GOOSE frame back and the sending Pi times the round trip. Also tests the first message after 5 s and 30 s of quiet. Runs 5 minutes. The app does not build a tunnel: the router in between must carry the Layer-2 traffic.",
  "need": "Both Pis on the same Layer-2 network (or a tunnel such as GRE that carries Ethernet frames between them), and the Ethernet port chosen on each Pi (row 1 Port on this Pi, More options for the far Pi). The answering Pi listens for this test only while it runs. The full installed app is needed because GOOSE uses raw network access.",
  "pass_means": "Pass = 99 out of every 100 round trips took 60 ms or less and no more than 0.1 % were lost; the first message after each quiet period must also be inside the limit.",
  "typical": "Checking that GOOSE-based protection signals survive an cellular router GRE tunnel or a switched network."
 },
 "GOOSE trip test - L3 multicast": {
  "what": "The same GOOSE round-trip timing, but using routed GOOSE (R-GOOSE) that travels across IP networks as multicast.",
  "how": "The sending Pi sends a routed-GOOSE message in a UDP multicast packet every 500 ms; the Pi you send to sends one back on a second multicast address and the sending Pi times the round trip. Also tests the first message after 5 s and 30 s of quiet. Runs 5 minutes.",
  "need": "Both Pis on a network that routes multicast between them (check with your network team), and the Ethernet port chosen on each Pi (row 1 Port on this Pi, More options for the far Pi). The answering Pi mirrors the messages only while the test runs. If the carrier drops multicast, you can enter a unicast fallback address in the settings.",
  "pass_means": "Pass = 99 out of every 100 round trips took 60 ms or less and no more than 0.1 % were lost; the first message after each quiet period must also be inside the limit.",
  "typical": "Checking that routed GOOSE reaches the other site across a cellular or WAN link."
 }
}


TRIP_INFO["DER trip \u2014 Mirrored Bits mode"] = {
    "what": "A pair of SEL-2505-style Mirrored Bits units, one at each end of the link, to see whether Mirrored Bits survives it. Both ends send a full message nonstop and check every message they receive; the test times how long a bit change takes to cross, and whether the channel ever drops. It uses our own message format (SimCodec), so it is emulator-to-emulator only and NOT wire-compatible with SEL relays.",
    "how": "Each Pi has 8 transmit bits (TMB1-8) and 8 receive bits (RMB1-8). Every send cycle each Pi sends all 8 bits (three copies of each, plus an ID), back to back at the channel speed, whether or not anything changed. Each Pi checks every message (characters, the three copies, the ID, how late it came). A bad, late or missing message puts ROK to 0 at once and the received bits fall back to their defaults; ROK returns after a few good messages. RBAD asserts when ROK stays 0 for a while, CBAD when ROK was 0 too much of a rolling window. One bit (TMB8) is an echo: one end copies it back and the other flips it each time it returns, so every flip is a round trip (one way is about half). Every 2 s the starting Pi also flips TMB1; with both clocks synced the time until it shows at the other Pi is measured one way. The live screen shows both ends, the message stream, and lets you flip bits, break the link or add delay, jitter, drops and bit errors. Runs 10 minutes.",
    "need": "The same serial setup as the DER trip test (USB-serial adapter or the Pi's serial pins on each Pi, null-modem cable or the radios' serial ports), with the same speed on both. The Pi you send to joins in for the length of the test and is set up as the mirror image (PROTO B, IDs swapped). Also works over UDP or TCP for IP-tunnelled radios. No second Pi? The Run screen has an internal self-test.",
    "pass_means": "Pass = 99 out of every 100 bit changes crossed in 60 ms or less, no more than 0.1 % of them were lost, ROK was 0 for no more than 0.1 % of the time, and RBAD never asserted. The result also shows availability, the longest outage, the rejected messages by reason, the echo round trip and an inter-arrival histogram.",
    "typical": "Proving that a serial-over-radio link (5G router, FirstNet, private LTE) carries Mirrored Bits the way a relay pair needs it: continuous traffic, a bit change inside 60 ms, and no false channel failures.",
}


def _trip_presets() -> List[dict]:
    idle = [5, 30, 120]
    return [
        _preset("DER trip test",
                "Emulates a Mirrored Bits trip signal over a serial link: a small message every 50 ms for 10 minutes, "
                "plus 'first message after quiet' checks. Pass = 99 % of messages arrive within 60 ms.",
                transport="serial", serial={"baud": 19200}, frame="compact", interval_ms=50, duration_s=600,
                threshold_ms=60, idle_gaps_s=idle, idle_repeats=3, rule={"percentile": 99}),
        _preset("DER trip — Mirrored Bits mode",
                "A pair of SEL-2505-style Mirrored Bits emulators, one at each end: full messages nonstop, ROK / RBAD / CBAD, and the time for a bit change to cross. "
                "Pass = 99 % of bit changes within 60 ms, ROK down no more than 0.1 % of the time, no RBAD. SimCodec (our own format): emulator-to-emulator only, not compatible with SEL relays.",
                transport="serial", serial={"baud": 19200}, mode="mirrored", frame="compact", interval_ms=50, duration_s=600,
                threshold_ms=60, rule={"percentile": 99}),
        _preset("DER trip test (IP)",
                "Same trip-signal timing over the radio's Ethernet port (UDP).",
                transport="udp", frame="extended", interval_ms=50, duration_s=600, threshold_ms=60,
                idle_gaps_s=idle, idle_repeats=3),
        _preset("Quick link check", "One minute over UDP, a message every 20 ms. Good for a first look at a new link.",
                transport="udp", frame="extended", interval_ms=20, duration_s=60, threshold_ms=60),
        _preset("TCP session test",
                "Keeps one TCP session open, times the connection set-up and how fast it recovers after a drop.",
                transport="tcp", frame="extended", interval_ms=50, duration_s=600, threshold_ms=60,
                idle_gaps_s=idle, idle_repeats=2),
        _preset("Overnight soak",
                "12-hour serial test with quiet-period checks (up to 10 minutes) mixed in. Summaries every 10 s.",
                transport="serial", serial={"baud": 19200}, interval_ms=100, duration_s=12 * 3600, threshold_ms=60,
                idle_gaps_s=[5, 30, 120, 600], idle_repeats=3, idle_mode="interleaved", summary_interval_s=10),
        _preset("Back-to-back baseline",
                "Two Pis joined by a null-modem cable (no radio). Measures the delay added by the Pis and adapters themselves.",
                transport="serial", serial={"baud": 115200}, interval_ms=20, duration_s=60, threshold_ms=10,
                timeout_ms=200, rule={"percentile": 99, "max_loss_pct": 0}),
        _preset("GOOSE trip test - L2",
                "Layer-2 GOOSE (Ethernet, VLAN priority 4) event round trip across the link. Pass = 99 % within 60 ms.",
                transport="goose_l2", interval_ms=500, duration_s=300, threshold_ms=60, frame="extended",
                idle_gaps_s=[5, 30], idle_repeats=3),
        _preset("GOOSE trip test - L3 multicast",
                "Layer-3 routed GOOSE (R-GOOSE over UDP multicast) event round trip. Pass = 99 % within 60 ms.",
                transport="goose_l3", interval_ms=500, duration_s=300, threshold_ms=60, frame="extended",
                idle_gaps_s=[5, 30], idle_repeats=3),
    ]


def _gen(name, description, streams, duration_s=30, steps=None, **rule):
    return validate_profile({"name": name, "description": description, "builtin": True, "kind": "generator", "duration_s": 3600,
                             "gen": {"streams": streams, "duration_s": duration_s, "rule": rule, **({"steps": steps} if steps else {})}})


def generator_presets() -> List[dict]:
    S = lambda name, **k: {"name": name, **k}
    return [
        _gen("Mirrored-Bits-like small frames", "A small message 200 times a second, like a protection signal, with round-trip probes. Shows delay and loss of tiny packets.",
             [S("Trip-like UDP", type="udp", rate={"pps": 200}, size={"mode": "fixed", "size": 80}, rtt_probe_pps=20)], 60,
             max_loss_pct=0.1, max_latency_ms=60, max_jitter_ms=10),
        _gen("SCADA / DNP3 polling", "Small polling requests and responses over TCP, both directions, like a SCADA master polling a remote site.",
             [S("Poll request (from → to)", type="tcp", rate={"pps": 20}, size={"mode": "range", "min": 40, "max": 250}),
              S("Poll response (to → from)", type="tcp", direction="b_to_a", rate={"pps": 20}, size={"mode": "range", "min": 80, "max": 600})], 60,
             max_loss_pct=0.5, max_latency_ms=100, latency_percentile=95),
        _gen("Video / CCTV stream", "A steady 4 Mb/s camera-style stream of 1,358-byte packets in small bursts.",
             [S("Camera stream", type="udp", rate={"mbps": 4}, size={"mode": "fixed", "size": 1358}, burst_packets=7, rtt_probe_pps=5)], 60,
             max_loss_pct=0.5, max_jitter_ms=30, min_throughput_mbps=3.8),
        _gen("Fill a 5G link", "Pushes 50 Mb/s of large packets to see how much the link carries, with a small trip-like stream alongside to show delay under load.",
             [S("Bulk load", type="udp", rate={"mbps": 50}, size={"mode": "fixed", "size": 1400}, rtt_probe_pps=20),
              S("Trip-like (under load)", type="udp", rate={"pps": 100}, size={"mode": "fixed", "size": 80}, rtt_probe_pps=20)], 30,
             max_loss_pct=1, max_latency_ms=100),
        _gen("Throughput (find the clean rate)", "How much does the link carry cleanly? A 5-second flood measures the top rate, then the same traffic is sent in 5 steps (1/8, 1/4, 1/2, 3/4 and all of it), 10 seconds each. The result is the highest rate with loss inside the limit.",
             [S("Throughput", type="udp", rate={"mbps": 10}, size={"mode": "fixed", "size": 1400}, rtt_probe_pps=10)], 55,
             steps={"enabled": True}, max_loss_pct=0.1),
        _gen("IMIX (mixed packet sizes)", "20 Mb/s of the classic internet mix (64 / 594 / 1,514-byte frames): mostly small packets with some medium and large ones.",
             [S("IMIX", type="udp", rate={"mbps": 20}, size={"mode": "imix"}, rtt_probe_pps=10)], 30, max_loss_pct=0.5),
        _gen("Burst stress", "Bursts of 50 packets back to back every 20 ms. Finds the point where the link starts dropping.",
             [S("Bursts", type="udp", rate={"mbps": 10}, size={"mode": "fixed", "size": 1000}, burst_packets=50, burst_gap_ms=20, rtt_probe_pps=10)], 30,
             max_loss_pct=1),
        _gen("L2 priority frames", "Raw Ethernet frames with VLAN priority 5, to check that priority tags survive the path (needs the Pis on the same Layer-2 network).",
             [S("L2 priority", type="l2", rate={"pps": 500}, size={"mode": "fixed", "size": 128}, vlan=True, vlan_prio=5, rtt_probe_pps=20)], 30,
             max_loss_pct=0.1, max_latency_ms=60),
    ]


def _sniff(name, description, **cfg):
    return validate_profile({"name": name, "description": description, "builtin": True, "kind": "sniffer", "sniff": cfg})


def sniffer_presets() -> List[dict]:
    return [
        _sniff("Inline Ethernet tap", "Put this Pi between two devices (built-in port + a USB Ethernet adapter). Traffic passes straight through while every frame is saved for Wireshark. Open this page over Wi-Fi: the two wired ports are busy.",
               mode="eth_bridge", storage={"ring_mb": 100, "ring_files": 10}),
        _sniff("SPAN port", "Plug a switch's mirror (SPAN) port into this Pi and capture everything the switch copies to it. Nothing is sent back.",
               mode="eth_span", storage={"ring_mb": 100, "ring_files": 10}),
        _sniff("RS-485 Modbus listen", "Listen to a Modbus RTU RS-485 bus with one USB-RS485 adapter. Shows each request and reply decoded, with CRC checks.",
               mode="serial", serial={"protocol": "modbus_rtu", "channels": [{"name": "RS-485 bus", "baud": 9600, "parity": "N", "stopbits": 1}]}, storage={"ring_mb": 50, "ring_files": 10}),
        _sniff("RS-232 DNP3 tap", "Passive tap on an RS-232 DNP3 link with two USB-RS232 adapters, one per direction. Decodes DNP3 and checks every CRC.",
               mode="serial", serial={"protocol": "dnp3", "channels": [{"name": "Master to outstation", "baud": 9600}, {"name": "Outstation to master", "baud": 9600}]}, storage={"ring_mb": 50, "ring_files": 10}),
        _sniff("Wi-Fi channel scan", "Listens on one Wi-Fi channel after another and lists the networks it hears: name, channel and signal. Needs a USB Wi-Fi adapter with monitor mode.",
               mode="wifi", wifi={"hop": True, "channels": [1, 6, 11, 36, 40, 44, 48, 149, 153, 157, 161], "dwell_s": 3}, duration_s=120, storage={"ring_mb": 50, "ring_files": 5}),
        _sniff("Wi-Fi monitor, Pi's built-in Wi-Fi (experimental)", "Uses the Pi 5's own Wi-Fi chip (Nexmon) on one channel. Wi-Fi - and internet over Wi-Fi - goes off while it runs; normal Wi-Fi is restored when you stop. A USB Wi-Fi adapter is the reliable option.",
               mode="wifi", wifi={"interface": "builtin", "channel": 6, "hop": False}, duration_s=300, storage={"ring_mb": 50, "ring_files": 5}),
    ]


# ---------------------------------------------------------------- custom-test form (Setup -> Create custom test)
# The form shows the allowed ranges and validates while typing. The ranges are READ FROM THE SCHEMA above (never copied by hand),
# so the form and the server cannot disagree; the server still validates every save.
_FORM_FIELDS = {
    "": ["name", "interval_ms", "duration_s", "timeout_ms", "threshold_ms", "idle_repeats", "load_mbps", "max_clock_offset_ms"],
    "rule": ["max_loss_pct", "idle_threshold_ms"],
    "net": ["port", "dscp", "keepalive_idle_s"],
    "mirrored": ["fail_after", "trip_every_s", "max_down_pct"],
    "goose": ["vlan_id", "vlan_prio", "udp_port", "ttl", "dscp"],
}


def _bounds(model, name) -> Dict[str, Any]:
    f = model.model_fields[name]
    out: Dict[str, Any] = {}
    for m in f.metadata:
        for k in ("ge", "gt", "le", "lt", "min_length", "max_length"):
            v = getattr(m, k, None)
            if v is not None:
                out[k] = v
    return out


def form_limits() -> dict:
    """Allowed ranges for the custom-test form, the defaults of a brand-new custom test and the netem presets."""
    fields: Dict[str, Any] = {}
    for grp, names in _FORM_FIELDS.items():
        model = Profile if not grp else Profile.model_fields[grp].annotation
        for n in names:
            fields[f"{grp}.{n}" if grp else n] = _bounds(model, n)
    fields["serial.baud"] = {"ge": BAUD_MIN, "le": BAUD_MAX}
    fields["mirrored.rate_baud"] = {"ge": BAUD_MIN, "le": BAUD_MAX}
    fields["idle_gaps_s"] = {"max_count": IDLE_GAPS_MAX_COUNT, "gt": 0, "le": IDLE_GAP_MAX_S}
    d = validate_profile({"name": "My custom test", "transport": "udp", "frame": "extended"})
    for k in ("builtin", "kind", "gen", "sniff", "schema_version"):
        d.pop(k, None)
    return {"fields": fields, "defaults": d, "schema_version": SCHEMA_VERSION}


def export_profiles(profiles: List[dict]) -> dict:
    out = []
    for p in profiles:
        q = copy.deepcopy(p)
        q.pop("id", None)
        if isinstance(q.get("gen"), dict):
            q["gen"] = {**q["gen"], "peer_password": ""}          # never write a password into a shareable file
        if isinstance(q.get("mirrored"), dict):
            q["mirrored"] = {**q["mirrored"], "peer_password": ""}
        q["builtin"] = False if not p.get("builtin") else True
        out.append(q)
    return {"format": FORMAT, "schema_version": SCHEMA_VERSION,
            "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "app_version": __version__, "profiles": out}


def parse_import(doc: Any) -> List[dict]:
    """Validate an export document; return normalised, non-builtin profiles. Raises ValueError."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError("This is not a Pi Automation Traffic Tester profile file.")
    ver = doc.get("schema_version")
    if not isinstance(ver, int) or ver > SCHEMA_VERSION:
        raise ValueError(f"Profile file version {ver} is newer than this app supports ({SCHEMA_VERSION}).")
    plist = doc.get("profiles")
    if not isinstance(plist, list) or not plist:
        raise ValueError("The file contains no profiles.")
    out = []
    for i, p in enumerate(plist):
        if not isinstance(p, dict):
            raise ValueError(f"Profile #{i + 1} is not an object.")
        try:
            q = validate_profile({**p, "builtin": False})
        except ValueError as e:
            raise ValueError(f"Profile '{p.get('name', i + 1)}': {e}")
        out.append(q)
    return out
