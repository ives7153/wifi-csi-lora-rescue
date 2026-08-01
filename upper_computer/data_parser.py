"""Gateway JSON Lines 数据解析模块。

中文注释：Gateway 固件（firmware/gateway/main/main.c）把每个 LoRa 数据帧
转成一行 JSON 输出，示例：

    {"id":1,"seq":0,"presence":0,"motion":0,"bpm":0,"conf":0,
     "gas":0,"temp":25.0,"hum":50,"rssi":-60,"ts":12345}

上位机只消费规范化后的字段，避免 UI 层到处关心固件字段别名。本模块是纯函数，
不依赖 PyQt/串口，便于离线单元测试。
"""

from __future__ import annotations

import json
import time
from typing import Any

try:
    from .gas_calibration import calculate_gas_ppm
except ImportError:
    if __package__:
        raise
    from gas_calibration import calculate_gas_ppm


def parse_gateway_frame(line: str) -> dict[str, Any]:
    """解析 Gateway 串口输出的一行 JSON 数据。

    返回的字典始终包含全部规范化字段；``valid`` 表示是否为一帧有效 Gateway 数据。
    解析失败时 ``error`` 描述原因，便于上位机在状态栏提示而不是直接崩溃。
    """

    raw = line.strip()
    received_at = time.time()
    result: dict[str, Any] = {
        "valid": False,
        "raw": raw,
        "error": "",
        "frame_type": "node_data",
        "gateway_status": None,
        "gateway_id": "",
        "node_mac": "",
        "links": [],
        "node_id": None,
        "seq": None,
        "presence_score": 0.0,
        "motion_score": 0.0,
        "breath_bpm": 0.0,
        "confidence": 0.0,
        "csi_quality": None,
        "csi_sample_count": None,
        "breath_lock": None,
        "noise_floor": None,
        "gas": 0.0,
        "gas_raw": 0.0,
        "gas_ppm": 0.0,
        "temperature": 0.0,
        "humidity": 0.0,
        "rssi": 0.0,
        "timestamp": received_at,
        "source_ts_ms": None,
        "node_label": "",
    }

    if not raw:
        result["error"] = "空行"
        return result

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        result["error"] = f"不是有效 JSON Lines: {exc.msg}"
        return result

    if not isinstance(payload, dict):
        result["error"] = "JSON 顶层不是对象"
        return result

    if str(payload.get("type") or "").strip().lower() == "gateway_status":
        result.update(
            {
                "valid": True,
                "frame_type": "gateway_status",
                "gateway_status": dict(payload),
                "source_ts_ms": _optional_int(_first(payload, "uptime_ms", "ts")),
            }
        )
        return result

    if str(payload.get("type") or "").strip().lower() == "csi_features":
        return _parse_csi_features(payload, result)

    node_id = _first(payload, "id", "node_id")
    if node_id is None:
        result["error"] = "缺少节点 id"
        return result

    gas_raw = _number(_first(payload, "gas", "gas_raw"), 0.0)
    gas_ppm = _number(_first(payload, "gas_ppm", "ppm"), -1.0)
    if gas_ppm < 0.0:
        gas_ppm = calculate_gas_ppm(gas_raw)

    result.update(
        {
            "valid": True,
            "node_id": int(_number(node_id, 0)),
            "seq": _optional_int(_first(payload, "seq", "sequence")),
            "presence_score": _normalize_score(_first(payload, "presence", "presence_score")),
            "motion_score": _normalize_score(_first(payload, "motion", "motion_score")),
            "breath_bpm": _number(_first(payload, "bpm", "breath_bpm"), 0.0),
            "confidence": _normalize_score(_first(payload, "conf", "confidence")),
            "csi_quality": _optional_score(_first(payload, "csi_quality", "quality")),
            "csi_sample_count": _optional_int(_first(payload, "csi_sample_count", "csi_n", "sample_count")),
            "breath_lock": _optional_bool(_first(payload, "breath_lock", "breath_locked")),
            "noise_floor": _optional_float(_first(payload, "noise_floor", "csi_noise_floor")),
            "gas": gas_ppm,
            "gas_raw": gas_raw,
            "gas_ppm": gas_ppm,
            "temperature": _number(_first(payload, "temp", "temperature"), 0.0),
            "humidity": _number(_first(payload, "hum", "humidity"), 0.0),
            "rssi": _number(_first(payload, "rssi"), 0.0),
            "source_ts_ms": _optional_int(_first(payload, "ts", "timestamp_ms")),
            "node_label": _text(_first(payload, "name", "label", "node_name", "node_label")),
        }
    )
    return result


def _parse_csi_features(payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """规范化 Gateway 转发的区域 CSI 特征帧。

    固件为节省串口带宽使用 ``src/n/active/corr/mad/diff`` 等短字段；区域检测器
    只消费这里展开后的稳定字段，避免固件协议细节泄漏到业务和 UI 层。
    """

    node_id = _optional_int(_first(payload, "node", "node_id", "id"))
    if node_id is None or node_id <= 0:
        result["error"] = "区域特征帧缺少有效节点 id"
        return result

    gateway_id = _text(_first(payload, "gateway_id", "gateway"))
    node_mac = _normalize_mac(_first(payload, "node_mac", "mac"))
    raw_links = payload.get("links")
    if not isinstance(raw_links, list):
        result["error"] = "区域特征帧 links 不是数组"
        return result

    links: list[dict[str, Any]] = []
    for raw_link in raw_links:
        if not isinstance(raw_link, dict):
            continue
        mad = _byte_vector(_first(raw_link, "mad", "mad_bands"))
        diff = _byte_vector(_first(raw_link, "diff", "diff_bands"))
        source_id = _optional_int(_first(raw_link, "src", "source_id"))
        if source_id is None:
            continue
        links.append(
            {
                "source_id": source_id,
                "valid": bool(_optional_bool(raw_link.get("valid"))),
                "sample_count": max(0, _optional_int(_first(raw_link, "n", "sample_count")) or 0),
                "rssi": _number(raw_link.get("rssi"), -100.0),
                "rssi_std": max(0.0, _number(raw_link.get("rssi_std"), 0.0)),
                "active_ratio": max(0.0, min(_number(_first(raw_link, "active", "active_ratio"), 0.0), 255.0)),
                "correlation_delta": max(0.0, min(_number(_first(raw_link, "corr", "correlation_delta"), 0.0), 255.0)),
                "mad_bands": mad,
                "diff_bands": diff,
            }
        )

    if not links:
        result["error"] = "区域特征帧没有可用链路"
        return result

    result.update(
        {
            "valid": True,
            "frame_type": "csi_features",
            "gateway_id": gateway_id,
            "node_id": node_id,
            "node_mac": node_mac,
            "seq": _optional_int(payload.get("seq")),
            "source_ts_ms": _optional_int(_first(payload, "epoch_ms", "ts")),
            "links": links,
            "region_protocol": _optional_int(_first(payload, "v", "version")),
            "region_flags": _optional_int(payload.get("flags")) or 0,
        }
    )
    return result


def _first(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    return None


def _number(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _normalize_mac(value: Any) -> str:
    text = _text(value).lower().replace("-", ":")
    parts = text.split(":")
    if len(parts) != 6:
        return text
    try:
        return ":".join(f"{int(part, 16):02x}" for part in parts)
    except ValueError:
        return text


def _byte_vector(value: Any) -> list[int]:
    if not isinstance(value, (list, tuple)):
        return []
    return [max(0, min(int(_number(item, 0.0)), 255)) for item in value]


def _optional_score(value: Any) -> float | None:
    if value is None:
        return None
    return _normalize_score(value)


def _normalize_score(value: Any) -> float:
    """把 presence/motion/conf 等字段统一到 0~1。

    中文注释：固件可能用 0~100 的整数表示百分比，这里超过 1.0 自动除以 100，
    保证 UI 与报警规则永远拿到 0~1 的标准分值。
    """

    score = _number(value, 0.0)
    if score > 1.0:
        score /= 100.0
    return max(0.0, min(score, 1.0))
