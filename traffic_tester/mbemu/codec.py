"""Message codec interface and the one codec we ship: SimCodec.

!!  SimCodec is OUR OWN framing, for emulator-to-emulator tests only.  It is NOT the SEL Mirrored Bits wire format and it will not talk
!!  to a real SEL relay or a SEL-2505.  Every screen and report says so.  The real layout (character format, bit positions, ID /
!!  inversion pattern, MB vs MB8) is not public in the spec we were given; do not invent one.

How to add the SEL codec later (from SEL's application guide "Understanding and Effectively Using the SEL Mirrored Bits Protocol",
or from serial captures of a real device):
    1. subclass `Codec`, set `name`, `label`, `chars_per_msg`, implement `encode` and `decode`;
    2. register it in `CODECS`;
    3. nothing else changes: the framer, receive checks, channel status, impairment injector and UI only see this interface.

The second codec, TripCodec ("Trip frame (seq + timestamp + CRC)"), is the frame of the DER transfer-trip design (see its docstring).

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
    crc_ok: bool = True                   # codecs with a checksum: False when it did not match (the engine counts a "crc" reject)
    seq: Optional[int] = None             # frame codec only: 16-bit sequence number, send time (sender's clock, us), and the echo of what the sender last heard
    ts_us: Optional[int] = None
    echo_valid: bool = False
    echo_seq: Optional[int] = None
    echo_ts_us: Optional[int] = None
    hold_us: Optional[int] = None


class Codec:
    name = "base"
    label = "base codec"
    chars_per_msg = 4
    sel_compatible = False
    trip_frame = False                    # True: frames carry seq / timestamp / echo / CRC (TripCodec); the engine then does sequence + echo round-trip accounting
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


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE (polynomial 0x1021, start value 0xFFFF).  Catches every single-bit error and every burst up to 16 bits in a frame this short."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


TRIP_LABEL = "Trip frame (seq + timestamp + CRC) — emulator-to-emulator only, not SEL wire-compatible"


class TripCodec(Codec):
    """The frame of the "DER transfer trip" design: eight status bits, a sequence number, a send timestamp, the echo of the last frame heard, a CRC.

    19 characters, big-endian:
        0      0b101 sss ss   sync pattern + 5-bit sender ID (same as SimCodec, so the resync logic is shared)
        1      TMB1..8        the eight status bits (bit0 = TMB1) - sent once; the CRC protects them
        2      flags          bit0 = the echo fields below are valid (a frame has been heard from the other end)
        3-4    seq            16-bit frame sequence number, wraps 65535 -> 0
        5-8    ts             send time in microseconds of the SENDER's monotonic clock (32 bit, wraps every 71.6 min; only differences are used)
        9-10   echo_seq       sequence number of the last good frame received from the other end
        11-14  echo_ts        that frame's `ts` (the other end's clock, returned untouched)
        15-16  hold           microseconds this end held that frame before sending this one (0..65535; 65535 = "too long", no sample)
        17-18  crc            CRC-16/CCITT-FALSE over characters 0..16

    Round trip with no clock sync: the end that sent frame `echo_seq` at `echo_ts` (its own clock) sees the echo come back at `now` (its own clock):
        rtt = (now - echo_ts) - hold.
    At 19200 baud 8N1 one frame takes 19 x 10 bits / 19200 = 9.9 ms on the wire (about 100 per second); at 38400 it is 4.95 ms.
    One-way latency needs both clocks on the same time source (GPS) - that is not built in: see engine.Emulator._one_way_hook."""
    name = "trip"
    label = TRIP_LABEL
    chars_per_msg = 19
    trip_frame = True
    LEN = 19
    HOLD_MAX = 0xFFFF

    def encode_frame(self, bits: int, tx_id: int, seq: int, ts_us: int, echo_seq: Optional[int] = None, echo_ts_us: int = 0, hold_us: int = 0) -> bytes:
        body = bytearray(self.LEN - 2)
        body[0] = (SIM_SYNC << 5) | (tx_id & ID_MAX)
        body[1] = bits & 0xFF
        body[2] = 1 if echo_seq is not None else 0
        body[3:5] = (seq & 0xFFFF).to_bytes(2, "big")
        body[5:9] = (ts_us & 0xFFFFFFFF).to_bytes(4, "big")
        body[9:11] = ((echo_seq or 0) & 0xFFFF).to_bytes(2, "big")
        body[11:15] = (echo_ts_us & 0xFFFFFFFF).to_bytes(4, "big")
        body[15:17] = min(max(int(hold_us), 0), self.HOLD_MAX).to_bytes(2, "big")
        return bytes(body) + crc16_ccitt(bytes(body)).to_bytes(2, "big")

    def encode(self, bits: int, tx_id: int) -> bytes:                  # plain interface (no sequence state): frame number 0, no echo
        return self.encode_frame(bits, tx_id, 0, 0)

    def frame_valid(self, chars: List[RxChar]) -> bool:
        """Right length, no character the driver marked bad, sync pattern and CRC all fine (used by the framer to find a frame boundary)."""
        if len(chars) != self.LEN or any(c.err for c in chars):
            return False
        raw = bytes(c.value for c in chars)
        return (raw[0] >> 5) == SIM_SYNC and crc16_ccitt(raw[:-2]) == int.from_bytes(raw[-2:], "big")

    def decode(self, chars: List[RxChar]) -> Decoded:
        if len(chars) != self.LEN:
            return Decoded(False)
        err = next((c.err for c in chars if c.err), None)
        raw = bytes(c.value for c in chars)
        if (raw[0] >> 5) != SIM_SYNC:
            return Decoded(False, err)
        ok = crc16_ccitt(raw[:-2]) == int.from_bytes(raw[-2:], "big")
        b = raw[1]
        return Decoded(True, err, [b, b, b], raw[0] & ID_MAX, ok, int.from_bytes(raw[3:5], "big"), int.from_bytes(raw[5:9], "big"),
                       bool(raw[2] & 1), int.from_bytes(raw[9:11], "big"), int.from_bytes(raw[11:15], "big"), int.from_bytes(raw[15:17], "big"))

    def looks_like_start(self, value: int) -> bool:
        return (value >> 5) == SIM_SYNC

    def corrupt_copy(self, msg: bytes, copy: int, bit: int) -> bytes:   # test helper: flip one bit of the status byte (there is one copy; the CRC must catch it)
        m = bytearray(msg)
        m[1] ^= 1 << bit
        return bytes(m)


CODECS: Dict[str, Codec] = {"sim": SimCodec(), "trip": TripCodec()}


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
        self._hunt = False    # CRC codecs: the last frame failed its check, so the frame boundary may be off (a character was lost or added)

    def reset(self) -> None:
        self.buf.clear()
        self._pm = 0
        self._hunt = False

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
            if getattr(self.codec, "trip_frame", False):
                # A frame with a CRC: one bad frame is reported once (its length is assumed right: a flipped bit does not move the boundary).  If the NEXT one is bad
                # too, a character was probably lost or added: slide one character at a time to the next frame that checks out, instead of reporting every
                # misaligned slice (the lost frames are counted exactly by their sequence numbers anyway).
                if self.codec.frame_valid(cand):
                    self._hunt = False
                elif self._hunt:
                    del buf[0]
                    self.junk_chars += 1
                    continue
                else:
                    self._hunt = True
            # a candidate whose first character is fine but which does not decode: the engine rejects it and we consume all n characters;
            # if its sync/ID were damaged (head.err) we still consume n: the position is right, only the value is not.
            out.append(cand)
            del buf[:n]
        if junk:
            out.append(("junk", junk))
            self.junk_chars += junk
        return out
