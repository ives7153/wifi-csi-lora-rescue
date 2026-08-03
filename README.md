# EchoGuard

[![ESP-IDF](https://img.shields.io/badge/ESP--IDF-v5.2.6-e7352c?logo=espressif)](https://docs.espressif.com/projects/esp-idf/)
[![Python](https://img.shields.io/badge/Python-3.11+-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![PyQt6](https://img.shields.io/badge/UI-PyQt6-41cd52?logo=qt&logoColor=white)](https://www.riverbankcomputing.com/software/pyqt/)
[![Platform](https://img.shields.io/badge/Platform-ESP32--S3%20%7C%20Windows-lightgrey)](#)

基于 ESP32-S3、WiFi CSI 与 LoRa 的感传分离救援原型系统。EchoGuard 面向地震、坍塌、废墟遮挡等应急场景，用低成本节点采集 WiFi CSI 生命微动特征，经 LoRa 回传到 Gateway，再由 Windows 上位机完成可视化、规则融合和 AI 辅助研判。

![EchoGuard social preview](docs/assets/EchoGuard-social-preview.png)

## 项目简介

EchoGuard 由三部分组成：

- **Rescue Node 感知节点**：ESP32-S3 采集 WiFi CSI、AHT20 温湿度、姿态和 MQ-135 气体原始值，并通过 Ra-02/SX1278 LoRa 模块上报。
- **Gateway 汇聚网关**：ESP32-S3 接收 LoRa 帧，将节点数据转换为 JSON Lines，经 UART0/USB-UART 或 USB Serial/JTAG 输出给上位机。
- **EchoGuard 上位机**：PyQt6 桌面程序，负责串口接收、节点自动发现、实时曲线、事件流、历史导出、规则报警、AI 辅助解释和现场问答。

本项目强调“真实数据链路优先”：未收到 Gateway 串口帧前，上位机不生成假节点、不伪造历史样本；实时结论由规则融合输出，AI 只做异步辅助解释。

## 核心能力

- WiFi CSI 生命微动感知：基于 ESP32-S3 WiFi CSI 滑动窗口提取幅度扰动特征。
- 三角形区域检测：Node 1/2/3 与 GW-02 组成 9 条定向 CSI 链路，经现场标定后输出独立的“区域内有人/无人/数据不足”状态；不覆盖原节点 presence。
- LoRa 远距离回传：节点与 Gateway 使用 433 MHz、BW125、SF7、CR4/5 的 SX1278 链路。
- 多节点自动发现：上位机根据 Gateway JSON 中的 `id` 自动创建 `node{id}`。
- 存在感知硬阈值：节点 presence 低于用户设置值判为无人，达到或高于设置值判为疑似微动；confidence 与 CSI quality 仅用于诊断展示。
- 多节点综合研判：最近 5 秒窗口内按统一存在阈值统计触发节点，并保留 motion、confidence 和 RSSI 作为辅助上下文。
- 现场安全提示：LoRa 天线、电源共地、MQ-135 分压、I2C 上拉等硬件注意事项文档化。
- MQ-135 CO2 估算 ppm：上位机将节点上报的 ADC 原始值按分压、电阻和 R0 标定参数换算为估算 ppm，支持按节点 R0 校准和全部在线节点校准。
- 数据导出能力：支持 CSV 导出、融合扰动曲线截图和整窗截图，导出/截图结果通过右上角 toast 弹窗提示。
- AI 辅助研判与问答：仪表盘显示短摘要，`AI 辅助` 页提供结构化研判、右侧上下文栏和 Markdown 对话；本地 Jina GGUF embedding 用于向量检索，可选大模型 API 用于联网生成回答，AI 不接管实时判断。
- 数据录制回放：保留原始 Gateway JSON Lines 和接收时间，回放数据明确标记来源，不与实时数据混淆。
- 双发行版：普通监测版与无密码手机演示控制版共用 v0.5.0 核心代码、真实区域检测器和 PyInstaller 打包流程；手机演示值与区域判定严格隔离。

## 系统架构

```text
Rescue Node(s)
  ESP32-S3 + WiFi CSI + AHT20/MPU6050/MQ-135
        |
        | 14-byte LoRa binary frame
        v
Gateway
  ESP32-S3 + SX1278 LoRa receiver
        |
        | UART0/USB-UART or USB Serial/JTAG JSON Lines @ 115200
        v
EchoGuard Upper Computer
  PyQt6 UI + parser + rule fusion + export + AI helper/chat

Triangle region path (GW-02 only)
  GW-02 UDP sync -> 3 Nodes ESP-NOW time-slot probes
  9 directed CSI links -> UDP feature frames -> Gateway JSON Lines
  -> robust calibration + weighted k-NN -> global region state
```

## 数据协议

节点通过 LoRa 发送 14 字节二进制帧：

```text
id:u8
seq:u32
presence:u8
motion:u8
bpm:u8
conf:u8
gas:u16
temp_x10:i16
hum:u8
```

Gateway 输出一行 JSON：

```json
{
  "id": 1,
  "seq": 0,
  "presence": 0,
  "motion": 0,
  "bpm": 0,
  "conf": 0,
  "gas": 0,
  "temp": 25.0,
  "hum": 50,
  "rssi": -80,
  "ts": 12345
}
```

Gateway 每 10 秒还会输出 `type=gateway_status` 状态行，包含固件版本、SSID、LoRa 正确接收数、CRC 错误数、异常长度、队列丢包和 Wi-Fi 客户端数量。状态行仅用于技术诊断，不会创建节点。

区域模式下，Node 1/2/3 每 500 ms 向 GW-02 的 UDP 33334 端口上报 3 条接收链路特征。Gateway 校验 `EGCF` 魔数、协议版本、固定长度和 CRC32 后，额外输出 `type=csi_features` JSON Lines。该数据流不修改原 14 字节 LoRa 协议，也不会作为普通节点历史样本。

上位机将字段规范化为 `node_id`、`presence_score`、`motion_score`、`confidence`、`gas_raw`、`gas_ppm`、`temperature`、`humidity`、`rssi` 等内部字段，其中 `gas` 兼容字段等同于 CO2 估算 ppm。详见 [docs/interface_alignment.md](docs/interface_alignment.md)。

## 目录结构

```text
wifi-csi-lora-rescue/
|-- docs/                    # 接口、AI、本地部署、打包说明
|-- firmware/
|   |-- gateway/              # Gateway LoRa 接收与 JSON 串口转发固件
|   +-- node/                 # Rescue Node WiFi CSI、传感器与 LoRa 上报固件
|-- hardware/                 # 接线、自检、硬件风险与接线表
|-- scripts/                  # 上位机打包与辅助脚本
|-- tests/                    # 测试与联调记录目录
|-- upper_computer/           # PyQt6 上位机源码
|-- EchoGuard.spec            # PyInstaller 打包配置
|-- partitions-8Mib.csv       # Node/GW-02 单应用分区表
|-- partitions-ota-8Mib.csv   # GW-01 双槽 OTA 分区表
+-- README.md
```

## 硬件准备

最小演示需要：

- ESP32-S3-DevKitC-1 N8R8 开发板至少 2 块：1 块 Gateway，1 块 Rescue Node。
- Ra-02/SX1278 LoRa 模块每块板 1 个。
- 433 MHz 匹配天线，上电和发射前必须安装。
- 节点侧可接 AHT20、MPU6050、MQ-135。
- USB 数据线、杜邦线、面包板或焊接底板、稳定 5V 电源。

硬件接线请先阅读：

- [hardware/readme.md](hardware/readme.md)
- [hardware/接线表.md](hardware/%E6%8E%A5%E7%BA%BF%E8%A1%A8.md)

## 固件构建

本项目固件使用 ESP-IDF v5.2.6，目标芯片为 ESP32-S3。

Gateway 默认启动 `EchoGuard-GW-01` WPA2-PSK SoftAP，也可在
`menuconfig -> EchoGuard Gateway Configuration -> Gateway SoftAP SSID`
中构建为 `EchoGuard-GW-02`。两个网关使用相同密码 `511511511`。Rescue Node
会自动扫描两个 SSID，优先连接 GW-01；GW-01 不可用或连续连接失败时回退到 GW-02。

确认环境：

```powershell
idf.py --version
```

构建并烧录 Gateway：

```powershell
cd firmware\gateway
idf.py set-target esp32s3
idf.py build
idf.py -p COMx flash monitor
```

构建并烧录 Rescue Node：

```powershell
cd firmware\node
idf.py set-target esp32s3
idf.py menuconfig
idf.py build
idf.py -p COMx flash monitor
```

每个实体节点烧录前，需要在 `menuconfig -> Rescue Node Configuration -> Rescue node ID` 中设置唯一编号，例如 `1 / 2 / 3 / 4`。Gateway 串口输出中的 `id` 会直接作为上位机节点编号。

Gateway 默认以 UART0（115200）作为主控制台，并将输出同步到 ESP32-S3 USB Serial/JTAG；因此带 CH340/CP210x 的自制板和带原生 USB 接口的开发板都可向上位机输出同一套 JSON Lines。

### Gateway 01 局域网 OTA

Gateway 01 的 OTA 只通过未跟踪的 `firmware/gateway/sdkconfig.gw01.local` 启用。该本地配置使用 `partitions-ota-8Mib.csv`、开启 Bootloader 回滚并保存独立 OTA 令牌；Node、GW-02 和默认 `partitions-8Mib.csv` 不启用 OTA。

OTA 令牌不会进入 Git 跟踪的源码或公开配置，但会作为认证常量存在于本地编译出的 Gateway 应用镜像中。包含该镜像的首次安装包和 OTA 固件只能私下分发，不得上传为公开 GitHub Release 资产。

使用本机 ESP-IDF 环境构建 GW-01 OTA 固件：

```powershell
cd firmware\gateway
$env:PYTHONUTF8="1"
idf.py -D SDKCONFIG=sdkconfig.gw01.local -B build-gw01-ota-v050 build
```

旧版单 `factory` 分区不能直接接收 OTA。首次安装必须通过 USB/串口完整写入 Bootloader、OTA 分区表、初始 OTA 数据和应用镜像；此后电脑连接 `EchoGuard-GW-01`，在上位机“技术诊断 -> Gateway 01 OTA”中选择新的 Gateway 应用 `.bin` 即可升级。上传会校验 Gateway 工程、ESP32-S3 目标、递增版本、SHA-256 和本地令牌；新固件只有在 SoftAP、HTTP OTA 服务和 LoRa 均就绪后才取消回滚。

## 三角形区域检测部署与标定

区域检测固定使用 `EchoGuard-GW-02`；运行时关闭 GW-01，确保三个节点均连接 GW-02。推荐部署：

- Node 1/2/3 构成边长约 2 m 的三角形，安装高度 0.8–1.0 m，天线方向和位置固定。
- GW-02 放在三角形外，距各节点约 2–4 m，作为专用 AP 持续发包。
- 当前模型面向单人走动，不承诺检测完全静止人员。
- 三角形边线内外各 30 cm 作为不计验收指标的过渡带。

连接 GW-02 到上位机后，在仪表盘点击“区域标定”，按顺序完成空场 60 秒、内部走动 180 秒、外部走动 180 秒。上位机使用稳健中位数/MAD 标准化、Fisher 特征选择和加权 k-NN（k=7）训练现场配置，并绑定 GW-02 与三个 Node STA MAC。配置默认保存到 `%APPDATA%\EchoGuard\triangle_calibration_gw02.json`。

标定交叉验证目标为内部检出率不低于 95%、空场和外部合计误报率不高于 5%；未达到目标时不会启用该配置。运行时连续 2 个窗口确认进入，连续 3 个窗口确认清除；链路缺失、设备 MAC 不匹配或持续离群时显示“数据不足/需要重新标定”，而不是猜测有人或无人。

## 上位机运行

推荐在仓库根目录运行：

```powershell
python -m pip install -r upper_computer\requirements.txt
python -m upper_computer.main
```

也可以进入目录后运行：

```powershell
cd upper_computer
python main.py
```

上位机启动后会自动刷新串口列表。连接 Gateway 后，收到有效 JSON Lines 时会自动发现节点并刷新仪表盘、节点管理、数据分析、历史记录和技术诊断页面。

普通监测版按演示要求处理每个节点的首次连接：前 20 秒存在感知值在 `0.01～0.09` 之间变化，不显示倒计时；20 秒后自动恢复该节点的真实存在值。Mobile 版不启用此启动演示，只在手机明确下发控制命令时覆盖存在值。

左侧提供原始会话录制与回放入口。历史记录和 CSV 使用 `serial_real`、`mobile_override`、`demo_mode`、`replay` 区分真实串口、手机覆盖、本地演示和文件回放。

## 打包 Windows 程序

安装打包依赖：

```powershell
python -m pip install -r requirements-build.txt
```

执行打包：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_upper_computer.ps1
```

同时构建普通版和 Mobile 版并生成 SHA-256：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package_releases.ps1
```

打包产物：

```text
dist/
+-- EchoGuard-v0.5.0-windows/
|   +-- EchoGuard.exe
+-- EchoGuard-Mobile-v0.5.0-windows/
    +-- EchoGuard.exe
```

详细说明见 [docs/upper_computer_packaging.md](docs/upper_computer_packaging.md)。

## AI 辅助

AI 模块默认以规则回退为主，不影响实时判断。v0.3.1 起，上位机提供三层 AI 参与方式：

- 仪表盘短摘要：综合研判卡片只显示稳定、简短的 AI 辅助摘要。
- AI 辅助页：展示依据、风险、趋势、建议、节点贡献和最近 AI 研判历史。
- AI 对话工作台：支持现场问答，回答区采用 Markdown 显示，右侧上下文栏常驻并可滚动。

可选能力包括：

- 本地 Jina GGUF embedding：用于对规则融合摘要和本地知识片段做向量检索，不作为生成式聊天模型。
- 可选大模型 API：用于联网生成更自然的解释、建议和问答回复。
- 规则回退：无 Jina、无 API 或网络不可用时，仍使用本地规则模板生成谨慎建议。
- 离线包导入：适合比赛、答辩和无网现场。

模型和 llama.cpp runtime 不提交到仓库，默认放在：

```text
upper_computer/models/
upper_computer/runtime/
```

这两个目录已由 `.gitignore` 排除。部署说明见 [docs/local_jina_deployment.md](docs/local_jina_deployment.md)。

AI 只解释“疑似、风险、依据、建议”，不会确认生命存在，不会修改报警规则，也不会上传原始 CSI 曲线或改变 Gateway/Node 协议。

## 文档索引

- [hardware/readme.md](hardware/readme.md)：硬件接线、自检、电源与天线布局。
- [hardware/接线表.md](hardware/%E6%8E%A5%E7%BA%BF%E8%A1%A8.md)：Gateway 与 Rescue Node 接线表。
- [docs/interface_alignment.md](docs/interface_alignment.md)：固件、Gateway 与上位机协议对齐。
- [docs/ai_auxiliary_judgement.md](docs/ai_auxiliary_judgement.md)：AI 辅助研判边界与异步链路。
- [docs/ai_auxiliary_feature.md](docs/ai_auxiliary_feature.md)：AI 功能构造、原理和回退策略。
- [docs/local_jina_deployment.md](docs/local_jina_deployment.md)：本地 Jina GGUF 离线部署。
- [docs/upper_computer_packaging.md](docs/upper_computer_packaging.md)：Windows 打包流程。

## 项目阶段

- Phase 0：项目初始化与需求拆解，明确救援场景、系统边界、目录结构和原型目标。
- Phase 1：硬件接线与上电自检，完成 ESP32-S3、LoRa 与传感器基础连通。
- Phase 2：感知节点固件开发，完成 WiFi CSI 采集、传感器采集与 LoRa 上报链路。
- Phase 3：Gateway 固件开发，完成 LoRa 接收、数据汇聚与 USB 串口转发。
- Phase 4：上位机开发，完成串口接收、协议解析、数据可视化、规则判断与 AI 模块接入。
- Phase 5：系统联调与演示封装，完成遮挡场景验证、问题闭环、文档整理和展示材料准备。

## 注意事项

- Ra-02/SX1278 只能接 3.3V，禁止接 5V。
- LoRa 天线必须在上电和发射前安装。
- 所有模块必须共地。
- MQ-135 的 AO 进入 ESP32-S3 ADC 前必须确认不超过 3.3V。
- 当前 `gas` 在 Gateway JSON 中仍是 MQ-135 ADC 原始值；上位机显示的 `gas_ppm` / 兼容 `gas` 为 CO2 估算 ppm，需通过清洁空气校准提升可信度。
- 当前节点固件不上传电池电量，上位机显示为“未上报”。
- `upper_computer/models/`、`upper_computer/runtime/` 和 `upper_computer/exports/` 不应提交到 GitHub。
- `dist/` 与 `releases/` 为本地打包 / 发布产物目录，不应提交到 GitHub；正式分发优先使用 GitHub Releases 上传 `EchoGuard-vX.Y.Z-windows.zip`。
