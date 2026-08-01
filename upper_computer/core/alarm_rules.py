"""本地报警规则引擎。

中文注释：规则模块保持纯函数风格，不依赖 PyQt，便于仪表盘、离线演示和单元测试
复用。阈值从 config 读取，UI 修改阈值后会同步传入这里，实现“配置即生效”。

返回值统一为规范化事件字典，字段：time / node_id / kind / level / message / title。
"""

from __future__ import annotations

import time
from typing import Any

try:
    from ..config import (
        ALARM_DEDUP_SECONDS,
        CONFIDENCE_THRESHOLD,
        GAS_ALARM_PPM,
        OFFLINE_SECONDS,
        PRESENCE_THRESHOLD,
    )
    from ..rules.detection_fusion import life_motion_triggered
except ImportError:  # 兼容 cd upper_computer 后直接 python main.py
    if __package__ and __package__.startswith("upper_computer"):
        raise
    from config import (
        ALARM_DEDUP_SECONDS,
        CONFIDENCE_THRESHOLD,
        GAS_ALARM_PPM,
        OFFLINE_SECONDS,
        PRESENCE_THRESHOLD,
    )
    from rules.detection_fusion import life_motion_triggered  # type: ignore


_TITLE_BY_KIND = {
    "life_motion": "疑似生命微动",
    "gas": "有害气体异常",
    "offline": "节点离线",
    "alarm": "SYSTEM ALARM",
}


class AlarmEngine:
    """带去重状态的报警规则引擎。

    中文注释：把去重状态封装进实例，避免旧实现中模块级全局字典在多次实例化或
    测试之间互相污染。阈值可在运行时通过 ``update_thresholds`` 热更新。
    """

    def __init__(
        self,
        presence_threshold: float = PRESENCE_THRESHOLD,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
        gas_alarm_ppm: float = GAS_ALARM_PPM,
        offline_seconds: float = OFFLINE_SECONDS,
        dedup_seconds: float = ALARM_DEDUP_SECONDS,
    ) -> None:
        self.presence_threshold = presence_threshold
        self.confidence_threshold = confidence_threshold
        self.gas_alarm_ppm = gas_alarm_ppm
        self.offline_seconds = offline_seconds
        self.dedup_seconds = dedup_seconds
        self._last_alarm_at: dict[tuple[int, str], float] = {}

    def update_thresholds(
        self,
        presence_threshold: float | None = None,
        gas_alarm_ppm: float | None = None,
        gas_alarm_raw: float | None = None,
        confidence_threshold: float | None = None,
    ) -> None:
        """从传感器页配置面板热更新阈值。"""

        if presence_threshold is not None:
            self.presence_threshold = float(presence_threshold)
        if gas_alarm_raw is not None:
            self.gas_alarm_ppm = float(gas_alarm_raw)
        elif gas_alarm_ppm is not None:
            self.gas_alarm_ppm = float(gas_alarm_ppm)
        if confidence_threshold is not None:
            self.confidence_threshold = float(confidence_threshold)

    def reset(self) -> None:
        """清空去重状态（测试 / 重新演示时使用）。"""

        self._last_alarm_at.clear()

    def evaluate(
        self,
        sample: dict[str, Any] | None,
        node_states: dict[int, dict[str, Any]] | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """根据最新样本和全局节点状态返回报警事件列表。"""

        if now is None:
            now = time.time()

        events: list[dict[str, Any]] = []

        # ----- 单样本规则：生命微动 / CO2 估算 ppm 异常 -----
        if sample and sample.get("valid", True):
            node_id = int(sample.get("node_id") or 0)
            gas = _number(sample.get("gas_ppm", sample.get("gas")))

            if (
                node_id > 0
                and life_motion_triggered(
                    sample,
                    presence_threshold=self.presence_threshold,
                )
            ):
                self._append(events, now, node_id, "life_motion", "疑似生命微动", level="ALARM")
            if node_id > 0 and gas > self.gas_alarm_ppm:
                self._append(events, now, node_id, "gas", "CO2 估算 ppm 异常", level="ALARM")

        # ----- 全局规则：节点离线（依赖 node_states 才判断，避免误报） -----
        if node_states is not None:
            for node_id, state in node_states.items():
                last_received = state.get("last_received")
                if last_received is None:
                    last_received = state.get("created_at", now)
                if now - float(last_received) > self.offline_seconds:
                    self._append(events, now, int(node_id), "offline", "节点离线", level="ALARM")

        return events

    def _append(
        self,
        events: list[dict[str, Any]],
        now: float,
        node_id: int,
        kind: str,
        message: str,
        level: str = "ALARM",
    ) -> None:
        key = (node_id, kind)
        last_at = self._last_alarm_at.get(key, 0.0)
        if now - last_at < self.dedup_seconds:
            return

        self._last_alarm_at[key] = now
        events.append(
            {
                "time": now,
                "node_id": node_id,
                "kind": kind,
                "level": level,
                "message": message,
                "title": _TITLE_BY_KIND.get(kind, "SYSTEM ALARM"),
            }
        )


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
