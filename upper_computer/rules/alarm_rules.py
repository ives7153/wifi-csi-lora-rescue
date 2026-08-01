"""本地规则报警。

中文注释：规则模块保持纯函数风格，不直接依赖 Dear PyGui，便于主界面、
离线演示和后续单元测试复用。
"""

from __future__ import annotations

from typing import Any, overload

try:
    from ..config import GAS_ALARM_PPM, OFFLINE_SECONDS, PRESENCE_THRESHOLD
    from .detection_fusion import life_motion_triggered
except ImportError:
    if __package__ and __package__.startswith("upper_computer"):
        raise
    from config import GAS_ALARM_PPM, OFFLINE_SECONDS, PRESENCE_THRESHOLD  # type: ignore
    from rules.detection_fusion import life_motion_triggered  # type: ignore

DEDUP_SECONDS = 5.0
GAS_THRESHOLD = GAS_ALARM_PPM

_LAST_ALARM_AT: dict[tuple[int, str], float] = {}


def reset_alarm_state() -> None:
    """清空报警去重状态，主要用于测试或重新开始演示。"""

    _LAST_ALARM_AT.clear()


@overload
def evaluate_sample(sample: dict[str, Any] | None) -> list[str]:
    ...


@overload
def evaluate_sample(
    sample: dict[str, Any] | None,
    node_states: dict[int, dict[str, Any]],
    now: float,
) -> list[dict[str, Any]]:
    ...


def evaluate_sample(
    sample: dict[str, Any] | None,
    node_states: dict[int, dict[str, Any]] | None = None,
    now: float | None = None,
) -> list[str] | list[dict[str, Any]]:
    """根据最新样本和节点状态返回报警事件列表。

    中文注释：目标接口要求支持 evaluate_sample(sample)，当前 main.py 仍会传入
    node_states 与 now 来检查离线规则，因此这里用可选参数同时兼容两种调用方式。
    """

    if now is None:
        import time

        now = time.time()

    dict_events: list[dict[str, Any]] = []
    text_events: list[str] = []

    if sample and sample.get("valid", True):
        node_id = int(sample.get("node_id") or 0)
        gas = _number(sample.get("gas"))

        if node_id > 0 and life_motion_triggered(
            sample,
            presence_threshold=PRESENCE_THRESHOLD,
        ):
            _append_alarm(dict_events, text_events, now, node_id, "life_motion", "疑似生命微动")
        if node_id > 0 and gas > GAS_THRESHOLD:
            _append_alarm(dict_events, text_events, now, node_id, "gas", "气体异常")

        # 中文注释：单样本调用如果带有 last_received，也可以独立判断离线。
        last_received = sample.get("last_received")
        if node_states is None and last_received is not None and now - _number(last_received, now) > OFFLINE_SECONDS:
            _append_alarm(dict_events, text_events, now, node_id, "offline", "离线")

    if node_states is None:
        return text_events

    # 中文注释：离线规则依赖全局节点状态，单样本调用时不会误报离线。
    for node_id, state in node_states.items():
        last_received = state.get("last_received")
        if last_received is None:
            last_received = state.get("created_at", now)
        if now - float(last_received) > OFFLINE_SECONDS:
            _append_alarm(dict_events, text_events, now, int(node_id), "offline", "节点离线")

    return dict_events


def _append_alarm(
    dict_events: list[dict[str, Any]],
    text_events: list[str],
    now: float,
    node_id: int,
    kind: str,
    message: str,
) -> None:
    key = (node_id, kind)
    last_at = _LAST_ALARM_AT.get(key, 0.0)
    if now - last_at < DEDUP_SECONDS:
        return

    _LAST_ALARM_AT[key] = now
    text_events.append(f"Node {node_id} {message}")
    dict_events.append(
        {
            "time": now,
            "node_id": node_id,
            "kind": kind,
            "level": "ALARM",
            "message": message,
        }
    )


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
