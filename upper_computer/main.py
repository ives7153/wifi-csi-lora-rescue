"""EchoGuard · WiFi-CSI + LoRa 感知救援系统 PyQt6 上位机入口。

中文注释：启动 QApplication、加载全局 QSS，并把 MainWindow 的用户操作信号连接到
DataManager 的业务槽函数，再把 DataManager 的数据信号连回界面刷新。所有串口数据
最终都以快照形式驱动界面，主线程不做阻塞 I/O。

运行方式：
    python -m upper_computer.main            # 包内运行（推荐）
    cd upper_computer && python main.py      # 目录内直接运行
"""

from __future__ import annotations

import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

try:
    from .config import APP_ICON_PATH, APP_TITLE, WINDOW_TITLE, build_qss, load_ui_settings, set_theme_mode
    from .core import DataManager
    from .ui import MainWindow
except ImportError:
    if __package__:
        raise
    # 兼容 cd upper_computer 后直接 python main.py
    from config import APP_ICON_PATH, APP_TITLE, WINDOW_TITLE, build_qss, load_ui_settings, set_theme_mode
    from core import DataManager
    from ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName(APP_TITLE)
    app_icon = QIcon(str(APP_ICON_PATH))
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    set_theme_mode(str(load_ui_settings().get("theme_mode", "dark")))
    app.setStyleSheet(build_qss())

    # 普通演示版：每个节点首次连接后的 20 秒内仅展示低于 0.1 的变化值；
    # 不显示倒计时，窗口结束后自动恢复节点真实存在值。
    manager = DataManager(startup_presence_demo_seconds=20.0)
    window = MainWindow()
    window.setWindowTitle(WINDOW_TITLE)
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)

    # ---------------- UI -> DataManager ----------------
    window.refresh_ports_requested.connect(manager.refresh_ports)
    window.connect_requested.connect(manager.connect_to_port)
    window.disconnect_requested.connect(manager.disconnect_serial)
    window.export_csv_requested.connect(manager.export_csv)
    window.export_filtered_csv_requested.connect(manager.export_filtered_csv)
    window.screenshot_requested.connect(manager.save_screenshot)
    window.csi_shot_requested.connect(manager.save_csi_image)
    window.analysis_shot_requested.connect(manager.save_analysis_image)
    window.active_node_changed.connect(manager.set_active_node)
    window.pause_toggled.connect(manager.set_paused)
    window.clear_events_requested.connect(manager.clear_events)
    window.clear_history_requested.connect(manager.clear_history)
    window.presence_threshold_changed.connect(manager.set_presence_threshold)
    window.gas_threshold_changed.connect(manager.set_gas_threshold)
    window.gas_calibration_requested.connect(manager.calibrate_mq135_clean_air)
    window.gas_calibration_all_requested.connect(manager.calibrate_all_mq135_clean_air)
    window.afh_toggled.connect(manager.set_afh_enabled)
    window.mesh_toggled.connect(manager.set_mesh_enabled)
    window.sync_requested.connect(manager.sync_global_config)
    window.matrix_filter_changed.connect(manager.set_matrix_filter)
    window.matrix_maintenance_requested.connect(manager.toggle_matrix_maintenance)
    window.diagnostics_requested.connect(manager.generate_diagnostics_report)
    window.ai_config_save_requested.connect(manager.save_ai_config)
    window.ai_jina_start_requested.connect(manager.start_local_jina)
    window.ai_jina_stop_requested.connect(manager.stop_local_jina)
    window.ai_embedding_test_requested.connect(manager.test_local_embedding)
    window.ai_models_requested.connect(manager.fetch_ai_models)
    window.ai_llm_test_requested.connect(manager.test_llm_api)
    window.ai_action_requested.connect(manager.handle_ai_action)
    window.recording_toggled.connect(manager.set_recording_enabled)
    window.replay_requested.connect(manager.replay_recording)
    window.region_calibration_phase_requested.connect(manager.start_region_calibration_phase)
    window.region_calibration_cancel_requested.connect(manager.cancel_region_calibration)

    # ---------------- DataManager -> UI ----------------
    manager.ports_changed.connect(window.update_ports)
    manager.status_changed.connect(window.set_status)
    manager.latest_frame_changed.connect(window.set_latest_frame)
    manager.export_message_changed.connect(window.show_export_message)
    manager.ai_operation_message_changed.connect(window.show_ai_operation_message)
    manager.ai_operation_result_changed.connect(window.set_ai_operation_result)
    manager.ai_models_changed.connect(window.set_ai_models)
    manager.recording_state_changed.connect(window.set_recording_state)
    manager.snapshot_changed.connect(window.update_snapshot)

    app.aboutToQuit.connect(manager.shutdown)

    window.show()
    manager.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
