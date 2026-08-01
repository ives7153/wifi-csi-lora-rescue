from __future__ import annotations

import unittest

from upper_computer.core.data_manager import DataManager
from upper_computer.rules.detection_fusion import build_detection_summary, life_motion_triggered
from upper_computer.ui.pages import _sample_validity


class PresenceHardThresholdTests(unittest.TestCase):
    def test_below_equal_and_above_threshold(self) -> None:
        threshold = 0.50

        self.assertFalse(life_motion_triggered({"presence_score": 0.49}, presence_threshold=threshold))
        self.assertTrue(life_motion_triggered({"presence_score": 0.50}, presence_threshold=threshold))
        self.assertTrue(life_motion_triggered({"presence_score": 0.51}, presence_threshold=threshold))

    def test_zero_to_one_and_zero_to_one_hundred_values_share_rule(self) -> None:
        self.assertTrue(life_motion_triggered({"presence_score": 0.72}, presence_threshold=0.70))
        self.assertTrue(life_motion_triggered({"presence": 72}, presence_threshold=0.70))
        self.assertFalse(life_motion_triggered({"presence": 69}, presence_threshold=0.70))

    def test_missing_presence_never_triggers(self) -> None:
        self.assertFalse(life_motion_triggered({"node_id": 1}, presence_threshold=0.0))
        self.assertFalse(life_motion_triggered(None, presence_threshold=0.0))

    def test_summary_uses_only_presence_threshold(self) -> None:
        sample = {
            "node_id": 1,
            "presence_score": 0.60,
            "confidence": 0.0,
            "csi_quality": 0.0,
            "timestamp": 10.0,
        }

        triggered = build_detection_summary({}, [sample], reference_ts=10.0, presence_threshold=0.60)
        not_triggered = build_detection_summary({}, [sample], reference_ts=10.0, presence_threshold=0.61)

        self.assertEqual(triggered.status, "疑似局部微动")
        self.assertEqual(triggered.triggered_ids, [1])
        self.assertEqual(not_triggered.status, "数据不足")
        self.assertEqual(not_triggered.triggered_ids, [])

    def test_runtime_threshold_updates_alarm_engine_and_history_label(self) -> None:
        manager = DataManager()
        manager.set_presence_threshold(0.80)
        sample = {
            "node_id": 3,
            "presence_score": 0.70,
            "confidence": 0.0,
            "csi_quality": 0.0,
            "timestamp": 10.0,
        }

        self.assertAlmostEqual(manager.presence_threshold, 0.80)
        self.assertAlmostEqual(manager.alarm_engine.presence_threshold, 0.80)
        self.assertEqual(manager.alarm_engine.evaluate(sample, now=10.0), [])
        self.assertNotEqual(_sample_validity(sample, manager.presence_threshold), "疑似微动")

        manager.set_presence_threshold(0.70)
        alarms = manager.alarm_engine.evaluate(sample, now=20.0)

        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0]["kind"], "life_motion")
        self.assertEqual(_sample_validity(sample, manager.presence_threshold), "疑似微动")

    def test_runtime_threshold_is_clamped_to_zero_one(self) -> None:
        manager = DataManager()

        manager.set_presence_threshold(-1.0)
        self.assertEqual(manager.presence_threshold, 0.0)
        self.assertEqual(manager.alarm_engine.presence_threshold, 0.0)

        manager.set_presence_threshold(2.0)
        self.assertEqual(manager.presence_threshold, 1.0)
        self.assertEqual(manager.alarm_engine.presence_threshold, 1.0)


if __name__ == "__main__":
    unittest.main()
