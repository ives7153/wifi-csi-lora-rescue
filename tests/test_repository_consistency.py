from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

from upper_computer.version import DISPLAY_VERSION, MOBILE_DIST_NAME, STANDARD_DIST_NAME


ROOT = Path(__file__).resolve().parents[1]


class RepositoryConsistencyTests(unittest.TestCase):
    def _tracked_files(self) -> list[Path]:
        output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        return [ROOT / item.decode("utf-8") for item in output.split(b"\0") if item]

    def test_release_names_share_one_version(self) -> None:
        self.assertEqual(DISPLAY_VERSION, "v0.5.1")
        self.assertEqual(STANDARD_DIST_NAME, "EchoGuard-v0.5.1-windows")
        self.assertEqual(MOBILE_DIST_NAME, "EchoGuard-Mobile-v0.5.1-windows")

    def test_tracked_text_no_longer_mentions_sht20(self) -> None:
        offenders: list[str] = []
        for path in self._tracked_files():
            if path.suffix.lower() not in {".md", ".py", ".c", ".h", ".txt", ".yml", ".yaml"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if ("SHT" + "20") in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_local_artifacts_are_not_tracked(self) -> None:
        forbidden: list[str] = []
        for path in self._tracked_files():
            relative = path.relative_to(ROOT).as_posix()
            parts = relative.split("/")
            if any(part in {"build", "dist", "releases", "models", "runtime", "exports"} for part in parts):
                forbidden.append(relative)
            if path.name == "sdkconfig":
                forbidden.append(relative)
        self.assertEqual(forbidden, [])

    def test_gateway_console_supports_usb_uart_and_native_usb(self) -> None:
        defaults = (ROOT / "firmware" / "gateway" / "sdkconfig.defaults").read_text(
            encoding="utf-8"
        )
        self.assertIn("CONFIG_ESP_CONSOLE_UART_DEFAULT=y", defaults)
        self.assertIn("CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG=y", defaults)
        self.assertIn("CONFIG_ESP_CONSOLE_UART_BAUDRATE=115200", defaults)

    def test_gateway_ota_is_isolated_from_nodes_and_default_partition_table(self) -> None:
        ota_partitions = (ROOT / "partitions-ota-8Mib.csv").read_text(encoding="utf-8")
        self.assertIn("otadata,data,ota,0xF000,0x2000", ota_partitions)
        self.assertIn("ota_0,app,ota_0,0x20000,0x1F0000", ota_partitions)
        self.assertIn("ota_1,app,ota_1,0x210000,0x1F0000", ota_partitions)

        default_partitions = (ROOT / "partitions-8Mib.csv").read_text(encoding="utf-8")
        self.assertIn("factory,app,factory", default_partitions)
        self.assertNotIn("ota_0,app", default_partitions)

        node_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self._tracked_files()
            if path.is_relative_to(ROOT / "firmware" / "node")
            and path.suffix in {".c", ".h", ".txt"}
        )
        self.assertNotIn("echoguard_ota", node_sources)

    def test_node_project_and_startup_versions_agree_at_0_5_2(self) -> None:
        cmake = (ROOT / "firmware" / "node" / "CMakeLists.txt").read_text(encoding="utf-8")
        project_ver = re.search(r'set\(PROJECT_VER\s+"([^"]+)"\)', cmake)
        self.assertIsNotNone(project_ver, "PROJECT_VER missing in firmware/node/CMakeLists.txt")
        self.assertEqual(project_ver.group(1), "0.5.2")

        main_c = (ROOT / "firmware" / "node" / "main" / "main.c").read_text(encoding="utf-8")
        startup_ver = re.search(r'#define\s+NODE_FIRMWARE_VERSION\s+"v([^"]+)"', main_c)
        self.assertIsNotNone(startup_ver, "NODE_FIRMWARE_VERSION missing in firmware/node/main/main.c")
        self.assertEqual(startup_ver.group(1), "0.5.2")
        self.assertEqual(
            startup_ver.group(1),
            project_ver.group(1),
            "Node CMake PROJECT_VER and runtime NODE_FIRMWARE_VERSION disagree",
        )
        self.assertIn(
            'EchoGuard Node %s WiFi multi-link CSI + Sensor Fusion starting",\n'
            "             NODE_FIRMWARE_VERSION);",
            main_c,
        )
        self.assertEqual(
            main_c.count("v0.5.2"),
            1,
            "Node startup version must be reported via NODE_FIRMWARE_VERSION, not hard-coded",
        )


if __name__ == "__main__":
    unittest.main()
