# EchoGuard 上位机打包说明

本文档说明如何把当前 PyQt 上位机打包为 Windows 可运行目录。v0.4.0 提供普通监测版和无密码手机演示控制版，两者共用数据、报警、AI、录制回放与导出核心。打包目标不包含 Jina GGUF 模型和 `llama-server.exe`。

## 打包前检查

在仓库根目录执行：

```powershell
python -m pip install -r requirements-build.txt
python -m compileall upper_computer
python -m unittest discover -s tests -p "test_*.py" -v
```

上位机正式入口为：

```powershell
python -m upper_computer.main
```

## 执行打包

推荐使用脚本：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_upper_computer.ps1
powershell -ExecutionPolicy Bypass -File scripts\build_upper_computer_mobile_v033.ps1
```

也可以直接执行：

```powershell
python -m PyInstaller --clean --noconfirm EchoGuard.spec
```

产物位置：

```text
dist/
├── EchoGuard-v0.4.0-windows/
│   └── EchoGuard.exe
└── EchoGuard-Mobile-v0.4.0-windows/
    └── EchoGuard.exe
```

## 资源与排除项

打包配置 `EchoGuard.spec` 会把以下资源放入程序：

- `upper_computer/assets/app_icon.ico`
- `upper_computer/assets/app_icon.png`

以下内容不进入主程序包：

- `upper_computer/models/`
- `upper_computer/runtime/`
- `upper_computer/exports/`
- `*.gguf`
- 旧 DearPyGui 可视化路径

## AI 本地模型

本地 Jina embedding 仍采用外置部署：

- 有网环境：在 AI 设置中点击 `在线部署`。
- 无网环境：提前准备 `EchoGuard-AI-Runtime.zip`，在 AI 设置中点击 `导入离线包`。
- 现场离线包结构和常见错误见 `docs/local_jina_deployment.md`。

不要把 GGUF 模型、`llama-server.exe` 或 `EchoGuard-AI-Runtime.zip` 提交到 GitHub。

Jina 只作为本地 embedding/向量检索服务使用；AI 对话的自然语言生成来自可选大模型 API 或规则模板回退。

## 打包后验收

启动两个发行目录中的 `EchoGuard.exe` 后检查：

- 窗口标题、任务栏名称和图标均为 `EchoGuard`。
- 侧边导航图标和太阳/月亮主题按钮正常。
- Gateway 串口可刷新、连接、显示最新帧。
- 节点收到数据后自动出现在仪表盘、节点管理、分析、历史和诊断页。
- 环境状态中显示 `CO2 估算 ppm`，传感器页可执行当前节点校准和全部在线节点校准。
- CSV 导出、历史记录导出、融合扰动曲线截图、整窗截图可写入 `upper_computer/exports/`，成功后右上角出现自动消失的 toast 提示。
- `AI 辅助` 导航页可打开，右侧上下文栏可滚动，中间对话区支持 Markdown 显示并在发送后自动滚动到底部。
- 无本地 Jina 或 API 时，AI 区域保持规则回退，AI 对话可用本地模板回答，不影响主界面运行。
- 普通版不启动手机 HTTP 服务；Mobile 版可隐藏到系统托盘，并在覆盖存在数值时显示醒目的“手机演示数据”标识。
- 普通版对每个新连接节点执行 20 秒启动演示，存在值在 `0.01～0.09` 间变化且不显示倒计时，随后恢复真实值；Mobile 版不执行该启动演示。
- “开始录制”生成 JSONL 文件；断开串口后可选择该文件回放，历史和 CSV 来源显示为“文件回放”。

## Release 打包建议

执行以下脚本可同时构建两个版本、生成 ZIP 和 SHA-256：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package_releases.ps1
```

正式分发包命名为：

```text
EchoGuard-v0.4.0-windows.zip
EchoGuard-Mobile-v0.4.0-windows.zip
SHA256SUMS.txt
```

zip 不包含 `upper_computer/models/`、`upper_computer/runtime/`、`upper_computer/exports/` 或 GGUF 模型文件。
