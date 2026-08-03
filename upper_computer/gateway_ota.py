"""Gateway 01 LAN OTA image inspection and HTTP client."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
from pathlib import Path
import re
import struct
import time
from typing import Any, Callable
from urllib.parse import SplitResult, urlsplit


OTA_PROJECT_NAME = "wifi_csi_lora_gateway"
OTA_GATEWAY_ID = "GW-01"
OTA_TARGET = f"gateway:{OTA_GATEWAY_ID}"
ESP_IMAGE_MAGIC = 0xE9
ESP_APP_DESC_MAGIC = 0xABCD5432
ESP32S3_CHIP_ID = 9
ESP_IMAGE_HEADER_SIZE = 24
ESP_IMAGE_SEGMENT_HEADER_SIZE = 8
ESP_APP_DESC_OFFSET = ESP_IMAGE_HEADER_SIZE + ESP_IMAGE_SEGMENT_HEADER_SIZE
ESP_APP_DESC_SIZE = 256
OTA_CHUNK_SIZE = 64 * 1024
_SEMVER_PATTERN = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)$")


class GatewayOTAError(RuntimeError):
    """Raised for a local image error or a rejected/unreachable Gateway OTA API."""


@dataclass(frozen=True)
class GatewayFirmwareInfo:
    path: Path
    size: int
    sha256: str
    project: str
    version: str
    idf_version: str
    chip_id: int


@dataclass(frozen=True)
class GatewayOTAStatus:
    gateway_id: str
    target: str
    project: str
    version: str
    idf_version: str
    state: str
    enabled: bool
    pending_verify: bool
    softap_ready: bool
    server_ready: bool
    lora_ready: bool
    running_partition: str
    next_partition: str
    next_size: int
    received: int
    total: int
    last_error: str
    raw: dict[str, Any]


ProgressCallback = Callable[[int, int], None]
StatusCallback = Callable[[str], None]


def parse_semver(value: str) -> tuple[int, int, int]:
    """Parse the strict release format accepted by Gateway firmware."""

    match = _SEMVER_PATTERN.fullmatch(str(value).strip())
    if match is None:
        raise GatewayOTAError(f"无效固件版本：{value!r}，需要 X.Y.Z 格式")
    return tuple(int(part) for part in match.groups())


def inspect_gateway_image(path: str | Path) -> GatewayFirmwareInfo:
    """Read ESP-IDF app metadata and SHA-256 without loading the image into RAM."""

    image_path = Path(path).expanduser().resolve()
    if not image_path.is_file():
        raise GatewayOTAError(f"固件文件不存在：{image_path}")
    size = image_path.stat().st_size
    minimum_size = ESP_APP_DESC_OFFSET + ESP_APP_DESC_SIZE
    if size < minimum_size:
        raise GatewayOTAError("固件文件过小，不是有效的 ESP32-S3 应用镜像")

    with image_path.open("rb") as stream:
        prefix = stream.read(minimum_size)
    if prefix[0] != ESP_IMAGE_MAGIC:
        raise GatewayOTAError("固件镜像头无效，不是 ESP-IDF 应用镜像")
    chip_id = struct.unpack_from("<H", prefix, 12)[0]
    if chip_id != ESP32S3_CHIP_ID:
        raise GatewayOTAError(
            f"芯片目标不匹配：镜像 chip_id={chip_id}，Gateway 01 需要 ESP32-S3"
        )

    descriptor = prefix[ESP_APP_DESC_OFFSET:minimum_size]
    descriptor_magic = struct.unpack_from("<I", descriptor, 0)[0]
    if descriptor_magic != ESP_APP_DESC_MAGIC:
        raise GatewayOTAError("固件应用描述无效")
    version = _read_c_string(descriptor[16:48])
    project = _read_c_string(descriptor[48:80])
    idf_version = _read_c_string(descriptor[112:144])
    if project != OTA_PROJECT_NAME:
        raise GatewayOTAError(
            f"固件目标不匹配：{project or '未知工程'} 不是 EchoGuard Gateway"
        )
    parse_semver(version)

    digest = hashlib.sha256()
    with image_path.open("rb") as stream:
        while chunk := stream.read(OTA_CHUNK_SIZE):
            digest.update(chunk)
    return GatewayFirmwareInfo(
        path=image_path,
        size=size,
        sha256=digest.hexdigest(),
        project=project,
        version=version,
        idf_version=idf_version,
        chip_id=chip_id,
    )


def _read_c_string(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="strict")


class GatewayOTAClient:
    """Small synchronous client intended to run inside a UI worker thread."""

    def __init__(self, gateway_address: str = "192.168.4.1", timeout: float = 15.0) -> None:
        self._url = _normalize_gateway_url(gateway_address)
        self.timeout = max(1.0, float(timeout))

    @property
    def base_url(self) -> str:
        netloc = self._url.hostname or ""
        if self._url.port is not None and self._url.port != 80:
            netloc = f"{netloc}:{self._url.port}"
        return f"http://{netloc}{self._url.path.rstrip('/')}"

    def get_status(self) -> GatewayOTAStatus:
        payload = self._request_json("GET", "/api/ota/status")
        status = GatewayOTAStatus(
            gateway_id=str(payload.get("gateway_id") or ""),
            target=str(payload.get("target") or ""),
            project=str(payload.get("project") or ""),
            version=str(payload.get("version") or ""),
            idf_version=str(payload.get("idf_version") or ""),
            state=str(payload.get("state") or "unknown"),
            enabled=bool(payload.get("enabled")),
            pending_verify=bool(payload.get("pending_verify")),
            softap_ready=bool(payload.get("softap_ready")),
            server_ready=bool(payload.get("server_ready")),
            lora_ready=bool(payload.get("lora_ready")),
            running_partition=str(payload.get("running_partition") or ""),
            next_partition=str(payload.get("next_partition") or ""),
            next_size=int(payload.get("next_size") or 0),
            received=int(payload.get("received") or 0),
            total=int(payload.get("total") or 0),
            last_error=str(payload.get("last_error") or ""),
            raw=payload,
        )
        if status.gateway_id != OTA_GATEWAY_ID or status.target != OTA_TARGET:
            raise GatewayOTAError(
                f"目标不是 Gateway 01：gateway_id={status.gateway_id or '未知'}"
            )
        if status.project != OTA_PROJECT_NAME:
            raise GatewayOTAError("Gateway OTA 状态中的工程标识不匹配")
        parse_semver(status.version)
        return status

    def upload(
        self,
        image: GatewayFirmwareInfo,
        token: str,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        token = str(token).strip()
        if len(token) < 16:
            raise GatewayOTAError("OTA 令牌至少需要 16 个字符")
        status = self.get_status()
        if not status.enabled or not status.server_ready:
            raise GatewayOTAError("Gateway 01 OTA 服务尚未就绪")
        if status.pending_verify:
            raise GatewayOTAError("Gateway 当前固件尚未完成启动健康确认")
        if parse_semver(image.version) <= parse_semver(status.version):
            raise GatewayOTAError(
                f"固件版本 {image.version} 必须高于当前版本 {status.version}"
            )
        if image.size > status.next_size:
            raise GatewayOTAError(
                f"固件大小 {image.size} 超过备用分区容量 {status.next_size}"
            )

        connection = self._new_connection(timeout=max(self.timeout, 30.0))
        request_path = self._api_path("/api/ota/upload")
        try:
            connection.putrequest("POST", request_path)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(image.size))
            connection.putheader("X-EchoGuard-OTA-Token", token)
            connection.putheader("X-EchoGuard-SHA256", image.sha256)
            connection.putheader("X-EchoGuard-Version", image.version)
            connection.putheader("X-EchoGuard-Target", OTA_TARGET)
            connection.endheaders()
            sent = 0
            if progress is not None:
                progress(sent, image.size)
            with image.path.open("rb") as stream:
                while chunk := stream.read(OTA_CHUNK_SIZE):
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress is not None:
                        progress(sent, image.size)
            response = connection.getresponse()
            payload = _decode_json_response(response.status, response.read(65537))
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise GatewayOTAError(f"OTA 上传连接失败：{exc}") from exc
        finally:
            connection.close()
        if str(payload.get("version") or "") != image.version:
            raise GatewayOTAError("Gateway 返回的待启动版本与上传镜像不一致")
        return payload

    def upload_and_verify(
        self,
        image: GatewayFirmwareInfo,
        token: str,
        progress: ProgressCallback | None = None,
        status_callback: StatusCallback | None = None,
        reboot_timeout: float = 90.0,
    ) -> GatewayOTAStatus:
        if status_callback is not None:
            status_callback("正在校验 Gateway 01 与固件版本")
        self.upload(image, token, progress)
        if status_callback is not None:
            status_callback("固件已接收，等待 Gateway 重启和健康确认")
        return self.wait_for_version(
            image.version,
            timeout=reboot_timeout,
            status_callback=status_callback,
        )

    def wait_for_version(
        self,
        expected_version: str,
        timeout: float = 90.0,
        poll_interval: float = 2.0,
        status_callback: StatusCallback | None = None,
    ) -> GatewayOTAStatus:
        deadline = time.monotonic() + max(1.0, float(timeout))
        last_error = "Gateway 尚未重新上线"
        while time.monotonic() < deadline:
            time.sleep(max(0.1, float(poll_interval)))
            try:
                status = self.get_status()
            except GatewayOTAError as exc:
                last_error = str(exc)
                continue
            if status.version != expected_version:
                last_error = f"Gateway 当前版本仍为 {status.version}"
                continue
            if status.pending_verify or status.state != "idle":
                last_error = "新固件正在等待 SoftAP、OTA 服务与 LoRa 健康确认"
                if status_callback is not None:
                    status_callback(last_error)
                continue
            if not (status.softap_ready and status.server_ready and status.lora_ready):
                last_error = "新固件健康条件未全部就绪"
                continue
            if status_callback is not None:
                status_callback(f"Gateway 01 已运行 {status.version}，启动健康确认通过")
            return status
        raise GatewayOTAError(f"Gateway 重启验证超时：{last_error}")

    def _request_json(self, method: str, endpoint: str) -> dict[str, Any]:
        connection = self._new_connection(timeout=self.timeout)
        try:
            connection.request(method, self._api_path(endpoint), headers={"Accept": "application/json"})
            response = connection.getresponse()
            return _decode_json_response(response.status, response.read(65537))
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise GatewayOTAError(f"无法连接 Gateway OTA 服务：{exc}") from exc
        finally:
            connection.close()

    def _new_connection(self, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(
            self._url.hostname,
            self._url.port or 80,
            timeout=timeout,
        )

    def _api_path(self, endpoint: str) -> str:
        prefix = self._url.path.rstrip("/")
        return f"{prefix}{endpoint}"


def _normalize_gateway_url(address: str) -> SplitResult:
    raw = str(address).strip()
    if not raw:
        raise GatewayOTAError("Gateway 地址不能为空")
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "http":
        raise GatewayOTAError("局域网 OTA 地址只支持 http://")
    if not parsed.hostname or parsed.username or parsed.password:
        raise GatewayOTAError("Gateway 地址格式无效")
    if parsed.query or parsed.fragment:
        raise GatewayOTAError("Gateway 地址不能包含查询参数或片段")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise GatewayOTAError("Gateway 端口无效") from exc
    return parsed


def _decode_json_response(status_code: int, raw: bytes) -> dict[str, Any]:
    if len(raw) > 65536:
        raise GatewayOTAError("Gateway OTA 响应过大")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayOTAError(f"Gateway 返回了无效响应（HTTP {status_code}）") from exc
    if not isinstance(payload, dict):
        raise GatewayOTAError("Gateway OTA 响应不是 JSON 对象")
    if status_code < 200 or status_code >= 300 or not payload.get("ok"):
        message = str(payload.get("message") or payload.get("error") or "OTA 请求被拒绝")
        raise GatewayOTAError(f"Gateway OTA 请求失败（HTTP {status_code}）：{message}")
    return payload
