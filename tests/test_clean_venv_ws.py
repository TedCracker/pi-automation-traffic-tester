"""Regression (real Pis, 2026-11-12): a venv built ONLY from requirements.txt must serve /ws/live.

Uvicorn needs a WebSocket library; without `websockets` in requirements.txt the live view was dead on every real Pi
(the dev venv happened to have it, so no test noticed). This builds a clean venv from requirements.txt alone, starts
the real app in it, upgrades /ws/live by hand (no client library, so the test itself cannot hide the gap) and waits
for a live test event."""
import base64, json, os, socket, struct, subprocess, sys, time, urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _http(port, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read() or b"{}")


def _ws_frames(port, path, secs):
    """Raw WebSocket client: yields decoded text frames; first yields the HTTP status line of the upgrade."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(4096)
        if not d:
            break
        buf += d
    head, _, buf = buf.partition(b"\r\n\r\n")
    yield head.split(b"\r\n")[0].decode()
    end = time.time() + secs
    while time.time() < end:
        while len(buf) < 2 or len(buf) < 2 + {126: 2, 127: 8}.get(buf[1] & 0x7F, 0):
            try:
                d = s.recv(65536)
            except socket.timeout:
                d = b""
            if not d:
                if time.time() > end:
                    s.close(); return
                continue
            buf += d
        ln, off = buf[1] & 0x7F, 2
        if ln == 126:
            ln, off = struct.unpack(">H", buf[2:4])[0], 4
        elif ln == 127:
            ln, off = struct.unpack(">Q", buf[2:10])[0], 10
        while len(buf) < off + ln:
            buf += s.recv(65536)
        payload, buf = buf[off:off + ln], buf[off + ln:]
        if buf is not None and (payload[:1] == b"{"):
            yield payload.decode()
    s.close()


@pytest.fixture(scope="module")
def clean_py(tmp_path_factory):
    d = tmp_path_factory.mktemp("cleanvenv")
    r = subprocess.run([sys.executable, "-m", "venv", str(d / "v")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    py = str(d / "v" / "bin" / "python")
    r = subprocess.run([py, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", os.path.join(ROOT, "requirements.txt")], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        pytest.skip("cannot build a clean venv here (no network / pip cache): " + r.stderr[-200:])
    return py


def test_requirements_list_a_websocket_library():
    req = open(os.path.join(ROOT, "requirements.txt")).read().lower()
    assert "websockets" in req or "uvicorn[standard]" in req


def test_live_websocket_upgrades_in_a_clean_venv_and_delivers_events(clean_py, tmp_path):
    p_app, p_data = _free_port(), _free_port()
    env = {**os.environ, "PYTHONPATH": ROOT, "TT_PORT": str(p_app)}
    proc = subprocess.Popen([clean_py, "-m", "traffic_tester", "--role", "A", "--port", str(p_app), "--host", "127.0.0.1", "--data-dir", str(tmp_path)],
                            cwd=str(tmp_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                _http(p_app, "GET", "/api/status"); break
            except Exception:
                time.sleep(0.2)
        else:
            raise AssertionError("app did not start: " + (proc.stdout.read(2000).decode() if proc.poll() is not None else "timeout"))
        _http(p_app, "POST", "/api/responder/start", {"transport": "udp", "net": {"port": p_data}, "frame": "extended"})
        frames = _ws_frames(p_app, "/ws/live", 12)
        assert next(frames).startswith("HTTP/1.1 101"), "the live view WebSocket did not upgrade (uvicorn has no WebSocket library?)"
        _http(p_app, "POST", "/api/runs", {"profile": {"name": "ws", "transport": "udp", "frame": "extended", "net": {"peer_host": "127.0.0.1", "port": p_data},
                                                       "duration_s": 3, "interval_ms": 50}})
        seen = set()
        for f in frames:
            for e in json.loads(f).get("events", []):
                seen.add(e.get("ev"))
            if "msg" in seen:
                break
        assert "msg" in seen, f"no live test event arrived over the WebSocket (saw {sorted(seen)})"
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0].decode()
        except Exception:
            proc.kill(); out = ""
        assert "No supported WebSocket library" not in out and "Unsupported upgrade request" not in out
