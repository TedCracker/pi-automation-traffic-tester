#!/usr/bin/env python3
"""Traffic helper for the sniffer e2e (runs inside a network namespace): dnp3 | modbus | udpbulk | vlan | lldp | listen."""
import os, socket, struct, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from traffic_tester.sniff import dnp3, modbus

kind, dst = sys.argv[1], sys.argv[2]
n = int(sys.argv[3]) if len(sys.argv) > 3 else 10
if kind == "dnp3":                                       # DNP3 over TCP port 20000 (what a real master/outstation pair does)
    s = socket.create_connection((dst, 20000), timeout=5)
    for i in range(n):
        s.sendall(dnp3.build_frame(0xC4, 1, 1024, bytes([0xC0, 0xC0 | (i & 15), 1, 60, 2, 6]))); time.sleep(0.02)
    s.close()
elif kind == "modbus":
    s = socket.create_connection((dst, 502), timeout=5)
    for i in range(n):
        s.sendall(struct.pack(">HHHB", i + 1, 0, 6, 1) + bytes([3, 0, i, 0, 2])); time.sleep(0.02)
    s.close()
elif kind == "udpbulk":
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pay = b"\xab" * 1400
    for i in range(n):
        s.sendto(pay, (dst, 20500 + i % 7))
        if i % 200 == 0:
            time.sleep(0.005)
elif kind in ("vlan", "lldp"):
    iface = sys.argv[4]
    r = socket.socket(socket.AF_PACKET, socket.SOCK_RAW); r.bind((iface, 0))
    src = open(f"/sys/class/net/{iface}/address").read().strip()
    smac = bytes(int(x, 16) for x in src.split(":"))
    if kind == "vlan":
        f = bytes.fromhex("ffffffffffff") + smac + struct.pack(">HHH", 0x8100, (5 << 13) | 100, 0x88B5) + b"vlan-test-frame".ljust(46, b"\0")
    else:
        f = bytes.fromhex("0180c200000e") + smac + b"\x88\xcc" + bytes.fromhex("0207040102030405" "04070302030405" "0602" "0078" "0000").ljust(46, b"\0")
    for _ in range(n):
        r.send(f); time.sleep(0.02)
elif kind == "listen":                                   # TCP 502 + UDP 20000 sink for the SPAN test, runs until killed
    import threading
    def _drain(c):
        try:
            while c.recv(4096):
                pass
        except OSError:
            pass
    def tcp(port):
        ls = socket.socket(); ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); ls.bind((dst, port)); ls.listen(5)
        while True:
            c, _ = ls.accept()
            threading.Thread(target=_drain, args=(c,), daemon=True).start()
    for port in (502, 20000):
        threading.Thread(target=tcp, args=(port,), daemon=True).start()
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind((dst, 20500))
    while True:
        u.recv(4096)
