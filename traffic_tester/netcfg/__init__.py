"""Network settings of the Pi: Wi-Fi (access point / join a network / off) and Ethernet ports (DHCP or static, VLAN, label Internet/Test),
with an automatic revert when a change cuts you off and an emergency access point when the Pi has no network at all.

  client.py    app side of the privileged helper (traffic_tester/update/netops.py runs as root; the app never is)
  api.py       register(app, st)  -> /api/net/*
  plan.py      dry run in plain words
  state.py     saved + live state for the card
  watchdog.py  start(get_state)   the dead-man thread
  paths.py     path_warning(host) -> plain-words warning when a test's path would go over the Wi-Fi / an Internet-only port
"""
from .paths import path_warning  # noqa: F401
from . import client  # noqa: F401
