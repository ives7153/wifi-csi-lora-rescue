"""三角形区域 CSI 标定与运行时判定。

该模块只消费 Gateway ``csi_features`` 帧，不读取或改写节点演示 presence 值，
因此普通版、Mobile 版和离线单元测试可以共享同一套真实区域检测逻辑。
"""

from __future__ import annotations

import json
import math
import os
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROFILE_VERSION = 1
EXPECTED_GATEWAY_ID = "GW-02"
EXPECTED_NODE_IDS = (1, 2, 3)
EXPECTED_LINKS = {
    1: (0, 2, 3),
    2: (0, 1, 3),
    3: (0, 1, 2),
}
PHASE_DURATIONS = {"empty": 60.0, "inside": 180.0, "outside": 180.0}
PHASE_LABELS = {"empty": "空场", "inside": "区域内部", "outside": "区域外部"}
PHASE_ORDER = ("empty", "inside", "outside")
MIN_PHASE_SAMPLES = {"empty": 80, "inside": 250, "outside": 250}


class CalibrationError(RuntimeError):
    """现场标定数据不足或验证未达标。"""


@dataclass(slots=True)
class RegionProfile:
    version: int
    gateway_id: str
    node_macs: dict[str, str]
    channel: int
    created_at: float
    feature_medians: list[float]
    feature_scales: list[float]
    selected_indices: list[int]
    train_vectors: list[list[float]]
    train_labels: list[str]
    inside_threshold: float
    outlier_distance: float
    metrics: dict[str, float]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RegionProfile":
        return cls(
            version=int(payload.get("version") or 0),
            gateway_id=str(payload.get("gateway_id") or ""),
            node_macs={str(k): str(v).lower() for k, v in dict(payload.get("node_macs") or {}).items()},
            channel=int(payload.get("channel") or 6),
            created_at=float(payload.get("created_at") or 0.0),
            feature_medians=[float(v) for v in payload.get("feature_medians") or []],
            feature_scales=[float(v) for v in payload.get("feature_scales") or []],
            selected_indices=[int(v) for v in payload.get("selected_indices") or []],
            train_vectors=[[float(v) for v in row] for row in payload.get("train_vectors") or []],
            train_labels=[str(v) for v in payload.get("train_labels") or []],
            inside_threshold=float(payload.get("inside_threshold") or 0.5),
            outlier_distance=float(payload.get("outlier_distance") or 1.0),
            metrics={str(k): float(v) for k, v in dict(payload.get("metrics") or {}).items()},
        )


def default_profile_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "EchoGuard" / "triangle_calibration_gw02.json"
    return Path.home() / ".echoguard" / "triangle_calibration_gw02.json"


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction))))
    return float(ordered[index])


def _normalize_mac(value: Any) -> str:
    return str(value or "").strip().lower()


def _downsample(rows: list[list[float]], maximum: int = 240) -> list[list[float]]:
    if len(rows) <= maximum:
        return list(rows)
    return [rows[round(index * (len(rows) - 1) / (maximum - 1))] for index in range(maximum)]


def _knn_inside_probability(
    vector: list[float],
    train_vectors: list[list[float]],
    labels: list[str],
    *,
    k: int = 7,
) -> tuple[float, float, str]:
    if not train_vectors or len(train_vectors) != len(labels):
        return 0.0, math.inf, "unknown"
    distances: list[tuple[float, str]] = []
    for candidate, label in zip(train_vectors, labels, strict=False):
        squared = sum((value - reference) ** 2 for value, reference in zip(vector, candidate, strict=False))
        distances.append((math.sqrt(squared / max(1, len(vector))), label))
    distances.sort(key=lambda item: item[0])
    neighbours = distances[: min(k, len(distances))]
    votes: dict[str, float] = {"empty": 0.0, "inside": 0.0, "outside": 0.0}
    for distance, label in neighbours:
        votes[label] = votes.get(label, 0.0) + 1.0 / max(1e-4, distance)
    total = sum(votes.values()) or 1.0
    predicted = max(votes, key=votes.get)
    return votes.get("inside", 0.0) / total, neighbours[0][0], predicted


def _fit_feature_space(
    rows: list[list[float]],
    labels: list[str],
    *,
    maximum_features: int = 24,
) -> tuple[list[float], list[float], list[int]]:
    """仅用给定训练集拟合稳健缩放与 Fisher 特征选择。"""

    if not rows or len(rows) != len(labels):
        raise CalibrationError("区域特征训练集为空或标签不一致")
    width = len(rows[0])
    if width == 0 or any(len(row) != width for row in rows):
        raise CalibrationError("区域特征维度不一致")

    medians: list[float] = []
    scales: list[float] = []
    for index in range(width):
        column = [row[index] for row in rows]
        median = _median(column)
        mad = _median([abs(value - median) for value in column])
        medians.append(median)
        scales.append(max(0.01, mad * 1.4826))

    fisher_scores: list[tuple[float, int]] = []
    for index in range(width):
        class_means: list[float] = []
        within = 0.0
        for label in PHASE_ORDER:
            values = [row[index] for row, row_label in zip(rows, labels, strict=False) if row_label == label]
            if not values:
                raise CalibrationError(f"{PHASE_LABELS[label]}训练样本为空")
            mean = sum(values) / len(values)
            class_means.append(mean)
            within += sum((value - mean) ** 2 for value in values) / len(values)
        grand = sum(class_means) / len(class_means)
        between = sum((value - grand) ** 2 for value in class_means)
        fisher_scores.append((between / max(1e-8, within), index))
    selected = [
        index
        for _, index in sorted(fisher_scores, reverse=True)[: min(maximum_features, width)]
    ]
    return medians, scales, selected


def _transform_with_feature_space(
    row: list[float],
    medians: list[float],
    scales: list[float],
    selected: list[int],
) -> list[float]:
    return [
        max(-8.0, min(8.0, (row[index] - medians[index]) / scales[index]))
        for index in selected
    ]


class TriangleRegionDetector:
    """组装九条链路、执行现场标定并输出带迟滞的全局区域状态。"""

    def __init__(self, profile_path: Path | None = None) -> None:
        self.profile_path = profile_path or default_profile_path()
        self.profile: RegionProfile | None = None
        self.latest_frames: dict[int, dict[str, Any]] = {}
        self.node_macs: dict[int, str] = {}
        self.last_feature_at = 0.0
        self.last_vector_at = 0.0
        self.current_phase = ""
        self.phase_deadline = 0.0
        self.completed_phases: list[str] = []
        self.samples: dict[str, list[list[float]]] = {phase: [] for phase in PHASE_ORDER}
        self.calibration_error = ""
        self.validation_metrics: dict[str, float] = {}
        self.status = "not_calibrated"
        self.confidence = 0.0
        self.inside_probability = 0.0
        self.valid_links = 0
        self.updated_at = 0.0
        self.inside_streak = 0
        self.outside_streak = 0
        self.outlier_streak = 0
        self.last_transition = ""
        self._load_profile()

    def _load_profile(self) -> None:
        try:
            payload = json.loads(self.profile_path.read_text(encoding="utf-8"))
            profile = RegionProfile.from_dict(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        if (
            profile.version != PROFILE_VERSION
            or profile.gateway_id != EXPECTED_GATEWAY_ID
            or not profile.selected_indices
            or not profile.train_vectors
            or len(profile.train_vectors) != len(profile.train_labels)
            or set(profile.node_macs) != {str(node) for node in EXPECTED_NODE_IDS}
            or any(index < 0 for index in profile.selected_indices)
            or max(profile.selected_indices, default=-1) >= len(profile.feature_medians)
            or max(profile.selected_indices, default=-1) >= len(profile.feature_scales)
            or any(len(row) != len(profile.selected_indices) for row in profile.train_vectors)
        ):
            return
        self.profile = profile
        self.validation_metrics = dict(profile.metrics)
        self.status = "insufficient"

    def start_phase(self, phase: str, *, now: float | None = None) -> None:
        phase = str(phase or "").strip().lower()
        if phase not in PHASE_ORDER:
            raise CalibrationError("未知标定阶段")
        expected_index = len(self.completed_phases)
        if expected_index >= len(PHASE_ORDER) or PHASE_ORDER[expected_index] != phase:
            expected = PHASE_ORDER[min(expected_index, len(PHASE_ORDER) - 1)]
            raise CalibrationError(f"请先完成{PHASE_LABELS[expected]}阶段")
        if self.current_phase:
            raise CalibrationError("当前标定阶段尚未结束")
        if not self._identity_ready():
            raise CalibrationError("尚未收到GW-02三个节点的完整九链路特征")

        self.samples[phase] = []
        self.current_phase = phase
        current = time.time() if now is None else float(now)
        self.phase_deadline = current + PHASE_DURATIONS[phase]
        self.calibration_error = ""
        self.status = "calibrating"
        self.last_transition = ""

    def cancel_calibration(self) -> None:
        self.current_phase = ""
        self.phase_deadline = 0.0
        self.completed_phases.clear()
        self.samples = {phase: [] for phase in PHASE_ORDER}
        self.calibration_error = "标定已取消"
        self.status = "insufficient" if self.profile is not None else "not_calibrated"

    def ingest(self, frame: dict[str, Any], *, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        if str(frame.get("gateway_id") or "") != EXPECTED_GATEWAY_ID:
            self.status = "gateway_mismatch"
            self.updated_at = current
            return True
        node_id = int(frame.get("node_id") or 0)
        if node_id not in EXPECTED_NODE_IDS:
            return False
        node_mac = _normalize_mac(frame.get("node_mac"))
        if node_mac:
            self.node_macs[node_id] = node_mac
        copied = dict(frame)
        copied["received_at"] = current
        self.latest_frames[node_id] = copied
        self.last_feature_at = current

        vector = self._assemble_vector(current)
        if vector is None or current - self.last_vector_at < 0.4:
            return False
        self.last_vector_at = current
        self.updated_at = current
        self.valid_links = 9

        if self.current_phase:
            self.samples[self.current_phase].append(vector)
            self.status = "calibrating"
            self.tick(now=current)
            return True
        if self.profile is None:
            self.status = "not_calibrated"
            return True
        if not self._profile_matches_current_identity():
            self.status = "profile_mismatch"
            return True

        transformed = self._transform(vector, self.profile)
        probability, nearest_distance, predicted = _knn_inside_probability(
            transformed,
            self.profile.train_vectors,
            self.profile.train_labels,
        )
        self.inside_probability = probability
        self.confidence = max(probability, 1.0 - probability)
        if nearest_distance > self.profile.outlier_distance:
            self.outlier_streak += 1
        else:
            self.outlier_streak = 0
        if self.outlier_streak >= 60:
            self.status = "needs_recalibration"
            return True

        inside = probability >= self.profile.inside_threshold and predicted == "inside"
        if inside:
            self.inside_streak += 1
            self.outside_streak = 0
            if self.inside_streak >= 2 and self.status != "occupied":
                self.status = "occupied"
                self.last_transition = "occupied"
        else:
            self.inside_streak = 0
            self.outside_streak += 1
            if self.outside_streak >= 3 and self.status != "clear":
                self.status = "clear"
                self.last_transition = "clear"
        return True

    def tick(self, *, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        changed = False
        if self.current_phase and current >= self.phase_deadline:
            phase = self.current_phase
            self.current_phase = ""
            self.phase_deadline = 0.0
            count = len(self.samples[phase])
            if count < MIN_PHASE_SAMPLES[phase]:
                self.calibration_error = (
                    f"{PHASE_LABELS[phase]}样本不足：{count}/{MIN_PHASE_SAMPLES[phase]}，请重新标定"
                )
                self.completed_phases.clear()
                self.status = "calibration_failed"
            else:
                self.completed_phases.append(phase)
                self.status = "calibration_ready_next"
                if phase == "outside":
                    try:
                        self._train_and_save()
                    except CalibrationError as exc:
                        self.calibration_error = str(exc)
                        self.status = "calibration_failed"
                    else:
                        self.status = "clear"
                        self.calibration_error = ""
            changed = True
        if (
            not self.current_phase
            and self.last_feature_at > 0.0
            and current - self.last_feature_at > 2.0
            and self.status not in {"not_calibrated", "calibration_failed", "profile_mismatch"}
        ):
            self.status = "insufficient"
            self.valid_links = 0
            self.inside_streak = 0
            self.outside_streak = 0
            changed = True
        return changed

    def consume_transition(self) -> str:
        transition = self.last_transition
        self.last_transition = ""
        return transition

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        remaining = max(0.0, self.phase_deadline - current) if self.current_phase else 0.0
        labels = {
            "not_calibrated": "未标定",
            "calibrating": "正在标定",
            "calibration_ready_next": "等待下一阶段",
            "calibration_failed": "标定未通过",
            "insufficient": "数据不足",
            "gateway_mismatch": "需要GW-02",
            "profile_mismatch": "设备与标定不匹配",
            "needs_recalibration": "需要重新标定",
            "occupied": "区域内有人",
            "clear": "区域内无人",
        }
        return {
            "status": self.status,
            "label": labels.get(self.status, self.status),
            "confidence": self.confidence,
            "inside_probability": self.inside_probability,
            "valid_links": self.valid_links,
            "updated_at": self.updated_at,
            "gateway_id": EXPECTED_GATEWAY_ID,
            "profile_loaded": self.profile is not None,
            "profile_path": str(self.profile_path),
            "metrics": dict(self.validation_metrics),
            "calibration": {
                "phase": self.current_phase,
                "phase_label": PHASE_LABELS.get(self.current_phase, ""),
                "remaining_seconds": remaining,
                "completed": list(self.completed_phases),
                "counts": {phase: len(rows) for phase, rows in self.samples.items()},
                "error": self.calibration_error,
            },
        }

    def _identity_ready(self) -> bool:
        if set(self.node_macs) != set(EXPECTED_NODE_IDS):
            return False
        vector = self._assemble_vector(time.time())
        return vector is not None

    def _profile_matches_current_identity(self) -> bool:
        if self.profile is None or set(self.node_macs) != set(EXPECTED_NODE_IDS):
            return False
        current = {str(node): mac for node, mac in sorted(self.node_macs.items())}
        return current == self.profile.node_macs

    def _assemble_vector(self, current: float) -> list[float] | None:
        if set(self.latest_frames) != set(EXPECTED_NODE_IDS):
            self.valid_links = 0
            return None
        timestamps = [float(self.latest_frames[node].get("received_at") or 0.0) for node in EXPECTED_NODE_IDS]
        if max(timestamps) - min(timestamps) > 1.2 or current - min(timestamps) > 1.5:
            self.valid_links = 0
            return None

        vector: list[float] = []
        valid_links = 0
        for receiver in EXPECTED_NODE_IDS:
            frame = self.latest_frames[receiver]
            links = {
                int(link.get("source_id") or 0): link
                for link in frame.get("links") or []
                if isinstance(link, dict)
            }
            for source in EXPECTED_LINKS[receiver]:
                link = links.get(source)
                if not link or not link.get("valid") or int(link.get("sample_count") or 0) < 5:
                    self.valid_links = valid_links
                    return None
                mad = list(link.get("mad_bands") or [])
                diff = list(link.get("diff_bands") or [])
                if len(mad) != 8 or len(diff) != 8:
                    self.valid_links = valid_links
                    return None
                vector.extend(max(0.0, min(float(value), 255.0)) / 255.0 for value in mad)
                vector.extend(max(0.0, min(float(value), 255.0)) / 255.0 for value in diff)
                vector.extend(
                    (
                        max(0.0, min(float(link.get("active_ratio") or 0.0), 255.0)) / 255.0,
                        max(0.0, min(float(link.get("correlation_delta") or 0.0), 255.0)) / 255.0,
                        (max(-100.0, min(float(link.get("rssi") or -100.0), -20.0)) + 100.0) / 80.0,
                        max(0.0, min(float(link.get("rssi_std") or 0.0), 255.0)) / 255.0,
                        min(float(link.get("sample_count") or 0.0) / 32.0, 1.0),
                    )
                )
                valid_links += 1
        self.valid_links = valid_links
        return vector

    def _train_and_save(self) -> None:
        raw_rows: list[list[float]] = []
        raw_labels: list[str] = []
        for label in PHASE_ORDER:
            rows = _downsample(self.samples[label])
            if len(rows) < MIN_PHASE_SAMPLES[label]:
                raise CalibrationError(f"{PHASE_LABELS[label]}有效样本不足")
            raw_rows.extend(rows)
            raw_labels.extend([label] * len(rows))
        medians, scales, selected = _fit_feature_space(raw_rows, raw_labels)
        transformed = [
            _transform_with_feature_space(row, medians, scales, selected)
            for row in raw_rows
        ]
        probabilities = [0.0] * len(raw_rows)
        predictions = ["unknown"] * len(raw_rows)
        folds: list[int] = []
        class_offsets = {label: 0 for label in PHASE_ORDER}
        class_counts = {label: raw_labels.count(label) for label in PHASE_ORDER}
        for label in raw_labels:
            offset = class_offsets[label]
            count = class_counts[label]
            folds.append(min(4, int(offset * 5 / max(1, count))))
            class_offsets[label] += 1
        # 五段连续块交叉验证。每折的缩放参数和特征选择只用训练段拟合，
        # 避免验证段信息泄漏并高估现场检出率。
        for fold in range(5):
            train_indices = [index for index, value in enumerate(folds) if value != fold]
            validation_indices = [index for index, value in enumerate(folds) if value == fold]
            fold_rows = [raw_rows[index] for index in train_indices]
            fold_labels = [raw_labels[index] for index in train_indices]
            fold_medians, fold_scales, fold_selected = _fit_feature_space(fold_rows, fold_labels)
            train_vectors = [
                _transform_with_feature_space(raw_rows[index], fold_medians, fold_scales, fold_selected)
                for index in train_indices
            ]
            for index in validation_indices:
                vector = _transform_with_feature_space(
                    raw_rows[index], fold_medians, fold_scales, fold_selected
                )
                probability, _, predicted = _knn_inside_probability(
                    vector, train_vectors, fold_labels
                )
                probabilities[index] = probability
                predictions[index] = predicted

        best: tuple[float, float, float] | None = None
        for integer in range(30, 96):
            threshold = integer / 100.0
            inside_total = sum(label == "inside" for label in raw_labels)
            negative_total = len(raw_labels) - inside_total
            recall = sum(
                label == "inside" and probability >= threshold and predicted == "inside"
                for label, probability, predicted in zip(
                    raw_labels, probabilities, predictions, strict=False
                )
            ) / max(1, inside_total)
            false_positive = sum(
                label != "inside" and probability >= threshold and predicted == "inside"
                for label, probability, predicted in zip(
                    raw_labels, probabilities, predictions, strict=False
                )
            ) / max(1, negative_total)
            if false_positive <= 0.05 and (best is None or recall > best[1] or (recall == best[1] and threshold < best[0])):
                best = (threshold, recall, false_positive)
        if best is None:
            raise CalibrationError("外部误报率无法降到5%以内，请检查摆位并重新采集")
        threshold, recall, false_positive = best
        metrics = {
            "inside_recall": recall,
            "outside_false_positive": false_positive,
            "inside_threshold": threshold,
            "sample_count": float(len(raw_rows)),
            "cv_folds": 5.0,
        }
        self.validation_metrics = metrics
        if recall < 0.95:
            raise CalibrationError(
                f"内部检出率仅{recall * 100:.1f}%（目标≥95%），请调整摆位后重新标定"
            )

        nearest_distances: list[float] = []
        for index, vector in enumerate(transformed):
            peers = [row for j, row in enumerate(transformed) if j != index]
            peer_labels = [label for j, label in enumerate(raw_labels) if j != index]
            _, distance, _ = _knn_inside_probability(vector, peers, peer_labels, k=1)
            nearest_distances.append(distance)
        outlier_distance = max(0.5, _percentile(nearest_distances, 0.99) * 2.0)

        profile = RegionProfile(
            version=PROFILE_VERSION,
            gateway_id=EXPECTED_GATEWAY_ID,
            node_macs={str(node): mac for node, mac in sorted(self.node_macs.items())},
            channel=6,
            created_at=time.time(),
            feature_medians=medians,
            feature_scales=scales,
            selected_indices=selected,
            train_vectors=transformed,
            train_labels=raw_labels,
            inside_threshold=threshold,
            outlier_distance=outlier_distance,
            metrics=metrics,
        )
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.profile_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(profile), ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.profile_path)
        self.profile = profile
        self.inside_streak = 0
        self.outside_streak = 0
        self.outlier_streak = 0

    @staticmethod
    def _transform(vector: list[float], profile: RegionProfile) -> list[float]:
        return [
            max(
                -8.0,
                min(
                    8.0,
                    (vector[index] - profile.feature_medians[index]) /
                    max(0.01, profile.feature_scales[index]),
                ),
            )
            for index in profile.selected_indices
        ]
