import argparse
import os
import sys

import uvicorn

from .server import create_app


def main():
    ap = argparse.ArgumentParser(description="Pi Automation Traffic Tester")
    ap.add_argument("--role", choices=["A", "B"], default=None, help="ignored: there are no fixed roles any more (every Pi is the same); read once to carry an older install over")
    ap.add_argument("--port", type=int, default=int(os.environ.get("TT_PORT", "8080")))
    ap.add_argument("--host", default=os.environ.get("TT_HOST", "0.0.0.0"))
    ap.add_argument("--data-dir", default=os.environ.get("TT_DATA_DIR", "/var/lib/traffic-tester"))
    a = ap.parse_args()
    if a.role:
        print("Note: --role is ignored. There are no fixed roles any more: every Pi is the same; each test picks its source and destination.", file=sys.stderr)
    os.environ["TT_PORT"] = str(a.port)          # the Claude tool server and the Pi list call back to this port
    uvicorn.run(create_app(a.data_dir, a.role or os.environ.get("TT_ROLE")), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
