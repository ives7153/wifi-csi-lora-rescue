from __future__ import annotations

import json
import time
import unittest
import urllib.error
import urllib.request

from PyQt6.QtCore import QCoreApplication

from upper_computer.core.data_manager import DataManager
from upper_computer.mobile_control import MobileControlService


class MobilePresenceOverrideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._qt_app = QCoreApplication.instance() or QCoreApplication([])

    def test_mobile_value_overrides_presence_but_keeps_serial_fields(self) -> None:
        manager = DataManager()
        manager.set_mobile_presence(2, 0.42)

        enriched = manager._apply_sample(
            {
                "timestamp": 100.0,
                "node_id": 2,
                "presence_score": 0.91,
                "motion_score": 0.35,
                "confidence": 0.88,
                "temperature": 29.4,
                "humidity": 51.2,
                "rssi": -63,
                "source": "serial",
            },
            run_rules=False,
        )

        self.assertIsNotNone(enriched)
        assert enriched is not None
        self.assertAlmostEqual(manager.nodes[2].presence_score, 0.42)
        self.assertAlmostEqual(manager.nodes[2].temperature, 29.4)
        self.assertAlmostEqual(manager.nodes[2].humidity, 51.2)
        self.assertAlmostEqual(float(enriched["presence_score"]), 0.42)
        self.assertAlmostEqual(float(enriched["raw_presence_score"]), 0.91)
        self.assertEqual(enriched["source"], "mobile_override")

    def test_single_and_all_restore_return_to_real_presence(self) -> None:
        manager = DataManager()
        manager.set_mobile_presence(1, 0.25)
        manager.set_mobile_presence(2, 0.75)
        manager.clear_mobile_presence(1)

        restored = manager._apply_sample(
            {"timestamp": 101.0, "node_id": 1, "presence_score": 0.64, "source": "serial"},
            run_rules=False,
        )
        controlled = manager._apply_sample(
            {"timestamp": 101.0, "node_id": 2, "presence_score": 0.34, "source": "serial"},
            run_rules=False,
        )

        self.assertIsNotNone(restored)
        self.assertIsNotNone(controlled)
        assert restored is not None and controlled is not None
        self.assertAlmostEqual(float(restored["presence_score"]), 0.64)
        self.assertAlmostEqual(float(controlled["presence_score"]), 0.75)

        manager.clear_all_mobile_presence()
        node_2_real = manager._apply_sample(
            {"timestamp": 102.0, "node_id": 2, "presence_score": 0.34, "source": "serial"},
            run_rules=False,
        )
        self.assertIsNotNone(node_2_real)
        assert node_2_real is not None
        self.assertAlmostEqual(float(node_2_real["presence_score"]), 0.34)

    def test_restore_immediately_reapplies_last_real_sample(self) -> None:
        manager = DataManager()
        manager._apply_sample(
            {"timestamp": 100.0, "node_id": 1, "presence_score": 0.61, "source": "serial_real"},
            run_rules=False,
        )
        manager.set_mobile_presence(1, 0.12)
        self.assertEqual(manager.nodes[1].source, "mobile_override")

        manager.clear_all_mobile_presence()

        self.assertAlmostEqual(manager.nodes[1].presence_score, 0.61)
        self.assertEqual(manager.nodes[1].source, "serial_real")

    def test_invalid_direct_values_are_ignored(self) -> None:
        manager = DataManager()

        manager.set_mobile_presence(4, 0.5)
        manager.set_mobile_presence(1, float("nan"))
        manager.set_mobile_presence(2, 1.1)

        self.assertEqual(manager._mobile_presence_overrides, {})


class MobileHttpServiceTests(unittest.TestCase):
    @staticmethod
    def _json_request(url: str, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=2.0) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def setUp(self) -> None:
        self.service = MobileControlService()
        self.assertTrue(self.service.start_service(0))
        self.base_url = f"http://127.0.0.1:{self.service.port}"

    def tearDown(self) -> None:
        self.service.stop_service()

    def test_page_state_set_and_restore(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/", timeout=2.0) as response:
            page = response.read().decode("utf-8")
        self.assertIn("EchoGuard Mobile", page)
        self.assertIn("Node ${id}", page)
        self.assertIn("无人模式", page)
        self.assertIn("活动模式", page)
        self.assertIn("微动模式", page)

        status, result = self._json_request(
            f"{self.base_url}/api/node",
            {"node_id": 3, "value": 0.58},
        )
        self.assertEqual(status, 200)
        node_3 = result["nodes"]["3"]  # type: ignore[index]
        self.assertEqual(node_3, {"active": True, "value": 0.58})

        _, restored = self._json_request(
            f"{self.base_url}/api/node",
            {"node_id": 3, "active": False},
        )
        self.assertFalse(restored["nodes"]["3"]["active"])  # type: ignore[index]

    def test_restore_all_and_invalid_requests(self) -> None:
        for node_id in (1, 2, 3):
            self._json_request(
                f"{self.base_url}/api/node",
                {"node_id": node_id, "value": node_id / 10},
            )

        _, restored = self._json_request(f"{self.base_url}/api/restore", {})
        self.assertTrue(all(not state["active"] for state in restored["nodes"].values()))  # type: ignore[union-attr]

        for payload in (
            {"node_id": 4, "value": 0.5},
            {"node_id": 1, "value": -0.1},
            {"node_id": 1, "value": 1.01},
            {"node_id": 1, "value": True},
        ):
            with self.assertRaises(urllib.error.HTTPError) as context:
                self._json_request(f"{self.base_url}/api/node", payload)
            self.assertEqual(context.exception.code, 400)

    def test_modes_stay_in_range_and_change_each_second(self) -> None:
        ranges = {
            "empty": (0.01, 0.10),
            "active": (0.80, 1.00),
            "micro": (0.50, 0.70),
        }
        for mode, (low, high) in ranges.items():
            _, started = self._json_request(f"{self.base_url}/api/mode", {"mode": mode})
            self.assertEqual(started["mode"], mode)
            first_values = {
                node_id: float(state["value"])
                for node_id, state in started["nodes"].items()  # type: ignore[union-attr]
            }
            self.assertTrue(all(low <= value <= high for value in first_values.values()))
            self.assertEqual(len(set(first_values.values())), 3)

            time.sleep(1.15)
            _, changed = self._json_request(f"{self.base_url}/api/state")
            second_values = {
                node_id: float(state["value"])
                for node_id, state in changed["nodes"].items()  # type: ignore[union-attr]
            }
            self.assertTrue(all(low <= value <= high for value in second_values.values()))
            self.assertEqual(len(set(second_values.values())), 3)
            self.assertTrue(all(second_values[node_id] != value for node_id, value in first_values.items()))

    def test_manual_or_restore_command_stops_mode(self) -> None:
        self._json_request(f"{self.base_url}/api/mode", {"mode": "active"})

        _, manual = self._json_request(
            f"{self.base_url}/api/node",
            {"node_id": 1, "value": 0.33},
        )
        self.assertIsNone(manual["mode"])
        self.assertEqual(manual["nodes"]["1"]["value"], 0.33)  # type: ignore[index]

        self._json_request(f"{self.base_url}/api/mode", {"mode": "micro"})
        _, restored = self._json_request(
            f"{self.base_url}/api/node",
            {"node_id": 2, "active": False},
        )
        self.assertIsNone(restored["mode"])
        self.assertFalse(restored["nodes"]["2"]["active"])  # type: ignore[index]

        with self.assertRaises(urllib.error.HTTPError) as context:
            self._json_request(f"{self.base_url}/api/mode", {"mode": "unknown"})
        self.assertEqual(context.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
