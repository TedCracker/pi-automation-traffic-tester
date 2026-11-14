"""Runs one baud rate of the pty soak in its own process (see test_mb_soak.py)."""
import time

from mbfix import Socat, wait_for
from traffic_tester.mbemu import Emulator
from traffic_tester.mbemu.engine import default_pair


def soak(baud, seconds):
    sc = Socat()
    try:
        la, lb = sc.links(baud)
        sa, sb = default_pair({"baud": baud, "trip_every_s": 0, "echo": "off"}, {"baud": baud, "echo": "off"})
        ea, eb = Emulator(sa, la, f"A{baud}", duration_s=seconds), Emulator(sb, lb, f"B{baud}", duration_s=seconds + 3)
        eb.start()
        ea.start()
        time.sleep(2.0)
        for i in range(8):
            ea.set_tmb(i, 1)
            eb.set_tmb(i, 1)
        ok_a = wait_for(lambda: ea.rx.value == 0xFF, 3.0)
        ok_b = wait_for(lambda: eb.rx.value == 0xFF, 3.0)
        ea.thread.join(seconds + 10)
        eb.stop()
        return {"seconds": seconds, "baud": baud, "ok_a": ok_a, "ok_b": ok_b, "a": ea.counters(), "b": eb.counters(), "nominal_ns": ea.nominal_ns,
                "a_avail": ea.ch.stats(ea.mono())["availability_pct"], "b_avail": eb.ch.stats(eb.mono())["availability_pct"],
                "cpu_a_s": round(ea.cpu_s, 2), "cpu_b_s": round(eb.cpu_s, 2), "msgs_per_s": round(ea.tx_count / seconds), "gap_max_ms": round(ea.gap_max, 2),
                "tx_late_max_ms": round(ea.tx_late_max_ns / 1e6, 2)}
    finally:
        sc.close()
