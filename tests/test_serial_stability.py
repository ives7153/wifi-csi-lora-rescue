from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import Mock, patch

from upper_computer.data_parser import parse_gateway_frame
from upper_computer.serial_handler import open_serial_port


class _FakeSerial:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self.is_open = False

    def __setattr__(self, name: str, value: object) -> None:
        if name not in {"events", "is_open"} and "events" in self.__dict__:
            self.events.append((name, value))
        object.__setattr__(self, name, value)

    def open(self) -> None:
        self.events.append(("open", True))
        self.is_open = True

    def close(self) -> None:
        self.events.append(("close", True))
        self.is_open = False


class SerialStabilityTest(unittest.TestCase):
    def test_safe_open_disables_control_lines_before_open(self) -> None:
        fake_serial = _FakeSerial()
        serial_module = types.SimpleNamespace(Serial=lambda: fake_serial)

        with patch.dict(sys.modules, {"serial": serial_module}):
            result = open_serial_port("COM11", 115200, timeout=0.12)

        self.assertIs(result, fake_serial)
        self.assertTrue(fake_serial.is_open)
        self.assertLess(fake_serial.events.index(("dtr", False)), fake_serial.events.index(("open", True)))
        self.assertLess(fake_serial.events.index(("rts", False)), fake_serial.events.index(("open", True)))
        self.assertEqual(fake_serial.port, "COM11")
        self.assertEqual(fake_serial.baudrate, 115200)

    def test_auto_connected_gateway_is_not_dropped_when_frames_are_delayed(self) -> None:
        from upper_computer.core.data_manager import DataManager

        manager = DataManager()
        manager._serial_connected = True
        manager._serial_auto_connected = True
        manager._serial_started_at = 1.0
        manager._last_serial_sample_at = 0.0
        manager.stop_serial = Mock()

        manager._auto_service()

        manager.stop_serial.assert_not_called()

    def test_unique_ch340_gateway_is_selected_without_probe_open(self) -> None:
        from upper_computer.core import data_manager as data_manager_module

        manager = data_manager_module.DataManager()
        manager.available_ports = ["COM10", "COM11"]
        manager._preferred_gateway_ports = ["COM11"]

        with patch.object(data_manager_module, "open_serial_port") as serial_open:
            selected = manager._probe_gateway_port()

        self.assertEqual(selected, "COM11")
        serial_open.assert_not_called()

    def test_live_node3_gateway_frame_is_valid(self) -> None:
        parsed = parse_gateway_frame(
            '{"id":3,"seq":1,"presence":0,"motion":0,"bpm":0,"conf":32,'
            '"gas":207,"temp":30.2,"hum":53,"rssi":-72,"ts":38848}'
        )

        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["node_id"], 3)
        self.assertEqual(parsed["seq"], 1)
        self.assertEqual(parsed["rssi"], -72.0)


if __name__ == "__main__":
    unittest.main()
