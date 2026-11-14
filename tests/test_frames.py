import random
from traffic_tester import frames as F


def test_crc16_known_vector():
    assert F.crc16(b"123456789") == 0x29B1          # CRC-16/CCITT-FALSE check value


def test_compact_roundtrip_and_size():
    f = F.Frame(F.T_TRIP, 513, 0xA5, F.F_IDLE)
    b = F.encode(f)
    assert len(b) == F.COMPACT_LEN == 7 and b[0] == 0xA5
    d = F.decode_datagram(b)
    assert (d.type, d.seq, d.bits, d.flags, d.extended) == (F.T_TRIP, 513, 0xA5, F.F_IDLE, False)


def test_extended_roundtrip_size_and_layout():
    f = F.Frame(F.T_ECHO, 70000, 3, F.F_SYNCED, t1=11, t2=22, t3=33, offset_us=-1500, extended=True)
    b = F.encode(f)
    assert len(b) == F.EXT_LEN == 40 and b[:2] == b"\xA5\x5B"
    assert int.from_bytes(b[6:10], "big") == 70000          # seq at offset 6 (spec 3.2)
    assert int.from_bytes(b[10:18], "big") == 11            # t1 at offset 10
    d = F.decode_datagram(b)
    assert (d.seq, d.t1, d.t2, d.t3, d.offset_us, d.flags, d.bits) == (70000, 11, 22, 33, -1500, F.F_SYNCED, 3)


def test_corrupted_frame_rejected():
    b = bytearray(F.encode(F.Frame(F.T_TRIP, 5, 1)))
    b[4] ^= 0xFF
    assert F.decode_datagram(bytes(b)) is None
    e = bytearray(F.encode(F.Frame(F.T_TRIP, 5, 1, extended=True)))
    e[20] ^= 1
    assert F.decode_datagram(bytes(e)) is None


def test_stream_parser_resync_split_and_mixed():
    p = F.StreamParser()
    f1, f2 = F.encode(F.Frame(F.T_TRIP, 1, 1)), F.encode(F.Frame(F.T_ECHO, 2, 2, extended=True))
    out = p.feed(b"\x00\x13\xA5" + f1[:3])          # garbage, a stray sync byte, then half a frame
    out += p.feed(f1[3:] + f2[:10])
    out += p.feed(f2[10:])
    assert [(x.type, x.seq) for x in out] == [(F.T_TRIP, 1), (F.T_ECHO, 2)]


def test_stream_parser_counts_crc_failures_and_recovers():
    p = F.StreamParser()
    bad = bytearray(F.encode(F.Frame(F.T_TRIP, 9, 1))); bad[3] ^= 0x55
    good = F.encode(F.Frame(F.T_TRIP, 10, 1))
    out = p.feed(bytes(bad) + good)
    assert [x.seq for x in out] == [10] and p.corrupted >= 1


def test_stream_parser_random_noise_never_crashes():
    rnd = random.Random(7)
    p = F.StreamParser()
    frames = []
    for i in range(200):
        frames.append(F.encode(F.Frame(F.T_TRIP, i, i & 0xFF)))
    stream = b"".join(rnd.choice([b"", bytes(rnd.randrange(256) for _ in range(3))]) + f for f in frames)
    got = []
    for i in range(0, len(stream), 5):
        got += p.feed(stream[i:i + 5])
    assert len(got) >= 195                              # almost all recovered despite noise


def test_unwrap_seq():
    assert F.unwrap_seq(5, 65540, 16) == 65541
    assert F.unwrap_seq(65535, 65537, 16) == 65535
    assert F.unwrap_seq(3, 10, 16) == 3
