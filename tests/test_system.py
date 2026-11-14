from traffic_tester import system


def test_selfcheck_not_ftdi(monkeypatch):
    monkeypatch.setattr(system, "ftdi_latency", lambda p: None)
    r = system.selfcheck("/dev/pts/3")
    assert r["ok"] and not r["ftdi"]


def test_selfcheck_slow_adapter_reports_and_gives_udev_rule(monkeypatch):
    monkeypatch.setattr(system, "ftdi_latency", lambda p: 16)
    r = system.selfcheck("/dev/ttyUSB0")
    assert not r["ok"] and r["latency_timer_ms"] == 16 and "16 ms" in r["message"] and "latency_timer" in r["udev_rule"]


def test_selfcheck_fix_without_permission_explains(monkeypatch):
    monkeypatch.setattr(system, "ftdi_latency", lambda p: 16)
    r = system.selfcheck("/dev/ttyUSB_nonexistent", fix=True)    # sysfs path missing -> OSError
    assert not r["ok"] and not r["fixed"] and "udev rule" in r["message"]


def test_selfcheck_good_adapter(monkeypatch):
    monkeypatch.setattr(system, "ftdi_latency", lambda p: 1)
    assert system.selfcheck("/dev/ttyUSB0")["ok"]
