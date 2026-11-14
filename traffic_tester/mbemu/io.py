"""Contact inputs / outputs of a SEL-2505-style unit: IN1..8 drive TMB1..8, RMB1..8 drive OUT1..8, no logic in between (spec 1).
Virtual by default (switches and lamps in the UI, an API to set and read them).  Optional mapping to Raspberry Pi GPIO pins.
GPIO pins are 3.3 V logic: real relay contacts / field wiring need an opto-isolated or relay board between the Pi and the plant."""
from __future__ import annotations

import threading
from typing import Callable, List, Optional

GPIO_WARNING = ("Pi GPIO pins are 3.3 V logic and cannot drive or sense real contacts directly. For field wiring use an opto-isolated "
                "input board and a relay output board between the Pi and the equipment.")


class GpioBackend:
    """Thin wrapper so tests can inject a fake.  Uses gpiozero when it is installed on the Pi (Raspberry Pi OS ships it)."""

    def __init__(self):
        import gpiozero            # noqa: F401  (ImportError -> caller reports "GPIO not available")
        self._z = gpiozero
        self.devs = {}

    def input(self, pin: int, pull_up: bool):
        d = self._z.DigitalInputDevice(pin, pull_up=pull_up)
        self.devs[("in", pin)] = d
        return lambda: 1 if d.value else 0

    def output(self, pin: int):
        d = self._z.DigitalOutputDevice(pin)
        self.devs[("out", pin)] = d
        return lambda v: d.on() if v else d.off()

    def close(self):
        for d in self.devs.values():
            try:
                d.close()
            except Exception:
                pass
        self.devs.clear()


class IO:
    """IN1..8 (what drives TMB1..8) and OUT1..8 (what RMB1..8 drive)."""

    def __init__(self, on_in_change: Callable[[int, int], None] = lambda i, v: None):
        self.lock = threading.Lock()
        self.ins = [0] * 8              # virtual switches
        self.mask = 0                   # the same as a bit mask (bit0 = IN1), kept current so reading it costs nothing
        self.outs = [0] * 8             # lamps
        self.on_in_change = on_in_change
        self.mode = "virtual"           # "virtual" | "gpio"
        self.gpio_error: Optional[str] = None
        self._gin: List[Optional[Callable[[], int]]] = [None] * 8
        self._gout: List[Optional[Callable[[int], None]]] = [None] * 8
        self._backend: Optional[GpioBackend] = None
        self.map = {"in": [None] * 8, "out": [None] * 8, "pull_up": True, "active_low_in": False}

    # ---- inputs
    def set_in(self, idx: int, value: int) -> None:
        if not 0 <= idx < 8:
            raise ValueError("IN1..8")
        v = 1 if value else 0
        with self.lock:
            changed = self.ins[idx] != v
            self.ins[idx] = v
            self.mask = (self.mask | (1 << idx)) if v else (self.mask & ~(1 << idx))
        if changed:
            self.on_in_change(idx, v)

    def poll_gpio(self) -> None:
        """GPIO inputs are sampled here (the engine calls this every few ms); virtual switches need no polling."""
        if self.mode != "gpio":
            return
        for i, f in enumerate(self._gin):
            if f is not None:
                try:
                    v = f()
                except Exception:
                    continue
                if self.map["active_low_in"]:
                    v = 1 - v
                if v != self.ins[i]:
                    self.set_in(i, v)

    # ---- outputs
    def write_outs(self, bits: int) -> None:
        for i in range(8):
            v = (bits >> i) & 1
            if self.outs[i] != v:
                self.outs[i] = v
                f = self._gout[i]
                if f is not None:
                    try:
                        f(v)
                    except Exception as e:  # noqa
                        self.gpio_error = f"OUT{i + 1}: {type(e).__name__}: {e}"

    # ---- GPIO mapping
    def configure_gpio(self, in_pins: List[Optional[int]], out_pins: List[Optional[int]], pull_up: bool = True,
                       active_low_in: bool = False, backend: Optional[object] = None) -> dict:
        self.disable_gpio()
        if len(in_pins) != 8 or len(out_pins) != 8:
            raise ValueError("Give 8 input pins and 8 output pins (blank = not mapped)")
        used = [p for p in list(in_pins) + list(out_pins) if p is not None]
        if len(used) != len(set(used)):
            raise ValueError("A pin can only be used once")
        if any(p is not None and not 2 <= int(p) <= 27 for p in used):
            raise ValueError("Use BCM pin numbers 2..27")
        try:
            be = backend or GpioBackend()
        except Exception as e:  # noqa
            self.gpio_error = f"GPIO is not available on this computer ({type(e).__name__}). It works on a Raspberry Pi with gpiozero installed."
            raise RuntimeError(self.gpio_error)
        self._backend = be
        for i in range(8):
            if in_pins[i] is not None:
                self._gin[i] = be.input(int(in_pins[i]), pull_up)
            if out_pins[i] is not None:
                self._gout[i] = be.output(int(out_pins[i]))
        self.map = {"in": list(in_pins), "out": list(out_pins), "pull_up": pull_up, "active_low_in": active_low_in}
        self.mode, self.gpio_error = "gpio", None
        return self.state()

    def disable_gpio(self) -> None:
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception:
                pass
        self._backend = None
        self._gin, self._gout = [None] * 8, [None] * 8
        self.mode = "virtual"

    def close(self) -> None:
        self.disable_gpio()

    def state(self) -> dict:
        return {"mode": self.mode, "ins": self.mask, "outs": sum(1 << i for i in range(8) if self.outs[i]),
                "map": self.map, "gpio_error": self.gpio_error, "warning": GPIO_WARNING if self.mode == "gpio" else None}
