"""多节点生命微动规则融合与 AI 摘要生成。

中文注释：本模块只做可解释的工程规则，不调用 AI。UI 的综合研判与 AI 后端都从
这里拿同一份最近窗口摘要，避免“界面结论”和“AI 输入”不一致。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

try:
    from ..config import PRESENCE_THRESHOLD
except ImportError:
    if __package__ and __package__.startswith("upper_computer"):
        raise
    from config import PRESENCE_THRESHOLD  # type: ignore


@dataclass(slots=True)
class NodeWindowStats:
    node_id: int
    label: str
    sample_count: int
    presence_avg: float
    motion_avg: float
    motion_peak: float
    confidence_avg: float
    rssi_avg: float
    latest_timestamp: float


@dataclass(slots=True)
class DetectionSummary:
    status: str
    detail: str
    participant_ids: list[int]
    participant_labels: list[str]
    triggered_ids: list[int]
    triggered_labels: list[str]
    window_seconds: float
    window_start: float
    window_end: float
    summary_text: str
    stats: list[NodeWindowStats] = field(default_factory=list)

    @property
    def state_key(self) -> str:
        participants = ",".join(str(node_id) for node_id in self.participant_ids)
        triggered = ",".join(str(node_id) for node_id in self.triggered_ids)
        return f"{self.status}|{participants}|{triggered}"


def build_detection_summary(
    nodes: dict[int, dict[str, Any]],
    history: list[dict[str, Any]],
    window_seconds: float = 5.0,
    reference_ts: float | None = None,
    presence_threshold: float = PRESENCE_THRESHOLD,
) -> DetectionSummary:
    """基于最近窗口样本生成多节点规则研判和 AI 输入摘要。"""

    now = reference_ts if reference_ts is not None else time.time()
    window_start = now - window_seconds
    recent = [
        sample
        for sample in history
        if window_start <= _float(sample.get("timestamp"), now) <= now
    ]

    if not history:
        return _empty_summary("等待数据", "参与节点：0；等待 Gateway 串口数据", window_seconds, window_start, now)
    if not recent:
        return _empty_summary("等待数据", "参与节点：0；等待有效节点样本", window_seconds, window_start, now)

    by_node: dict[int, list[dict[str, Any]]] = {}
    for sample in recent:
        node_id = int(sample.get("node_id") or 0)
        if node_id <= 0:
            continue
        by_node.setdefault(node_id, []).append(sample)

    if not by_node:
        return _empty_summary("等待数据", "参与节点：0；等待有效节点样本", window_seconds, window_start, now)

    stats: list[NodeWindowStats] = []
    latest_by_node: dict[int, dict[str, Any]] = {}
    for node_id, samples in sorted(by_node.items()):
        samples = sorted(samples, key=lambda item: _float(item.get("timestamp")))
        latest = samples[-1]
        latest_by_node[node_id] = latest
        label = _node_label(node_id, nodes.get(node_id), latest)
        presence_values = [_score(item.get("presence_score")) for item in samples]
        motion_values = [_score(item.get("motion_score")) for item in samples]
        confidence_values = [_score(item.get("confidence")) for item in samples]
        rssi_values = [_float(item.get("rssi")) for item in samples]
        stats.append(
            NodeWindowStats(
                node_id=node_id,
                label=label,
                sample_count=len(samples),
                presence_avg=_avg(presence_values),
                motion_avg=_avg(motion_values),
                motion_peak=max(motion_values) if motion_values else 0.0,
                confidence_avg=_avg(confidence_values),
                rssi_avg=_avg(rssi_values),
                latest_timestamp=_float(latest.get("timestamp"), now),
            )
        )

    participants = [item.node_id for item in stats]
    participant_labels = [item.label for item in stats]
    triggered: list[int] = []
    for node_id, latest in latest_by_node.items():
        node_state = nodes.get(node_id, {})
        if life_motion_triggered(
            latest,
            node_state,
            presence_threshold=presence_threshold,
        ):
            triggered.append(node_id)

    triggered = sorted(triggered)
    triggered_labels = [_node_label(node_id, nodes.get(node_id), latest_by_node.get(node_id)) for node_id in triggered]
    participant_text = f"参与节点：{len(participants)}"
    trigger_text = "触发节点：" + (", ".join(triggered_labels) if triggered_labels else "无")
    window_text = f"时间窗口：最近 {window_seconds:.0f} 秒"

    if len(participants) == 1:
        if triggered:
            status = "疑似局部微动"
            advice = "建议继续采集，等待多节点支持"
        else:
            status = "数据不足"
            advice = "建议等待更多节点上报"
    elif len(triggered) >= 2:
        status = "多节点疑似生命微动"
        advice = "建议继续采集观察"
    elif len(triggered) == 1:
        status = "疑似局部微动"
        advice = "单节点触发，建议继续观察"
    else:
        status = "未检测到稳定微动"
        advice = "多节点暂未达到稳定阈值"

    detail = f"{participant_text}；{trigger_text}；{window_text}；{advice}"
    summary_text = _build_summary_text(status, detail, stats, window_seconds)
    return DetectionSummary(
        status=status,
        detail=detail,
        participant_ids=participants,
        participant_labels=participant_labels,
        triggered_ids=triggered,
        triggered_labels=triggered_labels,
        window_seconds=window_seconds,
        window_start=window_start,
        window_end=now,
        summary_text=summary_text,
        stats=stats,
    )


def ai_fallback_text(status: str, summary: DetectionSummary | None = None) -> str:
    """生成不依赖大模型的现场辅助摘要。"""

    if summary is None:
        messages = {
            "等待数据": "AI辅助研判：暂无有效样本，建议连接 Gateway 后继续采集",
            "数据不足": "AI辅助研判：当前仅单节点参与，建议等待更多节点形成交叉验证",
            "疑似局部微动": "AI辅助研判：单节点出现异常响应，建议继续观察并等待多节点支持",
            "多节点疑似生命微动": "AI辅助研判：多节点同窗触发，建议重点复核覆盖区域并持续采集",
            "未检测到稳定微动": "AI辅助研判：暂未形成稳定微动特征，建议保持采集并关注趋势",
        }
        return messages.get(status, "AI辅助研判：建议结合多节点数据继续观察")

    participants = len(summary.participant_ids)
    triggered = len(summary.triggered_ids)
    window = f"{summary.window_seconds:.0f}秒"
    trigger_text = _join_labels(summary.triggered_labels, "无")
    participant_text = _join_labels(summary.participant_labels, "无")

    if status == "等待数据":
        return "AI辅助研判：等待 Gateway 有效样本，保持串口连接后继续采集"
    if status == "数据不足":
        return f"AI辅助研判：最近{window}仅{participants}个节点参与，建议等待更多节点交叉验证"
    if status == "疑似局部微动":
        node = trigger_text if triggered else participant_text
        return f"AI辅助研判：{node}单点触发，建议复核覆盖区域并等待相邻节点支持"
    if status == "多节点疑似生命微动":
        return f"AI辅助研判：{trigger_text}在最近{window}同窗高置信触发，建议重点复核并持续采集"
    if status == "未检测到稳定微动":
        return f"AI辅助研判：最近{window}{participants}个节点未形成稳定微动，建议保持采集关注趋势"
    return "AI辅助研判：建议结合触发节点、时间窗口和现场环境继续观察"


def verdict_color_key(status: str) -> str:
    if status == "多节点疑似生命微动":
        return "green"
    if status in {"数据不足", "疑似局部微动"}:
        return "orange"
    if status == "未检测到稳定微动":
        return "blue_soft"
    return "muted"


def life_motion_triggered(
    sample: dict[str, Any] | None,
    node_state: dict[str, Any] | None = None,
    *,
    presence_threshold: float = PRESENCE_THRESHOLD,
) -> bool:
    """按用户设置的存在值硬阈值统一判断疑似微动。"""

    if not sample:
        return False
    node_state = node_state or {}
    raw_presence = sample.get("presence_score", sample.get("presence"))
    if raw_presence is None:
        raw_presence = node_state.get("presence_score", node_state.get("presence"))
    if raw_presence is None:
        return False
    presence = _score(raw_presence)
    threshold = max(0.0, min(_float(presence_threshold, PRESENCE_THRESHOLD), 1.0))
    return presence >= threshold


def _empty_summary(
    status: str,
    detail: str,
    window_seconds: float,
    window_start: float,
    window_end: float,
) -> DetectionSummary:
    return DetectionSummary(
        status=status,
        detail=detail,
        participant_ids=[],
        participant_labels=[],
        triggered_ids=[],
        triggered_labels=[],
        window_seconds=window_seconds,
        window_start=window_start,
        window_end=window_end,
        summary_text=f"最近 {window_seconds:.0f} 秒暂无有效节点样本。规则融合结果：{status}。",
    )


def _build_summary_text(
    status: str,
    detail: str,
    stats: list[NodeWindowStats],
    window_seconds: float,
) -> str:
    lines = [
        f"最近 {window_seconds:.0f} 秒共有 {len(stats)} 个节点参与。",
        detail,
    ]
    for item in stats:
        lines.append(
            f"{item.label} 样本 {item.sample_count} 条，presence 均值 {item.presence_avg:.2f}，"
            f"confidence 均值 {item.confidence_avg:.2f}，motion 峰值 {item.motion_peak:.2f}，"
            f"RSSI 均值 {item.rssi_avg:.0f} dBm。"
        )
    lines.append(f"规则融合结果：{status}。")
    return "\n".join(lines)


def _join_labels(labels: list[str], empty: str) -> str:
    if not labels:
        return empty
    shown = labels[:3]
    suffix = f"等{len(labels)}个节点" if len(labels) > len(shown) else ""
    return "、".join(shown) + suffix


def _node_label(
    node_id: int,
    node_state: dict[str, Any] | None = None,
    sample: dict[str, Any] | None = None,
) -> str:
    label = str((node_state or {}).get("label") or (sample or {}).get("node_code") or "").strip()
    if label:
        return label
    if node_id <= 0:
        return "等待节点接入"
    return f"node{node_id}"


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _score(value: Any) -> float:
    score = _float(value)
    if score > 1.0:
        score /= 100.0
    return max(0.0, min(score, 1.0))


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
