# 管理员维护 Compose 模式：服务器验收计划

本计划用于在目标 Linux 服务器验收 `operator_compose`。先使用独立的 `operator-smoke`
staging 应用，不修改正在运行的业务应用。执行人逐步记录证据；任一步失败时停止后续发布，
保留现场。服务器安装与 MCP 连接方式以 [OPERATIONS.md](OPERATIONS.md) 和
[MCP_CLIENTS.md](MCP_CLIENTS.md) 为准。历史 t4 结果见 [VERIFICATION_RECORD.md](VERIFICATION_RECORD.md)，
不视为本次验收结果。

## 验收目标与边界

| 目标 | 通过标准 |
| --- | --- |
| 管理员文件来源 | Compose、`.env`、服务级 `env_file` 来自管理员目录；代码来自指定 Git commit |
| 免重复审批 | 改动管理员文件内容后，只需重新 plan；不改批准 SHA，不重启服务，不重新注册 |
| 计划一致性 | 文件在 plan 与 Runner 执行之间变化，任务以 `STALE_PLAN` 失败，当前 release 不变 |
| 环境变量 | `.env` 插值和服务级 `env_file` 均进入容器；MCP 响应和事件不返回文件内容 |
| 镜像 | Runner 使用已装入本地 Docker Engine 的预建镜像 ID；不构建、不拉取 |
| 权限 | Gateway/Runner 可读管理员文件，但不能写；`editable_files` 重叠由本地回归测试覆盖 |
| 兼容 | Git Compose 的严格/摘要批准模式由本地 harness 回归测试覆盖；服务器检查固定服务集合约束 |

本地 harness 已在 2026-09-23 通过（见 `var/verification/20260923T063927Z/report.json`），
但没有运行真实 Docker。此计划的服务器结果应另附时间、主机、Drawbridge commit、配置版本、
计划与任务 ID、发布 ID 和必要的脱敏日志。

## 1. 执行前准备

1. 记录服务器名、Drawbridge 仓库路径、当前分支与 commit、`git status --short`、
   Gateway/Runner 的 systemd 单元和实际配置路径。若服务器工作树已有改动，先记录并保留，
   不用 `git reset` 或 `git clean` 清理。
2. 将本分支的提交按现有部署流程放到服务器。检查目标 commit 后执行：

   ```sh
   uv sync --frozen --extra dev
   .venv/bin/python scripts/verify.py
   ```

   保存打印的 `report.json` 路径；检查 `result: passed` 和相邻步骤日志。simulation 通过后才
   进入真实 Docker 验收。
3. 读取当前 `allowed_project_roots`、`managed_release_root`、`state_dir`、Gateway/Runner
   systemd 的 `User`、`Group`、`ProtectHome`、`ReadOnlyPaths` 和 `ReadWritePaths`。
   选择受允许项目根目录中的**独立测试 Git 仓库**，记为 `<SMOKE_REPO>`。该目录须有真正的
   `.git` 目录、一个 `main` commit 和唯一 `origin`；记录 origin，并使两份 Drawbridge 配置
   的 `apps.operator-smoke.git` 与之完全一致。测试使用 `source_mode="local"`，无需访问 origin。
4. 选择 Runner 所用 Docker Engine 中已有的非敏感镜像 `<SMOKE_IMAGE>`。它须有 `sh` 与
   `sleep`，且 `docker image inspect <SMOKE_IMAGE>` 成功；若不存在，由管理员按现有镜像流程
   手动装入。记录镜像 ID；本测试不调用构建或拉取。以 Runner 用户检查 `docker info` 和
   `docker image inspect`。Gateway 不需要 Docker daemon 权限。
5. 在 `/etc/drawbridge/apps/operator-smoke`（或同等管理员路径）准备三个文件。目录必须位于
   `allowed_project_roots`、状态目录和发布目录之外；从 `/` 到这三个文件的任一父路径均不能
   是符号链接，不能由 Gateway/Runner 用户持有或由组/其他用户写入。建议目录权限 `0750`、
   文件权限 `0640`，属主为管理员，属组是两个服务用户均可读取的组。分别以两服务用户检查
   可读且不可写；不要在检查输出中打印 `.env` 或 `runtime.env` 内容。

   `compose.yaml`：

   ```yaml
   services:
     app:
       image: ${SMOKE_IMAGE}
       env_file: runtime.env
       environment:
         INTERP_MARKER: ${INTERP_MARKER}
       command: ["sh", "-c", "sleep 3600"]
   ```

   `.env` 填入 `SMOKE_IMAGE=<SMOKE_IMAGE>` 和 `INTERP_MARKER=one`；`runtime.env` 填入
   `FILE_MARKER=one`。这些标记均为非敏感测试值。一个环境只配置一个 Compose 入口文件。
6. 给 Gateway 和 Runner 的管理员配置加入相同的应用与环境参数，保留各自现有认证配置：

   ```yaml
   apps:
     operator-smoke:
       git:
         repo_path: <SMOKE_REPO>
         origin: <SMOKE_ORIGIN>
       environments:
         staging:
           project_name: drawbridge-operator-smoke-staging
           deployment_mode: docker
           operator_compose:
             directory: /etc/drawbridge/apps/operator-smoke
             file: compose.yaml
   ```

   `<SMOKE_REPO>`、`<SMOKE_ORIGIN>` 必须换成真实值。初次修改管理员配置后，按
   [OPERATIONS.md](OPERATIONS.md) 分别运行 Gateway/Runner `self-check`，再重启两服务并确认
   `active`。此后测试文件内容的修改不得再重启服务。

## 2. 首次发布

1. 使用实际 MCP 连接执行 `ops_catalog()`，确认 Gateway 可用。
2. 调用 `ops_app_register(app="operator-smoke", environment="staging",
   project_dir="<SMOKE_REPO>", compose_file="compose.yaml", profile="default",
   idempotency_key="register-operator-smoke-001")`。确认服务集合恰为 `app`，记录 binding version。
3. 调用 `ops_release_plan(app="operator-smoke", environment="staging",
   git_ref="refs/heads/main", source_mode="local")`。记录 `commit_sha`、`plan_id`、`services`、
   `compose_digest`；确认它不回传 `.env` 或 `runtime.env` 内容。
4. 调用 `ops_release_apply(plan_id="<PLAN_ID>", idempotency_key="apply-operator-smoke-001")`；
   轮询 `ops_release_status(job_id="<JOB_ID>")` 至终态。应为 `succeeded`，`built_images` 为空，
   `image_services.app` 为 `sha256:...` 镜像 ID。检查 release 目录的运行时 Compose 引用此 ID，
   Runner 日志没有 `buildctl`、`docker image load` 或拉取步骤。
5. 用 Docker 的 Compose project/service 标签找到测试容器，确认容器正常运行。以实际容器
   ID 替换 `<CONTAINER_ID>`，执行只返回退出码的检查；后续 `two`、`three` 同样检查：

   ```sh
   docker exec <CONTAINER_ID> sh -c 'test "$INTERP_MARKER" = "one"'
   docker exec <CONTAINER_ID> sh -c 'test "$FILE_MARKER" = "one"'
   ```

   不要执行会打印完整环境变量的 `env`、`docker inspect` 环境字段或不加过滤的
   `docker compose config`。

## 3. 人工修改文件后的发布

3A、3B 只改管理员目录中的文件；**不改 Git commit、批准 SHA 或 Drawbridge 配置，不重启，
不重新注册**。3C 专门验证入口路径变更，需更新配置并重启、重新注册。遵守当前
`min_deploy_interval_seconds`，等冷却时间结束后再 apply。

| 步骤 | 操作 | 预期结果 |
| --- | --- | --- |
| 3A：环境文件 | 将 `.env` 中 `INTERP_MARKER` 改为 `two`，将 `runtime.env` 中 `FILE_MARKER` 改为 `two`，保持原权限 | 新 plan → apply 成功；新容器两个标记均为 `two`；旧 commit SHA 可保持不变 |
| 3B：Compose | 在同一服务加一个无害 label（例如 `drawbridge.smoke.revision: "two"`），保持服务名 `app` | 新 plan → apply 成功；运行时 Compose 包含该 label；无需更新 `approved_compose_digests` |
| 3C：入口文件名 | 复制同内容为 `docker-compose.yaml`，把两份管理员配置的 `operator_compose.file` 改为新文件名，重启并重新注册 | binding version 增加，之后 plan 使用新入口；这是配置路径变更，不属于日常内容修改 |

每次保存 `plan_id`、`job_id`、`release_id`、响应状态与健康证据。3C 可在其他测试完成后再做，
也可仅在独立测试应用上执行；业务应用无需为了本次验收切换入口文件名。

## 4. 旧计划与隔离边界

1. 为当前管理员文件创建 plan，记录 `plan_id`，**不要立即 apply**。随后只把
   `runtime.env` 的 `FILE_MARKER` 从 `two` 改为 `three`，保持服务名和文件权限。对旧 plan
   执行 apply 并等待 job 终态：应为 `STALE_PLAN`，且当前 release ID 与测试容器状态不变。
   然后重新 plan → apply，应成功并读到 `three`。
2. 在测试 Git 仓库中改动一个普通代码/README 文件但不提交，再 plan。记录的 `commit_sha`
   仍应是原 commit，发布快照不应包含这次工作区改动。恢复测试文件或单独提交新 commit 后，
   可验证新 commit 会进入下一次发布。不要修改服务器上的其他仓库。
3. 在管理员 Compose 中临时增加第二个服务或改名，调用 plan：应返回
   `UNSUPPORTED_SERVICE_CHANGE`，不应生成可执行的新 plan。恢复原文件并重新 plan。
4. 可选的权限负例仅在测试应用执行：将服务级 `env_file` 临时指向 `../outside.env`，
   plan 应拒绝；恢复后继续。将管理员目录改成符号链接或赋予服务用户写权限也应在
   register/plan 阶段被拒绝，但这会改变服务配置路径/权限，须由管理员按变更窗口单独做，
   不作为首次验收的必要步骤。

## 5. 证据、通过标准与收尾

| 证据 | 保存内容 |
| --- | --- |
| 版本与配置 | 服务器、时间、Drawbridge commit、实际配置路径、测试仓库 commit、服务用户与受控目录权限；不保存环境文件内容 |
| Harness | `report.json` 路径、`result`、失败时相邻步骤日志 |
| MCP | 注册版本、每次 plan/job/release ID、终态和错误码；响应中不得有环境文件内容 |
| Docker | 运行时 Compose 中的镜像 ID、容器状态、两个标记的退出码；不打印完整容器环境 |
| 隔离 | 旧计划 `STALE_PLAN`、服务集合变化被拒绝、失败前后当前 release ID 一致 |

全部必要步骤通过后，结论只能写为“该服务器上的独立测试应用通过管理员维护 Compose 模式
验收”；不能推论其他应用、GPU/NPU、BuildKit、回滚或中断恢复已通过。将结果和限制以日期
追加到 [VERIFICATION_RECORD.md](VERIFICATION_RECORD.md)。

测试容器与测试应用的停用由管理员在核对证据后决定，只针对
`drawbridge-operator-smoke-staging` 项目操作；不要运行影响其他 Compose 项目的 `down`、
`prune` 或批量删除。保留此次 release/报告直到完成复盘。业务应用切换到本模式时，应另行
核对其镜像、挂载、健康检查和现有容器的实际影响。
