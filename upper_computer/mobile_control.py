"""EchoGuard 手机热点局域网控制服务与独立控制窗口。"""

from __future__ import annotations

import ipaddress
import json
import math
import random
import socket
import socketserver
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QMenu,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

try:
    from .config import THEME
    from .version import DISPLAY_VERSION
except ImportError:
    if __package__ and __package__.startswith("upper_computer"):
        raise
    from config import THEME  # type: ignore
    from version import DISPLAY_VERSION  # type: ignore


DEFAULT_MOBILE_CONTROL_PORT = 8765
MOBILE_NODE_IDS = (1, 2, 3)
MAX_REQUEST_BYTES = 4096
MOBILE_MODE_RANGES = {
    "empty": (1, 10),
    "active": (80, 100),
    "micro": (50, 70),
}
MOBILE_MODE_INTERVAL_SECONDS = 1.0


class MobileHttpServer(ThreadingHTTPServer):
    """避免 HTTPServer 在启动时对 0.0.0.0 执行缓慢的反向 DNS 查询。"""

    allow_reuse_address = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


MOBILE_PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
  <title>EchoGuard Mobile</title>
  <style>
    :root{color-scheme:dark;--bg:#07111e;--card:#102036;--line:#294661;--text:#edf7ff;--muted:#9db3ca;--accent:#5fe0ce;--danger:#ff7d87}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 90% 0,#193a5d 0,transparent 42%),var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,"Microsoft YaHei",sans-serif}
    main{max-width:620px;margin:auto;padding:22px 16px 42px}header{margin-bottom:18px}h1{font-size:25px;margin:0 0 5px}.sub,.status{color:var(--muted);font-size:14px}.node{background:rgba(16,32,54,.96);border:1px solid var(--line);border-radius:17px;padding:18px;margin:13px 0;box-shadow:0 12px 28px rgba(0,0,0,.2)}
    .row{display:flex;align-items:center;justify-content:space-between;gap:12px}.name{font-weight:750;font-size:18px}.badge{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:4px 9px;color:var(--muted)}.badge.on{color:var(--accent);border-color:var(--accent)}
    .value{font-size:34px;font-weight:800;color:var(--accent);font-variant-numeric:tabular-nums;margin:12px 0 6px}input[type=range]{width:100%;accent-color:var(--accent);height:34px}.number{width:96px;padding:10px;border:1px solid var(--line);border-radius:10px;background:#091727;color:var(--text);font-size:17px;text-align:center}
    button{border:1px solid var(--line);border-radius:11px;background:#132b45;color:var(--text);padding:10px 13px;font-size:14px;font-weight:650}.restore{color:var(--muted)}.actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:18px}.modes{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:15px 0}.mode.active{background:var(--accent);border-color:var(--accent);color:#06201d}.mode-state{text-align:center;color:var(--muted);font-size:13px}.primary{background:var(--accent);border-color:var(--accent);color:#06201d}.danger{color:var(--danger)}#message{min-height:24px;margin-top:15px;text-align:center}.ok{color:var(--accent)}.error{color:var(--danger)}
  </style>
</head>
<body><main>
  <header><h1>EchoGuard Mobile</h1><div class="sub">手机热点局域网存在感知控制</div></header>
  <div class="modes"><button id="mode-empty" class="mode" onclick="setMode('empty')">无人模式</button><button id="mode-active" class="mode" onclick="setMode('active')">活动模式</button><button id="mode-micro" class="mode" onclick="setMode('micro')">微动模式</button></div>
  <div id="mode-state" class="mode-state">当前：手动控制</div>
  <div id="nodes"></div>
  <div class="actions"><button class="primary" onclick="setAllZero()">全部设为 0</button><button class="danger" onclick="restoreAll()">恢复全部真实数据</button></div>
  <div id="message" class="status">正在连接上位机…</div>
</main>
<script>
const ids=[1,2,3], timers={}, modeNames={empty:'无人模式',active:'活动模式',micro:'微动模式'};
const nodes=document.getElementById('nodes'), message=document.getElementById('message');
for(const id of ids){nodes.insertAdjacentHTML('beforeend',`<section class="node"><div class="row"><span class="name">Node ${id}</span><span id="badge-${id}" class="badge">真实数据</span></div><div id="value-${id}" class="value">0.00</div><input id="range-${id}" type="range" min="0" max="1" step="0.01" value="0"><div class="row"><input id="number-${id}" class="number" type="number" min="0" max="1" step="0.01" value="0.00"><button class="restore" onclick="restoreNode(${id})">恢复真实数据</button></div></section>`);const range=document.getElementById(`range-${id}`), number=document.getElementById(`number-${id}`);range.addEventListener('input',()=>{number.value=Number(range.value).toFixed(2);document.getElementById(`value-${id}`).textContent=number.value;queueSet(id,Number(range.value));});number.addEventListener('change',()=>{const value=Math.max(0,Math.min(1,Number(number.value)||0));number.value=value.toFixed(2);range.value=value;document.getElementById(`value-${id}`).textContent=number.value;queueSet(id,value);});}
function notice(text,ok=true){message.textContent=text;message.className=ok?'ok':'error';}
async function api(path,payload){const options=payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)};const response=await fetch(path,options);const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||`HTTP ${response.status}`);return data;}
function setModeActive(mode){for(const key of Object.keys(modeNames))document.getElementById(`mode-${key}`).classList.toggle('active',key===mode);document.getElementById('mode-state').textContent=mode?`当前：${modeNames[mode]}（每秒变化）`:'当前：手动控制';}
function applyState(data){setModeActive(data.mode||null);for(const id of ids){const state=data.nodes[String(id)];setActive(id,state.active);const focused=document.activeElement;const range=document.getElementById(`range-${id}`),number=document.getElementById(`number-${id}`);if(focused!==range&&focused!==number&&state.active){range.value=state.value;number.value=Number(state.value).toFixed(2);document.getElementById(`value-${id}`).textContent=number.value;}}}
async function setMode(mode){try{const data=await api('/api/mode',{mode});applyState(data);notice(`${modeNames[mode]}已启动，数值每秒变化`);}catch(error){notice(error.message,false);}}
function queueSet(id,value){clearTimeout(timers[id]);timers[id]=setTimeout(async()=>{try{await api('/api/node',{node_id:id,value});setModeActive(null);setActive(id,true);notice(`Node ${id} 已设置为 ${value.toFixed(2)}`);}catch(error){notice(error.message,false);}},90);}
function setActive(id,active){const badge=document.getElementById(`badge-${id}`);badge.textContent=active?'手机覆盖':'真实数据';badge.classList.toggle('on',active);}
async function restoreNode(id){try{const data=await api('/api/node',{node_id:id,active:false});applyState(data);notice(`Node ${id} 已恢复真实数据`);}catch(error){notice(error.message,false);}}
async function restoreAll(){try{const data=await api('/api/restore',{});applyState(data);notice('已恢复全部真实数据');}catch(error){notice(error.message,false);}}
async function setAllZero(){for(const id of ids){document.getElementById(`range-${id}`).value=0;document.getElementById(`number-${id}`).value='0.00';document.getElementById(`value-${id}`).textContent='0.00';}try{await Promise.all(ids.map(id=>api('/api/node',{node_id:id,value:0})));setModeActive(null);for(const id of ids)setActive(id,true);notice('全部节点已设置为 0.00');}catch(error){notice(error.message,false);}}
async function refresh(){try{const data=await api('/api/state');applyState(data);if(!message.classList.contains('error'))notice('已连接上位机');}catch(error){notice(`连接失败：${error.message}`,false);}}
refresh();setInterval(refresh,1000);
</script></body></html>"""


def discover_lan_ipv4() -> str:
    """返回默认网络路由使用的 IPv4，失败时回退到可用私网地址。"""

    candidates: list[str] = []
    route_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        route_socket.connect(("8.8.8.8", 80))
        candidates.append(str(route_socket.getsockname()[0]))
    except OSError:
        pass
    finally:
        route_socket.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            candidates.append(str(info[4][0]))
    except OSError:
        pass

    unique = list(dict.fromkeys(candidates))
    for candidate in unique:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.version == 4 and address.is_private and not address.is_loopback and not address.is_link_local:
            return candidate
    for candidate in unique:
        if candidate and not candidate.startswith("127.") and not candidate.startswith("169.254."):
            return candidate
    return "127.0.0.1"


class MobileControlService(QObject):
    """HTTP 服务桥；所有业务修改均通过 Qt 信号交回主线程。"""

    node_presence_requested = pyqtSignal(int, float)
    restore_node_requested = pyqtSignal(int)
    restore_all_requested = pyqtSignal()
    service_state_changed = pyqtSignal(bool, str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._states: dict[int, dict[str, Any]] = {
            node_id: {"active": False, "value": 0.0} for node_id in MOBILE_NODE_IDS
        }
        self._server: MobileHttpServer | None = None
        self._thread: threading.Thread | None = None
        self._mode: str | None = None
        self._mode_thread: threading.Thread | None = None
        self._mode_stop = threading.Event()
        self._random = random.SystemRandom()
        self._port = 0
        self._address = discover_lan_ipv4()

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        return self._port

    @property
    def address(self) -> str:
        return self._address

    @property
    def url(self) -> str:
        return f"http://{self._address}:{self._port}" if self._port else ""

    def start_service(self, port: int = DEFAULT_MOBILE_CONTROL_PORT) -> bool:
        if self.running:
            self.service_state_changed.emit(True, self.url, "手机控制服务已运行")
            return True

        self._address = discover_lan_ipv4()
        handler_class = self._build_handler_class()
        try:
            server = MobileHttpServer(("0.0.0.0", int(port)), handler_class)
        except OSError as exc:
            self.service_state_changed.emit(False, "", f"服务启动失败：{exc}")
            return False

        server.daemon_threads = True
        self._mode_stop.clear()
        self._server = server
        self._port = int(server.server_address[1])
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="echoguard-mobile-http",
            daemon=True,
        )
        self._thread.start()
        message = "手机控制服务已启动"
        if self._address == "127.0.0.1":
            message = "服务已启动，但未检测到可供手机访问的局域网 IP"
        self.service_state_changed.emit(True, self.url, message)
        return True

    def stop_service(self) -> None:
        server = self._server
        thread = self._thread
        mode_thread = self._mode_thread
        self._server = None
        self._thread = None
        self._mode_thread = None
        self._port = 0
        self._mode_stop.set()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        if mode_thread is not None and mode_thread.is_alive():
            mode_thread.join(timeout=1.5)
        self.restore_all()
        self.service_state_changed.emit(False, "", "手机控制服务已停止，已恢复真实数据")

    def restore_all(self) -> None:
        with self._lock:
            self._mode = None
            for state in self._states.values():
                state["active"] = False
        self.restore_all_requested.emit()

    def shutdown(self) -> None:
        self.stop_service()

    def state_snapshot(self) -> dict[str, Any]:
        with self._lock:
            nodes = {str(node_id): dict(state) for node_id, state in self._states.items()}
            mode = self._mode
        return {"ok": True, "nodes": nodes, "port": self._port, "mode": mode}

    def _set_node(self, node_id: int, value: float) -> None:
        with self._lock:
            self._mode = None
            self._states[node_id] = {"active": True, "value": value}
        self.node_presence_requested.emit(node_id, value)

    def _restore_node(self, node_id: int) -> None:
        with self._lock:
            self._mode = None
            self._states[node_id]["active"] = False
        self.restore_node_requested.emit(node_id)

    def _set_mode(self, mode: str) -> None:
        if mode not in MOBILE_MODE_RANGES:
            raise RequestError("模式无效")
        with self._lock:
            self._mode = mode
        self._emit_mode_values()
        self._ensure_mode_thread()

    def _ensure_mode_thread(self) -> None:
        thread = self._mode_thread
        if thread is not None and thread.is_alive():
            return
        self._mode_thread = threading.Thread(
            target=self._mode_loop,
            name="echoguard-mobile-mode",
            daemon=True,
        )
        self._mode_thread.start()

    def _mode_loop(self) -> None:
        while not self._mode_stop.wait(MOBILE_MODE_INTERVAL_SECONDS):
            self._emit_mode_values()

    def _emit_mode_values(self) -> None:
        with self._lock:
            mode = self._mode
            if mode is None:
                return
            low, high = MOBILE_MODE_RANGES[mode]
            used_values: set[int] = set()
            for node_id in MOBILE_NODE_IDS:
                previous = int(round(float(self._states[node_id]["value"]) * 100))
                value_hundredths = self._random.randint(low, high)
                while value_hundredths in used_values or (
                    low <= previous <= high and value_hundredths == previous
                ):
                    value_hundredths = self._random.randint(low, high)
                used_values.add(value_hundredths)
                value = value_hundredths / 100.0
                self._states[node_id] = {"active": True, "value": value}
                # 在同一锁区间内排队信号，保证随后到来的“恢复/手动”命令不会被旧模式帧反超。
                self.node_presence_requested.emit(node_id, value)

    def _build_handler_class(self) -> type[BaseHTTPRequestHandler]:
        service = self

        class MobileRequestHandler(BaseHTTPRequestHandler):
            server_version = "EchoGuardMobile/0.3.3"

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                path = urlsplit(self.path).path
                if path == "/":
                    self._send_bytes(HTTPStatus.OK, MOBILE_PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/state":
                    self._send_json(HTTPStatus.OK, service.state_snapshot())
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "接口不存在"})

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                path = urlsplit(self.path).path
                try:
                    payload = self._read_json()
                    if path == "/api/node":
                        self._handle_node(payload)
                    elif path == "/api/mode":
                        self._handle_mode(payload)
                    elif path == "/api/restore":
                        service.restore_all()
                        self._send_json(HTTPStatus.OK, service.state_snapshot())
                    else:
                        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "接口不存在"})
                except RequestError as exc:
                    self._send_json(exc.status, {"ok": False, "error": str(exc)})

            def _handle_node(self, payload: dict[str, Any]) -> None:
                raw_node_id = payload.get("node_id")
                if isinstance(raw_node_id, bool):
                    raise RequestError("节点编号无效")
                try:
                    node_id = int(raw_node_id)
                except (TypeError, ValueError) as exc:
                    raise RequestError("节点编号无效") from exc
                if node_id not in MOBILE_NODE_IDS:
                    raise RequestError("仅允许控制 Node 1～3")

                if payload.get("active") is False:
                    service._restore_node(node_id)
                    self._send_json(HTTPStatus.OK, service.state_snapshot())
                    return

                raw_value = payload.get("value")
                if isinstance(raw_value, bool):
                    raise RequestError("存在感知值无效")
                try:
                    value = float(raw_value)
                except (TypeError, ValueError) as exc:
                    raise RequestError("存在感知值无效") from exc
                if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                    raise RequestError("存在感知值必须在 0.00～1.00 之间")
                value = round(value, 2)
                service._set_node(node_id, value)
                self._send_json(HTTPStatus.OK, service.state_snapshot())

            def _handle_mode(self, payload: dict[str, Any]) -> None:
                mode = str(payload.get("mode") or "").strip().lower()
                service._set_mode(mode)
                self._send_json(HTTPStatus.OK, service.state_snapshot())

            def _read_json(self) -> dict[str, Any]:
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length)
                except ValueError as exc:
                    raise RequestError("请求长度无效") from exc
                if length < 0 or length > MAX_REQUEST_BYTES:
                    raise RequestError("请求内容过大", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RequestError("JSON 格式无效") from exc
                if not isinstance(payload, dict):
                    raise RequestError("JSON 必须是对象")
                return payload

            def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._send_bytes(status, body, "application/json; charset=utf-8")

            def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
                self.send_response(int(status))
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return MobileRequestHandler


class RequestError(ValueError):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


class MobileControlPanel(QWidget):
    """独立小窗口：管理手机控制服务并展示局域网访问地址。"""

    node_presence_changed = pyqtSignal(int, float)
    restore_node_requested = pyqtSignal(int)
    restore_all_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._allow_close = False
        self._tray_notice_shown = False
        self._tray: QSystemTrayIcon | None = None
        self.setWindowTitle(f"EchoGuard Mobile {DISPLAY_VERSION}")
        self.setMinimumSize(430, 330)
        self.setWindowFlag(Qt.WindowType.Window)
        self.service = MobileControlService(self)
        self.service.node_presence_requested.connect(self.node_presence_changed.emit)
        self.service.restore_node_requested.connect(self.restore_node_requested.emit)
        self.service.restore_all_requested.connect(self.restore_all_requested.emit)
        self.service.service_state_changed.connect(self._set_service_state)
        self._build_ui()
        self._build_tray()
        self._refresh_network()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(13)

        title = QLabel("手机热点控制")
        title.setObjectName("SectionTitle")
        title.setStyleSheet("font-size: 19px; font-weight: 700;")
        root.addWidget(title)
        subtitle = QLabel("请先在 Windows 中连接手机热点，再启动控制服务")
        subtitle.setObjectName("SectionSub")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {THEME['divider']};")
        root.addWidget(divider)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.addWidget(QLabel("局域网 IP"), 0, 0)
        self.ip_label = QLabel("检测中…")
        self.ip_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(self.ip_label, 0, 1)
        grid.addWidget(QLabel("服务端口"), 1, 0)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(DEFAULT_MOBILE_CONTROL_PORT)
        grid.addWidget(self.port_spin, 1, 1)
        grid.addWidget(QLabel("手机地址"), 2, 0)
        self.url_edit = QLineEdit()
        self.url_edit.setReadOnly(True)
        self.url_edit.setPlaceholderText("启动服务后生成")
        grid.addWidget(self.url_edit, 2, 1)
        root.addLayout(grid)

        self.status_label = QLabel("服务未启动")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {THEME['muted']};")
        root.addWidget(self.status_label)

        buttons = QGridLayout()
        self.start_button = QPushButton("启动服务")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.clicked.connect(self._start_service)
        buttons.addWidget(self.start_button, 0, 0)
        self.stop_button = QPushButton("停止服务")
        self.stop_button.setObjectName("GhostButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.service.stop_service)
        buttons.addWidget(self.stop_button, 0, 1)
        copy_button = QPushButton("复制地址")
        copy_button.setObjectName("GhostButton")
        copy_button.clicked.connect(self._copy_address)
        buttons.addWidget(copy_button, 1, 0)
        restore_button = QPushButton("恢复全部真实数据")
        restore_button.setObjectName("GhostButton")
        restore_button.clicked.connect(self.service.restore_all)
        buttons.addWidget(restore_button, 1, 1)
        refresh_button = QPushButton("刷新网络地址")
        refresh_button.setObjectName("GhostButton")
        refresh_button.clicked.connect(self._refresh_network)
        buttons.addWidget(refresh_button, 2, 0, 1, 2)
        hide_button = QPushButton("隐藏到后台")
        hide_button.setObjectName("GhostButton")
        hide_button.clicked.connect(self._hide_to_background)
        buttons.addWidget(hide_button, 3, 0, 1, 2)
        root.addLayout(buttons)
        root.addStretch(1)

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self.windowIcon(), self)
        tray.setToolTip(f"EchoGuard Mobile {DISPLAY_VERSION}")
        menu = QMenu(self)
        show_action = QAction("显示手机控制窗口", menu)
        show_action.triggered.connect(self._show_from_background)
        menu.addAction(show_action)
        restore_action = QAction("恢复全部真实数据", menu)
        restore_action.triggered.connect(self.service.restore_all)
        menu.addAction(restore_action)
        menu.addSeparator()
        exit_action = QAction("退出 EchoGuard Mobile", menu)
        exit_action.triggered.connect(self._exit_application)
        menu.addAction(exit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _refresh_network(self) -> None:
        address = discover_lan_ipv4()
        self.ip_label.setText(address)
        if address == "127.0.0.1":
            self.status_label.setText("未检测到可供手机访问的局域网 IP，请检查手机热点连接")
        elif not self.service.running:
            self.status_label.setText("网络地址已就绪，点击“启动服务”")

    def _start_service(self) -> None:
        self.service.start_service(self.port_spin.value())

    def _set_service_state(self, running: bool, url: str, message: str) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.port_spin.setEnabled(not running)
        self.url_edit.setText(url)
        self.ip_label.setText(self.service.address)
        color = THEME["cyan"] if running else THEME["muted"]
        self.status_label.setStyleSheet(f"color: {color};")
        self.status_label.setText(message)

    def _copy_address(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            self.status_label.setText("请先启动手机控制服务")
            return
        QApplication.clipboard().setText(url)
        self.status_label.setText("手机访问地址已复制")

    def _hide_to_background(self) -> None:
        tray = self._tray
        if tray is None:
            self.status_label.setText("当前系统托盘不可用，无法隐藏到后台")
            return
        self.hide()
        if not self._tray_notice_shown:
            tray.showMessage(
                "EchoGuard Mobile",
                "手机控制窗口已隐藏，服务和随机模式将在后台继续运行。",
                QSystemTrayIcon.MessageIcon.Information,
                3500,
            )
            self._tray_notice_shown = True

    def _show_from_background(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_from_background()

    def _exit_application(self) -> None:
        self._allow_close = True
        self.shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def shutdown(self) -> None:
        self.service.shutdown()
        if self._tray is not None:
            self._tray.hide()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if not self._allow_close and self._tray is not None:
            self._hide_to_background()
            event.ignore()
            return
        self.shutdown()
        event.accept()
