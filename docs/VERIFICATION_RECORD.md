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
