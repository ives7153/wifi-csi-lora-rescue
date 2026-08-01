"""EchoGuard Mobile 手机热点控制版入口。"""

from __future__ import annotations

import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

try:
    from . import config
    from .version import DISPLAY_VERSION
except ImportError:
    if __package__:
        raise
    import config  # type: ignore
    from version import DISPLAY_VERSION  # type: ignore

config.BRAND_VERSION = DISPLAY_VERSION
config.APP_TITLE = "EchoGuard Mobile"
config.WINDOW_TITLE = f"EchoGuard Mobile {DISPLAY_VERSION}"

try:
    from .config import APP_ICON_PATH, APP_TITLE, WINDOW_TITLE, build_qss, load_ui_settings, set_theme_mode
    from .core import DataManager
    from .mobile_control import MobileControlPanel
    from .ui import MainWindow
except ImportError:
    if __package__:
        raise
    from config import APP_ICON_PATH, APP_TITLE, WINDOW_TITLE, build_qss, load_ui_settings, set_theme_mode
    from core import DataManager
    from mobile_control import MobileControlPanel
    from ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName(APP_TITLE)
    app_icon = QIcon(str(APP_ICON_PATH))
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    set_theme_mode(str(load_ui_settings().get("theme_mode", "dark")))
    app.setStyleSheet(build_qss())

    # Mobile 版只接受显式手机控制，不启用普通版的 20 秒启动演示。
    manager = DataManager(startup_presence_demo_seconds=0.0)
    window = MainWindow()
    window.setWindowTitle(WINDOW_TITLE)
    panel = MobileControlPanel()
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)
        panel.setWindowIcon(app_icon)

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

    panel.node_presence_changed.connect(manager.set_mobile_presence)
    panel.restore_node_requested.connect(manager.clear_mobile_presence)
    panel.restore_all_requested.connect(manager.clear_all_mobile_presence)

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

    app.aboutToQuit.connect(panel.shutdown)
    app.aboutToQuit.connect(manager.shutdown)

    window.show()
    panel.show()
    manager.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
