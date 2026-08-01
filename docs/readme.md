# 文档目录

本目录用于存放 EchoGuard 的接口、AI、打包和本地部署说明。

## 文档索引

- `interface_alignment.md`：说明 Node、Gateway 和上位机之间的协议边界，包含 LoRa 14-byte 帧、Gateway JSON、节点自动发现、电池字段、MQ-135 CO2 估算和多节点规则融合。
- `ai_auxiliary_judgement.md`：说明 AI 辅助的异步边界，强调实时主判断由规则融合完成，AI 只做解释、复核和现场问答。
- `ai_auxiliary_feature.md`：说明 AI 辅助页、结构化研判、Markdown 对话、本地 Jina 检索、大模型 API 和规则回退的完整链路。
- `local_jina_deployment.md`：说明本地 Jina GGUF embedding 的在线部署、离线包导入和离线包生成流程。
- `upper_computer_packaging.md`：说明 PyInstaller 打包、验收和 Release zip 建议。

## 维护原则

- 固件协议或 Gateway JSON 变化时，优先更新 `interface_alignment.md`。
- AI 页面、提示词、Jina 检索或大模型回退变化时，优先更新两个 AI 文档。
- 发布新的 Windows 上位机版本时，同步检查 README 和打包文档中的版本、验收项和 Release 命名。
