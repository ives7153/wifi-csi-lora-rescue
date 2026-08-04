from __future__ import annotations

import re
import unittest
from pathlib import Path

from upper_computer.version import DISPLAY_VERSION


ROOT = Path(__file__).resolve().parents[1]


class NodeV053RegionUploadStackTests(unittest.TestCase):
    """Static/consistency regression coverage for the approved Node v0.5.3 fix.

    These tests assert only stable version/protocol/stack invariants and never
    assert or expose local credentials, SSIDs, passwords, or OTA tokens.
    """

    def _read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_node_version_bumped_to_0_5_3_only(self) -> None:
        node_cmake = self._read("firmware/node/CMakeLists.txt")
        node_main = self._read("firmware/node/main/main.c")

        project_ver = re.search(r'set\(PROJECT_VER\s+"([^"]+)"\)', node_cmake)
        self.assertIsNotNone(project_ver, "Node CMake PROJECT_VER missing")
        self.assertEqual(project_ver.group(1), "0.5.3")

        runtime_ver = re.search(
            r'#define\s+NODE_FIRMWARE_VERSION\s+"v([^"]+)"', node_main
        )
        self.assertIsNotNone(runtime_ver, "Node NODE_FIRMWARE_VERSION missing")
        self.assertEqual(runtime_ver.group(1), "0.5.3")
        self.assertEqual(
            runtime_ver.group(1),
            project_ver.group(1),
            "Node CMake PROJECT_VER and runtime version disagree",
        )
        self.assertEqual(
            node_main.count("v0.5.3"),
            1,
            "startup version must not be hard-coded again",
        )

    def test_gateway_and_upper_computer_versions_unchanged(self) -> None:
        gateway_cmake = self._read("firmware/gateway/CMakeLists.txt")
        gateway_main = self._read("firmware/gateway/main/main.c")

        gateway_ver = re.search(r'set\(PROJECT_VER\s+"([^"]+)"\)', gateway_cmake)
        self.assertIsNotNone(gateway_ver)
        self.assertEqual(gateway_ver.group(1), "0.5.2")

        gateway_runtime = re.search(
            r'#define\s+GATEWAY_FIRMWARE_VERSION\s+"v([^"]+)"', gateway_main
        )
        self.assertIsNotNone(gateway_runtime)
        self.assertEqual(gateway_runtime.group(1), "0.5.2")
        self.assertEqual(DISPLAY_VERSION, "v0.5.1")

    def test_region_upload_task_stack_is_8192_no_4096_remains(self) -> None:
        node_main = self._read("firmware/node/main/main.c")

        stack_define = re.search(
            r"#define\s+REGION_UPLOAD_TASK_STACK_SIZE\s+(\d+)", node_main
        )
        self.assertIsNotNone(stack_define, "REGION_UPLOAD_TASK_STACK_SIZE missing")
        self.assertEqual(stack_define.group(1), "8192")
        self.assertNotIn(
            "#define REGION_UPLOAD_TASK_STACK_SIZE   4096", node_main
        )
        self.assertIn(
            "REGION_UPLOAD_TASK_STACK_SIZE, NULL,",
            node_main,
            "region upload task must use the stack size constant",
        )

    def test_region_upload_cadence_and_ports_unchanged(self) -> None:
        node_main = self._read("firmware/node/main/main.c")
        region_header = self._read(
            "firmware/components/echoguard_region/include/echoguard_region_protocol.h"
        )

        self.assertIn("#define UDP_KEEPALIVE_PORT              33333", node_main)
        self.assertIn("ECHOGUARD_REGION_FEATURE_PORT 33334U", region_header)
        self.assertIn("ECHOGUARD_REGION_PROTOCOL_VERSION 1U", region_header)
        self.assertGreaterEqual(
            node_main.count("vTaskDelay(pdMS_TO_TICKS(500))"),
            2,
            "region upload cadence must stay 500 ms",
        )

    def test_egsync_cadence_and_formats_unchanged(self) -> None:
        gateway_main = self._read("firmware/gateway/main/main.c")
        node_region_csi = self._read(
            "firmware/node/components/region_csi/echoguard_region_csi.c"
        )
        region_source = self._read(
            "firmware/components/echoguard_region/echoguard_region_protocol.c"
        )

        self.assertIn("#define UDP_KEEPALIVE_INTERVAL_MS 20", gateway_main)
        self.assertIn('"EGSYNC:%" PRIu32 ":%u"', gateway_main)
        self.assertIn('sscanf(text, "EGSYNC:%" SCNu32 ":%u"', node_region_csi)
        self.assertIn(
            "static const uint8_t REGION_MAGIC[4] = {'E', 'G', 'C', 'F'};",
            region_source,
        )

    def test_dual_gateway_any_active_upload_unchanged(self) -> None:
        node_main = self._read("firmware/node/main/main.c")
        selector = self._read(
            "firmware/node/components/wifi_manager/wifi_gateway_selector.c"
        )

        self.assertIn(
            "echoguard_region_csi_set_gateway(connected->bssid, true);", node_main
        )
        self.assertIn("return ECHOGUARD_GATEWAY_PRIMARY;", selector)
        self.assertIn("return ECHOGUARD_GATEWAY_SECONDARY;", selector)
        self.assertIn("candidates[ECHOGUARD_GATEWAY_PRIMARY].visible", selector)
        self.assertIn("candidates[ECHOGUARD_GATEWAY_SECONDARY].visible", selector)


if __name__ == "__main__":
    unittest.main()
