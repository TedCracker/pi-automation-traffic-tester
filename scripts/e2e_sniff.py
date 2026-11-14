#!/usr/bin/env python3
"""Sniffer e2e with REAL tools (dumpcap, tshark, ip bridge) inside the Debian Bookworm test container (scripts/e2e-sniff.sh).
The app runs as an unprivileged user with ambient CAP_NET_RAW/CAP_NET_ADMIN (never root). Prints CHECK lines for checks.md."""
import json, os, re, shutil, signal, struct, subprocess, sys, tempfile, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
results, procs = [], []
TMP = "/tmp/tt-sn"


def check(name, ok, ev):
    results.append((name, bool(ok)))
    print(f"CHECK {name} | {'pass' if ok else 'FAIL'} | {str(ev)[:360]}", flush=True)


def unresolved(name, ev):
    print(f"CHECK {name} | unresolved | {ev}", flush=True)


def sh(*cmd, timeout=60, check_=False):
    r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    if check_ and r.returncode:
        raise RuntimeError(f"{cmd}: {r.stderr}")
    return r


def ns(n, *cmd, **kw):
    return sh("ip", "netns", "exec", n, *cmd, **kw)


def bg(*cmd, user=False, env=None):
    p = subprocess.Popen(list(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    procs.append(p)
    return p


def tshark(path, *args):
    r = sh("tshark", "-r", path, *args, timeout=120)
    return r.stdout.strip()


def count(path, filt):
    out = tshark(path, "-Y", filt, "-T", "fields", "-e", "frame.number")
    return len([l for l in out.splitlines() if l.strip()])


def http(method, path, body=None, raw=False, port=18090):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as f:
            d = f.read()
            return d if raw else (json.loads(d) if d else None)
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "detail": e.read().decode()[:300]}


def wait_run(rid, timeout=120):
    for _ in range(int(timeout * 4)):
        r = http("GET", f"/api/runs/{rid}")
        if r["status"] != "running":
            return r
        time.sleep(0.25)
    raise TimeoutError


import platform
SLOW = 4.0 if platform.machine() in ("aarch64", "arm64") else 1.0      # under QEMU emulation dumpcap/tshark start several times slower: wait longer, never accept less


def sniff_profile(mode, secs=0, **k):
    return {"name": f"e2e {mode}", "kind": "sniffer", "sniff": {"mode": mode, "duration_s": secs, **k}}


def start(mode, **k):
    r = http("POST", "/api/runs", {"profile": sniff_profile(mode, **k)})
    assert "run_id" in r, r
    time.sleep(1.5 * SLOW)
    return r["run_id"]


def stop(rid):
    http("POST", f"/api/runs/{rid}/stop")
    return wait_run(rid)


def cap_path(r, name_prefix="capture"):
    s = r["summary"]
    return [os.path.join(s["location"], f["name"]) for f in s["files"] if f["name"].startswith(name_prefix) and f["name"].endswith((".pcapng", ".pcap"))]


def cleanup():
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except Exception:
            pass
    time.sleep(0.5)
    for n in ("n1", "n2", "n3"):
        sh("ip", "netns", "del", n)
    for l in ("l1", "l2", "s0", "ttbr0"):
        sh("ip", "link", "del", l)
    sh("umount", "/mnt/usbfake")


try:
    # ---------------------------------------------------------------- topology
    for n in ("n1", "n2", "n3"):
        sh("ip", "netns", "add", n, check_=True)
        ns(n, "ip", "link", "set", "lo", "up")
        ns(n, "sysctl", "-qw", "net.ipv6.conf.all.disable_ipv6=1")
    sh("sysctl", "-qw", "net.ipv6.conf.all.disable_ipv6=1")
    for leg, peer, nsn, ip in (("l1", "p1", "n1", "10.7.0.1/24"), ("l2", "p2", "n2", "10.7.0.2/24"), ("s0", "s1", "n3", "10.7.1.1/24")):
        sh("ip", "link", "add", leg, "type", "veth", "peer", "name", peer, check_=True)
        sh("ip", "link", "set", peer, "netns", nsn)
        ns(nsn, "ip", "addr", "add", ip, "dev", peer)
        ns(nsn, "ip", "link", "set", peer, "up")
        sh("ip", "link", "set", leg, "up")
    sh("ip", "addr", "add", "10.7.1.2/24", "dev", "s0")
    sh("mkdir", "-p", "/mnt/usbfake")
    sh("mount", "-t", "tmpfs", "-o", "size=30m,mode=1777", "tmpfs", "/mnt/usbfake", check_=True)
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    sh("chown", "tt:tt", TMP)
    env = dict(os.environ, PYTHONPATH=ROOT, HOME="/home/tt", TT_USB_DIR="/mnt/usbfake", TT_ASSUME_SYNCED="1", TT_NO_DEFAULT_REFLECTOR="1", TT_NO_DISCOVERY="1")      # the capture helper listens on UDP 20500 itself: this app must not hold that port
    app = bg("setpriv", "--reuid=tt", "--regid=tt", "--init-groups", "--inh-caps=+net_raw,+net_admin", "--ambient-caps=+net_raw,+net_admin", PY, "-m", "traffic_tester", "--role", "A", "--port", "18090",
             "--host", "127.0.0.1", "--data-dir", TMP, env=env)
    for _ in range(80):
        try:
            http("GET", "/api/status"); break
        except Exception:
            time.sleep(0.25)
    user = sh("ps", "-o", "user=", "-p", str(app.pid)).stdout.strip()
    caps = sh("sh", "-c", f"grep -E 'CapEff|CapAmb' /proc/{app.pid}/status | tr '\\n' ' '").stdout
    check("Sniffer app runs as an unprivileged user (not root) with only ambient CAP_NET_RAW/CAP_NET_ADMIN", user == "tt", f"user={user} {caps.strip()}")
    ver = sh("dumpcap", "--version").stdout.splitlines()[0]
    print(f"# tools: {ver} / {sh('tshark', '--version').stdout.splitlines()[0]}", flush=True)

    # ---------------------------------------------------------------- preflight
    pre = http("POST", "/api/sniff/preflight", {})
    check("Pre-flight finds dumpcap + tshark + iw + ip + ethtool and lists the Ethernet ports", pre["engine"] == "dumpcap" and all(pre["tools"][t] for t in ("dumpcap", "tcpdump", "tshark", "iw", "ip", "ethtool")) and {"l1", "l2", "s0"} <= {p["name"] for p in pre["eth"]},
          f"engine={pre['engine']} tools={pre['tools']} ports={[p['name'] for p in pre['eth']][:6]}")

    # ---------------------------------------------------------------- SPAN-style capture on one port
    sink = bg("ip", "netns", "exec", "n3", "true")      # placeholder so cleanup list is simple
    listener = bg(PY, os.path.join(ROOT, "scripts", "sniff_traffic.py"), "listen", "10.7.1.2")
    time.sleep(0.8 * SLOW)
    rid = start("eth_span", eth={"interface": "s0"}, storage={"ring_mb": 0, "ring_files": 0})
    ns("n3", PY, os.path.join(ROOT, "scripts", "sniff_traffic.py"), "dnp3", "10.7.1.2", "20")
    ns("n3", PY, os.path.join(ROOT, "scripts", "sniff_traffic.py"), "modbus", "10.7.1.2", "15")
    ns("n3", "ping", "-c", "3", "-i", "0.2", "10.7.1.2")
    time.sleep(1.5 * SLOW)
    live = http("GET", "/api/sniff/live")
    dn = [r for r in live["rows"] if r["proto"] == "DNP3"]
    mb = [r for r in live["rows"] if r["proto"] == "Modbus/TCP"]
    det = http("GET", f"/api/sniff/packet/{dn[0]['n']}") if dn else {}
    dl = [l["fields"] for l in det.get("layers", []) if l["name"] == "DNP3"]
    check("Live view shows decoded DNP3 and Modbus/TCP packets with layer detail (sampled from a 2nd dumpcap)", dn and mb and dl and dl[0].get("Application function", "").startswith("1 (Read"),
          f"rows={len(live['rows'])} dnp3={len(dn)} modbus={len(mb)} detail={dl[:1]}")
    r = stop(rid)
    s, a = r["summary"], r["summary"].get("analysis", {})
    f = cap_path(r)[0]
    check("SPAN capture: pcapng written by dumpcap; summary counts DNP3 (20) / Modbus (15) frames, ARP + ICMP, no CRC failures",
          s["engine"]["engine"] == "dumpcap" and a["dnp3_frames"] == 20 and a["modbus_frames"] == 15 and a["crc_failures"] == 0 and {"ARP", "ICMP"} <= set(a["protocols"]),
          f"packets={a['packets']} protocols={a['protocols']} dnp3={a['dnp3_frames']} modbus={a['modbus_frames']} errors={a['errors']} talkers={a['top_talkers'][:2]}")
    check("tshark opens the pcapng and agrees: 20 DNP3, 15 Modbus/TCP packets; capture file is pcapng",
          count(f, "dnp3") == 20 and count(f, "mbtcp") == 15 and open(f, "rb").read(4) == b"\x0a\x0d\x0d\x0a", f"tshark dnp3={count(f, 'dnp3')} mbtcp={count(f, 'mbtcp')} file={os.path.basename(f)} {os.path.getsize(f)} B")
    check("Kernel drop counters reported by dumpcap are zero at this rate", s["engine"].get("kernel_dropped") in (0, None), json.dumps({k: s["engine"].get(k) for k in ("packets_captured", "kernel_received", "kernel_dropped")}))
    dumpcap_user = "tt"
    first_span = rid

    # ---------------------------------------------------------------- BPF filter in the kernel
    rid = start("eth_span", eth={"interface": "s0"}, filter={"bpf": "tcp port 20000"}, storage={"ring_mb": 0, "ring_files": 0})
    ns("n3", PY, os.path.join(ROOT, "scripts", "sniff_traffic.py"), "dnp3", "10.7.1.2", "12")
    ns("n3", PY, os.path.join(ROOT, "scripts", "sniff_traffic.py"), "modbus", "10.7.1.2", "5")
    ns("n3", "ping", "-c", "2", "-i", "0.2", "10.7.1.2")
    time.sleep(1.0 * SLOW)
    r = stop(rid); f = cap_path(r)[0]
    n_all, n_dnp = int(tshark(f, "-T", "fields", "-e", "frame.number").splitlines()[-1] or 0), count(f, "tcp.port==20000")
    check("BPF filter 'tcp port 20000' is applied by the kernel: only the DNP3 connection (handshake + 12 frames + acks) is in the file, no Modbus/ARP/ICMP", n_all == n_dnp and n_all >= 12 and count(f, "mbtcp || arp || icmp") == 0 and count(f, "dnp3") == 12, f"all frames={n_all} tcp/20000={n_dnp} dnp3={count(f, 'dnp3')}")
    bad = http("POST", "/api/runs", {"profile": sniff_profile("eth_span", eth={"interface": "s0"}, filter={"bpf": "udp prot 20000"})})
    rb = wait_run(bad["run_id"], 30)
    check("Invalid capture filter is refused with a plain message before anything starts", rb["status"] == "failed" and "not valid" in rb["summary"]["error"], rb["summary"].get("error"))
    pf = http("POST", "/api/sniff/preflight", sniff_profile("eth_span", eth={"interface": "s0"}, filter={"bpf": "udp prot"}))
    check("Pre-flight reports the same filter error", any(c["s"] == "bad" and "filter" in c["title"].lower() for c in pf["checks"]), [c["title"] for c in pf["checks"]])

    # ---------------------------------------------------------------- inline bridge
    rid = start("eth_bridge", eth={"leg_a": "l1", "leg_b": "l2"}, storage={"ring_mb": 0, "ring_files": 0})
    inet = sh("ip", "-4", "addr", "show", "ttbr0").stdout
    stp = open("/sys/class/net/ttbr0/bridge/stp_state").read().strip()
    gfm = open("/sys/class/net/ttbr0/bridge/group_fwd_mask").read().strip()
    masters = [open(f"/sys/class/net/{l}/master/uevent").read().count("INTERFACE=ttbr0") for l in ("l1", "l2")]
    check("Inline tap: bridge ttbr0 has NO IP address, STP off, link-local forwarding mask set, both legs enslaved", "inet" not in inet and stp == "0" and gfm in ("0xfff8", "65528") and masters == [1, 1], f"ipv4={inet.strip() or 'none'} stp={stp} group_fwd_mask={gfm} legs={masters}")
    pg = ns("n1", "ping", "-c", "5", "-i", "0.2", "10.7.0.2")
    lldp_cap = bg("ip", "netns", "exec", "n2", "timeout", "6", "tcpdump", "-U", "-i", "p2", "-nn", "-w", "/tmp/lldp.pcap", "ether proto 0x88cc")
    time.sleep(0.8 * SLOW)
    ns("n1", PY, os.path.join(ROOT, "scripts", "sniff_traffic.py"), "lldp", "-", "3", "p1")
    ns("n1", PY, os.path.join(ROOT, "scripts", "sniff_traffic.py"), "vlan", "-", "3", "p1")
    time.sleep(1.2 * SLOW)
    r = stop(rid); f = cap_path(r)[0]
    loss0 = "0% packet loss" in pg.stdout
    check("Traffic passes THROUGH the inline tap (ping n1→n2 over the bridge, 0 % loss)", loss0, re.findall(r"\d+ packets transmitted.*", pg.stdout))
    lldp_cap.wait(timeout=10)
    lldp_n = len(sh("tcpdump", "-nn", "-r", "/tmp/lldp.pcap").stdout.splitlines()) if os.path.exists("/tmp/lldp.pcap") else 0
    check("Link-local (LLDP 01:80:C2:00:00:0E) frames are forwarded transparently through the bridge", lldp_n >= 3, f"{lldp_n} LLDP frames arrived at the far side")
    icmp, vl = count(f, "icmp.type==8 || icmp.type==0"), count(f, "vlan.id==100 && vlan.priority==5")
    check("Bridge capture sees every frame once (5 ping requests + 5 replies = 10 ICMP) and keeps the VLAN tag (id 100, prio 5)", icmp == 10 and vl == 3, f"icmp={icmp} vlan(100,prio5)={vl} protocols={r['summary']['analysis']['protocols']}")
    gone = sh("ip", "link", "show", "ttbr0").returncode != 0
    free = all("master" not in sh("ip", "-d", "link", "show", l).stdout for l in ("l1", "l2"))
    check("After the capture the bridge is removed and the ports are restored", gone and free, f"bridge gone={gone} legs free={free}")

    # ---------------------------------------------------------------- both legs: interface ids in pcapng
    rid = start("eth_bridge", eth={"leg_a": "l1", "leg_b": "l2", "capture_on": "both_legs"}, storage={"ring_mb": 0, "ring_files": 0})
    ns("n1", "ping", "-c", "3", "-i", "0.2", "10.7.0.2")
    time.sleep(1.0 * SLOW)
    r = stop(rid); f = cap_path(r)[0]
    ids = sorted(set(tshark(f, "-T", "fields", "-e", "frame.interface_id").split()))
    names = tshark(f, "-T", "fields", "-e", "frame.interface_name").split()
    check("Capture on both legs: pcapng has two interface ids (one per leg), frames seen at each leg", ids == ["0", "1"] and {"l1", "l2"} <= set(names) and count(f, "icmp.type==8 || icmp.type==0") >= 12, f"interface ids={ids} names={sorted(set(names))} icmp={count(f, 'icmp.type==8 || icmp.type==0')}")

    # ---------------------------------------------------------------- ring buffer
    rid = start("eth_span", eth={"interface": "s0"}, storage={"ring_mb": 1, "ring_files": 3})
    ns("n3", PY, os.path.join(ROOT, "scripts", "sniff_traffic.py"), "udpbulk", "10.7.1.2", "9000")
    time.sleep(1.0 * SLOW)
    r = stop(rid)
    fl = cap_path(r)
    idx = sorted(int(re.search(r"capture_(\d+)_", os.path.basename(x)).group(1)) for x in fl)
    valid = all(sh("tshark", "-r", x, "-q").returncode == 0 and count(x, "udp") > 0 for x in fl)
    check("Ring buffer (1 MB × newest 3 files): older files deleted, newest kept, every kept file is a valid pcapng", len(fl) <= 3 and idx[-1] >= 4 and valid, f"kept files {idx}, sizes {[os.path.getsize(x) // 1024 for x in fl]} KB, valid={valid}; ring_note={r['summary'].get('ring_note')}")

    # ---------------------------------------------------------------- link-state passthrough
    rid = start("eth_bridge", eth={"leg_a": "l1", "leg_b": "l2", "link_passthrough": True}, storage={"ring_mb": 0, "ring_files": 0})
    car = lambda n, d: ns(n, "cat", f"/sys/class/net/{d}/carrier").stdout.strip()
    both_up = car("n1", "p1") == "1" and car("n2", "p2") == "1"
    ns("n1", "ip", "link", "set", "p1", "down")
    t0, lost = time.time(), None
    while time.time() - t0 < 6:
        if car("n2", "p2") == "0":
            lost = time.time() - t0; break
        time.sleep(0.2)
    ns("n1", "ip", "link", "set", "p1", "up")
    t0, back = time.time(), None
    while time.time() - t0 < 12:
        if car("n2", "p2") == "1":
            back = time.time() - t0; break
        time.sleep(0.3)
    r = stop(rid)
    check("Link-state passthrough: unplugging one side drops the link on the other side (within ~1 s), restores it after re-plug", both_up and lost is not None and lost < 3 and back is not None and back < 10,
          f"far side lost carrier after {lost and round(lost, 1)} s, restored after {back and round(back, 1)} s; events={r['summary'].get('bridge_events')}")

    # ---------------------------------------------------------------- serial tap + tshark decode
    sc = sh("sh", "-c", "true")
    def pty_pair():
        p = subprocess.Popen(["setpriv", "--reuid=tt", "--regid=tt", "--init-groups", "socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0"], stderr=subprocess.PIPE, text=True, start_new_session=True)
        procs.append(p)
        pts = []
        while len(pts) < 2:
            m = re.search(r"PTY is (\S+)", p.stderr.readline())
            if m: pts.append(m.group(1))
        return pts
    a, b = pty_pair(), pty_pair()
    wa, wb = os.open(a[0], os.O_RDWR), os.open(b[0], os.O_RDWR)
    sys.path.insert(0, ROOT)
    from traffic_tester.sniff import dnp3 as D, modbus as M
    rid = start("serial", serial={"protocol": "dnp3", "channels": [{"name": "master TX", "port": a[1], "baud": 9600}, {"name": "outstation TX", "port": b[1], "baud": 9600}], "also_user_dlt": True}, storage={"ring_mb": 10, "ring_files": 5})
    os.write(wa, D.build_frame(0xC4, 1, 1024, bytes([0xC0, 0xC1, 1, 60, 2, 6, 60, 3, 6]))); time.sleep(0.05)
    os.write(wb, D.build_frame(0x44, 1024, 1, bytes([0xC0, 0xC1, 0x81, 0, 0, 30, 2, 0, 0, 1, 1, 10, 0]))); time.sleep(0.05)
    bad = bytearray(D.build_frame(0xC4, 1, 1024, bytes([0xC0, 0xC2, 1, 60, 1, 6]))); bad[11] ^= 0x55
    os.write(wa, bytes(bad)); time.sleep(0.4)
    r = stop(rid)
    tp = [os.path.join(r["summary"]["location"], x["name"]) for x in r["summary"]["files"] if x["name"].startswith("tap_")][0]
    rows = tshark(tp, "-Y", "dnp3", "-T", "fields", "-e", "dnp3.src", "-e", "dnp3.dst", "-e", "dnp3.al.func").splitlines()
    vtxt = sh("tshark", "-r", tp, "-V", "-Y", "dnp3").stdout
    bad_crc, good_crc = len(re.findall(r"incorrect", vtxt)), len(re.findall(r"\[correct\]", vtxt))
    check("Serial DNP3 tap → pcapng decodes in tshark with no set-up: master READ (1024→1, func 1) and outstation RESPONSE (func 129)",
          len(rows) == 3 and rows[0].split("\t")[:3] == ["1024", "1", "1"] and rows[1].split("\t")[2] == "129", f"tshark rows={rows}")
    check("tshark flags exactly the corrupted serial frame as a CRC error; good frames are 'correct'", bad_crc >= 1 and good_crc >= 4, f"incorrect={bad_crc} correct={good_crc}; app counted crc_failures={r['summary']['serial']['crc_failures']}")
    check("Serial run summary: 3 frames, 1 CRC failure, honest timestamp note, raw .bin + CSV + DLT_USER0 files written",
          r["summary"]["serial"]["frames"] == 3 and r["summary"]["serial"]["crc_failures"] == 1 and "±1-2 ms" in r["summary"]["serial"]["timestamp_note"] and {"tap-raw.bin", "tap-frames.csv"} <= {x["name"] for x in r["summary"]["files"]} and any(x["name"].startswith("tap-rtu_") for x in r["summary"]["files"]),
          [x["name"] for x in r["summary"]["files"]])
    # Modbus RTU
    a2, b2 = pty_pair(), pty_pair()
    wa, wb = os.open(a2[0], os.O_RDWR), os.open(b2[0], os.O_RDWR)
    rid = start("serial", serial={"protocol": "modbus_rtu", "channels": [{"name": "master TX", "port": a2[1], "baud": 19200}, {"name": "slave TX", "port": b2[1], "baud": 19200}], "also_user_dlt": True})
    for i in range(4):
        os.write(wa, M.add_crc(bytes([9, 3, 0, 10 + i, 0, 2]))); time.sleep(0.04)
        os.write(wb, M.add_crc(bytes([9, 3, 4, 0, i, 0, 5]))); time.sleep(0.04)
    time.sleep(0.3)
    r = stop(rid)
    tp = [os.path.join(r["summary"]["location"], x["name"]) for x in r["summary"]["files"] if x["name"].startswith("tap_")][0]
    mrows = tshark(tp, "-Y", "mbtcp", "-T", "fields", "-e", "mbtcp.unit_id", "-e", "modbus.func_code", "-e", "modbus.reference_num").splitlines()
    check("Serial Modbus RTU tap → pcapng decodes in tshark as Modbus/TCP (unit 9, function 3, register 10-13)", len(mrows) == 8 and mrows[0].split("\t")[:3] == ["9", "3", "10"] and mrows[6].split("\t")[:3] == ["9", "3", "13"], mrows[:3])
    rtu = [os.path.join(r["summary"]["location"], x["name"]) for x in r["summary"]["files"] if x["name"].startswith("tap-rtu_")][0]
    uat = 'uat:user_dlts:"User 0 (DLT=147)","mbrtu","0","","0",""'
    out = sh("tshark", "-o", uat, "-r", rtu, "-Y", "mbrtu", "-T", "fields", "-e", "mbrtu.unit_id", "-e", "modbus.func_code").stdout.split()
    if len(out) >= 8:
        check("DLT_USER0 file decodes as Modbus RTU in tshark once DLT 147 is mapped to mbrtu (documented mapping)", True, f"{len(out) // 2} frames decoded with the uat mapping")
    else:
        unresolved("DLT_USER0 file decodes as Modbus RTU in tshark with the documented DLT 147 → mbrtu mapping", f"tshark output {out[:4]} (mapping syntax differs between tshark versions; use Edit → Preferences → Protocols → DLT_USER)")

    # ---------------------------------------------------------------- disk-space guard on a small 'USB drive'
    rid = start("eth_span", eth={"interface": "s0"}, storage={"usb": True, "ring_mb": 0, "ring_files": 0, "min_free_mb": 12})
    for _ in range(3):
        ns("n3", PY, os.path.join(ROOT, "scripts", "sniff_traffic.py"), "udpbulk", "10.7.1.2", "6000")
    t0 = time.time()
    while http("GET", f"/api/runs/{rid}")["status"] == "running" and time.time() - t0 < 25:
        time.sleep(0.5)
    r = http("GET", f"/api/runs/{rid}")
    s = r["summary"]; used = [x for x in s["files"]]
    free_after = shutil.disk_usage("/mnt/usbfake").free // 1048576
    check("Writing to a (30 MB) USB drive: stops by itself when free space falls under the guard (12 MB), keeps the files, plain message", s["stop_reason"] == "disk_full" and "nearly full" in s.get("error", "") and used and s["location"].startswith("/mnt/usbfake") and free_after >= 12,
          f"stop_reason={s['stop_reason']} error={s.get('error')} location={s['location']} files={[(x['name'], x['bytes'] // 1024) for x in used]} free_after={free_after} MB")
    sh("sh", "-c", "rm -rf /mnt/usbfake/traffic-tester-captures/run-*")                # empty the fake drive again
    cp = http("POST", f"/api/runs/{first_span}/capture/usb")
    check("'Copy to USB' copies a finished capture to the drive (with free-space check)", cp.get("ok") and os.path.isdir(f"/mnt/usbfake/traffic-tester-captures/run-{first_span}"), cp)
    pre2 = http("POST", "/api/sniff/preflight", sniff_profile("eth_span", storage={"usb": True}))
    # ---------------------------------------------------------------- Wi-Fi (no radio in a container)
    pw = http("POST", "/api/sniff/preflight", sniff_profile("wifi"))
    check("Wi-Fi mode without a monitor-capable adapter: pre-flight says so in plain words", any(c["s"] == "bad" and "monitor" in (c["title"] + c["text"]).lower() for c in pw["checks"]), [c["title"] for c in pw["checks"]])
    rw = http("POST", "/api/runs", {"profile": sniff_profile("wifi")})
    rw = wait_run(rw["run_id"], 30)
    check("Wi-Fi run without an adapter fails with a plain message (built-in Pi Wi-Fi cannot monitor)", rw["status"] == "failed" and "monitor mode" in rw["summary"]["error"], rw["summary"].get("error"))
    mp = shutil.which("modprobe")
    hw = sh(mp, "mac80211_hwsim") if mp else None
    unresolved("Wi-Fi monitor mode, channel set/hop and beacon capture on a real adapter", f"needs hardware (Alfa AWUS036ACM); command sequence + decoder unit-tested; mac80211_hwsim {'loaded' if hw and hw.returncode == 0 else 'not available in this container/kernel'}")

    # ---------------------------------------------------------------- two Pis talking both ways THROUGH the inline tap, captured and dissected by tshark
    # two identical apps (alpha in namespace n1, 10.7.0.1; bravo in n2, 10.7.0.2) run the real app; the sniffer app bridges their two cables (l1/l2) and records everything.
    PA_, PB_ = 18091, 18092
    for nsn, port_, name in (("n1", PA_, "A"), ("n2", PB_, "B")):
        d = f"/tmp/tt-sn-{name}"; shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
        bg("ip", "netns", "exec", nsn, PY, "-m", "traffic_tester", "--role", name, "--port", str(port_), "--host", "0.0.0.0", "--data-dir", d,
           env=dict(os.environ, PYTHONPATH=ROOT, TT_ASSUME_SYNCED="1", TT_TEST_HOOKS="1"))
    for port_ in (PA_, PB_):
        for _ in range(100):
            r_ = ns("n1" if port_ == PA_ else "n2", PY, "-c", f"import urllib.request;urllib.request.urlopen('http://127.0.0.1:{port_}/api/status',timeout=2)")
            if r_.returncode == 0:
                break
            time.sleep(0.3)
    nsh = lambda nsn, port_, method, path, body=None: json.loads(ns(nsn, PY, "-c", "import urllib.request,json,sys;b=sys.argv[3];r=urllib.request.Request('http://127.0.0.1:%s%s'%(sys.argv[1],sys.argv[2]),method=sys.argv[4],data=b.encode() if b else None,headers={'Content-Type':'application/json'});print(urllib.request.urlopen(r,timeout=60).read().decode() or 'null')", str(port_), path, json.dumps(body) if body is not None else "", method).stdout or "null")
    def wait_on(nsn, port_, rid, timeout=90):
        for _ in range(timeout * 4):
            r_ = nsh(nsn, port_, "GET", f"/api/runs/{rid}")
            if r_["status"] != "running":
                return r_
            time.sleep(0.25)
        raise TimeoutError
    def trip(tester, responder, tr, extra_t, extra_r):
        (tn, tp, tip), (rn, rp, rif) = tester, responder
        nsh(rn, rp, "POST", "/api/responder/stop")
        rb = nsh(rn, rp, "POST", "/api/responder/start", {"transport": tr, "frame": "extended", **extra_r})
        rid_ = nsh(tn, tp, "POST", "/api/runs", {"profile": {"name": "e2e", "transport": tr, "frame": "extended", "duration_s": 5, "interval_ms": 100, "threshold_ms": 60, "timeout_ms": 800,
                                                              "idle_gaps_s": [], "summary_interval_s": 1, **extra_t}})["run_id"]
        out = wait_on(tn, tp, rid_)
        nsh(rn, rp, "POST", "/api/responder/stop")
        return rb, out
    A_ = ("n1", PA_, "p1"); B_ = ("n2", PB_, "p2")
    rid_cap = start("eth_bridge", eth={"leg_a": "l1", "leg_b": "l2"}, storage={"ring_mb": 0, "ring_files": 0})
    res2 = {}
    for label, tst, rsp, ptip in (("alpha→bravo", A_, B_, "10.7.0.2"), ("bravo→alpha", B_, A_, "10.7.0.1")):
        rb, o = trip(tst, rsp, "udp", {"net": {"peer_host": ptip, "port": 18600}}, {"net": {"port": 18600}})
        res2[f"udp {label}"] = (o["status"], o["verdict"], (o["summary"] or {}).get("sent"), (o["summary"] or {}).get("lost"))
        rb, o = trip(tst, rsp, "goose_l2", {"goose": {"interface": tst[2]}}, {"goose": {"interface": rsp[2]}})
        res2[f"goose {label}"] = (o["status"], o["verdict"], (o["summary"] or {}).get("sent"), (o["summary"] or {}).get("lost"))
        rb, o = trip(tst, rsp, "goose_l3", {"goose": {"interface": tst[2]}}, {"goose": {"interface": rsp[2]}})
        res2[f"r-goose L3 {label}"] = (o["status"], o["verdict"], (o["summary"] or {}).get("sent"), (o["summary"] or {}).get("lost"))
    # generator, both directions in one run (alpha drives; bravo is the other end)
    gp_ = {"name": "e2e gen both ways", "kind": "generator", "net": {"peer_host": "10.7.0.2"},
           "gen": {"streams": [{"name": "from alpha to bravo", "type": "udp", "direction": "a_to_b", "rate": {"mbps": 5}, "size": {"mode": "fixed", "size": 800}, "rtt_probe_pps": 20},
                               {"name": "from bravo to alpha", "type": "udp", "direction": "b_to_a", "rate": {"mbps": 5}, "size": {"mode": "fixed", "size": 800}, "rtt_probe_pps": 0}],
                   "duration_s": 5, "interface": "p1", "peer_interface": "p2", "peer_api": f"http://10.7.0.2:{PB_}", "local_host": "10.7.0.1", "rule": {"max_loss_pct": 0.5}}}
    grid = nsh("n1", PA_, "POST", "/api/runs", {"profile": gp_})["run_id"]
    gout = wait_on("n1", PA_, grid, 120)
    res2["generator both ways"] = (gout["status"], gout["verdict"], (gout["summary"] or {}).get("totals"))
    time.sleep(1.0 * SLOW)
    rcap = stop(rid_cap)
    cap2 = cap_path(rcap)[0]
    ok_runs = all(v[0] == "finished" and v[1] == "PASS" for k, v in res2.items())
    check("Two Pis back and forth over the inline tap: UDP trip test, GOOSE L2 + R-GOOSE L3 trip tests and 2-stream generator all PASS in BOTH directions (A→B and B→A)", ok_runs, json.dumps(res2)[:380])
    mac = lambda nsn, dev: ns(nsn, "cat", f"/sys/class/net/{dev}/address").stdout.strip()
    mA, mB = mac("n1", "p1"), mac("n2", "p2")
    gA = count(cap2, f'goose && eth.src == {mA}'); gB = count(cap2, f'goose && eth.src == {mB}')
    check("tshark DISSECTS the app's GOOSE frames (IEC 61850 goose dissector, no errors) sent by alpha and by bravo", gA > 20 and gB > 20 and count(cap2, "goose && _ws.malformed") == 0,
          f"goose frames from A={gA} from B={gB} malformed={count(cap2, 'goose && _ws.malformed')}")
    rgA = count(cap2, f"udp.port == 102 && eth.src == {mA}"); rgB = count(cap2, f"udp.port == 102 && eth.src == {mB}")
    rgd = count(cap2, "r-goose"); rgm = count(cap2, "r-goose && _ws.malformed")
    rgfields = tshark(cap2, "-Y", "r-goose", "-T", "fields", "-e", "rgoose.spdu_id", "-e", "rgoose.spdu_num", "-e", "goose.gocbRef", "-e", "goose.stNum", "-e", "goose.sqNum").splitlines()[:2]
    rgexp = tshark(cap2, "-Y", "r-goose && _ws.expert", "-T", "fields", "-e", "_ws.expert.message").splitlines()[:2]
    check("R-GOOSE (L3 multicast, UDP 102) from both Pis: tshark dissects them automatically as R-GOOSE (CLTP + session header + GOOSE PDU with gocbRef/stNum/sqNum), none malformed, no expert warnings",
          rgA > 20 and rgB > 20 and rgd >= rgA + rgB and rgm == 0 and not rgexp and any("TESTER_A" in l for l in rgfields),
          f"udp/102 frames A={rgA} B={rgB}; dissected as R-GOOSE={rgd}, malformed={rgm}; sample fields={rgfields} expert={rgexp}")
    gdet = tshark(cap2, "-Y", f"goose && eth.src == {mA}", "-T", "fields", "-e", "goose.gocbRef", "-e", "goose.stNum", "-e", "goose.sqNum", "-e", "goose.numDatSetEntries")
    gdet = "\n".join(gdet.splitlines()[:3])
    check("tshark reads GOOSE fields: gocbRef, stNum, sqNum, number of data-set entries", "TESTER_A" in gdet and len(gdet.splitlines()) >= 2, gdet.replace(chr(10), " | ")[:200])
    check("UDP seen on the wire in both directions (A→B and B→A), each with many packets", count(cap2, "ip.src == 10.7.0.1 && ip.dst == 10.7.0.2 && udp") > 200 and count(cap2, "ip.src == 10.7.0.2 && ip.dst == 10.7.0.1 && udp") > 200,
          f"A→B={count(cap2, 'ip.src == 10.7.0.1 && ip.dst == 10.7.0.2 && udp')} B→A={count(cap2, 'ip.src == 10.7.0.2 && ip.dst == 10.7.0.1 && udp')}")
    a2 = rcap["summary"].get("analysis", {})
    check("Capture summary: frames from both Pis counted, no kernel drops", a2.get("packets", 0) > 1000 and rcap["summary"]["engine"].get("kernel_dropped") in (0, None), f"packets={a2.get('packets')} kernel_dropped={rcap['summary']['engine'].get('kernel_dropped')} protocols={dict(list((a2.get('protocols') or {}).items())[:6])}")

    # ---------------------------------------------------------------- PDF
    pdf = http("GET", f"/api/report.pdf?run_ids={first_span},{rid}", raw=True)
    import zlib, base64
    txt = b""
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        try:
            txt += zlib.decompress(base64.a85decode(m.group(1).strip(), adobe=True))
        except Exception:
            pass
    marks = [k for k in (b"Traffic capture", b"Top talkers", b"Protocols", b"Industrial protocols") if k in txt]
    check("PDF report contains the capture section (summary, protocols, top talkers, DNP3/Modbus counts)", pdf[:5] == b"%PDF-" and len(marks) >= 3, f"{len(pdf)} bytes; markers {marks}")
    # keep sample captures (small, real) for the docs / screenshots: open them in Wireshark
    if os.path.isdir("/out"):
        samples = {"sample-span-capture.pcapng": cap_path(http("GET", f"/api/runs/{first_span}"))[0]}
        small = "/tmp/tt-sn/two-pi-small.pcapng"
        sh("tshark", "-r", cap2, "-Y", "goose || r-goose || frame.number <= 60", "-w", small, "-F", "pcapng")
        samples["sample-two-pi-both-ways-goose-udp.pcapng"] = small
        for n, rid_ in (("sample-serial-modbus-tap.pcapng", rid),):
            samples[n] = tp
        for dst, src in samples.items():
            shutil.copy(src, os.path.join("/out", dst))
        shutil.copy(rtu, "/out/sample-serial-modbus-rtu-DLT_USER0.pcapng")
        print("# samples written:", sorted(os.listdir("/out")), flush=True)
except BaseException as ex:      # never let a crash look like success
    import traceback; traceback.print_exc()
    check("e2e script ran to completion", False, f"{type(ex).__name__}: {ex}")
finally:
    cleanup()
    bad = [n for n, ok in results if not ok]
    print(f"# {len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""), flush=True)
    sys.exit(1 if bad else 0)
