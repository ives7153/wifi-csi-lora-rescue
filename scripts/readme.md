# 脚本目录

本目录用于存放构建、烧录、串口采集和数据回放等辅助脚本。

## 当前脚本

- `build_upper_computer.ps1`：构建普通监测版，版本和目录名读取 `upper_computer/version.py`。
- `build_upper_computer_mobile_v033.ps1`：构建无密码手机演示控制版，共用同一核心版本。
- `package_releases.ps1`：运行全部测试、构建两个发行版、生成 ZIP 和 `SHA256SUMS.txt`。

v0.3.3 正式产物：

```text
dist/EchoGuard-v0.3.3-windows/
dist/EchoGuard-Mobile-v0.3.3-windows/
releases/EchoGuard-v0.3.3-windows.zip
releases/EchoGuard-Mobile-v0.3.3-windows.zip
```

## 使用建议

- 打包前先运行 `python -m compileall upper_computer` 和 `python -m unittest discover -s tests -v`。
- 固件烧录或临时串口脚本如只用于本机调试，应避免提交到仓库。
- 生成的模型、运行时、导出文件和临时构建产物应按 `.gitignore` 规则排除。
