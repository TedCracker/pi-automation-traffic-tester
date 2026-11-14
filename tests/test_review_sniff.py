"""Regression tests for the 2026-11 review of the traffic sniffer (traffic_tester/sniff). No root, no real ports, no capture tools:
everything runs on fakes, pipes and temp dirs."""
import os
import struct
import tempfile
import threading
import time
import types
from collections import Counter

import pytest

from traffic_tester import profiles as P
from traffic_tester import system
from traffic_tester.sniff import analyze, bpf, capture, dnp3, pcapng, serialtap, session, wrap
from traffic_tester.sniff import preflight as spre


def tmp():
    return tempfile.mkdtemp(prefix="tt-review-")


def _fake_tools(monkeypatch, **kw):
    t = {"dumpcap": "/usr/bin/dumpcap", "tcpdump": "/usr/bin/tcpdump", "tshark": "/usr/bin/tshark", "iw": "/usr/sbin/iw", "ip": "/usr/sbin/ip", "ethtool": "/usr/sbin/ethtool", "engine": "dumpcap"}
    t.update(kw)
    monkeypatch.setattr(capture, "find_tools", lambda: t)
    return t


def _sniff_profile(**sniff):
    return P.validate_profile({"name": "t", "kind": "sniffer", "sniff": sniff})


def _port(name, usb=False, wired=True):
    return {"name": name, "usb": usb, "wired": wired, "ip": None, "carrier": True, "speed_mbps": 1000, "mac": "", "driver": "", "is_bridge": False}


# ---------------------------------------------------------------- 1: filter text can never become tcpdump / dumpcap options
def test_filter_starting_with_dash_is_rejected_and_never_reaches_tcpdump(monkeypatch):
    secret = tmp() + "/secret.txt"
    with open(secret, "w") as f:
        f.write("TOP-SECRET-FIRST-LINE\n")
    seen = []
    monkeypatch.setattr(bpf.shutil, "which", lambda n: "/usr/bin/tcpdump")
    monkeypatch.setattr(bpf.subprocess, "run", lambda cmd, **kw: (seen.append(cmd), types.SimpleNamespace(returncode=0, stderr="", stdout=""))[1])
    for bad in ("-V" + secret, "  -G 1 -z /bin/true", "--version"):
        err = bpf.validate(bad)
        assert err == "A capture filter cannot start with '-'." and "TOP-SECRET" not in err
    assert seen == []                                                                         # tcpdump was not even started for them
    assert bpf.validate("tcp port 502") is None
    assert seen[0][-2:] == ["--", "tcp port 502"]                                             # "--" ends the options: the rest is the filter


def test_validate_with_the_real_tcpdump_does_not_leak_a_file():
    if not bpf.shutil.which("tcpdump"):
        pytest.skip("tcpdump is not installed")
    secret = tmp() + "/secret.txt"
    with open(secret, "w") as f:
        f.write("TOP-SECRET-FIRST-LINE\n")
    err = bpf.validate("-V" + secret)
    assert err and "TOP-SECRET" not in err and "cannot start with '-'" in err
    assert bpf.validate("not tcp port 8080") is None and "not valid" in bpf.validate("tcp prot 5")      # "--" did not break normal filters


def test_capture_commands_keep_the_filter_out_of_the_options(monkeypatch):
    _fake_tools(monkeypatch, dumpcap=None, engine="tcpdump")
    cmd = capture.Capture(["eth0"], tmp(), "tcp port 502", {"ring_mb": 20, "ring_files": 4}).command()
    assert cmd[cmd.index("--") + 1:] == ["tcp", "port", "502"] and cmd.index("--") > cmd.index("-W")
    for engine_tools in ({"dumpcap": None, "engine": "tcpdump"}, {}):                         # tcpdump fallback and dumpcap
        _fake_tools(monkeypatch, **engine_tools)
        with pytest.raises(capture.CaptureError) as e:
            capture.Capture(["eth0"], tmp(), " -G 1 -z /bin/true")
        assert str(e.value) == "A capture filter cannot start with '-'."


# ---------------------------------------------------------------- 2: every clean-up step is attempted
class _FakeBridge:
    made = []

    def __init__(self, legs, name="ttbr0", passthrough=False):
        self.legs, self.name, self.events, self.downs = legs, name, [], 0
        _FakeBridge.made.append(self)

    def up(self):
        pass

    def down(self, quiet=False):
        self.downs += 1


def test_tcpdump_both_legs_failure_still_takes_the_bridge_down(monkeypatch):
    """tcpdump cannot capture two ports: the start fails, and Capture.stop() used to raise again in the clean-up so ttbr0 stayed up."""
    _fake_tools(monkeypatch, dumpcap=None, engine="tcpdump")
    _FakeBridge.made = []
    monkeypatch.setattr(capture, "EthBridge", _FakeBridge)
    monkeypatch.setattr(spre, "eth_ports", lambda: [_port("eth0"), _port("eth1", usb=True)])
    run = session.SnifferRun(_sniff_profile(mode="eth_bridge", eth={"capture_on": "both_legs"}), tmp())
    summary = run.run()
    assert run.status == "failed" and "only one port" in summary["error"]
    assert len(_FakeBridge.made) == 1 and _FakeBridge.made[0].downs == 1 and "cleanup_error" not in summary
    c = capture.Capture(["a", "b"], tmp())                                                    # and stop() itself no longer rebuilds the command
    with pytest.raises(capture.CaptureError):
        c.start()
    assert c.stop()["cmd"] == ""


def test_cleanup_attempts_every_resource_even_when_earlier_ones_fail(monkeypatch):
    d = tmp()
    rotated = os.path.join(d, "capture.pcap0")                                                # a tcpdump rotation file must also be analysed
    with open(rotated, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1) + struct.pack("<IIII", 1, 0, 60, 60) + bytes(60))

    class BadCapture:
        def stop(self):
            raise RuntimeError("capture stop failed")

        def files(self):
            return [rotated]

    class Res:
        iface, hop, visited, monitor_iface = "wlan1", [], [6], None

        def __init__(self, boom=False):
            self.boom, self.downs, self.stops, self.legs, self.events = boom, 0, 0, ["eth0", "eth1"], []

        def down(self):
            self.downs += 1
            if self.boom:
                raise OSError("bridge down failed")

        def stop(self):
            self.stops += 1
            raise OSError("feeder stop failed")

    run = session.SnifferRun(_sniff_profile(mode="eth_bridge"), d)
    run.capture, run.feeder, run.bridge, run.wifi = BadCapture(), Res(), Res(boom=True), Res()
    info = run._finish()
    assert run.feeder.stops == 1 and run.bridge.downs == 1 and run.wifi.downs == 1            # the bridge and the Wi-Fi restore were both attempted
    assert info["cleanup_error"] == "RuntimeError: capture stop failed" and "capture stop failed" in run.error      # the first error is the one reported
    assert info["bridge_legs"] == ["eth0", "eth1"] and info["wifi"]["interface"] == "wlan1"
    assert info["files"] == [{"name": "capture.pcap0", "bytes": 24 + 16 + 60}] and info["analysis"]["packets"] == 1
    run2 = session.SnifferRun(_sniff_profile(mode="eth_bridge"), d)
    run2.error = "the capture stopped unexpectedly"
    run2.bridge, run2.wifi = Res(boom=True), Res()
    run2._finish()
    assert run2.wifi.downs == 1 and run2.error == "the capture stopped unexpectedly"          # an earlier error is not overwritten


# ---------------------------------------------------------------- 3: link passthrough settles after one unplug / replug
def test_link_passthrough_settles_after_one_unplug_and_replug(monkeypatch):
    _fake_tools(monkeypatch)
    clock = {"t": 100.0}
    cable = {"l1": True, "l2": True}                                                          # is the partner on that cable alive
    admin = {"l1": True, "l2": True}
    ready = {"l1": 0.0, "l2": 0.0}                                                            # a re-enabled port needs 1.5 s to renegotiate
    monkeypatch.setattr(capture, "time", types.SimpleNamespace(monotonic=lambda: clock["t"], strftime=time.strftime, sleep=lambda s: None))
    br = capture.EthBridge(["l1", "l2"], "ttbr0", passthrough=True)
    ops = []

    def fake_ip(*a, check=True):
        leg, what = a[3], a[4]
        ops.append((round(clock["t"] - 100.0, 1), leg, what))
        if what == "up" and not admin[leg]:
            ready[leg] = clock["t"] + 1.5
        admin[leg] = what == "up"
        return ""
    monkeypatch.setattr(br, "_ip", fake_ip)
    monkeypatch.setattr(br, "_carrier", lambda leg: cable[leg] and admin[leg] and clock["t"] >= ready[leg])

    class Evt:                                                                                # drives _watch() with a fake clock, no real waiting
        def wait(self, s):
            clock["t"] += s
            el = clock["t"] - 100.0
            cable["l1"] = not 1.0 <= el < 6.0                                                 # side A unplugged after 1 s, plugged back in at 6 s
            return el > 40.0
    br.stop_evt = Evt()
    br._watch()
    assert [(leg, what) for _t, leg, what in ops] == [("l2", "down"), ("l2", "up")], ops      # one loss, one restore, then quiet (it used to flap for ever)
    assert sum("lost its link" in e for e in br.events) == 1 and sum("restored" in e for e in br.events) == 1
    assert admin == {"l1": True, "l2": True} and not any(br.forced.values())


def test_link_passthrough_still_reports_a_real_loss_after_the_grace_period(monkeypatch):
    _fake_tools(monkeypatch)
    clock = {"t": 100.0}
    cable = {"l1": True, "l2": True}
    admin = {"l1": True, "l2": True}
    monkeypatch.setattr(capture, "time", types.SimpleNamespace(monotonic=lambda: clock["t"], strftime=time.strftime, sleep=lambda s: None))
    br = capture.EthBridge(["l1", "l2"], "ttbr0", passthrough=True)
    downs = []

    def fake_ip(*a, check=True):
        admin[a[3]] = a[4] == "up"
        if a[4] == "down":
            downs.append(a[3])
        return ""
    monkeypatch.setattr(br, "_ip", fake_ip)
    monkeypatch.setattr(br, "_carrier", lambda leg: cable[leg] and admin[leg])

    class Evt:
        def wait(self, s):
            clock["t"] += s
            el = clock["t"] - 100.0
            cable["l1"] = not 1.0 <= el < 6.0
            if el >= 8.0:
                cable["l2"] = False                                                           # the restored side's partner never comes back
            return el > 20.0
    br.stop_evt = Evt()
    br._watch()
    assert downs == ["l2", "l1"]                                                              # after the grace period the dead side B is passed on to side A


# ---------------------------------------------------------------- 4: unplugged serial adapter
def test_serial_adapter_unplugged_stops_the_tap_with_a_plain_error():
    r, w = os.pipe()
    os.close(w)                                                                               # readable, but read() returns b"": what an unplugged adapter looks like
    tap = serialtap.SerialTap({"protocol": "raw", "channels": [{"name": "a", "port": "/dev/ttyUSB9"}]}, tmp())
    t = threading.Thread(target=tap._reader, args=(0, types.SimpleNamespace(fileno=lambda: r)), daemon=True)
    t.start()
    t.join(timeout=3)
    still_running = t.is_alive()
    tap.stop_evt.set()
    t.join(timeout=3)
    os.close(r)
    res = tap.stop()
    assert not still_running                                                                  # it used to spin for ever with error None
    assert tap.error == "Serial adapter /dev/ttyUSB9 was unplugged." and tap.live()["error"] == tap.error and res["frames"] == 0


# ---------------------------------------------------------------- 5: truncated Wi-Fi frames
def _radiotap(frame, freq=2437, sig=-60):
    body = struct.pack("<HH", freq, 0x0480) + struct.pack("<b", sig)
    return struct.pack("<BBHI", 0, 0, 8 + len(body), (1 << 3) | (1 << 5)) + body + frame


def _beacon(ssid=b"PlantNet", bssid=b"\x02\x11\x22\x33\x44\x55", chan=6):
    hdr = bytes([0x80, 0]) + b"\x00\x00" + b"\xff" * 6 + bssid + bssid + b"\x00\x00"
    return _radiotap(hdr + bytes(8) + struct.pack("<HH", 100, 0x0431) + bytes([0, len(ssid)]) + ssid + bytes([3, 1, chan]))


def test_truncated_beacon_never_raises():
    full = _beacon()
    assert analyze.decode_radiotap(full)["ssid"] == "PlantNet" and analyze.decode_radiotap(full)["channel"] == 6
    for n in range(len(full) + 1):                                                            # cut anywhere: radiotap header, 802.11 header, inside an element
        r = analyze.decode_radiotap(full[:n])
        assert r["proto"] in ("802.11", "Malformed")
    ext = struct.pack("<BBHII", 0, 0, 64, 0x80000008, 0x80000000) + b"\x01"                   # claims a 64-byte header with more "present" words than it has
    assert analyze.decode_radiotap(ext)["info"] == "radiotap only / short frame"
    d = tmp()
    w = pcapng.PcapngWriter(d + "/w.pcapng", [(127, "wlan1mon", "")])
    w.write(0, 1_000_000_000, full[:9])
    w.write(0, 1_100_000_000, full[:-1])                                                      # DS element says 1 byte, none is there
    w.write(0, 1_200_000_000, full)
    w.close()
    a = analyze.summarize_files([d + "/w.pcapng"])
    assert a["packets"] == 3 and a["wifi_networks"][0]["beacons"] == 2                        # the packets after the truncated one are not lost


def test_one_undecodable_packet_does_not_end_the_summary_or_the_live_view(monkeypatch):
    real = analyze.decode_packet
    calls = {"n": 0}

    def flaky(data, linktype, comment=""):
        calls["n"] += 1
        if calls["n"] == 2:
            raise IndexError("index out of range")
        return real(data, linktype, comment)
    monkeypatch.setattr(analyze, "decode_packet", flaky)
    d = tmp()
    w = pcapng.PcapngWriter(d + "/w.pcapng", [(127, "wlan1mon", "")])
    for i in range(4):
        w.write(0, 1_000_000_000 + i, _beacon())
    w.close()
    a = analyze.summarize_files([d + "/w.pcapng"])
    assert a["packets"] == 4 and a["protocols"] == {"802.11": 3, "Malformed": 1} and a["errors"] == 1
    # live view: the same stream through LiveFeeder.run(), read from a pipe instead of dumpcap
    _fake_tools(monkeypatch)
    calls["n"] = 0
    feeder = capture.LiveFeeder(["wlan1mon"])

    class Guarded(Counter):                                                                   # 11d: the protocol counter is only touched under the lock
        def __setitem__(self, k, v):
            assert feeder.lock.locked()
            super().__setitem__(k, v)

        def most_common(self, n=None):
            assert feeder.lock.locked()
            return super().most_common(n)
    feeder.proto = Guarded()
    r, wfd = os.pipe()
    with open(d + "/w.pcapng", "rb") as f:
        os.write(wfd, f.read())
    os.close(wfd)
    feeder.proc = types.SimpleNamespace(stdout=os.fdopen(r, "rb"))
    feeder.run()                                                                              # returns at end of stream
    feeder.proc.stdout.close()
    lv = feeder.live()
    assert [x["proto"] for x in lv["rows"]] == ["802.11", "Malformed", "802.11", "802.11"] and lv["rows"][1]["errors"] == 1
    assert lv["protocols"] == {"802.11": 3, "Malformed": 1} and lv["seen"] == lv["decoded"] == 4


# ---------------------------------------------------------------- 6: tcpdump rotation files
def test_tcpdump_rotation_files_are_listed_in_order(monkeypatch):
    _fake_tools(monkeypatch, dumpcap=None, engine="tcpdump")
    d = tmp()
    c = capture.Capture(["eth0"], d, "", {"ring_mb": 1, "ring_files": 12})
    for name in ("capture.pcap10", "capture.pcap0", "capture.pcap2", "capture.pcap", "capture.pcap.tmp", "other.pcap1", "capture.pcapng"):
        with open(os.path.join(d, name), "wb") as f:
            f.write(b"x" * 10)
    assert [os.path.basename(f) for f in c.files()] == ["capture.pcap", "capture.pcap0", "capture.pcap2", "capture.pcap10"]
    assert c.bytes_written() == 40
    _fake_tools(monkeypatch)                                                                  # dumpcap naming keeps working
    d2 = tmp()
    c2 = capture.Capture(["eth0"], d2)
    for name in ("capture_00002_20261110120500.pcapng", "capture_00001_20261110120000.pcapng", "tap_00001_x.pcapng"):
        open(os.path.join(d2, name), "wb").close()
    assert [os.path.basename(f) for f in c2.files()] == ["capture_00001_20261110120000.pcapng", "capture_00002_20261110120500.pcapng"]


# ---------------------------------------------------------------- 7: pre-flight, the cut-off guard and the run pick the same ports
def test_auto_ports_resolve_the_same_in_preflight_and_in_the_run(monkeypatch):
    names = ["enx00e04c680001", "eth0", "eth1", "wlan0"]                                      # the USB adapter sorts before the built-in ports
    monkeypatch.setattr(system, "interfaces", lambda: [{"name": n, "wired": not n.startswith("wl"), "up": True, "mac": ""} for n in names])
    real = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: "/sys/devices/platform/scb/usb2/2-1/net/enx00e04c680001" if str(p) == "/sys/class/net/enx00e04c680001" else real(p, *a, **k))
    monkeypatch.setattr(spre, "route_dev", lambda ip: "eth0")                                 # the browser reaches the Pi through eth0
    _fake_tools(monkeypatch)
    used = {}
    monkeypatch.setattr(session.SnifferRun, "_start_capture", lambda self, ring, bpf, rx_ifs: used.update(rx=list(rx_ifs)))
    _FakeBridge.made = []
    monkeypatch.setattr(capture, "EthBridge", _FakeBridge)
    # mirror port
    span = _sniff_profile(mode="eth_span")
    run = session.SnifferRun(span, tmp())
    run._start_span({}, "")
    pf = spre.preflight(span, "10.0.0.9")
    mirror = next(c for c in pf["checks"] if c["title"] == "Mirror port")
    assert used["rx"] == [spre.span_iface(span)] and f"Capturing on {used['rx'][0]}" in mirror["text"]
    explicit = _sniff_profile(mode="eth_span", eth={"interface": "eth1"})
    run = session.SnifferRun(explicit, tmp())
    run._start_span({}, "")
    assert used["rx"] == ["eth1"] == [spre.span_iface(explicit)]
    # inline tap
    tap = _sniff_profile(mode="eth_bridge")
    legs = spre.bridge_legs(tap)                                                              # what the server's cut-off guard looks at
    assert legs == ("eth0", "enx00e04c680001")
    run = session.SnifferRun(tap, tmp())
    run._start_bridge({}, "")
    assert tuple(_FakeBridge.made[-1].legs) == legs and used["rx"] == list(legs)
    pf = spre.preflight(tap, "10.0.0.9")
    assert any(c["title"] == "Inline ports" and "eth0" in c["text"] and "enx00e04c680001" in c["text"] for c in pf["checks"])
    assert any(c["s"] == "bad" and "connected through" in c["title"] for c in pf["checks"])
    # no USB adapter: the run falls back to the second built-in port, so pre-flight and the guard must say that port too
    names[:] = ["eth0", "eth1", "wlan0"]
    legs = spre.bridge_legs(tap)
    run = session.SnifferRun(tap, tmp())
    run._start_bridge({}, "")
    assert legs == ("eth0", "eth1") == tuple(_FakeBridge.made[-1].legs)
    names[:] = ["eth0", "wlan0"]                                                              # one wired port only: both refuse
    assert spre.bridge_legs(tap) == ("eth0", None)
    with pytest.raises(session.SniffError):
        session.SnifferRun(tap, tmp())._start_bridge({}, "")
    names[:] = ["wlan0"]
    assert spre.span_iface(span) is None
    with pytest.raises(session.SniffError):
        session.SnifferRun(span, tmp())._start_span({}, "")


# ---------------------------------------------------------------- 8: DNP3 framer timestamps and the garbage counter
def test_dnp3_stray_byte_does_not_backdate_the_next_frame():
    f1 = dnp3.build_frame(0xC4, 1, 1024, bytes([0xC0, 0xC1, 1, 60, 2, 6]))
    fr = dnp3.Dnp3Framer()
    assert fr.feed(b"\xff", 1_000) == []                                                      # one stray byte, long before the frame
    out = fr.feed(f1, 9_000_000)
    assert [(x[0], x[1], x[2], x[3]) for x in out] == [(f1, 9_000_000, 9_000_000, True)] and fr.skipped == 1
    fr = dnp3.Dnp3Framer()
    assert fr.feed(b"\x00\x05\x64\x01\x02", 1_000) == []                                      # a false start that fails the header CRC later
    out = fr.feed(bytes(8) + f1, 5_000)
    assert [(x[0], x[1]) for x in out] == [(f1, 5_000)]
    fr = dnp3.Dnp3Framer()
    assert fr.feed(f1[:7], 2_000) == [] and fr.feed(f1[7:], 3_000)[0][1:3] == (2_000, 3_000)  # a real split frame keeps its first-byte time


def test_serial_tap_reports_the_bytes_the_dnp3_framer_skipped():
    tap = serialtap.SerialTap({"protocol": "dnp3", "channels": [{"name": "a", "port": "/dev/null"}]}, tmp())
    f1 = dnp3.build_frame(0xC4, 1, 1024, bytes([0xC0, 0xC1, 1, 60, 2, 6]))
    for fr in tap.framers[0].feed(b"\x00\xff\x11" + f1, 1_000):
        tap._frame(0, *fr)
    res = tap.stop()
    assert res["frames"] == 1 and res["garbage_bytes"] == 3


# ---------------------------------------------------------------- 9: DNP3 object sizes and read requests
def test_dnp3_analog_event_sizes_and_read_request_with_several_headers():
    assert (dnp3.SIZES[(32, 4)], dnp3.SIZES[(32, 5)], dnp3.SIZES[(32, 6)]) == (9, 5, 9)       # flag + 16-bit + 48-bit time / flag + float / flag + double
    for var, size in ((1, 5), (2, 3), (3, 11), (4, 9), (5, 5), (6, 9), (7, 11), (8, 15)):
        objs = bytes([32, var, 0x17, 1, 7]) + bytes(size) + bytes([1, 2, 0x00, 0, 0, 0x81])    # one event (index 7), then g1v2 index 0-0
        d = dnp3.decode_frame(dnp3.build_frame(0x44, 1024, 1, bytes([0xC1, 0xC1, 0x81, 0, 0]) + objs))
        assert [o.split(" ")[0] for o in d["fields"]["Objects"]] == [f"g32v{var}", "g1v2"], (var, d["fields"]["Objects"])
    read = bytes([0xC0, 0xC1, 0x01]) + bytes([30, 1, 0x00, 0, 9]) + bytes([1, 1, 0x00, 0, 7]) + bytes([60, 2, 0x07, 5]) + bytes([30, 2, 0x17, 2, 4, 8]) + bytes([60, 3, 0x06])
    d = dnp3.decode_frame(dnp3.build_frame(0xC4, 1, 1024, read))
    assert [o.split(" ")[0] for o in d["fields"]["Objects"]] == ["g30v1", "g1v1", "g60v2", "g30v2", "g60v3"], d["fields"]["Objects"]      # a read carries no object data


# ---------------------------------------------------------------- 11: small decoding slips
def test_bad_crc_wrapped_dnp3_frame_counts_one_error():
    f = bytearray(dnp3.build_frame(0xC4, 1, 1024, bytes([0xC0, 0xC1, 1, 60, 2, 6])))
    f[12] ^= 0xFF                                                                             # data-block CRC fails; the header is fine
    eth = wrap.Wrapper().dnp3(bytes(f), True)
    r = analyze.decode_packet(eth, pcapng.LINKTYPE_ETHERNET, "channel a; DNP3; crc=FAIL; first byte 1 ns")
    assert r["proto"] == "DNP3" and not r["app"]["crc_ok"] and r["errors"] == 1
    d = tmp()
    w = pcapng.PcapngWriter(d + "/s.pcapng", [(1, "e", "")])
    w.write(0, 1_000_000_000, eth, "ch; crc=FAIL")
    w.close()
    a = analyze.summarize_files([d + "/s.pcapng"])
    assert a["errors"] == 1 and a["crc_failures"] == 1
    from traffic_tester.sniff import modbus
    mb = wrap.Wrapper().modbus(modbus.add_crc(bytes([1, 3, 0, 0, 0, 1])), True)               # Modbus/TCP has no CRC of its own: the comment still counts once
    assert analyze.decode_packet(mb, pcapng.LINKTYPE_ETHERNET, "ch; crc=FAIL")["errors"] == 1


def test_ipv6_tcp_is_not_shown_as_an_ipv4_layer():
    a = bytes(6)
    src, dst = bytes([0x20, 0x01, 0x0d, 0xb8] + [0] * 11 + [1]), bytes([0x20, 0x01, 0x0d, 0xb8] + [0] * 11 + [2])
    tcp = struct.pack(">HHIIBBHHH", 40000, 502, 1, 0, 0x50, 0x02, 8192, 0, 0)
    v6 = a + a + b"\x86\xdd" + struct.pack(">IHBB", 0x60000000, len(tcp), 6, 57) + src + dst + tcp
    ls = analyze.layers(v6, pcapng.LINKTYPE_ETHERNET)
    names = [x["name"] for x in ls]
    assert "IPv4" not in names and "Ports" not in names and names[:2] == ["Ethernet", "IPv6"]
    f = ls[1]["fields"]
    assert f["Source"] == "2001:db8:0:0:0:0:0:1" and f["Destination"] == "2001:db8:0:0:0:0:0:2" and f["Hop limit"] == 57 and "TTL" not in f
    v4 = wrap.Wrapper().dnp3(dnp3.build_frame(0xC4, 1, 1024, bytes([0xC0, 0xC1, 1, 60, 2, 6])), True)      # IPv4 is unchanged
    assert [x["name"] for x in analyze.layers(v4, pcapng.LINKTYPE_ETHERNET)] == ["Ethernet", "IPv4", "Ports", "DNP3"]


def test_short_wifi_control_frames_are_named():
    ra, ta = b"\x02\x00\x00\x00\x00\x01", b"\x02\x00\x00\x00\x00\x02"
    ack = analyze.decode_radiotap(_radiotap(bytes([0xD4, 0, 0, 0]) + ra))
    assert ack["info"] == "ACK" and ack["dst"] == "02:00:00:00:00:01" and ack["src"] == ""
    cts = analyze.decode_radiotap(_radiotap(bytes([0xC4, 0, 0, 0]) + ra + bytes(4)))          # with the 4 FCS bytes: they are not an address
    assert cts["info"] == "CTS" and cts["src"] == ""
    rts = analyze.decode_radiotap(_radiotap(bytes([0xB4, 0, 0, 0]) + ra + ta))
    assert rts["info"] == "RTS" and rts["src"] == "02:00:00:00:00:02" and rts["dst"] == "02:00:00:00:00:01"
    assert analyze.decode_radiotap(_radiotap(bytes([0x08, 0, 0, 0]) + ra + ta))["info"] == "radiotap only / short frame"      # a data frame that short is still "short"


# ---------------------------------------------------------------- 12: dumpcap ring without a file count
def test_dumpcap_ring_size_is_not_replaced_by_the_disk_limit(monkeypatch):
    _fake_tools(monkeypatch)
    cmd = capture.Capture(["eth0"], tmp(), "", {"ring_mb": 50, "ring_minutes": 0, "ring_files": 0}, 0, 0, "capture", max_kb=1_000_000).command()
    pairs = list(zip(cmd, cmd[1:]))
    assert ("-b", "filesize:51200") in pairs and not any(v.startswith("filesize:") for o, v in pairs if o == "-a")      # "-a filesize" + "-b" only switches files
    assert ("-a", "files:19") in pairs                                                        # 19 x 50 MB fit below the limit, then dumpcap stops
    one = capture.Capture(["eth0"], tmp(), "", {}, 0, 0, "capture", max_kb=17000).command()   # a single growing file still stops on its size
    assert one[one.index("-a") + 1] == "filesize:17000"
    ring = capture.Capture(["eth0"], tmp(), "", {"ring_mb": 50, "ring_files": 7}, 0, 0, "capture", max_kb=1_000_000).command()
    assert "-a" not in ring and ring[-2:] == ["-b", "files:7"]                                # a real ring is bounded already: never stop it
