"""EchoGuard 上位机全局配置。

中文注释：本文件集中放置品牌文案、主题色、刷新节奏、节点常量、阈值默认值、
导出目录以及全局 QSS。界面层 / 数据层 / 工具层不再各自硬编码魔法数字，
后续接入真实硬件或调整演示参数时也更安全。

参考界面（两张设计稿）：
* 仪表盘页：融合扰动趋势 + 生命体征指标卡 + 环境/无线分组 + 右侧事件流 + 拓扑。
* 传感器页：活动节点矩阵表格 + 右侧系统核心配置（阈值滑条 / 开关 / 同步按钮）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

try:
    from .version import DISPLAY_VERSION
except ImportError:
    if __package__:
        raise
    from version import DISPLAY_VERSION


# ---------------------------------------------------------------------------
# 品牌与基础常量
# ---------------------------------------------------------------------------
BRAND_NAME = "EchoGuard"
BRAND_VERSION = DISPLAY_VERSION
APP_TITLE = "EchoGuard"
WINDOW_TITLE = f"EchoGuard {BRAND_VERSION}"
CONTROL_ID = "0xFF-AD-01"

BAUDRATE = 115200

# 中文注释：常见竞赛配置为 1..4，但上位机实际以 Gateway 串口帧中的 id 自动发现节点。
NODE_IDS = (1, 2, 3, 4)
GATEWAY_ID = "GW_01"

UI_REFRESH_MS = 200
AUTO_PORT_REFRESH_MS = 3000
OFFLINE_SECONDS = 8.0
MAX_HISTORY_ROWS = 6000
MAX_EVENT_ROWS = 240

EXPORT_DIR = Path(__file__).resolve().parent / "exports"
APP_ICON_PATH = Path(__file__).resolve().parent / "assets" / "app_icon.ico"


# ---------------------------------------------------------------------------
# 主题色：深色保留现有工业风，浅色只改变颜色，不改变任何布局。
# ---------------------------------------------------------------------------
DARK_THEME = {
    "bg": "#0E0F12",
    "bg_deep": "#08090B",
    "topbar": "#0B0C0F",
    "panel": "#121317",
    "rail": "#101115",
    "card": "#191A1F",
    "card_alt": "#1F2128",
    "card_hover": "#23252D",
    "row_hover": "#1C1E24",
    "border": "#2A2C33",
    "border_soft": "#22242A",
    "divider": "#26282F",
    "text": "#F4F5F7",
    "text_soft": "#D7DBE3",
    "muted": "#9097A3",
    "muted_2": "#6C7280",
    "muted_3": "#565B66",
    "blue": "#2F80FF",
    "blue_bright": "#4C97FF",
    "blue_soft": "#A8C7FF",
    "green": "#34C759",
    "green_soft": "#5BE07E",
    "yellow": "#FFD166",
    "orange": "#FF9F0A",
    "red": "#FF453A",
    "red_soft": "#FF8A80",
    "cyan": "#64D2FF",
    "tag_bg": "#23252C",
    "tag_border": "#34373F",
    "nav_selected_bg": "#102A4D",
    "nav_selected_border": "#1C406F",
    "button_hover_border": "#3C3F48",
    "button_pressed": "#16171C",
    "danger_bg": "#36181A",
    "danger_border": "#5A2A2C",
    "progress_bg": "#2B2D34",
    "scroll_handle": "#3A3D45",
    "scroll_handle_hover": "#4A4D56",
    "plot_axis": "#3A3D45",
    "plot_axis_text": "#6C7280",
    "plot_noise": "#8E8E93",
    "plot_noise_legend": "#B8BCC6",
    "topology_bg": "#0C0D11",
    "topology_cross": "#172236",
    "topology_ring": "#2A3445",
    "topology_gateway": "#0F1118",
    "topology_gateway_text": "#E7EEFF",
    "topology_node_text": "#E8ECF5",
    "warning_bg": "#36181A",
    "selection_text": "#FFFFFF",
}

LIGHT_THEME = {
    "bg": "#F7F9FC",
    "bg_deep": "#EEF2F7",
    "topbar": "#FFFFFF",
    "panel": "#FFFFFF",
    "rail": "#F3F6FA",
    "card": "#FFFFFF",
    "card_alt": "#F3F6FA",
    "card_hover": "#EAF0F7",
    "row_hover": "#F0F4F9",
    "border": "#D7DEE8",
    "border_soft": "#E4E9F0",
    "divider": "#DCE3EB",
    "text": "#17202A",
    "text_soft": "#344050",
    "muted": "#6B7280",
    "muted_2": "#8A94A3",
    "muted_3": "#A3ACB8",
    "blue": "#1E6BFF",
    "blue_bright": "#2F80FF",
    "blue_soft": "#2B6CBF",
    "green": "#1D9A45",
    "green_soft": "#25B657",
    "yellow": "#B98500",
    "orange": "#D97706",
    "red": "#D92D20",
    "red_soft": "#B42318",
    "cyan": "#0891B2",
    "tag_bg": "#EAF1FF",
    "tag_border": "#C9D8F0",
    "nav_selected_bg": "#E7F0FF",
    "nav_selected_border": "#B8D1FF",
    "button_hover_border": "#B8C4D4",
    "button_pressed": "#E1E7EF",
    "danger_bg": "#FFF1F0",
    "danger_border": "#FFCDC7",
    "progress_bg": "#E3E8EF",
    "scroll_handle": "#C7D0DD",
    "scroll_handle_hover": "#AEB9C8",
    "plot_axis": "#C4CBD6",
    "plot_axis_text": "#697586",
    "plot_noise": "#98A2B3",
    "plot_noise_legend": "#667085",
    "topology_bg": "#F4F7FB",
    "topology_cross": "#D8E3F0",
    "topology_ring": "#C8D6E6",
    "topology_gateway": "#FFFFFF",
    "topology_gateway_text": "#2D4059",
    "topology_node_text": "#263242",
    "warning_bg": "#FFF1F0",
    "selection_text": "#FFFFFF",
}

_THEME_MODE = "dark"
THEME = DARK_THEME.copy()


def ui_settings_path() -> Path:
    """返回上位机 UI 本机配置路径。"""

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "EchoGuard" / "ui_settings.json"
    return Path.home() / ".echoguard" / "ui_settings.json"


def load_ui_settings() -> dict[str, object]:
    """读取本机 UI 设置；失败时回退默认值。"""

    path = ui_settings_path()
    if not path.exists():
        return {"theme_mode": "dark"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"theme_mode": "dark"}
    if not isinstance(payload, dict):
        return {"theme_mode": "dark"}
    mode = str(payload.get("theme_mode") or "dark").lower()
    payload["theme_mode"] = mode if mode in {"dark", "light"} else "dark"
    return payload


def save_ui_settings(settings: dict[str, object]) -> Path:
    """保存本机 UI 设置。"""

    path = ui_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_ui_settings()
    current.update(settings)
    mode = str(current.get("theme_mode") or "dark").lower()
    current["theme_mode"] = mode if mode in {"dark", "light"} else "dark"
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def theme_mode() -> str:
    """返回当前生效主题模式。"""

    return _THEME_MODE


def set_theme_mode(mode: str) -> str:
    """切换当前主题；THEME 原地更新，保证已导入引用同步生效。"""

    global _THEME_MODE
    normalized = str(mode or "dark").lower()
    if normalized not in {"dark", "light"}:
        normalized = "dark"
    source = LIGHT_THEME if normalized == "light" else DARK_THEME
    THEME.clear()
    THEME.update(source)
    _THEME_MODE = normalized
    _sync_derived_theme()
    return _THEME_MODE


# ---------------------------------------------------------------------------
# 左侧导航：five 页面（含占位 / 功能页）
# 中文注释：key 用于 QStackedWidget 路由，icon 为本地 SVG 图标注册表名称。
# ---------------------------------------------------------------------------
NAV_ITEMS = (
    {"key": "dashboard", "text": "仪表盘", "icon": "layout-dashboard"},
    {"key": "sensors", "text": "节点管理", "icon": "radio"},
    {"key": "analysis", "text": "数据分析", "icon": "chart-line"},
    {"key": "ai_assist", "text": "AI 辅助", "icon": "brain-circuit"},
    {"key": "diagnostics", "text": "技术诊断", "icon": "wrench"},
    {"key": "history", "text": "历史记录", "icon": "history"},
)


# ---------------------------------------------------------------------------
# 仪表盘页：4 个生命体征感知节点
# ---------------------------------------------------------------------------
NODE_LABELS = {
    1: "node1",
    2: "node2",
    3: "node3",
    4: "node4",
}

# 中文注释：拓扑图使用归一化坐标，绘制时按控件尺寸映射到圆形轨道。
TOPOLOGY_NODE_POSITIONS = {
    1: (-0.62, -0.42),
    2: (0.66, 0.40),
    3: (0.52, -0.50),
    4: (-0.40, 0.60),
}


# ---------------------------------------------------------------------------
# 传感器页：活动节点矩阵（LoRa 节点）
# 中文注释：节点矩阵由真实 Gateway 串口帧自动发现，本常量保留为空元组仅作历史兼容。
# ---------------------------------------------------------------------------
OPERATING_MODES = ("POWER_SAVE", "HIGH_PERF", "SLEEP", "DEBUG_MODE", "NORMAL")

NODE_MATRIX: tuple[dict[str, object], ...] = ()

# 运行健康度等级（设计稿：极佳 / 良好 / 未激活 / 严重错误）
HEALTH_EXCELLENT = "极佳"
HEALTH_GOOD = "良好"
HEALTH_INACTIVE = "未激活"
HEALTH_CRITICAL = "严重错误"

HEALTH_COLORS = {
    HEALTH_EXCELLENT: THEME["blue_soft"],
    HEALTH_GOOD: THEME["blue_soft"],
    HEALTH_INACTIVE: THEME["muted_2"],
    HEALTH_CRITICAL: THEME["red"],
}


def _sync_derived_theme() -> None:
    """同步依赖 THEME 的派生颜色表。"""

    health_colors = globals().get("HEALTH_COLORS")
    if isinstance(health_colors, dict):
        health_colors.update(
            {
                HEALTH_EXCELLENT: THEME["blue_soft"],
                HEALTH_GOOD: THEME["blue_soft"],
                HEALTH_INACTIVE: THEME["muted_2"],
                HEALTH_CRITICAL: THEME["red"],
            }
        )


# ---------------------------------------------------------------------------
# 阈值默认值（传感器页右侧配置 + 报警规则共享）
# ---------------------------------------------------------------------------
PRESENCE_THRESHOLD = 0.42       # 存在感应阈值（0~1，对应设计稿 42%）
GAS_THRESHOLD_PPM = 1000.0      # MQ-135 CO2 估算 ppm 预警阈值
GAS_ALARM_PPM = 2000.0          # 触发系统警告的 CO2 估算 ppm 上限
GAS_THRESHOLD_RAW = GAS_THRESHOLD_PPM  # 兼容旧导入名；当前语义已迁移为 ppm
GAS_ALARM_RAW = GAS_ALARM_PPM          # 兼容旧导入名；规则比较 ppm
CONFIDENCE_THRESHOLD = 0.75     # 置信度诊断参考值，不参与存在感知硬判定
CSI_QUALITY_THRESHOLD = 0.45    # CSI 质量诊断参考值，不参与存在感知硬判定
ALARM_DEDUP_SECONDS = 5.0       # 同类报警去重窗口

DEFAULT_AFH_ENABLED = True      # 自动频率跳变
DEFAULT_MESH_ENABLED = False    # 多级网格中继


# ---------------------------------------------------------------------------
# CSV 导出字段
# ---------------------------------------------------------------------------
CSV_FIELDS = [
    "timestamp",
    "datetime",
    "node_id",
    "node_code",
    "seq",
    "presence_score",
    "motion_score",
    "breath_bpm",
    "confidence",
    "csi_quality",
    "csi_sample_count",
    "breath_lock",
    "noise_floor",
    "gas",
    "gas_raw",
    "gas_ppm",
    "temperature",
    "humidity",
    "rssi",
    "wifi_rssi",
    "snr",
    "packet_loss",
    "battery",
    "mode",
    "source",
    "raw",
]


def build_qss(mode: str | None = None) -> str:
    """返回全局 QSS。

    中文注释：卡片、按钮、导航、状态胶囊、滑条、开关轨道和滚动条都在这里统一
    定义。阴影由 QGraphicsDropShadowEffect 负责，QSS 只管边框、圆角和配色。
    """

    if mode is not None:
        source = LIGHT_THEME if str(mode).lower() == "light" else DARK_THEME
        t = source
    else:
        t = THEME
    return f"""
    * {{
        font-family: "Microsoft YaHei", "Segoe UI", "PingFang SC", Arial, sans-serif;
        font-size: 14px;
        color: {t["text"]};
        selection-background-color: {t["blue"]};
        selection-color: {t["selection_text"]};
    }}

    QMainWindow, QWidget#Root {{
        background: {t["bg"]};
    }}

    /* ---------------- 顶部栏 ---------------- */
    QFrame#TopBar {{
        background: {t["topbar"]};
        border-bottom: 1px solid {t["border_soft"]};
    }}

    QLabel#AppTitle {{
        font-size: 16px;
        font-weight: 700;
        color: {t["text"]};
    }}

    QLabel#TopSubtitle {{
        font-size: 14px;
        color: {t["muted"]};
        font-weight: 600;
    }}

    QLabel#TopIcon {{
        font-size: 17px;
        color: {t["muted"]};
        padding: 4px;
    }}

    /* ---------------- 左侧导航 ---------------- */
    QFrame#LeftNav {{
        background: {t["bg_deep"]};
        border-right: 1px solid {t["border_soft"]};
    }}

    QLabel#NavCaption {{
        font-size: 13px;
        font-weight: 700;
        color: {t["muted"]};
        letter-spacing: 1px;
    }}

    QLabel#NavSession {{
        font-size: 12px;
        color: {t["muted_2"]};
    }}

    QFrame#NavItem {{
        background: transparent;
        border-radius: 12px;
        border: 1px solid transparent;
    }}

    QFrame#NavItem:hover {{
        background: {t["card_hover"]};
    }}

    QFrame#NavItem[selected="true"] {{
        background: {t["nav_selected_bg"]};
        border: 1px solid {t["nav_selected_border"]};
        border-left: 3px solid {t["blue_bright"]};
    }}

    QLabel#NavIcon {{
        font-size: 15px;
        color: {t["text_soft"]};
    }}

    QLabel#NavIcon[selected="true"] {{
        color: {t["blue_bright"]};
    }}

    QLabel#NavText {{
        font-size: 15px;
        font-weight: 600;
        color: {t["text_soft"]};
    }}

    QLabel#NavText[selected="true"] {{
        color: {t["text"]};
        font-weight: 700;
    }}

    /* ---------------- 右侧栏 ---------------- */
    QFrame#RightRail {{
        background: {t["rail"]};
        border-left: 1px solid {t["border_soft"]};
    }}

    /* ---------------- 卡片 ---------------- */
    QFrame[card="true"] {{
        background: {t["card"]};
        border: 1px solid {t["border"]};
        border-radius: 13px;
    }}

    QFrame[cardAlt="true"] {{
        background: {t["card_alt"]};
        border: 1px solid {t["border"]};
        border-radius: 10px;
    }}

    QFrame[plain="true"] {{
        background: transparent;
        border: 0;
    }}

    /* ---------------- 文案 ---------------- */
    QLabel#SectionTitle {{
        font-size: 16px;
        font-weight: 700;
        color: {t["text"]};
    }}

    QLabel#SectionSub {{
        font-size: 13px;
        color: {t["muted"]};
    }}

    QLabel#SubtleText {{
        color: {t["muted"]};
        font-size: 12px;
    }}

    QLabel#MetricTitle {{
        color: {t["muted"]};
        font-size: 12px;
        font-weight: 600;
    }}

    QLabel#MetricValue {{
        color: {t["text"]};
        font-size: 24px;
        font-weight: 700;
    }}

    QLabel#MetricHint {{
        color: {t["blue_soft"]};
        font-size: 12px;
        font-weight: 600;
    }}

    QLabel#ColHeader {{
        color: {t["muted"]};
        font-size: 12px;
        font-weight: 700;
    }}

    QLabel#NodeCode {{
        font-size: 14px;
        font-weight: 700;
        color: {t["text"]};
    }}

    /* ---------------- 按钮 ---------------- */
    QPushButton {{
        background: {t["card_alt"]};
        border: 1px solid {t["border"]};
        border-radius: 10px;
        padding: 8px 14px;
        font-weight: 600;
        color: {t["text_soft"]};
    }}

    QPushButton:hover {{
        background: {t["card_hover"]};
        border-color: {t["button_hover_border"]};
    }}

    QPushButton:pressed {{
        background: {t["button_pressed"]};
    }}

    QPushButton#PrimaryButton {{
        background: {t["blue"]};
        border: 1px solid {t["blue"]};
        color: {t["selection_text"]};
    }}

    QPushButton#PrimaryButton:hover {{
        background: {t["blue_bright"]};
        border-color: {t["blue_bright"]};
    }}

    QPushButton#GhostButton {{
        background: transparent;
        border: 1px solid {t["border"]};
        color: {t["text_soft"]};
    }}

    QPushButton#GhostButton:hover {{
        background: {t["card_hover"]};
    }}

    QPushButton#DangerButton {{
        background: {t["danger_bg"]};
        border: 1px solid {t["danger_border"]};
        color: {t["red_soft"]};
    }}

    QPushButton#SyncButton {{
        background: {t["card_alt"]};
        border: 1px solid {t["border"]};
        border-radius: 10px;
        padding: 14px 14px;
        font-size: 14px;
        font-weight: 700;
        color: {t["text"]};
    }}

    QPushButton#SyncButton:hover {{
        background: {t["card_hover"]};
    }}

    QPushButton#RowMenu {{
        background: transparent;
        border: 0;
        color: {t["muted"]};
        font-size: 18px;
        padding: 2px 8px;
    }}

    QPushButton#RowMenu:hover {{
        color: {t["text"]};
    }}

    QPushButton#IconClose {{
        background: transparent;
        border: 0;
        color: {t["muted"]};
        font-size: 18px;
    }}

    QPushButton#IconClose:hover {{
        color: {t["text"]};
    }}

    /* ---------------- 下拉框 ---------------- */
    QComboBox {{
        background: {t["card_alt"]};
        border: 1px solid {t["border"]};
        border-radius: 10px;
        padding: 6px 10px;
        min-width: 120px;
        color: {t["text_soft"]};
    }}

    QComboBox:hover {{
        border-color: {t["button_hover_border"]};
    }}

    QComboBox::drop-down {{
        border: 0;
        width: 18px;
    }}

    QComboBox QAbstractItemView {{
        background: {t["card_alt"]};
        border: 1px solid {t["border"]};
        outline: 0;
        selection-background-color: {t["blue"]};
        color: {t["text_soft"]};
    }}

    QMenu {{
        background: {t["card_alt"]};
        border: 1px solid {t["border"]};
        border-radius: 10px;
        padding: 6px;
    }}

    QMenu::item {{
        color: {t["text_soft"]};
        padding: 8px 24px 8px 12px;
        border-radius: 6px;
    }}

    QMenu::item:selected {{
        background: {t["blue"]};
        color: {t["selection_text"]};
    }}

    QMenu::item:disabled {{
        color: {t["muted_2"]};
    }}

    QLineEdit {{
        background: {t["card_alt"]};
        border: 1px solid {t["border"]};
        border-radius: 10px;
        padding: 7px 10px;
        color: {t["text_soft"]};
    }}

    QLineEdit:hover {{
        border-color: {t["button_hover_border"]};
    }}

    QLineEdit:focus {{
        border-color: {t["blue"]};
    }}

    QCheckBox {{
        color: {t["text_soft"]};
        spacing: 8px;
        font-weight: 600;
    }}

    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border-radius: 4px;
        border: 1px solid {t["border"]};
        background: {t["card_alt"]};
    }}

    QCheckBox::indicator:checked {{
        background: {t["blue"]};
        border-color: {t["blue"]};
    }}

    /* ---------------- 进度/电量条 ---------------- */
    QProgressBar {{
        background: {t["progress_bg"]};
        border: 0;
        border-radius: 4px;
        height: 7px;
        text-align: right;
    }}

    QProgressBar::chunk {{
        background: {t["blue_soft"]};
        border-radius: 4px;
    }}

    /* ---------------- 滑条 ---------------- */
    QSlider::groove:horizontal {{
        height: 5px;
        background: {t["progress_bg"]};
        border-radius: 3px;
    }}

    QSlider::sub-page:horizontal {{
        background: {t["blue"]};
        border-radius: 3px;
    }}

    QSlider::handle:horizontal {{
        width: 16px;
        height: 16px;
        margin: -6px 0;
        border-radius: 8px;
        background: {t["selection_text"]};
        border: 2px solid {t["blue"]};
    }}

    QSlider::handle:horizontal:hover {{
        background: {t["blue_soft"]};
    }}

    /* ---------------- 滚动条 ---------------- */
    QScrollArea {{
        background: transparent;
        border: 0;
    }}

    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 2px;
    }}

    QScrollBar::handle:vertical {{
        background: {t["scroll_handle"]};
        border-radius: 5px;
        min-height: 30px;
    }}

    QScrollBar::handle:vertical:hover {{
        background: {t["scroll_handle_hover"]};
    }}

    QScrollBar::add-line:vertical,
    QScrollBar::sub-line:vertical {{
        height: 0px;
    }}

    QScrollBar:horizontal {{
        background: transparent;
        height: 10px;
        margin: 2px;
    }}

    QScrollBar::handle:horizontal {{
        background: {t["scroll_handle"]};
        border-radius: 5px;
        min-width: 30px;
    }}

    QScrollBar::add-line:horizontal,
    QScrollBar::sub-line:horizontal {{
        width: 0px;
    }}

    /* ---------------- 表格（QTableWidget，用于历史记录页） ---------------- */
    QTableWidget {{
        background: {t["card"]};
        border: 1px solid {t["border"]};
        border-radius: 10px;
        gridline-color: {t["border_soft"]};
    }}

    QHeaderView::section {{
        background: {t["card_alt"]};
        color: {t["muted"]};
        border: 0;
        border-bottom: 1px solid {t["border"]};
        padding: 8px;
        font-weight: 700;
    }}

    QTableWidget::item {{
        padding: 6px;
        border-bottom: 1px solid {t["border_soft"]};
    }}
    """
