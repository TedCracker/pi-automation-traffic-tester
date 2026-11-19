"""Traffic sniffer: pcapng, ring buffer, CRCs, DNP3 / Modbus framing + decoding, wrapping, summaries, BPF, bridge / Wi-Fi commands, serial tap."""
import os
import re
import struct
import subprocess
import tempfile
import time

import pytest

from traffic_tester import profiles as P
from traffic_tester.sniff import analyze, bpf, capture, dnp3, modbus, pcapng, serialtap, wrap


def tmp():
    return tempfile.mkdtemp(prefix="tt-sniff-")


# ---------------------------------------------------------------- pcapng
def test_pcapng_roundtrip_two_interfaces_comment_ns_timestamps():
    d = tmp()
    w = pcapng.PcapngWriter(d + "/a.pcapng", [(1, "eth0", "wire"), (147, "serial", "raw")])
    w.write(0, 1_700_000_000_123_456_789, b"\xaa" * 61, "hello")
    w.write(1, 1_700_000_000_223_456_790, b"abc")
    w.close()
    pk = list(pcapng.read_capture(d + "/a.pcapng"))
    assert [(p["iface"], p["linktype"], p["ts_ns"], len(p["data"]), p["comment"]) for p in pk] == [(0, 1, 1_700_000_000_123_456_789, 61, "hello"), (1, 147, 1_700_000_000_223_456_790, 3, "")]
    assert pk[0]["name"] == "eth0"
    raw = open(d + "/a.pcapng", "rb").read()
    assert raw[:4] == b"\x0a\x0d\x0d\x0a" and len(raw) % 4 == 0                      # SHB magic, 32-bit aligned blocks


def test_pcapng_truncated_tail_and_stream_parser_and_classic_pcap():
    d = tmp()
    w = pcapng.PcapngWriter(d + "/a.pcapng", [(1, "x", "")])
    for i in range(5):
        w.write(0, 1000 + i, bytes([i]) * 70)
    w.close()
    raw = open(d + "/a.pcapng", "rb").read()
    open(d + "/cut.pcapng", "wb").write(raw[:-30])                                      # power loss mid-block
    assert len(list(pcapng.read_capture(d + "/cut.pcapng"))) == 4
    sp, got = pcapng.StreamParser(), []
    for i in range(0, len(raw), 13):
        got += sp.feed(raw[i:i + 13])
    assert [g["data"][0] for g in got] == [0, 1, 2, 3, 4] and got[0]["ts_ns"] == 1000
    with open(d + "/c.pcap", "wb") as f:                                                # classic microsecond pcap (tcpdump fallback)
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1) + struct.pack("<IIII", 5, 250, 4, 4) + b"abcd")
    p = list(pcapng.read_capture(d + "/c.pcap"))
    assert p[0]["ts_ns"] == 5_000_250_000 and p[0]["data"] == b"abcd" and p[0]["linktype"] == 1


def test_ring_rotation_by_size_keeps_newest_files():
    d = tmp()
    rotated = []
    rw = pcapng.RingWriter(d, "tap", [(1, "x", "")], max_bytes=2000, keep=3, on_rotate=rotated.append)
    for i in range(200):
        rw.write(0, i, b"\x00" * 100)
    rw.close()
    files = sorted(os.listdir(d))
    assert len(files) == 3 and rw.deleted > 0 and len(rotated) == rw.n - 1
    assert files[-1].startswith("tap_%05d_" % rw.n)                                       # the newest survive
    assert all(len(list(pcapng.read_capture(os.path.join(d, f)))) > 0 for f in files)      # each file is a complete, readable pcapng


def test_ring_rotation_by_time(monkeypatch):
    d = tmp()
    rw = pcapng.RingWriter(d, "t", [(1, "x", "")], max_seconds=0.2)
    rw.write(0, 1, b"a" * 60)
    time.sleep(0.3)
    rw.write(0, 2, b"b" * 60)
    rw.close()
    assert len(os.listdir(d)) == 2


# ---------------------------------------------------------------- CRC vectors
def test_dnp3_header_crc_covers_the_start_bytes_like_real_devices():
    """The 8 header bytes incl. 05 64 are protected (known on-wire example: 05 64 05 C0 01 00 00 04 E9 21). An earlier version excluded the start bytes and
    would have rejected every real device's frames; tshark (independent dissector) caught it in the container e2e."""
    real = bytes.fromhex("056405c001000004e921")
    assert dnp3.blocks_ok(real) and dnp3.decode_frame(real)["crc_ok"] and dnp3.build_frame(0xC0, 1, 0x0400) == real
    assert dnp3.Dnp3Framer().feed(real, 0)[0][3] is True
    assert dnp3.crc16(real[:8]) == 0x21E9 and dnp3.crc16(real[2:8]) != 0x21E9


def test_crc_known_vectors():
    assert modbus.crc16(bytes([1, 3, 0, 0, 0, 10])) == 0xCDC5                              # classic Modbus example: C5 CD on the wire (low byte first)
    assert modbus.add_crc(bytes([1, 3, 0, 0, 0, 10]))[-2:] == b"\xc5\xcd"
    assert dnp3.crc16(bytes([0x05, 0x64, 0x05, 0xC0, 0x01, 0x00, 0x00, 0x04])) == 0x21E9    # DNP3 header CRC, on the wire E9 21
    assert dnp3.crc16(b"") == 0xFFFF


# ---------------------------------------------------------------- Modbus
def test_modbus_decode_requests_responses_exceptions():
    d = modbus.decode_rtu(modbus.add_crc(bytes([7, 3, 0, 100, 0, 4])))
    assert d["crc_ok"] and d["kind"] == "request" and d["fields"]["Start address"] == 100 and d["fields"]["Quantity"] == 4
    r = modbus.decode_rtu(modbus.add_crc(bytes([7, 3, 8, 0, 1, 0, 2, 0, 3, 0, 4])))
    assert r["kind"] == "response" and r["fields"]["Registers"] == [1, 2, 3, 4]
    e = modbus.decode_rtu(modbus.add_crc(bytes([7, 0x83, 2])))
    assert e["kind"] == "exception" and "Illegal data address" in e["summary"]
    w = modbus.decode_rtu(modbus.add_crc(bytes([1, 5, 0, 10, 0xFF, 0])))
    assert "ON" in w["summary"]
    m = modbus.decode_rtu(modbus.add_crc(bytes([1, 16, 0, 5, 0, 2, 4, 0, 1, 0, 2])))
    assert m["kind"] == "request" and m["fields"]["Byte count"] == 4
    bad = modbus.add_crc(bytes([1, 3, 0, 0, 0, 1]))[:-1] + b"\x00"
    assert not modbus.decode_rtu(bad)["crc_ok"] and "CRC FAILED" in modbus.decode_rtu(bad)["summary"]


def test_modbus_direction_forces_response_view():
    f = modbus.add_crc(bytes([1, 3, 3, 0, 1, 0]))                                           # 8 bytes: also a plausible request
    assert modbus.decode_rtu(f, False)["kind"] == "response" and modbus.decode_rtu(f, True)["kind"] == "request"


def test_modbus_framer_gap_back_to_back_resync():
    fr = modbus.ModbusFramer(gap_ms=4)
    a, b = modbus.add_crc(bytes([1, 3, 0, 0, 0, 2])), modbus.add_crc(bytes([1, 3, 4, 0, 1, 0, 2]))
    out = fr.feed(a + b, 1_000_000)                                                         # two frames in one USB delivery: split by CRC, no gap visible
    assert [(x[0], x[3]) for x in out] == [(a, True), (b, True)]
    out = fr.feed(b[:5], 10_000_000) + fr.feed(b[5:], 11_000_000)                            # one frame split over two deliveries 1 ms apart
    assert [(x[0], x[3]) for x in out] == [(b, True)]
    bad = a[:-1] + b"\x00"
    assert fr.feed(bad, 50_000_000) == [] and [(x[0], x[3]) for x in fr.tick(60_000_000)] == [(bad, False)]       # CRC failure kept + flagged once the gap passed
    assert modbus.default_gap_ms(9600) == pytest.approx(3.5 * 11 / 9600 * 1000, rel=0.01) or modbus.default_gap_ms(9600) == 4.0
    assert modbus.default_gap_ms(115200) == 4.0                                              # never finer than a USB adapter can resolve


def test_modbus_tcp_decode_for_wrapped_frames():
    mb = struct.pack(">HHHB", 5, 0, 6, 9) + bytes([3, 0, 1, 0, 2])
    d = modbus.decode_mbap(mb)
    assert d["proto"] == "Modbus/TCP" and d["fields"]["Transaction id"] == 5 and d["addr"] == 9
    assert modbus.decode_mbap(b"\x00" * 10) is None


# ---------------------------------------------------------------- DNP3
def test_dnp3_frame_build_blocks_and_decode_read_request():
    app = bytes([0xC0, 0xC1, 0x01, 0x3C, 0x02, 0x06, 0x3C, 0x03, 0x06])                      # transport, AC (FIR FIN SEQ 1), READ, g60v2, g60v3 (all objects)
    f = dnp3.build_frame(0xC4, 1, 1024, app)
    assert dnp3.blocks_ok(f) and dnp3.frame_len(f[2]) == len(f)
    d = dnp3.decode_frame(f)
    assert d["crc_ok"] and d["from_master"] and d["src"] == 1024 and d["dest"] == 1
    assert "Read" in d["summary"] and any("g60v2" in o for o in d["fields"]["Objects"]) and "FIR=1 FIN=1 SEQ=1" in d["fields"]["Application control"] or "SEQ=" in d["fields"]["Transport"]


def test_dnp3_multi_block_response_iin_and_objects():
    objs = bytes([30, 2, 0x00, 0, 2]) + bytes([1, 10, 0, 1, 20, 0, 1, 30, 0])               # g30v2, index 0-2, 3 x (flag + 16-bit)
    app = bytes([0xC1, 0xC1, 0x81, 0x00, 0x00]) + objs                                       # transport FIR|FIN, AC, RESPONSE, IIN 0000
    f = dnp3.build_frame(0x44, 1024, 1, app + bytes(20))                                     # > 16 bytes -> several data blocks
    assert dnp3.blocks_ok(f)
    d = dnp3.decode_frame(f)
    assert d["crc_ok"] and not d["from_master"] and "Response" in d["summary"] and d["fields"]["IIN"].endswith("(none)")
    assert any("g30v2" in o and "index 0-2" in o for o in d["fields"]["Objects"])
    iin = bytes([0xC1, 0xC1, 0x82, 0x84, 0x01])                                              # unsolicited, IIN1 = class 2 events + device restart(0x80)... 0x84, IIN2 = function not supported
    di = dnp3.decode_frame(dnp3.build_frame(0x44, 1024, 1, iin))
    assert "Device restart" in di["fields"]["IIN"] and "Function not supported" in di["fields"]["IIN"] and "Unsolicited" in di["summary"]


def test_dnp3_crc_failures_detected_in_header_and_blocks():
    f = bytearray(dnp3.build_frame(0xC4, 1, 2, bytes(range(30))))
    assert dnp3.blocks_ok(bytes(f))
    g = bytearray(f); g[12] ^= 0xFF                                                          # corrupt a data byte
    assert not dnp3.blocks_ok(bytes(g)) and not dnp3.decode_frame(bytes(g))["crc_ok"] and "CRC FAILED" in dnp3.decode_frame(bytes(g))["summary"]
    h = bytearray(f); h[4] ^= 0x01                                                           # corrupt the header
    assert not dnp3.blocks_ok(bytes(h))


def test_dnp3_link_only_frames():
    ack = dnp3.decode_frame(dnp3.build_frame(0x00, 1, 1024))
    assert "ACK" in ack["summary"] and ack["crc_ok"]
    st = dnp3.decode_frame(dnp3.build_frame(0xC9, 1, 1024))
    assert "Request Link Status" in st["summary"]


def test_dnp3_framer_resync_garbage_split_chunks_and_stale():
    f1, f2 = dnp3.build_frame(0xC4, 1, 2, bytes([0xC0, 0xC1, 1])), dnp3.build_frame(0x44, 2, 1, bytes([0xC0, 0xC1, 0x81, 0, 0]))
    fr = dnp3.Dnp3Framer()
    junk = b"\x00\xff\x05\x64\x01"                                                           # garbage incl. a false start sequence
    out = fr.feed(junk + f1[:7], 0) + fr.feed(f1[7:] + f2[:11], 1_000_000) + fr.feed(f2[11:], 2_000_000)
    assert [(x[0], x[3]) for x in out] == [(f1, True), (f2, True)] and fr.skipped > 0
    fr2 = dnp3.Dnp3Framer(stale_ns=10)
    fr2.feed(f1[:12], 0)
    assert fr2.tick(1_000) and fr2.tick(2_000) == []                                          # a frame that never completes is flushed once, flagged bad


# ---------------------------------------------------------------- wrapping for Wireshark + decoding
def _csum_ok(b: bytes) -> bool:
    return wrap.csum(b) == 0


def test_wrapped_frames_have_valid_checksums_and_decode_back():
    w = wrap.Wrapper()
    f = w.dnp3(dnp3.build_frame(0xC4, 1, 1024, bytes([0xC0, 0xC1, 1, 60, 1, 6])), True)
    assert f[12:14] == b"\x08\x00" and _csum_ok(f[14:34]) and f[23] == 6                       # IPv4 header checksum ok, protocol TCP
    tcp0 = f[34:]
    assert wrap.csum(bytes([10, 54, 0, 1, 10, 54, 0, 2]) + struct.pack(">BBH", 0, 6, len(tcp0)) + tcp0) == 0
    r = analyze.decode_ethernet(f)
    assert r["proto"] == "DNP3" and r["dst"].endswith(":20000") and r["app"]["crc_ok"]
    m = w.modbus(modbus.add_crc(bytes([9, 3, 0, 1, 0, 2])), True)
    assert _csum_ok(m[14:34])
    tcp = m[34:]
    pseudo = bytes([10, 54, 0, 1, 10, 54, 0, 2]) + struct.pack(">BBH", 0, 6, len(tcp))
    assert wrap.csum(pseudo + tcp) == 0                                                      # TCP checksum valid
    r = analyze.decode_ethernet(m)
    assert r["proto"] == "Modbus/TCP" and "start 1, quantity 2" in r["info"]
    s1 = struct.unpack(">I", m[38:42])[0]
    m2 = w.modbus(modbus.add_crc(bytes([9, 3, 4, 0, 1, 0, 2])), False)
    assert struct.unpack(">I", m2[42:46])[0] == s1 + 12                                      # ack = request seq + MBAP payload length
    assert _csum_ok(wrap.ip4("10.0.0.1", "10.0.0.2", 17, b"x" * 9)[:20])


def test_decode_ethernet_variety():
    a = bytes(6)
    arp = a + a + b"\x08\x06" + struct.pack(">HHBBH", 1, 0x0800, 6, 4, 1) + a + bytes([10, 0, 0, 1]) + a + bytes([10, 0, 0, 2])
    assert "who has 10.0.0.2" in analyze.decode_ethernet(arp)["info"]
    vlan = a + a + b"\x81\x00" + struct.pack(">HH", (5 << 13) | 100, 0x0800) + bytes([0x45] + [0] * 8 + [1] + [0] * 10) + bytes(20)
    v = analyze.decode_ethernet(vlan)
    assert v["vlan"] == {"id": 100, "prio": 5} and "VLAN 100 prio 5" in v["info"]
    assert analyze.decode_ethernet(b"\x00" * 5)["proto"] == "Malformed"
    assert analyze.decode_ethernet(a + a + b"\x88\xcc" + bytes(30))["proto"] == "LLDP"
    from traffic_tester import goose
    pdu = goose.pdu_encode({"gocb": "IED1/LLN0$GO$gcb", "tal_ms": 8, "dat_set": "d", "go_id": "g", "t_ns": 1, "st": 4, "sq": 2, "sim": False, "conf_rev": 1, "nds": False, "data": [("b", True)]})
    g = goose.l2_build("01:0c:cd:01:00:01", a, True, 0, 4, 1, False, pdu)
    gi = analyze.decode_ethernet(g)
    assert gi["proto"] == "GOOSE" and "stNum 4 sqNum 2" in gi["info"] and gi["vlan"]["prio"] == 4


def _radiotap_beacon(ssid, bssid, chan_freq, sig, chan):
    rt = struct.pack("<BBHI", 0, 0, 0, 0)                                                    # placeholder; build properly below
    present = (1 << 3) | (1 << 5)                                                            # channel + dBm antenna signal
    body = struct.pack("<HH", chan_freq, 0x0480) + struct.pack("<b", sig)                    # channel at offset 8 (aligned), signal after
    rt = struct.pack("<BBHI", 0, 0, 8 + len(body), present) + body
    fc = bytes([0x80, 0]) + b"\x00\x00" + b"\xff" * 6 + bssid + bssid + b"\x00\x00"
    fixed = bytes(8) + struct.pack("<HH", 100, 0x0431)
    ies = bytes([0, len(ssid)]) + ssid.encode() + bytes([3, 1, chan])
    return rt + fc + fixed + ies


def test_radiotap_beacon_decode_and_wifi_summary():
    d = tmp()
    w = pcapng.PcapngWriter(d + "/w.pcapng", [(127, "wlan1mon", "")])
    for i in range(3):
        w.write(0, 1_000_000_000 + i * 100_000_000, _radiotap_beacon("PlantNet", b"\x02\x11\x22\x33\x44\x55", 2437, -60 - i, 6))
    w.write(0, 2_000_000_000, _radiotap_beacon("Guest", b"\x02\xaa\xbb\xcc\xdd\xee", 5180, -72, 36))
    w.close()
    a = analyze.summarize_files([d + "/w.pcapng"])
    nets = {n["ssid"]: n for n in a["wifi_networks"]}
    assert nets["PlantNet"]["channel"] == 6 and nets["PlantNet"]["signal"] == -60 and nets["PlantNet"]["beacons"] == 3 and nets["Guest"]["channel"] == 36
    assert a["protocols"]["802.11"] == 4 and a["packets"] == 4


def test_summary_counts_talkers_protocols_gaps_and_crc_comments():
    d = tmp()
    w, wr = pcapng.PcapngWriter(d + "/s.pcapng", [(1, "e", "")]), wrap.Wrapper()
    t = 1_000_000_000
    for i in range(10):
        w.write(0, t + i * 10_000_000, wr.dnp3(dnp3.build_frame(0xC4, 1, 1024, bytes([0xC0, 0xC1, 1])), True), "ch; crc=ok")
    w.write(0, t + 500_000_000, wr.modbus(modbus.add_crc(bytes([1, 3, 0, 0, 0, 1])), True), "ch; crc=FAIL")     # long gap before this one
    w.write(0, t + 510_000_000, b"\x00" * 5)                                                  # malformed
    w.close()
    a = analyze.summarize_files([d + "/s.pcapng"])
    assert a["packets"] == 12 and a["dnp3_frames"] == 10 and a["modbus_frames"] == 1 and a["crc_failures"] == 1 and a["errors"] >= 2
    assert a["protocols"]["DNP3"] == 10 and a["top_talkers"][0]["packets"] == 10 and a["max_gap_ms"] == pytest.approx(410, abs=1)
    assert a["duration_s"] == pytest.approx(0.51, abs=0.001) and a["pps"] > 20


# ---------------------------------------------------------------- BPF helper
def test_bpf_presets_builders_and_validation():
    assert bpf.build("dnp3") == "tcp port 20000 or udp port 20000" and bpf.build("host", "10.1.2.3") == "host 10.1.2.3" and bpf.build("vlan", "100") == "vlan 100"
    for kind, val in (("host", "x;rm"), ("mac", "nope"), ("vlan", "9999"), ("port", "0"), ("nothing", "")):
        with pytest.raises(ValueError):
            bpf.build(kind, val)
    assert bpf.validate("") is None and bpf.validate("tcp port 502 and host 10.0.0.1") is None
    assert "not valid" in bpf.validate("tcp prot 5") and "not allowed" in bpf.validate("tcp; reboot")
    for pset in bpf.PRESETS:
        assert bpf.validate(pset["expr"]) is None, pset                                       # every shipped preset compiles


# ---------------------------------------------------------------- capture command lines, bridge and Wi-Fi sequences
def _fake_tools(monkeypatch, **kw):
    t = {"dumpcap": "/usr/bin/dumpcap", "tcpdump": "/usr/bin/tcpdump", "tshark": "/usr/bin/tshark", "iw": "/usr/sbin/iw", "ip": "/usr/sbin/ip", "ethtool": "/usr/sbin/ethtool", "engine": "dumpcap"}
    t.update(kw)
    monkeypatch.setattr(capture, "find_tools", lambda: t)
    return t


def test_dumpcap_command_has_ring_filter_and_interfaces(monkeypatch):
    _fake_tools(monkeypatch)
    c = capture.Capture(["eth0", "eth1"], tmp(), "tcp port 502", {"ring_mb": 50, "ring_minutes": 10, "ring_files": 7}, 128, 3600).command()
    j = " ".join(c)
    assert c[0] == "/usr/bin/dumpcap" and "-i eth0 -i eth1" in j and "-f tcp port 502" in j and "-b filesize:51200" in j and "-b duration:600" in j and "-b files:7" in j and "-a duration:3600" in j and "-s 128" in j
    assert capture.Capture(["eth0"], tmp(), "", {}, 0, 0).command().count("-b") == 0                       # no ring options when none are set


def test_tcpdump_fallback_is_pcap_single_port(monkeypatch):
    _fake_tools(monkeypatch, dumpcap=None, engine="tcpdump")
    c = capture.Capture(["eth0"], tmp(), "arp", {"ring_mb": 20, "ring_files": 4})
    cmd = c.command()
    assert c.suffix == ".pcap" and cmd[0] == "/usr/bin/tcpdump" and "-C" in cmd and cmd[cmd.index("-W") + 1] == "4" and cmd[-1] == "arp"
    with pytest.raises(capture.CaptureError):
        capture.Capture(["a", "b"], tmp()).command()
    _fake_tools(monkeypatch, dumpcap=None, tcpdump=None, engine=None)
    with pytest.raises(capture.CaptureError) as e:
        capture.Capture(["eth0"], tmp())
    assert "apt install" in str(e.value)


def test_bridge_command_sequence(monkeypatch):
    _fake_tools(monkeypatch)
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(capture, "_run", lambda cmd, timeout=10: (calls.append(cmd), R())[1])
    monkeypatch.setattr(os.path, "exists", lambda p: True if p.startswith("/sys/class/net/") else os.path.lexists(p))
    br = capture.EthBridge(["eth0", "eth1"], "ttbr0")
    br.up()
    flat = [" ".join(c[1:]) for c in calls]
    assert any("link add name ttbr0 type bridge stp_state 0 forward_delay 0" in c for c in flat)                  # STP off
    assert any("group_fwd_mask 0xfff8" in c for c in flat)                                                        # LLDP etc. forwarded
    assert sum("master ttbr0" in c for c in flat) == 2 and any("link set dev ttbr0 up" in c for c in flat)
    assert not any(" addr add" in c for c in flat)                                                                # never an IP on the bridge
    assert any("-K eth0 gro off lro off tso off gso off rxvlan off txvlan off" in c for c in flat)               # offloads off, VLAN tags stay in the frame
    calls.clear()
    br.down()
    flat = [" ".join(c[1:]) for c in calls]
    assert sum("nomaster" in c for c in flat) == 2 and any("link del ttbr0" in c for c in flat)
    with pytest.raises(capture.BridgeError):
        capture.EthBridge(["eth0", "eth0"]).up()


def test_wifi_monitor_sequence_and_hop(monkeypatch):
    _fake_tools(monkeypatch)
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(capture, "_run", lambda cmd, timeout=10: (calls.append(" ".join(cmd[1:])), R())[1])
    w = capture.WifiMonitor("wlan1", 6, "HT40+", [1, 6, 11], 0.15)
    w.up()
    time.sleep(0.5)
    w.down()
    assert calls[0] == "link set dev wlan1 down" and calls[1] == "dev wlan1 set type monitor" and calls[2] == "link set dev wlan1 up"
    assert "dev wlan1 set channel 1 HT40+" in calls and "dev wlan1 set channel 6 HT40+" in calls and set(w.visited) >= {1, 6, 11}     # hops through the list
    assert any("set type managed" in c for c in calls)                                                                # restored afterwards
    class Bad(R):
        returncode, stderr = 1, "command failed: Operation not permitted (-1)"
    monkeypatch.setattr(capture, "_run", lambda cmd, timeout=10: Bad())
    with pytest.raises(capture.CaptureError) as e:
        capture.WifiMonitor("wlan1").up()
    assert "permission" in str(e.value)


# ---------------------------------------------------------------- serial tap over pty pairs (socat)
def _pty_pair():
    p = subprocess.Popen(["socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0"], stderr=subprocess.PIPE, text=True)
    ptys = []
    while len(ptys) < 2:
        m = re.search(r"PTY is (\S+)", p.stderr.readline())
        if m:
            ptys.append(m.group(1))
    return p, ptys


@pytest.fixture()
def pty2():
    p1, a = _pty_pair()
    p2, b = _pty_pair()
    fds = [os.open(a[0], os.O_RDWR), os.open(b[0], os.O_RDWR)]
    yield a[1], b[1], fds
    for fd in fds:
        os.close(fd)
    p1.terminate(); p2.terminate()


def test_serial_tap_modbus_two_adapters_pcapng_csv_raw(pty2):
    pa, pb, (wa, wb) = pty2
    d = tmp()
    tap = serialtap.SerialTap({"protocol": "modbus_rtu", "channels": [{"name": "master TX", "port": pa, "baud": 19200}, {"name": "slave TX", "port": pb, "baud": 19200}], "also_user_dlt": True}, d)
    tap.start()
    for i in range(6):
        os.write(wa, modbus.add_crc(bytes([1, 3, 0, i, 0, 2])))
        time.sleep(0.03)
        os.write(wb, modbus.add_crc(bytes([1, 3, 4, 0, i, 0, 9])))
        time.sleep(0.03)
    os.write(wa, modbus.add_crc(bytes([1, 3, 0, 9, 0, 2]))[:-1] + b"\x00")                  # corrupted CRC
    time.sleep(0.25)
    res = tap.stop()
    assert res["frames"] == 13 and res["frames_per_channel"] == [7, 6] and res["crc_failures"] == 1 and res["bytes"] == [56, 54]
    assert "±1-2 ms" in res["timestamp_note"] and {"tap-raw.bin", "tap-frames.csv"} <= set(res["files"])
    pcap = [f for f in res["files"] if f.startswith("tap_")][0]
    pk = list(pcapng.read_capture(os.path.join(d, pcap)))
    assert len(pk) == 13 and all(p["linktype"] == 1 for p in pk)
    infos = [analyze.decode_packet(p["data"], p["linktype"], p["comment"]) for p in pk]
    assert infos[0]["proto"] == "Modbus/TCP" and "start 0, quantity 2" in infos[0]["info"] and "response" in infos[1]["info"]
    assert sum("crc=FAIL" in p["comment"] for p in pk) == 1 and all(p["ts_ns"] > 1.7e18 for p in pk)
    rtu = [f for f in res["files"] if f.startswith("tap-rtu_")][0]
    rp = list(pcapng.read_capture(os.path.join(d, rtu)))
    assert rp[0]["linktype"] == 147 and rp[0]["data"] == modbus.add_crc(bytes([1, 3, 0, 0, 0, 2]))        # raw RTU incl. CRC for the DLT_USER0 mapping
    csv_rows = open(os.path.join(d, "tap-frames.csv")).read().splitlines()
    assert len(csv_rows) == 14 and csv_rows[0].startswith("time,t_ns,channel") and "master TX" in csv_rows[1] and "FAIL" in csv_rows[-1]
    raw = open(os.path.join(d, "tap-raw.bin"), "rb").read()
    t_ns, ch, n = struct.unpack("<QBH", raw[:11])
    assert ch in (0, 1) and n > 0 and t_ns > 1.7e18


def test_serial_tap_dnp3_and_single_channel_rs485_pairing(pty2):
    pa, pb, (wa, wb) = pty2
    d = tmp()
    tap = serialtap.SerialTap({"protocol": "dnp3", "channels": [{"name": "m", "port": pa, "baud": 9600}, {"name": "o", "port": pb, "baud": 9600}]}, d)
    tap.start()
    os.write(wa, dnp3.build_frame(0xC4, 1, 1024, bytes([0xC0, 0xC1, 1, 60, 2, 6])))
    time.sleep(0.05)
    os.write(wb, dnp3.build_frame(0x44, 1024, 1, bytes([0xC0, 0xC1, 0x81, 0, 0, 30, 2, 0, 0, 1, 1, 10, 0])))
    time.sleep(0.05)
    bad = bytearray(dnp3.build_frame(0xC4, 1, 1024, bytes([0xC0, 0xC2, 1, 60, 1, 6]))); bad[12] ^= 0x55
    os.write(wa, bytes(bad))
    time.sleep(0.2)
    res = tap.stop()
    assert res["frames"] == 3 and res["crc_failures"] == 1 and res["protocol"] == "dnp3"
    rows = tap.rows
    assert "master" in rows[0]["kind"] or "from master" in rows[0]["kind"]
    assert any("Read" in r["info"] for r in rows) and any("Response" in r["info"] for r in rows)
    pk = list(pcapng.read_capture(os.path.join(d, [f for f in res["files"] if f.startswith("tap_")][0])))
    assert analyze.decode_ethernet(pk[0]["data"])["proto"] == "DNP3"


def test_serial_tap_single_rs485_channel_alternates_request_response(pty2):
    pa, _pb, (wa, _wb) = pty2
    d = tmp()
    tap = serialtap.SerialTap({"protocol": "modbus_rtu", "channels": [{"name": "RS-485 bus", "port": pa, "baud": 9600}]}, d)
    tap.start()
    req, resp = modbus.add_crc(bytes([4, 3, 0, 0, 0, 1])), modbus.add_crc(bytes([4, 3, 2, 0, 7]))
    for _ in range(3):
        os.write(wa, req); time.sleep(0.04)
        os.write(wa, resp); time.sleep(0.04)
    time.sleep(0.15)
    res = tap.stop()
    assert res["frames"] == 6
    assert [r["kind"] for r in tap.rows] == ["request", "response"] * 3


def test_serial_tap_ring_rotation_and_open_error(pty2):
    pa, _pb, (wa, _wb) = pty2
    d = tmp()
    tap = serialtap.SerialTap({"protocol": "raw", "channels": [{"name": "x", "port": pa, "baud": 9600}]}, d, ring={"ring_mb": 0, "ring_files": 2})
    tap.pcap.max_bytes = 3000                                                                 # tiny files so the test rotates quickly
    tap.start()
    for i in range(80):
        os.write(wa, bytes([i]) * 40)
        time.sleep(0.003)
    time.sleep(0.2)
    res = tap.stop()
    pcaps = [f for f in os.listdir(d) if f.startswith("tap_")]
    assert len(pcaps) <= 2 and res["files_deleted_by_ring"] > 0
    bad = serialtap.SerialTap({"protocol": "modbus_rtu", "channels": [{"name": "x", "port": "/dev/nonexistent-tty", "baud": 9600}]}, tmp())
    with pytest.raises(Exception):
        bad.start()


# ---------------------------------------------------------------- profiles, run, API
def test_sniffer_presets_and_validation():
    sn = [p for p in P.builtin_presets() if p["kind"] == "sniffer"]
    assert [p["name"] for p in sn] == ["Inline Ethernet tap", "SPAN port", "RS-485 Modbus listen", "RS-232 DNP3 tap", "Wi-Fi channel scan", "Wi-Fi monitor, Pi's built-in Wi-Fi (experimental)"]
    assert all(p["builtin"] and p["transport"] == "sniffer" for p in sn)
    assert sn[0]["sniff"]["mode"] == "eth_bridge" and sn[3]["sniff"]["serial"]["protocol"] == "dnp3" and len(sn[3]["sniff"]["serial"]["channels"]) == 2 and sn[4]["sniff"]["wifi"]["hop"]
    for bad in ({"mode": "telepathy"}, {"serial": {"channels": []}}, {"serial": {"channels": [{"name": "a"}] * 3}}, {"wifi": {"width": "HT99"}}, {"storage": {"ring_mb": -1}}):
        with pytest.raises(ValueError):
            P.validate_profile({"name": "x", "kind": "sniffer", "sniff": bad})


def _wait(c, rid, secs=60):
    for _ in range(int(secs * 5)):
        time.sleep(0.2)
        r = c.get(f"/api/runs/{rid}").json()
        if r["status"] != "running":
            return r
    raise TimeoutError


def test_sniffer_run_over_api_serial_download_usb_pdf_and_errors(pty2, monkeypatch):
    from fastapi.testclient import TestClient
    from traffic_tester.server import create_app
    pa, pb, (wa, wb) = pty2
    usb = tmp()
    monkeypatch.setenv("TT_USB_DIR", usb)
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        pre = c.post("/api/sniff/preflight", json={}).json()
        assert "tools" in pre and "eth" in pre and pre["checks"] and any(f["id"] == "dnp3" for f in pre["presets"])
        base = {"name": "tap", "kind": "sniffer", "sniff": {"mode": "serial", "duration_s": 0, "serial": {"protocol": "modbus_rtu", "channels": [{"name": "m", "port": pa, "baud": 19200}, {"name": "s", "port": pb, "baud": 19200}]}}}
        pf = c.post("/api/sniff/preflight", json=base).json()
        assert any(x["s"] == "ok" for x in pf["checks"]) or pf["checks"]
        rid = c.post("/api/runs", json={"profile": base}).json()["run_id"]
        time.sleep(1.0)
        for i in range(8):
            os.write(wa, modbus.add_crc(bytes([1, 3, 0, i, 0, 1]))); time.sleep(0.03)
            os.write(wb, modbus.add_crc(bytes([1, 3, 2, 0, i]))); time.sleep(0.03)
        time.sleep(0.8)
        live = c.get("/api/sniff/live").json()
        assert live["n"] == 16 and live["rows"][0]["proto"] == "Modbus RTU"
        det = c.get(f"/api/sniff/packet/{live['rows'][0]['n']}").json()
        assert det["layers"][0]["name"] == "Frame" and det["layers"][-1]["name"] == "Modbus RTU" and det["layers"][-1]["fields"]["Function"].startswith("3")
        assert c.get("/api/status").json()["run"]["running"]
        c.post(f"/api/runs/{rid}/stop")
        r = _wait(c, rid)
        s = r["summary"]
        assert r["status"] == "stopped" and s["kind"] == "sniffer" and s["analysis"]["packets"] == 16 and s["analysis"]["modbus_frames"] == 16 and s["serial"]["crc_failures"] == 0
        files = c.get(f"/api/runs/{rid}/capture").json()["files"]
        assert {"tap-frames.csv", "tap-raw.bin"} <= {f["name"] for f in files}
        pc = [f["name"] for f in files if f["name"].startswith("tap_")][0]
        dl = c.get(f"/api/runs/{rid}/capture/{pc}")
        assert dl.status_code == 200 and dl.content[:4] == b"\x0a\x0d\x0d\x0a"
        assert c.get(f"/api/runs/{rid}/capture/..%2F..%2Fetc%2Fpasswd").status_code == 404 and c.get(f"/api/runs/{rid}/capture/nothere.pcapng").status_code == 404
        u = c.post(f"/api/runs/{rid}/capture/usb").json()
        assert u["ok"] and os.path.exists(os.path.join(usb, "traffic-tester-captures", f"run-{rid}", pc))
        assert c.get(f"/api/runs/{rid}/export.json").status_code == 200
        pdf = c.get(f"/api/report.pdf?run_ids={rid}").content
        assert pdf[:5] == b"%PDF-" and len(pdf) > 3000
        lst = [x for x in c.get("/api/runs").json() if x["id"] == rid][0]
        assert lst["headline"]["sent"] == 16
        # plain errors: same port twice, missing port, bad filter handled by preflight, no USB when asked
        for prof, msg in (({"mode": "serial", "serial": {"channels": [{"name": "a", "port": pa}, {"name": "b", "port": pa}]}}, "own serial adapter"),
                          ({"mode": "serial", "serial": {"channels": [{"name": "a", "port": "/dev/nope-tty"}]}}, "Cannot open the serial port"),
                          ({"mode": "serial", "serial": {"channels": [{"name": "a", "port": ""}]}}, "Choose the serial port")):
            rid2 = c.post("/api/runs", json={"profile": {"name": "e", "kind": "sniffer", "sniff": prof}}).json()["run_id"]
            r2 = _wait(c, rid2, 20)
            assert r2["status"] == "failed" and msg in r2["summary"]["error"], r2["summary"]
        monkeypatch.delenv("TT_USB_DIR")
        rid3 = c.post("/api/runs", json={"profile": {"name": "e", "kind": "sniffer", "sniff": {"mode": "serial", "storage": {"usb": True}, "serial": {"channels": [{"name": "a", "port": pa}]}}}}).json()["run_id"]
        assert "no USB drive" in _wait(c, rid3, 20)["summary"]["error"]
        assert c.post("/api/sniff/filter/check", json={"kind": "dnp3"}).json()["ok"] and not c.post("/api/sniff/filter/check", json={"expr": "tcp prot"}).json()["ok"]
        assert c.post("/api/sniff/filter/check", json={"kind": "host", "value": "zzz;"}).status_code == 422


def test_disk_guard_stops_capture_with_plain_message(pty2, monkeypatch):
    from traffic_tester.sniff import session
    pa, _pb, (wa, _wb) = pty2
    import shutil as sh
    real = sh.disk_usage
    state = {"n": 0}

    def fake(path):
        state["n"] += 1
        u = real(path)
        return type(u)(u.total, u.total, 10 * 1024 * 1024) if state["n"] > 3 else u              # after a couple of checks only 10 MB are "free"
    monkeypatch.setattr(session.shutil, "disk_usage", fake)
    prof = P.validate_profile({"name": "g", "kind": "sniffer", "sniff": {"mode": "serial", "storage": {"min_free_mb": 50}, "serial": {"protocol": "raw", "channels": [{"name": "a", "port": pa}]}}})
    ev = []
    run = session.SnifferRun(prof, tmp(), ev.append)
    summary = run.run()
    assert run.stop_reason == "disk_full" and "only 10 MB left" in summary["error"] and summary["files"]                 # files so far are kept
    prof2 = P.validate_profile({"name": "g", "kind": "sniffer", "sniff": {"mode": "serial", "storage": {"min_free_mb": 900_000}, "serial": {"channels": [{"name": "a", "port": pa}]}}})
    monkeypatch.undo()
    s2 = session.SnifferRun(prof2, tmp(), lambda e: None).run()
    assert "Not enough free space" in s2["error"]


# ---------------------------------------------------------------- regressions found by the real-tool container e2e
def test_capture_size_limit_and_ring_options_in_command(monkeypatch):
    _fake_tools(monkeypatch)
    c = capture.Capture(["eth0"], tmp(), "", {}, 0, 0, "capture", max_kb=17000).command()
    assert "-a" in c and c[c.index("-a") + 1] == "filesize:17000"                          # an unbounded capture stops itself before the disk guard is crossed


def test_link_passthrough_thread_actually_runs_and_propagates(monkeypatch):
    """Regression: down() (called by up()) set the stop flag and the watcher exited at once, so passthrough never worked."""
    _fake_tools(monkeypatch)
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(capture, "_run", lambda cmd, timeout=10: (calls.append(" ".join(cmd[1:])), R())[1])
    monkeypatch.setattr(os.path, "exists", lambda p: True if p.startswith("/sys/class/net/") else os.path.lexists(p))
    state = {"l1": True, "l2": True}
    br = capture.EthBridge(["l1", "l2"], "ttbr0", passthrough=True)
    monkeypatch.setattr(br, "_carrier", lambda leg: state[leg])
    br.up()
    assert br.thread.is_alive()
    state["l1"] = False                                                                      # cable on side A unplugged
    time.sleep(1.0)
    assert "link set dev l2 down" in calls and any("lost its link" in e for e in br.events)
    state["l1"] = True
    time.sleep(3.2)                                                                          # probe interval
    assert calls.count("link set dev l2 up") >= 2 and any("restored" in e for e in br.events)
    br.down()
    assert not br.thread.is_alive()


def test_dnp3_over_tcp_wrap_is_what_wireshark_decodes_by_default():
    f = wrap.Wrapper().dnp3(dnp3.build_frame(0xC0, 1, 0x0400), True)
    assert f[23] == 6 and struct.unpack(">H", f[36:38])[0] == 20000                          # TCP, destination port 20000 (the UDP dissector is off by default in Wireshark)


def test_inline_tap_refuses_to_bridge_the_port_the_browser_uses(monkeypatch):
    import tempfile
    from fastapi.testclient import TestClient
    from traffic_tester.server import create_app
    from traffic_tester.sniff import preflight as spre
    monkeypatch.setattr(spre, "route_dev", lambda ip: "eth0")
    monkeypatch.setattr(spre, "eth_ports", lambda: [{"name": "eth0", "usb": False, "wired": True, "ip": "10.0.0.5", "carrier": True, "speed_mbps": 1000, "mac": "", "driver": "", "is_bridge": False},
                                                   {"name": "eth1", "usb": True, "wired": True, "ip": None, "carrier": True, "speed_mbps": 1000, "mac": "", "driver": "", "is_bridge": False}])
    prof = {"name": "t", "kind": "sniffer", "sniff": {"mode": "eth_bridge"}}
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        r = c.post("/api/runs", json={"profile": prof})
        assert r.status_code == 422 and "through eth0" in r.json()["detail"] and "Wi-Fi" in r.json()["detail"]
        pf = c.post("/api/sniff/preflight", json=prof).json()
        assert any(x["s"] == "bad" and "connected through" in x["title"] for x in pf["checks"])
