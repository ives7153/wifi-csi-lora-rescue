# 上位机与固件接口一致性说明

本文档说明当前 Gateway、节点固件和上位机之间的数据接口关系，避免上位机展示内容脱离真实固件输出。

## 总体原则

- 上位机只消费 Gateway 串口输出的真实 JSON Lines 数据。
- 未收到真实节点帧之前，上位机不预置假节点、不生成模拟曲线、不伪造历史样本。
- 节点是否存在，以 Gateway 收到该节点 LoRa 帧并通过 USB 串口输出有效 JSON 为准。
- 实时主判断由多节点规则融合完成，AI 只做异步辅助解释，不接管实时判断。
- Mobile 演示覆盖和文件回放必须携带明确数据来源，不伪装成实时 Gateway 数据。
- 三角形区域检测只接受 GW-02 实时 `csi_features` 帧；Mobile 覆盖、启动演示和录制回放均不得驱动区域状态。

## 固件到上位机的数据链路

节点固件每秒读取 WiFi CSI、AHT20、MPU6050、MQ-135 等本地信息，并通过 LoRa 发送 14 字节二进制帧：

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

Gateway 固件接收 LoRa 帧后，补充 Gateway 侧 LoRa RSSI 与本机时间戳，并通过 UART0/USB-UART 或 USB Serial/JTAG 输出一行 JSON：

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

Gateway 每 10 秒额外输出一条状态 JSON，用于链路诊断，不会创建节点：

```json
{
  "type": "gateway_status",
  "protocol": 1,
  "firmware": "v0.5.1",
  "gateway_id": "GW-01",
  "ssid": "EchoGuard-GW-01",
  "uptime_ms": 10000,
  "rx_ok": 12,
  "crc_errors": 0,
  "bad_length": 0,
  "parse_errors": 0,
  "queue_drops": 0,
  "queue_depth": 0,
  "wifi_clients": 3
}
```

GW-02 区域模式还会转发独立的 CSI 特征 JSON。每个 Node 帧包含本机 STA MAC 和 3 条接收链路；三节点合计覆盖 3 条 Gateway→Node 链路与 6 条有方向的 Node→Node 链路：

```json
{
  "type": "csi_features",
  "v": 1,
  "gateway_id": "GW-02",
  "node": 1,
  "node_mac": "02:00:00:00:00:01",
  "seq": 7,
  "flags": 3,
  "links": [
    {"src": 0, "valid": true, "n": 16, "rssi": -55, "rssi_std": 4,
     "active": 20, "corr": 3, "mad": [1,2,3,4,5,6,7,8],
     "diff": [1,2,3,4,5,6,7,8]}
  ]
}
```

`data_parser.py` 将短字段规范化为 `source_id/sample_count/active_ratio/correlation_delta/mad_bands/diff_bands`。`DataManager` 将该帧直接送入 `TriangleRegionDetector`，不创建普通节点、不写入节点历史，也不执行原 presence 报警规则。

上位机 `data_parser.py` 将这些字段规范化为内部字段，例如：

- `id -> node_id`
- `presence -> presence_score`，归一化到 `0.0-1.0`
- `motion -> motion_score`，归一化到 `0.0-1.0`
- `conf -> confidence`，归一化到 `0.0-1.0`
- `gas -> gas`
- `temp -> temperature`
- `hum -> humidity`
- `rssi -> rssi`
- `ts -> source_ts_ms`

历史和 CSV 的 `source` 使用稳定值：

- `serial_real`：真实 Gateway 串口数据。
- `mobile_override`：手机端覆盖存在感知值。
- `demo_mode`：本地预设演示值。
- `replay`：录制文件回放。

上位机录制文件保留每一条原始串口行和本机接收时间，包括无法解析的启动日志；回放时异常行会被计数并跳过。

## 节点 ID 一致性

节点 ID 是多节点交叉验证的基础。节点固件不再固定写死为 `1`，而是使用：

```c
CONFIG_RESCUE_NODE_ID
```

烧录每个实体节点前，需要进入：

```text
idf.py menuconfig
Rescue Node Configuration -> Rescue node ID
```

为每个节点设置唯一编号，例如 `1 / 2 / 3 / 4`。

上位机收到 `id=3` 时显示为 `node3`；如果收到未来固件上报的 `name / label / node_name` 字段，则优先显示固件提供的名称。

## 上位机自动发现节点

上位机启动时不再预置 `node1-node4`。节点管理、仪表盘关注节点、数据分析、技术诊断和历史记录都以真实发现节点为准。

行为如下：

- 无有效串口帧时，节点管理显示等待 Gateway 节点接入。
- 收到 `id=1` 后，自动出现 `node1`。
- 收到 `id=9` 后，也允许显示为 `node9`，不因为超出默认 1-4 范围而丢弃。
- 节点离线后仍保留在列表中，但状态变为离线。

## 电池字段处理

当前节点固件没有上报电池电量，因此上位机不能显示或导出伪造百分比。

当前约定：

- UI 显示 `未上报`。
- 内部字段 `battery` 为 `None`。
- CSV 导出仍保留 `battery` 列以兼容未来协议，但无真实上报时该列为空。

未来如果固件增加电池字段，应先在 Gateway JSON 中明确字段名，再由上位机解析并展示。

## 气体字段处理

Gateway JSON 中的 `gas` 仍保持固件协议语义：MQ-135 ADC 原始值。上位机在解析层增加 CO2 估算 ppm 标定，内部字段约定如下：

- `gas_raw`：固件上报的 MQ-135 ADC 原始值。
- `gas_ppm`：上位机按 ADC 满量程、外部分压、MQ-135 负载电阻、R0 与 CO2 曲线估算出的 ppm。
- `gas`：兼容旧代码的字段，当前等同于 `gas_ppm`。

上位机默认按 `VCC=5.0V`、`RL=10kΩ`、外部分压 `R3=10kΩ / R4=20kΩ`、CO2 曲线 `ppm = A * (Rs/R0)^B` 估算。R0 取值优先级为：

1. 当前节点专属 `mq135_node_r0_kohm`
2. 旧全局 `mq135_r0_kohm` 兼容值
3. 程序默认 R0

传感器页提供“校准当前节点”和“校准全部在线节点”。校准时使用对应节点最新真实 `gas_raw`，按 400 ppm 清洁空气反推该节点 R0；全部在线节点校准只处理在线且有有效 `gas_raw` 的节点。

该 ppm 是 MQ-135 估算值，用于救援态势辅助展示和本地规则报警，不应作为计量级气体检测结论。

## 多节点综合研判

综合研判读取最近 5 秒内所有已发现节点的真实样本：

- 无参与节点：`等待数据`
- 单节点参与：`数据不足` 或 `疑似局部微动`
- 多节点均未触发：`未检测到稳定微动`
- 两个及以上节点的最新 presence 达到用户设置的硬阈值：`多节点疑似生命微动`

存在感知采用单一硬阈值：`presence < threshold` 判为无人，`presence >= threshold` 判为疑似微动。confidence 和可选 CSI quality 只保留为诊断与辅助解释字段，不否决存在判定。

当前 UI 中的运动、存在、置信度卡片只表示“当前关注节点观测”，不代表最终系统结论。最终结论以综合研判卡片为准。

## 验收要点

- 烧录多个节点前，每个节点必须配置唯一 `CONFIG_RESCUE_NODE_ID`。
- 上位机无串口数据时不应显示假节点或假历史。
- 连接 Gateway 并收到节点帧后，节点应自动出现在节点管理、仪表盘、分析、历史和诊断页。
- 电池应显示 `未上报`，CSV 电池列应为空。
- 气体相关 UI 应显示 CO2 估算 ppm，并保留 `gas_raw` 用于诊断；校准 node1 不应改变 node2 的节点专属 R0。
- 注入或接入两个 presence 达到用户阈值的节点后，综合研判应显示 `多节点疑似生命微动`。
- Gateway 状态帧不得创建 `node0`，诊断报告应显示 LoRa CRC、异常长度和队列丢包计数。
- 手机覆盖、演示和回放数据必须在历史记录与 CSV 中显示正确来源。
