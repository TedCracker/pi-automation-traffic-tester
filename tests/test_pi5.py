"""Raspberry Pi 5 serial/RP1 assumptions: port discovery order + labels, auto pick, model detection."""
import os
import pytest
from traffic_tester import system, factory


@pytest.fixture()
def fake_dev(tmp_path, monkeypatch):
    names = ["ttyAMA10", "ttyAMA0", "ttyAMA2", "ttyUSB0", "ttyACM0"]
    for n in names:
        (tmp_path / n).write_text("")
    monkeypatch.setattr(system, "_PATTERNS", [str(tmp_path / "ttyUSB*"), str(tmp_path / "ttyACM*"), str(tmp_path / "ttyAMA*")])
    monkeypatch.setattr(system, "_usb_info", lambda p: {"vendor": "FTDI", "product": "FT232R USB UART", "serial": "A10K3XYZ"} if "ttyUSB" in p else {})
    monkeypatch.setattr(system, "ftdi_latency", lambda p: 1 if "ttyUSB" in p else None)
    return tmp_path


def test_port_order_usb_then_gpio_uarts_then_acm_then_debug_connector(fake_dev):
    ports = system.list_ports()
    assert [os.path.basename(p["path"]) for p in ports] == ["ttyUSB0", "ttyAMA0", "ttyAMA2", "ttyACM0", "ttyAMA10"]
    by = {os.path.basename(p["path"]): p for p in ports}
    assert by["ttyAMA10"]["debug_connector"] and "debug connector" in by["ttyAMA10"]["label"] and by["ttyAMA10"]["kind"] == "debug"
    assert "GPIO 14/15" in by["ttyAMA0"]["label"] and "dtparam=uart0=on" in by["ttyAMA0"]["label"] and by["ttyAMA0"]["kind"] == "gpio"
    assert "uart2-pi5" in by["ttyAMA2"]["label"]
    assert by["ttyUSB0"]["usb"] and "FTDI" in by["ttyUSB0"]["label"] and not by["ttyUSB0"]["debug_connector"]


def test_auto_prefers_usb_then_gpio_never_debug_first(fake_dev, monkeypatch):
    assert factory.resolve_serial_port("auto").endswith("ttyUSB0")
    for n in ("ttyUSB0", "ttyACM0"):
        (fake_dev / n).unlink()
    assert factory.resolve_serial_port("auto").endswith("ttyAMA0")          # GPIO UART beats the debug connector
    (fake_dev / "ttyAMA0").unlink(); (fake_dev / "ttyAMA2").unlink()
    assert factory.resolve_serial_port("auto").endswith("ttyAMA10")          # only the debug connector left: still usable
    assert factory.resolve_serial_port("/dev/ttyAMA3") == "/dev/ttyAMA3"     # explicit choice is never overridden


def test_pi_model_detection(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_PI_MODEL", "Raspberry Pi 5 Model B Rev 1.0")
    assert system.pi_model() == {"model": "Raspberry Pi 5 Model B Rev 1.0", "is_pi": True, "is_pi5": True}
    monkeypatch.setenv("TT_PI_MODEL", "Raspberry Pi 4 Model B Rev 1.5")
    m = system.pi_model()
    assert m["is_pi"] and not m["is_pi5"]
    monkeypatch.delenv("TT_PI_MODEL")
    f = tmp_path / "model"; f.write_bytes(b"Raspberry Pi 5 Model B Rev 1.0\x00")       # device-tree strings end in NUL
    monkeypatch.setenv("TT_MODEL_FILE", str(f))
    assert system.pi_model()["is_pi5"]
    monkeypatch.setenv("TT_MODEL_FILE", str(tmp_path / "missing"))
    assert system.pi_model() == {"model": None, "is_pi": False, "is_pi5": False}


def test_status_reports_hardware(monkeypatch):
    import tempfile
    from fastapi.testclient import TestClient
    from traffic_tester.server import create_app
    monkeypatch.setenv("TT_PI_MODEL", "Raspberry Pi 5 Model B Rev 1.0")
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        assert c.get("/api/status").json()["hardware"]["is_pi5"] is True
        assert "built for the Raspberry Pi 5" in c.get("/").text
