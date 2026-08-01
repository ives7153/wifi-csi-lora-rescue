"""上位机数据协调层。

中文注释：DataManager 是 UI 与硬件 / 规则 / 导出之间的中间层。UI 不直接读串口、
不直接解析 JSON、不直接跑报警规则，只接收这里发出的 Qt 信号并刷新控件。
这样串口后台读取不会阻塞主线程，同时保持 serial_handler.py / data_parser.py 的兼容性。

默认运行时只驱动真实串口数据：收到 Gateway JSON 后按节点 id 自动创建节点状态与矩阵行。
控制版入口会额外启用面板驱动的样本注入，但仍复用同一条状态、历史和规则链路。
"""

from __future__ import annotations

import math
import time
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QWidget


MOBILE_CONTROL_NODE_IDS = frozenset((1, 2, 3))


try:
    from ..config import (
        AUTO_PORT_REFRESH_MS,
        BAUDRATE,
        CONTROL_ID,
        CSI_QUALITY_THRESHOLD,
        DEFAULT_AFH_ENABLED,
        DEFAULT_MESH_ENABLED,
        GAS_THRESHOLD_PPM,
        GATEWAY_ID,
        EXPORT_DIR,
        HEALTH_CRITICAL,
        HEALTH_EXCELLENT,
        HEALTH_GOOD,
        HEALTH_INACTIVE,
        MAX_EVENT_ROWS,
        MAX_HISTORY_ROWS,
        NODE_LABELS,
        OFFLINE_SECONDS,
        PRESENCE_THRESHOLD,
        UI_REFRESH_MS,
        load_ui_settings,
        save_ui_settings,
    )
    from ..data_parser import parse_gateway_frame
    from ..ai import (
        AISettings,
        LocalJinaRuntime,
        build_ai_detail,
        create_jina_offline_package,
        deploy_jina_package,
        fetch_llm_models,
        fetch_llm_models_result,
        import_jina_model,
        jina_deployment_status,
        load_ai_settings,
        online_deploy_jina,
        run_ai_chat,
        run_ai_judgement,
        save_ai_settings,
        settings_from_dict,
        test_embedding,
        test_llm,
        test_llm_result,
        wait_for_embedding_ready,
    )
    from ..rules.detection_fusion import ai_fallback_text, build_detection_summary, life_motion_triggered
    from ..serial_handler import SerialReader, open_serial_port
    from ..recording import GatewayRecorder, RecordedLine, load_recording
    from .alarm_rules import AlarmEngine
    from ..gas_calibration import (
        DEFAULT_MQ135_R0_KOHM,
        MQ135_CLEAN_AIR_PPM,
        calculate_gas_ppm,
        calibrate_r0_from_clean_air_raw,
    )
    from .exporter import (
        export_samples_to_csv,
        save_csi_screenshot,
        save_widget_screenshot,
    )
except ImportError:  # 兼容在 upper_computer 目录下直接 python main.py
    if __package__ and __package__.startswith("upper_computer"):
        raise
    from config import (
        AUTO_PORT_REFRESH_MS,
        BAUDRATE,
        CONTROL_ID,
        CSI_QUALITY_THRESHOLD,
        DEFAULT_AFH_ENABLED,
        DEFAULT_MESH_ENABLED,
        GAS_THRESHOLD_PPM,
        GATEWAY_ID,
        EXPORT_DIR,
        HEALTH_CRITICAL,
        HEALTH_EXCELLENT,
        HEALTH_GOOD,
        HEALTH_INACTIVE,
        MAX_EVENT_ROWS,
        MAX_HISTORY_ROWS,
        NODE_LABELS,
        OFFLINE_SECONDS,
        PRESENCE_THRESHOLD,
        UI_REFRESH_MS,
        load_ui_settings,
        save_ui_settings,
    )
    from data_parser import parse_gateway_frame
    from ai import (  # type: ignore
        AISettings,
        LocalJinaRuntime,
        build_ai_detail,
        create_jina_offline_package,
        deploy_jina_package,
        fetch_llm_models,
        fetch_llm_models_result,
        import_jina_model,
        jina_deployment_status,
        load_ai_settings,
        online_deploy_jina,
        run_ai_chat,
        run_ai_judgement,
        save_ai_settings,
        settings_from_dict,
        test_embedding,
        test_llm,
        test_llm_result,
        wait_for_embedding_ready,
    )
    from rules.detection_fusion import ai_fallback_text, build_detection_summary, life_motion_triggered  # type: ignore
    from serial_handler import SerialReader, open_serial_port
    from recording import GatewayRecorder, RecordedLine, load_recording
    from core.alarm_rules import AlarmEngine
    from gas_calibration import (
        DEFAULT_MQ135_R0_KOHM,
        MQ135_CLEAN_AIR_PPM,
        calculate_gas_ppm,
        calibrate_r0_from_clean_air_raw,
    )
    from core.exporter import (
        export_samples_to_csv,
        save_csi_screenshot,
        save_widget_screenshot,
    )


@dataclass(slots=True)
class NodeState:
    """单个生命体征感知节点当前状态。

    中文注释：字段命名沿用 data_parser.py 的规范化结果，UI 与报警规则可直接消费。
    """

    node_id: int
    label: str
    online: bool = False
    rssi: float = 0.0
    wifi_rssi: float = -42.0
    snr: float = 0.0
    packet_loss: float = 0.0
    battery: float | None = None
    last_received: float | None = None
    created_at: float = field(default_factory=time.time)
    seq: int | None = None
    presence_score: float = 0.0
    motion_score: float = 0.0
    breath_bpm: float = 0.0
    confidence: float = 0.0
    csi_quality: float | None = None
    csi_sample_count: int | None = None
    breath_lock: bool | None = None
    noise_floor: float | None = None
    gas: float = 0.0
    gas_raw: float = 0.0
    gas_ppm: float = 0.0
    temperature: float = 0.0
    humidity: float = 0.0
    source: str = "serial_real"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MatrixNodeState:
    """LoRa 节点矩阵中的一行（传感器页使用）。"""

    matrix_id: int
    code: str
    mode: str
    bound_node: int | None = None
    online: bool = False
    rssi: float = -110.0
    battery: float | None = None
    health: str = HEALTH_INACTIVE
    last_received: float | None = None
    maintenance: bool = False
    local: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ControlNodeState:
    """控制版上位机的节点注入状态。"""

    node_id: int
    enabled: bool = False
    csi_value: int = 0
    seq: int = 0


@dataclass(slots=True)
class EventRecord:
    """右侧事件流记录。"""

    time: float
    title: str
    message: str
    node_id: int = 0
    level: str = "INFO"
    kind: str = "system"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SerialWorker(QObject):
    """运行在 QThread 中的串口 Worker。

    中文注释：Worker 内部复用 SerialReader；SerialReader 自身创建 Python 后台线程
    读取串口，回调触发 Qt 信号，Qt 会自动把信号投递回主线程。
    """

    frames_received = pyqtSignal(object, object)
    error = pyqtSignal(str)
    opened = pyqtSignal(str)
    closed = pyqtSignal()
    _BATCH_INTERVAL_SECONDS = 0.06
    _MAX_QUEUE_LINES = 5000
    _MAX_BATCH_LINES = 1200

    def __init__(self, port: str, baudrate: int) -> None:
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self._reader: SerialReader | None = None
        self._pending_lines: deque[str] = deque()
        self._pending_lock = threading.Lock()
        self._batch_stop = threading.Event()
        self._batch_thread: threading.Thread | None = None
        self._dropped_lines = 0

    @pyqtSlot()
    def start_reader(self) -> None:
        try:
            self._start_batch_thread()
            self._reader = SerialReader()
            self._reader.start(
                port=self.port,
                baudrate=self.baudrate,
                on_line=self._enqueue_line,
                on_error=self.error.emit,
            )
        except Exception as exc:  # noqa: BLE001 - 串口占用 / 权限 / 拔插异常需回传 UI。
            self._stop_batch_thread()
            self.error.emit(str(exc))
            return

        self.opened.emit(self.port)

    @pyqtSlot()
    def stop_reader(self) -> None:
        if self._reader:
            self._reader.stop()
            self._reader = None
        self._stop_batch_thread()
        self.closed.emit()

    def _start_batch_thread(self) -> None:
        with self._pending_lock:
            self._pending_lines.clear()
            self._dropped_lines = 0
        self._batch_stop.clear()
        self._batch_thread = threading.Thread(
            target=self._batch_loop,
            name="gateway-serial-batcher",
            daemon=True,
        )
        self._batch_thread.start()

    def _stop_batch_thread(self) -> None:
        self._batch_stop.set()
        thread = self._batch_thread
        if thread and thread.is_alive():
            thread.join(timeout=0.8)
        self._batch_thread = None

    def _enqueue_line(self, line: str) -> None:
        with self._pending_lock:
            if len(self._pending_lines) >= self._MAX_QUEUE_LINES:
                self._pending_lines.popleft()
                self._dropped_lines += 1
            self._pending_lines.append(line)

    def _batch_loop(self) -> None:
        while not self._batch_stop.wait(self._BATCH_INTERVAL_SECONDS):
            self._flush_pending_lines()
        self._flush_pending_lines()

    def _flush_pending_lines(self) -> None:
        with self._pending_lock:
            if not self._pending_lines and not self._dropped_lines:
                return
            line_count = min(len(self._pending_lines), self._MAX_BATCH_LINES)
            lines = [self._pending_lines.popleft() for _ in range(line_count)]
            dropped = self._dropped_lines
            self._dropped_lines = 0
            pending = len(self._pending_lines)

        frames: list[dict[str, Any]] = []
        invalid = 0
        last_raw = ""
        for line in lines:
            parsed = parse_gateway_frame(line)
            if parsed.get("valid"):
                parsed["timestamp"] = time.time()
                parsed["source"] = "serial_real"
                frames.append(parsed)
                last_raw = str(parsed.get("raw", line))
            else:
                invalid += 1
                last_raw = line

        stats = {
            "total": len(lines),
            "valid": len(frames),
            "invalid": invalid,
            "dropped": dropped,
            "pending": pending,
            "last_raw": last_raw,
            "raw_lines": lines,
        }
        if frames or invalid or dropped:
            self.frames_received.emit(frames, stats)


class DataManager(QObject):
    """应用数据中心：串口、规则、历史、节点矩阵、控制样本和导出。"""

    snapshot_changed = pyqtSignal(object)
    status_changed = pyqtSignal(str, bool)
    latest_frame_changed = pyqtSignal(str)
    ports_changed = pyqtSignal(object, object)
    export_message_changed = pyqtSignal(str, bool)
    ai_operation_message_changed = pyqtSignal(str, bool)
    ai_operation_result_changed = pyqtSignal(object)
    ai_models_changed = pyqtSignal(object)
    recording_state_changed = pyqtSignal(bool, str)
    _ai_analysis_ready = pyqtSignal(int, object)
    _ai_chat_ready = pyqtSignal(int, object)
    _ai_operation_ready = pyqtSignal(str, bool)
    _ai_operation_result_ready = pyqtSignal(object)
    _ai_models_ready = pyqtSignal(object, bool, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        self.started_at = time.time()
        self.available_ports: list[str] = []
        self._preferred_gateway_ports: list[str] = []
        self.selected_port = ""
        self.history: list[dict[str, Any]] = []
        self.events: list[EventRecord] = []
        self.ai_settings: AISettings = load_ai_settings()
        self.ai_runtime = LocalJinaRuntime()
        self.ai_state: dict[str, Any] = {
            "enabled": self.ai_settings.enabled,
            "running": False,
            "status": "规则回退",
            "text": ai_fallback_text("等待数据"),
            "source": "rule_fallback",
            "detail": build_ai_detail(
                build_detection_summary({}, [], reference_ts=time.time())
            ),
            "detail_source": "rule_fallback",
            "detail_updated_at": 0.0,
            "detail_history": [],
            "chat_history": [],
            "chat_running": False,
            "chat_error": "",
            "chat_source": "rule_fallback",
            "chat_context_label": "当前快照 + 最近5分钟",
            "window_start": 0.0,
            "window_end": 0.0,
            "updated_at": 0.0,
            "top_matches": [],
            "error": "",
            "config": self._public_ai_config(),
        }
        self._ai_busy = False
        self._ai_request_id = 0
        self._ai_chat_request_id = 0
        self._ai_chat_busy = False
        self._ai_last_started_at = 0.0
        self._ai_last_state_key = ""
        self._ai_detail_event_key = ""

        # 报警引擎（阈值可被传感器页热更新）
        self.alarm_engine = AlarmEngine()
        self.presence_threshold = PRESENCE_THRESHOLD
        self.gas_threshold = GAS_THRESHOLD_PPM
        self.afh_enabled = DEFAULT_AFH_ENABLED
        self.mesh_enabled = DEFAULT_MESH_ENABLED
        self.gas_calibration_r0 = self._load_gas_calibration_r0()
        self.gas_node_calibration_r0 = self._load_gas_node_calibration_r0()
        self.last_sync_at: float | None = None

        # 节点由 Gateway 串口帧自动发现；无真实数据时保持空状态。
        self.nodes: dict[int, NodeState] = {}

        # LoRa 节点矩阵由 Gateway 串口帧自动发现；未收到真实数据前保持空表。
        self.matrix: dict[int, MatrixNodeState] = {}

        self._thread: QThread | None = None
        self._worker: SerialWorker | None = None
        self._serial_connected = False
        self._serial_auto_connected = False
        self._serial_started_at = 0.0
        self._last_serial_sample_at = 0.0
        self._last_frame_ui_at = 0.0
        self._last_invalid_frame_ui_at = 0.0
        self._last_serial_overload_notice_at = 0.0
        self._serial_stats: dict[str, int] = {
            "last_batch_total": 0,
            "last_batch_valid": 0,
            "last_batch_invalid": 0,
            "last_batch_dropped": 0,
            "last_batch_pending": 0,
            "total_valid": 0,
            "total_invalid": 0,
            "total_dropped": 0,
            "total_gateway_status": 0,
        }
        self._gateway_status: dict[str, Any] = {}
        self._dirty = True
        self._active_node = 0
        self._paused = False
        self._matrix_filter = "ALL"
        self._diagnostics_report = ""
        self._presence_flags: dict[int, bool] = {}
        self._breath_lock_flags: dict[int, bool] = {}
        self._offline_flags: dict[int, bool] = {}
        self._gas_alert_flags: dict[int, bool] = {}
        self._control_nodes: dict[int, ControlNodeState] = {}
        self._control_started_at = time.time()
        self._mobile_presence_overrides: dict[int, float] = {}
        self._last_real_samples: dict[int, dict[str, Any]] = {}
        self._recorder = GatewayRecorder()
        self._replay_records: list[RecordedLine] = []
        self._replay_index = 0
        self._replay_started_at = 0.0
        self._replay_base_time = 0.0
        self._replay_invalid_rows = 0
        self._replay_invalid_frames = 0
        self._replay_path: Path | None = None

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(UI_REFRESH_MS)
        self._ui_timer.timeout.connect(self._publish_snapshot)

        self._offline_timer = QTimer(self)
        self._offline_timer.setInterval(1000)
        self._offline_timer.timeout.connect(self._check_offline_nodes)

        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(AUTO_PORT_REFRESH_MS)
        self._auto_timer.timeout.connect(self._auto_service)

        self._ai_timer = QTimer(self)
        self._ai_timer.setInterval(1000)
        self._ai_timer.timeout.connect(self._maybe_schedule_ai_analysis)

        self._control_timer = QTimer(self)
        self._control_timer.setInterval(1000)
        self._control_timer.timeout.connect(self._inject_control_samples)

        self._replay_timer = QTimer(self)
        self._replay_timer.setInterval(25)
        self._replay_timer.timeout.connect(self._replay_tick)

        self._ai_analysis_ready.connect(self._handle_ai_analysis_ready)
        self._ai_chat_ready.connect(self._handle_ai_chat_ready)
        self._ai_operation_ready.connect(self._handle_ai_operation_ready)
        self._ai_operation_result_ready.connect(self._handle_ai_operation_result_ready)
        self._ai_models_ready.connect(self._handle_ai_models_ready)

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """启动数据服务：刷新串口列表并自动探测真实 Gateway。"""

        self._ui_timer.start()
        self._offline_timer.start()
        self._ai_timer.start()
        self.refresh_ports()
        self._auto_timer.start()
        self._auto_service()

    def shutdown(self) -> None:
        """程序退出时释放串口线程与定时器。"""

        self._ui_timer.stop()
        self._offline_timer.stop()
        self._auto_timer.stop()
        self._ai_timer.stop()
        self._control_timer.stop()
        self._replay_timer.stop()
        self._replay_records.clear()
        self._recorder.stop()
        self.stop_serial()
        self.ai_runtime.stop()

    # ------------------------------------------------------------------ UI 槽
    @pyqtSlot()
    def refresh_ports(self) -> None:
        """刷新本机串口列表。"""

        try:
            import serial.tools.list_ports as list_ports

            port_infos = list(list_ports.comports())
            self.available_ports = [port.device for port in port_infos]
            self._preferred_gateway_ports = [
                port.device
                for port in port_infos
                if (port.vid, port.pid) == (0x1A86, 0x7523)
                or "CH340" in str(port.description or "").upper()
            ]
        except Exception:  # noqa: BLE001 - pyserial 缺失或枚举失败时仍可演示。
            self.available_ports = []
            self._preferred_gateway_ports = []

        selected = self.selected_port if self.selected_port in self.available_ports else ""
        self.ports_changed.emit(self.available_ports, selected)

    @pyqtSlot(str)
    def connect_to_port(self, port: str) -> None:
        """手动连接指定串口。"""

        if not port or port == "无可用串口":
            self.status_changed.emit("串口状态：没有可连接的串口", False)
            return

        self._connect_serial(port, auto=False)

    @pyqtSlot()
    def disconnect_serial(self) -> None:
        """手动断开串口。"""

        self.stop_serial()
        self.status_changed.emit("串口状态：已手动断开，等待连接", False)

    @pyqtSlot(int, bool, int)
    def set_control_node(self, node_id: int, enabled: bool, csi_value: int) -> None:
        """控制版：设置某个节点是否由控制面板注入 CSI 0/1 样本。"""

        node_id = int(node_id)
        if node_id <= 0:
            return
        state = self._control_nodes.get(node_id)
        if state is None:
            state = ControlNodeState(node_id=node_id)
            self._control_nodes[node_id] = state
        state.enabled = bool(enabled)
        state.csi_value = 1 if int(csi_value) else 0
        self._sync_control_timer()
        self._inject_control_samples()

    @pyqtSlot(str)
    def apply_control_scene(self, scene_name: str) -> None:
        """控制版：应用预设场景。"""

        normalized = str(scene_name or "").strip().lower()
        if normalized in {"all_zero", "clear_signal"}:
            targets = {node_id: 0 for node_id in NODE_LABELS}
        elif normalized in {"single_node", "node3"}:
            targets = {node_id: (1 if node_id == 3 else 0) for node_id in NODE_LABELS}
        elif normalized in {"multi_node", "multi"}:
            targets = {node_id: (1 if node_id in {1, 2} else 0) for node_id in NODE_LABELS}
        elif normalized in {"clear", "off", "restore"}:
            self.clear_control_nodes()
            return
        else:
            return

        for node_id, csi_value in targets.items():
            state = self._control_nodes.get(node_id)
            if state is None:
                state = ControlNodeState(node_id=node_id)
                self._control_nodes[node_id] = state
            state.enabled = True
            state.csi_value = int(csi_value)
        self._sync_control_timer()
        self._inject_control_samples()

    @pyqtSlot()
    def clear_control_nodes(self) -> None:
        """控制版：关闭所有节点控制，恢复真实串口优先。"""

        for state in self._control_nodes.values():
            state.enabled = False
        self._sync_control_timer()
        self._dirty = True

    @pyqtSlot(int, float)
    def set_mobile_presence(self, node_id: int, value: float) -> None:
        """手机控制版：立即覆盖指定节点的存在感知值。"""

        node_id = int(node_id)
        value = float(value)
        if node_id not in MOBILE_CONTROL_NODE_IDS or not math.isfinite(value) or not 0.0 <= value <= 1.0:
            return

        self._mobile_presence_overrides[node_id] = value
        self._apply_sample(
            self._build_mobile_presence_sample(node_id, value),
            run_rules=True,
            mark_dirty=True,
        )

    @pyqtSlot(int)
    def clear_mobile_presence(self, node_id: int) -> None:
        """手机控制版：取消单个节点的存在感知覆盖。"""

        node_id = int(node_id)
        self._mobile_presence_overrides.pop(node_id, None)
        self._restore_real_node(node_id)
        self._dirty = True

    @pyqtSlot()
    def clear_all_mobile_presence(self) -> None:
        """手机控制版：取消全部节点的存在感知覆盖。"""

        node_ids = set(self._mobile_presence_overrides)
        self._mobile_presence_overrides.clear()
        for node_id in node_ids:
            self._restore_real_node(node_id)
        self._dirty = True

    def _restore_real_node(self, node_id: int) -> None:
        sample = self._last_real_samples.get(int(node_id))
        if sample is not None:
            restored = dict(sample)
            restored["timestamp"] = time.time()
            restored["source"] = "serial_real"
            self._apply_sample(restored, run_rules=True, mark_dirty=False)
            return
        node = self.nodes.get(int(node_id))
        if node is not None:
            node.online = False
            node.last_received = None
            node.presence_score = 0.0
            node.motion_score = 0.0
            node.source = "serial_real"

    @pyqtSlot(bool)
    def set_recording_enabled(self, enabled: bool) -> None:
        """Start or stop lossless Gateway line recording."""

        if not enabled:
            path = self._recorder.stop()
            message = f"录制已保存：{path.name}" if path is not None else "录制未启动"
            self.recording_state_changed.emit(False, message)
            if path is not None:
                self._append_event("RECORDING SAVED", str(path), level="OK", kind="recording")
            return

        if self._recorder.active:
            self.recording_state_changed.emit(True, f"正在录制：{self._recorder.path.name}")
            return
        target = EXPORT_DIR / "recordings" / f"echoguard_session_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        try:
            path = self._recorder.start(target)
        except Exception as exc:  # noqa: BLE001
            self.recording_state_changed.emit(False, f"录制启动失败：{exc}")
            return
        self.recording_state_changed.emit(True, f"正在录制：{path.name}")
        self._append_event("RECORDING STARTED", str(path), level="OK", kind="recording")

    @pyqtSlot(str)
    def replay_recording(self, path_text: str) -> None:
        """Replay a recorded Gateway session through the normal data pipeline."""

        if self._serial_connected:
            self.status_changed.emit("回放前请先断开真实 Gateway 串口，避免数据混合", False)
            return
        path = Path(path_text)
        try:
            records, invalid = load_recording(path)
        except Exception as exc:  # noqa: BLE001
            self.status_changed.emit(f"回放文件读取失败：{exc}", False)
            return
        if not records:
            self.status_changed.emit("回放文件中没有可用 Gateway 记录", False)
            return
        self._replay_records = records
        self._replay_index = 0
        self._replay_started_at = time.monotonic()
        self._replay_base_time = records[0].recorded_at
        self._replay_invalid_rows = invalid
        self._replay_invalid_frames = 0
        self._replay_path = path
        self._replay_timer.start()
        self.status_changed.emit(f"正在回放：{path.name}", True)
        self._append_event("REPLAY STARTED", path.name, level="INFO", kind="replay")

    def _replay_tick(self) -> None:
        if not self._replay_records:
            self._replay_timer.stop()
            return
        elapsed = time.monotonic() - self._replay_started_at
        applied = False
        while self._replay_index < len(self._replay_records):
            record = self._replay_records[self._replay_index]
            if record.recorded_at - self._replay_base_time > elapsed:
                break
            self._replay_index += 1
            parsed = parse_gateway_frame(record.line)
            if not parsed.get("valid") or parsed.get("frame_type") == "gateway_status":
                self._replay_invalid_frames += int(not parsed.get("valid"))
                continue
            parsed["timestamp"] = time.time()
            parsed["source"] = "replay"
            if self._apply_sample(parsed, run_rules=True, mark_dirty=False):
                applied = True
        if applied:
            self._dirty = True
        if self._replay_index < len(self._replay_records):
            return
        self._replay_timer.stop()
        path_name = self._replay_path.name if self._replay_path is not None else "记录文件"
        skipped = self._replay_invalid_rows + self._replay_invalid_frames
        self.status_changed.emit(f"回放完成：{path_name}，跳过 {skipped} 条异常记录", True)
        self._append_event("REPLAY FINISHED", f"{path_name}，跳过 {skipped} 条", level="OK", kind="replay")
        self._replay_records.clear()
        self._replay_path = None

    @pyqtSlot()
    def export_csv(self) -> None:
        """导出当前历史样本为 CSV。"""

        if not self.history:
            self.export_message_changed.emit("暂无历史样本可导出", False)
            return
        try:
            path = export_samples_to_csv(self.history)
        except Exception as exc:  # noqa: BLE001 - 导出失败需落在界面状态上。
            self.export_message_changed.emit(f"CSV 导出失败：{exc}", False)
            return

        self.export_message_changed.emit(f"CSV 已导出：{path.name}", True)
        self._append_event("CSV EXPORTED", f"历史样本已保存到 {path.name}", level="OK", kind="export")

    def save_screenshot(self, widget: QWidget) -> None:
        """保存整窗截图。"""

        try:
            path = save_widget_screenshot(widget)
        except Exception as exc:  # noqa: BLE001
            self.export_message_changed.emit(f"截图失败：{exc}", False)
            return

        self.export_message_changed.emit(f"截图已保存：{path.name}", True)
        self._append_event("SCREENSHOT SAVED", f"控制台截图已保存到 {path.name}", level="OK", kind="export")

    def save_csi_image(self, widget: QWidget) -> None:
        """单独保存扰动曲线截图。"""

        try:
            path = save_csi_screenshot(widget)
        except Exception as exc:  # noqa: BLE001
            self.export_message_changed.emit(f"扰动曲线截图失败：{exc}", False)
            return

        self.export_message_changed.emit(f"扰动曲线截图已保存：{path.name}", True)
        self._append_event("DISTURBANCE SNAPSHOT", f"融合扰动曲线已保存到 {path.name}", level="OK", kind="export")

    @pyqtSlot(object)
    def save_analysis_image(self, widget: QWidget) -> None:
        """保存分析页当前图表截图。"""

        try:
            path = save_csi_screenshot(widget)
        except Exception as exc:  # noqa: BLE001
            self.export_message_changed.emit(f"分析图表截图失败：{exc}", False)
            return

        self.export_message_changed.emit(f"分析图表截图已保存：{path.name}", True)
        self._append_event("ANALYSIS SNAPSHOT", f"分析图表已保存到 {path.name}", level="OK", kind="export")

    @pyqtSlot(object)
    def export_filtered_csv(self, samples: object) -> None:
        """导出页面传入的过滤后样本。"""

        if not isinstance(samples, list) or not samples:
            self.export_message_changed.emit("当前筛选结果为空，无法导出", False)
            return
        try:
            path = export_samples_to_csv(samples)
        except Exception as exc:  # noqa: BLE001
            self.export_message_changed.emit(f"CSV 导出失败：{exc}", False)
            return

        self.export_message_changed.emit(f"CSV 已导出：{path.name}", True)
        self._append_event("CSV EXPORTED", f"筛选样本已保存到 {path.name}", level="OK", kind="export")

    @pyqtSlot(int)
    def set_active_node(self, node_id: int) -> None:
        """切换当前关注节点。"""

        if int(node_id) not in self.nodes:
            return
        self._active_node = int(node_id)
        self._dirty = True

    @pyqtSlot(bool)
    def set_paused(self, paused: bool) -> None:
        """暂停 / 恢复实时样本应用。"""

        self._paused = bool(paused)
        self._append_event(
            "STREAM PAUSED" if self._paused else "STREAM RESUMED",
            "实时刷新已暂停" if self._paused else "实时刷新已恢复",
            level="WARN" if self._paused else "OK",
            kind="ui",
        )

    @pyqtSlot()
    def clear_events(self) -> None:
        """清空右侧实时事件流。"""

        self.events.clear()
        self._dirty = True

    @pyqtSlot()
    def clear_history(self) -> None:
        """清空本次运行内历史样本缓存。"""

        self.history.clear()
        self._append_event("HISTORY CLEARED", "本地历史样本缓存已清空", level="WARN", kind="history")
        self._dirty = True

    @pyqtSlot(object)
    def save_ai_config(self, payload: object) -> None:
        """保存 AI 设置到本机用户配置。"""

        if not isinstance(payload, dict):
            self.ai_operation_message_changed.emit("AI 设置格式无效", False)
            return
        try:
            path = self._apply_ai_settings(payload)
        except Exception as exc:  # noqa: BLE001
            self.ai_operation_message_changed.emit(f"AI 设置保存失败：{exc}", False)
            return
        self.ai_state["enabled"] = self.ai_settings.enabled
        self.ai_state["config"] = self._public_ai_config()
        self.ai_state["status"] = "AI 设置已保存"
        self._ai_last_state_key = ""
        self.ai_operation_message_changed.emit(f"AI 设置已保存：{path}", True)
        self._append_event("AI CONFIG SAVED", "AI 辅助研判设置已保存", level="OK", kind="ai")
        self._dirty = True

    @pyqtSlot(object)
    def handle_ai_action(self, payload: object) -> None:
        """统一处理 AI 设置弹窗动作，动作直接携带当前表单配置。"""

        if not isinstance(payload, dict):
            self._ai_operation_result_ready.emit(
                {"action": "unknown", "ok": False, "message": "AI 操作格式无效"}
            )
            return
        action = str(payload.get("action") or "").strip()
        if action == "refresh_ai_detail":
            self._schedule_ai_analysis(force=True, manual=True)
            return
        if action == "ask_ai_chat":
            self._schedule_ai_chat(str(payload.get("question") or ""))
            return
        if action == "clear_ai_chat":
            self.ai_state["chat_history"] = []
            self.ai_state["chat_error"] = ""
            self.ai_state["chat_running"] = False
            self.ai_state["chat_source"] = "rule_fallback"
            self.ai_state["chat_context_label"] = "当前快照 + 最近5分钟"
            self._dirty = True
            return
        if action == "save":
            config = payload.get("config")
            if not isinstance(config, dict):
                self._ai_operation_result_ready.emit(
                    {"action": action, "ok": False, "message": "AI 设置格式无效"}
                )
                return
            try:
                self._apply_ai_settings(config)
            except Exception as exc:  # noqa: BLE001
                self._ai_operation_result_ready.emit(
                    {"action": action, "ok": False, "message": f"AI 设置保存失败：{exc}"}
                )
                return
            self._ai_operation_result_ready.emit(
                {"action": action, "ok": True, "message": "AI 设置已保存", "config": self._public_ai_config()}
            )
            return
        if action == "stop_jina":
            try:
                message = self.ai_runtime.stop()
            except Exception as exc:  # noqa: BLE001
                self._ai_operation_result_ready.emit({"action": action, "ok": False, "message": str(exc)})
                return
            self._ai_operation_result_ready.emit({"action": action, "ok": True, "message": message})
            return

        settings = self.ai_settings.copy()
        config = payload.get("config")
        if isinstance(config, dict):
            try:
                settings = settings_from_dict(config)
            except Exception as exc:  # noqa: BLE001
                self._ai_operation_result_ready.emit(
                    {"action": action, "ok": False, "message": f"AI 设置格式无效：{exc}"}
                )
                return
        package_path = str(payload.get("package_path") or "").strip()

        def emit_progress(progress: dict[str, Any]) -> None:
            progress_message = str(progress.get("message") or "AI 操作执行中...")
            progress_payload = {
                "action": action,
                "ok": True,
                "running": True,
                "message": progress_message,
            }
            progress_payload.update(progress)
            self._ai_operation_result_ready.emit(progress_payload)

        def worker() -> dict[str, Any]:
            if action == "check_jina_deployment":
                status = jina_deployment_status(settings)
                return {
                    "action": action,
                    "ok": True,
                    "message": status["message"],
                    "deployed": status["deployed"],
                    "port_open": status.get("port_open", False),
                    "runtime_ok": status.get("runtime_ok", False),
                    "runtime_message": status.get("runtime_message", ""),
                    "model_ok": status.get("model_ok", False),
                    "model_message": status.get("model_message", ""),
                    "server_path": status["server_path"],
                    "model_path": status["model_path"],
                    "endpoint": status["endpoint"],
                    "provider": "local_jina",
                    "real_request": False,
                }
            if action == "deploy_jina_package":
                status = deploy_jina_package(settings, package_path, overwrite=True)
                return {
                    "action": action,
                    "ok": True,
                    "message": status["message"],
                    "deployed": status["deployed"],
                    "port_open": status.get("port_open", False),
                    "runtime_ok": status.get("runtime_ok", False),
                    "runtime_message": status.get("runtime_message", ""),
                    "model_ok": status.get("model_ok", False),
                    "model_message": status.get("model_message", ""),
                    "server_path": status["server_path"],
                    "model_path": status["model_path"],
                    "endpoint": status["endpoint"],
                    "provider": "local_jina",
                    "real_request": False,
                }
            if action == "online_deploy_jina":
                status = online_deploy_jina(settings, progress=emit_progress, overwrite=False)
                return {
                    "action": action,
                    "ok": True,
                    "message": status["message"],
                    "deployed": status["deployed"],
                    "port_open": status.get("port_open", False),
                    "runtime_ok": status.get("runtime_ok", False),
                    "runtime_message": status.get("runtime_message", ""),
                    "model_ok": status.get("model_ok", False),
                    "model_message": status.get("model_message", ""),
                    "server_path": status["server_path"],
                    "model_path": status["model_path"],
                    "endpoint": status["endpoint"],
                    "provider": "local_jina",
                    "real_request": True,
                }
            if action == "create_jina_offline_package":
                result = create_jina_offline_package(settings, package_path)
                return {
                    "action": action,
                    "ok": True,
                    "message": result["message"],
                    "deployed": result["deployed"],
                    "package_path": result["package_path"],
                    "server_path": result["server_path"],
                    "model_path": result["model_path"],
                    "provider": "local_jina",
                    "real_request": False,
                }
            if action == "import_jina_model":
                result = import_jina_model(settings, package_path, overwrite=True)
                return {
                    "action": action,
                    "ok": True,
                    "message": result["message"],
                    "deployed": result["deployed"],
                    "port_open": result.get("port_open", False),
                    "runtime_ok": result.get("runtime_ok", False),
                    "model_ok": result.get("model_ok", False),
                    "server_path": result["server_path"],
                    "model_path": result["model_path"],
                    "endpoint": result["endpoint"],
                    "provider": "local_jina",
                    "real_request": False,
                }
            if action == "start_jina":
                start_message = self.ai_runtime.start(settings)
                ready = wait_for_embedding_ready(settings)
                endpoint = f"{settings.jina_base_url.rstrip('/')}/v1/embeddings"
                return {
                    "action": action,
                    "ok": True,
                    "message": f"{start_message}；真实请求成功：POST {endpoint} · {ready['dimension']} 维",
                    "dimension": ready["dimension"],
                    "endpoint": endpoint,
                    "provider": "local_jina",
                    "real_request": True,
                }
            if action == "start_and_test_jina":
                self.ai_runtime.start(settings)
                ready = wait_for_embedding_ready(settings)
                endpoint = f"{settings.jina_base_url.rstrip('/')}/v1/embeddings"
                return {
                    "action": action,
                    "ok": True,
                    "message": f"本地 Jina 可用：POST {endpoint} · {ready['dimension']} 维",
                    "deployed": True,
                    "dimension": ready["dimension"],
                    "endpoint": endpoint,
                    "server_path": settings.llama_server_path,
                    "model_path": settings.jina_model_path,
                    "provider": "local_jina",
                    "real_request": True,
                }
            if action == "test_embedding":
                result = test_embedding(settings)
                endpoint = f"{settings.jina_base_url.rstrip('/')}/v1/embeddings"
                return {
                    "action": action,
                    "ok": True,
                    "message": f"真实请求成功：POST {endpoint} · {result['dimension']} 维",
                    "dimension": result["dimension"],
                    "endpoint": endpoint,
                    "provider": "local_jina",
                    "real_request": True,
                }
            if action == "fetch_models":
                model_result = fetch_llm_models_result(settings)
                models = list(model_result.get("models") or [])
                model_source = str(model_result.get("model_source") or "api")
                if model_source == "preset":
                    message = str(model_result.get("message") or "已加载供应商预设模型 ID")
                else:
                    message = f"真实请求成功：GET {model_result.get('endpoint')} · 已获取 {len(models)} 个模型"
                return {
                    "action": action,
                    "ok": True,
                    "message": message,
                    "models": models,
                    "endpoint": model_result.get("endpoint"),
                    "http_status": model_result.get("http_status"),
                    "provider": model_result.get("provider"),
                    "real_request": model_result.get("real_request"),
                    "model_source": model_source,
                }
            if action == "test_llm":
                llm_result = test_llm_result(settings)
                return {
                    "action": action,
                    "ok": True,
                    "message": (
                        f"真实请求成功：POST {llm_result.get('endpoint')} · "
                        f"{llm_result.get('model')} · {llm_result.get('content')}"
                    ),
                    "endpoint": llm_result.get("endpoint"),
                    "http_status": llm_result.get("http_status"),
                    "provider": llm_result.get("provider"),
                    "real_request": True,
                }
            raise RuntimeError("未知 AI 操作")

        self._start_structured_ai_operation(action, worker)

    @pyqtSlot()
    def start_local_jina(self) -> None:
        """按当前配置启动本地 llama-server embedding 服务。"""

        try:
            message = self.ai_runtime.start(self.ai_settings)
        except Exception as exc:  # noqa: BLE001
            self.ai_state["error"] = str(exc)
            self.ai_operation_message_changed.emit(f"本地 Jina 启动失败：{exc}", False)
            self._dirty = True
            return
        self.ai_state["status"] = message
        self.ai_state["running"] = self.ai_runtime.is_running()
        self.ai_operation_message_changed.emit(message, True)
        self._append_event("JINA SERVICE START", message, level="OK", kind="ai")
        self._dirty = True

    @pyqtSlot()
    def stop_local_jina(self) -> None:
        """停止由上位机启动的本地 llama-server。"""

        try:
            message = self.ai_runtime.stop()
        except Exception as exc:  # noqa: BLE001
            self.ai_operation_message_changed.emit(f"本地 Jina 停止失败：{exc}", False)
            return
        self.ai_state["status"] = message
        self.ai_state["running"] = False
        self.ai_operation_message_changed.emit(message, True)
        self._append_event("JINA SERVICE STOP", message, level="WARN", kind="ai")
        self._dirty = True

    @pyqtSlot()
    def test_local_embedding(self) -> None:
        """异步测试本地 Jina embedding 接口。"""

        settings = self.ai_settings.copy()

        def worker() -> tuple[str, bool]:
            result = test_embedding(settings)
            return f"Embedding 测试通过：{result['dimension']} 维", True

        self._start_ai_operation(worker)

    @pyqtSlot()
    def fetch_ai_models(self) -> None:
        """异步获取 OpenAI 兼容大模型列表。"""

        settings = self.ai_settings.copy()

        def worker() -> list[str]:
            return fetch_llm_models(settings)

        def run() -> None:
            try:
                models = worker()
            except Exception as exc:  # noqa: BLE001
                self._ai_models_ready.emit([], False, str(exc))
                return
            self._ai_models_ready.emit(models, True, f"已获取 {len(models)} 个模型")

        threading.Thread(target=run, daemon=True).start()

    @pyqtSlot()
    def test_llm_api(self) -> None:
        """异步测试 OpenAI 兼容大模型接口。"""

        settings = self.ai_settings.copy()

        def worker() -> tuple[str, bool]:
            message = test_llm(settings)
            return f"大模型测试通过：{message}", True

        self._start_ai_operation(worker)

    @pyqtSlot(str)
    def set_matrix_filter(self, value: str) -> None:
        self._matrix_filter = str(value or "ALL")
        self._dirty = True

    @pyqtSlot(int)
    def toggle_matrix_maintenance(self, matrix_id: int) -> None:
        """切换节点维护标记。"""

        state = self.matrix.get(int(matrix_id))
        if state is None:
            return
        state.maintenance = not state.maintenance
        state.health = self._derive_health(state)
        self._append_event(
            "MAINTENANCE MARKED" if state.maintenance else "MAINTENANCE CLEARED",
            f"{state.code} {'已标记维护' if state.maintenance else '已取消维护标记'}",
            level="WARN" if state.maintenance else "OK",
            kind="matrix",
        )
        self._dirty = True

    @pyqtSlot()
    def generate_diagnostics_report(self) -> None:
        """基于当前快照生成本地诊断摘要，不下发硬件命令。"""

        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        online_nodes = [node for node in self.nodes.values() if node.online]
        offline_nodes = [node for node in self.nodes.values() if not node.online]
        critical_matrix = [state for state in self.matrix.values() if self._derive_health(state) == HEALTH_CRITICAL]
        gateway = self._gateway_status
        report_lines = [
            f"诊断时间：{now}",
            f"串口链路：{'已连接 ' + self.selected_port if self._serial_connected else '未连接'}",
            f"已发现节点：在线 {len(online_nodes)} / {len(self.nodes)}，离线 {len(offline_nodes)}",
            f"LoRa 矩阵：在线 {sum(1 for s in self.matrix.values() if s.online)} / {len(self.matrix)}，严重项 {len(critical_matrix)}",
            f"历史样本：{len(self.history)} 条；事件：{len(self.events)} 条",
            (
                "Gateway："
                f"{gateway.get('gateway_id', '未上报')} {gateway.get('firmware', '')}；"
                f"LoRa 成功 {gateway.get('rx_ok', '-')}；CRC 错误 {gateway.get('crc_errors', '-')}；"
                f"长度异常 {gateway.get('bad_length', '-')}；队列丢包 {gateway.get('queue_drops', '-')}；"
                f"Wi-Fi 节点 {gateway.get('wifi_clients', '-')}"
            ),
            "建议：优先检查离线节点供电、天线连接与 Gateway 串口输出；本报告未向硬件下发任何指令。",
        ]
        self._diagnostics_report = "\n".join(report_lines)
        self._append_event("DIAGNOSTICS READY", "本地链路自检报告已生成", level="OK", kind="diagnostics")
        self._dirty = True

    @pyqtSlot(float)
    def set_presence_threshold(self, value: float) -> None:
        """传感器页存在感应阈值滑条回调。"""

        self.presence_threshold = max(0.0, min(1.0, float(value)))
        self.alarm_engine.update_thresholds(presence_threshold=self.presence_threshold)
        self._dirty = True

    @pyqtSlot(float)
    def set_gas_threshold(self, value: float) -> None:
        """传感器页 CO2 估算 ppm 阈值滑条回调。"""

        self.gas_threshold = max(0.0, float(value))
        self._dirty = True

    @pyqtSlot()
    def calibrate_mq135_clean_air(self) -> None:
        """用当前关注节点最新 MQ-135 原始值按 400 ppm 清洁空气估算该节点 R0。"""

        latest = self._latest_gas_raw_for_calibration()
        if latest is None:
            self.status_changed.emit("MQ-135 当前节点校准失败：暂无当前节点气体原始值", False)
            self._append_event("MQ-135 CALIBRATION FAILED", "暂无当前节点气体原始值，无法校准 R0", level="WARN", kind="config")
            return

        node_id, gas_raw = latest
        self.gas_node_calibration_r0[node_id] = calibrate_r0_from_clean_air_raw(gas_raw, MQ135_CLEAN_AIR_PPM)
        self._save_gas_node_calibration()
        self._recalculate_gas_history()
        message = (
            f"MQ-135 当前节点校准完成：{self._node_label(node_id)} raw={gas_raw:.0f}，"
            f"R0={self.gas_node_calibration_r0[node_id]:.2f} kΩ"
        )
        self.status_changed.emit(message, True)
        self._append_event("MQ-135 CALIBRATED", message, node_id=node_id, level="OK", kind="config")
        self._dirty = True

    @pyqtSlot()
    def calibrate_all_mq135_clean_air(self) -> None:
        """对所有在线且已有原始值的节点分别按 400 ppm 清洁空气估算 R0。"""

        latest_by_node = self._latest_gas_raw_by_node_for_calibration(online_only=True)
        if not latest_by_node:
            self.status_changed.emit("MQ-135 全部校准失败：暂无在线节点气体原始值", False)
            self._append_event("MQ-135 CALIBRATION FAILED", "暂无在线节点气体原始值，无法校准 R0", level="WARN", kind="config")
            return

        for node_id, gas_raw in latest_by_node.items():
            self.gas_node_calibration_r0[node_id] = calibrate_r0_from_clean_air_raw(gas_raw, MQ135_CLEAN_AIR_PPM)
        self._save_gas_node_calibration()
        self._recalculate_gas_history()

        labels = [f"{self._node_label(node_id)} raw={gas_raw:.0f}" for node_id, gas_raw in sorted(latest_by_node.items())]
        message = f"MQ-135 全部在线节点校准完成：{len(latest_by_node)} 个节点（" + "；".join(labels) + "）"
        self.status_changed.emit(message, True)
        self._append_event("MQ-135 CALIBRATED", message, level="OK", kind="config")
        self._dirty = True

    @pyqtSlot(bool)
    def set_afh_enabled(self, enabled: bool) -> None:
        self.afh_enabled = bool(enabled)
        self._append_event(
            "AFH " + ("ENABLED" if enabled else "DISABLED"),
            f"自动频率跳变已{'开启' if enabled else '关闭'}",
            level="INFO",
            kind="config",
        )

    @pyqtSlot(bool)
    def set_mesh_enabled(self, enabled: bool) -> None:
        self.mesh_enabled = bool(enabled)
        self._append_event(
            "MESH " + ("ENABLED" if enabled else "DISABLED"),
            f"多级网格中继已{'开启' if enabled else '关闭'}",
            level="INFO",
            kind="config",
        )

    @pyqtSlot()
    def sync_global_config(self) -> None:
        """传感器页“同步全局配置”按钮。"""

        self.last_sync_at = time.time()
        self.alarm_engine.update_thresholds(
            presence_threshold=self.presence_threshold,
            gas_alarm_ppm=max(self.gas_threshold, self.alarm_engine.gas_alarm_ppm),
        )
        self._append_event(
            "CONFIG SYNCED",
            f"本地配置已同步：存在阈值 {self.presence_threshold * 100:.0f}% · CO2 估算阈值 {self.gas_threshold:.0f} ppm",
            level="OK",
            kind="config",
        )
        self._dirty = True

    # ------------------------------------------------------------------ 串口
    def stop_serial(self) -> None:
        """停止串口 Worker 与线程。"""

        self._serial_connected = False
        self._last_serial_sample_at = 0.0

        worker = self._worker
        thread = self._thread
        self._worker = None
        self._thread = None

        if worker is not None:
            worker.stop_reader()
            worker.deleteLater()

        if thread is not None:
            thread.quit()
            thread.wait(1500)
            thread.deleteLater()

    def _auto_service(self) -> None:
        """自动探测真实 Gateway 串口；未发现时保持等待，不注入任何模拟数据。"""

        if self._serial_connected:
            # Gateway 只有在收到节点 LoRa 包后才输出 JSON。短时间无节点帧并不代表
            # 串口失效，保持连接可避免 CH340 被反复打开以及 ESP32-S3 被重复复位。
            return

        self.refresh_ports()
        port = self._probe_gateway_port()
        if not port:
            self.status_changed.emit("串口状态：未发现有效 Gateway，等待真实串口接入", False)
            return

        self._connect_serial(port, auto=True)

    def _connect_serial(self, port: str, auto: bool) -> None:
        """创建 Qt 串口线程。"""

        self.stop_serial()

        self.selected_port = port
        self._serial_auto_connected = auto
        self._serial_started_at = time.time()
        self.status_changed.emit(f"串口状态：正在连接 {port} @ {BAUDRATE}", True)

        self._thread = QThread(self)
        self._worker = SerialWorker(port, BAUDRATE)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start_reader)
        self._worker.opened.connect(self._handle_serial_opened)
        self._worker.frames_received.connect(self._handle_frame_batch)
        self._worker.error.connect(self._handle_serial_error)
        self._thread.start()

    def _probe_gateway_port(self) -> str | None:
        """快速探测会输出 Gateway JSON Lines 的串口。"""

        if not self.available_ports:
            return None

        preferred = [port for port in self._preferred_gateway_ports if port in self.available_ports]
        if len(preferred) == 1:
            # GW-02 的 CH340 可通过 USB VID/PID 唯一识别。直接交给持久读取器只打开
            # 一次，避免“探测打开 -> 关闭 -> 正式打开”导致自研板发生二次复位。
            return preferred[0]

        probe_ports = preferred + [port for port in self.available_ports if port not in preferred]
        for port in probe_ports:
            try:
                with open_serial_port(port=port, baudrate=BAUDRATE, timeout=0.12) as probe:
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        raw = probe.readline()
                        if not raw:
                            continue
                        line = raw.decode("utf-8", errors="replace").strip()
                        if parse_gateway_frame(line).get("valid"):
                            return port
            except Exception:
                continue

        return None

    @pyqtSlot(str)
    def _handle_serial_opened(self, port: str) -> None:
        self._serial_connected = True
        self.selected_port = port
        self.ports_changed.emit(self.available_ports, port)
        self.status_changed.emit(f"串口状态：已连接 {port} @ {BAUDRATE}，等待 Gateway 节点帧", True)
        self._append_event(
            "WIFI SYNC ESTABLISHED",
            f"{GATEWAY_ID} 串口链路已建立：{port}",
            level="OK",
            kind="serial",
        )

    @pyqtSlot(str)
    def _handle_serial_error(self, message: str) -> None:
        self.status_changed.emit(f"串口状态：连接异常，等待重新探测 - {message}", False)
        self._append_event("SERIAL LINK LOST", message, level="ALARM", kind="serial")
        self.stop_serial()

    @pyqtSlot(str)
    def _handle_raw_line(self, raw_line: str) -> None:
        """兼容离线测试/旧调用入口；真实串口路径走批量帧处理。"""

        parsed = parse_gateway_frame(raw_line)
        if not parsed.get("valid"):
            self._handle_frame_batch(
                [],
                {"total": 1, "valid": 0, "invalid": 1, "dropped": 0, "pending": 0, "last_raw": raw_line},
            )
            return

        now = time.time()
        parsed["timestamp"] = now
        parsed["source"] = "serial_real"
        self._handle_frame_batch(
            [parsed],
            {"total": 1, "valid": 1, "invalid": 0, "dropped": 0, "pending": 0, "last_raw": raw_line},
        )

    @pyqtSlot(object, object)
    def _handle_frame_batch(self, frames: object, stats: object) -> None:
        """主线程低频消费后台解析好的串口帧批次。"""

        batch_stats = stats if isinstance(stats, dict) else {}
        raw_lines = batch_stats.get("raw_lines")
        if self._recorder.active and isinstance(raw_lines, list):
            try:
                self._recorder.append_many((str(line) for line in raw_lines), recorded_at=time.time())
            except Exception as exc:  # noqa: BLE001
                self._recorder.stop()
                self.recording_state_changed.emit(False, f"录制写入失败：{exc}")
        all_valid_frames = [frame for frame in frames if isinstance(frame, dict)] if isinstance(frames, list) else []
        status_frames = [frame for frame in all_valid_frames if frame.get("frame_type") == "gateway_status"]
        valid_frames = [frame for frame in all_valid_frames if frame.get("frame_type") != "gateway_status"]
        for frame in status_frames:
            payload = frame.get("gateway_status")
            if isinstance(payload, dict):
                self._gateway_status = dict(payload)
                self._gateway_status["received_at"] = time.time()
                self._dirty = True
        total = int(batch_stats.get("total") or len(all_valid_frames))
        invalid = int(batch_stats.get("invalid") or 0)
        dropped = int(batch_stats.get("dropped") or 0)
        pending = int(batch_stats.get("pending") or 0)
        self._serial_stats.update(
            {
                "last_batch_total": total,
                "last_batch_valid": len(valid_frames),
                "last_batch_invalid": invalid,
                "last_batch_dropped": dropped,
                "last_batch_pending": pending,
                "total_valid": self._serial_stats.get("total_valid", 0) + len(all_valid_frames),
                "total_invalid": self._serial_stats.get("total_invalid", 0) + invalid,
                "total_dropped": self._serial_stats.get("total_dropped", 0) + dropped,
                "total_gateway_status": self._serial_stats.get("total_gateway_status", 0) + len(status_frames),
            }
        )

        now = time.time()
        if not valid_frames:
            if status_frames:
                gateway_id = str(self._gateway_status.get("gateway_id") or "Gateway")
                firmware = str(self._gateway_status.get("firmware") or "")
                self.status_changed.emit(f"串口状态：已连接 {gateway_id} {firmware}，等待节点数据", True)
            if invalid and now - self._last_invalid_frame_ui_at >= 1.0:
                self._last_invalid_frame_ui_at = now
                self._emit_latest_frame(f"忽略非 JSON 数据 {invalid} 行", now=now, force=True)
            self._maybe_report_serial_overload(pending, dropped, now)
            return

        first_valid_batch = self._last_serial_sample_at <= 0
        self._last_serial_sample_at = max(_float(frame.get("timestamp"), now) for frame in valid_frames)
        if first_valid_batch:
            self.status_changed.emit(
                f"串口状态：已连接 {self.selected_port} @ {BAUDRATE}，节点数据正常",
                True,
            )
        self._emit_latest_frame(
            self._batch_frame_summary(valid_frames[-1], total, len(valid_frames), invalid, dropped, pending),
            now=now,
        )
        self._maybe_report_serial_overload(pending, dropped, now)
        if self._paused:
            return

        latest_by_node: dict[int, dict[str, Any]] = {}
        applied = False
        for frame in valid_frames:
            node_id = int(frame.get("node_id") or frame.get("id") or 0)
            if self._is_controlled_node(node_id):
                continue
            enriched = self._apply_sample(frame, run_rules=False, mark_dirty=False)
            if not enriched:
                continue
            applied = True
            latest_by_node[int(enriched.get("node_id") or 0)] = enriched

        for node_id, enriched in latest_by_node.items():
            node = self.nodes.get(node_id)
            if node is not None:
                self._handle_life_events(node)
            event_time = _float(enriched.get("timestamp"), now)
            for alarm in self.alarm_engine.evaluate(enriched, None, event_time):
                alarm_node = int(alarm.get("node_id") or node_id)
                self._append_event(
                    str(alarm.get("title", "SYSTEM ALARM")),
                    f"{self._node_label(alarm_node)} {alarm.get('message', '规则报警')}",
                    node_id=alarm_node,
                    level=str(alarm.get("level", "ALARM")),
                    kind=str(alarm.get("kind", "alarm")),
                    event_time=float(alarm.get("time", event_time)),
                )

        if applied:
            self._dirty = True

    def _emit_latest_frame(self, text: str, now: float | None = None, force: bool = False) -> None:
        """低频更新左侧最新帧，避免 Gateway 高频输出拖慢 QLabel 重排。"""

        current = now if now is not None else time.time()
        if not force and current - self._last_frame_ui_at < 0.3:
            return
        self._last_frame_ui_at = current
        compact = " ".join(str(text).split())
        if len(compact) > 180:
            compact = compact[:177] + "..."
        self.latest_frame_changed.emit(f"最新帧：{compact}")

    def _batch_frame_summary(
        self,
        frame: dict[str, Any],
        total: int,
        valid: int,
        invalid: int,
        dropped: int,
        pending: int,
    ) -> str:
        node_id = int(frame.get("node_id") or 0)
        label = str(frame.get("node_label") or "").strip() or NODE_LABELS.get(node_id, f"node{node_id}")
        seq = _optional_int(frame.get("seq"))
        parts = [label if seq is None else f"{label} seq={seq}", f"本批 {total} 行 / 有效 {valid} 帧"]
        if invalid:
            parts.append(f"忽略 {invalid} 行")
        if pending:
            parts.append(f"待处理 {pending} 行")
        if dropped:
            parts.append(f"丢弃 {dropped} 行")
        return " · ".join(parts)

    def _maybe_report_serial_overload(self, pending: int, dropped: int, now: float) -> None:
        if not dropped and pending < 1500:
            return
        if now - self._last_serial_overload_notice_at < 3.0:
            return
        self._last_serial_overload_notice_at = now
        self.status_changed.emit(
            f"串口状态：输入过快，已合并刷新（待处理 {pending} 行，丢弃 {dropped} 行）",
            True,
        )

    # ------------------------------------------------------------------ LoRa 矩阵
    def _ensure_node_state(self, node_id: int) -> NodeState | None:
        """确保真实上报的节点 ID 可以进入上位机状态表。"""

        if node_id <= 0:
            return None
        if node_id not in self.nodes:
            self.nodes[node_id] = NodeState(
                node_id=node_id,
                label=NODE_LABELS.get(node_id, f"node{node_id}"),
                battery=None,
            )
            self._presence_flags[node_id] = False
            self._breath_lock_flags[node_id] = False
            self._offline_flags[node_id] = False
            self._gas_alert_flags[node_id] = False
        return self.nodes[node_id]

    def _ensure_matrix_node(self, node_id: int) -> MatrixNodeState | None:
        """按真实 Gateway 帧自动创建节点管理表行。"""

        if node_id <= 0:
            return None
        state = self.matrix.get(node_id)
        if state is None:
            state = MatrixNodeState(
                matrix_id=node_id,
                code=NODE_LABELS.get(node_id, f"node{node_id}"),
                mode="NORMAL",
                bound_node=node_id,
                battery=None,
            )
            self.matrix[node_id] = state
            self._append_event("NODE DISCOVERED", f"已发现节点 {state.code}", node_id=node_id, level="OK", kind="matrix")
        return state

    def _refresh_matrix_node(self, state: MatrixNodeState, now: float) -> None:
        """根据绑定的真实节点刷新一行矩阵状态。"""

        if state.bound_node is not None and state.bound_node in self.nodes:
            src = self.nodes[state.bound_node]
            state.online = src.online and state.mode != "SLEEP"
            state.rssi = src.rssi
            state.last_received = src.last_received
        else:
            state.online = False
        state.health = self._derive_health(state)

    def _derive_health(self, state: MatrixNodeState) -> str:
        """从运行模式 / RSSI 推导运行健康度；当前固件未上报电池。"""

        if state.maintenance:
            return HEALTH_CRITICAL
        if state.mode == "SLEEP" or not state.online:
            return HEALTH_INACTIVE
        if state.rssi < -118.0:
            return HEALTH_CRITICAL
        if state.rssi < -100.0:
            return HEALTH_GOOD
        return HEALTH_EXCELLENT

    # ------------------------------------------------------------------ 控制版样本注入
    def _is_controlled_node(self, node_id: int) -> bool:
        state = self._control_nodes.get(int(node_id))
        return bool(state and state.enabled)

    def _sync_control_timer(self) -> None:
        has_enabled = any(state.enabled for state in self._control_nodes.values())
        if has_enabled and not self._control_timer.isActive():
            self._control_timer.start()
        elif not has_enabled and self._control_timer.isActive():
            self._control_timer.stop()

    def _inject_control_samples(self) -> None:
        enabled = [state for state in self._control_nodes.values() if state.enabled]
        if not enabled or self._paused:
            return

        for state in enabled:
            state.seq += 1
            sample = self._build_control_sample(state)
            enriched = self._apply_sample(sample, run_rules=True, mark_dirty=False)
            if enriched:
                self._emit_latest_frame(
                    f"{NODE_LABELS.get(state.node_id, f'node{state.node_id}')} seq={state.seq} · 本批 1 行 / 有效 1 帧"
                )
        self._dirty = True

    def _build_control_sample(self, state: ControlNodeState) -> dict[str, Any]:
        now = time.time()
        phase = int((now - self._control_started_at) * 10) + state.node_id * 7 + state.seq
        drift = ((phase % 7) - 3) * 0.01
        csi_on = bool(state.csi_value)
        if csi_on:
            presence = max(0.0, min(1.0, 0.85 + drift))
            motion = max(0.0, min(1.0, 0.30 + drift * 0.8))
            confidence = max(0.0, min(1.0, 0.92 + drift * 0.6))
        else:
            presence = max(0.0, min(1.0, 0.01 + max(drift, 0.0) * 0.4))
            motion = max(0.0, min(1.0, 0.01 + max(-drift, 0.0) * 0.4))
            confidence = max(0.0, min(1.0, 0.85 + drift * 0.4))

        gas_raw = 520.0 + ((phase % 11) - 5) * 1.5
        temperature = 27.0 + ((phase % 5) - 2) * 0.1
        humidity = 45.0 + ((phase % 9) - 4) * 0.2
        rssi = -58.0 + ((phase % 5) - 2)
        return {
            "timestamp": now,
            "node_id": state.node_id,
            "node_label": NODE_LABELS.get(state.node_id, f"node{state.node_id}"),
            "seq": state.seq,
            "presence_score": presence,
            "motion_score": motion,
            "confidence": confidence,
            "breath_bpm": 18 if csi_on else 0,
            "gas_raw": gas_raw,
            "temperature": temperature,
            "humidity": humidity,
            "rssi": rssi,
            "wifi_rssi": rssi + 3.0,
            "csi_quality": 0.90 if csi_on else 0.82,
            "csi_sample_count": 64,
            "breath_lock": csi_on,
            "noise_floor": 5.0 + abs(drift) * 10.0,
            "source": "demo_mode",
        }

    def _build_mobile_presence_sample(self, node_id: int, value: float) -> dict[str, Any]:
        """构造手机命令的即时样本，并沿用节点最近一次其他传感器字段。"""

        node = self.nodes.get(node_id)
        return {
            "timestamp": time.time(),
            "node_id": node_id,
            "node_label": self._node_label(node_id),
            "seq": (node.seq + 1) if node is not None and node.seq is not None else 1,
            "presence_score": value,
            "motion_score": node.motion_score if node is not None else 0.0,
            "confidence": node.confidence if node is not None else 0.0,
            "breath_bpm": node.breath_bpm if node is not None else 0.0,
            "gas_raw": node.gas_raw if node is not None else 0.0,
            "gas_ppm": node.gas_ppm if node is not None else 0.0,
            "temperature": node.temperature if node is not None else 0.0,
            "humidity": node.humidity if node is not None else 0.0,
            "rssi": node.rssi if node is not None else 0.0,
            "wifi_rssi": node.wifi_rssi if node is not None else 0.0,
            "csi_quality": node.csi_quality if node is not None else None,
            "csi_sample_count": node.csi_sample_count if node is not None else None,
            "breath_lock": node.breath_lock if node is not None else None,
            "noise_floor": node.noise_floor if node is not None else None,
            "source": "mobile_override",
        }

    def _load_gas_calibration_r0(self) -> float:
        settings = load_ui_settings()
        value = _float(settings.get("mq135_r0_kohm"), DEFAULT_MQ135_R0_KOHM)
        return value if value > 0.0 else DEFAULT_MQ135_R0_KOHM

    def _load_gas_node_calibration_r0(self) -> dict[int, float]:
        settings = load_ui_settings()
        payload = settings.get("mq135_node_r0_kohm")
        if not isinstance(payload, dict):
            return {}
        result: dict[int, float] = {}
        for key, value in payload.items():
            try:
                node_id = int(key)
            except (TypeError, ValueError):
                continue
            r0 = _float(value)
            if node_id > 0 and r0 > 0.0:
                result[node_id] = r0
        return result

    def _save_gas_node_calibration(self) -> None:
        save_ui_settings(
            {
                "mq135_node_r0_kohm": {
                    str(node_id): r0
                    for node_id, r0 in sorted(self.gas_node_calibration_r0.items())
                    if node_id > 0 and r0 > 0.0
                }
            }
        )

    def _gas_r0_for_node(self, node_id: int) -> float:
        node_r0 = self.gas_node_calibration_r0.get(int(node_id))
        if node_r0 is not None and node_r0 > 0.0:
            return node_r0
        return self.gas_calibration_r0 if self.gas_calibration_r0 > 0.0 else DEFAULT_MQ135_R0_KOHM

    def _apply_gas_calibration(self, sample: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(sample)
        node_id = int(enriched.get("node_id") or enriched.get("id") or 0)
        gas_raw = _float(enriched.get("gas_raw", enriched.get("gas")))
        gas_ppm = calculate_gas_ppm(gas_raw, self._gas_r0_for_node(node_id))
        enriched["gas_raw"] = gas_raw
        enriched["gas_ppm"] = gas_ppm
        enriched["gas"] = gas_ppm
        return enriched

    def _latest_gas_raw_for_calibration(self) -> tuple[int, float] | None:
        preferred_node = int(self._active_node or 0)
        if preferred_node <= 0:
            return None
        for sample in reversed(self.history):
            node_id = int(sample.get("node_id") or 0)
            if node_id != preferred_node:
                continue
            gas_raw = _float(sample.get("gas_raw"))
            if gas_raw > 0.0:
                return node_id, gas_raw
        return None

    def _latest_gas_raw_by_node_for_calibration(self, online_only: bool = False) -> dict[int, float]:
        target_ids = {
            node_id
            for node_id, node in self.nodes.items()
            if node_id > 0 and (not online_only or node.online)
        }
        if online_only and not target_ids:
            return {}
        result: dict[int, float] = {}
        for sample in reversed(self.history):
            node_id = int(sample.get("node_id") or 0)
            if node_id <= 0 or node_id in result:
                continue
            if target_ids and node_id not in target_ids:
                continue
            gas_raw = _float(sample.get("gas_raw"))
            if gas_raw > 0.0:
                result[node_id] = gas_raw
            if target_ids and len(result) >= len(target_ids):
                break
        return result

    def _recalculate_gas_history(self) -> None:
        for sample in self.history:
            node_id = int(sample.get("node_id") or 0)
            gas_raw = _float(sample.get("gas_raw", sample.get("gas")))
            gas_ppm = calculate_gas_ppm(gas_raw, self._gas_r0_for_node(node_id))
            sample["gas_raw"] = gas_raw
            sample["gas_ppm"] = gas_ppm
            sample["gas"] = gas_ppm
        for node_id, node in self.nodes.items():
            node.gas_ppm = calculate_gas_ppm(node.gas_raw, self._gas_r0_for_node(node_id))
            node.gas = node.gas_ppm

    # ------------------------------------------------------------------ 样本应用
    def _apply_sample(
        self,
        sample: dict[str, Any],
        run_rules: bool = True,
        mark_dirty: bool = True,
    ) -> dict[str, Any] | None:
        sample = self._apply_gas_calibration(sample)
        node_id = int(sample.get("node_id") or 0)
        incoming_source = _canonical_sample_source(sample.get("source"))
        if node_id > 0 and incoming_source == "serial_real":
            self._last_real_samples[node_id] = dict(sample)
        node = self._ensure_node_state(node_id)
        matrix_state = self._ensure_matrix_node(node_id)
        if node is None:
            return None

        now = float(sample.get("timestamp") or time.time())
        was_online = node.online
        raw_presence_score = _score(sample.get("presence_score", sample.get("presence")))
        mobile_presence_score = self._mobile_presence_overrides.get(node_id)
        presence_score = mobile_presence_score if mobile_presence_score is not None else raw_presence_score
        label = str(sample.get("node_label") or "").strip()
        if label:
            node.label = label
            if matrix_state is not None:
                matrix_state.code = label

        node.online = True
        node.rssi = _float(sample.get("rssi"))
        node.wifi_rssi = _float(sample.get("wifi_rssi"), node.rssi)
        node.snr = _float(sample.get("snr"), _snr_from_rssi(node.rssi))
        node.packet_loss = _float(sample.get("packet_loss"), _packet_loss_from_rssi(node.rssi))
        node.last_received = now
        node.seq = _optional_int(sample.get("seq"))
        node.presence_score = presence_score
        node.motion_score = _score(sample.get("motion_score", sample.get("motion")))
        node.breath_bpm = _float(sample.get("breath_bpm", sample.get("bpm")))
        node.confidence = _score(sample.get("confidence", sample.get("conf")))
        node.csi_quality = _optional_score(sample.get("csi_quality"))
        node.csi_sample_count = _optional_int(sample.get("csi_sample_count"))
        node.breath_lock = _optional_bool(sample.get("breath_lock"))
        node.noise_floor = _optional_float(sample.get("noise_floor"))
        node.gas_raw = _float(sample.get("gas_raw", sample.get("gas")))
        node.gas_ppm = _float(sample.get("gas_ppm", sample.get("gas")))
        node.gas = node.gas_ppm
        node.temperature = _float(sample.get("temperature", sample.get("temp")))
        node.humidity = _float(sample.get("humidity", sample.get("hum")))
        source = incoming_source
        if mobile_presence_score is not None and source in {"serial_real", "replay"}:
            source = "mobile_override"
        node.source = source
        if matrix_state is not None:
            self._refresh_matrix_node(matrix_state, now)

        enriched = dict(sample)
        enriched.update(
            {
                "timestamp": now,
                "node_id": node_id,
                "node_code": node.label,
                "raw_presence_score": raw_presence_score,
                "presence_score": node.presence_score,
                "presence": node.presence_score,
                "source": source,
                "wifi_rssi": node.wifi_rssi,
                "snr": node.snr,
                "packet_loss": node.packet_loss,
                "battery": node.battery,
                "csi_quality": node.csi_quality,
                "csi_sample_count": node.csi_sample_count,
                "breath_lock": node.breath_lock,
                "noise_floor": node.noise_floor,
                "gas": node.gas_ppm,
                "gas_raw": node.gas_raw,
                "gas_ppm": node.gas_ppm,
                "mode": "NORMAL",
            }
        )
        self.history.append(enriched)
        if len(self.history) > MAX_HISTORY_ROWS:
            del self.history[: len(self.history) - MAX_HISTORY_ROWS]

        if self._active_node <= 0 or self._active_node not in self.nodes:
            self._active_node = node_id
        self._offline_flags[node_id] = False

        if not was_online:
            self._append_event(
                "WIFI SYNC ESTABLISHED",
                f"CSI 子载波同步完成 ({node.label})",
                node_id=node_id,
                level="OK",
                kind="node",
            )

        if run_rules:
            self._handle_life_events(node)
            for alarm in self.alarm_engine.evaluate(enriched, None, now):
                alarm_node = int(alarm.get("node_id") or node_id)
                self._append_event(
                    str(alarm.get("title", "SYSTEM ALARM")),
                    f"{self._node_label(alarm_node)} {alarm.get('message', '规则报警')}",
                    node_id=alarm_node,
                    level=str(alarm.get("level", "ALARM")),
                    kind=str(alarm.get("kind", "alarm")),
                    event_time=float(alarm.get("time", now)),
                )

        if mark_dirty:
            self._dirty = True
        return enriched

    def _handle_life_events(self, node: NodeState) -> None:
        """根据状态变化生成右侧实时事件流。"""

        node_id = node.node_id
        has_presence = life_motion_triggered(
            node.to_dict(),
            presence_threshold=self.presence_threshold,
        )

        if has_presence and not self._presence_flags[node_id]:
            self._append_event(
                "疑似生命微动",
                f"检测到疑似生命微动信号 ({node.label})",
                node_id=node_id,
                level="ALARM",
                kind="presence",
            )
        elif not has_presence and self._presence_flags[node_id]:
            self._append_event(
                "未检测到稳定微动",
                f"微动信号低于阈值或需要继续观察 ({node.label})",
                node_id=node_id,
                level="WARN",
                kind="presence",
            )

        self._presence_flags[node_id] = has_presence

    def _check_offline_nodes(self) -> None:
        now = time.time()

        if self._serial_connected:
            for node in self.nodes.values():
                last = node.last_received
                if last is None:
                    continue
                is_online = bool(last is not None and now - float(last) <= OFFLINE_SECONDS)
                if node.online and not is_online:
                    node.online = False
                    self._dirty = True
                if not is_online and not self._offline_flags[node.node_id]:
                    self._offline_flags[node.node_id] = True
                    self._append_event(
                        "节点离线",
                        f"{node.label} 超过 {OFFLINE_SECONDS:.0f}s 未收到数据",
                        node_id=node.node_id,
                        level="ALARM",
                        kind="offline",
                    )

            discovered_nodes = {
                node_id: state
                for node_id, state in self._node_dicts().items()
                if state.get("last_received") is not None
            }
            for alarm in self.alarm_engine.evaluate(None, discovered_nodes, now):
                alarm_node = int(alarm.get("node_id") or 0)
                self._append_event(
                    str(alarm.get("title", "节点离线")),
                    f"{self._node_label(alarm_node)} {alarm.get('message', '节点离线')}",
                    node_id=alarm_node,
                    level=str(alarm.get("level", "ALARM")),
                    kind=str(alarm.get("kind", "offline")),
                    event_time=float(alarm.get("time", now)),
                )

        # 中文注释：矩阵行根据绑定的真实节点刷新在线状态与健康度；未绑定的行保持离线。
        for state in self.matrix.values():
            self._refresh_matrix_node(state, now)

        self._auto_service()

    # ------------------------------------------------------------------ AI 辅助研判
    def _public_ai_config(self, settings: AISettings | None = None) -> dict[str, Any]:
        """返回可发给 UI 的 AI 配置，避免 API Key 进入快照或弹窗预填。"""

        payload = asdict(settings or self.ai_settings)
        payload["llm_api_key"] = ""
        return payload

    def _apply_ai_settings(self, payload: dict[str, Any]) -> object:
        self.ai_settings = settings_from_dict(payload)
        path = save_ai_settings(self.ai_settings)
        self.ai_state["enabled"] = self.ai_settings.enabled
        self.ai_state["config"] = self._public_ai_config()
        self._ai_last_state_key = ""
        self._dirty = True
        return path

    def _start_structured_ai_operation(self, action: str, worker: object) -> None:
        self.ai_operation_message_changed.emit("AI 操作执行中...", True)
        self._ai_operation_result_ready.emit(
            {"action": action, "ok": True, "running": True, "message": "AI 操作执行中..."}
        )

        def run() -> None:
            try:
                result = worker()  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                self._ai_operation_result_ready.emit(
                    {"action": action, "ok": False, "running": False, "message": str(exc)}
                )
                return
            if isinstance(result, dict):
                result.setdefault("action", action)
                result.setdefault("running", False)
                self._ai_operation_result_ready.emit(result)
            else:
                self._ai_operation_result_ready.emit(
                    {"action": action, "ok": True, "running": False, "message": str(result)}
                )

        threading.Thread(target=run, daemon=True).start()

    def _start_ai_operation(self, worker: object) -> None:
        """在后台线程执行 AI 测试 / 获取模型等短任务。"""

        self.ai_operation_message_changed.emit("AI 操作执行中...", True)

        def run() -> None:
            try:
                message, ok = worker()  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                self._ai_operation_ready.emit(str(exc), False)
                return
            self._ai_operation_ready.emit(str(message), bool(ok))

        threading.Thread(target=run, daemon=True).start()

    def _maybe_schedule_ai_analysis(self) -> None:
        """低频异步生成 AI 辅助解释；实时主判断仍由规则融合负责。"""

        self._schedule_ai_analysis(force=False, manual=False)

    def _schedule_ai_chat(self, question: str) -> None:
        question = " ".join(str(question or "").split())
        if not question:
            self.ai_state["chat_error"] = "请输入要询问的问题"
            self._dirty = True
            return
        if self._ai_chat_busy:
            self.ai_state["chat_error"] = "AI 对话正在生成，请稍后再问"
            self._dirty = True
            return

        now = time.time()
        history = list(self.ai_state.get("chat_history") or [])
        history.append({"role": "user", "content": question, "time": now})
        history = history[-20:]
        context = self._build_ai_chat_context()

        self._ai_chat_busy = True
        self._ai_chat_request_id += 1
        request_id = self._ai_chat_request_id
        self.ai_state.update(
            {
                "chat_history": history,
                "chat_running": True,
                "chat_error": "",
                "chat_context_label": str(context.get("context_label") or "当前快照 + 最近5分钟"),
                "config": self._public_ai_config(),
            }
        )
        self._dirty = True
        settings = self.ai_settings.copy()
        chat_history = [
            {"role": str(item.get("role") or ""), "content": str(item.get("content") or "")}
            for item in history[-8:]
        ]

        def run() -> None:
            try:
                result = run_ai_chat(settings, question, context, chat_history)
            except Exception as exc:  # noqa: BLE001
                result = {
                    "answer": "AI 对话暂不可用，建议先依据右侧规则融合和节点贡献继续复核。",
                    "source": "rule_fallback",
                    "status": "AI 对话失败",
                    "error": str(exc),
                    "context_label": str(context.get("context_label") or "当前快照 + 最近5分钟"),
                }
            self._ai_chat_ready.emit(request_id, result)

        threading.Thread(target=run, daemon=True).start()

    @pyqtSlot(int, object)
    def _handle_ai_chat_ready(self, request_id: int, result: object) -> None:
        if request_id != self._ai_chat_request_id:
            return
        self._ai_chat_busy = False
        if not isinstance(result, dict):
            return
        history = list(self.ai_state.get("chat_history") or [])
        answer = str(result.get("answer") or "暂无回答")
        history.append(
            {
                "role": "assistant",
                "content": answer,
                "time": time.time(),
                "source": str(result.get("source") or "rule_fallback"),
            }
        )
        self.ai_state.update(
            {
                "chat_history": history[-20:],
                "chat_running": False,
                "chat_error": str(result.get("error") or ""),
                "chat_source": str(result.get("source") or "rule_fallback"),
                "chat_context_label": str(result.get("context_label") or "当前快照 + 最近5分钟"),
                "status": str(result.get("status") or self.ai_state.get("status") or "AI 对话完成"),
                "config": self._public_ai_config(),
            }
        )
        self._dirty = True

    def _build_ai_chat_context(self) -> dict[str, Any]:
        nodes = self._node_dicts()
        recent_history = self._recent_history(300.0, 1200)
        summary = build_detection_summary(
            nodes,
            self._recent_history(5.0, 1200),
            presence_threshold=self.presence_threshold,
        )
        node_lines: list[str] = []
        for node_id, node in sorted(nodes.items()):
            if node.get("last_received") is None:
                continue
            node_lines.append(
                (
                    f"{node.get('label') or self._node_label(node_id)} "
                    f"online={bool(node.get('online'))} "
                    f"presence={_score(node.get('presence_score')):.2f} "
                    f"motion={_score(node.get('motion_score')):.2f} "
                    f"confidence={_score(node.get('confidence')):.2f} "
                    f"gas_ppm={_float(node.get('gas_ppm')):.0f}"
                )
            )
        event_lines = [
            f"{event.title}: {event.message}"
            for event in self.events[-8:]
            if event.kind in {"ai", "alarm", "serial", "node", "config", "system"}
        ]
        thresholds = (
            f"presence>={self.presence_threshold:.2f}, "
            "confidence=diagnostic-only, csi_quality=diagnostic-only, "
            f"gas>={self.gas_threshold:.0f}ppm"
        )
        detail = self.ai_state.get("detail") if isinstance(self.ai_state.get("detail"), dict) else {}
        return {
            "context_label": "当前快照 + 最近5分钟",
            "status": summary.status,
            "detail": summary.detail,
            "participants": "、".join(summary.participant_labels) or "无",
            "triggered": "、".join(summary.triggered_labels) or "无",
            "summary_text": summary.summary_text,
            "node_lines": node_lines,
            "event_lines": event_lines,
            "history_samples": len(recent_history),
            "thresholds": thresholds,
            "ai_detail": detail,
        }

    def _schedule_ai_analysis(self, force: bool = False, manual: bool = False) -> None:
        summary = build_detection_summary(
            self._node_dicts(),
            self._recent_history(5.0, 1200),
            presence_threshold=self.presence_threshold,
        )
        self._refresh_ai_fallback(summary)
        if not self.ai_settings.enabled or not self.ai_settings.embedding_enabled:
            if manual:
                self.ai_operation_message_changed.emit("AI 详情已使用规则回退刷新", True)
            return
        if not summary.participant_ids:
            if manual:
                self.ai_operation_message_changed.emit("暂无有效节点样本，AI 详情使用规则回退", False)
            return
        if self._ai_busy:
            if manual:
                self.ai_operation_message_changed.emit("AI 正在生成详情，请稍后再刷新", False)
            return

        now = time.time()
        if not force and now - self._ai_last_started_at < 3.0:
            return
        if (
            not force
            and summary.state_key == self._ai_last_state_key
            and now - _float(self.ai_state.get("updated_at")) < 8.0
        ):
            return

        self._ai_busy = True
        self._ai_request_id += 1
        request_id = self._ai_request_id
        self._ai_last_started_at = now
        self._ai_last_state_key = summary.state_key
        self.ai_state.update(
            {
                "running": True,
                "status": "AI 分析中",
                "text": str(self.ai_state.get("text") or ai_fallback_text(summary.status, summary)),
                "detail": self.ai_state.get("detail") or build_ai_detail(summary),
                "state_key": summary.state_key,
                "window_start": summary.window_start,
                "window_end": summary.window_end,
                "config": self._public_ai_config(),
            }
        )
        if manual:
            self.ai_operation_message_changed.emit("AI 详情生成中...", True)
        self._dirty = True
        settings = self.ai_settings.copy()

        def run() -> None:
            result = run_ai_judgement(settings, summary)
            self._ai_analysis_ready.emit(request_id, result)

        threading.Thread(target=run, daemon=True).start()

    @pyqtSlot(int, object)
    def _handle_ai_analysis_ready(self, request_id: int, result: object) -> None:
        self._ai_busy = False
        if request_id != self._ai_request_id or not isinstance(result, dict):
            return

        current_summary = build_detection_summary(
            self._node_dicts(),
            self._recent_history(5.0, 1200),
            presence_threshold=self.presence_threshold,
        )
        if str(result.get("state_key", "")) != current_summary.state_key:
            now = time.time()
            self.ai_state.update(
                {
                    "running": False,
                    "status": "上一轮 AI 结果已过期，等待更新",
                    "text": ai_fallback_text(current_summary.status, current_summary),
                    "source": "rule_fallback",
                    "detail": build_ai_detail(current_summary),
                    "detail_source": "rule_fallback",
                    "detail_updated_at": now,
                    "window_start": current_summary.window_start,
                    "window_end": current_summary.window_end,
                    "updated_at": now,
                    "top_matches": [],
                    "error": "",
                    "state_key": current_summary.state_key,
                    "config": self._public_ai_config(),
                }
            )
        else:
            result["running"] = False
            result["config"] = self._public_ai_config()
            result["jina_running"] = self.ai_runtime.is_running()
            result["detail_updated_at"] = time.time()
            self.ai_state.update(result)

        self._record_ai_detail(current_summary)
        self._dirty = True

    @pyqtSlot(str, bool)
    def _handle_ai_operation_ready(self, message: str, ok: bool) -> None:
        self.ai_operation_message_changed.emit(message, ok)
        self.ai_state["status"] = message
        self.ai_state["error"] = "" if ok else message
        self.ai_state["config"] = self._public_ai_config()
        self._dirty = True

    @pyqtSlot(object)
    def _handle_ai_operation_result_ready(self, result: object) -> None:
        if not isinstance(result, dict):
            return
        ok = bool(result.get("ok", False))
        message = str(result.get("message") or "")
        action = str(result.get("action") or "")
        running = bool(result.get("running", False))
        self.ai_operation_result_changed.emit(result)
        self.ai_operation_message_changed.emit(message, ok)
        if "models" in result:
            self.ai_models_changed.emit(result.get("models") or [])
        self.ai_state["running"] = running
        self.ai_state["status"] = message
        self.ai_state["error"] = "" if ok else message
        self.ai_state["jina_running"] = self.ai_runtime.is_running()
        self.ai_state["config"] = self._public_ai_config()
        if action == "stop_jina":
            self.ai_state["jina_running"] = False
        if ok and action in {"test_embedding", "start_jina", "start_and_test_jina"}:
            self.ai_state["source"] = "local_jina"
        self._dirty = True

    @pyqtSlot(object, bool, str)
    def _handle_ai_models_ready(self, models: object, ok: bool, message: str) -> None:
        self.ai_models_changed.emit(models if ok else [])
        self.ai_operation_message_changed.emit(message if ok else f"获取模型失败：{message}", ok)
        self.ai_state["status"] = message if ok else "获取模型失败"
        self.ai_state["error"] = "" if ok else message
        self._dirty = True

    def _refresh_ai_fallback(self, summary: object) -> None:
        if not hasattr(summary, "state_key"):
            return
        current_key = str(summary.state_key)
        if self._ai_busy and self.ai_state.get("state_key") == current_key:
            return
        if self.ai_state.get("source") != "rule_fallback" and self.ai_state.get("state_key") == current_key:
            return
        if (
            self.ai_state.get("source") == "rule_fallback"
            and self.ai_state.get("state_key") == current_key
            and isinstance(self.ai_state.get("detail"), dict)
        ):
            return
        now = time.time()
        self.ai_state.update(
            {
                "enabled": self.ai_settings.enabled,
                "running": False,
                "status": "规则回退" if self.ai_settings.enabled else "AI 未启用，使用规则回退",
                "text": ai_fallback_text(summary.status, summary),
                "source": "rule_fallback",
                "detail": build_ai_detail(summary),
                "detail_source": "rule_fallback",
                "detail_updated_at": now,
                "window_start": summary.window_start,
                "window_end": summary.window_end,
                "updated_at": _float(self.ai_state.get("updated_at")),
                "top_matches": [],
                "error": "",
                "state_key": current_key,
                "jina_running": self.ai_runtime.is_running(),
                "config": self._public_ai_config(),
            }
        )
        self._record_ai_detail(summary)

    def _record_ai_detail(self, summary: object) -> None:
        if not hasattr(summary, "state_key"):
            return
        detail = self.ai_state.get("detail")
        if not isinstance(detail, dict):
            return
        source = str(self.ai_state.get("detail_source") or self.ai_state.get("source") or "rule_fallback")
        state_key = str(summary.state_key)
        record_key = f"{state_key}|{source}|{detail.get('headline', '')}"
        history = list(self.ai_state.get("detail_history") or [])
        if not history or str(history[-1].get("record_key") or "") != record_key:
            history.append(
                {
                    "record_key": record_key,
                    "time": time.time(),
                    "status": str(getattr(summary, "status", "")),
                    "source": source,
                    "headline": str(detail.get("headline") or self.ai_state.get("text") or ""),
                    "basis": str(detail.get("basis") or ""),
                    "risk": str(detail.get("risk") or ""),
                    "trend": str(detail.get("trend") or ""),
                    "advice": str(detail.get("advice") or ""),
                }
            )
            self.ai_state["detail_history"] = history[-20:]
        if getattr(summary, "participant_ids", None):
            self._append_ai_summary_event(summary, detail, source)

    def _append_ai_summary_event(self, summary: object, detail: dict[str, Any], source: str) -> None:
        state_key = str(getattr(summary, "state_key", ""))
        event_key = f"{state_key}|{source}"
        if not state_key or event_key == self._ai_detail_event_key:
            return
        self._ai_detail_event_key = event_key
        message = str(detail.get("advice") or detail.get("headline") or "AI 已生成辅助研判")
        self._append_event("AI SUMMARY", message, level="INFO", kind="ai")

    # ------------------------------------------------------------------ 快照
    def _publish_snapshot(self) -> None:
        if not self._dirty:
            return

        self._dirty = False
        nodes = self._node_dicts()
        recent_history = self._recent_history(60.0, 1200)
        summary = build_detection_summary(
            nodes,
            recent_history,
            presence_threshold=self.presence_threshold,
        )
        self._refresh_ai_fallback(summary)
        self.ai_state["running"] = self._ai_busy
        self.ai_state["chat_running"] = self._ai_chat_busy
        self.ai_state["jina_running"] = self.ai_runtime.is_running()
        self.ai_state["config"] = self._public_ai_config()
        self.snapshot_changed.emit(
            {
                "nodes": nodes,
                "matrix": [state.to_dict() for state in self.matrix.values()],
                "history": self.history,
                "recent_history": recent_history,
                "events": [event.to_dict() for event in self.events],
                "detection_summary": summary,
                "history_total": len(self.history),
                "ai": dict(self.ai_state),
                "serial_stats": dict(self._serial_stats),
                "gateway_status": dict(self._gateway_status),
                "active_node": self._active_node,
                "serial_connected": self._serial_connected,
                "paused": self._paused,
                "diagnostics_report": self._diagnostics_report,
                "config": {
                    "presence_threshold": self.presence_threshold,
                    "confidence_threshold": self.alarm_engine.confidence_threshold,
                    "csi_quality_threshold": CSI_QUALITY_THRESHOLD,
                    "gas_threshold": self.gas_threshold,
                    "gas_threshold_ppm": self.gas_threshold,
                    "gas_calibration_r0": self.gas_calibration_r0,
                    "gas_node_calibration_count": len(self.gas_node_calibration_r0),
                    "gas_calibrated_online": sum(
                        1
                        for node_id, node in self.nodes.items()
                        if node.online and node_id in self.gas_node_calibration_r0
                    ),
                    "gas_online_nodes": sum(1 for node in self.nodes.values() if node.online),
                    "gas_clean_air_ppm": MQ135_CLEAN_AIR_PPM,
                    "afh_enabled": self.afh_enabled,
                    "mesh_enabled": self.mesh_enabled,
                    "last_sync_at": self.last_sync_at,
                    "control_id": CONTROL_ID,
                    "online_matrix": sum(1 for s in self.matrix.values() if s.online),
                    "total_matrix": len(self.matrix),
                    "matrix_filter": self._matrix_filter,
                    "recording_active": self._recorder.active,
                    "replay_active": self._replay_timer.isActive(),
                    "mobile_override_active": bool(self._mobile_presence_overrides),
                },
            }
        )

    def _recent_history(self, seconds: float, limit: int) -> list[dict[str, Any]]:
        """返回 UI 实时绘图/研判需要的最近窗口，避免反复扫描完整历史。"""

        if not self.history:
            return []
        now = time.time()
        cutoff = now - float(seconds)
        recent: list[dict[str, Any]] = []
        for sample in reversed(self.history):
            timestamp = _float(sample.get("timestamp"), now)
            if timestamp < cutoff:
                break
            recent.append(sample)
            if len(recent) >= limit:
                break
        recent.reverse()
        return recent

    def _append_event(
        self,
        title: str,
        message: str,
        node_id: int = 0,
        level: str = "INFO",
        kind: str = "system",
        event_time: float | None = None,
    ) -> None:
        self.events.append(
            EventRecord(
                time=event_time if event_time is not None else time.time(),
                title=title,
                message=message,
                node_id=node_id,
                level=level,
                kind=kind,
            )
        )
        if len(self.events) > MAX_EVENT_ROWS:
            del self.events[: len(self.events) - MAX_EVENT_ROWS]
        self._dirty = True

    def _node_dicts(self) -> dict[int, dict[str, Any]]:
        return {node_id: node.to_dict() for node_id, node in self.nodes.items()}

    def _node_label(self, node_id: int) -> str:
        node = self.nodes.get(node_id)
        if node is not None and node.label:
            return node.label
        return NODE_LABELS.get(node_id, f"node{node_id}")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _canonical_sample_source(value: Any) -> str:
    """Normalize legacy source labels to stable export/UI identifiers."""

    source = str(value or "serial_real").strip().lower()
    aliases = {
        "serial": "serial_real",
        "control": "demo_mode",
        "mobile": "mobile_override",
    }
    return aliases.get(source, source)


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


def _optional_score(value: Any) -> float | None:
    if value is None:
        return None
    return _score(value)


def _score(value: Any) -> float:
    score = _float(value)
    if score > 1.0:
        score /= 100.0
    return max(0.0, min(score, 1.0))


def _snr_from_rssi(rssi: float) -> float:
    """真实帧暂未提供 SNR 时，根据 RSSI 做保守估算用于 UI 展示。"""

    return max(-8.0, min(14.0, 18.0 - (abs(rssi) - 45.0) * 0.22))


def _packet_loss_from_rssi(rssi: float) -> float:
    """真实帧暂未提供丢包率时，根据 RSSI 估算展示值。"""

    return max(0.0, min(18.0, (abs(rssi) - 50.0) * 0.12))
