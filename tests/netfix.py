"""Real apps on real HTTP ports inside one test process: each `Pi` is a full app (own data dir, own port) so peers can talk to each other."""
import os, socket, tempfile, threading, time
import uvicorn
from traffic_tester.server import create_app


def free_tcp_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Pi:
    def __init__(self, name, password=None):
        self.name, self.port, self.dir = name, free_tcp_port(), tempfile.mkdtemp()
        os.environ["TT_PORT"] = str(self.port)
        self.app = create_app(self.dir)
        self.st = self.app.state.tt
        self.st.me.set_name(name)
        if password:
            self.st.store.set_setting("password_hash", self.st.hash_pw(password))
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{self.port}"

    @property
    def addr(self):
        return f"127.0.0.1:{self.port}"

    def stop(self):
        self.st.runs.stop()
        self.st.runs.wait(5)
        self.st.peers.stop()
        self.st.reflectors.stop_all()
        self.server.should_exit = True
        self.thread.join(5)

    def wait_done(self, timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self.st.runs.busy() and self.st.runs.legs:
                time.sleep(0.2)
                if not self.st.runs.busy():
                    return
            time.sleep(0.1)
        raise TimeoutError("test did not finish")

    def runs(self):
        return self.st.store.list_runs()
