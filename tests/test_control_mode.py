from __future__ import annotations

import unittest

from PyQt6.QtCore import QCoreApplication

from upper_computer.core.data_manager import DataManager
from upper_computer.rules.detection_fusion import build_detection_summary


class ControlModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._qt_app = QCoreApplication.instance() or QCoreApplication([])

    def test_control_sample_creates_node_and_history(self) -> None:
        manager = DataManager()

        manager.set_control_node(3, True, 1)

        self.assertIn(3, manager.nodes)
        self.assertTrue(manager.nodes[3].online)
        self.assertGreater(manager.nodes[3].presence_score, 0.8)
        self.assertEqual(manager.nodes[3].source, "demo_mode")
        self.assertTrue(manager.history)
        self.assertEqual(manager.history[-1]["node_id"], 3)
        self.assertEqual(manager.history[-1]["source"], "demo_mode")

    def test_controlled_node_ignores_serial_sample(self) -> None:
        manager = DataManager()
        manager.set_control_node(2, True, 1)
        controlled_presence = manager.nodes[2].presence_score

        manager._handle_frame_batch(
            [
                {
                    "node_id": 2,
                    "presence_score": 0.0,
                    "motion_score": 0.0,
                    "confidence": 0.1,
                    "gas_raw": 500,
                    "timestamp": manager.history[-1]["timestamp"] + 0.1,
                    "source": "serial",
                }
            ],
            {"total": 1, "valid": 1, "invalid": 0, "dropped": 0, "pending": 0},
        )

        self.assertAlmostEqual(manager.nodes[2].presence_score, controlled_presence)
        self.assertEqual(manager.history[-1]["source"], "demo_mode")

    def test_uncontrolled_node_accepts_serial_sample(self) -> None:
        manager = DataManager()
        manager.set_control_node(2, True, 1)

        manager._handle_frame_batch(
            [
                {
                    "node_id": 4,
                    "presence_score": 0.44,
                    "motion_score": 0.12,
                    "confidence": 0.79,
                    "gas_raw": 500,
                    "timestamp": 100.0,
                    "source": "serial",
                }
            ],
            {"total": 1, "valid": 1, "invalid": 0, "dropped": 0, "pending": 0},
        )

        self.assertIn(4, manager.nodes)
        self.assertAlmostEqual(manager.nodes[4].presence_score, 0.44)
        self.assertEqual(manager.nodes[4].source, "serial_real")

    def test_multi_node_scene_triggers_summary(self) -> None:
        manager = DataManager()

        manager.apply_control_scene("multi_node")
        manager._inject_control_samples()
        summary = build_detection_summary(manager._node_dicts(), manager._recent_history(60, 1200))

        self.assertEqual(summary.status, "多节点疑似生命微动")
        self.assertEqual(summary.triggered_ids, [1, 2])

    def test_all_zero_scene_does_not_trigger_life_motion(self) -> None:
        manager = DataManager()

        manager.apply_control_scene("all_zero")
        summary = build_detection_summary(manager._node_dicts(), manager._recent_history(60, 1200))

        self.assertNotEqual(summary.status, "多节点疑似生命微动")
        self.assertEqual(summary.triggered_ids, [])


if __name__ == "__main__":
    unittest.main()
