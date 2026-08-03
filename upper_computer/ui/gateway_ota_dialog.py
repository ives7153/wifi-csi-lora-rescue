"""Non-blocking Gateway 01 LAN OTA dialog."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

try:
    from ..config import THEME
    from ..gateway_ota import (
        GatewayFirmwareInfo,
        GatewayOTAClient,
        GatewayOTAError,
        GatewayOTAStatus,
        inspect_gateway_image,
    )
except ImportError:
    if __package__ and __package__.startswith("upper_computer"):
        raise
    from config import THEME
    from gateway_ota import (  # type: ignore
        GatewayFirmwareInfo,
        GatewayOTAClient,
        GatewayOTAError,
        GatewayOTAStatus,
        inspect_gateway_image,
    )


class GatewayOTAWorker(QThread):
    progress_changed = pyqtSignal(int)
    status_changed = pyqtSignal(str)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        mode: str,
        gateway_address: str,
        image: GatewayFirmwareInfo | None = None,
        token: str = "",
    ) -> None:
        super().__init__()
        self.mode = mode
        self.gateway_address = gateway_address
        self.image = image
        self.token = token

    def run(self) -> None:
        try:
            client = GatewayOTAClient(self.gateway_address)
            if self.mode == "status":
                self.completed.emit(client.get_status())
                return
            if self.mode != "upload" or self.image is None:
                raise GatewayOTAError("OTA 后台任务参数无效")
            status = client.upload_and_verify(
                self.image,
                self.token,
                progress=self._emit_progress,
                status_callback=self.status_changed.emit,
            )
            self.completed.emit(status)
        except GatewayOTAError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # pragma: no cover - final UI safety boundary
            self.failed.emit(f"OTA 后台任务异常：{exc}")

    def _emit_progress(self, sent: int, total: int) -> None:
        percent = 0 if total <= 0 else round(sent * 100 / total)
        self.progress_changed.emit(max(0, min(100, percent)))


class GatewayOTADialog(QDialog):
    """Firmware picker, uploader and post-reboot health verifier."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        self.setWindowTitle("Gateway 01 局域网 OTA")
        self.setModal(False)
        self.resize(660, 560)
        self.setMinimumSize(600, 520)
        self._image: GatewayFirmwareInfo | None = None
        self._worker: GatewayOTAWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)

        title = QLabel("Gateway 01 固件升级")
        title.setObjectName("SectionTitle")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        root.addWidget(title)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        self.address_edit = QLineEdit("192.168.4.1")
        self.address_edit.setPlaceholderText("192.168.4.1")
        form.addRow("Gateway 地址", self.address_edit)

        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("本地 sdkconfig.gw01.local 中的 OTA 令牌")
        form.addRow("OTA 令牌", self.token_edit)

        firmware_row = QHBoxLayout()
        firmware_row.setSpacing(8)
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("选择 Gateway 01 application .bin")
        browse_button = QPushButton("选择")
        browse_button.setObjectName("GhostButton")
        browse_button.clicked.connect(self._choose_firmware)
        firmware_row.addWidget(self.path_edit, 1)
        firmware_row.addWidget(browse_button)
        form.addRow("固件文件", firmware_row)
        root.addLayout(form)

        self.image_label = QLabel("尚未选择固件")
        self.image_label.setWordWrap(True)
        self.image_label.setObjectName("SubtleText")
        root.addWidget(self.image_label)

        self.sha_edit = QLineEdit()
        self.sha_edit.setReadOnly(True)
        self.sha_edit.setPlaceholderText("SHA-256 会在选择固件后自动计算")
        root.addWidget(self.sha_edit)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        root.addWidget(self.progress)

        self.status_box = QTextEdit()
        self.status_box.setReadOnly(True)
        self.status_box.setMinimumHeight(135)
        self.status_box.setPlainText("等待检查 Gateway 01 OTA 状态。")
        root.addWidget(self.status_box, 1)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.check_button = QPushButton("检查 Gateway")
        self.check_button.setObjectName("GhostButton")
        self.check_button.clicked.connect(self._check_gateway)
        self.upload_button = QPushButton("上传并升级")
        self.upload_button.setObjectName("PrimaryButton")
        self.upload_button.clicked.connect(self._upload)
        close_button = QPushButton("关闭")
        close_button.setObjectName("GhostButton")
        close_button.clicked.connect(self.close)
        actions.addWidget(self.check_button)
        actions.addWidget(self.upload_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        root.addLayout(actions)
        self.refresh_theme()

    def _choose_firmware(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择 Gateway 01 OTA 应用镜像",
            "",
            "ESP32-S3 应用镜像 (*.bin);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            image = inspect_gateway_image(path)
        except GatewayOTAError as exc:
            self._image = None
            self.path_edit.clear()
            self.sha_edit.clear()
            self.image_label.setText(str(exc))
            QMessageBox.critical(self, "固件校验失败", str(exc))
            return
        self._image = image
        self.path_edit.setText(str(image.path))
        self.sha_edit.setText(image.sha256)
        self.image_label.setText(
            f"版本 {image.version}  |  ESP32-S3  |  ESP-IDF {image.idf_version or '未知'}"
            f"  |  {image.size / 1024:.1f} KiB"
        )

    def _check_gateway(self) -> None:
        self._start_worker(
            GatewayOTAWorker("status", self.address_edit.text().strip()),
            "正在读取 Gateway 01 OTA 状态...",
        )

    def _upload(self) -> None:
        if self._image is None:
            QMessageBox.warning(self, "未选择固件", "请先选择并校验 Gateway 01 应用镜像。")
            return
        token = self.token_edit.text().strip()
        if len(token) < 16:
            QMessageBox.warning(self, "令牌无效", "请输入本地配置中的 OTA 令牌。")
            return
        confirmation = QMessageBox.question(
            self,
            "确认 Gateway 01 OTA",
            f"将上传 Gateway 固件 {self._image.version} 并自动重启 Gateway 01。\n"
            "此操作不会更新 Node 或 Gateway 02。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        self.progress.setValue(0)
        self._start_worker(
            GatewayOTAWorker(
                "upload",
                self.address_edit.text().strip(),
                image=self._image,
                token=token,
            ),
            "正在校验 Gateway 01 与固件...",
        )

    def _start_worker(self, worker: GatewayOTAWorker, status: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "OTA 正在运行", "请等待当前 OTA 操作完成。")
            return
        self._worker = worker
        self.status_box.setPlainText(status)
        self._set_busy(True)
        worker.progress_changed.connect(self.progress.setValue)
        worker.status_changed.connect(self.status_box.setPlainText)
        worker.completed.connect(self._worker_completed)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(self._worker_finished)
        worker.start()

    def _worker_completed(self, result: object) -> None:
        upload_completed = self._worker is not None and self._worker.mode == "upload"
        if not isinstance(result, GatewayOTAStatus):
            self.status_box.setPlainText("Gateway OTA 操作已完成。")
            return
        status = result
        lines = [
            f"Gateway：{status.gateway_id}",
            f"版本：{status.version}",
            f"状态：{status.state}",
            f"运行分区：{status.running_partition}",
            f"备用分区：{status.next_partition} ({status.next_size / 1024:.0f} KiB)",
            f"健康条件：SoftAP={'是' if status.softap_ready else '否'} / "
            f"HTTP={'是' if status.server_ready else '否'} / "
            f"LoRa={'是' if status.lora_ready else '否'}",
            f"回滚待确认：{'是' if status.pending_verify else '否'}",
        ]
        if status.last_error:
            lines.append(f"最近错误：{status.last_error}")
        self.status_box.setPlainText("\n".join(lines))
        if upload_completed:
            self.progress.setValue(100)
            QMessageBox.information(
                self,
                "OTA 升级完成",
                f"Gateway 01 已运行 {status.version}，启动健康确认通过。",
            )

    def _worker_failed(self, message: str) -> None:
        self.status_box.setPlainText(message)
        QMessageBox.critical(self, "Gateway OTA 失败", message)

    def _worker_finished(self) -> None:
        worker = self._worker
        self._worker = None
        self._set_busy(False)
        if worker is not None:
            worker.deleteLater()

    def _set_busy(self, busy: bool) -> None:
        self.check_button.setEnabled(not busy)
        self.upload_button.setEnabled(not busy)
        self.address_edit.setEnabled(not busy)
        self.token_edit.setEnabled(not busy)

    def refresh_theme(self) -> None:
        self.status_box.setStyleSheet(
            f"background: {THEME['card_alt']}; border: 1px solid {THEME['border']};"
            f" border-radius: 6px; color: {THEME['text_soft']}; padding: 8px;"
        )
        self.image_label.setStyleSheet(f"color: {THEME['text_soft']};")

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "OTA 正在运行", "OTA 完成前不能关闭此窗口。")
            event.ignore()
            return
        super().closeEvent(event)
