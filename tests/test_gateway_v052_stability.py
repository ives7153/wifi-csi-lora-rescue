from __future__ import annotations

import re
import unittest
from pathlib import Path

from upper_computer.version import DISPLAY_VERSION


ROOT = Path(__file__).resolve().parents[1]


def _extract_function_body(source: str, signature: str) -> str:
    """Return the balanced-brace body that follows the ``signature`` definition."""
    match = re.search(re.escape(signature) + r"\s*\{", source)
    if match is None:
        raise AssertionError(f"definition not found for signature {signature!r}")
    open_idx = match.end() - 1
    depth = 0
    for i in range(open_idx, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[open_idx : i + 1]
    raise AssertionError(f"unbalanced braces after signature {signature!r}")


class GatewayV052StabilityTests(unittest.TestCase):
    """Static/consistency regression coverage for the approved GW v0.5.2 fix.

    These tests assert only stable protocol/version/build invariants and never
    assert or expose local credentials, SSIDs, passwords, or OTA tokens.
    """

    def _read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_gateway_version_bumped_to_0_5_2_only(self) -> None:
        gateway_cmake = self._read("firmware/gateway/CMakeLists.txt")
        gateway_main = self._read("firmware/gateway/main/main.c")

        project_ver = re.search(r'set\(PROJECT_VER\s+"([^"]+)"\)', gateway_cmake)
        self.assertIsNotNone(project_ver, "Gateway CMake PROJECT_VER missing")
        self.assertEqual(project_ver.group(1), "0.5.2")

        runtime_ver = re.search(
            r'#define\s+GATEWAY_FIRMWARE_VERSION\s+"v([^"]+)"', gateway_main
        )
        self.assertIsNotNone(runtime_ver, "Gateway GATEWAY_FIRMWARE_VERSION missing")
        self.assertEqual(runtime_ver.group(1), "0.5.2")
        self.assertEqual(
            runtime_ver.group(1),
            project_ver.group(1),
            "Gateway CMake PROJECT_VER and runtime version disagree",
        )

    def test_node_v053_and_upper_computer_versions(self) -> None:
        node_cmake = self._read("firmware/node/CMakeLists.txt")
        node_main = self._read("firmware/node/main/main.c")
        node_project = re.search(r'set\(PROJECT_VER\s+"([^"]+)"\)', node_cmake)
        self.assertIsNotNone(node_project)
        self.assertEqual(node_project.group(1), "0.5.3")
        node_runtime = re.search(
            r'#define\s+NODE_FIRMWARE_VERSION\s+"v([^"]+)"', node_main
        )
        self.assertIsNotNone(node_runtime)
        self.assertEqual(node_runtime.group(1), "0.5.3")
        self.assertEqual(DISPLAY_VERSION, "v0.5.1")

    def test_gateway_event_task_stack_size_is_4096(self) -> None:
        defaults = self._read("firmware/gateway/sdkconfig.defaults")
        self.assertIn("CONFIG_ESP_SYSTEM_EVENT_TASK_STACK_SIZE=4096", defaults)
        self.assertNotIn("CONFIG_ESP_SYSTEM_EVENT_TASK_STACK_SIZE=2304", defaults)

    def test_keepalive_uses_single_softap_subnet_broadcast(self) -> None:
        main_c = self._read("firmware/gateway/main/main.c")
        self.assertIn("#define UDP_KEEPALIVE_INTERVAL_MS 20", main_c)
        self.assertIn("SO_BROADCAST", main_c)
        self.assertIn("INADDR_BROADCAST", main_c)
        self.assertIn("esp_netif_get_ip_info", main_c)
        self.assertIn('"EGSYNC:%" PRIu32 ":%u"', main_c)

    def test_keepalive_task_sends_exactly_one_broadcast_per_cycle(self) -> None:
        main_c = self._read("firmware/gateway/main/main.c")
        body = _extract_function_body(main_c, "static void udp_keepalive_task(void *arg)")
        self.assertEqual(
            body.count("sendto("),
            1,
            "udp_keepalive_task must send exactly one broadcast per cycle",
        )
        self.assertIn("(struct sockaddr *)&broadcast_addr", body)
        self.assertIn("UDP_KEEPALIVE_INTERVAL_MS", body)

    def test_keepalive_task_has_no_client_or_station_list_gating(self) -> None:
        main_c = self._read("firmware/gateway/main/main.c")
        body = _extract_function_body(main_c, "static void udp_keepalive_task(void *arg)")
        for token in (
            "esp_wifi_ap_get_sta_list",
            "wifi_sta_list",
            "esp_netif_dhcps_get_clients_by_mac",
            "mac_ip_pairs",
            "pair_count",
        ):
            self.assertNotIn(token, body, f"high-frequency keepalive path must not use {token}")
        self.assertNotIn("for (int i = 0; i < pair_count", body)

    def test_status_function_still_reports_wifi_clients(self) -> None:
        main_c = self._read("firmware/gateway/main/main.c")
        status_body = _extract_function_body(main_c, "static void gateway_print_status(void)")
        self.assertIn("esp_wifi_ap_get_sta_list", status_body)
        self.assertIn('\\"wifi_clients\\":%', status_body)

    def test_egsync_egcf_formats_and_region_protocol_unchanged(self) -> None:
        gateway_main = self._read("firmware/gateway/main/main.c")
        node_region_csi = self._read(
            "firmware/node/components/region_csi/echoguard_region_csi.c"
        )
        region_header = self._read(
            "firmware/components/echoguard_region/include/echoguard_region_protocol.h"
        )
        region_source = self._read(
            "firmware/components/echoguard_region/echoguard_region_protocol.c"
        )

        self.assertIn('"EGSYNC:%" PRIu32 ":%u"', gateway_main)
        self.assertIn('sscanf(text, "EGSYNC:%" SCNu32 ":%u"', node_region_csi)
        self.assertIn("ECHOGUARD_REGION_PROTOCOL_VERSION 1U", region_header)
        self.assertIn("static const uint8_t REGION_MAGIC[4] = {'E', 'G', 'C', 'F'};", region_source)
        self.assertIn('\\"type\\":\\"csi_features\\",\\"v\\":%u', gateway_main)

    def test_gateway_status_has_additive_sync_counters(self) -> None:
        main_c = self._read("firmware/gateway/main/main.c")
        for counter in ("s_egsync_sent", "s_egsync_ok", "s_egsync_errors"):
            self.assertIn(f"static volatile uint32_t {counter};", main_c)
        self.assertIn('\\"egsync_sent\\":%', main_c)
        self.assertIn('\\"egsync_ok\\":%', main_c)
        self.assertIn('\\"egsync_errors\\":%', main_c)
        self.assertIn("++s_egsync_sent", main_c)
        self.assertIn("++s_egsync_ok", main_c)
        self.assertIn("++s_egsync_errors", main_c)

    def test_gateway_status_existing_fields_are_preserved(self) -> None:
        main_c = self._read("firmware/gateway/main/main.c")
        for field in (
            '\\"rx_ok\\":%',
            '\\"crc_errors\\":%',
            '\\"bad_length\\":%',
            '\\"parse_errors\\":%',
            '\\"queue_drops\\":%',
            '\\"queue_depth\\":%',
            '\\"wifi_clients\\":%',
            '\\"region_protocol\\":%',
            '\\"region_rx_ok\\":%',
            '\\"region_invalid\\":%',
            '\\"region_queue_drops\\":%',
        ):
            self.assertIn(field, main_c)


if __name__ == "__main__":
    unittest.main()
