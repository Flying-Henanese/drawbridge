# Drawbridge 编程智能体入口

Drawbridge 是运行在目标 Linux 服务器上的部署与运行观测 MCP 服务。Gateway 提供
`/mcp`，Runner 消费共享 SQLite 队列中的变更任务。它只操作已登记的 Git/Compose 项目，
当前实现只支持 `staging` 环境。需要构建时使用单独的 rootless BuildKit；可信项目也可按
管理员配置复用现有镜像并跳过构建。本地示例配置使用 simulation，不启动业务容器。

项目要解决的是编程智能体修改代码后，如何在远端服务器按固定步骤构建、部署并收集验证
证据。MCP 客户端提交应用、Git ref 和计划 ID；服务端校验输入、冻结源码版本、排队执行，
再把任务状态和运行证据返回给客户端。这样智能体可以判断一次发布的实际结果。

本目录只存放帮助编程智能体理解和维护项目的说明。可执行验证脚本仍在 `scripts/`，产品
测试仍在 `tests/`，面向运维人员的安装步骤和历史记录仍在 `docs/`。

## 目录分工

| 分类 | 内容 |
| --- | --- |
| [`context/`](context/README.md) | 项目背景、[当前架构](context/ARCHITECTURE.md)和[系统发布路径](context/RELEASE_FLOW.md) |
| [`rules/`](rules/README.md) | 开发时持续适用的安全与代码约束 |
| [`workflow/`](workflow/README.md) | 理解任务、开发、验证和交付的步骤 |
| [`validation/`](validation/README.md) | 质量门命令、验证证据与真实服务器验收范围 |

## 阅读顺序

1. [项目背景](context/README.md) → [当前架构](context/ARCHITECTURE.md)：了解组件、数据流和已知限制。
2. [开发守则](rules/README.md) → [工作流程](workflow/README.md)：确认约束，再按步骤开发和验证。
3. [验证说明](validation/README.md)：查具体命令、报告字段和真实服务器验收范围。
4. 涉及系统发布路径时读[发布流程](context/RELEASE_FLOW.md)；需要更细的接口或服务器操作时，再读
   [技术设计](../docs/TECHNICAL_DESIGN.md)、[实施规格](../docs/MVP_IMPLEMENTATION_SPEC.md)
   和 [运维步骤](../docs/OPERATIONS.md)。长篇设计包含目标状态；实际行为以当前源码和测试为准。

根目录 [AGENTS.md](../AGENTS.md) 是简短入口，完整项目上下文放在本目录。

## 当前改造进度

- [优化改造计划](../docs/OPTIMIZATION_PLAN.md)中的任务 01A、01B、01C 和 02 已完成。
- 当前第一个未完成任务是任务 03：收紧 SQLite 事务边界，并保证 Gateway 初始化不会执行
  写迁移。开始下一阶段时先核对该任务及其依赖，不重复实现已完成任务。
- 任务 02 已在 t4 上完成 harness 和 ContractLens 现有镜像发布验证；这是带日期的历史证据。
  BuildKit 构建、失败回滚、Runner 中断恢复和严格进程权限仍需各自验收，因此任务 11 尚未完成。

## 常用入口

| 任务 | 入口 |
| --- | --- |
| 理解注册、计划、构建和部署 | [架构](context/ARCHITECTURE.md) → [发布流程](context/RELEASE_FLOW.md) → `src/drawbridge/service.py` |
| 修改 MCP、认证或直连访问 | [架构](context/ARCHITECTURE.md) → `src/drawbridge/gateway.py` → `tests/test_gateway.py` |
| 修改配置、Compose 或 BuildKit 规则 | `src/drawbridge/config.py`、`compose.py`、`build.py` 与相关测试 |
| 修改队列、发布状态或 Runner | `src/drawbridge/storage.py`、`service.py`、`runner.py` 与相关测试 |
| 按任务改造安全性、恢复能力和权限边界 | [优化改造计划](../docs/OPTIMIZATION_PLAN.md) |
| 开发与本地验证 | [守则](rules/README.md) → [工作流程](workflow/README.md) → [验证说明](validation/README.md) |
| 真实服务器部署 | [运维步骤](../docs/OPERATIONS.md)；本地 simulation 不能代替实际验收 |

本地完整验证命令：

```sh
uv sync --frozen --extra dev
.venv/bin/python scripts/verify.py
```

脚本输出 `var/verification/<UTC 时间>/report.json` 的路径。交付前读取 `result` 和步骤日志。
