"""Message codec interface and the one codec we ship: SimCodec.

!!  SimCodec is OUR OWN framing, for emulator-to-emulator tests only.  It is NOT the SEL Mirrored Bits wire format and it will not talk
!!  to a real SEL relay or a SEL-2505.  Every screen and report says so.  The real layout (character format, bit positions, ID /
!!  inversion pattern, MB vs MB8) is not public in the spec we were given; do not invent one.

How to add the SEL codec later (from SEL's application guide "Understanding and Effectively Using the SEL Mirrored Bits Protocol",
or from serial captures of a real device):
    1. subclass `Codec`, set `name`, `label`, `chars_per_msg`, implement `encode` and `decode`;
    2. register it in `CODECS`;
    3. nothing else changes: the framer, receive checks, channel status, impairment injector and UI only see this interface.

SimCodec message (4 characters, each 8 data bits):
    char 0 : 0b101 sss ss  -> sync pattern 101 (top 3 bits) + 5-bit sender ID (0..31)   (the "address / ID pattern")
    char 1 : TMB1..8, copy 1  (bit0 = TMB1)
    char 2 : TMB1..8, copy 2
    char 3 : TMB1..8, copy 3                      -> every bit travels 3 times; the receiver requires all copies to agree
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

SIM_SYNC = 0b101
SIM_LABEL = "SimCodec — emulator-to-emulator only, not SEL wire-compatible"
ID_MAX = 31


@dataclass
class RxChar:
    """One received character with what the serial driver said about it."""
    value: int
    err: Optional[str] = None            # None | "parity" | "framing" (the driver marked this character bad)


@dataclass
class Decoded:
    """What a codec makes of one message-length run of characters (before the receive checks, which live in the engine)."""
    ok_structure: bool                    # the characters have the shape of a message (sync pattern etc.)
    char_err: Optional[str] = None        # "parity" / "framing" when any character was marked bad by the driver
    copies: List[int] = field(default_factory=list)      # the 8 bits as received, once per copy (3 copies)
    msg_id: Optional[int] = None


class Codec:
    name = "base"
    label = "base codec"
    chars_per_msg = 4
    sel_compatible = False
    ID_MAX = ID_MAX

    def encode(self, bits: int, tx_id: int) -> bytes:                  # 8 transmit bits + sender ID -> message bytes
        raise NotImplementedError

    def decode(self, chars: List[RxChar]) -> Decoded:                   # exactly chars_per_msg characters -> bits x copies, ID, char errors
        raise NotImplementedError

    def looks_like_start(self, value: int) -> bool:                     # can a message start with this character? (resync after garbage)
        return True

    def corrupt_copy(self, msg: bytes, copy: int, bit: int) -> bytes:   # test helper: flip one bit of one copy
        raise NotImplementedError


class SimCodec(Codec):
    name = "sim"
    label = SIM_LABEL
    chars_per_msg = 4

    def encode(self, bits: int, tx_id: int) -> bytes:
        b = bits & 0xFF
        return bytes([(SIM_SYNC << 5) | (tx_id & ID_MAX), b, b, b])

    def decode(self, chars: List[RxChar]) -> Decoded:
        if len(chars) != self.chars_per_msg:
            return Decoded(False)
        err = next((c.err for c in chars if c.err), None)
        head = chars[0].value
        if (head >> 5) != SIM_SYNC:
            return Decoded(False, err)
        return Decoded(True, err, [chars[1].value, chars[2].value, chars[3].value], head & ID_MAX)

    def looks_like_start(self, value: int) -> bool:
        return (value >> 5) == SIM_SYNC

    def corrupt_copy(self, msg: bytes, copy: int, bit: int) -> bytes:
        m = bytearray(msg)
        m[1 + copy] ^= 1 << bit
        return bytes(m)


CODECS: Dict[str, Codec] = {"sim": SimCodec()}


def get_codec(name: str = "sim") -> Codec:
    if name not in CODECS:
        raise ValueError(f"unknown codec '{name}' (available: {', '.join(CODECS)})")
    return CODECS[name]


class Framer:
    """Turns a character stream into messages of `codec.chars_per_msg` characters and keeps in step after damage.

    Serial input is in the termios PARMRK form (see `parse_parmrk`): a character the driver found bad arrives as
    0xFF 0x00 <char>, a real 0xFF as 0xFF 0xFF.  feed() returns, per message, either a list of RxChar (a candidate message) or
    ("junk", n) for n characters that could not start a message (counted once as a framing reject by the engine)."""

    def __init__(self, codec: Codec):
        self.codec = codec
        self.buf: List[RxChar] = []
        self._pm = 0          # PARMRK parser state: 0 normal, 1 got 0xFF, 2 got 0xFF 0x00
        self.junk_chars = 0

    def reset(self) -> None:
        self.buf.clear()
        self._pm = 0

    # ---- termios PARMRK stream -> characters
    def parse_parmrk(self, data: bytes, err_name: str = "parity") -> List[RxChar]:
        out: List[RxChar] = []
        for b in data:
            if self._pm == 0:
                if b == 0xFF:
                    self._pm = 1
                else:
                    out.append(RxChar(b))
            elif self._pm == 1:
                if b == 0xFF:
                    out.append(RxChar(0xFF))
                    self._pm = 0
                elif b == 0x00:
                    self._pm = 2
                else:                          # not a valid escape: keep both as data (should not happen)
                    out.append(RxChar(0xFF))
                    out.append(RxChar(b))
                    self._pm = 0
            else:
                out.append(RxChar(b, err_name))
                self._pm = 0
        return out

    @staticmethod
    def plain(data: bytes) -> List[RxChar]:
        return [RxChar(b) for b in data]

    def feed(self, chars: List[RxChar]) -> list:
        """Candidate messages (list of RxChar) and ('junk', n) markers, in stream order."""
        n = self.codec.chars_per_msg
        buf = self.buf
        buf.extend(chars)
        out: list = []
        junk = 0
        while len(buf) >= n:
            head = buf[0]
            if not head.err and not self.codec.looks_like_start(head.value):
                junk += 1
                del buf[0]
                continue
            if junk:
                out.append(("junk", junk))
                self.junk_chars += junk
                junk = 0
            cand = buf[:n]
            # a candidate whose first character is fine but which does not decode: the engine rejects it and we consume all n characters;
            # if its sync/ID were damaged (head.err) we still consume n: the position is right, only the value is not.
            out.append(cand)
            del buf[:n]
        if junk:
            out.append(("junk", junk))
            self.junk_chars += junk
        return out
