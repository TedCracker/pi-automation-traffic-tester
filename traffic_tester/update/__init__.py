"""Software updates: signed update bundles (.ptt), the on-Pi installer/rollback helper, and the web-app side that drives them.
Everything in this package imports only the standard library and itself, so the same files also run as the small root-owned
helper (system install) or as a detached helper (rootless install) without the rest of the app."""

# Bumped only when a bundle needs something the installed helper/units cannot do (then the Pi says: run install-pi.sh from the new package).
INSTALLER_LEVEL = 1
BUNDLE_FORMAT = 1
APP_NAME = "pi-traffic-tester"
