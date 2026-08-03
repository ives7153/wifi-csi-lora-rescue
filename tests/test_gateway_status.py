from __future__ import annotations

import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication

import upper_computer.core.data_manager as data_manager_module
from upper_computer.core.data_manager import DataManager
from upper_computer.data_parser import parse_gateway_frame
from upper_computer.region_detection import EXPECTED_LINKS, RegionProfile


def _status_json(gateway_id: str) -> str:
    return (
        '{"type":"gateway_status","protocol":1,"firmware":"v0.5.1",'
        f'"gateway_id":"{gateway_id}","ssid":"EchoGuard-{gateway_id}","uptime_ms":1000,'
        '"rx_ok":12,"crc_errors":1,"bad_length":0,"parse_errors":0,'
        '"queue_drops":0,"queue_depth":0,"wifi_clients":2}'
    )


def _csi_json(node_id: int, gateway_id: str = "GW-02", level: int = 32) -> str:
    links = [
        '{"src":%d,"valid":true,"n":16,"rssi":-%d,"rssi_std":4,"active":20,"corr":3,'
        '"mad":[%d,%d,%d,%d,%d,%d,%d,%d],"diff":[%d,%d,%d,%d,%d,%d,%d,%d]}' % (
            source, 55 + source, level, level, level, level, level, level, level, level,
            level + 10, level + 10, level + 10, level + 10, level + 10, level + 10, level + 10, level + 10,
        )
        for source in EXPECTED_LINKS[node_id]
    ]
    return (
        f'{{"type":"csi_features","v":1,"gateway_id":"{gateway_id}","node":{node_id},'
        f'"node_mac":"02:00:00:00:00:0{node_id}","seq":7,"flags":3,'
        f'"links":[{",".join(links)}]}}'
    )


class _AdvancingClock:
    """逐步推进时钟：每批帧约推进 0.44 秒，
    既满足 1.2 秒帧跨度约束，又绕开 0.4 秒向量节流。"""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        self.now += 0.22
        return self.now


class GatewayStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_gateway_status_is_valid_without_creating_a_node(self) -> None:
        raw = (
            '{"type":"gateway_status","protocol":1,"firmware":"v0.4.0",'
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

    def test_region_detector_learns_active_gateway_from_status_frame(self) -> None:
        manager = DataManager()
        manager._handle_raw_line(_status_json("GW-01"))

        self.assertEqual(manager.region_detector.gateway_id, "GW-01")
        self.assertEqual(manager.region_detector.latest_frames, {})
        self.assertEqual(manager.region_detector.status, "gateway_switching")

    def test_region_detector_falls_back_to_csi_features_gateway(self) -> None:
        manager = DataManager()
        parsed = parse_gateway_frame(_csi_json(1, gateway_id="GW-01"))
        parsed["source"] = "serial_real"
        manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})

        self.assertEqual(manager.region_detector.gateway_id, "GW-01")
        self.assertEqual(manager.region_detector.status, "gateway_switching")

    def test_replay_csi_features_do_not_switch_region_context(self) -> None:
        manager = DataManager()
        parsed = parse_gateway_frame(_csi_json(1, gateway_id="GW-01"))
        parsed["source"] = "replay"
        manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})

        self.assertEqual(manager.region_detector.gateway_id, "GW-02")
        self.assertEqual(manager.region_detector.latest_frames, {})

    def test_unknown_gateway_status_does_not_switch_region_context(self) -> None:
        manager = DataManager()
        manager._handle_raw_line(_status_json("GW-03"))

        self.assertEqual(manager.region_detector.gateway_id, "GW-02")

    def test_gateway_switch_via_data_manager_resets_region_state(self) -> None:
        manager = DataManager()
        for node_id in (1, 2, 3):
            parsed = parse_gateway_frame(_csi_json(node_id, gateway_id="GW-02"))
            parsed["source"] = "serial_real"
            manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})
        self.assertEqual(manager.region_detector.gateway_id, "GW-02")
        self.assertTrue(manager.region_detector.latest_frames)

        manager._handle_raw_line(_status_json("GW-01"))

        self.assertEqual(manager.region_detector.gateway_id, "GW-01")
        self.assertEqual(manager.region_detector.latest_frames, {})
        self.assertEqual(manager.region_detector.valid_links, 0)
        self.assertEqual(manager.region_detector.status, "gateway_switching")
        self.assertTrue(
            any(event.title == "REGION GATEWAY SWITCHED" for event in manager.events)
        )

    def test_authoritative_status_beats_mismatching_csi(self) -> None:
        manager = DataManager()
        manager._handle_raw_line(_status_json("GW-02"))
        self.assertEqual(manager._authoritative_gateway_id, "GW-02")
        self.assertEqual(manager.region_detector.gateway_id, "GW-02")

        parsed = parse_gateway_frame(_csi_json(1, gateway_id="GW-01"))
        parsed["source"] = "serial_real"
        manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})

        self.assertEqual(manager.region_detector.gateway_id, "GW-02")
        self.assertEqual(manager.region_detector.status, "gateway_mismatch")
        self.assertEqual(manager.region_detector.latest_frames, {})

    def test_mixed_gateway_same_batch_resets_and_ingests_nothing(self) -> None:
        manager = DataManager()
        for node_id in (1, 2, 3):
            parsed = parse_gateway_frame(_csi_json(node_id, gateway_id="GW-02"))
            parsed["source"] = "serial_real"
            manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})
        self.assertTrue(manager.region_detector.latest_frames)

        frames: list[dict[str, object]] = []
        for node_id in (1, 2, 3):
            first = parse_gateway_frame(_csi_json(node_id, gateway_id="GW-01"))
            first["source"] = "serial_real"
            frames.append(first)
        for node_id in (1, 2, 3):
            second = parse_gateway_frame(_csi_json(node_id, gateway_id="GW-02"))
            second["source"] = "serial_real"
            frames.append(second)

        manager._handle_frame_batch(frames, {"total": 6, "valid": 6})

        self.assertEqual(manager.region_detector.latest_frames, {})
        self.assertEqual(manager.region_detector.valid_links, 0)
        self.assertEqual(manager.region_detector.status, "gateway_switching")
        self.assertTrue(
            any(event.title == "REGION GATEWAY MIXED" for event in manager.events)
        )

    def test_supported_and_unknown_gateway_batch_is_rejected_independent_of_order(self) -> None:
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                manager = DataManager()
                for node_id in (1, 2, 3):
                    parsed = parse_gateway_frame(_csi_json(node_id, gateway_id="GW-02"))
                    parsed["source"] = "serial_real"
                    manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})
                self.assertTrue(manager.region_detector.latest_frames)

                supported = parse_gateway_frame(_csi_json(1, gateway_id="GW-02"))
                unknown = parse_gateway_frame(_csi_json(2, gateway_id="GW-03"))
                supported["source"] = "serial_real"
                unknown["source"] = "serial_real"
                frames = [supported, unknown]
                if reverse:
                    frames.reverse()

                manager._handle_frame_batch(frames, {"total": 2, "valid": 2})

                self.assertEqual(manager.region_detector.status, "unsupported_gateway")
                self.assertEqual(manager.region_detector.latest_frames, {})
                self.assertEqual(manager.region_detector.valid_links, 0)
                self.assertTrue(
                    any(
                        event.title == "REGION GATEWAY UNSUPPORTED"
                        for event in manager.events
                    )
                )

    def test_unknown_csi_cancels_calibration_with_visible_warning(self) -> None:
        manager = DataManager()
        manager.region_detector.current_phase = "inside"
        manager.region_detector.samples["inside"] = [[0.25] * (9 * 21)]

        parsed = parse_gateway_frame(_csi_json(1, gateway_id="GW-03"))
        parsed["source"] = "serial_real"
        manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})

        self.assertEqual(manager.region_detector.status, "unsupported_gateway")
        self.assertEqual(manager.region_detector.current_phase, "")
        self.assertTrue(all(not rows for rows in manager.region_detector.samples.values()))
        self.assertTrue(
            any(event.title == "REGION GATEWAY UNSUPPORTED" for event in manager.events)
        )

    def test_unknown_gateway_status_clears_occupied_stale_state(self) -> None:
        manager = DataManager()
        manager.region_detector.profile = RegionProfile(
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
        with patch.object(data_manager_module.time, "time", _AdvancingClock()):
            for _ in range(2):
                for node_id in (1, 2, 3):
                    parsed = parse_gateway_frame(_csi_json(node_id, gateway_id="GW-02", level=204))
                    parsed["source"] = "serial_real"
                    manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})
        self.assertEqual(manager.region_detector.status, "occupied")

        manager._handle_raw_line(_status_json("GW-03"))

        self.assertEqual(manager.region_detector.status, "unsupported_gateway")
        self.assertEqual(manager.region_detector.latest_frames, {})
        self.assertEqual(manager.region_detector.inside_probability, 0.0)
        self.assertEqual(manager.region_detector.inside_streak, 0)
        self.assertIsNone(manager._authoritative_gateway_id)
        self.assertTrue(
            any(event.title == "REGION GATEWAY UNSUPPORTED" for event in manager.events)
        )

    def test_unknown_csi_frame_clears_region_runtime(self) -> None:
        manager = DataManager()
        for node_id in (1, 2, 3):
            parsed = parse_gateway_frame(_csi_json(node_id, gateway_id="GW-02"))
            parsed["source"] = "serial_real"
            manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})
        self.assertTrue(manager.region_detector.latest_frames)

        parsed = parse_gateway_frame(_csi_json(1, gateway_id="GW-03"))
        parsed["source"] = "serial_real"
        manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})

        self.assertEqual(manager.region_detector.status, "unsupported_gateway")
        self.assertEqual(manager.region_detector.latest_frames, {})

    def test_repeated_authoritative_status_recovers_after_unknown_csi(self) -> None:
        manager = DataManager()
        manager._handle_raw_line(_status_json("GW-02"))
        self.assertEqual(manager._authoritative_gateway_id, "GW-02")

        parsed = parse_gateway_frame(_csi_json(1, gateway_id="GW-03"))
        parsed["source"] = "serial_real"
        manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})
        self.assertEqual(manager.region_detector.status, "unsupported_gateway")

        manager._handle_raw_line(_status_json("GW-02"))

        self.assertEqual(manager._authoritative_gateway_id, "GW-02")
        self.assertEqual(manager.region_detector.gateway_id, "GW-02")
        self.assertEqual(manager.region_detector.status, "gateway_switching")
        self.assertEqual(manager.region_detector.latest_frames, {})

    def test_serial_stop_clears_authoritative_selection_and_runtime(self) -> None:
        manager = DataManager()
        manager._handle_raw_line(_status_json("GW-01"))
        self.assertEqual(manager._authoritative_gateway_id, "GW-01")
        for node_id in (1, 2, 3):
            parsed = parse_gateway_frame(_csi_json(node_id, gateway_id="GW-01"))
            parsed["source"] = "serial_real"
            manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})
        self.assertTrue(manager.region_detector.latest_frames)

        manager.stop_serial()

        self.assertIsNone(manager._authoritative_gateway_id)
        self.assertEqual(manager._gateway_status, {})
        self.assertEqual(manager.region_detector.latest_frames, {})
        self.assertEqual(manager.region_detector.status, "insufficient")
        self.assertEqual(manager.region_detector.inside_streak, 0)

    def test_supported_reconnect_recovers_after_serial_stop(self) -> None:
        manager = DataManager()
        manager._handle_raw_line(_status_json("GW-01"))
        manager.stop_serial()
        self.assertIsNone(manager._authoritative_gateway_id)

        manager._handle_raw_line(_status_json("GW-02"))

        self.assertEqual(manager._authoritative_gateway_id, "GW-02")
        self.assertEqual(manager.region_detector.gateway_id, "GW-02")
        for node_id in (1, 2, 3):
            parsed = parse_gateway_frame(_csi_json(node_id, gateway_id="GW-02"))
            parsed["source"] = "serial_real"
            manager._handle_frame_batch([parsed], {"total": 1, "valid": 1})
        self.assertTrue(manager.region_detector.latest_frames)


if __name__ == "__main__":
    unittest.main()
