import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Unit tests never start the always-on reflector or mDNS discovery (many apps live in one process and one network namespace);
# the tests that are about them switch them on themselves.
os.environ.setdefault("TT_NO_DEFAULT_REFLECTOR", "1")
os.environ.setdefault("TT_NO_DISCOVERY", "1")
os.environ.setdefault("TT_NO_NET_WATCHDOG", "1")
