"""Ed25519 (RFC 8032) in pure Python: no extra package needed on the Pi, and it runs under the system python3 for the root update helper.
Verification is all the Pi ever does; signing is used only on the release machine (scripts/build-update.py). Speed is not a concern
(a few milliseconds per signature, one signature per update)."""
from __future__ import annotations

import hashlib
import os

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _h(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _xrecover(y: int):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P:
        x = x * _I % _P
    if (x * x - xx) % _P:
        return None
    if x % 2:
        x = _P - x
    return x


_BY = 4 * _inv(5) % _P
_BX = _xrecover(_BY)
_B = (_BX % _P, _BY % _P, 1, _BX * _BY % _P)
_IDENT = (0, 1, 1, 0)


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, p):
    q = _IDENT
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _enc(p) -> bytes:
    zi = _inv(p[2])
    x, y = p[0] * zi % _P, p[1] * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _dec(b: bytes):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return None
    x = _xrecover(y)
    if x is None or (x == 0 and sign):
        return None
    if (x & 1) != sign:
        x = _P - x
    return (x, y, 1, x * y % _P)


def _eq(p, q) -> bool:
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _clamp(h: bytes) -> int:
    a = int.from_bytes(h[:32], "little")
    return (a & ((1 << 254) - 8)) | (1 << 254)


def generate_seed() -> bytes:
    return os.urandom(32)


def public_key(seed: bytes) -> bytes:
    return _enc(_mul(_clamp(_h(seed)), _B))


def sign(seed: bytes, msg: bytes) -> bytes:
    h = _h(seed)
    a = _clamp(h)
    pub = _enc(_mul(a, _B))
    r = int.from_bytes(_h(h[32:] + msg), "little") % _L
    rb = _enc(_mul(r, _B))
    k = int.from_bytes(_h(rb + pub + msg), "little") % _L
    return rb + ((r + k * a) % _L).to_bytes(32, "little")


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    """True only for a valid signature of `msg` by `pub`. Never raises on malformed input."""
    try:
        if len(sig) != 64 or len(pub) != 32:
            return False
        a_pt, r_pt = _dec(pub), _dec(sig[:32])
        s = int.from_bytes(sig[32:], "little")
        if a_pt is None or r_pt is None or s >= _L:
            return False
        k = int.from_bytes(_h(sig[:32] + pub + msg), "little") % _L
        return _eq(_mul(s, _B), _add(r_pt, _mul(k, a_pt)))
    except Exception:
        return False
