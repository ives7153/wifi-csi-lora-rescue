# 本地 Jina 一键部署说明

本文档说明 EchoGuard 上位机中本地 Jina embedding 的在线部署、离线包导入和离线包生成方式。该能力用于 AI 辅助研判链路中的“本地向量化 + 模式相似度匹配”，也用于 AI 对话页的本地知识片段检索，不负责实时主判断，也不是生成式聊天模型。

## 设计原则

- 实时结论仍由上位机规则融合输出，AI 只做异步辅助解释。
- AI 对话页会使用 Jina 检索相关本地知识片段；联网大模型不可用时，上位机会结合检索结果和规则模板生成离线回答。
- 有网环境可使用在线部署；比赛现场优先使用提前生成的离线包，避免临场下载 Hugging Face 模型或运行时失败。
- GitHub 仓库不提交 GGUF 模型和 `llama-server.exe`，只提交部署逻辑、UI 和文档。
- 本地 Jina 服务使用 llama.cpp `llama-server` 的 OpenAI 兼容 `/v1/embeddings` 接口。

## 离线包结构

`导入离线包` 按钮要求选择 `EchoGuard-AI-Runtime.zip`，压缩包内至少包含以下两个文件：

```text
EchoGuard-AI-Runtime.zip
├── runtime/
│   ├── llama-server.exe
│   ├── *.dll
│   └── 其他 llama.cpp Windows 运行时文件
└── models/
    └── v5-nano-retrieval-Q4_K_M.gguf
```

压缩包外层可以再套一个目录，例如 `EchoGuard-AI-Runtime/runtime/llama-server.exe`，上位机会按后缀自动查找关键文件。`runtime/` 目录不要只放 `llama-server.exe`，Windows 版 llama.cpp 通常还需要同目录的 DLL/运行库。

## 默认部署位置

在线部署或导入离线包都会把两个关键文件部署到当前上位机配置路径，默认位置为：

```text
upper_computer/runtime/llama-server.exe
upper_computer/models/v5-nano-retrieval-Q4_K_M.gguf
```

这些目录已在 `.gitignore` 中排除，避免误提交大文件。

## 使用流程

### 在线部署

1. 打开上位机，进入仪表盘“综合研判结果”卡片中的 `AI设置`。
2. 在“本地 Jina embedding”区域点击 `在线部署`。
3. 上位机会从 llama.cpp GitHub Release 获取 Windows CPU x64 完整运行时，并从 Hugging Face 获取 Jina Q4 GGUF 模型。
4. 看到 `部署状态：已部署` 后，点击 `一键启动`。
5. 上位机会启动 `llama-server`，并轮询真实 embedding 请求。
6. 成功时显示类似：

```text
本地 Jina 可用：POST http://127.0.0.1:18081/v1/embeddings · 768 维
```

### 离线导入

1. 提前准备 `EchoGuard-AI-Runtime.zip`。
2. 点击 `导入离线包`，选择该 zip。
3. 看到 `部署状态：已部署` 后点击 `一键启动`。

### 生成离线包

1. 在有网电脑上先完成 `在线部署`。
2. 点击 `生成离线包`，选择保存位置。
3. 把生成的 `EchoGuard-AI-Runtime.zip` 拷贝到比赛现场电脑。
4. 现场电脑使用 `导入离线包` 完成部署。

## 按钮说明

- `在线部署`：从官方源下载 llama.cpp Windows CPU x64 运行时和 Jina GGUF 模型，并更新本机 AI 配置。
- `导入离线包`：选择离线 zip 包，解压运行时和 GGUF 模型，并更新本机 AI 配置。
- `导入 GGUF`：在 Hugging Face 下载不稳定时，手动选择已经下载好的 `.gguf` 模型文件并复制到配置路径。
- `生成离线包`：把当前已部署的运行时和模型打包为 `EchoGuard-AI-Runtime.zip`。
- `一键启动`：启动本地 `llama-server`，并立即发起一次真实 embedding 测试，成功后才显示可用。
- `启动本地 Jina`：只按当前配置启动服务，并等待 embedding 就绪。
- `停止服务`：停止由上位机启动的 `llama-server` 进程。
- `测试 Embedding`：对当前服务地址发起真实 `/v1/embeddings` 请求，显示返回向量维度。

## 常见错误

- `下载失败`：网络不可用、GitHub/Hugging Face 不可达，或代理配置异常。
- `未在 llama.cpp 最新 release 中找到 Windows CPU x64 运行时`：GitHub release 资产命名变化，可临时手动下载后使用离线包导入。
- `未找到离线包`：没有选择 zip 文件，或路径已失效。
- `离线包结构不完整`：zip 内缺少 `runtime/llama-server.exe` 或 `models/v5-nano-retrieval-Q4_K_M.gguf`。
- `llama.cpp runtime 不完整`：只有极小的 `llama-server.exe`，或缺少同目录 DLL/运行库，请重新在线部署或导入完整离线包。
- `未找到 Jina GGUF 模型`：配置路径指向的模型不存在，请重新在线部署、导入离线包或点击 `导入 GGUF`。
- `本地 Jina 服务未就绪`：服务启动了但 `/v1/embeddings` 没有在超时时间内返回有效向量。
- `响应不是 JSON`：服务地址可能不是 llama-server 的 OpenAI 兼容接口，或端口被其他程序占用。

## 打包建议

后续做安装包时，可以把 `EchoGuard-AI-Runtime.zip` 作为安装资源随包提供。首次运行时由用户在 AI 设置中执行离线导入，或由安装器预解压到同样的默认目录。

不要把 `upper_computer/models/`、`upper_computer/runtime/`、`*.gguf` 提交到 GitHub。
