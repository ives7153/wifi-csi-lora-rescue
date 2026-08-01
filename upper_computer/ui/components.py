"""现代深色工业风 UI 组件。

中文注释：这些组件只负责视觉呈现，不直接访问串口和业务规则。页面拿到
DataManager 的快照后，把数据分发给这里的卡片、曲线、日志、拓扑、节点矩阵表
和配置控件。组件尽量自洽，方便在多个页面复用。
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable

import pyqtgraph as pg
from PyQt6.QtCore import QPointF, QPropertyAnimation, QRectF, Qt, QTimer, pyqtProperty, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

try:
    from ..config import (
        GATEWAY_ID,
        HEALTH_COLORS,
        HEALTH_CRITICAL,
        HEALTH_INACTIVE,
        NODE_LABELS,
        PRESENCE_THRESHOLD,
        THEME,
        TOPOLOGY_NODE_POSITIONS,
    )
    from ..rules.detection_fusion import life_motion_triggered
except ImportError:
    if __package__ and __package__.startswith("upper_computer"):
        raise
    from config import (
        GATEWAY_ID,
        HEALTH_COLORS,
        HEALTH_CRITICAL,
        HEALTH_INACTIVE,
        NODE_LABELS,
        PRESENCE_THRESHOLD,
        THEME,
        TOPOLOGY_NODE_POSITIONS,
    )
    from rules.detection_fusion import life_motion_triggered  # type: ignore


# ---------------------------------------------------------------------------
# 基础辅助
# ---------------------------------------------------------------------------
def apply_shadow(widget: QWidget, blur: int = 26, alpha: int = 120, dy: int = 10) -> None:
    """给卡片添加轻微悬浮阴影。"""

    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(blur)
    shadow.setOffset(0, dy)
    shadow.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(shadow)


def clear_layout(layout: QVBoxLayout | QHBoxLayout) -> None:
    """递归清空布局内子项。"""

    while layout.count():
        item = layout.takeAt(0)
        child = item.widget()
        if child is not None:
            child.deleteLater()
            continue
        sub = item.layout()
        if sub is not None:
            clear_layout(sub)


class CardFrame(QFrame):
    """统一卡片容器。"""

    def __init__(self, alt: bool = False, shadow: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("cardAlt" if alt else "card", True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        if shadow:
            apply_shadow(self)


# ---------------------------------------------------------------------------
# 状态胶囊 / 指标卡
# ---------------------------------------------------------------------------
class StatusPill(QLabel):
    """顶部运行状态胶囊。"""

    def __init__(self, text: str = "● 实时生命体征监测", ok: bool = True) -> None:
        super().__init__(text)
        self.setObjectName("StatusPill")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._text = text
        self._ok = ok
        self.set_state(text, ok)

    def set_state(self, text: str, ok: bool) -> None:
        self._text = text
        self._ok = ok
        color = THEME["green"] if ok else THEME["orange"]
        self.setText(text)
        self.setStyleSheet(
            f"QLabel#StatusPill {{ color: {color}; font-size: 14px; font-weight: 700; }}"
        )

    def refresh_theme(self) -> None:
        self.set_state(self._text, self._ok)


class MetricCard(CardFrame):
    """生命体征 / 环境 / 链路指标卡片。"""

    def __init__(self, title: str, value: str = "-", hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.setMinimumHeight(108)
        self._accent: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("MetricTitle")
        self.title_label.setWordWrap(True)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("MetricValue")
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("MetricHint")

        layout.addWidget(self.title_label)
        layout.addStretch(1)
        layout.addWidget(self.value_label)
        layout.addWidget(self.hint_label)

    def set_value(self, value: str, hint: str = "", accent: str | None = None) -> None:
        if self.value_label.text() == value and self.hint_label.text() == hint and self._accent == accent:
            return
        self._accent = accent
        self.value_label.setText(value)
        self.hint_label.setText(hint)
        self.value_label.setStyleSheet(f"color: {accent};" if accent else "")

    def refresh_theme(self) -> None:
        self.value_label.setStyleSheet(f"color: {self._accent};" if self._accent else "")


# ---------------------------------------------------------------------------
# 融合扰动趋势图
# ---------------------------------------------------------------------------
class CsiTrendPlot(CardFrame):
    """实时融合扰动趋势图。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.setMinimumHeight(330)

        pg.setConfigOptions(antialias=True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("融合扰动趋势 (Fusion Disturbance Trends)")
        title.setObjectName("SectionTitle")
        subtitle = QLabel("Derived from node presence, motion and confidence scores")
        subtitle.setObjectName("SubtleText")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        self.node_badge = QLabel("等待节点")
        self.node_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rssi_badge = QLabel("RSSI: -- dBm")
        self.rssi_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        header.addLayout(title_box)
        header.addStretch(1)
        header.addWidget(self.node_badge)
        header.addWidget(self.rssi_badge)
        layout.addLayout(header)

        self.plot = pg.PlotWidget()
        self.plot.setBackground(THEME["card"])
        self.plot.showGrid(x=True, y=True, alpha=0.16)
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.setYRange(0.0, 1.0)
        self.plot.setXRange(-60.0, 0.0)
        self.plot.hideButtons()
        for axis_name in ("bottom", "left"):
            axis = self.plot.getAxis(axis_name)
            axis.setPen(pg.mkPen(THEME["plot_axis"]))
            axis.setTextPen(pg.mkPen(THEME["plot_axis_text"]))
        self.plot.getPlotItem().setContentsMargins(4, 4, 4, 4)
        self.plot.setMinimumHeight(220)
        self.empty_label = QLabel("等待 Gateway 串口数据")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("SubtleText")
        self.empty_label.setStyleSheet(f"color: {THEME['muted']}; font-size: 13px;")

        self.amplitude_curve = self.plot.plot(
            [], [], pen=pg.mkPen(THEME["blue_bright"], width=2.4), name="融合扰动"
        )
        self.noise_curve = self.plot.plot(
            [],
            [],
            pen=pg.mkPen(THEME["plot_noise"], width=1.4, style=Qt.PenStyle.DashLine),
            name="基准噪声",
        )

        legend = QHBoxLayout()
        legend.setSpacing(16)
        self.amplitude_legend = _legend_label("● 融合扰动", THEME["blue_bright"])
        self.noise_legend = _legend_label("○ 基准噪声", THEME["plot_noise_legend"])
        legend.addWidget(self.amplitude_legend)
        legend.addWidget(self.noise_legend)
        legend.addStretch(1)
        self.sample_rate_label = QLabel("刷新: 1Hz")
        self.sample_rate_label.setObjectName("SubtleText")
        legend.addWidget(self.sample_rate_label)

        layout.addWidget(self.plot)
        layout.addWidget(self.empty_label)
        layout.addLayout(legend)
        self.refresh_theme()

    def set_history(self, history: list[dict[str, Any]], active_node: int, node_state: dict[str, Any]) -> None:
        now = time.time()
        recent = [
            sample
            for sample in history
            if int(sample.get("node_id") or 0) == active_node
            and now - _float(sample.get("timestamp"), now) <= 60.0
        ]
        if not recent:
            recent = [
                sample for sample in history if now - _float(sample.get("timestamp"), now) <= 60.0
            ][-90:]
        self.empty_label.setVisible(not bool(recent))

        xs: list[float] = []
        ys: list[float] = []
        noise: list[float] = []
        for index, sample in enumerate(recent[-160:]):
            timestamp = _float(sample.get("timestamp"), now)
            xs.append(timestamp - now)
            motion = _score(sample.get("motion_score"))
            presence = _score(sample.get("presence_score"))
            confidence = _score(sample.get("confidence"))
            amplitude = min(1.0, 0.11 + motion * 0.42 + presence * 0.32 + confidence * 0.16)
            ys.append(amplitude)
            noise.append(
                max(0.03, 0.19 + math.sin(index * 0.41) * 0.08 + math.cos(index * 0.17) * 0.035)
            )

        self.amplitude_curve.setData(xs, ys)
        self.noise_curve.setData(xs, noise)
        self.plot.setXRange(-60.0, 0.0, padding=0)
        self.plot.setYRange(0.0, 1.0, padding=0)

        label = str(node_state.get("label") or "").strip()
        if not label:
            label = NODE_LABELS.get(active_node, f"node{active_node}") if active_node > 0 else "等待节点"
        self.node_badge.setText(label)
        # 中文注释：CSI 徽标显示 WiFi RSSI（较强），缺失时回退到 LoRa rssi。
        wifi_rssi = node_state.get("wifi_rssi")
        if active_node <= 0 or not node_state:
            self.rssi_badge.setText("RSSI: -- dBm")
        else:
            rssi_value = _float(wifi_rssi if wifi_rssi is not None else node_state.get("rssi"))
            self.rssi_badge.setText(f"RSSI: {rssi_value:.0f}dBm")

    def refresh_theme(self) -> None:
        badge_style = (
            f"background: {THEME['tag_bg']}; color: {THEME['text_soft']};"
            f" border: 1px solid {THEME['tag_border']};"
            " border-radius: 8px; padding: 6px 12px; font-weight: 600;"
        )
        for badge in (self.node_badge, self.rssi_badge):
            badge.setStyleSheet(badge_style)
        self.plot.setBackground(THEME["card"])
        for axis_name in ("bottom", "left"):
            axis = self.plot.getAxis(axis_name)
            axis.setPen(pg.mkPen(THEME["plot_axis"]))
            axis.setTextPen(pg.mkPen(THEME["plot_axis_text"]))
        self.empty_label.setStyleSheet(f"color: {THEME['muted']}; font-size: 13px;")
        self.amplitude_curve.setPen(pg.mkPen(THEME["blue_bright"], width=2.4))
        self.noise_curve.setPen(pg.mkPen(THEME["plot_noise"], width=1.4, style=Qt.PenStyle.DashLine))
        self.amplitude_legend.setStyleSheet(f"color: {THEME['blue_bright']};")
        self.noise_legend.setStyleSheet(f"color: {THEME['plot_noise_legend']};")


# ---------------------------------------------------------------------------
# 事件日志面板
# ---------------------------------------------------------------------------
class EventLogPanel(CardFrame):
    """右侧节点日志面板（实时事件流）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.setMinimumWidth(300)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 14, 16)
        layout.setSpacing(10)

        title = QLabel("节点日志 (Node Cluster Log)")
        title.setObjectName("SectionTitle")
        subtitle = QLabel("实时事件流记录")
        subtitle.setObjectName("SubtleText")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(2, 8, 8, 6)
        self.content_layout.setSpacing(16)
        self.content_layout.addStretch(1)
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll, 1)
        self._events: list[dict[str, Any]] = []
        self._last_render_key: tuple[Any, ...] | None = None

    def set_events(self, events: list[dict[str, Any]]) -> None:
        rows = list(reversed(events[-40:]))
        render_key = tuple(
            (
                event.get("time"),
                event.get("title"),
                event.get("message"),
                event.get("level"),
                event.get("node_id"),
            )
            for event in rows
        )
        if render_key == self._last_render_key:
            return
        self._last_render_key = render_key
        self._events = list(events)
        clear_layout(self.content_layout)

        if not events:
            empty = QLabel("暂无实时事件")
            empty.setObjectName("SubtleText")
            self.content_layout.addWidget(empty)
            self.content_layout.addStretch(1)
            return

        for event in rows:
            self.content_layout.addWidget(self._build_row(event))
        self.content_layout.addStretch(1)

    def _build_row(self, event: dict[str, Any]) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        time_label = QLabel(
            time.strftime("%H:%M:%S", time.localtime(_float(event.get("time"), time.time())))
        )
        time_label.setObjectName("EventTime")
        time_label.setStyleSheet(f"color: {THEME['muted_2']}; font-size: 11px;")
        time_label.setFixedWidth(58)
        time_label.setAlignment(Qt.AlignmentFlag.AlignTop)

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        title = QLabel(str(event.get("title", "SYSTEM EVENT")))
        title.setStyleSheet(f"color: {_event_color(event)}; font-size: 14px; font-weight: 750;")
        message = QLabel(str(event.get("message", "")))
        message.setObjectName("SubtleText")
        message.setWordWrap(True)
        text_box.addWidget(title)
        text_box.addWidget(message)

        layout.addWidget(time_label)
        layout.addLayout(text_box, 1)
        return row

    def refresh_theme(self) -> None:
        self._last_render_key = None
        self.set_events(self._events)


# ---------------------------------------------------------------------------
# 拓扑图
# ---------------------------------------------------------------------------
class TopologyWidget(QWidget):
    """Gateway 雷达式节点接入态势图。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.nodes: dict[int, dict[str, Any]] = {}
        self.presence_threshold = PRESENCE_THRESHOLD
        self.setMinimumHeight(220)
        self._anim_started_at = time.time()
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_nodes(
        self,
        nodes: dict[int, dict[str, Any]],
        presence_threshold: float = PRESENCE_THRESHOLD,
    ) -> None:
        self.nodes = nodes
        self.presence_threshold = max(0.0, min(float(presence_threshold), 1.0))
        if self.isVisible():
            self.update()

    def _tick(self) -> None:
        if self.isVisible():
            self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect().adjusted(8, 6, -8, -6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(THEME["topology_bg"]))
        painter.drawRoundedRect(rect, 10, 10)

        center = QPointF(rect.center())
        outer_radius = min(rect.width(), rect.height()) * 0.36

        painter.setPen(QPen(QColor(THEME["topology_ring"]), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for scale in (0.34, 0.67, 1.0):
            ring = int(outer_radius * scale)
            painter.drawEllipse(center, ring, ring)

        painter.setPen(QPen(QColor(THEME["topology_cross"]), 1))
        painter.drawLine(int(center.x() - outer_radius), int(center.y()), int(center.x() + outer_radius), int(center.y()))
        painter.drawLine(int(center.x()), int(center.y() - outer_radius), int(center.x()), int(center.y() + outer_radius))

        sweep_angle = (time.time() - self._anim_started_at) * 1.35
        sweep_end = self._point_at(center, outer_radius, sweep_angle)
        sweep_pen = QPen(QColor(92, 173, 255, 120), 2)
        painter.setPen(sweep_pen)
        painter.drawLine(center, sweep_end)

        painter.setPen(QPen(QColor("#8DA8D8"), 1))
        painter.setBrush(QColor(THEME["topology_gateway"]))
        painter.drawEllipse(center, 30, 30)
        painter.setBrush(QColor(THEME["selection_text"]))
        painter.drawEllipse(center, 8, 8)
        painter.setBrush(QColor(THEME["topology_bg"]))
        painter.drawEllipse(center, 3, 3)
        painter.setPen(QPen(QColor(THEME["topology_gateway_text"]), 1))
        painter.setFont(QFont("Microsoft YaHei", 8))
        painter.drawText(int(center.x()) - 20, int(center.y()) + 52, GATEWAY_ID)

        discovered_nodes = self._discovered_nodes()
        if not discovered_nodes:
            painter.setPen(QPen(QColor(THEME["muted"]), 1))
            painter.setFont(QFont("Microsoft YaHei", 9))
            painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, "等待 Gateway 节点接入")
            painter.end()
            return

        for node_id, state in discovered_nodes:
            angle = self._node_angle(node_id)
            distance = self._node_distance(_float(state.get("rssi")), outer_radius)
            point = self._point_at(center, distance, angle)
            online = bool(state.get("online"))
            active = self._is_active_node(state)
            color = self._node_color(state)
            alpha = 130 if online else 55

            line_color = QColor(color)
            line_color.setAlpha(alpha)
            painter.setPen(QPen(line_color, 1))
            painter.drawLine(center, point)

            pulse = self._pulse(node_id, online)
            glow_radius = 15 + int(7 * pulse)
            if active:
                glow_radius += 8

            glow = QColor(color)
            glow.setAlpha(82 if online else 28)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(point, glow_radius, glow_radius)
            if active:
                outer_glow = QColor(color)
                outer_glow.setAlpha(42 + int(35 * pulse))
                painter.setBrush(outer_glow)
                painter.drawEllipse(point, glow_radius + 12, glow_radius + 12)

            painter.setBrush(QColor(color))
            node_radius = 6 + int(2 * pulse) if online else 5
            painter.drawEllipse(point, node_radius, node_radius)

            painter.setPen(QPen(QColor(THEME["topology_node_text"]), 1))
            painter.setFont(QFont("Microsoft YaHei", 8))
            label = str(state.get("label") or "").strip() or NODE_LABELS.get(node_id, f"node{node_id}")
            painter.drawText(int(point.x()) - 24, int(point.y()) + 28, label)

        painter.end()

    def _discovered_nodes(self) -> list[tuple[int, dict[str, Any]]]:
        discovered: list[tuple[int, dict[str, Any]]] = []
        for raw_id, state in self.nodes.items():
            try:
                node_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if state.get("last_received") is None:
                continue
            discovered.append((node_id, state))
        return sorted(discovered, key=lambda item: item[0])

    def _node_angle(self, node_id: int) -> float:
        fixed_angles = {
            1: -35.0,
            2: -145.0,
            3: 145.0,
            4: 35.0,
        }
        return math.radians(fixed_angles.get(node_id, (node_id * 137.5 - 90.0) % 360.0))

    def _node_distance(self, rssi: float, outer_radius: float) -> float:
        if rssi >= -1.0:
            return outer_radius * 0.78
        clamped = max(-115.0, min(-45.0, rssi))
        strength = (clamped + 115.0) / 70.0
        return outer_radius * (0.96 - strength * 0.44)

    def _point_at(self, center: QPointF, distance: float, angle: float) -> QPointF:
        return QPointF(center.x() + math.cos(angle) * distance, center.y() + math.sin(angle) * distance)

    def _pulse(self, node_id: int, online: bool) -> float:
        if not online:
            return 0.0
        phase = (time.time() - self._anim_started_at) * 3.2 + node_id * 0.7
        return (math.sin(phase) + 1.0) * 0.5

    def _node_color(self, state: dict[str, Any]) -> str:
        if not bool(state.get("online")):
            return THEME["muted"]
        if self._is_active_node(state):
            return THEME["orange"]
        return _rssi_color(_float(state.get("rssi")), True)

    def _is_active_node(self, state: dict[str, Any]) -> bool:
        return life_motion_triggered(state, presence_threshold=self.presence_threshold)


# ---------------------------------------------------------------------------
# 传感器页：节点矩阵相关组件
# ---------------------------------------------------------------------------
class ModeTag(QLabel):
    """运行模式标签（POWER_SAVE / HIGH_PERF / SLEEP / DEBUG_MODE / NORMAL）。"""

    def __init__(self, mode: str = "NORMAL", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mode = mode
        self.set_mode(mode)

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.setText(mode)
        self.setStyleSheet(
            "QLabel {"
            f" background: {THEME['tag_bg']};"
            f" color: {THEME['muted']};"
            f" border: 1px solid {THEME['tag_border']};"
            " border-radius: 6px; padding: 3px 8px;"
            " font-size: 11px; font-weight: 700; letter-spacing: 0.5px; }"
        )

    def refresh_theme(self) -> None:
        self.set_mode(self._mode)


class BatteryBar(QWidget):
    """电池状态条；固件未上报时显示“未上报”。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = 0.0
        self.setMinimumHeight(20)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(7)
        self.bar.setFixedWidth(86)
        self.percent = QLabel("--%")
        self.percent.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 13px;")
        layout.addWidget(self.bar)
        layout.addWidget(self.percent)
        layout.addStretch(1)
        self._unknown = True

    def set_unknown(self) -> None:
        self._unknown = True
        self._value = 0.0
        self.bar.setValue(0)
        self.percent.setText("未上报")
        self.percent.setStyleSheet(f"color: {THEME['muted']}; font-size: 13px;")
        self.bar.setStyleSheet(
            f"QProgressBar {{ background: {THEME['progress_bg']}; border: 0; border-radius: 4px; }}"
            f" QProgressBar::chunk {{ background: {THEME['muted_2']}; border-radius: 4px; }}"
        )

    def set_value(self, value: float) -> None:
        self._unknown = False
        value = max(0.0, min(100.0, float(value)))
        self._value = value
        self.bar.setValue(int(value))
        self.percent.setText(f"{value:.0f}%")
        self.percent.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 13px;")
        if value <= 35:
            chunk = THEME["red_soft"]
        elif value <= 60:
            chunk = THEME["yellow"]
        else:
            chunk = THEME["blue_soft"]
        self.bar.setStyleSheet(
            f"QProgressBar {{ background: {THEME['progress_bg']}; border: 0; border-radius: 4px; }}"
            f" QProgressBar::chunk {{ background: {chunk}; border-radius: 4px; }}"
        )

    def refresh_theme(self) -> None:
        if self._unknown:
            self.set_unknown()
        else:
            self.set_value(self._value)


class HealthBadge(QWidget):
    """运行健康度：彩色圆点 + 文本。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._health = HEALTH_INACTIVE
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.dot = QLabel("●")
        self.text = QLabel(HEALTH_INACTIVE)
        self.text.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 13px;")
        layout.addWidget(self.dot)
        layout.addWidget(self.text)
        layout.addStretch(1)

    def set_health(self, health: str) -> None:
        self._health = health
        color = HEALTH_COLORS.get(health, THEME["muted"])
        self.dot.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.text.setText(health)
        is_critical = health == HEALTH_CRITICAL
        is_inactive = health == HEALTH_INACTIVE
        style = f"color: {color if is_critical else THEME['text_soft']}; font-size: 13px;"
        if is_inactive:
            style += " font-style: italic;"
        if is_critical:
            style += " font-weight: 700;"
        self.text.setStyleSheet(style)

    def refresh_theme(self) -> None:
        self.set_health(self._health)


class ToggleSwitch(QAbstractButton):
    """iOS 风格开关（自定义绘制 + 动画）。"""

    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._offset = 1.0 if checked else 0.0  # 先于任何绘制赋值，避免 paintEvent 读到未定义属性
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(46, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(140)
        self.toggled.connect(self._animate)

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def get_offset(self) -> float:
        return self._offset

    def set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = pyqtProperty(float, fget=get_offset, fset=set_offset)

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        track_on = QColor(THEME["green"])
        track_off = QColor(THEME["scroll_handle"])
        track = _blend(track_off, track_on, self._offset)
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 13, 13)

        margin = 3.0
        diameter = self.height() - margin * 2
        x = margin + self._offset * (self.width() - diameter - margin * 2)
        painter.setBrush(QColor(THEME["selection_text"]))
        painter.drawEllipse(QRectF(x, margin, diameter, diameter))
        painter.end()


class ThresholdSlider(QWidget):
    """带标题、当前值胶囊和上下限说明的阈值滑条。"""

    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        title: str,
        minimum: float,
        maximum: float,
        value: float,
        value_fmt: Callable[[float], str],
        min_label: str,
        max_label: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._fmt = value_fmt
        self._steps = 1000

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        head = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 14px; font-weight: 600;")
        self.value_pill = QLabel(value_fmt(value))
        self.value_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value_pill.setStyleSheet(
            "QLabel {"
            f" background: {THEME['tag_bg']}; color: {THEME['blue_soft']};"
            f" border: 1px solid {THEME['tag_border']}; border-radius: 6px;"
            " padding: 2px 10px; font-size: 13px; font-weight: 700; }"
        )
        head.addWidget(self.title_label)
        head.addStretch(1)
        head.addWidget(self.value_pill)
        layout.addLayout(head)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, self._steps)
        self.slider.setValue(self._to_slider(value))
        self.slider.valueChanged.connect(self._on_slider)
        layout.addWidget(self.slider)

        bounds = QHBoxLayout()
        lo = QLabel(min_label)
        lo.setObjectName("SubtleText")
        hi = QLabel(max_label)
        hi.setObjectName("SubtleText")
        hi.setAlignment(Qt.AlignmentFlag.AlignRight)
        bounds.addWidget(lo)
        bounds.addStretch(1)
        bounds.addWidget(hi)
        layout.addLayout(bounds)

    def _to_slider(self, value: float) -> int:
        ratio = (value - self._min) / (self._max - self._min) if self._max > self._min else 0.0
        return int(round(max(0.0, min(1.0, ratio)) * self._steps))

    def _to_value(self, slider_value: int) -> float:
        ratio = slider_value / self._steps
        return self._min + ratio * (self._max - self._min)

    def _on_slider(self, slider_value: int) -> None:
        value = self._to_value(slider_value)
        self.value_pill.setText(self._fmt(value))
        self.valueChanged.emit(value)

    def refresh_theme(self) -> None:
        self.title_label.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 14px; font-weight: 600;")
        self.value_pill.setStyleSheet(
            "QLabel {"
            f" background: {THEME['tag_bg']}; color: {THEME['blue_soft']};"
            f" border: 1px solid {THEME['tag_border']}; border-radius: 6px;"
            " padding: 2px 10px; font-size: 13px; font-weight: 700; }"
        )


class SettingRow(QWidget):
    """配置行：左侧文案 + 右侧开关。"""

    toggled = pyqtSignal(bool)

    def __init__(self, title: str, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 14px; font-weight: 600;")
        self.switch = ToggleSwitch(checked)
        self.switch.toggled.connect(self.toggled.emit)
        layout.addWidget(self.title_label)
        layout.addStretch(1)
        layout.addWidget(self.switch)

    def refresh_theme(self) -> None:
        self.title_label.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 14px; font-weight: 600;")
        self.switch.update()


class SystemWarningBar(QFrame):
    """底部系统警告条（设计稿底部红色告警 + 右侧延迟/内存 + 关闭按钮）。"""

    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("WarningBar")
        self.setFixedHeight(74)
        self.setStyleSheet(
            "QFrame#WarningBar {"
            f" background: {THEME['panel']};"
            f" border-top: 1px solid {THEME['border_soft']}; }}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(22, 10, 18, 10)
        layout.setSpacing(14)

        self.icon = QLabel("⚠")
        self.icon.setFixedSize(46, 46)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon.setStyleSheet(
            "QLabel {"
            f" background: {THEME['warning_bg']}; color: {THEME['red']};"
            " border-radius: 10px; font-size: 20px; }"
        )
        layout.addWidget(self.icon)

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.tag = QLabel("系统警告")
        self.tag.setStyleSheet(
            "QLabel {"
            f" background: {THEME['warning_bg']}; color: {THEME['red']};"
            " border-radius: 5px; padding: 2px 8px; font-size: 12px; font-weight: 700; }"
        )
        self.time_label = QLabel("--:--:--")
        self.time_label.setObjectName("SubtleText")
        head.addWidget(self.tag)
        head.addWidget(self.time_label)
        head.addStretch(1)
        self.message = QLabel("等待系统事件…")
        self.message.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 13px;")
        self.message.setWordWrap(True)
        text_box.addLayout(head)
        text_box.addWidget(self.message)
        layout.addLayout(text_box, 1)

        metrics = QHBoxLayout()
        metrics.setSpacing(22)
        self.latency = _metric_block("服务器延迟", "12ms", "Stable", THEME["text_soft"])
        self.memory = _metric_block("内存占用", "4.2 GB", "", THEME["text_soft"])
        metrics.addLayout(self.latency["layout"])
        metrics.addLayout(self.memory["layout"])
        layout.addLayout(metrics)

        close_btn = QPushButton("✕")
        close_btn.setObjectName("IconClose")
        close_btn.setFixedSize(40, 40)
        close_btn.clicked.connect(self._hide_self)
        layout.addWidget(close_btn)

    def _hide_self(self) -> None:
        self.hide()
        self.closed.emit()

    def show_warning(self, message: str, when: float | None = None) -> None:
        self.message.setText(message)
        self.time_label.setText(
            time.strftime("%H:%M:%S", time.localtime(when if when else time.time()))
        )
        self.show()

    def set_metrics(self, latency_ms: float, memory_gb: float) -> None:
        self.latency["value"].setText(f"{latency_ms:.0f}ms")
        self.memory["value"].setText(f"{memory_gb:.1f} GB")

    def refresh_theme(self) -> None:
        self.setStyleSheet(
            "QFrame#WarningBar {"
            f" background: {THEME['panel']};"
            f" border-top: 1px solid {THEME['border_soft']}; }}"
        )
        self.icon.setStyleSheet(
            "QLabel {"
            f" background: {THEME['warning_bg']}; color: {THEME['red']};"
            " border-radius: 10px; font-size: 20px; }"
        )
        self.tag.setStyleSheet(
            "QLabel {"
            f" background: {THEME['warning_bg']}; color: {THEME['red']};"
            " border-radius: 5px; padding: 2px 8px; font-size: 12px; font-weight: 700; }"
        )
        self.message.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 13px;")
        self.latency["value"].setStyleSheet(f"color: {THEME['text_soft']}; font-size: 16px; font-weight: 700;")
        self.memory["value"].setStyleSheet(f"color: {THEME['text_soft']}; font-size: 16px; font-weight: 700;")


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------
def _metric_block(title: str, value: str, suffix: str, color: str) -> dict[str, Any]:
    layout = QVBoxLayout()
    layout.setSpacing(2)
    caption = QLabel(title)
    caption.setObjectName("SubtleText")
    row = QHBoxLayout()
    row.setSpacing(6)
    value_label = QLabel(value)
    value_label.setStyleSheet(f"color: {color}; font-size: 16px; font-weight: 700;")
    row.addWidget(value_label)
    if suffix:
        suffix_label = QLabel(suffix)
        suffix_label.setObjectName("SubtleText")
        row.addWidget(suffix_label)
    row.addStretch(1)
    layout.addWidget(caption)
    layout.addLayout(row)
    return {"layout": layout, "value": value_label}


def _legend_label(text: str, color: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SubtleText")
    label.setStyleSheet(f"color: {color};")
    return label


def _event_color(event: dict[str, Any]) -> str:
    level = str(event.get("level", "INFO")).upper()
    title = str(event.get("title", ""))
    if level == "ALARM":
        return THEME["red"]
    if "PRESENCE" in title or "微动" in title:
        return THEME["blue_soft"]
    if level == "WARN":
        return THEME["orange"]
    if level == "OK":
        return THEME["green"]
    return THEME["text"]


def _rssi_color(rssi: float, online: bool) -> str:
    if not online:
        return THEME["muted_2"]
    if rssi >= -60:
        return THEME["blue_soft"]
    if rssi >= -75:
        return THEME["green"]
    if rssi >= -90:
        return THEME["yellow"]
    return THEME["red"]


def _blend(c0: QColor, c1: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(c0.red() + (c1.red() - c0.red()) * t),
        int(c0.green() + (c1.green() - c0.green()) * t),
        int(c0.blue() + (c1.blue() - c0.blue()) * t),
    )


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
