# 测试目录

本目录用于存放固件单元测试、上位机解析测试和端到端联调记录。

## 上位机测试

在仓库根目录运行：

```powershell
python -m unittest discover -s tests -v
```

当前测试重点覆盖：

- Gateway JSON 解析和历史导出字段
- MQ-135 CO2 估算、按节点 R0 校准和兼容回退
- 多节点规则融合、报警和 UI 判定 helper 的一致性
- 存在值低于、等于和高于用户硬阈值时的统一判定，以及低置信度/低 CSI 质量不否决存在判定
- 普通版节点连接后 20 秒启动演示、变化值范围、真实值恢复和 Mobile 版隔离
- AI 详情研判、AI 对话、Jina/大模型/规则回退路径

## 静态检查

文档或源码提交前建议运行：

```powershell
python -m compileall upper_computer
git diff --check
```

固件改动还应在 ESP-IDF 环境可用时分别构建 `firmware/node` 和 `firmware/gateway`。
