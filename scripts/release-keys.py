#!/usr/bin/env python3
"""Release signing key for software updates. The PRIVATE key stays on the release machine only:
   ~/.config/pi-traffic-tester/release-signing.key   (hex seed, chmod 600, never in git, never printed)
The matching PUBLIC key is built into the app (traffic_tester/update/pubkey.py). Commands:
   release-keys.py init     create the private key if missing, then write pubkey.py from it (idempotent)
   release-keys.py show     print the PUBLIC key (hex)"""
import os, stat, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from traffic_tester.update import ed25519

KEY = os.path.expanduser(os.environ.get("TT_RELEASE_KEY", "~/.config/pi-traffic-tester/release-signing.key"))
PUBPY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "traffic_tester", "update", "pubkey.py")


def load_seed(path: str = KEY) -> bytes:
    with open(path) as f:
        return bytes.fromhex(f.read().strip())


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "init":
        if not os.path.exists(KEY):
            os.makedirs(os.path.dirname(KEY), mode=0o700, exist_ok=True)
            fd = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(ed25519.generate_seed().hex() + "\n")
            print("created the signing key at", KEY, "(keep it private; back it up somewhere safe)")
        os.chmod(KEY, stat.S_IRUSR | stat.S_IWUSR)
        pub = ed25519.public_key(load_seed()).hex()
        with open(PUBPY, "w") as f:
            f.write('"""The public half of the release signing key. An update is accepted ONLY if its signature verifies against this key."""\n'
                    f'PUBLIC_KEY_HEX = "{pub}"\n')
        print("public key:", pub)
    elif cmd == "show":
        print(ed25519.public_key(load_seed()).hex())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
