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

## 12. 编程智能体架构文档整理后的本地验证（2026-09-21）

`.harness/` 现提供当前实现架构、开发工作流程和验证说明；执行脚本仍在 `scripts/`。
本次同时修正 `src/drawbridge/build.py` 导入区的一处 Ruff 空行格式错误。执行
`uv sync --frozen --extra dev` 和 `.venv/bin/python scripts/verify.py`，报告保存在本机
`var/verification/20260920T160250Z/report.json`（UTC 时间），`result: passed`。

macOS arm64、Python 3.14.5 环境下，Ruff、格式、mypy、编译、26 个 pytest 测试和
隔离 simulation 均通过；self-check 的阻断项全部通过。pytest 记录 165 个 warning，
其中包含依赖在 Python 3.14 上的弃用提示。simulation job 为 `succeeded`，发布物
`release.json` 与 `compose.yaml` 存在，SQLite 中记录了 1 个 plan、1 个 job、1 个
release 和 3 个 event。本次没有连接 t4，也没有执行真实 Docker/BuildKit 部署。

## 13. Harness 说明分类后的本地复验（2026-09-21）

将 `.harness/` 说明整理为 `context/`、`workflow/`、`validation/`，补充项目概念和
MCP 发布调用流程；验证脚本仍位于 `scripts/`。执行 `uv sync --frozen --extra dev`
和 `.venv/bin/python scripts/verify.py`，证据位于本机
`var/verification/20260920T161405Z/report.json`（UTC 时间）。报告 `result: passed`：
Ruff、格式、mypy、编译、26 个 pytest 测试、隔离 self-check 和 simulation 均通过。
同时检查了 6 个 harness Markdown 文件的相对链接，未发现失效链接。
本次未连接 t4，也未执行真实 BuildKit 或 Docker 部署。

## 14. Rules 与开发验证流程拆分后的本地复验（2026-09-21）

将 `.harness/rules/` 用于持续适用的开发约束，`.harness/workflow/` 用于开发与验证步骤，
系统自身的 MCP 发布路径移入 `.harness/context/`。执行 `uv sync --frozen --extra dev`
和 `.venv/bin/python scripts/verify.py`，本机报告为
`var/verification/20260920T162705Z/report.json`（UTC 时间），`result: passed`。
Ruff、格式、mypy、编译、26 个 pytest 测试、隔离 self-check 与 simulation 均通过；
7 个 harness Markdown 文件的相对链接全部有效。此结果不覆盖真实服务器部署。

## 15. 优化改造计划文档后的本地复验（2026-09-21）

新增 [优化改造计划](OPTIMIZATION_PLAN.md)，并从 `.harness/README.md` 链接该计划。
在本地 macOS arm64、Python 3.14.5 上运行 `uv sync --frozen --extra dev` 和
`.venv/bin/python scripts/verify.py`。报告位于本机忽略目录
`var/verification/20260920T164252Z/report.json`（UTC 时间），`result: passed`：
Ruff、格式、mypy、编译、26 个 pytest 测试、隔离 self-check 与 simulation 均通过。
同时检查了计划和入口文档的相对链接及尾随空格，均未发现问题。本次只验证本地
simulation，没有连接真实 Docker/BuildKit 服务，也没有在服务器执行发布或回滚。

## 16. Compose 宿主挂载路径收紧（01A，本地验证，2026-09-21）

实现 Compose 短语法与 `type: bind` 长语法的统一宿主源校验：拒绝 `..`、越界路径、源及
父路径中的符号链接和未登记的绝对宿主路径；命名卷不再按本地相对路径处理。新增严格的
`data_mounts.read_only` 配置字段，并在注册与 Git 快照校验时复用显式传入的允许挂载列表。
危险挂载不会形成 binding，快照校验失败也不会触发外部执行器。

本地 macOS arm64、Python 3.14.5 执行 `uv sync --frozen --extra dev`、聚焦测试和完整
harness。36 个 pytest 测试、Ruff、格式、mypy、编译、隔离 self-check 与 simulation 均通过；
完整报告为 `var/verification/20260921T065359Z/report.json`，`result: passed`。本地证据不覆盖
真实 Docker/BuildKit 或服务器运行时，后续服务器验证结果另行追加。

## 17. 01A 的 t4 远端与 ContractLens 部署验证（2026-09-21）

将 01A 的 `compose.py`、`config.py`、`service.py` 同步到 t4 的 Drawbridge 工作树
`/home/mineru_dev/github_repo/drawbridge` 后，运行同一组聚焦测试，结果为 **23 passed**；
远端 `compileall` 和 mypy 也通过。重启后的 `drawbridge-gateway.service`、
`drawbridge-runner.service`、`drawbridge-buildkit.service` 均为 active。远端完整 harness 报告
位于 `/home/mineru_dev/github_repo/drawbridge/var/verification/20260921T065756Z/report.json`，
`result: passed`；其中 Ruff、格式、mypy、26 个 pytest、编译、self-check 和 simulation 均通过。

通过实际 MCP Gateway 读取 `ops_catalog`、`ops_status`、`ops_git_status` 和 `ops_git_log`，
确认 Gateway 可认证访问、`contractlens` 注册配置可读取、当前无已发布 release，且源仓库状态
干净。远端配置中的项目根目录是 `/data1/zsj/projects/ContractLens`；用户指定的
`/home/mineru_dev/projects/ContractLens` 是指向该目录的符号链接。01A 按设计拒绝包含符号链接
的 Compose 路径，因此显式测试 `/home/...` 得到 `compose_file contains a symlink`，实际验证使用
配置中的规范路径，未修改服务器配置。

随后通过 Gateway 创建并应用真实 Docker 部署计划：plan
`e174713b-a4c8-42e2-8e34-854b52145a6e`，job
`46691567-9156-48ed-aca8-fffe8a4b8162`。BuildKit 构建、Docker image load、Compose 网络/容器
创建均已执行；但 `paddleocr-vlm-server` 首次启动时下载约 1.79 GB 的模型，在当前健康检查超时内
未能监听 `127.0.0.1:8118`，导致依赖等待超时，job 最终为 `DEPLOY_FAILED`。该失败发生在模型
首次下载/服务就绪阶段，不是 01A 宿主挂载路径校验失败；本次也不能据此宣称真实服务已部署成功。

失败后的测试 Compose 容器和网络已清理；缓存卷和构建镜像按保留制品策略保留，release 目录也保留
在 `/home/mineru_dev/.local/share/drawbridge/releases/contractlens/staging/625246a0-d5dc-4589-944d-e8bf2ee394e4`。
远端工作树中的 01A 修改及用于恢复现场的 `.pre-*` 备份均有意保留，未执行破坏性回滚。

## 18. 01B runtime profile、Ascend 挂载与无构建发布验证（2026-09-21）

01B 增加管理员登记的 runtime profile：`privileged`、宿主机挂载、端口和 GPU device reservation
必须逐项匹配，应用注册与 Git 快照校验都会重新验证；旧 binding 可在重新注册时刷新 profile。Ascend
Compose 的两个推理服务均加入以下挂载：

```text
/etc/ascend_install.info:/etc/ascend_install.info:ro
/var/log/npu:/var/log/npu
```

本地执行 `uv sync --frozen --extra dev` 与 `.venv/bin/python scripts/verify.py`，报告为
`var/verification/20260921T091855Z/report.json`，`result: passed`；Ruff、格式、mypy、编译、隔离
self-check 和 simulation 均通过。远端 t4 的同一 harness 报告为
`/home/mineru_dev/github_repo/drawbridge/var/verification/20260921T091913Z/report.json`，`result: passed`；
远端聚焦测试为 **40 passed**，mypy、compileall 也通过。Ascend 原始 Compose 和静态
`compose.drawbridge.ascend.yaml` 均通过远端 Compose 配置校验及 Drawbridge Compose 解析；t4 本身没有
Ascend driver、`npu-smi`、`/etc/ascend_install.info` 或 `/var/log/npu`，因此本记录不宣称 Ascend
运行时已验收，需在真实 Ascend 主机复测。

在 t4 上通过真实 MCP Gateway 完成了应用重新注册（binding version 2→3）、plan、apply 和
`ops_release_status`。为验证“直接启动现有镜像”提交了 ContractLens commit `bb3599732383d300790f634ce536dfdec5a9b673`：
API 使用已有 image digest，两个推理服务使用宿主机 `/home/mineru_dev/.paddlex` 缓存，GPU 容器内编号
使用 `0,1`/`0`。Drawbridge plan 为 `e3ba4d99-1e20-47a0-85c0-960931531221`，job 为
`bf96cfa4-accf-4637-8614-454c750bed74`；Runner 没有执行 BuildKit，Compose 直接使用现有镜像，但
job 最终为 `DEPLOY_FAILED`。失败日志确认模型文件已命中缓存（没有重新下载），实际原因是 t4 的 0–7 号
GPU 均被其他进程占用，VLM 启动时只剩约 0.05 GiB 可用显存，无法满足 0.8 GPU memory utilization。

本次 Drawbridge 创建的失败测试容器和网络已按精确项目名清理；named cache volume、镜像和 release
制品保留，未终止其他项目进程。Ascend 挂载与静态 Compose 已提交到远端 ContractLens commit
`fd19ac4`；恢复备份仍作为未跟踪文件保留，没有执行破坏性回滚。

## 19. 01C 可信 Compose 与预建镜像策略本地验证（2026-09-22）

新增管理员 runtime profile 字段 `approved_compose_digests`，只有旧 Compose 文件 SHA-256
与批准值精确匹配时才允许额外字段、顶层资源、宿主能力和环境变量插值。未启用预建选项时，
文件变化后恢复严格策略；配套的
`prefer_prebuilt_images` 使同时含 `image`/`build` 的服务只使用现有镜像，只有 `build`、没有
`image` 的服务会被拒绝，摘要不匹配也直接拒绝而不回退到构建。顶层及服务级
`include`/`extends`、项目外 Compose 和符号链接限制继续生效。

本地 macOS arm64、Python 3.14.5 执行任务聚焦测试与完整 harness。聚焦测试中
`tests/test_validation.py` 和 `tests/test_service.py` 共 **44 passed**；完整报告位于
`var/verification/20260922T025132Z/report.json`，`result: passed`。Ruff、格式、mypy、编译、
57 个 pytest、隔离 self-check 与 simulation 全部通过。受控 executor 测试确认可信 Compose
同时包含 `image` 和带 args 的 `build` 时只执行 `compose-deploy`，没有 BuildKit、镜像导入或
镜像识别调用。本地验证没有启动 ContractLens 容器；t4 真实验证结果在部署后另行追加。

## 20. 01C 的 t4 可信 Compose 与预建镜像发布验证（2026-09-22）

将 Drawbridge commit `17e940d` 快进部署到 t4 的
`/home/mineru_dev/github_repo/drawbridge`，执行远端完整 harness，报告位于
`/home/mineru_dev/github_repo/drawbridge/var/verification/20260922T025307Z/report.json`，
`result: passed`；Ruff、格式、mypy、编译、57 个 pytest、隔离 self-check 和 simulation
全部通过。重启后的 `drawbridge-gateway.service` 与 `drawbridge-runner.service` 均为 active。

远端新增独立的 `contractlens-trusted` 应用和同名 runtime profile，未修改原有
`contractlens` 登记。profile 批准的 Compose SHA-256 为
`33df309daa107f3f3378e8b67f0936a8aca00dbb25184d106be0e3d9b96c51bc`，并启用
`prefer_prebuilt_images`。用户给出的 `/home/mineru_dev/projects/ContractLens` 解析到配置使用的
规范目录 `/data1/zsj/projects/ContractLens`。为避开服务器上已占用的 GPU，验证分支提交
`e62f54c4be797821947cd1fc6e02c0450a56a6a0` 将 VLM 默认 GPU 调整为 2、3，API 继续使用 4；
该提交只存在于 t4 的 ContractLens 工作树，未推送远端。

随后通过实际 MCP Gateway 完成 `ops_catalog`、`ops_app_register`、`ops_release_plan`、
`ops_release_apply` 和 `ops_release_status`：plan
`797ec2c0-cacd-4081-a24c-6bdf555b8907`，job
`7e0213c0-0b3a-4b34-a69d-094288d84393`，release
`05031d57-5a2c-428d-969f-eeec5648ab77`。job 最终为 `succeeded`，HTTP 健康检查为
`passed`，结果中的 `built_images` 为 `{}`；三个服务分别直接使用已存在的
`pdf-parser:cuda12.2`、PaddleOCR VLM 和 PaddleOCR API 镜像，没有执行 BuildKit 构建或镜像导入。
运行时固定使用 `docker compose up --no-build --pull never`，因此不会隐式构建或拉取镜像。

Compose 项目 `drawbridge-contractlens-trusted-staging` 的三个容器均为 `healthy`；端口
8888、8880、8118 正常监听。通过 MCP 再执行 `ops_status`、`ops_logs` 和
`ops_http_request`：当前 release 与上述 release ID 一致，API 日志包含应用启动完成和
`GET /openapi.json` 200，受限 HTTP 请求也返回 200。服务器配置修改前的备份位于
`/home/mineru_dev/.config/drawbridge/config.yaml.pre-trusted-20260922T110208`；拉取前的远端
Drawbridge 修改保留在 `stash@{0}`，原有 `.pre-*` 文件未删除。

## 21. 任务 02 冻结 SHA 计划指纹本地验证（2026-09-22）

任务 02 将计划阶段与 Runner 阶段统一为同一个快照准备原语：从计划解析出的完整 commit SHA
执行 `git archive`，应用显式 workspace revision，再校验 Compose、build 声明和固定服务集合。
版本化 plan 保存规范化的 Compose、build 声明、revision、binding 配置和完整 build profile
摘要；apply 与 Runner 都拒绝旧 schema，Runner 在任何构建、Docker 或 simulation release
副作用前重新计算并逐项比较指纹。

本地 macOS arm64、Python 3.14.5 先运行 `tests/test_service.py` 聚焦测试，结果为
**25 passed**。完整执行 `uv sync --frozen --extra dev` 和
`.venv/bin/python scripts/verify.py`，报告位于
`var/verification/20260922T052709Z/report.json`，`result: passed`；Ruff、格式、mypy、编译、
67 个 pytest、隔离 self-check 与 simulation 全部通过。测试覆盖分支前进和未提交工作区不影响
旧计划、服务拓扑在计划阶段拒绝、workspace revision 在解析前应用、旧 plan schema 拒绝、
各快照指纹篡改、无 build 服务时 profile 变化、binding 配置变化，以及注释、空白和 mapping
键顺序不影响 Compose 摘要。本地结果没有覆盖真实 Docker/BuildKit；t4 验证另行记录。

## 22. 任务 02 的 t4 冻结 SHA 与 ContractLens 发布验证（2026-09-22）

将 Drawbridge commit `a47734a` 部署到 t4 的
`/home/mineru_dev/github_repo/drawbridge`，远端完整 harness 报告为
`/home/mineru_dev/github_repo/drawbridge/var/verification/20260922T052846Z/report.json`，
`result: passed`；Python 3.12.12 下 Ruff、格式、mypy、编译、67 个 pytest、隔离 self-check
和 simulation 全部通过。随后重启 Gateway 与 Runner，两个 systemd user service 均为 active。

通过实际 MCP Gateway 为 `contractlens-trusted` 创建计划
`79a95de3-b3d8-4bc5-ab2a-dd56cf7a3297`，计划冻结 ContractLens SHA
`e62f54c4be797821947cd1fc6e02c0450a56a6a0`。创建计划后，在同一分支生成空提交
`b82488da940db3eec58f3c08c14f12312d68ef08`，并在 apply 和 Runner 执行期间将工作区
`compose.yaml` 临时替换为无效内容。job
`f5fb1d65-9310-4b7c-a85e-f583d18d81af` 最终 `succeeded`，release
`bad03865-5f7c-4bb8-883d-ea5496e24ee1` 的 `source_sha` 仍为计划冻结的 `e62f54c...`，证明
Runner 没有重新解析已前进的分支，也没有读取未提交工作区。

本次继续使用可信 Compose 的预建镜像路径；job 结果中的 `built_images` 为 `{}`，没有执行
BuildKit、镜像导入或镜像拉取。三个 Compose 容器均为 `healthy`，MCP `ops_status` 指向上述
release，`ops_logs` 返回 API 日志，`ops_http_request` 对 OpenAPI 地址返回 200。测试结束后
`compose.yaml` 已恢复，SHA-256 仍为
`33df309daa107f3f3378e8b67f0936a8aca00dbb25184d106be0e3d9b96c51bc`。用于推进分支的空提交只
保留在 t4 的 ContractLens 工作树，没有推送远端；原有未跟踪备份文件未删除。

## 23. 任务 03 SQLite 事务与 Gateway 初始化本地验证（2026-09-22）

任务 03 为每个 `Database` 实例增加统一的异步连接锁，并让所有多语句写操作在锁内使用
`BEGIN IMMEDIATE` 完成完整 commit/rollback。队列容量、幂等键和 job 插入保持同一事务，
job 认领使用带 `status = 'queued'` 条件的更新；binding 版本读写也在一个事务中。成功部署或
simulation 回滚现在把 release、可选成功事件和 job 终态一次提交。Gateway 数据库 schema
初始化从每个 MCP 工具调用迁移到 ASGI 应用启动周期，应用关闭时释放连接。

聚焦执行 `tests/test_storage.py`、`tests/test_gateway.py` 和 `tests/test_service.py`，结果为
**42 passed**。测试覆盖不同请求并发入队、相同幂等键、同 plan 不同键、全局和目标容量竞争、
失败事务隔离、两个 `Database` 实例竞争认领、binding 并发版本递增、release/job 原子提交，
以及 8 个并发 MCP 工具调用只初始化一次 schema。

本地 macOS arm64、Python 3.14.5 完整执行 `.venv/bin/python scripts/verify.py`，报告位于
`var/verification/20260922T065404Z/report.json`，`result: passed`；Ruff、格式、mypy、编译、
78 个 pytest、隔离 self-check 与 simulation 全部通过。本地结果没有覆盖 t4 上两个 systemd
进程对同一 SQLite 文件的实际协作，服务器验证另行记录。

## 24. 任务 03 t4 服务启动与并发 MCP 验证（2026-09-22）

将任务 03 提交 `a556cfb` 部署到 t4 的
`/home/mineru_dev/github_repo/drawbridge`。远端执行 `uv sync --frozen --extra dev` 和
`.venv/bin/python scripts/verify.py`，报告位于
`/home/mineru_dev/github_repo/drawbridge/var/verification/20260922T065504Z/report.json`，
`result: passed`；Python 3.12.12 下 Ruff、格式、mypy、编译、78 个 pytest、隔离 self-check
与 simulation 全部通过。

随后重启 `drawbridge-gateway.service` 和 `drawbridge-runner.service`，二者均为 `active`。
Gateway 日志显示 ASGI application startup complete；对实际 MCP `/mcp` 同时发送 16 个只读
`ops_catalog` 请求，16 个均返回 HTTP 200 并包含完整工具目录。此操作没有创建变更 job。

ContractLens 的三个现有容器仍为 healthy。本次只重启 Drawbridge 服务并执行只读 MCP 检查，
没有构建镜像，也没有改动 ContractLens。t4 工作树原有的 7 个未跟踪 `.pre-*` 备份文件均
保留。

## 25. Harness 与当前权限及 Python 基线对齐（本地验证，2026-09-22）

本次将项目安装元数据、Ruff 和 mypy 的 Python 基线统一为 3.12，并在 `scripts/verify.py`
增加 `runtime_metadata` 检查，防止运行时要求与开发工具配置再次漂移。切换 Ruff 目标后，
按 Python 3.12 语义等价地改用 `datetime.UTC`、内置 `TimeoutError` 和 `StrEnum`。

Harness、README、运维文档和 systemd 模板现在按当前调用路径说明权限：Gateway 负责注册、
Git 查询和计划，`fetch` 模式需要写登记仓库；Runner 读取冻结 SHA 并负责 Docker 构建和变更。
Gateway 没有 Docker daemon 权限时，`ops_app_discover` 和 Docker release 的 `ops_logs` 不可用。
文档同时明确 `self-check` 不验证 Docker daemon、目标镜像、仓库属主、Git fetch 或 BuildKit
daemon 的 rootless 属性，这些条件仍须以对应服务用户在真实服务器上单独验收。为避免共享
BuildKit 配置要求 Gateway 访问 Runner 专属 socket，CLI 新增 `--role gateway|runner|all`：
Gateway 角色跳过 BuildKit 检查，Runner 角色保留对应阻断项，省略参数时保持兼容的完整检查。
systemd 模板分别读取 `gateway.yaml` 和 `runner.yaml`，两个进程使用 `drawbridge` 主组及
`UMask=0007`；Runner 通过补充组保留 Docker 访问。状态目录须由共享组持有并启用 setgid，
这样 Gateway 创建的 SQLite/WAL 文件才能由 Runner 继续读写。

本地先执行受影响的进程、HTTP 和角色化 self-check 聚焦测试，共 **6 passed**。随后使用临时
Python 3.12.13 环境执行完整 harness，报告位于
`var/verification/20260922T104010Z/report.json`；再以日常 `.venv` 的 Python 3.14.5 复验，
报告位于 `var/verification/20260922T105741Z/report.json`。两份报告均为 `result: passed`，
Ruff、格式、mypy、编译、79 个 pytest、隔离 self-check 和 simulation 全部通过；
`runtime_metadata` 为 `requires_python: >=3.12`、`ruff_target: py312`、
`mypy_python_version: 3.12`。本次没有在 t4 重启服务或执行真实 Docker/BuildKit 发布；历史
t4 证据仍是带日期的记录，不代表当前服务器就绪状态。

## 26. 管理员维护 Compose 文件模式（本地验证，2026-09-23）

新增每个应用环境的 `operator_compose` 入口，从管理员只读目录复制 Compose、`.env` 和服务级
`env_file` 到 Git SHA 对应的发布快照。计划记录这些文件的内容摘要；Runner 重新读取并比较，
变更后旧计划以 `STALE_PLAN` 终止。管理员模式只使用预建镜像；Runner 从本地 Docker Engine
解析镜像并用实际镜像 ID 启动，不要求 Gateway 访问 Docker daemon。Git Compose 的摘要批准
与严格校验路径继续保留。

本地 macOS arm64、Python 3.14.5 执行 `uv sync --frozen --extra dev` 和完整
`.venv/bin/python scripts/verify.py`，报告位于
`var/verification/20260923T063927Z/report.json`，`result: passed`；Ruff、格式、mypy、编译、
**84 个 pytest**、隔离 self-check 和 simulation 均通过。聚焦测试覆盖管理员文件改动后旧计划
失效、重新计划后发布、Compose 入口文件名刷新、受控目录、相对路径和 MCP 可编辑文件重叠限制，以及用受控执行器
模拟的 Docker 镜像 ID 固定。此结果没有在 t4 上验收实际管理员目录权限、Docker Compose
解析、本地镜像运行或业务健康；不代表真实服务器发布就绪。

## 27. 管理员维护 Compose 模式 t4 独立应用验收（2026-09-23）

2026-09-23 09:16 UTC 在 t4（hostname `bms-v38f-0004`）完成独立 smoke 应用验证。Drawbridge
服务器工作树位于 `/home/mineru_dev/github_repo/drawbridge`，分支
`codex/operator-managed-compose-validation`，commit
`d1480293582c19fff76aa3350d315936537c92bc`。远端执行 `uv sync --frozen --extra dev` 和
`.venv/bin/python scripts/verify.py`，报告为
`/home/mineru_dev/github_repo/drawbridge/var/verification/20260923T074245Z/report.json`，
`result: passed`，84 个 pytest 及其余 harness 步骤通过。Drawbridge 跟踪文件无改动；原有
7 个未跟踪 `.pre-*` 备份保留。Gateway 和 Runner 均为 active、running，重启计数为 0。

MCP `ops_catalog`、应用注册、首次发布以及 3A/3B 的计划从 t4 客户端发起。按外部客户端场景调整后，
从 3B apply 的同幂等键重试开始，其余 MCP 工具调用由本机客户端直接请求
`192.168.0.67:8787/mcp`，未使用 SSH 隧道。测试配置 SHA-256 为
`cb5014e3be73e6aeeb74aca6657018daa60e508cc8e624e756cdf4e14cc04743`。Gateway 与 Runner 使用同一个
`mineru_dev` user service 账号和配置；账号属于 `docker` 组。管理员目录为
`/etc/drawbridge/apps/operator-smoke`，属主/属组为 `root:mineru_dev`，目录权限 `0750`，
三个文件均为 `0640`；服务账号可读而不可直接写，路径中没有符号链接。该主机没有为 Gateway
与 Runner 提供不同的 Unix 身份或独立的 Docker 最小权限边界，此项结果仅证明直接文件权限检查。

测试源仓库为 `/data1/zsj/projects/drawbridge-operator-smoke/repo`，仅由 ContractLens 已提交文件
形成独立 Git 仓库，`main` commit 为
`9853fbe814565c284d112a85181c89ea810da312`，`origin` 与登记值一致。用户给出的原始
`/home/mineru_dev/projects/ContractLens`（规范路径 `/data1/zsj/projects/ContractLens`）没有被修改；
其分支仍为 `codex/unify-inference-stack`、HEAD 仍为
`b82488da940db3eec58f3c08c14f12312d68ef08`，原有未跟踪备份保留。测试使用本地镜像
`docker.m.daocloud.io/library/node:24.15.0-alpine`，镜像 ID 为
`sha256:edd927012c1ea203e392a59f0ae655a9e50a67bf3a5cb42dc1c638889a03a3b0`。

应用注册为 `operator-smoke/staging`，binding version 为 1，固定服务集合为 `app`。MCP 发布记录：

- 首次发布：plan `e418d8b2-936e-4054-b48f-6e90d2f26776`，job
  `c4531aa2-487d-4605-ae7b-88243327015e`，release
  `003c658a-1b09-4dfd-9db1-0474d70e5f5b`；succeeded，`built_images` 为空。
- 3A 管理员环境文件改为标记 `two` 后，无需改 SHA、配置、重启或重新注册即可重新 plan/apply：plan
  `98db8c76-0453-4086-9bf0-9c80ddef5d8f`，job
  `4dad20da-1434-4e9a-ac0e-b5de5e5e61f9`，release
  `55492eef-1bd2-4afc-83fc-5ca1843a839b`；succeeded，两个标记检查退出码均为 0。
- 3B 管理员 Compose 增加 revision label `two` 后，同样无需重新注册或批准新 SHA：plan
  `f3cc4fde-8781-4915-8c5d-4d50e80daab3`，job
  `55b98a59-bb84-49b3-a2f8-ced721ad61ee`，release
  `2763bcf8-d86d-4ff8-ad24-53e87a2c40fc`；succeeded，label 在容器上可见。
- 4.1 先为文件状态创建旧 plan `3af84901-87c9-4ffd-9215-45d7c1301ac3`，再只把
  `runtime.env` 改为 `three`。旧 plan 对应 job
  `8efbc2fe-193b-4f1a-8ab0-260513db53b6` 以 `STALE_PLAN` 失败，当前 release 仍为
  `2763bcf8-d86d-4ff8-ad24-53e87a2c40fc`，原容器仍运行且原标记检查通过。新 plan
  `9dab65ff-365e-4ae1-b5ee-eb4078a7d8d3`、job
  `1e8ad813-bbaa-4aaa-83d6-7900c82ada19`、release
  `d42b6c04-8495-4f8b-8661-4aed8328d4a7` 均成功；`INTERP_MARKER=two` 和
  `FILE_MARKER=three` 的只返回退出码检查均为 0，容器 running，revision label 仍为 `two`。
- 4.2 在隔离仓库 `README.md` 追加未提交探针时创建 plan
  `8f0b8f1f-dd9e-4ae1-9bea-087ca00a0f40`；返回 SHA 仍为原 `main` commit，工作区探针不在该
  commit 的 `git archive` 中。探针随后被精确移除，隔离仓库工作树恢复干净；没有 apply。
- 4.3 临时添加第二个 Compose 服务时，plan 按预期返回
  `UNSUPPORTED_SERVICE_CHANGE`，没有生成可执行 plan，也没有 apply。恢复单服务文件后，新 plan
  `ac5e5352-c353-485a-a195-2e540a87634b` 成功，services 仍为 `app`，Compose digest 恢复为
  `ec1f1caba5cd5696b491c3f7010c69dc8524236666258279dc3cf79309199618`。

所有成功发布均使用相同本地镜像 ID，`built_images` 为空，健康检查为 `runtime_only/passed`；
受检 Runner 日志中没有 BuildKit、镜像导入或拉取步骤。MCP 响应扫描未发现 `.env` 或
`runtime.env` 文件内容。最终 `ops_status` 仍指向 release
`d42b6c04-8495-4f8b-8661-4aed8328d4a7`，容器保持运行在隔离项目
`drawbridge-operator-smoke-staging` 中；没有对该项目执行 down/prune，也没有切换或重启
ContractLens 业务应用。

本结果仅支持结论“t4 上的独立 `operator-smoke` 应用通过本计划的管理员维护 Compose 核心验收”。
没有执行可选的入口文件名切换、权限/符号链接负例、GPU/NPU、BuildKit 构建、回滚或中断恢复；
也不证明原始 ContractLens 应用已通过管理员模式验收。测试容器和 release 保留以供复盘，后续停用
由管理员核对证据后决定。

## 28. 管理员 Compose 文档边界修订（本地验证，2026-09-23）

在 commit `8af61237ac22488051a64aef314299f24ad29406` 的工作树上修订文档：
`.harness/context/ARCHITECTURE.md` 记录 Docker 部署失败时错误输出可能携带管理员 `.env`
插值值的剩余风险、当前内网运维前提和扩大使用范围前的处理条件；README 与服务器验收计划
同步限定响应扫描的覆盖范围，并把管理员模式的 t4 验收状态更新为第 27 节实际结论。
未修改产品代码，也未在本次文档修订后重新执行服务器部署。

本地 macOS arm64、Python 3.14.5 执行 `uv sync --frozen --extra dev` 和完整
`.venv/bin/python scripts/verify.py`，报告为
`var/verification/20260923T100117Z/report.json`，`result: passed`；Ruff、格式、mypy、
编译、pytest、隔离 self-check 和 simulation 均通过。该本地结果不消除上述 Docker
异常失败路径的潜在泄露，也不扩大第 27 节的真实服务器验收范围。

## 29. simulation 绑定切换 Docker 的重新注册（本地验证，2026-09-23）

在 commit `653d07ea67fed74a09486d93cc58ba912077825a` 的工作树上复现：同一 SQLite
binding 先以 `simulation` 注册，再用 `docker` 配置和新 Service 实例重新注册，返回版本仍为
1，数据库保留 `simulation`。新增聚焦测试先两次稳定失败于版本断言（期望 2，实际 1）。
将 `deployment_mode` 纳入注册时的运行策略比较后，该测试通过；同时验证新 binding 为
`docker`、旧 simulation plan 返回 `STALE_PLAN`、新 plan 可创建、同配置再注册不重复增版。

本地 macOS arm64、Python 3.14.5 执行 `uv sync --frozen --extra dev` 和完整
`.venv/bin/python scripts/verify.py`，报告为
`var/verification/20260923T100858Z/report.json`，`result: passed`；Ruff、格式、mypy、
编译、85 个 pytest、隔离 self-check 和 simulation 均通过。本次未在真实服务器上更新
Drawbridge 或重新注册已有应用；已有 SQLite binding 仍需在新版本运行后主动重新注册。
