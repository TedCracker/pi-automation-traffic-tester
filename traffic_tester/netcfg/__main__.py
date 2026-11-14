"""Command line for the same operations the web page uses (also what the container end-to-end test drives):
   python -m traffic_tester.netcfg state | pending | apply '<json changes>' [--no-revert] | keep | revert | scan | clock serve|client|none|status [--server IP] | warn <host>"""
from __future__ import annotations

import json
import sys

from . import client


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if not a:
        print(__doc__)
        return 2
    cmd = a[0]
    try:
        if cmd == "state":
            out = client.read_state()
        elif cmd == "pending":
            out = {"pending": client.read_pending(), "result": client.read_result()}
        elif cmd == "apply":
            out = client.call("apply", {"changes": json.loads(a[1]), "auto_revert": "--no-revert" not in a, "revert_seconds": 90}, 120)
        elif cmd in ("keep", "revert", "scan"):
            out = client.call(cmd, {}, 90)
        elif cmd == "clock":
            out = client.clock(a[1], a[a.index("--server") + 1] if "--server" in a else None)
        elif cmd == "warn":
            from .paths import path_warning
            out = {"warning": path_warning(a[1])}
        else:
            print(__doc__)
            return 2
    except client.NetClientError as e:
        print(json.dumps({"ok": False, "message": e.message}))
        return 1
    print(json.dumps(out))
    return 0 if out.get("ok", True) is not False else 1


if __name__ == "__main__":
    sys.exit(main())
