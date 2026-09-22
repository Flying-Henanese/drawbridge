# 发布调用流程

本文用于调用或修改当前 MCP 发布路径。组件与权限关系见
[当前系统架构](ARCHITECTURE.md)；服务器准备和真实验收见
[运维文档](../../docs/OPERATIONS.md)。

## 前提

- 先调用 `ops_catalog` 获取服务器当前工具和限制。目标环境目前只能是 `staging`。
- 项目须有本地 `.git` 目录、唯一 `origin`、至少一个 commit，以及可通过校验的
  Compose 文件。传给注册操作的 `project_dir` 必须是管理员允许的真实绝对路径，不能
  含符号链接。构建服务还须匹配已登记的 BuildKit profile。
- `ops_app_register` 失败时不会形成可操作的应用绑定。修正源项目、Compose 或管理员
  配置后再注册；`ops_workspace_patch` 只能作用于已登记的可编辑文件。

## 按顺序调用

1. **注册**：尚未绑定时调用 `ops_app_register(app, project_dir, compose_file,
   idempotency_key, profile)`。服务端检查路径、Git、Compose 和构建声明，并记录绑定。
2. **计划**：调用 `ops_release_plan(app, git_ref, source_mode)`。`git_ref` 使用完整
   `refs/heads/...`、`refs/tags/...` 或允许的 40 位 SHA；`fetch` 仅接受分支或标签，
   `local` 可使用本地已有提交。若要发布登记过的配置 revision，同时传
   `workspace_revision`。保存返回的 `plan_id` 和 `commit_sha`，计划 15 分钟后过期。
3. **排队**：调用 `ops_release_apply(plan_id, idempotency_key)`，保存 `job_id`。重试同一
   请求时复用幂等键。`status: ok` 表示任务已入队或复用已有任务，不表示部署成功。
4. **查询**：轮询 `ops_release_status(job_id)`，直到任务进入终态。成功后核对
   `release_id`、`source_sha`、`mode`、`health` 和构建制品信息；再按需使用
   `ops_status`、`ops_logs`、`ops_http_request` 取得业务运行证据。

## Runner 实际执行的步骤

Runner 从共享 SQLite 队列认领部署 job，从计划中的 SHA 创建发布快照，并重新校验
Compose。simulation 模式只写发布记录。Docker 模式对登记的 `build:` 服务依次调用
BuildKit、导出 archive、`docker image load`、查验 image ID，然后以引用 image ID 的
运行时 Compose 执行 `docker compose up --no-build --pull never --wait`。已有 `image:`
服务不经 BuildKit。最后保存 release 和健康检查结果。构建流程没有单独的 MCP 工具。

## 失败与后续操作

- 注册错误通常指向路径、Git origin、Compose 或 profile；检查错误字段后修正输入。
  特权键如 `privileged: true`、`network_mode: host` 和 `devices` 默认不能通过注册；管理员可在
  runtime profile 中精确登记支持的能力，或批准一份既有 Compose 文件的 SHA-256。
- 计划过期、绑定或构建 profile 改变、当前基线变化时应重新创建计划，不复用旧 `plan_id`。
- 构建失败发生在 Compose 更新前；Compose 更新开始后失败，release 制品会保留供检查，
  容器现场可能已有变化。通过 job 错误、发布目录和容器状态判断实际情况。
- 当前 Docker 发布失败不会自动回滚，Docker release 的显式回滚也未实现；
  `ops_release_rollback` 当前仅能处理 simulation release。不要把失败 job 当成恢复完成。

本地 simulation 只能验证请求、队列和记录路径。真实构建、镜像源连通性、Docker
权限、端口与健康检查需要在服务器上单独验收，范围见
[验证说明](../validation/README.md)。
