# 开发与验证工作流程

本文件描述编程智能体处理一次仓库任务的步骤。持续适用的约束见
[开发守则](../rules/README.md)，具体质量门、报告格式和服务器验证范围见
[验证说明](../validation/README.md)。Drawbridge 自身的 MCP 注册与发布路径属于
[系统背景](../context/RELEASE_FLOW.md)。

## 1. 理解任务

1. 阅读[当前架构](../context/ARCHITECTURE.md)，确定改动影响 Gateway、服务流程、
   存储、Git、Compose、构建、HTTP 或验证脚本中的哪些部分。
2. 执行 `git status --short`，确认已有改动；阅读对应源码和聚焦测试。
3. 按需查[技术设计](../../docs/TECHNICAL_DESIGN.md)、
   [实施规格](../../docs/MVP_IMPLEMENTATION_SPEC.md)或
   [运维步骤](../../docs/OPERATIONS.md)，再以源码和测试确认当前行为。
4. 若任务来自[优化改造计划](../../docs/OPTIMIZATION_PLAN.md)，从第一个未完成任务继续，
   核对依赖和验收标准。当前 01A、01B、01C、02 已完成，下一项是任务 03。

## 2. 修改与局部检查

1. 在现有模块边界内实现改动，保持 MCP 请求、应用绑定、发布计划和 Runner job 的契约。
2. 行为变化补充或调整聚焦测试；文档变化检查路径、命令和描述是否仍与代码一致。
3. 运行与改动直接相关的检查，修复明确失败，再进入完整验证。

## 3. 完整验证

1. 在仓库根目录执行 `uv sync --frozen --extra dev` 和
   `.venv/bin/python scripts/verify.py`。
2. 读取脚本打印的 `report.json`，确认退出码为 0 且 `result: passed`；失败时检查同目录
   对应的步骤日志。记录环境、时间、通过的质量门和实际覆盖范围。
3. 涉及真实服务器、Docker、BuildKit、认证、HTTP 出站或进程权限时，按
   [运维步骤](../../docs/OPERATIONS.md)执行独立的服务器检查。本地 simulation 不证明
   实际镜像构建或业务容器健康。

## 4. 交付

说明改了什么、原因、验证证据以及仍未验证的环节。将新的验证结果附日期追加到
[历史记录](../../docs/VERIFICATION_RECORD.md)，不要覆盖旧结果。

## 按任务定位

| 改动 | 优先核对 |
| --- | --- |
| MCP 工具或入口认证 | `gateway.py`、`models.py`、`tests/test_gateway.py` |
| 注册、计划、部署、回滚 | `service.py`、`storage.py`、`tests/test_service.py`、`tests/test_storage.py` |
| 冻结 SHA、计划指纹或服务拓扑 | `service.py`、`gitops.py`、`tests/test_service.py` |
| Git ref 或快照 | `gitops.py`、`tests/test_git.py` |
| Compose、构建 profile 或镜像 | `compose.py`、`build.py`、`config.py`、`tests/test_validation.py`、`tests/test_service.py` |
| 出站 HTTP、受控进程 | `httpverify.py`、`process.py` 及对应测试 |
| 本地验证脚本 | `scripts/verify.py`、`scripts/smoke_simulation.py`、[验证说明](../validation/README.md) |
