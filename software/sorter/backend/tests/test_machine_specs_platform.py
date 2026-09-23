from unittest.mock import patch

import machine_specs


def test_v4l2_camera_model_returns_none_off_linux(monkeypatch):
    monkeypatch.setattr(machine_specs.platform, "system", lambda: "Windows")

    assert machine_specs._v4l2CameraModel(0) is None


def test_mac_addresses_falls_back_to_uuid_node(monkeypatch):
    monkeypatch.setattr(machine_specs.platform, "system", lambda: "Windows")
    monkeypatch.setattr(machine_specs.uuid, "getnode", lambda: 0x001122334455)

    assert machine_specs._macAddresses() == ["00:11:22:33:44:55"]