"""Shared helpers for the Mirrored Bits integration tests: a socat pty pair as a stand-in for a serial cable, and a pair runner."""
import os
import shutil
import subprocess
import tempfile
import time

import pytest

from traffic_tester.mbemu import Emulator
from traffic_tester.mbemu.engine import default_pair
from traffic_tester.mbemu.links import InternalLink, MBSerialLink


class Socat:
    """`socat pty,raw,echo=0,link=A pty,raw,echo=0,link=B`: what one end writes the other reads, like a null-modem cable."""

    def __init__(self, loop=False):
        if not shutil.which("socat"):
            pytest.skip("socat not installed")
        self.dir = tempfile.mkdtemp(prefix="mbpty")
        self.a, self.b = os.path.join(self.dir, "a"), os.path.join(self.dir, "b")
        args = ["socat", f"pty,raw,echo=0,link={self.a}"] + (["EXEC:cat"] if loop else [f"pty,raw,echo=0,link={self.b}"])
        self.p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(self.a) and (loop or os.path.exists(self.b)):
                break
            time.sleep(0.05)
        else:
            self.close()
            raise RuntimeError("socat did not create the pty pair")

    def links(self, baud=9600, **kw):
        return MBSerialLink(self.a, baud, **kw), MBSerialLink(self.b, baud, **kw)

    def close(self):
        self.p.terminate()
        try:
            self.p.wait(3)
        except Exception:
            self.p.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


class Chan:
    """kind 'internal' (two emulators in one process) or 'socat' (a pty pair, the real serial code path)."""

    def __init__(self, kind, baud=9600):
        self.kind, self.baud = kind, baud
        self.sc = Socat() if kind == "socat" else None

    def links(self):
        return self.sc.links(self.baud) if self.sc else InternalLink.pair()

    def close(self):
        if self.sc:
            self.sc.close()


def run_pair(chan, duration, a=None, b=None, impair_a=None, impair_b=None, script=None, links=None):
    """Start B then A, run `script` [(t, fn(ea, eb))], wait for A to finish, stop B.  Returns (ea, eb)."""
    sa, sb = default_pair({"baud": chan.baud, "trip_every_s": 0, **(a or {})}, {"baud": chan.baud, **(b or {})})
    la, lb = links or chan.links()
    ea, eb = Emulator(sa, la, "A", duration_s=duration), Emulator(sb, lb, "B", duration_s=duration + 5)
    if impair_a:
        ea.imp.set(impair_a, 0)
    if impair_b:
        eb.imp.set(impair_b, 0)
    eb.start()
    ea.start()
    t0 = time.monotonic()
    for at, fn in sorted(script or [], key=lambda x: x[0]):
        while time.monotonic() - t0 < at:
            time.sleep(0.002)
        fn(ea, eb)
    ea.thread.join(duration + 10)
    eb.stop()
    return ea, eb


def wait_for(cond, timeout=3.0, step=0.0005):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return time.monotonic() - t0
        time.sleep(step)
    return None
