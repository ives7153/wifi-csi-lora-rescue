"""EchoGuard 上位机主窗口。

中文注释：主窗口只负责整体骨架——顶部栏、左侧导航、页面容器（QStackedWidget）和
信号转发。每个页面自己处理快照刷新；串口 / Demo / 规则 / 导出动作都通过信号交给
DataManager，保证界面不会被后台 I/O 卡顿。
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

try:
    from ..config import (
        APP_TITLE,
        BRAND_NAME,
        BRAND_VERSION,
        NAV_ITEMS,
        THEME,
        build_qss,
        save_ui_settings,
        set_theme_mode,
        theme_mode,
    )
    from .components import StatusPill
    from .icons import IconButton, SvgIcon, refresh_widget_icons
    from .pages import (
        AIAssistPage,
        AnalysisPage,
        DashboardPage,
        DiagnosticsPage,
        HistoryPage,
        SensorMatrixPage,
    )
except ImportError:
    if __package__ and __package__.startswith("upper_computer"):
        raise
    from config import (
        APP_TITLE,
        BRAND_NAME,
        BRAND_VERSION,
        NAV_ITEMS,
        THEME,
        build_qss,
        save_ui_settings,
        set_theme_mode,
        theme_mode,
    )
    from ui.components import StatusPill
    from ui.icons import IconButton, SvgIcon, refresh_widget_icons
    from ui.pages import (
        AIAssistPage,
        AnalysisPage,
        DashboardPage,
        DiagnosticsPage,
        HistoryPage,
        SensorMatrixPage,
    )


class NavItem(QFrame):
    """左侧导航条目，点击切换页面。"""

    clicked = pyqtSignal(str)

    def __init__(self, key: str, text: str, icon: str, selected: bool = False) -> None:
        super().__init__()
        self.key = key
        self.setObjectName("NavItem")
        self.setProperty("selected", selected)
        self.setFixedHeight(52)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 14, 0)
        layout.setSpacing(14)

        self.icon_label = SvgIcon(icon, size=19, color=self._icon_color(selected))
        self.icon_label.setObjectName("NavIcon")
        self.icon_label.setFixedWidth(34)
        self.text_label = QLabel(text)
        self.text_label.setObjectName("NavText")
        self.text_label.setProperty("selected", selected)
        layout.addWidget(self.icon_label)
        layout.addWidget(self.text_label)
        layout.addStretch(1)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.icon_label.set_icon_color(self._icon_color(selected))
        self.text_label.setProperty("selected", selected)
        for widget in (self, self.text_label):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        del event
        self.clicked.emit(self.key)

    @staticmethod
    def _icon_color(selected: bool) -> str:
        return THEME["blue_bright"] if selected else THEME["text_soft"]


class ToastStack(QWidget):
    """右上角全局 Toast 通知容器。"""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedWidth(340)
        self.setObjectName("ToastStack")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(10)
        self._layout.addStretch(1)
        self.hide()

    def show_toast(self, text: str, ok: bool = True, timeout_ms: int = 3200) -> None:
        toast = QFrame(self)
        toast.setObjectName("Toast")
        toast.setProperty("ok", ok)
        toast.setMinimumHeight(54)
        toast.setStyleSheet(self._toast_style(ok))
        layout = QVBoxLayout(toast)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        title = QLabel(self._title(text, ok))
        title.setStyleSheet(f"color: {THEME['text']}; font-size: 13px; font-weight: 700;")
        detail = QLabel(self._detail(text))
        detail.setWordWrap(True)
        detail.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 12px;")
        layout.addWidget(title)
        if detail.text():
            layout.addWidget(detail)
        insert_at = max(0, self._layout.count() - 1)
        self._layout.insertWidget(insert_at, toast)
        self.show()
        self.raise_()
        QTimer.singleShot(timeout_ms, lambda item=toast: self._remove_toast(item))

    def _remove_toast(self, toast: QFrame) -> None:
        self._layout.removeWidget(toast)
        toast.setParent(None)
        toast.deleteLater()
        if self._layout.count() <= 1:
            self.hide()

    def _toast_style(self, ok: bool) -> str:
        accent = THEME["green"] if ok else THEME["red"]
        return (
            f"QFrame#Toast {{ background: {THEME['card']}; border: 1px solid {THEME['border']};"
            " border-radius: 8px;"
            f" border-left: 4px solid {accent}; }}"
        )

    @staticmethod
    def _title(text: str, ok: bool) -> str:
        if not ok:
            return "操作失败"
        if "CSV" in text:
            return "CSV 导出完成"
        if "截图" in text:
            return "截图已保存"
        return "操作完成"

    @staticmethod
    def _detail(text: str) -> str:
        if "：" in text:
            return text.split("：", 1)[1].strip()
        return text.strip()


class MainWindow(QMainWindow):
    """EchoGuard 控制台主窗口。"""

    # UI -> DataManager
    refresh_ports_requested = pyqtSignal()
    connect_requested = pyqtSignal(str)
    disconnect_requested = pyqtSignal()
    export_csv_requested = pyqtSignal()
    export_filtered_csv_requested = pyqtSignal(object)
    screenshot_requested = pyqtSignal(object)
    csi_shot_requested = pyqtSignal(object)
    analysis_shot_requested = pyqtSignal(object)
    active_node_changed = pyqtSignal(int)
    pause_toggled = pyqtSignal(bool)
    clear_events_requested = pyqtSignal()
    clear_history_requested = pyqtSignal()
    presence_threshold_changed = pyqtSignal(float)
    gas_threshold_changed = pyqtSignal(float)
    gas_calibration_requested = pyqtSignal()
    gas_calibration_all_requested = pyqtSignal()
    afh_toggled = pyqtSignal(bool)
    mesh_toggled = pyqtSignal(bool)
    sync_requested = pyqtSignal()
    matrix_filter_changed = pyqtSignal(str)
    matrix_maintenance_requested = pyqtSignal(int)
    diagnostics_requested = pyqtSignal()
    ai_config_save_requested = pyqtSignal(object)
    ai_jina_start_requested = pyqtSignal()
    ai_jina_stop_requested = pyqtSignal()
    ai_embedding_test_requested = pyqtSignal()
    ai_models_requested = pyqtSignal()
    ai_llm_test_requested = pyqtSignal()
    ai_action_requested = pyqtSignal(object)
    recording_toggled = pyqtSignal(bool)
    replay_requested = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 940)
        self.setMinimumSize(1240, 840)

        self.nav_items: dict[str, NavItem] = {}
        self.pages: dict[str, QWidget] = {}
        self._current_key = "dashboard"
        self._last_status_text = "串口状态：初始化中"
        self._last_status_ok = False
        self._latest_snapshot: dict[str, Any] | None = None

        self._build_ui()
        self._wire_pages()
        self.refresh_theme()

    # ------------------------------------------------------------------ 构建
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.root_widget = root
        self.setCentralWidget(root)

        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_top_bar())
        self.demo_banner = QLabel("")
        self.demo_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.demo_banner.setFixedHeight(34)
        self.demo_banner.hide()
        root_layout.addWidget(self.demo_banner)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_left_nav())
        body.addWidget(self._build_stack(), 1)
        root_layout.addLayout(body, 1)
        self.toast_stack = ToastStack(root)
        self._position_toasts()

    def _build_top_bar(self) -> QWidget:
        top = QFrame()
        top.setObjectName("TopBar")
        top.setFixedHeight(60)

        layout = QHBoxLayout(top)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(14)

        brand_text = f"{BRAND_NAME} {BRAND_VERSION}".strip()
        brand = QLabel(brand_text)
        brand.setObjectName("AppTitle")
        layout.addWidget(brand)

        self.top_sep = QLabel("|")
        self.top_sep.setStyleSheet(f"color: {THEME['muted_3']};")
        layout.addWidget(self.top_sep)

        self.page_subtitle = QLabel("实时生命体征监测")
        self.page_subtitle.setObjectName("TopSubtitle")
        layout.addWidget(self.page_subtitle)

        layout.addStretch(1)

        self.theme_btn = IconButton("sun")
        self.theme_btn.setObjectName("GhostButton")
        self.theme_btn.setFixedSize(38, 34)
        self.theme_btn.clicked.connect(self.toggle_theme)
        layout.addWidget(self.theme_btn)

        self.status_pill = StatusPill()
        layout.addWidget(self.status_pill)

        self.port_combo = QComboBox()
        self.port_combo.setFixedWidth(140)
        layout.addWidget(self.port_combo)

        refresh_btn = QPushButton("刷新")
        refresh_btn.setObjectName("GhostButton")
        refresh_btn.clicked.connect(self.refresh_ports_requested.emit)
        layout.addWidget(refresh_btn)

        connect_btn = QPushButton("连接")
        connect_btn.setObjectName("PrimaryButton")
        connect_btn.clicked.connect(lambda: self.connect_requested.emit(self.port_combo.currentText()))
        layout.addWidget(connect_btn)

        disconnect_btn = QPushButton("断开")
        disconnect_btn.setObjectName("DangerButton")
        disconnect_btn.clicked.connect(self.disconnect_requested.emit)
        layout.addWidget(disconnect_btn)

        return top

    def _build_left_nav(self) -> QWidget:
        nav = QFrame()
        nav.setObjectName("LeftNav")
        nav.setFixedWidth(258)

        layout = QVBoxLayout(nav)
        layout.setContentsMargins(18, 24, 18, 22)
        layout.setSpacing(8)

        caption = QLabel("系统核心")
        caption.setObjectName("NavCaption")
        self.session_label = QLabel("● 当前会话")
        self.session_label.setObjectName("NavSession")
        self.session_label.setStyleSheet(f"color: {THEME['green']}; font-size: 12px;")
        layout.addWidget(caption)
        layout.addWidget(self.session_label)
        layout.addSpacing(16)

        for entry in NAV_ITEMS:
            item = NavItem(
                entry["key"], entry["text"], entry["icon"],
                selected=entry["key"] == self._current_key,
            )
            item.clicked.connect(self._on_nav_clicked)
            self.nav_items[entry["key"]] = item
            layout.addWidget(item)

        layout.addSpacing(16)
        self.nav_divider = QFrame()
        self.nav_divider.setFixedHeight(1)
        self.nav_divider.setStyleSheet(f"background: {THEME['divider']};")
        layout.addWidget(self.nav_divider)
        layout.addSpacing(10)

        capture_controls = QHBoxLayout()
        capture_controls.setSpacing(8)
        self.record_btn = QPushButton("开始录制")
        self.record_btn.setObjectName("GhostButton")
        self.record_btn.setCheckable(True)
        self.record_btn.toggled.connect(self.recording_toggled.emit)
        replay_btn = QPushButton("回放记录")
        replay_btn.setObjectName("GhostButton")
        replay_btn.clicked.connect(self._choose_replay_file)
        capture_controls.addWidget(self.record_btn)
        capture_controls.addWidget(replay_btn)
        layout.addLayout(capture_controls)

        layout.addStretch(1)

        self.latest_frame_label = QLabel("最新帧：-")
        self.latest_frame_label.setObjectName("SubtleText")
        self.latest_frame_label.setWordWrap(True)
        layout.addWidget(self.latest_frame_label)

        self.status_detail = QLabel("串口状态：初始化中")
        self.status_detail.setObjectName("SubtleText")
        self.status_detail.setWordWrap(True)
        layout.addWidget(self.status_detail)

        return nav

    def _build_stack(self) -> QWidget:
        self.stack = QStackedWidget()

        self.dashboard_page = DashboardPage()
        self.sensor_page = SensorMatrixPage()
        self.analysis_page = AnalysisPage()
        self.ai_assist_page = AIAssistPage()
        self.diagnostics_page = DiagnosticsPage()
        self.history_page = HistoryPage()

        self.pages = {
            "dashboard": self.dashboard_page,
            "sensors": self.sensor_page,
            "analysis": self.analysis_page,
            "ai_assist": self.ai_assist_page,
            "diagnostics": self.diagnostics_page,
            "history": self.history_page,
        }
        for page in self.pages.values():
            self.stack.addWidget(page)

        self.stack.setCurrentWidget(self.dashboard_page)
        return self.stack

    def _wire_pages(self) -> None:
        # Dashboard 导出动作
        self.dashboard_page.export_csv_requested.connect(self.export_csv_requested.emit)
        self.dashboard_page.screenshot_requested.connect(self.screenshot_requested.emit)
        self.dashboard_page.csi_shot_requested.connect(self.csi_shot_requested.emit)
        self.dashboard_page.active_node_changed.connect(self.active_node_changed.emit)
        self.dashboard_page.pause_toggled.connect(self.pause_toggled.emit)
        self.dashboard_page.clear_events_requested.connect(self.clear_events_requested.emit)
        self.dashboard_page.ai_config_save_requested.connect(self.ai_config_save_requested.emit)
        self.dashboard_page.ai_jina_start_requested.connect(self.ai_jina_start_requested.emit)
        self.dashboard_page.ai_jina_stop_requested.connect(self.ai_jina_stop_requested.emit)
        self.dashboard_page.ai_embedding_test_requested.connect(self.ai_embedding_test_requested.emit)
        self.dashboard_page.ai_models_requested.connect(self.ai_models_requested.emit)
        self.dashboard_page.ai_llm_test_requested.connect(self.ai_llm_test_requested.emit)
        self.dashboard_page.ai_action_requested.connect(self.ai_action_requested.emit)

        # 历史页导出
        self.history_page.export_csv_requested.connect(self.export_csv_requested.emit)
        self.history_page.export_filtered_csv_requested.connect(self.export_filtered_csv_requested.emit)
        self.history_page.clear_history_requested.connect(self.clear_history_requested.emit)

        # 传感器页配置
        self.sensor_page.presence_threshold_changed.connect(self.presence_threshold_changed.emit)
        self.sensor_page.gas_threshold_changed.connect(self.gas_threshold_changed.emit)
        self.sensor_page.gas_calibration_requested.connect(self.gas_calibration_requested.emit)
        self.sensor_page.gas_calibration_all_requested.connect(self.gas_calibration_all_requested.emit)
        self.sensor_page.afh_toggled.connect(self.afh_toggled.emit)
        self.sensor_page.mesh_toggled.connect(self.mesh_toggled.emit)
        self.sensor_page.sync_requested.connect(self.sync_requested.emit)
        self.sensor_page.matrix_filter_changed.connect(self.matrix_filter_changed.emit)
        self.sensor_page.matrix_maintenance_requested.connect(self.matrix_maintenance_requested.emit)

        # 分析 / 诊断页动作
        self.analysis_page.active_node_changed.connect(self.active_node_changed.emit)
        self.analysis_page.analysis_shot_requested.connect(self.analysis_shot_requested.emit)
        self.ai_assist_page.ai_action_requested.connect(self.ai_action_requested.emit)
        self.diagnostics_page.diagnostics_requested.connect(self.diagnostics_requested.emit)

    # ------------------------------------------------------------------ 导航
    _SUBTITLE = {
        "dashboard": "实时生命体征监测",
        "sensors": "节点管理与配置",
        "analysis": "数据分析与趋势",
        "ai_assist": "AI 辅助研判",
        "diagnostics": "诊断与维护",
        "history": "历史记录",
    }

    def _on_nav_clicked(self, key: str) -> None:
        if key not in self.pages or key == self._current_key:
            if key in self.pages:
                self.stack.setCurrentWidget(self.pages[key])
                self._refresh_current_page()
            return

        self.nav_items[self._current_key].set_selected(False)
        self.nav_items[key].set_selected(True)
        self._current_key = key
        self.stack.setCurrentWidget(self.pages[key])
        self.page_subtitle.setText(self._SUBTITLE.get(key, ""))
        self._refresh_current_page()

    # ------------------------------------------------------------------ DataManager -> UI
    def update_ports(self, ports: list[str], selected: str | None = None) -> None:
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        values = ports or ["无可用串口"]
        self.port_combo.addItems(values)
        if selected and selected in values:
            self.port_combo.setCurrentText(selected)
        self.port_combo.blockSignals(False)

    def set_status(self, text: str, ok: bool = True) -> None:
        self._last_status_text = text
        self._last_status_ok = ok
        self.status_pill.set_state("● 实时生命体征监测" if ok else "● 待连接真实串口", ok)
        self.status_detail.setText(text)
        self.status_detail.setStyleSheet(
            f"color: {THEME['green'] if ok else THEME['orange']}; font-size: 12px;"
        )

    def set_latest_frame(self, text: str) -> None:
        self.latest_frame_label.setText(text)

    def show_export_message(self, text: str, ok: bool = True) -> None:
        self.toast_stack.show_toast(text, ok)
        self._position_toasts()

    def set_recording_state(self, active: bool, message: str) -> None:
        self.record_btn.blockSignals(True)
        self.record_btn.setChecked(active)
        self.record_btn.setText("停止录制" if active else "开始录制")
        self.record_btn.blockSignals(False)
        self.show_export_message(message, active or "已保存" in message)

    def show_ai_operation_message(self, text: str, ok: bool = True) -> None:
        self.dashboard_page.set_ai_operation_message(text, ok)

    def set_ai_models(self, models: object) -> None:
        self.dashboard_page.set_ai_models(models)

    def set_ai_operation_result(self, result: object) -> None:
        self.dashboard_page.set_ai_operation_result(result)

    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        # 中文注释：串口高频输入时只刷新当前页；后台页切换显示时再吃最新快照。
        self._latest_snapshot = snapshot
        self._update_data_source_banner(snapshot)
        self._refresh_current_page()

    def _choose_replay_file(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择 EchoGuard 录制文件",
            "",
            "EchoGuard JSONL (*.jsonl);;所有文件 (*.*)",
        )
        if path:
            self.replay_requested.emit(path)

    def _update_data_source_banner(self, snapshot: dict[str, Any]) -> None:
        config = snapshot.get("config") if isinstance(snapshot.get("config"), dict) else {}
        sources = {
            str(node.get("source") or "")
            for node in (snapshot.get("nodes") or {}).values()
            if isinstance(node, dict) and node.get("online")
        }
        if config.get("mobile_override_active") or "mobile_override" in sources:
            text = "演示数据已启用：存在感知值正由手机控制，停止服务或恢复全部可返回真实数据"
            color = THEME["orange"]
        elif "demo_mode" in sources and not config.get("startup_presence_demo_active"):
            text = "演示数据已启用：当前节点数值来自本地演示模式"
            color = THEME["orange"]
        elif config.get("replay_active") or "replay" in sources:
            text = "数据回放中：当前数据来自录制文件，不是实时 Gateway 数据"
            color = THEME["blue_bright"]
        else:
            self.demo_banner.hide()
            return
        self.demo_banner.setText(text)
        self.demo_banner.setStyleSheet(
            f"background: {THEME['warning_bg']}; color: {color}; border-bottom: 1px solid {color}; font-weight: 700;"
        )
        self.demo_banner.show()

    def _refresh_current_page(self) -> None:
        if self._latest_snapshot is None:
            return
        page = self.pages.get(self._current_key)
        if page is not None:
            page.update_snapshot(self._latest_snapshot)

    # ------------------------------------------------------------------ 主题
    def toggle_theme(self) -> None:
        target = "light" if theme_mode() == "dark" else "dark"
        self.apply_theme(target)

    def apply_theme(self, mode: str) -> None:
        applied = set_theme_mode(mode)
        save_ui_settings({"theme_mode": applied})
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_qss())
        self.refresh_theme()

    def refresh_theme(self) -> None:
        if hasattr(self, "theme_btn"):
            is_dark = theme_mode() == "dark"
            self.theme_btn.set_icon_name("sun" if is_dark else "moon")
            self.theme_btn.setToolTip("切换到浅色主题" if is_dark else "切换到深色主题")
        if hasattr(self, "top_sep"):
            self.top_sep.setStyleSheet(f"color: {THEME['muted_3']};")
        if hasattr(self, "session_label"):
            self.session_label.setStyleSheet(f"color: {THEME['green']}; font-size: 12px;")
        if hasattr(self, "nav_divider"):
            self.nav_divider.setStyleSheet(f"background: {THEME['divider']};")
        if hasattr(self, "status_pill"):
            self.status_pill.refresh_theme()
        if hasattr(self, "status_detail"):
            self.status_detail.setStyleSheet(
                f"color: {THEME['green'] if self._last_status_ok else THEME['orange']}; font-size: 12px;"
            )
        for page in getattr(self, "pages", {}).values():
            refresh = getattr(page, "refresh_theme", None)
            if callable(refresh):
                refresh()
        refresh_widget_icons(self)
        if hasattr(self, "toast_stack"):
            self.toast_stack.raise_()

    def resizeEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._position_toasts()

    def _position_toasts(self) -> None:
        if not hasattr(self, "toast_stack") or not hasattr(self, "root_widget"):
            return
        margin = 22
        top = 76
        width = self.toast_stack.width()
        x = max(margin, self.root_widget.width() - width - margin)
        height = max(120, self.root_widget.height() - top - margin)
        self.toast_stack.setGeometry(x, top, width, height)
        self.toast_stack.raise_()
