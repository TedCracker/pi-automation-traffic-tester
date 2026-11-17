"""Virtual-time two-emulator harness with a delayed, lossy wire: for the Mirrored Bits echo round-trip tests (no sleeping, fully deterministic)."""
import random

from traffic_tester.mbemu import Emulator
from traffic_tester.mbemu.engine import default_pair
from traffic_tester.mbemu.links import Link

MS = 1_000_000


class Clock:
    t = 0

    def __call__(self):
        return self.t


class DelayLink(Link):
    """One end of a wire with a fixed one-way delay, random frame loss and blackout windows (every frame sent inside a blackout is lost)."""
    datagram = False

    def __init__(self, clk, delay_ms, loss=0.0, rng=None, blackouts=()):
        self.clk, self.delay, self.loss = clk, int(delay_ms * MS), loss
        self.rng = rng or random.Random(1)
        self.blackouts = list(blackouts)             # [(start_ns, end_ns)]
        self.peer = None
        self.q = []
        self.sent_n = self.lost_n = 0

    def open(self): ...
    def close(self): ...

    def send(self, data):
        self.sent_n += 1
        t = self.clk()
        if any(a <= t < b for a, b in self.blackouts) or self.rng.random() < self.loss:
            self.lost_n += 1
            return len(data)
        self.peer.q.append((bytes(data), t + self.delay))
        return len(data)

    def recv(self, timeout_s):
        t = self.clk()
        due = [(d, a, a) for d, a in self.q if a <= t]
        self.q = [(d, a) for d, a in self.q if a > t]
        return due


def run_echo(delay_ms=150.0, loss=0.10, seconds=30.0, blackout_every_s=0.0, blackout_ms=0.0, baud=38400, seed=1, a=None, b=None, flip_every_s=0.0, trace=None,
             direction="both", blackout_from_s=None):
    """Run PROTO A (near) <-> PROTO B (far), SimCodec with the echo bit, on a virtual clock.  Returns (ea, eb).
    Blackouts are in both directions (a radio dropout) unless direction is "a2b" (only A -> B frames are lost: the far end loses ROK) or "b2a".  flip_every_s > 0 flips IN1 at A on that period (a trip)."""
    clk = Clock()
    rng = random.Random(seed)
    bl = []
    if blackout_every_s:
        t = blackout_every_s if blackout_from_s is None else blackout_from_s
        while t < seconds:
            bl.append((int(t * 1e9), int((t + blackout_ms / 1000.0) * 1e9)))
            t += blackout_every_s
    la = DelayLink(clk, delay_ms, loss, rng, bl if direction in ("both", "a2b") else [])     # la: what A sends
    lb = DelayLink(clk, delay_ms, loss, rng, bl if direction in ("both", "b2a") else [])
    la.peer, lb.peer = lb, la
    sa, sb = default_pair({"baud": baud, "trip_every_s": 0, **(a or {})}, {"baud": baud, **(b or {})})
    ea = Emulator(sa, la, "A", mono=clk, real=lambda: clk.t, duration_s=seconds)
    eb = Emulator(sb, lb, "B", mono=clk, real=lambda: clk.t, duration_s=seconds)
    ea.begin(0)
    eb.begin(0)
    step = ea.nominal_ns // 4
    end = int(seconds * 1e9)
    next_flip = int(flip_every_s * 1e9) if flip_every_s else None
    val = 0
    while clk.t < end:
        clk.t += step
        for e in (ea, eb):
            e.step()
        if next_flip is not None and clk.t >= next_flip:
            val ^= 1
            ea.io.set_in(0, val)
            next_flip += int(flip_every_s * 1e9)
        if trace is not None:
            trace(clk.t, ea, eb)
    return ea, eb
