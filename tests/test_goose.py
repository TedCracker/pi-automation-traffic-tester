from traffic_tester import goose as G, frames as F


def pdu(**kw):
    d = {"gocb": "A/LLN0$GO$gcb", "tal_ms": 8, "dat_set": "A/LLN0$ds", "go_id": "x", "t_ns": 1_700_000_000_123_456_789,
         "st": 5, "sq": 2, "sim": False, "conf_rev": 1, "nds": False, "data": [("b", True), ("b", False), ("i", -3), ("i", 1 << 40)]}
    d.update(kw)
    return d


def test_ber_pdu_roundtrip():
    q = G.pdu_decode(G.pdu_encode(pdu()))
    assert q["gocb"] == "A/LLN0$GO$gcb" and q["st"] == 5 and q["sq"] == 2 and q["tal_ms"] == 8 and q["conf_rev"] == 1
    assert q["data"] == [("b", True), ("b", False), ("i", -3), ("i", 1 << 40)]
    assert abs(q["t_ns"] - 1_700_000_000_123_456_789) < 100          # UtcTime fraction resolution ~60 ns


def test_pdu_starts_with_goosepdu_tag_and_uses_standard_field_tags():
    b = G.pdu_encode(pdu())
    assert b[0] == 0x61
    tags = []
    _, body, _ = G.read_tlv(b, 0)
    i = 0
    while i < len(body):
        t, _, i = G.read_tlv(body, i); tags.append(t)
    assert tags == [0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8A, 0xAB]   # IEC 61850-8-1 goosePdu order


def test_long_form_length_and_big_values():
    big = pdu(data=[("b", i % 2 == 0) for i in range(200)])
    assert len(G.pdu_decode(G.pdu_encode(big))["data"]) == 200
    assert G.pdu_decode(G.pdu_encode(pdu(st=0xFFFFFFFF)))["st"] == 0xFFFFFFFF


def test_l2_frame_layout_vlan_and_untagged():
    b = G.pdu_encode(pdu())
    tagged = G.l2_build("01:0c:cd:01:00:01", bytes([2, 0, 0, 0, 0, 1]), True, 0, 4, 0x0001, False, b)
    assert tagged[12:14] == b"\x81\x00" and tagged[16:18] == b"\x88\xb8" and tagged[14] >> 5 == 4      # 802.1Q, EtherType, PCP 4
    e = G.l2_parse(tagged)
    assert e["vlan"] and e["prio"] == 4 and e["appid"] == 1 and int.from_bytes(tagged[20:22], "big") == 8 + len(b)
    untagged = G.l2_build("01:0c:cd:01:00:01", bytes(6), False, 0, 4, 2, True, b)
    e2 = G.l2_parse(untagged)
    assert not e2["vlan"] and e2["appid"] == 2 and e2["sim"] and G.pdu_decode(e2["pdu"])["st"] == 5
    assert G.l2_parse(b"\x00" * 30) is None


def test_l3_session_roundtrip():
    b = G.pdu_encode(pdu())
    d = G.l3_build(7, 9, False, b)
    e = G.l3_parse(d)
    assert d[:3] == b"\x01\x40\xa1" and e["appid"] == 9 and e["spdu"] == 7 and G.pdu_decode(e["pdu"])["gocb"].startswith("A/")
    assert G.l3_parse(b"\x00\x01\x02") is None and G.l3_parse(b"") is None


def test_default_frames_small_enough_for_gre():
    b = G.pdu_encode(pdu(data=[("b", True)] * 8 + [("i", 17), ("i", 0), ("i", 0), ("i", 0)]))
    assert len(G.l2_build("01:0c:cd:01:00:01", bytes(6), True, 0, 4, 1, False, b)) < 300


def test_rgoose_layout_matches_wireshark_dissector():
    """Field positions exactly as Wireshark's R-GOOSE dissector reads them (CLTP header, session header, payload length, APDU header)."""
    b = G.pdu_encode(pdu())
    d = G.l3_build(0x01020304, 0x0102, True, b)
    assert d[0:2] == b"\x01\x40"                                          # CLTP: LI=1, TPDU UD
    assert d[2] == 0xA1 and d[3] == 23                                     # SPDU id GOOSE, session header length (no IV)
    assert d[4] == 0x80 and d[5] == 10                                     # common session header id + length
    assert int.from_bytes(d[6:10], "big") == len(d) - 2                    # SPDU length
    assert int.from_bytes(d[10:14], "big") == 0x01020304                   # SPDU number
    assert d[14:16] == b"\x00\x01"                                         # version
    assert d[16:26] == bytes(10) and d[26] == 0                            # key times + key id zero, IV length 0
    plen = int.from_bytes(d[27:31], "big")
    assert plen == 6 + len(b) and d[31] == 0x81 and d[32] == 1             # payload length, APDU tag, simulation flag
    assert int.from_bytes(d[33:35], "big") == 0x0102 and int.from_bytes(d[35:37], "big") == len(b)
    assert d[37:] == b and G.l3_parse(d)["pdu"] == b
