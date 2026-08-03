from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

from upper_computer.gateway_ota import (
    ESP32S3_CHIP_ID,
    ESP_APP_DESC_MAGIC,
    ESP_APP_DESC_OFFSET,
    ESP_APP_DESC_SIZE,
    ESP_IMAGE_MAGIC,
    GatewayOTAClient,
    GatewayOTAError,
    GatewayOTAStatus,
    OTA_GATEWAY_ID,
    OTA_PROJECT_NAME,
    OTA_TARGET,
    inspect_gateway_image,
    parse_semver,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_test_image(
    path: Path,
    *,
    version: str = "0.5.0",
    project: str = OTA_PROJECT_NAME,
    chip_id: int = ESP32S3_CHIP_ID,
) -> None:
    image = bytearray(ESP_APP_DESC_OFFSET + ESP_APP_DESC_SIZE + 128)
    image[0] = ESP_IMAGE_MAGIC
    struct.pack_into("<H", image, 12, chip_id)
    descriptor = ESP_APP_DESC_OFFSET
    struct.pack_into("<I", image, descriptor, ESP_APP_DESC_MAGIC)
    image[descriptor + 16:descriptor + 16 + len(version)] = version.encode("ascii")
    image[descriptor + 48:descriptor + 48 + len(project)] = project.encode("ascii")
    idf_version = b"v5.2.6"
    image[descriptor + 112:descriptor + 112 + len(idf_version)] = idf_version
    image[-128:] = bytes(range(128))
    path.write_bytes(image)


def _status(**overrides: object) -> GatewayOTAStatus:
    values: dict[str, object] = {
        "gateway_id": OTA_GATEWAY_ID,
        "target": OTA_TARGET,
        "project": OTA_PROJECT_NAME,
        "version": "0.5.0",
        "idf_version": "v5.2.6",
        "state": "idle",
        "enabled": True,
        "pending_verify": False,
        "softap_ready": True,
        "server_ready": True,
        "lora_ready": True,
        "running_partition": "ota_1",
        "next_partition": "ota_0",
        "next_size": 0x1F0000,
        "received": 0,
        "total": 0,
        "last_error": "",
        "raw": {},
    }
    values.update(overrides)
    return GatewayOTAStatus(**values)  # type: ignore[arg-type]


class GatewayOTAImageTests(unittest.TestCase):
    def test_semver_is_strict_and_comparable(self) -> None:
        self.assertEqual(parse_semver("v0.5.0"), (0, 5, 0))
        self.assertGreater(parse_semver("0.5.1"), parse_semver("0.5.0"))
        with self.assertRaises(GatewayOTAError):
            parse_semver("0.5")

    def test_image_inspection_checks_project_chip_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "gateway.bin"
            _write_test_image(image_path)
            image = inspect_gateway_image(image_path)
            self.assertEqual(image.version, "0.5.0")
            self.assertEqual(image.project, OTA_PROJECT_NAME)
            self.assertEqual(image.idf_version, "v5.2.6")
            self.assertEqual(len(image.sha256), 64)

            _write_test_image(image_path, project="wifi_csi_lora_node")
            with self.assertRaisesRegex(GatewayOTAError, "固件目标不匹配"):
                inspect_gateway_image(image_path)

            _write_test_image(image_path, chip_id=0)
            with self.assertRaisesRegex(GatewayOTAError, "芯片目标不匹配"):
                inspect_gateway_image(image_path)


class GatewayOTAClientTests(unittest.TestCase):
    def setUp(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/api/ota/status":
                    self.send_error(404)
                    return
                server = self.server
                payload = {
                    "ok": True,
                    "enabled": True,
                    "gateway_id": OTA_GATEWAY_ID,
                    "target": OTA_TARGET,
                    "project": OTA_PROJECT_NAME,
                    "version": server.current_version,
                    "idf_version": "v5.2.6",
                    "state": "idle",
                    "pending_verify": False,
                    "softap_ready": True,
                    "server_ready": True,
                    "lora_ready": True,
                    "running_partition": "ota_0",
                    "next_partition": "ota_1",
                    "next_size": server.next_size,
                    "received": 0,
                    "total": 0,
                    "last_error": "",
                }
                self._send_json(200, payload)

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/api/ota/upload":
                    self.send_error(404)
                    return
                length = int(self.headers["Content-Length"])
                self.server.upload_body = self.rfile.read(length)
                self.server.upload_headers = dict(self.headers.items())
                required_token = self.server.required_token
                if required_token is not None and self.headers.get(
                    "X-EchoGuard-OTA-Token"
                ) != required_token:
                    self._send_json(
                        401,
                        {
                            "ok": False,
                            "error": "unauthorized",
                            "message": "invalid OTA token",
                        },
                    )
                    return
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "state": "rebooting",
                        "version": self.headers["X-EchoGuard-Version"],
                        "partition": "ota_1",
                        "bytes": length,
                    },
                )

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _send_json(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.current_version = "0.4.0"  # type: ignore[attr-defined]
        self.server.next_size = 0x1F0000  # type: ignore[attr-defined]
        self.server.required_token = None  # type: ignore[attr-defined]
        self.server.upload_body = b""  # type: ignore[attr-defined]
        self.server.upload_headers = {}  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_status_and_streaming_upload_use_required_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "gateway.bin"
            _write_test_image(image_path)
            image = inspect_gateway_image(image_path)
            client = GatewayOTAClient(f"127.0.0.1:{self.server.server_port}")
            progress: list[tuple[int, int]] = []
            response = client.upload(
                image,
                "0123456789abcdef",
                progress=lambda sent, total: progress.append((sent, total)),
            )
            expected_body = image_path.read_bytes()

        self.assertEqual(response["version"], "0.5.0")
        self.assertEqual(self.server.upload_body, expected_body)
        headers = self.server.upload_headers
        self.assertEqual(headers["X-EchoGuard-Target"], OTA_TARGET)
        self.assertEqual(headers["X-EchoGuard-SHA256"], image.sha256)
        self.assertEqual(headers["X-EchoGuard-OTA-Token"], "0123456789abcdef")
        self.assertEqual(progress[0], (0, image.size))
        self.assertEqual(progress[-1], (image.size, image.size))

    def test_wait_for_version_requires_boot_health_confirmation(self) -> None:
        class SequencedClient(GatewayOTAClient):
            def __init__(self) -> None:
                super().__init__("127.0.0.1")
                self.statuses = [
                    _status(state="pending_health", pending_verify=True),
                    _status(),
                ]

            def get_status(self) -> GatewayOTAStatus:
                return self.statuses.pop(0)

        status = SequencedClient().wait_for_version(
            "0.5.0", timeout=1.0, poll_interval=0.01
        )
        self.assertEqual(status.state, "idle")
        self.assertFalse(status.pending_verify)

    def test_server_permission_error_is_reported(self) -> None:
        self.server.required_token = "correct-token-0123456789"  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "gateway.bin"
            _write_test_image(image_path)
            image = inspect_gateway_image(image_path)
            client = GatewayOTAClient(f"127.0.0.1:{self.server.server_port}")
            with self.assertRaisesRegex(GatewayOTAError, "HTTP 401.*invalid OTA token"):
                client.upload(image, "incorrect-token-012345")

    def test_preflight_rejects_non_newer_and_oversized_images(self) -> None:
        self.server.current_version = "0.5.0"  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "gateway.bin"
            _write_test_image(image_path, version="0.5.0")
            current_image = inspect_gateway_image(image_path)
            client = GatewayOTAClient(f"127.0.0.1:{self.server.server_port}")
            with self.assertRaisesRegex(GatewayOTAError, "必须高于当前版本"):
                client.upload(current_image, "0123456789abcdef")

            _write_test_image(image_path, version="0.5.1")
            next_image = inspect_gateway_image(image_path)
            self.server.next_size = next_image.size - 1  # type: ignore[attr-defined]
            with self.assertRaisesRegex(GatewayOTAError, "超过备用分区容量"):
                client.upload(next_image, "0123456789abcdef")

        self.assertEqual(self.server.upload_body, b"")

    def test_node_target_status_is_rejected(self) -> None:
        self.server.current_version = "0.4.0"  # type: ignore[attr-defined]
        original = self.server.RequestHandlerClass.do_GET

        def wrong_target(handler: BaseHTTPRequestHandler) -> None:
            body = json.dumps(
                {
                    "ok": True,
                    "enabled": True,
                    "gateway_id": "NODE-01",
                    "target": "node:1",
                    "project": "wifi_csi_lora_node",
                    "version": "0.4.0",
                }
            ).encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        self.server.RequestHandlerClass.do_GET = wrong_target
        try:
            client = GatewayOTAClient(f"127.0.0.1:{self.server.server_port}")
            with self.assertRaisesRegex(GatewayOTAError, "目标不是 Gateway 01"):
                client.get_status()
        finally:
            self.server.RequestHandlerClass.do_GET = original


class GatewayOTAUISmokeTests(unittest.TestCase):
    def test_dialog_constructs_offscreen(self) -> None:
        environment = dict(os.environ)
        environment["QT_QPA_PLATFORM"] = "offscreen"
        command = (
            "from PyQt6.QtWidgets import QApplication; "
            "from upper_computer.ui.gateway_ota_dialog import GatewayOTADialog; "
            "app=QApplication([]); dialog=GatewayOTADialog(); "
            "assert dialog.windowTitle() == 'Gateway 01 局域网 OTA'; dialog.close()"
        )
        completed = subprocess.run(
            [sys.executable, "-c", command],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
