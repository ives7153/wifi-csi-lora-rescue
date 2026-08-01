from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from upper_computer.core.data_manager import DataManager
from upper_computer.ui.main_window import MainWindow


REPO_ROOT = Path(__file__).resolve().parents[1]


class _BannerProbe:
    def __init__(self) -> None:
        self.hidden = False
        self.shown = False
        self.text = ""

    def hide(self) -> None:
        self.hidden = True

    def show(self) -> None:
        self.shown = True

    def setText(self, text: str) -> None:  # noqa: N802 - Qt-compatible probe
        self.text = text

    def setStyleSheet(self, _style: str) -> None:  # noqa: N802 - Qt-compatible probe
        return


def _serial_sample(*, seq: int, presence: float = 0.92) -> dict[str, object]:
    return {
        "timestamp": 1_700_000_000.0 + seq,
        "node_id": 1,
        "node_label": "node1",
        "seq": seq,
        "presence_score": presence,
        "motion_score": 0.24,
        "confidence": 0.88,
        "source": "serial_real",
    }


class StartupPresenceDemoTests(unittest.TestCase):
    def test_release_entries_enable_only_the_standard_edition(self) -> None:
        standard_entry = (REPO_ROOT / "upper_computer" / "main.py").read_text(encoding="utf-8")
        mobile_entry = (REPO_ROOT / "upper_computer" / "mobile_control_main.py").read_text(encoding="utf-8")
        self.assertIn("DataManager(startup_presence_demo_seconds=20.0)", standard_entry)
        self.assertIn("DataManager(startup_presence_demo_seconds=0.0)", mobile_entry)

    def test_standard_edition_masks_first_twenty_seconds_then_restores_real_value(self) -> None:
        manager = DataManager(startup_presence_demo_seconds=20.0)
        with patch("upper_computer.core.data_manager.time.monotonic", side_effect=[100.0, 101.0, 120.0]):
            first = manager._apply_sample(_serial_sample(seq=1), run_rules=False)
            second = manager._apply_sample(_serial_sample(seq=2), run_rules=False)
            restored = manager._apply_sample(_serial_sample(seq=3), run_rules=False)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNotNone(restored)
        assert first is not None and second is not None and restored is not None
        self.assertGreaterEqual(first["presence_score"], 0.01)
        self.assertLess(first["presence_score"], 0.1)
        self.assertLess(second["presence_score"], 0.1)
        self.assertNotEqual(first["presence_score"], second["presence_score"])
        self.assertEqual(first["source"], "demo_mode")
        self.assertEqual(second["source"], "demo_mode")
        self.assertEqual(restored["presence_score"], 0.92)
        self.assertEqual(restored["source"], "serial_real")
        self.assertEqual(restored["raw_presence_score"], 0.92)

    def test_default_and_mobile_managers_do_not_apply_startup_mask(self) -> None:
        default_manager = DataManager()
        default = default_manager._apply_sample(_serial_sample(seq=1), run_rules=False)
        self.assertIsNotNone(default)
        assert default is not None
        self.assertEqual(default["presence_score"], 0.92)
        self.assertEqual(default["source"], "serial_real")

        mobile_manager = DataManager(startup_presence_demo_seconds=0.0)
        mobile_manager.set_mobile_presence(1, 0.67)
        mobile = mobile_manager._apply_sample(_serial_sample(seq=2), run_rules=False)
        self.assertIsNotNone(mobile)
        assert mobile is not None
        self.assertEqual(mobile["presence_score"], 0.67)
        self.assertEqual(mobile["source"], "mobile_override")

    def test_reconnected_node_starts_a_new_twenty_second_window(self) -> None:
        manager = DataManager(startup_presence_demo_seconds=20.0)
        with patch("upper_computer.core.data_manager.time.monotonic", side_effect=[10.0, 35.0]):
            first = manager._apply_sample(_serial_sample(seq=1), run_rules=False)
            manager.nodes[1].online = False
            reconnected = manager._apply_sample(_serial_sample(seq=10), run_rules=False)

        self.assertIsNotNone(first)
        self.assertIsNotNone(reconnected)
        assert reconnected is not None
        self.assertLess(reconnected["presence_score"], 0.1)
        self.assertEqual(reconnected["source"], "demo_mode")

    def test_startup_demo_does_not_show_a_banner_or_countdown(self) -> None:
        probe = _BannerProbe()
        window = type("WindowProbe", (), {"demo_banner": probe})()
        snapshot = {
            "config": {"startup_presence_demo_active": True},
            "nodes": {1: {"online": True, "source": "demo_mode"}},
        }
        MainWindow._update_data_source_banner(window, snapshot)
        self.assertTrue(probe.hidden)
        self.assertFalse(probe.shown)
        self.assertEqual(probe.text, "")


if __name__ == "__main__":
    unittest.main()
