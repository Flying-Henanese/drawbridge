# Drawbridge 验证记录

## 1. 验证概况

| 项目 | 内容 |
| --- | --- |
| 验证日期 | 2026-09-20 |
| 验证依据 | `TECHNICAL_DESIGN.md` |
| 本地仓库 | `/Users/zhoushujian/Projects/GitHub/drawbridge` |
| 主要验证环境 | t4：`/home/mineru_dev/github_repo/drawbridge` |
| t4 Shell 约束 | 使用 `zsh`，未使用 `bash` |
| 验证配置 | `config.example.yaml`，simulation profile |

本记录汇总本次本地检查和 t4 服务器上的最终验证结果。t4 上的命令均通过登录式 `zsh` 执行，以确保使用用户登录环境中的 Python 和工具链。

## 2. 验证环境

### 2.1 t4 运行时

- Python：`3.12.12`
- Python 来源：`/home/mineru_dev/.pyenv/versions/3.12.12/bin/python3.12`
- 项目虚拟环境：`.venv`，已重建为 Python `3.12.12`
- uv：`0.10.6`，路径为 `/usr/local/bin/uv`
- Docker：`/usr/bin/docker`
- Docker Compose：`v2.35.1`

原有的 Python 3.10 虚拟环境保留在 t4 的 `.venv-py310`，未被删除。

### 2.2 依赖安装

在 t4 项目目录执行：

```zsh
uv venv --python /home/mineru_dev/.pyenv/versions/3.12.12/bin/python3.12 .venv
uv sync --frozen --extra dev
```

结果：依赖同步成功，`.venv/bin/python --version` 输出 `Python 3.12.12`。

## 3. 自动化测试结果

以下命令均在 t4 的项目目录中执行：

```zsh
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src/drawbridge
.venv/bin/pytest -q
python3 -m compileall -q src
```

结果如下：

| 检查项 | 结果 |
| --- | --- |
| Ruff | 通过：`All checks passed` |
| mypy | 通过：`Success, no issues found` |
| pytest | 通过：`17 passed, 1 warning` |
| Python 编译检查 | 通过 |

pytest 的唯一 warning 来自 Python `tarfile.extract` 的未来行为弃用提示；在当前 Python 3.12 环境下不影响测试通过。

## 4. 运行时自检

执行：

```zsh
.venv/bin/drawbridge self-check --config config.example.yaml
```

结果：整体 `ok: true`。

已验证的项目包括：

- Python 版本满足 3.12+
- Git、Docker 和 Docker Compose 可用
- Gateway 认证配置存在
- 状态目录可用
- SQLite WAL 可用
- HTTP 出站策略包含配置的 CIDR
- 模拟部署配置可用

`rootless_buildkit` 当前显示为 `not configured`。由于本次使用的是 simulation profile，该项为非阻塞检查；因此不影响本次 simulation 验证通过，但仍是后续真实 Docker/BuildKit 部署前必须补齐的环境项。

## 5. 模拟部署 Smoke Test

执行：

```zsh
PYTHONPATH=src .venv/bin/python scripts/smoke_simulation.py \
  --config config.example.yaml \
  --project examples/demo-repo
```

上述命令和路径仅记录当时的 t4 操作；当前仓库不再提供 `examples/` 应用目录。
新的本地验证入口见 `.harness/README.md`。

结果：模拟部署成功。

| 项目 | 结果 |
| --- | --- |
| 应用 | `demo` |
| 环境 | `staging` |
| Compose 服务 | `app` |
| Git commit | `f135b933591190bcc2185e5d18a8e9f4f59b6567` |
| Plan ID | `50fdac32-d843-4017-96a1-813a38042a45` |
| Job ID | `362c129e-db5f-410d-ba77-4a912598c402` |
| Release ID | `16cffbaf-defb-4325-a640-10ab5f078c5f` |
| 健康检查 | 通过 |
| 验证级别 | `simulation` |

## 6. Gateway 验证

使用以下命令启动 Gateway：

```zsh
.venv/bin/drawbridge-gateway \
  --config config.example.yaml \
  --host 127.0.0.1 \
  --port 8793
```

验证结果：

1. 未携带认证访问 `/mcp`，返回 `401 Unauthorized`。
2. 携带配置的认证信息调用 `ops_catalog`，调用成功。
3. 工具目录包含状态、日志、应用发现/注册、Git、进程、配置校验、发布计划/执行/回滚、服务重启、工作区补丁和 HTTP 请求等能力。
4. Gateway 验证完成后已停止测试进程。

## 7. t4 上的持久化验证证据

本次运行证据保存在：

```text
/home/mineru_dev/github_repo/drawbridge/var/state/state.db
/home/mineru_dev/github_repo/drawbridge/var/state/selfcheck.db
/home/mineru_dev/github_repo/drawbridge/var/releases/demo/staging/
```

截至本次检查，`state.db` 中包含：

| 表 | 记录数 |
| --- | ---: |
| `plans` | 6 |
| `jobs` | 5 |
| `releases` | 5 |
| `events` | 12 |

最新 release 目录为：

```text
/home/mineru_dev/github_repo/drawbridge/var/releases/demo/staging/16cffbaf-defb-4325-a640-10ab5f078c5f/
```

其中包含：

- `release.json`
- `compose.yaml`
- `README.md`

这些文件和 SQLite 记录构成了本次 simulation 部署的结构化证据。`var/state/state.db-wal` 和 `var/state/state.db-shm` 是 SQLite WAL 模式产生的运行文件。

## 8. 结论与未完成项

### 已完成

- Python 3.12.12 环境已在 t4 正确建立并用于最终验证。
- uv 依赖同步成功。
- Ruff、mypy、pytest 和编译检查全部通过。
- Drawbridge self-check 通过。
- simulation profile 下的计划、执行、健康检查和 release 产物验证通过。
- Gateway 认证和工具目录访问验证通过。
- 运行记录已落盘到 SQLite 和 release 目录。

### 尚未覆盖

- t4 当前未配置 rootless BuildKit。
- 本次没有执行真实 Docker/BuildKit 构建和真实服务切换。
- `pytest` 等检查的原始终端输出未单独保存为 JUnit XML 或日志文件；本文件保存了结果摘要，程序运行证据保存在 t4 的 `var/` 目录中。

因此，本次验证结论为：**Drawbridge MVP 在 Python 3.12.12、simulation profile 和 t4 目标目录下验证通过；真实生产部署前仍需配置并验证 rootless BuildKit 及真实服务运行链路。**

## 9. 后续本地 harness 复验（2026-09-20）

在本地 macOS arm64、Python 3.14.5 上运行新增的 `scripts/verify.py`，使用独立的临时配置、Git demo 仓库与 SQLite 状态目录。机器可读摘要与原始命令输出保存在本地忽略目录 `var/verification/20260920T034254Z/`。这是对 harness 的一次本地复验，**不是**对上述 t4 环境或真实 Docker/BuildKit 链路的再次验收。

| 检查 | 本次结果 |
| --- | --- |
| Ruff / 格式检查 / mypy / compileall | 全部通过 |
| pytest | 18 passed；新增 Gateway 测试确认匿名请求 401、认证后的 `ops_catalog` 调用成功 |
| self-check | `ok: true`；simulation profile 下 rootless BuildKit 为非阻断项 |
| simulation | job `succeeded`，健康检查 `passed`，source SHA 与 fixture HEAD 相同 |
| 持久化与制品 | 1 plan、1 job、1 release、3 events；`release.json`、`compose.yaml`、`README.md` 存在 |

本次 plan ID 为 `c2872bde-4f67-47f2-b85c-8cd76ba8f6f8`，job ID 为 `b17a4cb8-f615-4246-9128-85350a3fca7c`，release ID 为 `ebe5c7bb-348b-4c31-ac0b-d50123cabb44`。本地 Python 3.14 上有依赖库的弃用 warning；18 个测试均通过。验证目录是本机未跟踪的运行证据，不随仓库分发；其他环境可按 `AGENTS.md` 和 `.harness/README.md` 复现。

## 10. 移除示例应用后的本地 harness 验证（2026-09-20）

移除仓库内的 `examples/demo-app` 后，`scripts/verify.py` 改为在系统临时目录生成最小
Git/Compose 验证仓库，并以 `verification` 应用运行 simulation。该 Compose 文件引用
`example.invalid` 镜像；验证不拉取镜像或启动容器。运行 `uv sync --frozen --extra dev`
和 `.venv/bin/python scripts/verify.py`，证据保存在本地忽略目录
`var/verification/20260920T093319Z/`。

本次环境为 macOS arm64、Python 3.14.5。`report.json` 的 `result` 为 `passed`：
Ruff、格式检查、mypy、18 个 pytest 测试和编译均通过；隔离配置的 self-check
`ok: true`；simulation job 为 `succeeded`，source SHA 与临时仓库 HEAD 一致，
发布物 `release.json`、`compose.yaml` 存在，SQLite 中记录了 1 个 plan、1 个 job、
1 个 release 和 3 个 event。完整命令输出见同目录的步骤日志。此结果只覆盖本地
simulation，不能作为真实 Docker/BuildKit 部署的验收结果。

## 11. BuildKit 构建流程的本地验证（2026-09-20）

新增从冻结 Git 快照调用 BuildKit、导入 Docker archive、按 tag 查验镜像 ID，再以镜像 ID
部署的流程。本机先执行 `uv sync --frozen --extra dev`，再执行
`.venv/bin/python scripts/verify.py`；报告位于
`var/verification/20260920T104742Z/report.json`，`result: passed`。macOS arm64、
Python 3.14.5 环境下，Ruff、格式、mypy、编译和 25 个 pytest 测试通过；隔离
simulation 的 self-check 和发布流程也通过。构建测试使用受控的 BuildKit/Docker 替身
进程，覆盖两个构建服务、冻结源码、构建失败后不执行部署、Compose 启动前清理镜像、
Compose 启动后失败保留制品、profile 变更使计划失效、现成镜像绕过构建，以及注册时
拒绝未登记的构建参数。
本次没有运行真实 rootless BuildKit 构建、Docker image load 或 NPU 容器；服务器安装与
实际部署仍须按 `docs/OPERATIONS.md` 单独验收。
