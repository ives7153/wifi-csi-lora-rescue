from __future__ import annotations

import unittest

from upper_computer.config import CSV_FIELDS, PRESENCE_THRESHOLD
from upper_computer.core.alarm_rules import AlarmEngine
from upper_computer.data_parser import parse_gateway_frame
from upper_computer.rules.detection_fusion import build_detection_summary, life_motion_triggered


class CsiQualityRulesTest(unittest.TestCase):
    def test_old_gateway_frame_still_parses_without_csi_fields(self) -> None:
        frame = parse_gateway_frame(
            '{"id":1,"seq":2,"presence":82,"motion":24,"bpm":18,"conf":88,'
            '"gas":1000,"temp":25.0,"hum":50,"rssi":-60}'
        )

        self.assertTrue(frame["valid"])
        self.assertEqual(frame["csi_quality"], None)
        self.assertEqual(frame["csi_sample_count"], None)
        self.assertTrue(life_motion_triggered(frame))

    def test_optional_csi_quality_fields_parse_and_export_columns_exist(self) -> None:
        frame = parse_gateway_frame(
            '{"id":2,"presence":70,"motion":20,"conf":90,"csi_quality":76,'
            '"csi_sample_count":48,"breath_lock":1,"noise_floor":2.5}'
        )

        self.assertTrue(frame["valid"])
        self.assertAlmostEqual(frame["csi_quality"], 0.76)
        self.assertEqual(frame["csi_sample_count"], 48)
        self.assertIs(frame["breath_lock"], True)
        self.assertEqual(frame["noise_floor"], 2.5)
        for field in ("csi_quality", "csi_sample_count", "breath_lock", "noise_floor"):
            self.assertIn(field, CSV_FIELDS)

    def test_low_quality_low_confidence_high_presence_still_triggers(self) -> None:
        sample = {
            "node_id": 1,
            "presence_score": 0.92,
            "motion_score": 0.2,
            "confidence": 0.10,
            "csi_quality": 0.2,
            "timestamp": 10.0,
        }

        self.assertTrue(life_motion_triggered(sample))
        summary = build_detection_summary({1: sample}, [sample], reference_ts=10.0)
        self.assertEqual(summary.status, "疑似局部微动")

        engine = AlarmEngine()
        alarms = engine.evaluate(sample, now=10.0)
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0]["kind"], "life_motion")

    def test_two_high_quality_nodes_trigger_multi_node_summary(self) -> None:
        history = [
            {"node_id": 1, "presence_score": 0.72, "confidence": 0.84, "timestamp": 10.0},
            {"node_id": 2, "presence_score": 0.68, "confidence": 0.82, "timestamp": 10.0},
        ]
        summary = build_detection_summary({}, history, reference_ts=10.0)

        self.assertEqual(summary.status, "多节点疑似生命微动")
        self.assertEqual(summary.triggered_ids, [1, 2])

    def test_alarm_engine_uses_same_presence_threshold_helper(self) -> None:
        sample = {"node_id": 3, "presence_score": PRESENCE_THRESHOLD + 0.05, "confidence": 0.8}
        engine = AlarmEngine()

        self.assertTrue(life_motion_triggered(sample, presence_threshold=engine.presence_threshold))
        alarms = engine.evaluate(sample, now=20.0)
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0]["kind"], "life_motion")


if __name__ == "__main__":
    unittest.main()
