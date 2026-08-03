"""EchoGuard 上位机页面集合。

中文注释：每个页面都是一个完整的 body 区域（中间内容 + 可选右栏 + 可选底栏），
由 MainWindow 放进 QStackedWidget 切换。页面统一暴露 ``update_snapshot`` 接收
DataManager 快照刷新自身，不可见时会自动跳过重活，降低 CPU 占用。

页面清单：
* DashboardPage   —— 设计稿图 2：CSI 振幅趋势 + 生命体征指标 + 环境/无线 + 事件流 + 拓扑。
* SensorMatrixPage—— 设计稿图 1：活动节点矩阵表 + 右侧系统核心配置 + 底部系统警告条。
 * AnalysisPage    —— 数据分析：聚合统计 + 运动/存在趋势曲线。
* DiagnosticsPage —— 技术诊断：链路质量表 + 系统信息。
* HistoryPage     —— 历史记录：可滚动样本表 + CSV 导出。
"""

from __future__ import annotations

import time
from typing import Any

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from ..config import (
        CONTROL_ID,
        DEFAULT_AFH_ENABLED,
        DEFAULT_MESH_ENABLED,
        GAS_THRESHOLD_PPM,
        GATEWAY_ID,
        HEALTH_CRITICAL,
        NODE_LABELS,
        PRESENCE_THRESHOLD,
        THEME,
    )
    from .components import (
        BatteryBar,
        CardFrame,
        CsiTrendPlot,
        EventLogPanel,
        HealthBadge,
        MetricCard,
        ModeTag,
        SettingRow,
        SystemWarningBar,
        ThresholdSlider,
        TopologyWidget,
        clear_layout,
    )
    from .icons import refresh_widget_icons
    from ..rules.detection_fusion import ai_fallback_text, build_detection_summary, life_motion_triggered, verdict_color_key
except ImportError:
    if __package__ and __package__.startswith("upper_computer"):
        raise
    from config import (
        CONTROL_ID,
        DEFAULT_AFH_ENABLED,
        DEFAULT_MESH_ENABLED,
        GAS_THRESHOLD_PPM,
        GATEWAY_ID,
        HEALTH_CRITICAL,
        NODE_LABELS,
        PRESENCE_THRESHOLD,
        THEME,
    )
    from ui.components import (
        BatteryBar,
        CardFrame,
        CsiTrendPlot,
        EventLogPanel,
        HealthBadge,
        MetricCard,
        ModeTag,
        SettingRow,
        SystemWarningBar,
        ThresholdSlider,
        TopologyWidget,
        clear_layout,
    )
    from ui.icons import refresh_widget_icons
    from rules.detection_fusion import ai_fallback_text, build_detection_summary, life_motion_triggered, verdict_color_key  # type: ignore


# 矩阵表列宽（首列拉伸，其余固定，保证表头与每行对齐）
_MATRIX_COLUMNS = (
    ("节点 ID", 0),       # 0 = 拉伸
    ("运行模式", 150),
    ("LORA RSSI", 110),
    ("电池状态", 185),
    ("运行健康度", 130),
    ("建议动作", 140),
    ("操作", 56),
)


class AISettingsDialog(QDialog):
    """仪表盘 AI 设置卡片式弹窗。"""

    action_requested = pyqtSignal(object)
    save_requested = pyqtSignal(object)
    start_jina_requested = pyqtSignal()
    stop_jina_requested = pyqtSignal()
    test_embedding_requested = pyqtSignal()
    fetch_models_requested = pyqtSignal()
    test_llm_requested = pyqtSignal()

    def __init__(self, config: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 辅助研判设置")
        self.setMinimumWidth(760)
        self.setStyleSheet(
            f"QDialog {{ background: {THEME['bg']}; }}"
            f"QLabel {{ color: {THEME['text_soft']}; }}"
        )
        self._config = dict(config or {})
        self._action_buttons: dict[str, QPushButton] = {}
        self._last_operation_active = False
        self._available_models: list[str] = []
        self._model_source = ""
        self._status_ok = True
        self._jina_status_ok = True
        self._build_ui()
        self._load_config(self._config)
        self.refresh_theme()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(14)

        title = QLabel("AI 辅助研判设置")
        title.setObjectName("SectionTitle")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        subtitle = QLabel("规则融合仍负责实时主判断；AI 只做异步解释和模式辅助。")
        subtitle.setObjectName("SubtleText")
        outer.addWidget(title)
        outer.addWidget(subtitle)

        main_card = CardFrame()
        main_layout = QVBoxLayout(main_card)
        main_layout.setContentsMargins(18, 16, 18, 16)
        main_layout.setSpacing(14)

        self.enabled_box = QCheckBox("启用 AI 辅助研判")
        self.embedding_box = QCheckBox("启用本地 Jina embedding")
        self.llm_box = QCheckBox("启用大模型 API 辅助解释")
        flags = QHBoxLayout()
        flags.setSpacing(18)
        flags.addWidget(self.enabled_box)
        flags.addWidget(self.embedding_box)
        flags.addWidget(self.llm_box)
        flags.addStretch(1)
        main_layout.addLayout(flags)

        jina_title = QLabel("本地 Jina embedding")
        jina_title.setObjectName("SectionTitle")
        main_layout.addWidget(jina_title)
        self.server_path_edit = self._line_edit()
        self.model_path_edit = self._line_edit()
        self.jina_url_edit = self._line_edit()
        self.embedding_model_edit = self._line_edit()
        main_layout.addLayout(
            self._path_row(
                "llama-server",
                self.server_path_edit,
                "选择 llama-server.exe",
                "Executable (*.exe);;All Files (*)",
            )
        )
        main_layout.addLayout(
            self._path_row(
                "GGUF 模型",
                self.model_path_edit,
                "选择 Jina GGUF 模型",
                "GGUF Model (*.gguf);;All Files (*)",
            )
        )
        main_layout.addLayout(self._field_row("服务地址", self.jina_url_edit))
        main_layout.addLayout(self._field_row("Embedding 模型名", self.embedding_model_edit))
        self.jina_status_label = QLabel("部署状态：未检查")
        self.jina_status_label.setObjectName("SubtleText")
        self.jina_status_label.setWordWrap(True)
        main_layout.addWidget(self.jina_status_label)
        jina_actions = QHBoxLayout()
        jina_actions.setSpacing(10)
        online_deploy_btn = QPushButton("在线部署")
        online_deploy_btn.setObjectName("PrimaryButton")
        online_deploy_btn.clicked.connect(lambda: self._request_action("online_deploy_jina"))
        deploy_btn = QPushButton("导入离线包")
        deploy_btn.setObjectName("GhostButton")
        deploy_btn.clicked.connect(self._deploy_jina_package)
        import_model_btn = QPushButton("导入 GGUF")
        import_model_btn.setObjectName("GhostButton")
        import_model_btn.clicked.connect(self._import_jina_model)
        package_btn = QPushButton("生成离线包")
        package_btn.setObjectName("GhostButton")
        package_btn.clicked.connect(self._create_jina_package)
        one_key_start_btn = QPushButton("一键启动")
        one_key_start_btn.setObjectName("PrimaryButton")
        one_key_start_btn.clicked.connect(lambda: self._request_action("start_and_test_jina"))
        start_btn = QPushButton("启动本地 Jina")
        start_btn.setObjectName("GhostButton")
        start_btn.clicked.connect(lambda: self._request_action("start_jina"))
        stop_btn = QPushButton("停止服务")
        stop_btn.setObjectName("GhostButton")
        stop_btn.clicked.connect(lambda: self._request_action("stop_jina"))
        test_embed_btn = QPushButton("测试 Embedding")
        test_embed_btn.setObjectName("GhostButton")
        test_embed_btn.clicked.connect(lambda: self._request_action("test_embedding"))
        self._action_buttons["online_deploy_jina"] = online_deploy_btn
        self._action_buttons["deploy_jina_package"] = deploy_btn
        self._action_buttons["import_jina_model"] = import_model_btn
        self._action_buttons["create_jina_offline_package"] = package_btn
        self._action_buttons["start_and_test_jina"] = one_key_start_btn
        self._action_buttons["start_jina"] = start_btn
        self._action_buttons["stop_jina"] = stop_btn
        self._action_buttons["test_embedding"] = test_embed_btn
        jina_actions.addWidget(online_deploy_btn)
        jina_actions.addWidget(deploy_btn)
        jina_actions.addWidget(import_model_btn)
        jina_actions.addWidget(package_btn)
        jina_actions.addWidget(one_key_start_btn)
        jina_actions.addWidget(start_btn)
        jina_actions.addWidget(stop_btn)
        jina_actions.addWidget(test_embed_btn)
        jina_actions.addStretch(1)
        main_layout.addLayout(jina_actions)

        llm_title = QLabel("大模型 API")
        llm_title.setObjectName("SectionTitle")
        main_layout.addWidget(llm_title)
        self.provider_combo = QComboBox()
        self.provider_combo.addItem("智谱GLM官方", "zhipu_glm")
        self.provider_combo.addItem("OpenAI兼容", "openai_compatible")
        self.provider_combo.addItem("自定义", "custom")
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.llm_url_edit = self._line_edit()
        self.llm_key_edit = self._line_edit()
        self.llm_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.save_key_box = QCheckBox("保存 API Key 到本机")
        self.save_key_box.setToolTip("默认不保存；测试 API 时只临时使用当前输入。")
        self.llm_model_combo = QComboBox()
        self.llm_model_combo.setEditable(True)
        self.llm_model_combo.setMinimumWidth(260)
        main_layout.addLayout(self._field_row("供应商", self.provider_combo))
        main_layout.addLayout(self._field_row("API 地址", self.llm_url_edit))
        main_layout.addLayout(self._field_row("API Key", self.llm_key_edit))
        save_key_row = QHBoxLayout()
        save_key_row.setContentsMargins(122, 0, 0, 0)
        save_key_row.addWidget(self.save_key_box)
        save_key_row.addStretch(1)
        main_layout.addLayout(save_key_row)
        model_row = self._field_row("可用模型", self.llm_model_combo)
        self.model_select_btn = QPushButton("选择模型")
        self.model_select_btn.setObjectName("GhostButton")
        self.model_select_btn.setFixedWidth(104)
        self.model_select_btn.clicked.connect(self._show_model_menu)
        model_row.addWidget(self.model_select_btn)
        main_layout.addLayout(model_row)
        llm_actions = QHBoxLayout()
        llm_actions.setSpacing(10)
        fetch_btn = QPushButton("获取模型")
        fetch_btn.setObjectName("GhostButton")
        fetch_btn.clicked.connect(lambda: self._request_action("fetch_models"))
        test_llm_btn = QPushButton("测试 API")
        test_llm_btn.setObjectName("GhostButton")
        test_llm_btn.clicked.connect(lambda: self._request_action("test_llm"))
        self._action_buttons["fetch_models"] = fetch_btn
        self._action_buttons["test_llm"] = test_llm_btn
        llm_actions.addWidget(fetch_btn)
        llm_actions.addWidget(test_llm_btn)
        llm_actions.addStretch(1)
        main_layout.addLayout(llm_actions)

        self.status_label = QLabel("AI 设置未测试")
        self.status_label.setObjectName("SubtleText")
        self.status_label.setWordWrap(True)
        main_layout.addWidget(self.status_label)
        outer.addWidget(main_card)

        footer = QHBoxLayout()
        footer.addStretch(1)
        save_btn = QPushButton("保存设置")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(lambda: self._request_action("save"))
        self._action_buttons["save"] = save_btn
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("GhostButton")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(save_btn)
        footer.addWidget(close_btn)
        outer.addLayout(footer)

    def _load_config(self, config: dict[str, Any]) -> None:
        self.enabled_box.setChecked(bool(config.get("enabled", True)))
        self.embedding_box.setChecked(bool(config.get("embedding_enabled", True)))
        self.llm_box.setChecked(bool(config.get("llm_enabled", False)))
        self.server_path_edit.setText(str(config.get("llama_server_path", "upper_computer/runtime/llama-server.exe")))
        self.model_path_edit.setText(str(config.get("jina_model_path", "upper_computer/models/v5-nano-retrieval-Q4_K_M.gguf")))
        self.jina_url_edit.setText(str(config.get("jina_base_url", "http://127.0.0.1:18081")))
        self.embedding_model_edit.setText(str(config.get("embedding_model", "jina-embeddings-v5-text-nano-retrieval")))
        provider = str(config.get("llm_provider") or self._infer_provider(config))
        provider_index = self.provider_combo.findData(provider)
        self.provider_combo.setCurrentIndex(provider_index if provider_index >= 0 else 0)
        self.llm_url_edit.setText(str(config.get("llm_base_url", "")))
        self.llm_key_edit.setText("")
        self.llm_key_edit.setPlaceholderText("默认不保存；测试时临时输入")
        self.save_key_box.setChecked(bool(config.get("save_api_key", False)))
        model = self._normalize_model_label(str(config.get("llm_model", "")), self.provider_combo.currentData())
        if model:
            self.llm_model_combo.addItem(model)
            self.llm_model_combo.setCurrentText(model)
        self._on_provider_changed()

    def _collect_config(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled_box.isChecked(),
            "embedding_enabled": self.embedding_box.isChecked(),
            "llama_server_path": self.server_path_edit.text().strip(),
            "jina_model_path": self.model_path_edit.text().strip(),
            "jina_base_url": self.jina_url_edit.text().strip(),
            "embedding_model": self.embedding_model_edit.text().strip(),
            "llm_enabled": self.llm_box.isChecked(),
            "llm_provider": str(self.provider_combo.currentData() or "zhipu_glm"),
            "llm_base_url": self.llm_url_edit.text().strip(),
            "llm_api_key": self.llm_key_edit.text().strip(),
            "llm_model": self._normalize_model_label(
                self.llm_model_combo.currentText().strip(),
                self.provider_combo.currentData(),
            ),
            "save_api_key": self.save_key_box.isChecked(),
        }

    def _save_then_emit(self, signal: pyqtSignal | None) -> None:
        self.save_requested.emit(self._collect_config())
        if signal is not None:
            signal.emit()

    def request_deployment_check(self) -> None:
        self._request_action("check_jina_deployment")

    def _request_action(self, action: str, extra: dict[str, Any] | None = None) -> None:
        self._last_operation_active = True
        self._set_action_running(action, True)
        self.set_status(self._action_label(action) + "执行中...", True)
        if action in {
            "check_jina_deployment",
            "deploy_jina_package",
            "online_deploy_jina",
            "import_jina_model",
            "create_jina_offline_package",
        }:
            self.set_jina_status(self._action_label(action) + "执行中...", True)
        elif action in {"start_jina", "start_and_test_jina"}:
            self.set_jina_status("部署状态：服务启动中", True)
        payload = {"action": action, "config": self._collect_config()}
        if extra:
            payload.update(extra)
        self.action_requested.emit(payload)

    def _set_action_running(self, action: str, running: bool) -> None:
        button = self._action_buttons.get(action)
        if button is None:
            return
        button.setEnabled(not running)
        label = self._action_label(action)
        button.setText(f"{label}..." if running else label)
        refresh_widget_icons(button)

    def _action_label(self, action: str) -> str:
        return {
            "save": "保存设置",
            "check_jina_deployment": "检查部署",
            "online_deploy_jina": "在线部署",
            "deploy_jina_package": "导入离线包",
            "import_jina_model": "导入 GGUF",
            "create_jina_offline_package": "生成离线包",
            "start_and_test_jina": "一键启动",
            "start_jina": "启动本地 Jina",
            "stop_jina": "停止服务",
            "test_embedding": "测试 Embedding",
            "fetch_models": "获取模型",
            "test_llm": "测试 API",
        }.get(action, "AI 操作")

    def _show_model_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(_menu_qss())
        if not self._available_models:
            empty = QAction("暂无模型，请先点击获取模型", menu)
            empty.setEnabled(False)
            menu.addAction(empty)
        else:
            for model in self._available_models:
                action = QAction(model, menu)
                action.triggered.connect(lambda _checked=False, value=model: self._select_model_value(value))
                menu.addAction(action)
        menu.exec(self.model_select_btn.mapToGlobal(self.model_select_btn.rect().bottomLeft()))

    def _refresh_model_button(self, models: list[str], source: str = "") -> None:
        self._available_models = list(models)
        self._model_source = source
        if not self._available_models:
            self.model_select_btn.setText("选择模型")
            return
        self.model_select_btn.setText(f"选择模型 ({len(self._available_models)})")

    def _select_model_value(self, model: str) -> None:
        text = str(model or "").strip()
        if not text:
            return
        self.llm_model_combo.setCurrentText(text)
        self.set_status(f"已选择模型：{text}", True)

    def _on_provider_changed(self) -> None:
        provider = str(self.provider_combo.currentData() or "zhipu_glm")
        if provider == "zhipu_glm":
            self.llm_url_edit.setText("https://open.bigmodel.cn/api/paas/v4")
            current = self._normalize_model_label(self.llm_model_combo.currentText(), provider)
            presets = ("glm-5.1", "glm-5-turbo", "glm-4.5", "glm-4.5-air")
            self.llm_model_combo.blockSignals(True)
            self.llm_model_combo.clear()
            self.llm_model_combo.addItems(presets)
            self.llm_model_combo.setCurrentText(current if current in presets else "glm-5.1")
            self.llm_model_combo.blockSignals(False)

    def _infer_provider(self, config: dict[str, Any]) -> str:
        model = str(config.get("llm_model") or "").lower().replace(" ", "")
        base = str(config.get("llm_base_url") or "").lower()
        if "glm" in model or "bigmodel.cn" in base:
            return "zhipu_glm"
        return "openai_compatible"

    def _normalize_model_label(self, model: str, provider: object) -> str:
        text = str(model or "").strip()
        if str(provider) == "zhipu_glm":
            compact = text.lower().replace(" ", "-").replace("_", "-")
            aliases = {
                "glm-5.1": "glm-5.1",
                "glm5.1": "glm-5.1",
                "glm-5-turbo": "glm-5-turbo",
                "glm5-turbo": "glm-5-turbo",
            }
            return aliases.get(compact, compact or "glm-5.1")
        return text

    def _line_edit(self) -> QLineEdit:
        edit = QLineEdit()
        edit.setMinimumHeight(34)
        return edit

    def _field_row(self, label: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        caption = QLabel(label)
        caption.setObjectName("SubtleText")
        caption.setFixedWidth(112)
        row.addWidget(caption)
        row.addWidget(widget, 1)
        return row

    def _path_row(self, label: str, edit: QLineEdit, title: str, file_filter: str) -> QHBoxLayout:
        row = self._field_row(label, edit)
        browse = QPushButton("浏览")
        browse.setObjectName("GhostButton")
        browse.clicked.connect(lambda: self._browse_file(edit, title, file_filter))
        row.addWidget(browse)
        return row

    def _browse_file(self, edit: QLineEdit, title: str, file_filter: str) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, title, edit.text(), file_filter)
        if path:
            edit.setText(path)

    def _deploy_jina_package(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择 EchoGuard-AI-Runtime.zip",
            "",
            "Zip Package (*.zip);;All Files (*)",
        )
        if not path:
            self.set_jina_status("部署状态：已取消选择离线包", False)
            return
        self._request_action("deploy_jina_package", {"package_path": path})

    def _create_jina_package(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "保存 EchoGuard-AI-Runtime.zip",
            "EchoGuard-AI-Runtime.zip",
            "Zip Package (*.zip);;All Files (*)",
        )
        if not path:
            self.set_jina_status("部署状态：已取消生成离线包", False)
            return
        self._request_action("create_jina_offline_package", {"package_path": path})

    def _import_jina_model(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择 Jina GGUF 模型",
            self.model_path_edit.text(),
            "GGUF Model (*.gguf);;All Files (*)",
        )
        if not path:
            self.set_jina_status("部署状态：已取消导入 GGUF 模型", False)
            return
        self._request_action("import_jina_model", {"package_path": path})

    def set_status(self, text: str, ok: bool = True) -> None:
        self._status_ok = ok
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color: {THEME['blue_soft'] if ok else THEME['red']}; font-size: 12px;"
        )

    def set_jina_status(self, text: str, ok: bool = True) -> None:
        self._jina_status_ok = ok
        self.jina_status_label.setText(text)
        self.jina_status_label.setStyleSheet(
            f"color: {THEME['blue_soft'] if ok else THEME['red']}; font-size: 12px;"
        )

    def set_models(self, models: object) -> None:
        current = self.llm_model_combo.currentText().strip()
        self.llm_model_combo.blockSignals(True)
        self.llm_model_combo.clear()
        model_values: list[str] = []
        if isinstance(models, list):
            model_values = [str(model) for model in models]
            self.llm_model_combo.addItems(model_values)
        if current:
            self.llm_model_combo.setCurrentText(current)
        self.llm_model_combo.blockSignals(False)
        self._refresh_model_button(model_values)

    def set_operation_result(self, result: object) -> None:
        if not isinstance(result, dict):
            return
        action = str(result.get("action") or "")
        running = bool(result.get("running", False))
        self._set_action_running(action, running)
        if "models" in result:
            self._apply_model_result(result)
        if action in {
            "check_jina_deployment",
            "online_deploy_jina",
            "deploy_jina_package",
            "import_jina_model",
            "create_jina_offline_package",
            "start_and_test_jina",
            "start_jina",
            "stop_jina",
            "test_embedding",
        }:
            self._apply_jina_result(result)
        message = str(result.get("message") or "")
        if action == "fetch_models" and bool(result.get("ok", False)):
            count = len(result.get("models") or [])
            source = str(result.get("model_source") or "")
            prefix = "已加载官方预设" if source == "preset" else "获取模型成功"
            message = f"{prefix}：共 {count} 个模型"
        elif action == "test_llm" and bool(result.get("ok", False)):
            endpoint = str(result.get("endpoint") or "")
            model = self.llm_model_combo.currentText().strip()
            raw_message = message.replace("真实请求成功：", "")
            message = f"测试成功：{raw_message or endpoint or model}"
        elif message and not bool(result.get("ok", False)):
            message = f"{self._action_label(action)}失败：{message}"
        if message:
            self._last_operation_active = True
            self.set_status(message, bool(result.get("ok", False)))

    def set_runtime_state(self, ai_state: dict[str, Any]) -> None:
        if self._last_operation_active:
            return
        status = str(ai_state.get("status") or "")
        error = str(ai_state.get("error") or "")
        if error:
            self.set_status(error, False)
        elif status:
            self.set_status(status, True)

    def refresh_theme(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background: {THEME['bg']}; }}"
            f"QLabel {{ color: {THEME['text_soft']}; }}"
        )
        self.set_status(self.status_label.text(), self._status_ok)
        self.set_jina_status(self.jina_status_label.text(), self._jina_status_ok)
        refresh_widget_icons(self)

    def _apply_model_result(self, result: dict[str, Any]) -> None:
        models = result.get("models")
        model_values = [str(model) for model in models] if isinstance(models, list) else []
        current = self.llm_model_combo.currentText().strip()
        self.llm_model_combo.blockSignals(True)
        self.llm_model_combo.clear()
        self.llm_model_combo.addItems(model_values)
        if current:
            self.llm_model_combo.setCurrentText(current)
        elif model_values:
            self.llm_model_combo.setCurrentText(model_values[0])
        self.llm_model_combo.blockSignals(False)
        self._refresh_model_button(model_values, str(result.get("model_source") or ""))

    def _apply_jina_result(self, result: dict[str, Any]) -> None:
        action = str(result.get("action") or "")
        running = bool(result.get("running", False))
        if running:
            running_messages = {
                "check_jina_deployment": "部署状态：检查中",
                "online_deploy_jina": "部署状态：在线部署中",
                "deploy_jina_package": "部署状态：部署中",
                "import_jina_model": "部署状态：正在导入 GGUF 模型",
                "create_jina_offline_package": "部署状态：正在生成离线包",
                "start_jina": "部署状态：服务启动中",
                "start_and_test_jina": "部署状态：服务启动中",
                "test_embedding": "部署状态：测试中",
            }
            message = str(result.get("message") or "")
            self.set_jina_status(message or running_messages.get(action, "部署状态：处理中"), True)
            return

        server_path = str(result.get("server_path") or "")
        model_path = str(result.get("model_path") or "")
        if server_path:
            self.server_path_edit.setText(server_path)
        if model_path:
            self.model_path_edit.setText(model_path)

        ok = bool(result.get("ok", False))
        message = str(result.get("message") or "")
        if ok and action in {"check_jina_deployment", "deploy_jina_package", "online_deploy_jina", "import_jina_model"}:
            deployed = bool(result.get("deployed", False))
            prefix = "部署状态：已部署" if deployed else "部署状态：未部署"
            self.set_jina_status(f"{prefix} · {message}", deployed)
        elif ok and action == "create_jina_offline_package":
            package_path = str(result.get("package_path") or "")
            self.set_jina_status(f"部署状态：离线包已生成 · {package_path or message}", True)
        elif ok and action in {"start_jina", "start_and_test_jina", "test_embedding"}:
            dimension = result.get("dimension")
            endpoint = str(result.get("endpoint") or "")
            if dimension:
                self.set_jina_status(f"部署状态：本地 Jina 可用 · POST {endpoint} · {dimension} 维", True)
            else:
                self.set_jina_status(f"部署状态：{message or '本地 Jina 可用'}", True)
        elif ok and action == "stop_jina":
            self.set_jina_status("部署状态：服务已停止", True)
        elif message:
            self.set_jina_status(f"部署状态：{self._action_label(action)}失败 · {message}", False)


# ===========================================================================
# 仪表盘页（图 2）
# ===========================================================================
class RegionCalibrationDialog(QDialog):
    """活动 Gateway 三角形区域三阶段现场标定向导。"""

    phase_requested = pyqtSignal(str)
    cancel_requested = pyqtSignal()

    _PHASES = (
        ("empty", "1. 空场 60 秒", "区域内外均保持无人，不要在节点附近走动。"),
        ("inside", "2. 内部 180 秒", "一人在三角形内部持续自然走动，避开边线 30 cm 过渡带。"),
        ("outside", "3. 外部 180 秒", "一人在三角形外部持续走动，避开边线外 30 cm 过渡带。"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.gateway_id = "GW-02"
        self.setWindowTitle(f"三角形区域标定 · {self.gateway_id}")
        self.setModal(False)
        self.resize(620, 430)
        self.setMinimumSize(560, 390)
        self._buttons: dict[str, QPushButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        title = QLabel("三角形区域现场标定")
        title.setObjectName("PageTitle")
        self.intro = QLabel(
            f"仅使用 {self.gateway_id} 和 Node 1/2/3 的真实九链路 CSI。请按顺序完成三阶段；"
            "标定期间手机演示值不会参与区域判定。"
        )
        self.intro.setWordWrap(True)
        self.intro.setObjectName("SubtleText")
        layout.addWidget(title)
        layout.addWidget(self.intro)

        self.status_label = QLabel("等待九链路数据")
        self.status_label.setStyleSheet(f"color: {THEME['blue_soft']}; font-size: 16px; font-weight: 700;")
        self.progress_label = QLabel("有效链路：0/9")
        self.progress_label.setObjectName("SubtleText")
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_label)

        for phase, button_text, instruction in self._PHASES:
            card = CardFrame()
            row = QHBoxLayout(card)
            row.setContentsMargins(16, 12, 16, 12)
            text_box = QVBoxLayout()
            phase_label = QLabel(button_text)
            phase_label.setStyleSheet(f"color: {THEME['text']}; font-weight: 700;")
            hint = QLabel(instruction)
            hint.setWordWrap(True)
            hint.setObjectName("SubtleText")
            text_box.addWidget(phase_label)
            text_box.addWidget(hint)
            button = QPushButton("开始")
            button.setObjectName("PrimaryButton")
            button.setFixedWidth(92)
            button.clicked.connect(lambda _checked=False, value=phase: self.phase_requested.emit(value))
            self._buttons[phase] = button
            row.addLayout(text_box, 1)
            row.addWidget(button)
            layout.addWidget(card)

        footer = QHBoxLayout()
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {THEME['orange']};")
        cancel = QPushButton("取消本次标定")
        cancel.setObjectName("GhostButton")
        cancel.clicked.connect(self.cancel_requested.emit)
        close = QPushButton("收起窗口")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.hide)
        footer.addWidget(self.error_label, 1)
        footer.addWidget(cancel)
        footer.addWidget(close)
        layout.addLayout(footer)

    def update_region(self, region: dict[str, Any]) -> None:
        gateway = str(region.get("gateway_id") or self.gateway_id).strip().upper()
        if gateway and gateway != self.gateway_id:
            self.gateway_id = gateway
            self.setWindowTitle(f"三角形区域标定 · {self.gateway_id}")
            self.intro.setText(
                f"仅使用 {self.gateway_id} 和 Node 1/2/3 的真实九链路 CSI。请按顺序完成三阶段；"
                "标定期间手机演示值不会参与区域判定。"
            )
        calibration = region.get("calibration") if isinstance(region.get("calibration"), dict) else {}
        current = str(calibration.get("phase") or "")
        completed = {str(value) for value in calibration.get("completed") or []}
        counts = calibration.get("counts") if isinstance(calibration.get("counts"), dict) else {}
        remaining = max(0, int(float(calibration.get("remaining_seconds") or 0.0) + 0.999))
        status = str(region.get("status") or "not_calibrated")
        valid_links = int(region.get("valid_links") or 0)

        if current:
            label = str(calibration.get("phase_label") or current)
            self.status_label.setText(f"正在采集：{label} · 剩余 {remaining} 秒")
        elif status == "calibration_ready_next":
            self.status_label.setText("本阶段完成，请按顺序开始下一阶段")
        elif status == "clear" and len(completed) == 3:
            self.status_label.setText("标定完成，配置已保存并开始实时判定")
        else:
            self.status_label.setText(str(region.get("label") or "等待标定"))
        self.progress_label.setText(
            "有效链路：{}/9 · 样本 空场 {} / 内部 {} / 外部 {}".format(
                valid_links,
                int(counts.get("empty") or 0),
                int(counts.get("inside") or 0),
                int(counts.get("outside") or 0),
            )
        )
        self.error_label.setText(str(calibration.get("error") or ""))

        expected_index = len(completed)
        order = ("empty", "inside", "outside")
        for index, phase in enumerate(order):
            button = self._buttons[phase]
            button.setText("采集中" if phase == current else "已完成" if phase in completed else "开始")
            button.setEnabled(not current and index == expected_index and valid_links == 9)


class DashboardPage(QWidget):
    """实时生命体征仪表盘。"""

    export_csv_requested = pyqtSignal()
    screenshot_requested = pyqtSignal(object)
    csi_shot_requested = pyqtSignal(object)
    active_node_changed = pyqtSignal(int)
    pause_toggled = pyqtSignal(bool)
    clear_events_requested = pyqtSignal()
    ai_config_save_requested = pyqtSignal(object)
    ai_jina_start_requested = pyqtSignal()
    ai_jina_stop_requested = pyqtSignal()
    ai_embedding_test_requested = pyqtSignal()
    ai_models_requested = pyqtSignal()
    ai_llm_test_requested = pyqtSignal()
    ai_action_requested = pyqtSignal(object)
    region_calibration_phase_requested = pyqtSignal(str)
    region_calibration_cancel_requested = pyqtSignal()

    _VERDICT_PRIORITY = {
        "等待数据": 0,
        "未检测到稳定微动": 1,
        "数据不足": 2,
        "疑似局部微动": 3,
        "多节点疑似生命微动": 4,
    }
    _VERDICT_MIN_HOLD_SECONDS = 2.0
    _VERDICT_STABLE_SECONDS = 1.5
    _AI_TEXT_MIN_HOLD_SECONDS = 5.0
    _AI_CARD_TEXT_LIMIT = 60

    def __init__(self) -> None:
        super().__init__()
        self.metric_cards: dict[str, MetricCard] = {}
        self.group_values: dict[str, QLabel] = {}
        self._last_event_count = -1
        self._paused = False
        self._last_verdict = ("等待数据", "尚未收到有效节点数据", THEME["muted"])
        self._latest_ai_config: dict[str, Any] = {}
        self._ai_dialog: AISettingsDialog | None = None
        self._latest_nodes: dict[int, dict[str, Any]] = {}
        self._latest_active_node = 0
        self._latest_active_state: dict[str, Any] = {}
        self._export_ok = True
        self._known_focus_nodes: list[tuple[int, str]] = []
        self._last_dashboard_heavy_refresh_at = 0.0
        self._display_verdict_summary: Any | None = None
        self._pending_verdict_summary: Any | None = None
        self._pending_verdict_since = 0.0
        self._display_verdict_since = 0.0
        self._display_ai_text = ""
        self._display_ai_tooltip = ""
        self._display_ai_key = ""
        self._display_ai_since = 0.0
        self._verdict_chips: dict[str, QLabel] = {}
        self._last_pause_text = ""
        self._last_group_values: dict[str, tuple[str, str]] = {}
        self._region_dialog: RegionCalibrationDialog | None = None
        self._latest_region: dict[str, Any] = {}

        body = QHBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_center(), 1)
        body.addWidget(self._build_right_rail())

    # ----- 中间区 -----
    def _build_center(self) -> QWidget:
        center = QWidget()
        center.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(center)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.node_combo = QComboBox()
        self.node_combo.setFixedWidth(140)
        self.node_combo.addItem("等待节点接入", 0)
        self.node_combo.setEnabled(False)
        self.node_combo.currentIndexChanged.connect(self._on_focus_node_changed)
        self.pause_btn = QPushButton("暂停刷新")
        self.pause_btn.setObjectName("GhostButton")
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.detail_btn = QPushButton("节点详情")
        self.detail_btn.setObjectName("GhostButton")
        self.detail_btn.setEnabled(False)
        self.detail_btn.clicked.connect(self._show_active_node_detail)
        controls.addWidget(QLabel("关注节点"))
        controls.addWidget(self.node_combo)
        controls.addWidget(self.pause_btn)
        controls.addWidget(self.detail_btn)
        controls.addStretch(1)
        csv_btn = QPushButton("CSV 导出")
        csv_btn.setObjectName("PrimaryButton")
        csv_btn.clicked.connect(self.export_csv_requested.emit)
        csi_btn = QPushButton("扰动曲线截图")
        csi_btn.setObjectName("GhostButton")
        csi_btn.clicked.connect(lambda: self.csi_shot_requested.emit(self.csi_plot))
        shot_btn = QPushButton("整窗截图")
        shot_btn.setObjectName("GhostButton")
        shot_btn.clicked.connect(lambda: self.screenshot_requested.emit(self.window()))
        controls.addWidget(csv_btn)
        controls.addWidget(csi_btn)
        controls.addWidget(shot_btn)
        layout.addLayout(controls)

        self.region_card = CardFrame()
        self.region_card.setFixedHeight(108)
        region_layout = QHBoxLayout(self.region_card)
        region_layout.setContentsMargins(18, 12, 18, 12)
        region_layout.setSpacing(18)
        region_title_box = QVBoxLayout()
        self.region_title = QLabel("三角形区域检测 · GW-02")
        self.region_title.setObjectName("SectionTitle")
        region_hint = QLabel("9 条定向 CSI 链路 · 单人走动 · 边线 ±30 cm 为过渡带")
        region_hint.setObjectName("SubtleText")
        region_title_box.addWidget(self.region_title)
        region_title_box.addWidget(region_hint)
        self.region_status = QLabel("未标定")
        self.region_status.setObjectName("MetricValue")
        self.region_detail = QLabel("等待活动 Gateway 与三个节点的完整特征")
        self.region_detail.setObjectName("SubtleText")
        self.region_detail.setWordWrap(True)
        region_status_box = QVBoxLayout()
        region_status_box.addWidget(self.region_status)
        region_status_box.addWidget(self.region_detail)
        region_button = QPushButton("区域标定")
        region_button.setObjectName("PrimaryButton")
        region_button.clicked.connect(self._show_region_calibration)
        region_layout.addLayout(region_title_box, 2)
        region_layout.addLayout(region_status_box, 2)
        region_layout.addWidget(region_button)
        layout.addWidget(self.region_card)

        self.verdict_card = CardFrame()
        self.verdict_card.setFixedHeight(156)
        verdict_layout = QHBoxLayout(self.verdict_card)
        verdict_layout.setContentsMargins(18, 12, 18, 12)
        verdict_layout.setSpacing(16)
        verdict_title_box = QVBoxLayout()
        verdict_title_box.setSpacing(5)
        verdict_title = QLabel("综合研判结果")
        verdict_title.setObjectName("SectionTitle")
        verdict_subtitle = QLabel("基于多节点最近数据的辅助判断")
        verdict_subtitle.setObjectName("SubtleText")
        self.ai_settings_btn = QPushButton("AI设置")
        self.ai_settings_btn.setObjectName("GhostButton")
        self.ai_settings_btn.setFixedWidth(86)
        self.ai_settings_btn.clicked.connect(self._show_ai_settings_dialog)
        verdict_title_box.addWidget(verdict_title)
        verdict_title_box.addWidget(verdict_subtitle)
        verdict_title_box.addWidget(self.ai_settings_btn)
        verdict_title_box.addStretch(1)

        verdict_status_box = QVBoxLayout()
        verdict_status_box.setSpacing(5)
        self.verdict_status = QLabel("等待数据")
        self.verdict_status.setObjectName("MetricValue")
        self.verdict_meta = QLabel("规则融合 · 最近 5 秒 · 稳定显示")
        self.verdict_meta.setObjectName("SubtleText")
        self.verdict_detail = QLabel("规则融合：尚未收到有效节点数据")
        self.verdict_detail.setObjectName("SubtleText")
        self.verdict_detail.setWordWrap(True)
        self.ai_verdict = QLabel("AI辅助研判：暂无有效样本，建议连接 Gateway 后继续采集")
        self.ai_verdict.setObjectName("SubtleText")
        self.ai_verdict.setWordWrap(True)
        self.ai_verdict.setMaximumHeight(40)
        self.ai_verdict.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        verdict_status_box.addWidget(self.verdict_status)
        verdict_status_box.addWidget(self.verdict_meta)
        verdict_status_box.addWidget(self.verdict_detail)
        verdict_status_box.addWidget(self.ai_verdict)

        verdict_facts = QGridLayout()
        verdict_facts.setHorizontalSpacing(8)
        verdict_facts.setVerticalSpacing(8)
        for key, text in (
            ("participants", "参与节点：0"),
            ("triggered", "触发节点：无"),
            ("window", "时间窗口：最近 5 秒"),
            ("updated", "更新：--"),
        ):
            chip = QLabel(text)
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip.setMinimumHeight(26)
            self._verdict_chips[key] = chip
        verdict_facts.addWidget(self._verdict_chips["participants"], 0, 0)
        verdict_facts.addWidget(self._verdict_chips["triggered"], 0, 1)
        verdict_facts.addWidget(self._verdict_chips["window"], 1, 0)
        verdict_facts.addWidget(self._verdict_chips["updated"], 1, 1)

        verdict_layout.addLayout(verdict_title_box, 1)
        verdict_layout.addLayout(verdict_status_box, 3)
        verdict_layout.addLayout(verdict_facts, 2)
        self._apply_verdict_styles(THEME["muted"])
        layout.addWidget(self.verdict_card)

        self.csi_plot = CsiTrendPlot()
        layout.addWidget(self.csi_plot, 5)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(16)
        metrics.setVerticalSpacing(16)
        self.metric_cards["motion"] = MetricCard("当前节点运动\n(MOTION)", "0.00", "等待数据")
        self.metric_cards["presence"] = MetricCard("当前节点存在\n(PRESENCE)", "CLEAR", "未检测")
        self.metric_cards["confidence"] = MetricCard("当前节点置信度\n(CONFIDENCE)", "-- %", "模型输出")
        for index, card in enumerate(self.metric_cards.values()):
            metrics.addWidget(card, 0, index)
        layout.addLayout(metrics)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(16)
        bottom_row.addWidget(
            self._build_value_group(
                "环境状态 (ENVIRONMENT)",
                (
                    ("temp", "温度 TEMP", "-- °C"),
                    ("hum", "湿度 HUM", "-- %"),
                    ("gas", "CO2 估算 ppm", "-- ppm"),
                ),
            ),
            1,
        )
        bottom_row.addWidget(
            self._build_value_group(
                "无线链路 (WIRELESS)",
                (
                    ("rssi", "LoRa RSSI", "-- dBm"),
                    ("snr", "SNR", "-- dB"),
                    ("loss", "数据包丢弃", "-- %"),
                ),
            ),
            1,
        )
        layout.addLayout(bottom_row)

        return center

    # ----- 右栏 -----
    def _build_right_rail(self) -> QWidget:
        rail = QFrame()
        rail.setObjectName("RightRail")
        rail.setFixedWidth(360)

        layout = QVBoxLayout(rail)
        layout.setContentsMargins(20, 22, 20, 20)
        layout.setSpacing(16)

        self.event_panel = EventLogPanel()
        layout.addWidget(self.event_panel, 2)

        event_tools = QHBoxLayout()
        event_tools.setSpacing(8)
        self.event_filter = QComboBox()
        self.event_filter.addItems(("全部事件", "ALARM", "WARN", "OK", "INFO"))
        self.event_filter.currentIndexChanged.connect(lambda: setattr(self, "_last_event_count", -1))
        clear_btn = QPushButton("清空事件")
        clear_btn.setObjectName("GhostButton")
        clear_btn.clicked.connect(self.clear_events_requested.emit)
        event_tools.addWidget(self.event_filter, 1)
        event_tools.addWidget(clear_btn)
        layout.addLayout(event_tools)

        topology_card = CardFrame()
        topology_layout = QVBoxLayout(topology_card)
        topology_layout.setContentsMargins(18, 16, 18, 18)
        topology_layout.setSpacing(12)
        title = QLabel("Gateway 雷达视图 (RADAR)")
        title.setObjectName("SectionTitle")
        topology_layout.addWidget(title)
        self.topology_widget = TopologyWidget()
        topology_layout.addWidget(self.topology_widget, 1)
        layout.addWidget(topology_card, 1)

        return rail

    def _build_value_group(self, title: str, items: tuple[tuple[str, str, str], ...]) -> QWidget:
        card = CardFrame()
        card.setMinimumHeight(118)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(16)

        title_label = QLabel(title)
        title_label.setObjectName("SectionTitle")
        layout.addWidget(title_label)

        row = QHBoxLayout()
        row.setSpacing(12)
        for key, label, value in items:
            box = QVBoxLayout()
            box.setSpacing(6)
            caption = QLabel(label)
            caption.setObjectName("SubtleText")
            value_label = QLabel(value)
            value_label.setObjectName("MetricValue")
            value_label.setStyleSheet("font-size: 19px;")
            self.group_values[key] = value_label
            box.addWidget(caption)
            box.addWidget(value_label)
            row.addLayout(box, 1)
        layout.addLayout(row)
        return card

    # ----- 数据刷新 -----
    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        nodes: dict[int, dict[str, Any]] = snapshot.get("nodes", {})
        history: list[dict[str, Any]] = snapshot.get("recent_history") or snapshot.get("history", [])
        events: list[dict[str, Any]] = snapshot.get("events", [])
        ai_state: dict[str, Any] = snapshot.get("ai", {})
        config: dict[str, Any] = snapshot.get("config", {})
        requested_active = int(snapshot.get("active_node") or 0)
        active_node = self._refresh_focus_nodes(nodes, requested_active)
        active_state = nodes.get(active_node, {}) if active_node else {}
        self._latest_nodes = nodes
        self._latest_active_node = active_node
        self._latest_active_state = dict(active_state)
        self._latest_ai_config = dict(ai_state.get("config") or {})
        if self._ai_dialog is not None:
            self._ai_dialog.set_runtime_state(ai_state)
        region = snapshot.get("region") if isinstance(snapshot.get("region"), dict) else {}
        self._update_region_status(region)

        self._paused = bool(snapshot.get("paused"))
        pause_text = "恢复刷新" if self._paused else "暂停刷新"
        if pause_text != self._last_pause_text:
            self._last_pause_text = pause_text
            self.pause_btn.setText(pause_text)

        # 中文注释：不可见时跳过曲线与拓扑重绘，降低后台 CPU 占用。
        if not self.isVisible():
            return

        self._update_metric_cards(active_state, config)
        self._update_group_cards(active_state)

        filtered_events = self._filter_events(events)
        last_event = events[-1] if events else {}
        event_key = (
            len(events),
            self.event_filter.currentText(),
            last_event.get("time"),
            last_event.get("title"),
            last_event.get("message"),
            last_event.get("level"),
        )
        if event_key != self._last_event_count:
            self._last_event_count = event_key
            self.event_panel.set_events(filtered_events)

        now = time.time()
        if now - self._last_dashboard_heavy_refresh_at < 0.75:
            return
        self._last_dashboard_heavy_refresh_at = now

        self.csi_plot.set_history(history, active_node, active_state)
        self._update_verdict(nodes, history, ai_state, config, snapshot.get("detection_summary"))
        self.topology_widget.set_nodes(
            nodes,
            _float(config.get("presence_threshold"), PRESENCE_THRESHOLD),
        )

    def _show_region_calibration(self) -> None:
        if self._region_dialog is None:
            self._region_dialog = RegionCalibrationDialog(self.window())
            self._region_dialog.phase_requested.connect(self.region_calibration_phase_requested.emit)
            self._region_dialog.cancel_requested.connect(self.region_calibration_cancel_requested.emit)
        self._region_dialog.update_region(self._latest_region)
        self._region_dialog.show()
        self._region_dialog.raise_()
        self._region_dialog.activateWindow()

    def _update_region_status(self, region: dict[str, Any]) -> None:
        self._latest_region = dict(region)
        status = str(region.get("status") or "not_calibrated")
        label = str(region.get("label") or "未标定")
        gateway = str(region.get("gateway_id") or "GW-02").strip().upper()
        links = int(region.get("valid_links") or 0)
        probability = max(0.0, min(float(region.get("inside_probability") or 0.0), 1.0))
        calibration = region.get("calibration") if isinstance(region.get("calibration"), dict) else {}
        self.region_title.setText(f"三角形区域检测 · {gateway}")
        if status == "calibrating":
            detail = f"{calibration.get('phase_label') or '现场'}采集中 · 有效链路 {links}/9"
            color = THEME["orange"]
        elif status == "occupied":
            detail = f"内部概率 {probability * 100:.0f}% · 有效链路 {links}/9"
            color = THEME["red"]
        elif status == "clear":
            detail = f"内部概率 {probability * 100:.0f}% · 有效链路 {links}/9"
            color = THEME["green"]
        elif status in {"calibration_failed", "profile_mismatch", "needs_recalibration", "gateway_mismatch"}:
            detail = str(calibration.get("error") or f"有效链路 {links}/9，请检查设备或重新标定")
            color = THEME["red"]
        elif status in {"gateway_switching", "unsupported_gateway"}:
            detail = f"{label} · 有效链路 {links}/9"
            color = THEME["orange"]
        else:
            detail = f"有效链路 {links}/9 · 需要 {gateway} 与 Node 1/2/3"
            color = THEME["blue_soft"]
        self.region_status.setText(label)
        self.region_status.setStyleSheet(f"color: {color}; font-size: 24px; font-weight: 800;")
        self.region_detail.setText(detail)
        if self._region_dialog is not None:
            self._region_dialog.update_region(region)

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_toggled.emit(self._paused)

    def _on_focus_node_changed(self) -> None:
        node_id = int(self.node_combo.currentData() or 0)
        if node_id > 0:
            self.active_node_changed.emit(node_id)

    def _refresh_focus_nodes(self, nodes: dict[int, dict[str, Any]], active_node: int) -> int:
        discovered = [
            (node_id, _node_label(node_id, state))
            for node_id, state in sorted(nodes.items())
            if state.get("last_received") is not None
        ]
        if not discovered:
            if self._known_focus_nodes:
                self.node_combo.blockSignals(True)
                self.node_combo.clear()
                self.node_combo.addItem("等待节点接入", 0)
                self.node_combo.blockSignals(False)
            self.node_combo.setEnabled(False)
            self.detail_btn.setEnabled(False)
            self._known_focus_nodes = []
            return 0

        discovered_ids = [node_id for node_id, _label in discovered]
        selected = active_node if active_node in discovered_ids else discovered_ids[0]
        if discovered != self._known_focus_nodes:
            self.node_combo.blockSignals(True)
            self.node_combo.clear()
            for node_id, label in discovered:
                self.node_combo.addItem(label, node_id)
            self._known_focus_nodes = discovered
            self.node_combo.blockSignals(False)
        self.node_combo.setEnabled(True)
        self.detail_btn.setEnabled(True)
        refresh_widget_icons(self.detail_btn)
        target_index = discovered_ids.index(selected)
        if self.node_combo.currentIndex() != target_index:
            self.node_combo.blockSignals(True)
            self.node_combo.setCurrentIndex(target_index)
            self.node_combo.blockSignals(False)
        return selected

    def _filter_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        level = self.event_filter.currentText()
        if level == "全部事件":
            return events
        return [event for event in events if str(event.get("level", "")).upper() == level]

    def _show_active_node_detail(self) -> None:
        nodes = getattr(self, "_latest_nodes", {})
        node_id = int(getattr(self, "_latest_active_node", 0))
        state = nodes.get(node_id, {})
        _show_detail_dialog(
            self,
            f"{_node_label(node_id, state)} 节点详情",
            (
                ("在线状态", "在线" if state.get("online") else "离线"),
                ("Presence", f"{_score(state.get('presence_score')):.2f}"),
                ("Motion", f"{_score(state.get('motion_score')):.2f}"),
                ("Confidence", f"{_score(state.get('confidence')) * 100:.0f}%"),
                ("CO2 估算 ppm", f"{_float(state.get('gas_ppm', state.get('gas'))):.0f} ppm"),
                ("RSSI", f"{_float(state.get('rssi')):.0f} dBm"),
            ),
        )

    def _show_ai_settings_dialog(self) -> None:
        dialog = AISettingsDialog(self._latest_ai_config, self)
        self._ai_dialog = dialog
        dialog.action_requested.connect(self.ai_action_requested.emit)
        dialog.save_requested.connect(self.ai_config_save_requested.emit)
        dialog.start_jina_requested.connect(self.ai_jina_start_requested.emit)
        dialog.stop_jina_requested.connect(self.ai_jina_stop_requested.emit)
        dialog.test_embedding_requested.connect(self.ai_embedding_test_requested.emit)
        dialog.fetch_models_requested.connect(self.ai_models_requested.emit)
        dialog.test_llm_requested.connect(self.ai_llm_test_requested.emit)
        dialog.finished.connect(lambda _code: setattr(self, "_ai_dialog", None))
        dialog.request_deployment_check()
        dialog.exec()

    def set_ai_operation_message(self, text: str, ok: bool = True) -> None:
        if self._ai_dialog is not None:
            self._ai_dialog.set_status(text, ok)

    def set_ai_models(self, models: object) -> None:
        if self._ai_dialog is not None:
            self._ai_dialog.set_models(models)

    def set_ai_operation_result(self, result: object) -> None:
        if self._ai_dialog is not None:
            self._ai_dialog.set_operation_result(result)

    def _update_verdict(
        self,
        nodes: dict[int, dict[str, Any]],
        history: list[dict[str, Any]],
        ai_state: dict[str, Any],
        config: dict[str, Any],
        snapshot_summary: Any | None = None,
    ) -> None:
        summary = snapshot_summary or build_detection_summary(
            nodes,
            history,
            presence_threshold=_float(config.get("presence_threshold"), PRESENCE_THRESHOLD),
        )
        display_summary = self._stable_verdict_summary(summary)
        status = display_summary.status
        detail = display_summary.detail
        color = THEME[verdict_color_key(status)]
        self._last_verdict = (status, detail, color)
        self._render_verdict(display_summary, ai_state, color)

    def _stable_verdict_summary(self, candidate: Any) -> Any:
        now = time.time()
        current = self._display_verdict_summary
        if current is None:
            self._display_verdict_summary = candidate
            self._pending_verdict_summary = None
            self._display_verdict_since = now
            return candidate

        if candidate.state_key == current.state_key:
            self._display_verdict_summary = candidate
            self._pending_verdict_summary = None
            return candidate

        pending = self._pending_verdict_summary
        if pending is None or pending.state_key != candidate.state_key:
            self._pending_verdict_summary = candidate
            self._pending_verdict_since = now
            return current

        stable_seconds = self._VERDICT_STABLE_SECONDS
        hold_seconds = self._VERDICT_MIN_HOLD_SECONDS
        if (
            now - self._pending_verdict_since >= stable_seconds
            and now - self._display_verdict_since >= hold_seconds
        ):
            self._display_verdict_summary = candidate
            self._pending_verdict_summary = None
            self._display_verdict_since = now
            return candidate
        return current

    def _render_verdict(self, summary: Any, ai_state: dict[str, Any], color: str) -> None:
        self.verdict_status.setText(summary.status)
        self.verdict_detail.setText(f"规则融合：{self._verdict_advice(summary.detail)}")
        age = max(0.0, time.time() - self._display_verdict_since) if self._display_verdict_since else 0.0
        self.verdict_meta.setText(f"规则主判断 · 最近 {summary.window_seconds:.0f} 秒 · 稳定显示 {age:.0f}s")
        self._verdict_chips["participants"].setText(
            f"参与节点：{self._format_verdict_nodes(summary.participant_labels, '0')}"
        )
        self._verdict_chips["triggered"].setText(
            f"触发节点：{self._format_verdict_nodes(summary.triggered_labels, '无')}"
        )
        self._verdict_chips["window"].setText(f"时间窗口：最近 {summary.window_seconds:.0f} 秒")
        self._verdict_chips["updated"].setText(f"更新：{age:.0f}s前")
        self._apply_verdict_styles(color)

        display_ai_text, tooltip = self._paced_ai_text(summary, ai_state)
        self.ai_verdict.setText(display_ai_text)
        self.ai_verdict.setToolTip(tooltip)

    def _paced_ai_text(self, summary: Any, ai_state: dict[str, Any]) -> tuple[str, str]:
        now = time.time()
        raw_text = str(ai_state.get("text") or "").strip()
        if not raw_text or "分析中" in raw_text:
            raw_text = ai_fallback_text(summary.status, summary)

        source = str(ai_state.get("source") or "rule_fallback")
        state_key = str(ai_state.get("state_key") or summary.state_key)
        candidate_key = f"{state_key}|{source}|{raw_text}"
        updated_at = _float(ai_state.get("updated_at"))
        tooltip = raw_text
        if updated_at > 0 and source != "rule_fallback":
            ai_age = max(0.0, now - updated_at)
            tooltip = f"{raw_text}（{ai_age:.0f}s前）"

        if not self._display_ai_text:
            self._store_display_ai(raw_text, tooltip, candidate_key, now)
        elif candidate_key != self._display_ai_key and now - self._display_ai_since >= self._AI_TEXT_MIN_HOLD_SECONDS:
            self._store_display_ai(raw_text, tooltip, candidate_key, now)
        elif candidate_key == self._display_ai_key:
            self._display_ai_tooltip = tooltip

        display_text = self._short_ai_text(self._display_ai_text)
        display_tooltip = self._display_ai_tooltip or tooltip
        if bool(ai_state.get("running")):
            display_text = self._short_ai_text(f"{self._display_ai_text} · 更新中")
            display_tooltip = f"{display_tooltip}\nAI 正在更新，上一条有效结论继续保留。"
        return display_text, display_tooltip

    def _store_display_ai(self, text: str, tooltip: str, key: str, now: float) -> None:
        self._display_ai_text = text
        self._display_ai_tooltip = tooltip
        self._display_ai_key = key
        self._display_ai_since = now

    def _short_ai_text(self, text: str) -> str:
        text = " ".join(str(text or "").split())
        if len(text) <= self._AI_CARD_TEXT_LIMIT:
            return text
        return text[: self._AI_CARD_TEXT_LIMIT - 3] + "..."

    def _apply_verdict_styles(self, color: str) -> None:
        self.verdict_status.setStyleSheet(f"font-size: 25px; font-weight: 800; color: {color};")
        self.verdict_detail.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 13px;")
        self.verdict_meta.setStyleSheet(f"color: {THEME['muted']}; font-size: 12px;")
        self.ai_verdict.setStyleSheet(
            f"color: {THEME['blue_soft']}; font-size: 12px; font-weight: 600; line-height: 18px;"
        )
        chip_style = (
            f"background: {THEME['card_alt']};"
            f"border: 1px solid {THEME['border']};"
            "border-radius: 8px;"
            "padding: 5px 8px;"
            f"color: {THEME['text_soft']};"
            "font-size: 12px;"
        )
        for chip in self._verdict_chips.values():
            chip.setStyleSheet(chip_style)

    def _format_verdict_nodes(self, labels: list[str], empty: str) -> str:
        if not labels:
            return empty
        shown = labels[:3]
        suffix = f" +{len(labels) - len(shown)}" if len(labels) > len(shown) else ""
        return f"{len(labels)} · " + "、".join(shown) + suffix

    def _verdict_advice(self, detail: str) -> str:
        parts = [part.strip() for part in str(detail).split("；") if part.strip()]
        return parts[-1] if parts else "等待有效节点样本"

    def set_export_message(self, text: str, ok: bool = True) -> None:
        self._export_ok = ok

    def _update_metric_cards(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> None:
        config = config or {}
        motion = _score(state.get("motion_score"))
        presence = _score(state.get("presence_score"))
        confidence = _score(state.get("confidence"))
        triggered = life_motion_triggered(
            state,
            presence_threshold=_float(config.get("presence_threshold"), PRESENCE_THRESHOLD),
        )
        motion_hint = "活跃" if motion >= 0.52 else "平稳"
        presence_text = "疑似微动" if triggered else "未检测"
        presence_hint = f"{presence * 100:.0f}% 阈值响应"
        conf_hint = "可信" if confidence >= 0.75 else "观测中"

        self.metric_cards["motion"].set_value(
            f"{motion:.2f}", motion_hint, THEME["green"] if motion >= 0.52 else None
        )
        self.metric_cards["presence"].set_value(
            presence_text,
            presence_hint,
            THEME["green"] if presence_text == "疑似微动" else THEME["blue_soft"],
        )
        self.metric_cards["confidence"].set_value(
            f"{confidence * 100:.0f} %", conf_hint, THEME["blue_soft"]
        )

    def _update_group_cards(self, state: dict[str, Any]) -> None:
        temp = _float(state.get("temperature"))
        hum = _float(state.get("humidity"))
        gas = _float(state.get("gas_ppm", state.get("gas")))
        rssi = _float(state.get("rssi"))
        snr = _float(state.get("snr"))
        loss = _float(state.get("packet_loss"))

        self._set_group_value("temp", f"{temp:.1f}°C")
        self._set_group_value("hum", f"{hum:.0f}%")
        self._set_group_value(
            "gas",
            f"{gas:.0f} ppm",
            f"font-size: 19px; color: {THEME['red'] if gas >= 2000 else THEME['orange'] if gas >= 1000 else THEME['text']};",
        )

        self._set_group_value("rssi", f"{rssi:.0f} dBm", f"font-size: 19px; color: {THEME['blue_soft']};")
        self._set_group_value("snr", f"{snr:.1f} dB")
        self._set_group_value(
            "loss",
            f"{loss:.2f}%",
            f"font-size: 19px; color: {THEME['red'] if loss >= 8 else THEME['orange'] if loss >= 2 else THEME['text']};",
        )

    def _set_group_value(self, key: str, text: str, style: str = "font-size: 19px;") -> None:
        cached = self._last_group_values.get(key)
        if cached == (text, style):
            return
        self._last_group_values[key] = (text, style)
        label = self.group_values[key]
        label.setText(text)
        label.setStyleSheet(style)

    def refresh_theme(self) -> None:
        status, detail, _color = self._last_verdict
        color = THEME[verdict_color_key(status)]
        self._last_verdict = (status, detail, color)
        self._apply_verdict_styles(color)
        self.csi_plot.refresh_theme()
        self.event_panel.refresh_theme()
        self.topology_widget.update()
        for card in self.metric_cards.values():
            card.refresh_theme()
        self._last_group_values.clear()
        if self._latest_active_state:
            self._update_metric_cards(self._latest_active_state)
            self._update_group_cards(self._latest_active_state)
        else:
            for value_label in self.group_values.values():
                value_label.setStyleSheet("font-size: 19px;")
        if self._ai_dialog is not None:
            self._ai_dialog.refresh_theme()
        refresh_widget_icons(self)


# ===========================================================================
# 传感器页 / 活动节点矩阵（图 1）
# ===========================================================================
class _MatrixRow(QFrame):
    """节点矩阵单行，支持原地更新。"""

    menu_requested = pyqtSignal(int)

    def __init__(self, code: str) -> None:
        super().__init__()
        self.matrix_id = 0
        self._state: dict[str, Any] = {}
        self.setObjectName("MatrixRow")
        self.setFixedHeight(78)
        self._apply_row_style()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 8, 16, 8)
        layout.setSpacing(0)

        # 节点 ID（拉伸列）
        self.code_label = QLabel(code)
        self.code_label.setObjectName("NodeCode")
        self.code_label.setWordWrap(True)
        self.code_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.code_label, 1)

        # 运行模式
        self.mode_tag = ModeTag()
        layout.addWidget(self._fixed(self.mode_tag, _MATRIX_COLUMNS[1][1], align_left=True))

        # RSSI
        self.rssi_label = QLabel("-- dBm")
        self.rssi_label.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 14px;")
        layout.addWidget(self._fixed(self.rssi_label, _MATRIX_COLUMNS[2][1], align_left=True))

        # 电池
        self.battery = BatteryBar()
        layout.addWidget(self._fixed(self.battery, _MATRIX_COLUMNS[3][1], align_left=True))

        # 健康度
        self.health = HealthBadge()
        layout.addWidget(self._fixed(self.health, _MATRIX_COLUMNS[4][1], align_left=True))

        # 建议动作
        self.advice_label = QLabel("等待数据")
        self.advice_label.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 13px;")
        self.advice_label.setWordWrap(True)
        layout.addWidget(self._fixed(self.advice_label, _MATRIX_COLUMNS[5][1], align_left=True))

        # 操作
        self.menu_btn = QPushButton("⋯")
        self.menu_btn.setObjectName("RowMenu")
        self.menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_btn.clicked.connect(lambda: self.menu_requested.emit(self.matrix_id))
        layout.addWidget(self._fixed(self.menu_btn, _MATRIX_COLUMNS[6][1], align_left=False))

    def _apply_row_style(self) -> None:
        self.setStyleSheet(
            "QFrame#MatrixRow { background: transparent; border: 0;"
            f" border-bottom: 1px solid {THEME['border_soft']}; }}"
            f"QFrame#MatrixRow:hover {{ background: {THEME['row_hover']}; }}"
        )

    @staticmethod
    def _fixed(widget: QWidget, width: int, align_left: bool) -> QWidget:
        holder = QWidget()
        holder.setFixedWidth(width)
        box = QHBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(widget)
        if not align_left:
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            box.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return holder

    def update_state(self, state: dict[str, Any]) -> None:
        self._state = state
        self.matrix_id = int(state.get("matrix_id") or 0)
        self.code_label.setText(str(state.get("code", "")))
        self.mode_tag.set_mode(str(state.get("mode", "NORMAL")))
        rssi = _float(state.get("rssi"))
        online = bool(state.get("online"))
        self.rssi_label.setText(f"{rssi:.0f} dBm")
        self.rssi_label.setStyleSheet(
            f"color: {THEME['text_soft'] if online else THEME['muted_2']}; font-size: 14px;"
        )
        if state.get("battery") is None:
            self.battery.set_unknown()
        else:
            self.battery.set_value(_float(state.get("battery")))
        self.health.set_health(str(state.get("health", "")))
        advice, color = _matrix_advice(state)
        self.advice_label.setText(advice)
        self.advice_label.setStyleSheet(f"color: {color}; font-size: 13px;")

    def refresh_theme(self) -> None:
        self._apply_row_style()
        self.mode_tag.refresh_theme()
        self.battery.refresh_theme()
        self.health.refresh_theme()
        self.update_state(self._state)


class SensorMatrixPage(QWidget):
    """活动节点矩阵 + 系统核心配置（图 1）。"""

    presence_threshold_changed = pyqtSignal(float)
    gas_threshold_changed = pyqtSignal(float)
    gas_calibration_requested = pyqtSignal()
    gas_calibration_all_requested = pyqtSignal()
    afh_toggled = pyqtSignal(bool)
    mesh_toggled = pyqtSignal(bool)
    sync_requested = pyqtSignal()
    matrix_filter_changed = pyqtSignal(str)
    matrix_maintenance_requested = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__()
        self._rows: dict[int, _MatrixRow] = {}
        self._matrix_filter = "ALL"
        self._latest_matrix: dict[int, dict[str, Any]] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_center(), 1)
        body.addWidget(self._build_right_rail())
        outer.addLayout(body, 1)

        self.warning_bar = SystemWarningBar()
        self.warning_bar.hide()
        outer.addWidget(self.warning_bar)

    # ----- 中间区 -----
    def _build_center(self) -> QWidget:
        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title = QLabel("活动节点矩阵")
        title.setObjectName("SectionTitle")
        title.setStyleSheet("font-size: 19px; font-weight: 700;")
        self.subtitle = QLabel("已发现 0 个节点（在线 0）")
        self.subtitle.setObjectName("SectionSub")
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box)
        header.addStretch(1)

        self.filter_btn = QPushButton("筛选：全部")
        self.filter_btn.setObjectName("GhostButton")
        self.filter_btn.clicked.connect(self._open_filter_menu)
        header.addWidget(self.filter_btn)
        layout.addLayout(header)

        # 表格卡片
        table_card = CardFrame()
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(0)

        self.matrix_header = self._build_table_header()
        table_layout.addWidget(self.matrix_header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        self.empty_matrix_label = QLabel("等待 Gateway 节点接入")
        self.empty_matrix_label.setObjectName("SubtleText")
        self.empty_matrix_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_matrix_label.setStyleSheet(f"color: {THEME['muted']}; padding: 24px;")
        self.rows_layout.addWidget(self.empty_matrix_label)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(self.rows_host)
        table_layout.addWidget(self.scroll, 1)

        layout.addWidget(table_card, 1)
        return center

    def _build_table_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("MatrixHeader")
        header.setFixedHeight(50)
        self._style_matrix_header(header)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 0, 16, 0)
        layout.setSpacing(0)

        for index, (name, width) in enumerate(_MATRIX_COLUMNS):
            label = QLabel(name)
            label.setObjectName("ColHeader")
            if width == 0:
                label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                layout.addWidget(label, 1)
            else:
                holder = QWidget()
                holder.setFixedWidth(width)
                box = QHBoxLayout(holder)
                box.setContentsMargins(0, 0, 0, 0)
                box.addWidget(label)
                if index == len(_MATRIX_COLUMNS) - 1:
                    box.setAlignment(Qt.AlignmentFlag.AlignCenter)
                else:
                    box.setAlignment(Qt.AlignmentFlag.AlignLeft)
                layout.addWidget(holder)
        return header

    # ----- 右栏：系统核心配置 -----
    def _build_right_rail(self) -> QWidget:
        rail = QFrame()
        rail.setObjectName("RightRail")
        rail.setFixedWidth(380)

        layout = QVBoxLayout(rail)
        layout.setContentsMargins(24, 24, 24, 22)
        layout.setSpacing(22)

        title = QLabel("系统核心")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        self.presence_slider = ThresholdSlider(
            "存在感应阈值 (Presence)",
            minimum=0.0,
            maximum=1.0,
            value=PRESENCE_THRESHOLD,
            value_fmt=lambda v: f"{v:.2f}",
            min_label="MIN 0.00",
            max_label="MAX 1.00",
        )
        self.presence_slider.valueChanged.connect(self.presence_threshold_changed.emit)
        layout.addWidget(self.presence_slider)

        self.gas_slider = ThresholdSlider(
            "CO2 估算 ppm 阈值",
            minimum=0.0,
            maximum=5000.0,
            value=GAS_THRESHOLD_PPM,
            value_fmt=lambda v: f"{v:.0f} ppm",
            min_label="0 ppm",
            max_label="5000 ppm",
        )
        self.gas_slider.valueChanged.connect(self.gas_threshold_changed.emit)
        layout.addWidget(self.gas_slider)

        self.gas_calibrate_btn = QPushButton("校准当前节点")
        self.gas_calibrate_btn.setObjectName("GhostButton")
        self.gas_calibrate_btn.clicked.connect(self.gas_calibration_requested.emit)
        layout.addWidget(self.gas_calibrate_btn)

        self.gas_calibrate_all_btn = QPushButton("校准全部在线节点")
        self.gas_calibrate_all_btn.setObjectName("GhostButton")
        self.gas_calibrate_all_btn.clicked.connect(self.gas_calibration_all_requested.emit)
        layout.addWidget(self.gas_calibrate_all_btn)

        self.gas_calibration_label = QLabel("MQ-135：按节点校准未完成")
        self.gas_calibration_label.setObjectName("SubtleText")
        self.gas_calibration_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.gas_calibration_label)

        self.config_divider = QFrame()
        self.config_divider.setFixedHeight(1)
        self.config_divider.setStyleSheet(f"background: {THEME['divider']};")
        layout.addWidget(self.config_divider)

        self.afh_row = SettingRow("自动频率跳变 (AFH)", checked=DEFAULT_AFH_ENABLED)
        self.afh_row.toggled.connect(self.afh_toggled.emit)
        layout.addWidget(self.afh_row)

        self.mesh_row = SettingRow("多级网格中继", checked=DEFAULT_MESH_ENABLED)
        self.mesh_row.toggled.connect(self.mesh_toggled.emit)
        layout.addWidget(self.mesh_row)

        layout.addStretch(1)

        sync_btn = QPushButton("同步全局配置")
        sync_btn.setObjectName("SyncButton")
        sync_btn.clicked.connect(self.sync_requested.emit)
        layout.addWidget(sync_btn)

        self.sync_time_label = QLabel("最后同步时间：尚未同步")
        self.sync_time_label.setObjectName("SubtleText")
        self.sync_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.control_id_label = QLabel(f"控制台 ID: {CONTROL_ID}")
        self.control_id_label.setObjectName("SubtleText")
        self.control_id_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.sync_time_label)
        layout.addWidget(self.control_id_label)

        return rail

    # ----- 数据刷新 -----
    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        matrix: list[dict[str, Any]] = snapshot.get("matrix", [])
        config: dict[str, Any] = snapshot.get("config", {})
        self._latest_matrix = {int(entry.get("matrix_id") or 0): entry for entry in matrix}

        online = config.get("online_matrix", sum(1 for m in matrix if m.get("online")))
        total = config.get("total_matrix", len(matrix))
        self.subtitle.setText(f"已发现 {total} 个节点（在线 {online}）")

        # 原地构建/更新行
        visible_ids: set[int] = set()
        for entry in self._visible_matrix(matrix):
            matrix_id = int(entry.get("matrix_id"))
            visible_ids.add(matrix_id)
            row = self._rows.get(matrix_id)
            if row is None:
                row = _MatrixRow(str(entry.get("code", "")))
                row.menu_requested.connect(self._open_row_menu)
                self._rows[matrix_id] = row
                self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
            row.update_state(entry)
            row.show()

        for matrix_id, row in self._rows.items():
            if matrix_id not in visible_ids:
                row.hide()
        if not matrix:
            self.empty_matrix_label.setText("等待 Gateway 节点接入")
            self.empty_matrix_label.show()
        elif not visible_ids:
            self.empty_matrix_label.setText("当前筛选无匹配节点")
            self.empty_matrix_label.show()
        else:
            self.empty_matrix_label.hide()

        last_sync = config.get("last_sync_at")
        if last_sync:
            self.sync_time_label.setText(
                "最后同步时间: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(last_sync)))
            )
        r0 = _float(config.get("gas_calibration_r0"))
        online = int(config.get("gas_online_nodes") or 0)
        calibrated = int(config.get("gas_calibrated_online") or 0)
        total_calibrated = int(config.get("gas_node_calibration_count") or 0)
        clean_air = _float(config.get("gas_clean_air_ppm"), 400.0)
        if online > 0:
            self.gas_calibration_label.setText(f"MQ-135：已校准 {calibrated}/{online} 个在线节点 · 清洁空气 {clean_air:.0f} ppm")
        elif total_calibrated > 0:
            self.gas_calibration_label.setText(f"MQ-135：已保存 {total_calibrated} 个节点 R0 · 清洁空气 {clean_air:.0f} ppm")
        elif r0 > 0.0:
            self.gas_calibration_label.setText(f"MQ-135：默认/旧全局 R0 {r0:.2f} kΩ")

        # 严重错误节点 → 底部系统警告条
        critical = next((m for m in matrix if m.get("health") == HEALTH_CRITICAL), None)
        if critical is not None:
            self.warning_bar.show_warning(
                f"节点 {critical.get('code')} 报告严重故障。运行模式 {critical.get('mode')}，"
                f"RSSI {_float(critical.get('rssi')):.0f} dBm，建议进行现场物理检查。"
            )
        else:
            self.warning_bar.hide()

    def _open_filter_menu(self) -> None:
        menu = QMenu(self)
        filters = (
            ("ALL", "全部"),
            ("ONLINE", "在线"),
            ("OFFLINE", "离线"),
            ("CRITICAL", "严重错误"),
            ("MAINTENANCE", "维护标记"),
        )
        for key, label in filters:
            action = QAction(label, self)
            action.triggered.connect(lambda _checked=False, k=key, text=label: self._set_filter(k, text))
            menu.addAction(action)
        menu.exec(self.filter_btn.mapToGlobal(self.filter_btn.rect().bottomLeft()))

    def _set_filter(self, key: str, label: str) -> None:
        self._matrix_filter = key
        self.filter_btn.setText(f"筛选：{label}")
        self.matrix_filter_changed.emit(key)
        visible_count = 0
        for matrix_id, row in self._rows.items():
            state = self._latest_matrix.get(matrix_id, {})
            visible = self._matrix_matches(state)
            row.setVisible(visible)
            if visible:
                visible_count += 1
        if not self._latest_matrix:
            self.empty_matrix_label.setText("等待 Gateway 节点接入")
            self.empty_matrix_label.show()
        elif visible_count == 0:
            self.empty_matrix_label.setText("当前筛选无匹配节点")
            self.empty_matrix_label.show()
        else:
            self.empty_matrix_label.hide()

    def _visible_matrix(self, matrix: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [entry for entry in matrix if self._matrix_matches(entry)]

    def _matrix_matches(self, entry: dict[str, Any]) -> bool:
        if self._matrix_filter == "ONLINE":
            return bool(entry.get("online"))
        if self._matrix_filter == "OFFLINE":
            return not bool(entry.get("online"))
        if self._matrix_filter == "CRITICAL":
            return entry.get("health") == HEALTH_CRITICAL
        if self._matrix_filter == "MAINTENANCE":
            return bool(entry.get("maintenance"))
        return True

    def _open_row_menu(self, matrix_id: int) -> None:
        state = self._latest_matrix.get(int(matrix_id), {})
        menu = QMenu(self)
        detail_action = QAction("查看详情", self)
        detail_action.triggered.connect(lambda: self._show_matrix_detail(state))
        maintenance_action = QAction("取消维护标记" if state.get("maintenance") else "标记维护", self)
        maintenance_action.triggered.connect(lambda: self.matrix_maintenance_requested.emit(int(matrix_id)))
        menu.addAction(detail_action)
        menu.addAction(maintenance_action)
        row = self._rows.get(int(matrix_id))
        anchor = row.menu_btn if row else self
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _show_matrix_detail(self, state: dict[str, Any]) -> None:
        _show_detail_dialog(
            self,
            f"{state.get('code', 'NODE')} 详情",
            (
                ("节点 ID", state.get("matrix_id", "-")),
                ("运行模式", state.get("mode", "-")),
                ("在线状态", "在线" if state.get("online") else "离线"),
                ("RSSI", f"{_float(state.get('rssi')):.0f} dBm"),
                ("电池", "未上报" if state.get("battery") is None else f"{_float(state.get('battery')):.0f}%"),
                ("健康度", state.get("health", "-")),
                ("维护标记", "是" if state.get("maintenance") else "否"),
                ("本地节点", "是" if state.get("local") else "否"),
            ),
        )

    def _style_matrix_header(self, header: QFrame) -> None:
        header.setStyleSheet(
            f"QFrame#MatrixHeader {{ background: {THEME['card_alt']}; border: 0;"
            f" border-bottom: 1px solid {THEME['border']};"
            " border-top-left-radius: 12px; border-top-right-radius: 12px; }"
        )

    def refresh_theme(self) -> None:
        self.empty_matrix_label.setStyleSheet(f"color: {THEME['muted']}; padding: 24px;")
        self._style_matrix_header(self.matrix_header)
        self.config_divider.setStyleSheet(f"background: {THEME['divider']};")
        self.presence_slider.refresh_theme()
        self.gas_slider.refresh_theme()
        self.afh_row.refresh_theme()
        self.mesh_row.refresh_theme()
        self.warning_bar.refresh_theme()
        for row in self._rows.values():
            row.refresh_theme()
        refresh_widget_icons(self)


# ===========================================================================
# AI 辅助页
# ===========================================================================
class AIAssistPage(QWidget):
    """联网增强 AI 研判页，主判断仍由规则融合负责。"""

    ai_action_requested = pyqtSignal(object)

    _DETAIL_TITLES = (
        ("basis", "依据"),
        ("risk", "风险"),
        ("trend", "趋势"),
        ("advice", "建议"),
    )

    def __init__(self) -> None:
        super().__init__()
        self._latest_snapshot: dict[str, Any] = {}
        self._detail_labels: dict[str, QLabel] = {}
        self._last_history_key: tuple[Any, ...] | None = None
        self._last_chat_key: tuple[Any, ...] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title = QLabel("AI 辅助")
        title.setObjectName("SectionTitle")
        title.setStyleSheet("font-size: 19px; font-weight: 700;")
        subtitle = QLabel("联网增强研判、规则回退与节点贡献解释")
        subtitle.setObjectName("SectionSub")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.refresh_detail_btn = QPushButton("刷新研判")
        self.refresh_detail_btn.setObjectName("PrimaryButton")
        self.refresh_detail_btn.clicked.connect(
            lambda: self.ai_action_requested.emit({"action": "refresh_ai_detail"})
        )
        header.addWidget(self.refresh_detail_btn)
        layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(16)

        chat_card = CardFrame()
        chat_card.setMinimumWidth(520)
        chat_layout = QVBoxLayout(chat_card)
        chat_layout.setContentsMargins(18, 16, 18, 16)
        chat_layout.setSpacing(12)

        chat_header = QHBoxLayout()
        chat_title_box = QVBoxLayout()
        chat_title_box.setSpacing(2)
        chat_title = QLabel("现场 AI 对话")
        chat_title.setObjectName("SectionTitle")
        self.chat_context_label = QLabel("上下文：当前快照 + 最近5分钟")
        self.chat_context_label.setObjectName("SectionSub")
        chat_title_box.addWidget(chat_title)
        chat_title_box.addWidget(self.chat_context_label)
        chat_header.addLayout(chat_title_box)
        chat_header.addStretch(1)
        self.clear_chat_btn = QPushButton("清空对话")
        self.clear_chat_btn.setObjectName("GhostButton")
        self.clear_chat_btn.clicked.connect(lambda: self.ai_action_requested.emit({"action": "clear_ai_chat"}))
        chat_header.addWidget(self.clear_chat_btn)
        chat_layout.addLayout(chat_header)

        quick_row = QGridLayout()
        quick_row.setHorizontalSpacing(8)
        quick_row.setVerticalSpacing(8)
        quick_questions = ("为什么低置信？", "哪个节点最关键？", "CO2/空气质量怎么看？", "下一步怎么排查？", "当前是否需要复核？")
        for index, text in enumerate(quick_questions):
            btn = QPushButton(text)
            btn.setObjectName("GhostButton")
            btn.clicked.connect(lambda _checked=False, value=text: self._ask_ai(value))
            quick_row.addWidget(btn, index // 3, index % 3)
        chat_layout.addLayout(quick_row)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.chat_scroll.setMinimumHeight(420)
        self.chat_container = QWidget()
        self.chat_messages_layout = QVBoxLayout(self.chat_container)
        self.chat_messages_layout.setContentsMargins(0, 4, 0, 4)
        self.chat_messages_layout.setSpacing(10)
        self.chat_messages_layout.addStretch(1)
        self.chat_scroll.setWidget(self.chat_container)
        chat_layout.addWidget(self.chat_scroll, 1)

        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("询问当前研判、节点贡献、CO2 或下一步排查...")
        self.chat_input.returnPressed.connect(self._ask_ai)
        self.chat_send_btn = QPushButton("发送")
        self.chat_send_btn.setObjectName("PrimaryButton")
        self.chat_send_btn.clicked.connect(self._ask_ai)
        input_row.addWidget(self.chat_input, 1)
        input_row.addWidget(self.chat_send_btn)
        chat_layout.addLayout(input_row)
        self.chat_error_label = QLabel("")
        self.chat_error_label.setObjectName("SectionSub")
        self.chat_error_label.setWordWrap(True)
        chat_layout.addWidget(self.chat_error_label)
        body.addWidget(chat_card, 5)

        context_scroll = QScrollArea()
        context_scroll.setWidgetResizable(True)
        context_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        context_scroll.setFrameShape(QFrame.Shape.NoFrame)
        context_scroll.setMinimumWidth(410)
        context_scroll.setMaximumWidth(480)
        context_rail = QWidget()
        context_rail.setMinimumWidth(390)
        context_layout = QVBoxLayout(context_rail)
        context_layout.setContentsMargins(2, 2, 10, 2)
        context_layout.setSpacing(18)

        cards = QGridLayout()
        cards.setHorizontalSpacing(14)
        cards.setVerticalSpacing(14)
        self.ai_cards: dict[str, MetricCard] = {
            "status": MetricCard("当前主判断\n(VERDICT)", "等待数据", "规则融合"),
            "source": MetricCard("AI 来源\n(SOURCE)", "规则回退", "离线可用"),
            "updated": MetricCard("详情更新\n(UPDATED)", "--", "等待刷新"),
            "participants": MetricCard("参与节点\n(NODES)", "0", "最近窗口"),
        }
        for index, card in enumerate(self.ai_cards.values()):
            cards.addWidget(card, index // 2, index % 2)
        context_layout.addLayout(cards)

        detail_grid = QGridLayout()
        detail_grid.setHorizontalSpacing(14)
        detail_grid.setVerticalSpacing(14)
        for index, (key, title_text) in enumerate(self._DETAIL_TITLES):
            card = CardFrame()
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 14, 16, 14)
            card_layout.setSpacing(8)
            title_label = QLabel(title_text)
            title_label.setObjectName("SectionTitle")
            body_label = QLabel("等待 AI 或规则融合生成详情")
            body_label.setObjectName("SubtleText")
            body_label.setWordWrap(True)
            body_label.setMinimumHeight(58)
            body_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            card_layout.addWidget(title_label)
            card_layout.addWidget(body_label, 1)
            self._detail_labels[key] = body_label
            detail_grid.addWidget(card, index // 2, index % 2)
        context_layout.addLayout(detail_grid)

        contribution_card = CardFrame()
        contribution_layout = QVBoxLayout(contribution_card)
        contribution_layout.setContentsMargins(16, 14, 16, 14)
        contribution_layout.setSpacing(10)
        contribution_title = QLabel("节点贡献 (NODE CONTRIBUTION)")
        contribution_title.setObjectName("SectionTitle")
        self.contribution_table = QTableWidget(0, 6)
        self.contribution_table.setHorizontalHeaderLabels(("节点", "角色", "样本", "存在", "置信", "运动"))
        self.contribution_table.verticalHeader().setVisible(False)
        self.contribution_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.contribution_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.contribution_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.contribution_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.contribution_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        contribution_header = self.contribution_table.horizontalHeader()
        for col in range(self.contribution_table.columnCount()):
            contribution_header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.contribution_table.setMinimumHeight(180)
        self.contribution_table.setMaximumHeight(260)
        contribution_layout.addWidget(contribution_title)
        contribution_layout.addWidget(self.contribution_table)
        context_layout.addWidget(contribution_card)

        history_card = CardFrame()
        history_layout = QVBoxLayout(history_card)
        history_layout.setContentsMargins(16, 14, 16, 14)
        history_layout.setSpacing(10)
        history_title = QLabel("AI 研判历史 (AI HISTORY)")
        history_title.setObjectName("SectionTitle")
        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(("时间", "来源", "结论", "建议"))
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.history_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.history_table.setWordWrap(True)
        self.history_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.history_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        history_header = self.history_table.horizontalHeader()
        history_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.history_table.setColumnWidth(0, 76)
        history_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.history_table.setColumnWidth(1, 78)
        history_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        history_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.history_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.setMinimumHeight(240)
        history_layout.addWidget(history_title)
        history_layout.addWidget(self.history_table)
        context_layout.addWidget(history_card)
        context_layout.addStretch(1)

        context_scroll.setWidget(context_rail)
        body.addWidget(context_scroll, 3)
        layout.addLayout(body, 1)

    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._latest_snapshot = snapshot
        ai_state: dict[str, Any] = snapshot.get("ai", {})
        summary = snapshot.get("detection_summary")
        nodes: dict[int, dict[str, Any]] = snapshot.get("nodes", {})
        detail = ai_state.get("detail") if isinstance(ai_state.get("detail"), dict) else {}
        detail = detail or self._fallback_detail(summary, ai_state)

        status = str(getattr(summary, "status", "等待数据"))
        participant_count = len(getattr(summary, "participant_ids", []) or [])
        triggered_count = len(getattr(summary, "triggered_ids", []) or [])
        source = self._source_label(str(ai_state.get("detail_source") or ai_state.get("source") or "rule_fallback"))
        updated_at = _float(ai_state.get("detail_updated_at") or ai_state.get("updated_at"))
        age = max(0.0, time.time() - updated_at) if updated_at else 0.0

        status_color = THEME[verdict_color_key(status)]
        self.ai_cards["status"].set_value(status, "规则融合主判断", status_color)
        self.ai_cards["source"].set_value(source, str(ai_state.get("status") or "等待 AI 状态"))
        self.ai_cards["updated"].set_value(f"{age:.0f}s 前" if updated_at else "--", "详情生成时间")
        self.ai_cards["participants"].set_value(
            f"{participant_count}", f"触发 {triggered_count} 个节点",
            THEME["green"] if triggered_count >= 2 else THEME["orange"] if triggered_count else None,
        )
        self.refresh_detail_btn.setEnabled(not bool(ai_state.get("running")))
        self.refresh_detail_btn.setText("生成中..." if ai_state.get("running") else "刷新研判")
        chat_running = bool(ai_state.get("chat_running"))
        self.chat_send_btn.setEnabled(not chat_running)
        self.chat_send_btn.setText("生成中..." if chat_running else "发送")
        self.chat_input.setEnabled(not chat_running)
        self.clear_chat_btn.setEnabled(not chat_running)
        self.chat_context_label.setText(
            f"上下文：{str(ai_state.get('chat_context_label') or '当前快照 + 最近5分钟')}"
        )
        chat_error = str(ai_state.get("chat_error") or "")
        if chat_error:
            self.chat_error_label.setText(f"提示：{chat_error}")
        else:
            chat_source = self._source_label(str(ai_state.get("chat_source") or "rule_fallback"))
            self.chat_error_label.setText(f"回答来源：{chat_source} · AI 不改变报警和主判断")

        for key, _title in self._DETAIL_TITLES:
            text = str(detail.get(key) or "暂无详情，等待有效样本或 AI 结果")
            self._detail_labels[key].setText(text)
            self._detail_labels[key].setToolTip(text)

        self._render_chat_history(list(ai_state.get("chat_history") or []), chat_running)
        self._render_contribution_table(summary, nodes)
        self._render_history_table(list(ai_state.get("detail_history") or []))

    def _ask_ai(self, text: str | None = None) -> None:
        question = str(text if text is not None else self.chat_input.text()).strip()
        if not question:
            self.chat_error_label.setText("提示：请输入要询问的问题")
            return
        self.ai_action_requested.emit({"action": "ask_ai_chat", "question": question})
        if text is None:
            self.chat_input.clear()
        self._schedule_chat_scroll_to_bottom()

    def _render_chat_history(self, history: list[dict[str, Any]], running: bool) -> None:
        chat_key = tuple((item.get("role"), item.get("content"), item.get("source")) for item in history)
        full_key = chat_key + (("running", running),)
        if full_key == self._last_chat_key:
            return
        self._last_chat_key = full_key
        while self.chat_messages_layout.count() > 1:
            item = self.chat_messages_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
                continue
            sub_layout = item.layout()
            if sub_layout is not None:
                clear_layout(sub_layout)
        if not history:
            self._add_markdown_turn(
                "",
                "可以问我当前为什么低置信、哪个节点贡献最大、CO2/空气质量怎么看，或下一步如何排查。",
            )
        else:
            pending_question = ""
            for item in history:
                role = str(item.get("role") or "assistant")
                content = str(item.get("content") or "")
                if role == "user":
                    if pending_question:
                        self._add_markdown_turn(pending_question, "")
                    pending_question = content
                else:
                    self._add_markdown_turn(pending_question, content)
                    pending_question = ""
            if pending_question:
                self._add_markdown_turn(pending_question, "生成中..." if running else "")
            elif running:
                self._add_markdown_turn("", "生成中...")
        self._schedule_chat_scroll_to_bottom()

    def _add_markdown_turn(self, question: str, answer: str) -> None:
        turn = QFrame()
        turn.setObjectName("AIChatTurn")
        turn_layout = QVBoxLayout(turn)
        turn_layout.setContentsMargins(14, 12, 14, 12)
        turn_layout.setSpacing(10)

        if question:
            question_title = QLabel("用户问题")
            question_title.setObjectName("SectionSub")
            question_text = QLabel(question)
            question_text.setWordWrap(True)
            question_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            question_text.setStyleSheet(f"color: {THEME['text']}; font-size: 13px; line-height: 19px;")
            turn_layout.addWidget(question_title)
            turn_layout.addWidget(question_text)

        if answer:
            answer_title = QLabel("AI 回复")
            answer_title.setObjectName("SectionSub")
            answer_view = QTextBrowser()
            answer_view.setFrameShape(QFrame.Shape.NoFrame)
            answer_view.setOpenExternalLinks(False)
            answer_view.setReadOnly(True)
            answer_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            answer_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            answer_view.setMarkdown(answer)
            answer_view.setStyleSheet(
                f"QTextBrowser {{ background: transparent; color: {THEME['text']};"
                " border: 0; font-size: 13px; line-height: 20px; }}"
            )
            turn_layout.addWidget(answer_title)
            turn_layout.addWidget(answer_view)
            QTimer.singleShot(0, lambda view=answer_view: self._fit_markdown_view(view))
            QTimer.singleShot(80, lambda view=answer_view: self._fit_markdown_view(view))

        turn.setStyleSheet(
            f"QFrame#AIChatTurn {{ background: {THEME['card_alt']};"
            f" border: 1px solid {THEME['border']}; border-radius: 8px; }}"
        )
        insert_at = max(0, self.chat_messages_layout.count() - 1)
        self.chat_messages_layout.insertWidget(insert_at, turn)

    def _fit_markdown_view(self, view: QTextBrowser) -> None:
        width = max(240, view.viewport().width())
        view.document().setTextWidth(width)
        height = int(view.document().size().height()) + 8
        view.setFixedHeight(max(42, min(height, 520)))

    def _schedule_chat_scroll_to_bottom(self) -> None:
        QTimer.singleShot(0, self._scroll_chat_to_bottom)
        QTimer.singleShot(80, self._scroll_chat_to_bottom)
        QTimer.singleShot(180, self._scroll_chat_to_bottom)

    def _scroll_chat_to_bottom(self) -> None:
        bar = self.chat_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _fallback_detail(self, summary: Any, ai_state: dict[str, Any]) -> dict[str, str]:
        status = str(getattr(summary, "status", "等待数据"))
        text = str(ai_state.get("text") or ai_fallback_text(status))
        return {
            "basis": text,
            "risk": "AI 详情尚未生成，当前仅展示规则回退信息。",
            "trend": "等待更多实时样本后刷新趋势解释。",
            "advice": "保持采集，必要时点击刷新研判。",
        }

    def _render_contribution_table(self, summary: Any, nodes: dict[int, dict[str, Any]]) -> None:
        stats = list(getattr(summary, "stats", []) or [])
        triggered_ids = set(getattr(summary, "triggered_ids", []) or [])
        if not stats:
            discovered = [
                (node_id, state)
                for node_id, state in sorted(nodes.items())
                if state.get("last_received") is not None
            ]
            self.contribution_table.setRowCount(len(discovered))
            for row, (node_id, state) in enumerate(discovered):
                values = (
                    _node_label(node_id, state),
                    "观测中",
                    "--",
                    f"{_score(state.get('presence_score')):.2f}",
                    f"{_score(state.get('confidence')):.2f}",
                    f"{_score(state.get('motion_score')):.2f}",
                )
                self._set_table_row(self.contribution_table, row, values)
            return

        self.contribution_table.setRowCount(len(stats))
        for row, item in enumerate(stats):
            role = "触发" if int(item.node_id) in triggered_ids else "参与"
            values = (
                str(item.label),
                role,
                str(item.sample_count),
                f"{item.presence_avg:.2f}",
                f"{item.confidence_avg:.2f}",
                f"{item.motion_peak:.2f}",
            )
            self._set_table_row(self.contribution_table, row, values)

    def _render_history_table(self, history: list[dict[str, Any]]) -> None:
        shown = history[-12:][::-1]
        history_key = tuple((item.get("record_key"), item.get("time")) for item in shown)
        if history_key == self._last_history_key:
            return
        self._last_history_key = history_key
        self.history_table.setRowCount(len(shown))
        for row, item in enumerate(shown):
            ts = _float(item.get("time"))
            values = (
                time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--",
                self._source_label(str(item.get("source") or "")),
                str(item.get("headline") or item.get("status") or ""),
                str(item.get("advice") or ""),
            )
            self._set_table_row(self.history_table, row, values)
        self.history_table.resizeRowsToContents()
        for row in range(self.history_table.rowCount()):
            self.history_table.setRowHeight(row, max(self.history_table.rowHeight(row), 54))

    def _set_table_row(self, table: QTableWidget, row: int, values: tuple[str, ...]) -> None:
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            if table is self.history_table and col >= 2:
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            else:
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row, col, item)

    def _source_label(self, source: str) -> str:
        return {
            "llm_api": "大模型",
            "local_jina": "本地 Jina",
            "rule_fallback": "规则回退",
        }.get(source, source or "规则回退")

    def refresh_theme(self) -> None:
        for card in self.ai_cards.values():
            card.refresh_theme()
        for label in self._detail_labels.values():
            label.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 13px; line-height: 19px;")
        if self._latest_snapshot:
            self.update_snapshot(self._latest_snapshot)
        refresh_widget_icons(self)


# ===========================================================================
# 数据分析页
# ===========================================================================
class AnalysisPage(QWidget):
    """聚合统计 + 运动/存在趋势曲线。"""

    active_node_changed = pyqtSignal(int)
    analysis_shot_requested = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._last_plot_at = 0.0
        self._metric_key = "motion_score"
        self._window_seconds = 60
        self._selected_node = 0
        self._known_nodes: list[tuple[int, str]] = []
        self._last_stats_at = 0.0
        self._last_stats_key: tuple[Any, ...] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)

        title = QLabel("数据分析")
        title.setObjectName("SectionTitle")
        title.setStyleSheet("font-size: 19px; font-weight: 700;")
        subtitle = QLabel("基于实时历史样本的聚合统计与趋势")
        subtitle.setObjectName("SectionSub")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.analysis_node_combo = QComboBox()
        self.analysis_node_combo.addItem("全部节点", 0)
        self.analysis_node_combo.currentIndexChanged.connect(self._on_analysis_control_changed)
        self.metric_combo = QComboBox()
        for label, key in (
            ("运动分值", "motion_score"),
            ("存在感应", "presence_score"),
            ("CO2 估算 ppm", "gas_ppm"),
            ("LoRa RSSI", "rssi"),
        ):
            self.metric_combo.addItem(label, key)
        self.metric_combo.currentIndexChanged.connect(self._on_analysis_control_changed)
        self.window_combo = QComboBox()
        for label, seconds in (("最近 60 秒", 60), ("最近 5 分钟", 300), ("全部历史", 0)):
            self.window_combo.addItem(label, seconds)
        self.window_combo.currentIndexChanged.connect(self._on_analysis_control_changed)
        shot_btn = QPushButton("图表截图")
        shot_btn.setObjectName("GhostButton")
        shot_btn.clicked.connect(lambda: self.analysis_shot_requested.emit(self.plot))
        controls.addWidget(QLabel("节点"))
        controls.addWidget(self.analysis_node_combo)
        controls.addWidget(QLabel("指标"))
        controls.addWidget(self.metric_combo)
        controls.addWidget(QLabel("窗口"))
        controls.addWidget(self.window_combo)
        controls.addStretch(1)
        controls.addWidget(shot_btn)
        layout.addLayout(controls)

        stats = QGridLayout()
        stats.setHorizontalSpacing(16)
        stats.setVerticalSpacing(16)
        self.stat_cards: dict[str, MetricCard] = {
            "samples": MetricCard("累计样本\n(SAMPLES)", "0", "历史缓存"),
            "avg_motion": MetricCard("平均运动\n(AVG MOTION)", "--", "全部节点"),
            "max_gas": MetricCard("CO2 峰值\n(PPM EST.)", "-- ppm", "全部节点"),
            "online": MetricCard("在线节点\n(ONLINE)", "0 / 0", "已发现节点"),
        }
        for index, card in enumerate(self.stat_cards.values()):
            stats.addWidget(card, 0, index)
        layout.addLayout(stats)

        plot_card = CardFrame()
        plot_layout = QVBoxLayout(plot_card)
        plot_layout.setContentsMargins(20, 18, 20, 18)
        plot_layout.setSpacing(12)
        self.plot_title = QLabel("运动分值趋势 (MOTION SCORE Trend)")
        self.plot_title.setObjectName("SectionTitle")
        plot_layout.addWidget(self.plot_title)

        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setBackground(THEME["card"])
        self.plot.showGrid(x=True, y=True, alpha=0.16)
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.addLegend(offset=(10, 10))
        for axis_name in ("bottom", "left"):
            axis = self.plot.getAxis(axis_name)
            axis.setPen(pg.mkPen(THEME["plot_axis"]))
            axis.setTextPen(pg.mkPen(THEME["plot_axis_text"]))
        self.plot.setLabel("left", "Value")
        self.plot.setLabel("bottom", "Last 60s")
        self._curves = {}
        self._palette = [THEME["blue_bright"], THEME["green"], THEME["orange"], THEME["cyan"]]
        self.analysis_empty_label = QLabel("暂无可分析样本")
        self.analysis_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.analysis_empty_label.setObjectName("SubtleText")
        self.analysis_empty_label.setStyleSheet(f"color: {THEME['muted']}; padding: 8px;")
        plot_layout.addWidget(self.plot)
        plot_layout.addWidget(self.analysis_empty_label)
        layout.addWidget(plot_card, 1)

    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not self.isVisible():
            return
        nodes: dict[int, dict[str, Any]] = snapshot.get("nodes", {})
        history: list[dict[str, Any]] = snapshot.get("history", [])
        recent_history: list[dict[str, Any]] = snapshot.get("recent_history", [])
        self._refresh_analysis_nodes(nodes)
        self._selected_node = int(self.analysis_node_combo.currentData() or 0)
        self._metric_key = str(self.metric_combo.currentData() or "motion_score")
        self._window_seconds = int(self.window_combo.currentData() or 0)
        source_history = recent_history if self._window_seconds and recent_history else history
        filtered_history = self._analysis_history(source_history)

        now = time.time()
        last_sample = filtered_history[-1] if filtered_history else {}
        stats_key = (
            len(source_history),
            len(filtered_history),
            self._selected_node,
            self._metric_key,
            self._window_seconds,
            last_sample.get("node_id"),
            last_sample.get("seq"),
            last_sample.get("timestamp"),
        )
        if stats_key != self._last_stats_key and now - self._last_stats_at >= 0.5:
            self._last_stats_key = stats_key
            self._last_stats_at = now
            self.analysis_empty_label.setVisible(not bool(filtered_history))
            self.stat_cards["samples"].set_value(f"{len(filtered_history)}", "当前筛选")
            motions = [_score(s.get("motion_score")) for s in filtered_history]
            avg_motion = sum(motions) / len(motions) if motions else 0.0
            self.stat_cards["avg_motion"].set_value(f"{avg_motion:.2f}", "当前筛选")
            gases = [_float(s.get("gas_ppm", s.get("gas"))) for s in filtered_history]
            max_gas = max(gases) if gases else 0.0
            self.stat_cards["max_gas"].set_value(
                f"{max_gas:.0f} ppm", "当前筛选", THEME["red"] if max_gas >= 2000 else THEME["orange"] if max_gas >= 1000 else None
            )
            online = sum(1 for n in nodes.values() if n.get("online"))
            self.stat_cards["online"].set_value(
                f"{online} / {len(nodes)}", "已发现节点",
                THEME["green"] if online else None,
            )

        if now - self._last_plot_at < 0.8:
            return
        self._last_plot_at = now

        for node_id in sorted(nodes):
            self._ensure_analysis_curve(node_id, _node_label(node_id, nodes.get(node_id)))
        for node_id, curve in self._curves.items():
            if self._selected_node and node_id != self._selected_node:
                curve.setData([], [])
                continue
            recent = [s for s in filtered_history if int(s.get("node_id") or 0) == node_id][-240:]
            xs = [_float(s.get("timestamp"), now) - now for s in recent]
            ys = [_score(s.get(self._metric_key)) if self._metric_key in ("motion_score", "presence_score") else _float(s.get(self._metric_key)) for s in recent]
            curve.setData(xs, ys)
        seconds = self._window_seconds or max(60, int(now - min((_float(s.get("timestamp"), now) for s in filtered_history), default=now)))
        self.plot.setXRange(-float(seconds), 0.0, padding=0)
        self.plot_title.setText(f"{self.metric_combo.currentText()} 趋势")

    def _refresh_analysis_nodes(self, nodes: dict[int, dict[str, Any]]) -> None:
        discovered = [
            (node_id, _node_label(node_id, state))
            for node_id, state in sorted(nodes.items())
            if state.get("last_received") is not None
        ]
        discovered_ids = [node_id for node_id, _label in discovered]
        if discovered == self._known_nodes:
            return
        current = int(self.analysis_node_combo.currentData() or 0)
        self.analysis_node_combo.blockSignals(True)
        self.analysis_node_combo.clear()
        self.analysis_node_combo.addItem("全部节点", 0)
        for node_id, label in discovered:
            self.analysis_node_combo.addItem(label, node_id)
        if current in discovered_ids:
            self.analysis_node_combo.setCurrentIndex(discovered_ids.index(current) + 1)
        else:
            self.analysis_node_combo.setCurrentIndex(0)
        self.analysis_node_combo.blockSignals(False)
        self._known_nodes = discovered

    def _ensure_analysis_curve(self, node_id: int, label: str) -> None:
        if node_id in self._curves:
            return
        index = len(self._curves)
        self._curves[node_id] = self.plot.plot(
            [], [],
            pen=pg.mkPen(self._palette[index % len(self._palette)], width=2.0),
            name=label,
        )

    def _on_analysis_control_changed(self) -> None:
        node_id = int(self.analysis_node_combo.currentData() or 0)
        if node_id:
            self.active_node_changed.emit(node_id)
        self._last_plot_at = 0.0
        self._last_stats_at = 0.0
        self._last_stats_key = None

    def refresh_theme(self) -> None:
        self.plot.setBackground(THEME["card"])
        for axis_name in ("bottom", "left"):
            axis = self.plot.getAxis(axis_name)
            axis.setPen(pg.mkPen(THEME["plot_axis"]))
            axis.setTextPen(pg.mkPen(THEME["plot_axis_text"]))
        self.analysis_empty_label.setStyleSheet(f"color: {THEME['muted']}; padding: 8px;")
        self._palette = [THEME["blue_bright"], THEME["green"], THEME["orange"], THEME["cyan"]]
        for index, curve in enumerate(self._curves.values()):
            curve.setPen(pg.mkPen(self._palette[index % len(self._palette)], width=2.0))
        for card in self.stat_cards.values():
            card.refresh_theme()
        refresh_widget_icons(self)

    def _analysis_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = time.time()
        selected_node = int(self.analysis_node_combo.currentData() or 0)
        seconds = int(self.window_combo.currentData() or 0)
        rows = history
        if selected_node:
            rows = [s for s in rows if int(s.get("node_id") or 0) == selected_node]
        if seconds:
            rows = [s for s in rows if now - _float(s.get("timestamp"), now) <= seconds]
        return rows


# ===========================================================================
# 技术诊断页
# ===========================================================================
class DiagnosticsPage(QWidget):
    """链路质量诊断 + 系统信息。"""

    diagnostics_requested = pyqtSignal()
    gateway_ota_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._link_labels: dict[int, dict[str, QLabel]] = {}
        self._latest_snapshot: dict[str, Any] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)

        title = QLabel("技术诊断")
        title.setObjectName("SectionTitle")
        title.setStyleSheet("font-size: 19px; font-weight: 700;")
        subtitle = QLabel("已发现节点链路质量与网关状态")
        subtitle.setObjectName("SectionSub")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        run_btn = QPushButton("本地链路自检")
        run_btn.setObjectName("PrimaryButton")
        run_btn.clicked.connect(self.diagnostics_requested.emit)
        copy_btn = QPushButton("复制报告")
        copy_btn.setObjectName("GhostButton")
        copy_btn.clicked.connect(self._copy_report)
        ota_btn = QPushButton("Gateway 01 OTA")
        ota_btn.setObjectName("GhostButton")
        ota_btn.clicked.connect(self.gateway_ota_requested.emit)
        actions.addWidget(run_btn)
        actions.addWidget(copy_btn)
        actions.addWidget(ota_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        link_card = CardFrame()
        link_layout = QVBoxLayout(link_card)
        link_layout.setContentsMargins(20, 18, 20, 10)
        link_layout.setSpacing(0)

        link_title = QLabel("节点链路质量 (LINK QUALITY)")
        link_title.setObjectName("SectionTitle")
        link_layout.addWidget(link_title)
        link_layout.addSpacing(8)

        self._link_grid = QGridLayout()
        self._link_grid.setHorizontalSpacing(18)
        self._link_grid.setVerticalSpacing(10)
        headers = ("节点", "状态", "RSSI", "SNR", "丢包率", "电池")
        for col, name in enumerate(headers):
            head = QLabel(name)
            head.setObjectName("ColHeader")
            self._link_grid.addWidget(head, 0, col)
        self._link_empty = QLabel("等待 Gateway 节点接入")
        self._link_empty.setObjectName("SubtleText")
        self._link_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._link_grid.addWidget(self._link_empty, 1, 0, 1, len(headers))
        link_layout.addLayout(self._link_grid)
        layout.addWidget(link_card)

        info_card = CardFrame()
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(20, 18, 20, 18)
        info_layout.setSpacing(10)
        info_title = QLabel("系统信息 (SYSTEM)")
        info_title.setObjectName("SectionTitle")
        info_layout.addWidget(info_title)

        self.info_labels: dict[str, QLabel] = {}
        for key, label in (
            ("gateway", "网关 ID"),
            ("control", "控制台 ID"),
            ("mode", "数据来源"),
            ("afh", "自动频率跳变 (AFH)"),
            ("mesh", "多级网格中继"),
            ("presence", "存在感应阈值"),
            ("gas", "CO2 估算 ppm 阈值"),
        ):
            row = QHBoxLayout()
            caption = QLabel(label)
            caption.setObjectName("SubtleText")
            value = QLabel("--")
            value.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 14px; font-weight: 600;")
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            row.addWidget(caption)
            row.addStretch(1)
            row.addWidget(value)
            info_layout.addLayout(row)
            self.info_labels[key] = value
        layout.addWidget(info_card)

        report_card = CardFrame()
        report_layout = QVBoxLayout(report_card)
        report_layout.setContentsMargins(20, 18, 20, 18)
        report_layout.setSpacing(10)
        report_title = QLabel("诊断报告 (LOCAL REPORT)")
        report_title.setObjectName("SectionTitle")
        self.report_box = QTextEdit()
        self.report_box.setReadOnly(True)
        self.report_box.setMinimumHeight(138)
        self.report_box.setText("尚未生成诊断报告。点击“本地链路自检”后会基于当前真实快照生成摘要。")
        self.report_box.setStyleSheet(
            f"background: {THEME['card_alt']}; border: 1px solid {THEME['border']};"
            f" border-radius: 8px; color: {THEME['text_soft']}; padding: 8px;"
        )
        report_layout.addWidget(report_title)
        report_layout.addWidget(self.report_box)
        layout.addWidget(report_card)
        layout.addStretch(1)

    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._latest_snapshot = snapshot
        nodes: dict[int, dict[str, Any]] = snapshot.get("nodes", {})
        config: dict[str, Any] = snapshot.get("config", {})

        discovered = [
            (node_id, state)
            for node_id, state in sorted(nodes.items())
            if state.get("last_received") is not None
        ]
        self._link_empty.setVisible(not bool(discovered))

        for node_id, state in discovered:
            cells = self._ensure_link_row(node_id, _node_label(node_id, state))
            state = nodes.get(node_id, {})
            online = bool(state.get("online"))
            cells["name"].setText(_node_label(node_id, state))
            cells["status"].setText("在线" if online else "离线")
            cells["status"].setStyleSheet(
                f"color: {THEME['green'] if online else THEME['red']}; font-size: 14px; font-weight: 600;"
            )
            cells["rssi"].setText(f"{_float(state.get('rssi')):.0f} dBm")
            cells["snr"].setText(f"{_float(state.get('snr')):.1f} dB")
            loss = _float(state.get("packet_loss"))
            cells["loss"].setText(f"{loss:.2f}%")
            cells["loss"].setStyleSheet(
                f"color: {THEME['red'] if loss >= 8 else THEME['text_soft']}; font-size: 14px;"
            )
            if state.get("battery") is None:
                cells["battery"].setText("未上报")
            else:
                cells["battery"].setText(f"{_float(state.get('battery')):.0f}%")

        self.info_labels["gateway"].setText(GATEWAY_ID)
        self.info_labels["control"].setText(str(config.get("control_id", CONTROL_ID)))
        self.info_labels["mode"].setText(
            "串口实时" if snapshot.get("serial_connected") else "未连接串口"
        )
        self.info_labels["afh"].setText("开启" if config.get("afh_enabled") else "关闭")
        self.info_labels["mesh"].setText("开启" if config.get("mesh_enabled") else "关闭")
        self.info_labels["presence"].setText(f"{_float(config.get('presence_threshold')) * 100:.0f}%")
        self.info_labels["gas"].setText(f"{_float(config.get('gas_threshold_ppm', config.get('gas_threshold'))):.0f} ppm")
        report = str(snapshot.get("diagnostics_report") or "")
        if report:
            self.report_box.setText(report)

    def _ensure_link_row(self, node_id: int, label: str) -> dict[str, QLabel]:
        cells = self._link_labels.get(node_id)
        if cells is not None:
            return cells
        row_index = len(self._link_labels) + 2
        cells = {}
        name = QLabel(label)
        name.setObjectName("NodeCode")
        self._link_grid.addWidget(name, row_index, 0)
        cells["name"] = name
        for col, key in enumerate(("status", "rssi", "snr", "loss", "battery"), start=1):
            lbl = QLabel("--")
            lbl.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 14px;")
            self._link_grid.addWidget(lbl, row_index, col)
            cells[key] = lbl
        self._link_labels[node_id] = cells
        return cells

    def _copy_report(self) -> None:
        QApplication.clipboard().setText(self.report_box.toPlainText())

    def refresh_theme(self) -> None:
        self.report_box.setStyleSheet(
            f"background: {THEME['card_alt']}; border: 1px solid {THEME['border']};"
            f" border-radius: 8px; color: {THEME['text_soft']}; padding: 8px;"
        )
        for cells in self._link_labels.values():
            for key, label in cells.items():
                if key not in {"status", "loss"}:
                    label.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 14px;")
        for value in self.info_labels.values():
            value.setStyleSheet(f"color: {THEME['text_soft']}; font-size: 14px; font-weight: 600;")
        if self._latest_snapshot:
            self.update_snapshot(self._latest_snapshot)
        refresh_widget_icons(self)


# ===========================================================================
# 历史记录页
# ===========================================================================
class HistoryPage(QWidget):
    """可滚动历史样本表 + CSV 导出。"""

    export_csv_requested = pyqtSignal()
    export_filtered_csv_requested = pyqtSignal(object)
    clear_history_requested = pyqtSignal()

    _COLUMNS = ("时间", "节点", "来源", "有效性", "存在", "运动", "置信度", "CO2 ppm", "温度", "湿度", "RSSI")
    _TABLE_MAX_ROWS = 500

    def __init__(self) -> None:
        super().__init__()
        self._last_refresh_at = 0.0
        self._latest_filtered: list[dict[str, Any]] = []
        self._displayed_rows: list[dict[str, Any]] = []
        self._known_nodes: list[tuple[int, str]] = []
        self._export_ok = True
        self._last_render_key: tuple[Any, ...] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title = QLabel("历史记录")
        title.setObjectName("SectionTitle")
        title.setStyleSheet("font-size: 19px; font-weight: 700;")
        self.subtitle = QLabel("最近 0 条样本")
        self.subtitle.setObjectName("SectionSub")
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.history_node_combo = QComboBox()
        self.history_node_combo.addItem("全部节点", 0)
        self.history_node_combo.currentIndexChanged.connect(self._invalidate_table_render)
        self.history_limit_combo = QComboBox()
        for label, limit in (("最新 100", 100), ("最新 300", 300), ("最新 500", 500)):
            self.history_limit_combo.addItem(label, limit)
        self.history_limit_combo.setCurrentIndex(1)
        self.history_limit_combo.currentIndexChanged.connect(self._invalidate_table_render)
        export_btn = QPushButton("CSV 导出")
        export_btn.setObjectName("PrimaryButton")
        export_btn.clicked.connect(self.export_csv_requested.emit)
        export_filter_btn = QPushButton("导出筛选")
        export_filter_btn.setObjectName("GhostButton")
        export_filter_btn.clicked.connect(lambda: self.export_filtered_csv_requested.emit(list(self._latest_filtered)))
        clear_btn = QPushButton("清空历史")
        clear_btn.setObjectName("DangerButton")
        clear_btn.clicked.connect(self.clear_history_requested.emit)
        header.addWidget(self.history_node_combo)
        header.addWidget(self.history_limit_combo)
        header.addWidget(export_btn)
        header.addWidget(export_filter_btn)
        header.addWidget(clear_btn)
        layout.addLayout(header)
        self.empty_label = QLabel("暂无历史样本，请连接网关后开始采集")
        self.empty_label.setObjectName("SubtleText")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet(f"color: {THEME['muted']}; padding: 8px;")
        layout.addWidget(self.empty_label)

        self.table = QTableWidget(0, len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.cellDoubleClicked.connect(self._show_sample_detail)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        fixed_widths = (86, 86, 78, 72, 72, 78, 66, 70, 70, 78)
        for col, width in enumerate(fixed_widths, start=1):
            header_view.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(col, width)
        layout.addWidget(self.table, 1)

    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not self.isVisible():
            return
        nodes: dict[int, dict[str, Any]] = snapshot.get("nodes", {})
        history: list[dict[str, Any]] = snapshot.get("history", [])
        config: dict[str, Any] = snapshot.get("config", {})
        presence_threshold = _float(config.get("presence_threshold"), PRESENCE_THRESHOLD)
        history_total = int(snapshot.get("history_total") or len(history))
        self._refresh_history_nodes(nodes)
        filtered = self._filter_history(history)
        self._latest_filtered = filtered
        limit = min(int(self.history_limit_combo.currentData() or self._TABLE_MAX_ROWS), self._TABLE_MAX_ROWS)
        recent = filtered[-limit:][::-1]
        self.subtitle.setText(
            f"最近 {history_total} 条样本（当前筛选 {len(filtered)} 条，表格显示 {len(recent)} 条）"
        )
        self.empty_label.setVisible(not bool(recent))

        last_sample = filtered[-1] if filtered else {}
        render_key = (
            len(history),
            len(filtered),
            int(self.history_node_combo.currentData() or 0),
            limit,
            last_sample.get("node_id"),
            last_sample.get("seq"),
            last_sample.get("timestamp"),
            presence_threshold,
        )
        if render_key == self._last_render_key:
            return

        limit = int(self.history_limit_combo.currentData() or 0)
        limit = min(limit or self._TABLE_MAX_ROWS, self._TABLE_MAX_ROWS)
        recent = filtered[-limit:][::-1]

        now = time.time()
        if now - self._last_refresh_at < 1.0:
            return
        self._last_refresh_at = now
        self._last_render_key = render_key
        self._displayed_rows = recent

        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(recent))
            for row, sample in enumerate(recent):
                ts = _float(sample.get("timestamp"), now)
                values = (
                    time.strftime("%H:%M:%S", time.localtime(ts)),
                    str(sample.get("node_code", sample.get("node_id", ""))),
                    _data_source_label(sample.get("source")),
                    _sample_validity(sample, presence_threshold),
                    f"{_score(sample.get('presence_score')):.2f}",
                    f"{_score(sample.get('motion_score')):.2f}",
                    f"{_score(sample.get('confidence')) * 100:.0f}%",
                    f"{_float(sample.get('gas_ppm', sample.get('gas'))):.0f}",
                    f"{_float(sample.get('temperature')):.1f}",
                    f"{_float(sample.get('humidity')):.0f}",
                    f"{_float(sample.get('rssi')):.0f}",
                )
                for col, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.table.setItem(row, col, item)
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)

    def _filter_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        node_id = int(self.history_node_combo.currentData() or 0)
        if not node_id:
            return history
        return [sample for sample in history if int(sample.get("node_id") or 0) == node_id]

    def _invalidate_table_render(self, *_args: Any) -> None:
        self._last_refresh_at = 0.0
        self._last_render_key = None

    def _refresh_history_nodes(self, nodes: dict[int, dict[str, Any]]) -> None:
        discovered = [
            (node_id, _node_label(node_id, state))
            for node_id, state in sorted(nodes.items())
            if state.get("last_received") is not None
        ]
        discovered_ids = [node_id for node_id, _label in discovered]
        if discovered == self._known_nodes:
            return
        current = int(self.history_node_combo.currentData() or 0)
        self.history_node_combo.blockSignals(True)
        self.history_node_combo.clear()
        self.history_node_combo.addItem("全部节点", 0)
        for node_id, label in discovered:
            self.history_node_combo.addItem(label, node_id)
        if current in discovered_ids:
            self.history_node_combo.setCurrentIndex(discovered_ids.index(current) + 1)
        else:
            self.history_node_combo.setCurrentIndex(0)
        self.history_node_combo.blockSignals(False)
        self._known_nodes = discovered
        self._invalidate_table_render()

    def _show_sample_detail(self, row: int, _col: int) -> None:
        if row < 0 or row >= len(self._displayed_rows):
            return
        sample = self._displayed_rows[row]
        _show_detail_dialog(
            self,
            "历史样本详情",
            (
                ("时间戳", sample.get("timestamp", "-")),
                ("节点", sample.get("node_code", sample.get("node_id", "-"))),
                ("序号", sample.get("seq", "-")),
                ("数据来源", _data_source_label(sample.get("source"))),
                ("Presence", f"{_score(sample.get('presence_score')):.2f}"),
                ("Motion", f"{_score(sample.get('motion_score')):.2f}"),
                ("Confidence", f"{_score(sample.get('confidence')) * 100:.0f}%"),
                ("呼吸锁定", _format_optional_bool(sample.get("breath_lock"))),
                ("噪声基线", _format_optional_float(sample.get("noise_floor"))),
                ("CO2 估算 ppm", f"{_float(sample.get('gas_ppm', sample.get('gas'))):.0f} ppm"),
                ("气体原始值", f"{_float(sample.get('gas_raw')):.0f}"),
                ("Raw", sample.get("raw", "")),
            ),
        )

    def set_export_message(self, text: str, ok: bool = True) -> None:
        self._export_ok = ok

    def refresh_theme(self) -> None:
        self.empty_label.setStyleSheet(f"color: {THEME['muted']}; padding: 8px;")
        refresh_widget_icons(self)

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        self._invalidate_table_render()


# ---------------------------------------------------------------------------
def _data_source_label(source: Any) -> str:
    labels = {
        "serial": "真实串口",
        "serial_real": "真实串口",
        "mobile": "手机演示",
        "mobile_override": "手机演示",
        "control": "本地演示",
        "demo_mode": "本地演示",
        "replay": "文件回放",
    }
    normalized = str(source or "serial_real").strip().lower()
    return labels.get(normalized, normalized or "未知")


def _show_detail_dialog(parent: QWidget, title: str, rows: tuple[tuple[str, Any], ...]) -> None:
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(460)
    dialog.setStyleSheet(
        f"QDialog {{ background: {THEME['bg']}; }}"
        f"QLabel {{ color: {THEME['text_soft']}; }}"
    )

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(14)

    title_label = QLabel(title)
    title_label.setObjectName("SectionTitle")
    title_label.setStyleSheet("font-size: 17px; font-weight: 700;")
    layout.addWidget(title_label)

    grid = QGridLayout()
    grid.setHorizontalSpacing(18)
    grid.setVerticalSpacing(10)
    for row_index, (name, value) in enumerate(rows):
        key_label = QLabel(str(name))
        key_label.setObjectName("SubtleText")
        value_label = QLabel(str(value))
        value_label.setWordWrap(True)
        value_label.setStyleSheet(f"color: {THEME['text']}; font-size: 14px; font-weight: 600;")
        grid.addWidget(key_label, row_index, 0)
        grid.addWidget(value_label, row_index, 1)
    layout.addLayout(grid)

    close_btn = QPushButton("关闭")
    close_btn.setObjectName("PrimaryButton")
    close_btn.clicked.connect(dialog.accept)
    footer = QHBoxLayout()
    footer.addStretch(1)
    footer.addWidget(close_btn)
    layout.addLayout(footer)
    dialog.exec()


def _menu_qss() -> str:
    return (
        f"QMenu {{ background: {THEME['card_alt']}; border: 1px solid {THEME['border']};"
        f" border-radius: 10px; padding: 6px; }}"
        f"QMenu::item {{ color: {THEME['text_soft']}; padding: 8px 22px 8px 12px; border-radius: 6px; }}"
        f"QMenu::item:selected {{ background: {THEME['blue']}; color: {THEME['selection_text']}; }}"
        f"QMenu::item:disabled {{ color: {THEME['muted_2']}; }}"
    )


def _node_label(node_id: int, state: dict[str, Any] | None = None) -> str:
    label = str((state or {}).get("label") or "").strip()
    if label:
        return label
    if node_id <= 0:
        return "等待节点接入"
    return NODE_LABELS.get(node_id, f"node{node_id}")


def _matrix_advice(state: dict[str, Any]) -> tuple[str, str]:
    if state.get("maintenance"):
        return "请检查节点", THEME["red"]
    if not state.get("online"):
        return "等待数据", THEME["muted"]
    rssi = _float(state.get("rssi"))
    if rssi < -95:
        return "信号较弱", THEME["orange"]
    if state.get("health") == HEALTH_CRITICAL:
        return "请检查节点", THEME["red"]
    return "正常监测", THEME["blue_soft"]


def _sample_validity(sample: dict[str, Any], presence_threshold: float = PRESENCE_THRESHOLD) -> str:
    confidence = _score(sample.get("confidence"))
    gas = _float(sample.get("gas"))
    if gas >= 550:
        return "异常"
    if life_motion_triggered(sample, presence_threshold=presence_threshold):
        return "疑似微动"
    if confidence and confidence < 0.45:
        return "低置信"
    return "有效"


def _format_optional_score(value: Any) -> str:
    if value is None:
        return "--"
    return f"{_score(value):.2f}"


def _format_optional_int(value: Any) -> str:
    if value is None:
        return "--"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "--"


def _format_optional_float(value: Any) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "--"


def _format_optional_bool(value: Any) -> str:
    if value is None:
        return "--"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return "是" if bool(value) else "否"
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return "是"
    if text in {"0", "false", "no", "n", "off"}:
        return "否"
    return "--"


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
