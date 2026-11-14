"""SEL-2505-style Mirrored Bits emulator (docs/MIRRORED-BITS-SPEC.md is the source of truth).

codec     pluggable message codec (SimCodec only: emulator-to-emulator, NOT SEL wire-compatible) + resyncing character framer
status    receive validation counters, ROK / RBAD / CBAD / LBOK, per-bit PU/DO security counts, settings
impair    in-app impairment injector between codec and transport
links     serial (parity/framing markers, overrun counter), TCP, UDP, in-process pair
io        virtual contact inputs/outputs and optional Raspberry Pi GPIO mapping
engine    the emulator itself (one thread per end)
"""
from .codec import Codec, SimCodec, Framer, RxChar, Decoded, CODECS, get_codec          # noqa: F401
from .status import Settings, Channel, RxBits, REASONS                                   # noqa: F401
from .impair import Impairer                                                             # noqa: F401
from .engine import Emulator, selftest                                                   # noqa: F401
