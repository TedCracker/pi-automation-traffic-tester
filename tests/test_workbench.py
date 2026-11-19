"""Sniffer workbench: display filter, delta / response time / retransmissions / flows, opening and saving captures, and the SNMP, IEC 104, PTP, GOOSE and R-GOOSE decoders."""
import os
import struct
import tempfile
import time

import pytest

from traffic_tester import goose
from traffic_tester.sniff import analyze, display, dnp3, pcapng, protocols, workbench


# ---------------------------------------------------------------- packet builders
def _ip(src, dst, proto, body):
    h = struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(body), 1, 0, 64, proto, 0) + bytes(map(int, src.split("."))) + bytes(map(int, dst.split(".")))
    return h + body


def eth(payload, et=0x0800, dst=b"\x02\0\0\0\0\x02", src=b"\x02\0\0\0\0\x01", vlan=None):
    if vlan is not None:
        return dst + src + struct.pack(">HHH", 0x8100, vlan, et) + payload
    return dst + src + struct.pack(">H", et) + payload


def tcp(src, dst, sp, dp, seq, payload=b"", flags=0x18, ack=1):
    return eth(_ip(src, dst, 6, struct.pack(">HHIIBBHHH", sp, dp, seq, ack, 0x50, flags, 8192, 0, 0) + payload))


def udp(src, dst, sp, dp, payload, **k):
    return eth(_ip(src, dst, 17, struct.pack(">HHHH", sp, dp, 8 + len(payload), 0) + payload), **k)


def tlv(tag, v):
    return bytes([tag]) + (bytes([len(v)]) if len(v) < 128 else bytes([0x81, len(v)])) + v


def snmp_msg(pdu_tag, rid, binds, community=b"public", ver=1, err=0):
    vb = b"".join(tlv(0x30, tlv(0x06, oid) + val) for oid, val in binds)
    pdu = tlv(pdu_tag, tlv(2, bytes([rid])) + tlv(2, bytes([err])) + tlv(2, b"\0") + tlv(0x30, vb))
    return tlv(0x30, tlv(2, bytes([ver])) + tlv(4, community) + pdu)


SYSDESCR = bytes([0x2B, 6, 1, 2, 1, 1, 1, 0])


# ---------------------------------------------------------------- decoders
def test_snmp_get_and_response():
    g = protocols.decode_snmp(snmp_msg(0xA0, 7, [(SYSDESCR, tlv(5, b""))]))
    assert g["proto"] == "SNMP" and g["kind"] == "get-request" and "sysDescr.0" in g["summary"] and g["fields"]["Community"] == "public" and g["fields"]["Request id"] == 7
    r = protocols.decode_snmp(snmp_msg(0xA2, 7, [(SYSDESCR, tlv(4, b"Recloser RTU"))]))
    assert r["kind"] == "get-response" and '"Recloser RTU"' in r["summary"] and r["fields"]["Error status"].startswith("0")
    e = protocols.decode_snmp(snmp_msg(0xA2, 8, [(SYSDESCR, tlv(5, b""))], err=2))
    assert "noSuchName" in e["summary"]
    assert protocols.decode_snmp(b"\x30\x03\x02\x01\x09") is None and protocols.decode_snmp(b"junk") is None and protocols.decode_snmp(snmp_msg(0xA0, 1, [])[:9]) is None


def test_snmp_trap_v1_and_v3_header():
    ent = tlv(6, bytes([0x2B, 6, 1, 4, 1, 99]))
    tr = tlv(0xA4, ent + tlv(0x40, bytes([10, 0, 0, 9])) + tlv(2, b"\x02") + tlv(2, b"\x00") + tlv(0x43, b"\x01") + tlv(0x30, b""))
    d = protocols.decode_snmp(tlv(0x30, tlv(2, b"\0") + tlv(4, b"public") + tr))
    assert d["kind"] == "trap (v1)" and d["fields"]["Agent address"] == "10.0.0.9" and d["fields"]["Generic trap"] == 2
    v3 = protocols.decode_snmp(tlv(0x30, tlv(2, b"\3") + tlv(0x30, tlv(2, b"\x05"))))
    assert v3["kind"] == "v3" and "id 5" in v3["summary"]


def test_iec104_frames():
    u = protocols.decode_iec104(bytes([0x68, 4, 0x07, 0, 0, 0]))
    assert u["proto"] == "IEC 104" and "STARTDT act" in u["summary"]
    s = protocols.decode_iec104(bytes([0x68, 4, 0x01, 0, 0x0A, 0]))
    assert "S-format" in s["fields"]["Frame"] and "ack 5" in s["summary"]
    # I-format: M_ME_NC_1 (13) float, spontaneous, CA 1, IOA 4001 = 50.0
    asdu = bytes([13, 1, 3, 0, 1, 0]) + (4001).to_bytes(3, "little") + struct.pack("<f", 50.0) + b"\x00"
    i = protocols.decode_iec104(bytes([0x68, 4 + len(asdu), 4, 0, 2, 0]) + asdu)
    assert "M_ME_NC_1" in i["fields"]["Type"] and "spontaneous" in i["fields"]["Cause of transmission"] and i["fields"]["Values"] == ["IOA 4001: 50"] and "IOA 4001: 50" in i["summary"]
    # sequence of 3 single points, general interrogation
    sp = bytes([1, 0x83, 20, 0, 1, 0]) + (100).to_bytes(3, "little") + b"\x01\x00\x01"
    d = protocols.decode_iec104(bytes([0x68, 4 + len(sp), 0, 0, 0, 0]) + sp)
    assert d["fields"]["Values"] == ["IOA 100: ON", "IOA 101: OFF", "IOA 102: ON"]
    gi = protocols.decode_iec104(bytes([0x68, 14, 6, 0, 2, 0, 100, 1, 6, 0, 1, 0, 0, 0, 0, 20]))
    assert "interrogation" in gi["summary"] and "activation" in gi["summary"]
    assert protocols.decode_iec104(b"\x68\x04") is None
    two = protocols.decode_iec104(bytes([0x68, 4, 0x43, 0, 0, 0, 0x68, 4, 0x83, 0, 0, 0]))
    assert "TESTFR act" in two["summary"] and "TESTFR con" in two["summary"]
    assert protocols.decode_iec104(bytes([0x68, 40, 0, 0, 0, 0]))  is None or not protocols.decode_iec104(bytes([0x68, 40, 0, 0, 0, 0]))["crc_ok"]


def test_ptp_sync_and_announce():
    hdr = lambda mt, ln, seq: bytes([mt, 2]) + struct.pack(">H", ln) + bytes([0, 0]) + struct.pack(">H", 0x0200) + struct.pack(">q", 0) + bytes(4) + bytes.fromhex("0011223344556677") + struct.pack(">HH", 1, seq) + bytes([0, 0])
    sync = hdr(0, 44, 77) + (1700000000).to_bytes(6, "big") + struct.pack(">I", 123456789)
    d = protocols.decode_ptp(sync)
    assert d["proto"] == "PTP" and d["kind"] == "Sync" and "seq 77" in d["summary"] and d["fields"]["Origin timestamp"] == "1700000000.123456789 s" and "two-step" in d["fields"]["Flags"]
    ann = hdr(11, 64, 5) + bytes(10) + struct.pack(">h", 37) + b"\0" + bytes([128, 6, 0x21, 0x4E, 0x5D, 128]) + bytes.fromhex("aabbccddeeff0011") + struct.pack(">H", 2) + bytes([0x20])
    a = protocols.decode_ptp(ann)
    assert "GM aabbccddeeff0011" in a["summary"] and a["fields"]["Time source"] == "GPS" and a["fields"]["Steps removed"] == 2
    assert protocols.decode_ptp(b"\xff" * 40) is None and protocols.decode_ptp(b"\x00\x02") is None


def _goose_pdu(st, sq):
    return goose.pdu_encode({"gocb": "IED1/LLN0$GO$gcb1", "tal_ms": 2000, "dat_set": "IED1/LLN0$DS1", "go_id": "TRIP1", "t_ns": 1_700_000_000_000_000_000, "st": st, "sq": sq, "sim": False, "conf_rev": 3, "nds": False,
                             "data": [("b", True), ("i", -5)]})


def test_goose_l2_and_rgoose():
    pdu = _goose_pdu(4, 0)
    f = goose.l2_build("01:0c:cd:01:00:01", bytes.fromhex("0200000000aa"), False, 0, 4, 0x1001, False, pdu)
    r = analyze.decode_ethernet(f)
    assert r["proto"] == "GOOSE" and "stNum 4 sqNum 0" in r["info"] and "state change" in r["info"] and r["app"]["fields"]["goID"] == "TRIP1" and r["app"]["fields"]["Data"] == ["[0] true", "[1] -5"]
    assert r["app"]["fields"]["confRev"] == 3 and r["app"]["fields"]["Time allowed to live"] == "2000 ms" and r["app"]["fields"]["APPID"] == "0x1001"
    rep = analyze.decode_ethernet(goose.l2_build("01:0c:cd:01:00:01", bytes(6), True, 100, 4, 0x1001, False, _goose_pdu(4, 9)))
    assert "sqNum 9" in rep["info"] and "state change" not in rep["info"] and rep["vlan"]["id"] == 100
    rg = analyze.decode_ethernet(udp("10.7.0.1", "224.0.1.2", 40000, 102, goose.l3_build(77, 0x1002, False, pdu)))
    assert rg["proto"] == "R-GOOSE" and "stNum 4" in rg["info"] and rg["app"]["fields"]["SPDU number"] == 77 and rg["app"]["fields"]["APPID"] == "0x1002"
    assert analyze.decode_ethernet(udp("10.7.0.1", "224.0.1.2", 40000, 102, b"\x01\x40\xa1\x01"))["proto"] == "UDP"


def test_new_protocols_in_ethernet_frames_have_layers_with_ranges():
    f = udp("10.0.0.2", "10.0.0.9", 40000, 161, snmp_msg(0xA0, 1, [(SYSDESCR, tlv(5, b""))]))
    r = analyze.decode_ethernet(f)
    assert r["proto"] == "SNMP" and r["sip"] == "10.0.0.2" and r["dport"] == 161
    ls = analyze.layers(f, 1)
    assert [x["name"] for x in ls] == ["Ethernet", "IPv4", "UDP", "SNMP"] and ls[0]["range"] == [0, 14] and ls[1]["range"] == [14, 34] and ls[2]["range"] == [34, 42] and ls[3]["range"] == [42, len(f)]
    t = tcp("10.0.0.5", "10.0.0.90", 49152, 2404, 1, bytes([0x68, 4, 0x07, 0, 0, 0]))
    assert analyze.decode_ethernet(t)["proto"] == "IEC 104"
    p = eth(bytes([0, 2]) + bytes(2) + bytes(30)[:0] + bytes(30) + bytes(10), 0x88F7)
    assert analyze.decode_ethernet(p)["proto"] == "PTP"
    s = analyze.summarize_files([_write([(1, f), (2, t)])])
    assert s["decoded"] == {"SNMP": 1, "IEC 104": 1} and s["dnp3_frames"] == 0 and s["modbus_frames"] == 0


def _write(pkts, name="a.pcapng"):
    d = tempfile.mkdtemp(prefix="tt-wb-")
    w = pcapng.PcapngWriter(os.path.join(d, name), [(1, "eth0", "")])
    for ts, data in pkts:
        w.write(0, int(ts * 1e9), data)
    w.close()
    return os.path.join(d, name)


# ---------------------------------------------------------------- analyzer: delta, response time, retransmission, flows
def dnp_payload(master):
    return dnp3.build_frame(0xC4 if master else 0x44, 1, 1024, bytes([0xC0, 0xC1, 1, 60, 2, 6]) if master else bytes([0xC0, 0x81, 0, 0]))


def scenario():
    M, O = "192.0.2.5", "192.0.2.90"
    return [
        (1.000, tcp(M, O, 49152, 20000, 100, dnp_payload(True))),               # request
        (1.144, tcp(O, M, 20000, 49152, 500, dnp_payload(False))),               # response 144 ms later
        (1.145, tcp(M, O, 49152, 20000, 100 + len(dnp_payload(True)), b"", 0x10)),  # ack
        (2.000, tcp(M, O, 49152, 20000, 100 + len(dnp_payload(True)), dnp_payload(True))),   # next request
        (2.802, tcp(M, O, 49152, 20000, 100 + len(dnp_payload(True)), dnp_payload(True))),   # retransmission of it
        (2.900, tcp(O, M, 20000, 49152, 500 + len(dnp_payload(False)), dnp_payload(False), flags=0x18)),   # response
    ]


def run(pkts):
    an = workbench.Analyzer()
    return an, [an.add(int(t * 1e9), d, 1) for t, d in pkts]


def test_analyzer_delta_response_retransmission():
    an, rows = run(scenario())
    assert rows[0]["delta_ms"] == 0 and rows[0]["resp_ms"] is None and rows[0]["dir"] == "→"
    assert rows[1]["resp_ms"] == 144.0 and rows[1]["delta_ms"] == 144.0 and rows[1]["dir"] == "←" and rows[1]["proto"] == "DNP3"
    assert rows[2]["delta_ms"] == 1.0 and rows[2]["resp_ms"] is None                     # an ack is not a response
    assert rows[4]["retrans"] and rows[4]["info"].startswith("Retransmission") and not rows[3].get("retrans")
    assert rows[5]["resp_ms"] == 900.0                                                   # measured from the first (original) request, not the retransmission
    fl = an.flow_rows()
    assert len(fl) == 1 and fl[0]["packets"] == 6 and fl[0]["retrans"] == 1 and fl[0]["responses"] == 2 and fl[0]["resp_max_ms"] == 900.0 and fl[0]["proto"] == "DNP3" and fl[0]["label"] == ".5 ⇄ .90:20000"
    tm = workbench.timing(rows)
    assert tm["responses"] == 2 and tm["resp_min_ms"] == 144.0 and tm["resp_max_ms"] == 900.0


def test_two_conversations_are_separate_flows_and_udp_one_way_has_no_response():
    pk = [(1.0, tcp("10.0.0.1", "10.0.0.2", 1111, 502, 1, b"x")), (1.1, udp("10.0.0.60", "10.0.0.7", 5004, 5004, b"v" * 100)), (1.2, udp("10.0.0.60", "10.0.0.7", 5004, 5004, b"v" * 100)),
          (1.3, eth(b"\0" * 28, 0x0806))]
    an, rows = run(pk)
    fl = {f["a"] + f["b"]: f for f in an.flow_rows()}
    assert len(fl) == 3 and fl["10.0.0.60:500410.0.0.7:5004"]["packets"] == 2 and fl["10.0.0.60:500410.0.0.7:5004"]["responses"] == 0
    assert rows[2]["delta_ms"] == 100.0                                                    # since the previous packet of THAT conversation


# ---------------------------------------------------------------- display filter
def test_display_filter_words_and_logic():
    an, rows = run(scenario() + [(3.0, udp("10.0.0.60", "10.0.0.7", 5004, 5004, b"v" * 100)), (3.1, udp("10.0.0.2", "10.0.0.9", 40000, 161, snmp_msg(0xA0, 1, [(SYSDESCR, tlv(5, b""))])))])
    n = lambda f: [r["n"] for r in rows if display.compile_filter(f)(r)]
    assert n("") == list(range(1, 9)) and n("dnp3") == [1, 2, 4, 5, 6] and n("host 192.0.2.90") == [1, 2, 3, 4, 5, 6] and n("src 192.0.2.90") == [2, 6]
    assert n("port 20000 and not dnp3") == [3] and n("snmp") == [8] and n("udp and not snmp") == [7] and n("retrans") == [5] and n("response") == [2, 6]
    assert n("resp > 500") == [6] and n("delta >= 800") == [4, 5, 6]
    assert n("len > 100") == [r["n"] for r in rows if r["len"] > 100] and n("dst port 161") == [8] and n("ip.addr == 10.0.0.60") == [7] and n("tcp.port == 20000") == [1, 2, 3, 4, 5, 6]
    assert n("192.0.2.5 and (retrans or response)") == [2, 5, 6] and n('"sysDescr"') == [8] and n("host 10.10.10. ") == [1, 2, 3, 4, 5, 6] and n("host 192.0.2.0/24") == [1, 2, 3, 4, 5, 6]
    assert n("not (tcp or udp)") == []


@pytest.mark.parametrize("bad", ["host", "len >", "port abc", "(host 1.2.3.4", "host 1.2.3.4 )", "dnp3 and", "len > x", "vlan abc", "\"unclosed"])
def test_display_filter_plain_errors(bad):
    err = display.check(bad)
    assert err and "Traceback" not in err and len(err) < 120


def test_display_filter_errors_vlan_mac():
    f = eth(b"\x00" * 46, 0x88B5, vlan=0x6064)
    an = workbench.Analyzer()
    r = an.add(1, f, 1)
    assert display.compile_filter("vlan 100")(r) and not display.compile_filter("vlan 5")(r) and display.compile_filter("mac 02:00:00:00:00:01")(r)
    bad = an.add(2, b"\x00" * 6, 1)
    assert display.compile_filter("errors")(bad) and not display.compile_filter("errors")(r)


# ---------------------------------------------------------------- open a saved capture, save only what is shown
def test_open_capture_filter_detail_and_save_shown():
    path = _write(scenario() + [(3.0, udp("10.0.0.60", "10.0.0.7", 5004, 5004, b"v" * 100))])
    c = workbench.open_capture("t:1", [path])
    assert c.view()["total"] == 7 and c.view("dnp3")["shown"] == 5 and c.view("dnp3", after=2)["rows"][0]["n"] == 4 and c.view("errors")["shown"] == 0
    assert workbench.open_capture("t:1", [path]) is c                                       # cached while the file is unchanged
    d = c.detail(2)
    assert d["layers"][0]["name"] == "Frame" and "144.0 ms after the request" in d["layers"][0]["fields"]["Response time"] and [x["name"] for x in d["layers"]][1:] == ["Ethernet", "IPv4", "TCP", "DNP3"]
    assert len(bytes.fromhex(d["hex"])) == d["len"] and d["layers"][-1]["range"][1] == d["len"]
    out = os.path.join(os.path.dirname(path), "shown.pcapng")
    seen, kept = workbench.save_filtered([path], "retrans or response", out)
    assert (seen, kept) == (7, 3)
    assert [p["ts_ns"] for p in pcapng.read_capture(out)] == [int(1.144e9), int(2.802e9), int(2.9e9)]
    assert workbench.save_filtered([path], "snmp", out + "2") == (7, 0)
    with pytest.raises(ValueError):
        workbench.save_filtered([path], "host", out)


def test_open_capture_reads_a_file_that_is_still_being_written():
    path = _write(scenario())
    raw = open(path, "rb").read()
    open(path, "wb").write(raw[:-25])                                                        # last block half written
    assert workbench.OpenCapture("x", [path]).view()["total"] == 5


# ---------------------------------------------------------------- over the API: a live serial tap, then the same capture reopened
def test_workbench_api_live_serial_then_reopen_and_save_shown():
    import subprocess
    import re
    from fastapi.testclient import TestClient
    from traffic_tester.server import create_app
    from traffic_tester.sniff import modbus

    def pair():
        p = subprocess.Popen(["socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0"], stderr=subprocess.PIPE, text=True)
        ptys = []
        while len(ptys) < 2:
            ptys.append(re.search(r"PTY is (\S+)", p.stderr.readline()).group(1))
        return p, ptys
    p1, a = pair()
    p2, b = pair()
    wa, wb = os.open(a[0], os.O_RDWR), os.open(b[0], os.O_RDWR)
    try:
        with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
            prof = {"name": "tap", "kind": "sniffer", "sniff": {"mode": "serial", "duration_s": 0, "serial": {"protocol": "modbus_rtu", "channels": [{"name": "Master", "port": a[1], "baud": 19200}, {"name": "Slave", "port": b[1], "baud": 19200}]}}}
            rid = c.post("/api/runs", json={"profile": prof}).json()["run_id"]
            time.sleep(1.0)
            for i in range(6):
                os.write(wa, modbus.add_crc(bytes([1, 3, 0, i, 0, 1])))
                time.sleep(0.05)
                os.write(wb, modbus.add_crc(bytes([1, 3, 2, 0, i])))
                time.sleep(0.05)
            os.write(wa, modbus.add_crc(bytes([1, 3, 0, 9, 0, 1]))[:-1] + b"\x00")             # a damaged frame
            time.sleep(0.8)
            v = c.get("/api/sniff/view").json()
            assert v["total"] == 13 and v["shown"] == 13 and v["errors"] == 1 and v["rows"][0]["proto"] == "Modbus RTU"
            resp = [r for r in v["rows"] if r["resp_ms"] is not None]
            assert len(resp) == 6 and all(0 < r["resp_ms"] < 400 for r in resp)
            assert c.get("/api/sniff/view", params={"filter": "errors"}).json()["shown"] == 1
            assert c.get("/api/sniff/view", params={"filter": "channel Slave"}).json()["shown"] == 6
            assert c.get("/api/sniff/view", params={"filter": "host"}).status_code == 422
            assert c.get("/api/sniff/view", params={"after": v["rows"][-1]["n"]}).json()["rows"] == []
            assert c.post("/api/sniff/display/check", json={"expr": "len >"}).json()["ok"] is False
            fl = c.get("/api/sniff/flows").json()["flows"]
            assert fl[0]["packets"] == 13 and fl[0]["responses"] == 6 and fl[0]["errors"] == 1
            sm = c.get("/api/sniff/summary").json()
            assert sm["protocols"][0] == {"proto": "Modbus RTU", "packets": 13, "bytes": sm["protocols"][0]["bytes"]} and sm["timing"]["responses"] == 6
            d = c.get(f"/api/sniff/packet/{v['rows'][0]['n']}").json()
            assert d["proto"] == "Modbus RTU"
            sv = c.post("/api/sniff/save-shown", json={"filter": "channel Slave"})
            assert sv.status_code == 200 and sv.headers["x-packets"] == "6" and sv.content[:4] == b"\x0a\x0d\x0d\x0a"
            assert c.post("/api/sniff/save-shown", json={"filter": "snmp"}).status_code == 409
            c.post(f"/api/runs/{rid}/stop")
            r = _wait_run(c, rid)
            assert r["status"] == "stopped"
            o = c.get("/api/sniff/view", params={"src": f"run:{rid}"}).json()
            assert o["total"] == 13 and o["errors"] == 1
            o2 = c.get("/api/sniff/view", params={"src": f"run:{rid}", "filter": "response"}).json()
            assert o2["shown"] == 6
            assert c.get("/api/sniff/flows", params={"src": f"run:{rid}"}).json()["flows"][0]["responses"] == 6
            det = c.get("/api/sniff/packet/2", params={"src": f"run:{rid}"}).json()
            assert det["layers"][0]["name"] == "Frame"
            kept = c.post("/api/sniff/save-shown", json={"src": f"run:{rid}", "filter": "errors", "keep": True}).json()
            assert kept["ok"] and kept["packets"] == 1 and kept["name"].startswith("shown-")
            assert kept["name"] in [f["name"] for f in c.get(f"/api/runs/{rid}/capture").json()["files"]]
            assert c.get(f"/api/runs/{rid}/capture/{kept['name']}").status_code == 200
            again = c.get("/api/sniff/view", params={"src": f"run:{rid}"}).json()
            assert again["total"] == 13                                                       # the saved subset is not read back in as part of the capture
            assert c.get("/api/sniff/view", params={"src": "run:99999"}).status_code == 404 and c.get("/api/sniff/view", params={"src": "bogus"}).status_code == 422
    finally:
        os.close(wa), os.close(wb)
        p1.terminate(), p2.terminate()


def _wait_run(c, rid, secs=30):
    for _ in range(int(secs * 5)):
        time.sleep(0.2)
        r = c.get(f"/api/runs/{rid}").json()
        if r["status"] != "running":
            return r
    raise TimeoutError
