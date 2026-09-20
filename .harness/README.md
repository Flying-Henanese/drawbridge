# Drawbridge Harness Guide

`.harness/` 是本项目面向 Codex 和其他编码智能体的仓库内工作入口。它负责把
任务路由、验证命令、证据边界和安全限制集中说明；`docs/` 仍然保存面向人的
设计、运维和历史验证长文档。

## 使用顺序

1. 先阅读根目录 [`AGENTS.md`](../AGENTS.md)。
2. 根据任务阅读 [`docs/`](../docs/) 中相关的设计、规格或运维文档；不要把本
   文件当作源码和测试的替代品。
3. 修改前检查 `git status --short`，保留用户已有改动，并核对相关源码和测试。
4. 完成修改后运行下方的 harness；涉及真实服务器、Docker、BuildKit、认证或
   进程权限时，另外遵循 [`docs/OPERATIONS.md`](../docs/OPERATIONS.md)。
5. 交付时检查 `report.json` 和失败步骤日志，并在交接中记录本次实际验证路径、
   环境和覆盖范围。

## 任务路由

| 任务 | 优先阅读/执行 |
| --- | --- |
| 理解架构、边界和安全不变量 | [`docs/TECHNICAL_DESIGN.md`](../docs/TECHNICAL_DESIGN.md)、[`docs/MVP_IMPLEMENTATION_SPEC.md`](../docs/MVP_IMPLEMENTATION_SPEC.md) |
| 修改 Gateway、MCP 或认证 | `src/drawbridge/gateway.py`、`tests/test_gateway.py`，再运行完整 harness |
| 修改操作、部署或队列 | `src/drawbridge/service.py`、`src/drawbridge/runner.py`、`src/drawbridge/storage.py` 及对应测试 |
| 修改配置或自检 | `src/drawbridge/config.py`、`src/drawbridge/selfcheck.py`、`tests/test_validation.py` |
| 接入 Codex、Claude Code 或其他 MCP 客户端 | [`docs/MCP_CLIENTS.md`](../docs/MCP_CLIENTS.md) |
| 本地回归验证 | `uv sync --frozen --extra dev`，然后 `.venv/bin/python scripts/verify.py` |
| 服务器部署/真实 Docker 验证 | [`docs/OPERATIONS.md`](../docs/OPERATIONS.md)，不能用 simulation harness 代替 |

## 验证入口

在仓库根目录执行（Python 3.12+）：

```sh
uv sync --frozen --extra dev
.venv/bin/python scripts/verify.py
```

harness 成功时返回退出码 0；`--output /path/to/new-directory` 可以指定一个尚不存在
的证据目录，默认写入 `var/verification/<UTC timestamp>/`。脚本从自身位置解析仓库根目录，
因此可从其他当前工作目录调用。

它使用 `config.example.yaml` 创建临时配置、从 `examples/demo-app` 创建临时 Git 仓库，
并把状态、日志、发布物、模板和数据重定向到临时目录。运行不会修改已安装应用或仓库现有
`var/` 状态；临时 fixture 会在结束时清理，证据目录中的日志和摘要会保留。

## 质量门和证据

| 质量门 | 内容 | 证据 |
| --- | --- | --- |
| Ruff | `ruff check src tests scripts` | `ruff.log` |
| 格式 | `ruff format --check src tests scripts` | `format.log` |
| mypy | 严格检查 `src/drawbridge` | `mypy.log` |
| pytest | 完整测试套件，包括 Gateway 匿名 `401` 和已认证 `ops_catalog` MCP 调用 | `pytest.log` |
| 编译 | Python 源码编译 | `compileall.log` |
| 主机自检 | 隔离 simulation 配置的阻断检查通过 | `self_check.log`、`report.json` |
| Simulation | register → plan → apply → run job → status；检查 job、health、source SHA、发布物和 SQLite 记录 | `simulation.log`、`report.json` |

`report.json` 包含结果、时间、主机/Python/Git 信息、自检明细、simulation ID 和数据库行数。
每个步骤日志记录命令、退出码、标准输出和标准错误。失败步骤会写入 `result: failed` 并以
非零退出码结束；不要仅凭报告文件存在就判断通过，必须读取其中的 `result`。

## 工作约束

1. 先阅读与改动相关的设计/规格文档，并确认实现和测试中的当前行为。
2. 为行为变化补充或调整聚焦测试；开发时可先运行聚焦测试，交付前必须运行完整 harness。
3. 检查 `report.json` 及失败日志。历史验证记录保存在
   [`docs/VERIFICATION_RECORD.md`](../docs/VERIFICATION_RECORD.md)，应追加带日期的结果，
   不要覆盖旧证据。
4. 涉及真实部署、认证、出站 HTTP 或进程执行时，必须同时审阅
   [`docs/OPERATIONS.md`](../docs/OPERATIONS.md) 和设计约束。

## 范围和服务器验证

harness 有意不执行网络抓取、真实容器构建、Docker Compose 更新或服务器状态变更。Gateway
测试使用进程内 ASGI client，验证策略和 MCP 工具分发，不等同于已部署的网络监听器。
simulation profile 下 rootless BuildKit 检查是非阻断项。真实部署必须按
[`docs/OPERATIONS.md`](../docs/OPERATIONS.md) 的服务器安装和验收步骤执行，并使用管理员批准的
配置、rootless BuildKit 和明确的运行时检查。不能仅凭 `report.json` 推断生产就绪。
