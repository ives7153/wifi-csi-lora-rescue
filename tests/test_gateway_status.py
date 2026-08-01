from __future__ import annotations

import unittest

from PyQt6.QtCore import QCoreApplication

from upper_computer.core.data_manager import DataManager
from upper_computer.data_parser import parse_gateway_frame


class GatewayStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_gateway_status_is_valid_without_creating_a_node(self) -> None:
        raw = (
            '{"type":"gateway_status","protocol":1,"firmware":"v0.3.3",'
            '"gateway_id":"GW-01","ssid":"EchoGuard-GW-01","uptime_ms":1000,'
            '"rx_ok":12,"crc_errors":1,"bad_length":0,"parse_errors":0,'
            '"queue_drops":0,"queue_depth":0,"wifi_clients":2}'
        )
        frame = parse_gateway_frame(raw)
        self.assertTrue(frame["valid"])
        self.assertEqual(frame["frame_type"], "gateway_status")

        manager = DataManager()
        manager._handle_raw_line(raw)
        self.assertEqual(manager.nodes, {})
        self.assertEqual(manager._gateway_status["gateway_id"], "GW-01")
        self.assertEqual(manager._serial_stats["total_gateway_status"], 1)


if __name__ == "__main__":
    unittest.main()
