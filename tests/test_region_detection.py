from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication

from upper_computer import region_detection as region_module
from upper_computer.core.data_manager import DataManager
from upper_computer.data_parser import parse_gateway_frame
from upper_computer.region_detection import (
    CalibrationError,
    EXPECTED_LINKS,
    RegionProfile,
    TriangleRegionDetector,
)


def _raw_csi_json(node_id: int = 1, gateway_id: str = "GW-02") -> str:
    sources = EXPECTED_LINKS[node_id]
    links = [
        {
            "src": source,
            "valid": True,
            "n": 16,
            "rssi": -55 - source,
            "rssi_std": 4,
            "active": 20,
            "corr": 3,
            "mad": list(range(1, 9)),
            "diff": list(range(11, 19)),
        }
        for source in sources
    ]
    return json.dumps(
        {
            "type": "csi_features",
            "v": 1,
            "gateway_id": gateway_id,
            "node": node_id,
            "node_mac": f"02:00:00:00:00:0{node_id}",
            "seq": 7,
            "epoch_ms": 1234,
            "flags": 3,
            "links": links,
        }
    )


def _feature_frame(
    node_id: int, level: int = 32, gateway_id: str = "GW-02"
) -> dict[str, object]:
    return {
        "frame_type": "csi_features",
        "gateway_id": gateway_id,
        "node_id": node_id,
        "node_mac": f"02:00:00:00:00:0{node_id}",
        "links": [
            {
                "source_id": source,
                "valid": True,
                "sample_count": 16,
                "rssi": -55.0,
                "rssi_std": 4.0,
                "active_ratio": float(level),
                "correlation_delta": float(level),
                "mad_bands": [level] * 8,
                "diff_bands": [level] * 8,
            }
            for source in EXPECTED_LINKS[node_id]
        ],
    }


def _ingest_cycle(
    detector: TriangleRegionDetector,
    *,
    level: int,
    now: float,
    gateway_id: str = "GW-02",
) -> None:
    for node_id in (1, 2, 3):
        detector.ingest(
            _feature_frame(node_id, level, gateway_id=gateway_id), now=now + node_id * 0.01
        )


class RegionDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_parser_normalizes_csi_feature_fields(self) -> None:
        frame = parse_gateway_frame(_raw_csi_json())

        self.assertTrue(frame["valid"])
        self.assertEqual(frame["frame_type"], "csi_features")
        self.assertEqual(frame["gateway_id"], "GW-02")
        self.assertEqual(frame["node_id"], 1)
        self.assertEqual(frame["node_mac"], "02:00:00:00:00:01")
        self.assertEqual(len(frame["links"]), 3)
        self.assertEqual(frame["links"][0]["sample_count"], 16)
        self.assertEqual(frame["links"][0]["mad_bands"], list(range(1, 9)))

    def test_nine_links_form_a_189_dimension_vector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(Path(directory) / "profile.json")
            _ingest_cycle(detector, level=32, now=100.0)

            vector = detector._assemble_vector(100.1)

        self.assertIsNotNone(vector)
        self.assertEqual(len(vector or []), 9 * 21)
        self.assertEqual(detector.valid_links, 9)

    def test_calibration_trains_and_saves_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "profile.json"
            detector = TriangleRegionDetector(profile_path)
            detector.node_macs = {node: f"02:00:00:00:00:0{node}" for node in (1, 2, 3)}
            width = 9 * 21
            detector.samples = {
                "empty": [[0.08 + (index % 3) * 0.001] * width for index in range(15)],
                "inside": [[0.82 + (index % 3) * 0.001] * width for index in range(15)],
                "outside": [[0.24 + (index % 3) * 0.001] * width for index in range(15)],
            }
            with patch.dict(
                "upper_computer.region_detection.MIN_PHASE_SAMPLES",
                {"empty": 10, "inside": 10, "outside": 10},
                clear=True,
            ):
                with patch.object(
                    region_module,
                    "_fit_feature_space",
                    wraps=region_module._fit_feature_space,
                ) as fit_feature_space:
                    detector._train_and_save()
            # 最终模型拟合一次，每个连续验证折单独拟合一次，验证段不参与参数估计。
            self.assertEqual(fit_feature_space.call_count, 6)

            self.assertTrue(profile_path.exists())
            loaded = TriangleRegionDetector(profile_path)

        self.assertIsNotNone(detector.profile)
        self.assertIsNotNone(loaded.profile)
        self.assertGreaterEqual(detector.validation_metrics["inside_recall"], 0.95)
        self.assertLessEqual(detector.validation_metrics["outside_false_positive"], 0.05)
        self.assertEqual(detector.validation_metrics["cv_folds"], 5.0)

    def test_production_phase_minimums_survive_training_downsample(self) -> None:
        """180 秒阶段达到 250 条后不能被限量逻辑重新压成“不足”。"""
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(Path(directory) / "profile.json")
            detector.node_macs = {
                node: f"02:00:00:00:00:0{node}" for node in (1, 2, 3)
            }
            width = 6
            detector.samples = {
                "empty": [
                    [0.08 + (index % 5) * 0.0005] * width
                    for index in range(region_module.MIN_PHASE_SAMPLES["empty"])
                ],
                "inside": [
                    [0.82 + (index % 5) * 0.0005] * width
                    for index in range(region_module.MIN_PHASE_SAMPLES["inside"])
                ],
                "outside": [
                    [0.24 + (index % 5) * 0.0005] * width
                    for index in range(region_module.MIN_PHASE_SAMPLES["outside"])
                ],
            }

            detector._train_and_save()

        self.assertIsNotNone(detector.profile)
        self.assertGreaterEqual(detector.validation_metrics["inside_recall"], 0.95)
        self.assertLessEqual(
            detector.validation_metrics["outside_false_positive"], 0.05
        )

    def test_two_inside_and_three_outside_windows_apply_hysteresis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(Path(directory) / "profile.json")
            detector.profile = RegionProfile(
                version=1,
                gateway_id="GW-02",
                node_macs={str(node): f"02:00:00:00:00:0{node}" for node in (1, 2, 3)},
                channel=6,
                created_at=1.0,
                feature_medians=[0.0] * (9 * 21),
                feature_scales=[1.0] * (9 * 21),
                selected_indices=[0],
                train_vectors=[[0.1], [0.11], [0.12], [0.8], [0.81], [0.82], [0.25]],
                train_labels=["empty", "empty", "outside", "inside", "inside", "inside", "outside"],
                inside_threshold=0.5,
                outlier_distance=10.0,
                metrics={},
            )
            _ingest_cycle(detector, level=204, now=10.0)
            self.assertNotEqual(detector.status, "occupied")
            _ingest_cycle(detector, level=204, now=10.6)
            self.assertEqual(detector.status, "occupied")
            _ingest_cycle(detector, level=25, now=11.2)
            _ingest_cycle(detector, level=25, now=11.8)
            self.assertEqual(detector.status, "occupied")
            _ingest_cycle(detector, level=25, now=12.4)
            self.assertEqual(detector.status, "clear")

    def test_profile_is_bound_to_gateway_and_node_macs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(Path(directory) / "profile.json")
            detector.profile = RegionProfile(
                version=1,
                gateway_id="GW-02",
                node_macs={"1": "aa:00:00:00:00:01", "2": "aa:00:00:00:00:02", "3": "aa:00:00:00:00:03"},
                channel=6,
                created_at=1.0,
                feature_medians=[0.0] * (9 * 21),
                feature_scales=[1.0] * (9 * 21),
                selected_indices=[0],
                train_vectors=[[0.1], [0.8]],
                train_labels=["empty", "inside"],
                inside_threshold=0.5,
                outlier_distance=10.0,
                metrics={},
            )
            _ingest_cycle(detector, level=32, now=20.0)

        self.assertEqual(detector.status, "profile_mismatch")

    def test_missing_link_yields_insufficient_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(Path(directory) / "profile.json")
            frames = {node: _feature_frame(node, 32) for node in (1, 2, 3)}
            frames[2]["links"] = list(frames[2]["links"])[1:]
            for node_id in (1, 2, 3):
                detector.ingest(frames[node_id], now=30.0 + node_id * 0.01)

        self.assertIsNone(detector._assemble_vector(30.1))
        self.assertLess(detector.valid_links, 9)
        self.assertEqual(detector.status, "not_calibrated")

    def test_calibration_rejects_indistinguishable_classes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(Path(directory) / "profile.json")
            detector.node_macs = {node: f"02:00:00:00:00:0{node}" for node in (1, 2, 3)}
            width = 9 * 21
            same_rows = [[0.3] * width for _ in range(12)]
            detector.samples = {phase: [list(row) for row in same_rows] for phase in ("empty", "inside", "outside")}
            with patch.dict(
                "upper_computer.region_detection.MIN_PHASE_SAMPLES",
                {"empty": 10, "inside": 10, "outside": 10},
                clear=True,
            ):
                with self.assertRaises(CalibrationError):
                    detector._train_and_save()

        self.assertIsNone(detector.profile)

    def test_mobile_and_replay_sources_do_not_drive_region_detector(self) -> None:
        manager = DataManager()
        parsed = parse_gateway_frame(_raw_csi_json())
        parsed["source"] = "replay"
        manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})
        manager.set_mobile_presence(1, 0.95)

        self.assertEqual(manager.region_detector.last_feature_at, 0.0)
        self.assertEqual(manager.region_detector.latest_frames, {})

    def test_gw01_nine_links_form_a_189_dimension_vector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(
                Path(directory) / "gw01_profile.json", gateway_id="GW-01"
            )
            _ingest_cycle(detector, level=32, now=100.0, gateway_id="GW-01")

            vector = detector._assemble_vector(100.1)

        self.assertIsNotNone(vector)
        self.assertEqual(len(vector or []), 9 * 21)
        self.assertEqual(detector.valid_links, 9)
        self.assertEqual(detector.gateway_id, "GW-01")
        self.assertEqual(detector.snapshot()["gateway_id"], "GW-01")

    def test_gw01_calibration_trains_and_saves_gateway_bound_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "gw01_profile.json"
            detector = TriangleRegionDetector(profile_path, gateway_id="GW-01")
            detector.node_macs = {
                node: f"02:00:00:00:00:0{node}" for node in (1, 2, 3)
            }
            width = 9 * 21
            detector.samples = {
                "empty": [[0.08 + (index % 3) * 0.001] * width for index in range(15)],
                "inside": [[0.82 + (index % 3) * 0.001] * width for index in range(15)],
                "outside": [[0.24 + (index % 3) * 0.001] * width for index in range(15)],
            }
            with patch.dict(
                "upper_computer.region_detection.MIN_PHASE_SAMPLES",
                {"empty": 10, "inside": 10, "outside": 10},
                clear=True,
            ):
                detector._train_and_save()

            self.assertTrue(profile_path.exists())
            self.assertEqual(detector.profile.gateway_id, "GW-01")
            loaded = TriangleRegionDetector(profile_path, gateway_id="GW-01")

        self.assertIsNotNone(loaded.profile)
        self.assertEqual(loaded.profile.gateway_id, "GW-01")

    def test_gateway_profiles_are_isolated_by_path_and_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gw01_path = root / "triangle_calibration_gw01.json"
            gw02_path = root / "triangle_calibration_gw02.json"
            gw01 = TriangleRegionDetector(gw01_path, gateway_id="GW-01")
            gw01.node_macs = {
                node: f"02:00:00:00:00:0{node}" for node in (1, 2, 3)
            }
            width = 9 * 21
            gw01.samples = {
                "empty": [[0.08 + (index % 3) * 0.001] * width for index in range(15)],
                "inside": [[0.82 + (index % 3) * 0.001] * width for index in range(15)],
                "outside": [[0.24 + (index % 3) * 0.001] * width for index in range(15)],
            }
            with patch.dict(
                "upper_computer.region_detection.MIN_PHASE_SAMPLES",
                {"empty": 10, "inside": 10, "outside": 10},
                clear=True,
            ):
                gw01._train_and_save()

            loaded_gw01 = TriangleRegionDetector(gw01_path, gateway_id="GW-01")
            loaded_gw02 = TriangleRegionDetector(gw02_path, gateway_id="GW-02")

        self.assertIsNotNone(gw01.profile)
        self.assertIsNotNone(loaded_gw01.profile)
        self.assertEqual(loaded_gw01.profile.gateway_id, "GW-01")
        # GW-02 检测器不得加载 GW-01 的 profile。
        self.assertIsNone(loaded_gw02.profile)

    def test_profile_paths_are_separate_per_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"APPDATA": directory}, clear=False):
                gw01 = region_module.profile_path_for_gateway("GW-01")
                gw02 = region_module.profile_path_for_gateway("GW-02")
                default = region_module.default_profile_path()

        self.assertEqual(gw01.name, "triangle_calibration_gw01.json")
        self.assertEqual(gw02.name, "triangle_calibration_gw02.json")
        self.assertEqual(gw01.parent, gw02.parent)
        self.assertEqual(default.name, "triangle_calibration_gw02.json")

    def test_gw02_default_keeps_backward_api_compatibility(self) -> None:
        self.assertEqual(region_module.EXPECTED_GATEWAY_ID, "GW-02")
        self.assertEqual(region_module.DEFAULT_GATEWAY_ID, "GW-02")
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(Path(directory) / "profile.json")
            self.assertEqual(detector.gateway_id, "GW-02")
            self.assertEqual(detector.snapshot()["gateway_id"], "GW-02")

    def test_gateway_switch_resets_detection_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(Path(directory) / "profile.json")
            detector.profile = RegionProfile(
                version=1,
                gateway_id="GW-02",
                node_macs={str(node): f"02:00:00:00:00:0{node}" for node in (1, 2, 3)},
                channel=6,
                created_at=1.0,
                feature_medians=[0.0] * (9 * 21),
                feature_scales=[1.0] * (9 * 21),
                selected_indices=[0],
                train_vectors=[[0.1], [0.11], [0.12], [0.8], [0.81], [0.82], [0.25]],
                train_labels=["empty", "empty", "outside", "inside", "inside", "inside", "outside"],
                inside_threshold=0.5,
                outlier_distance=10.0,
                metrics={},
            )
            _ingest_cycle(detector, level=204, now=10.0)
            _ingest_cycle(detector, level=204, now=10.6)
            self.assertEqual(detector.status, "occupied")
            self.assertTrue(detector.latest_frames)

            switched = detector.set_gateway("GW-01", now=20.0)

            self.assertTrue(switched)
            self.assertEqual(detector.gateway_id, "GW-01")
            self.assertEqual(detector.latest_frames, {})
            self.assertEqual(detector.node_macs, {})
            self.assertEqual(detector.inside_streak, 0)
            self.assertEqual(detector.outside_streak, 0)
            self.assertEqual(detector.outlier_streak, 0)
            self.assertEqual(detector.inside_probability, 0.0)
            self.assertEqual(detector.confidence, 0.0)
            self.assertEqual(detector.valid_links, 0)
            self.assertEqual(detector.status, "gateway_switching")
            # 切换后旧 GW-02 帧不得再进入新上下文。
            detector.ingest(_feature_frame(1, 32, gateway_id="GW-02"), now=20.1)
            self.assertEqual(detector.status, "gateway_mismatch")
            self.assertEqual(detector.latest_frames, {})

    def test_mixed_gateway_frames_never_combine_into_9_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(Path(directory) / "profile.json")
            _ingest_cycle(detector, level=32, now=100.0)
            pure = detector._assemble_vector(100.1)
            self.assertEqual(detector.valid_links, 9)

            detector.ingest(_feature_frame(1, 32, gateway_id="GW-01"), now=100.05)

            self.assertEqual(detector.status, "gateway_mismatch")
            self.assertEqual(
                {frame.get("gateway_id") for frame in detector.latest_frames.values()},
                {"GW-02"},
            )
            self.assertEqual(detector._assemble_vector(100.2), pure)
            self.assertEqual(detector.valid_links, 9)

    def test_unknown_gateway_is_rejected_without_presence_guess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(Path(directory) / "profile.json")
            detector.ingest(_feature_frame(1, 32, gateway_id="GW-03"), now=10.0)

            self.assertEqual(detector.status, "unsupported_gateway")
            self.assertEqual(detector.latest_frames, {})
            self.assertIsNone(detector._assemble_vector(10.1))
            self.assertEqual(detector.snapshot()["label"], "不支持的 Gateway")
            self.assertFalse(detector.set_gateway("GW-03"))

    def test_reset_runtime_preserves_gateway_context_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(
                Path(directory) / "profile.json", gateway_id="GW-01"
            )
            detector.profile = RegionProfile(
                version=1,
                gateway_id="GW-01",
                node_macs={str(node): f"02:00:00:00:00:0{node}" for node in (1, 2, 3)},
                channel=6,
                created_at=1.0,
                feature_medians=[0.0] * (9 * 21),
                feature_scales=[1.0] * (9 * 21),
                selected_indices=[0],
                train_vectors=[[0.1], [0.8]],
                train_labels=["empty", "inside"],
                inside_threshold=0.5,
                outlier_distance=10.0,
                metrics={},
            )
            detector.reset_runtime(status="insufficient")

        self.assertEqual(detector.gateway_id, "GW-01")
        self.assertIsNotNone(detector.profile)
        self.assertEqual(detector.status, "insufficient")
        self.assertEqual(detector.latest_frames, {})
        self.assertEqual(detector.inside_streak, 0)

    def test_unknown_gateway_ingest_clears_occupied_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            detector = TriangleRegionDetector(Path(directory) / "profile.json")
            detector.profile = RegionProfile(
                version=1,
                gateway_id="GW-02",
                node_macs={str(node): f"02:00:00:00:00:0{node}" for node in (1, 2, 3)},
                channel=6,
                created_at=1.0,
                feature_medians=[0.0] * (9 * 21),
                feature_scales=[1.0] * (9 * 21),
                selected_indices=[0],
                train_vectors=[[0.1], [0.11], [0.12], [0.8], [0.81], [0.82], [0.25]],
                train_labels=["empty", "empty", "outside", "inside", "inside", "inside", "outside"],
                inside_threshold=0.5,
                outlier_distance=10.0,
                metrics={},
            )
            _ingest_cycle(detector, level=204, now=10.0)
            _ingest_cycle(detector, level=204, now=10.6)
            self.assertEqual(detector.status, "occupied")
            self.assertGreater(detector.inside_probability, 0.0)

            detector.ingest(_feature_frame(1, 32, gateway_id="GW-03"), now=11.0)

            self.assertEqual(detector.status, "unsupported_gateway")
            self.assertEqual(detector.latest_frames, {})
            self.assertEqual(detector.node_macs, {})
            self.assertEqual(detector.inside_probability, 0.0)
            self.assertEqual(detector.confidence, 0.0)
            self.assertEqual(detector.valid_links, 0)
            self.assertEqual(detector.inside_streak, 0)
            self.assertEqual(detector.outside_streak, 0)
            self.assertEqual(detector.outlier_streak, 0)
            self.assertEqual(detector.last_transition, "")
            self.assertEqual(detector.snapshot()["label"], "不支持的 Gateway")


if __name__ == "__main__":
    unittest.main()
