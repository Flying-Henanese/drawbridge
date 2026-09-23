# Drawbridge

Drawbridge 是用于部署和运行观测的 MCP 服务。它由两个 Python 进程组成：Gateway 在
`/mcp` 提供 Streamable HTTP 接口，Runner 从共享 SQLite 队列执行任务；Drawbridge
自身不是一个 Docker Compose 应用。下文是 Linux 服务器上的安装、启停和接入流程。

## 设计灵感：来自《死亡搁浅》的“棒与绳”

Drawbridge 的这个设计灵感来自小岛秀夫的《死亡搁浅》，其中“棒与绳”的意象源自安部公房
作品：棒让人和威胁保持距离，绳把珍视之物连接起来。对 Drawbridge 来说，Gateway 承担“棒”
的作用，在 MCP 客户端与服务器部署能力之间建立边界：它校验请求来源，只开放登记好的操作，
不把任意 Shell 或 Docker 命令交给客户端。

Gateway 与 Runner 通过共享 SQLite 队列协作，这条受控通路则像“绳”：Gateway 把获准的
变更请求写入队列，Runner 执行后写回结果，客户端再通过 Gateway 查询状态。这样，智能体可以
连接并操作远端服务，而不需要直接持有宿主机的任意执行权限。Gateway 仍会处理部分 Git 操作
和 Docker 只读查询；实际构建与 Compose 变更由 Runner 执行。

## 当前能力边界

- `config.example.yaml` 默认启用 **simulation**：可以验证注册、计划、排队和发布记录，
  但不会启动业务容器。
- 真实 Docker 路径可使用服务器已有镜像，也可对已登记的 `build:` 服务通过独立的
  rootless BuildKit socket 构建。Runner 导入构建产物并用实际镜像 ID 执行
  `docker compose up --no-build --pull never`；构建目录和 Dockerfile 必须匹配管理员 profile。
- 发布计划先从解析后的完整 Git SHA 创建临时快照，在应用显式 workspace revision 后校验
  Compose、服务集合和 build 声明。Runner 从同一 SHA 重建最终快照并核对指纹；分支前进或
  未提交的工作区修改不会改变已有计划。
- t4 已完成可信 Compose 使用预建镜像的真实 Docker 验证；完整 BuildKit、回滚和中断恢复
  仍未验收。参见 [docs/VERIFICATION_RECORD.md](docs/VERIFICATION_RECORD.md)。

## 本地验证

在仓库根目录运行：

```sh
uv sync --frozen --extra dev
.venv/bin/python scripts/verify.py
```

脚本会在系统临时目录生成一次性的 Git/Compose 验证输入，执行静态检查、测试、自检和
simulation 全流程；不需要仓库内的示例业务应用，也不会拉取镜像或启动容器。它会输出
`var/verification/<UTC 时间>/report.json` 路径；请检查其中的 `result`，失败时再看同目录
的步骤日志。验证范围和真实服务器验收的区别见 [.harness/README.md](.harness/README.md)
与 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

## 1. 准备服务器

服务器需要 Linux、systemd、Python 3.12+、Git 和 `uv`。若要运行真实业务容器，
还需要 Docker Engine、Docker Compose v2；若由 Drawbridge 构建镜像，还需要 `buildctl`
和单独安装的 rootless BuildKit daemon。
先用普通用户检出项目；以下路径是示例，所有路径均需与实际服务器一致：

```sh
cd /opt/drawbridge
uv venv --python 3.12 .venv
uv sync --frozen
.venv/bin/python --version
docker compose version
```

`/opt/drawbridge` 应由管理员管理，两个服务用户只需读取程序及虚拟环境。业务 Git
仓库另放在例如 `/srv/projects/<app>`，应已有 `origin` 和至少一个 commit。当前实现中，
Gateway 执行注册、Git 查询和发布计划：`local` 模式需要它读取仓库，`fetch` 模式还需要
它写入仓库并访问 origin。Runner 需要读取同一仓库，才能从计划冻结的 SHA 导出发布快照。

实际镜像构建、导入和 Compose 更新只由 Runner 执行。Gateway 只有在调用
`ops_app_discover` 或读取 Docker release 的 `ops_logs` 时才会访问 Docker；不给 Gateway
Docker daemon 权限可以保留更小权限面，但这两个工具会不可用。Docker socket 通常等同宿主机
高权限，不应为了启用只读工具而默认把 Gateway 加入 `docker` 组。严格的内部权限分离尚未
完成，当前限制和后续迁移见 [.harness 当前架构](.harness/context/ARCHITECTURE.md)。

## 2. 配置与权限

以 [config.example.yaml](config.example.yaml) 为字段模板，准备管理员控制的配置。
将 `state_dir`、`log_dir`、`managed_release_root`、`managed_template_root`、
`managed_data_root` 和 `allowed_project_roots` 改为服务器上的绝对路径，例如共享状态
目录 `/var/lib/drawbridge/state`、发布目录 `/srv/drawbridge/releases`、项目根目录
`/srv/projects`。Gateway 和 Runner 必须使用**相同的状态库及部署参数**。

### 进程间通信：部署时必须核对

Gateway 与 Runner **不通过 HTTP 互相调用**。Gateway 将部署、回滚、重启等变更任务
写入 `state_dir/state.db` 并返回 `job_id`；Runner 默认每秒轮询同一个 SQLite 数据库，
认领任务并写回结果；Gateway 再通过 `ops_release_status(job_id)` 读取结果。因此：

1. 两份配置的 `state_dir` 必须指向**完全相同的绝对目录**。如果写成两个路径，两个
   进程会各自创建 `state.db`：Gateway 看得到已排队的任务，Runner 却永远认领不到。
2. 两个服务用户都必须能读写状态目录及 `state.db`、`state.db-wal`、`state.db-shm`。
   使用本机文件系统，不要把 SQLite WAL 状态库放在网络文件系统上。下面的共享组与
   `UMask` 配置正是为此准备的。
3. 两份配置除 Runner 专用的认证差异外，还应保持项目根目录、发布目录、运行模式和
   队列设置一致。自检时比较两份输出中 `state_directory.detail` 的路径是否完全相同；
   启动后再用一次 `plan → apply → status` 验证实际入队和消费。
4. Runner 停止时，Gateway 仍可能接受新任务，但任务只会等待，超过
   `concurrency.queue_timeout_seconds` 后会过期；默认等待上限为 600 秒。
   `apply` 的 `status: ok` 仅表示请求已接受，最终结果以 job 状态为准。

Gateway 在进程启动时初始化一次数据库 schema；并发 MCP 请求不会重复执行初始化。单个
进程内的 SQLite API 使用短事务串行化，Gateway 与 Runner 的独立连接再由 WAL 写锁协调。

推荐使用两份配置，以便仅 Gateway 读取 bearer token：

- `/etc/drawbridge/gateway.yaml`：`auth.mode: token`，用 `auth.token_file` 指向仅
  Gateway 用户可读的 token 文件，不使用示例的固定 `auth.token`。填写
  `allowed_client_cidrs`、`auth.allowed_hosts` 和需要的 `auth.allowed_origins`。
- `/etc/drawbridge/runner.yaml`：复制相同的状态、项目、构建和 HTTP 策略字段，
  但设 `auth.mode: none`，且不配置 token。这份配置**只供没有网络监听端口的 Runner
  使用**；绝不能传给 Gateway。下方 systemd 模板的 Runner `--config` 路径需随之修改。

两份配置的认证部分分别形如：

```yaml
# gateway.yaml：删除示例中的 auth.token
auth:
  mode: token
  token_file: /etc/drawbridge/gateway.token
  allowed_hosts: [127.0.0.1, localhost]
```

```yaml
# runner.yaml：只供无监听端口的 Runner 使用
auth:
  mode: none
```

两份配置由管理员持有且服务用户只能读取；token 文件权限设为 `0600`，属主为 Gateway
用户。Gateway 与 Runner 都要能读写同一个 SQLite 状态目录及其 WAL/SHM 文件；可用
专用共享组和目录 `2770` 权限实现；两个 systemd 模板都设 `Group=drawbridge`、
`UMask=0007`，Runner 另通过 `SupplementaryGroups=docker` 获得 Docker 权限。状态目录应由
`drawbridge` 组持有并启用 setgid。Runner 还需写发布目录并读取已登记的源码仓库。当前
Gateway 在 `source_mode: fetch` 的计划路径中需要写登记仓库，所有计划都会在状态目录创建
临时快照；如模板启用 `ProtectSystem=strict`，须按使用的 `source_mode` 把这些**准确路径**
加入对应单元的 `ReadWritePaths=`。不要把整个 `/srv` 或用户主目录设为可写。

先决定运行模式；**不要把上面的片段当作完整配置**，其余字段应从示例复制并按实际
服务器填写：

- **仅验证接口和队列**：保留示例的 `allow_simulation: true` 与
  `build_profiles.default.mode: simulation`；这不会部署容器。
- **使用现有镜像部署**：设置 `allow_simulation: false`，登记的 Compose 服务只使用
  `image:`，将默认 `build_profiles.default.mode` 改为 `prebuilt`，并提前在 Runner
  使用的 Docker Engine 中准备好镜像。
- **由 Drawbridge 构建后部署**：设置 `allow_simulation: false`，使用 `buildkit` profile，
  配置本机 `unix:///.../buildkitd.sock` 和每个构建服务的 `targets`。安装及配置示例见
  [docs/OPERATIONS.md](docs/OPERATIONS.md)。`self-check` 检查构建工具和 socket，
  仍须单独完成真实的构建、导入、Compose 和健康检查验收。

在启动前分别**以对应服务用户**执行自检，并查看 JSON 中的 `ok` 和每一项
`blocking`/`ok`，不能仅凭命令退出码判断：

```sh
cd /opt/drawbridge
.venv/bin/drawbridge self-check --config /etc/drawbridge/gateway.yaml --role gateway
.venv/bin/drawbridge self-check --config /etc/drawbridge/runner.yaml --role runner
```

`self-check` 不连接 Docker daemon、不检查目标镜像，也不验证登记仓库的属主或 Git fetch
权限。真实 Docker 模式还要以 Runner 用户执行 `docker info` 和逐个 `docker image inspect`，
以 Gateway 用户验证选定的 Git source mode；只有确实要启用发现/日志工具时，才以 Gateway
用户单独验证 Docker 连接。`--role gateway` 会跳过 Runner 专属的 BuildKit 工具和 socket
检查；`--role runner` 会把已配置的 BuildKit 条件作为阻断项。两者都不能证明 BuildKit
daemon 确实 rootless，真实构建仍需按运维文档验收。省略 `--role` 保留兼容行为并检查全部项目。

## 3. 安装和启动 systemd 服务

[Gateway 模板](deploy/systemd/drawbridge-gateway.service) 和
[Runner 模板](deploy/systemd/drawbridge-runner.service) 需要先按本机用户、路径和权限调整：

1. Gateway 的 `ExecStart` 指向 `gateway.yaml`。推荐把 `--host 0.0.0.0` 改为
   `--host 127.0.0.1`，通过 SSH 端口转发访问；模板默认监听所有网卡，不能原样用于
   公网。当前 MCP transport 还有独立的 Host 校验，直接使用未加入白名单的远端域名
   可能返回 `403` 或 `421`。
   如果客户端与服务器位于同一受信任内网并且可以直连，也可以让 Gateway 监听服务器
   内网 IP（或保留 `0.0.0.0`），但必须在防火墙中只放行可信客户端，并同步把客户端
   地址加入 `allowed_client_cidrs`、把客户端实际使用的服务器 IP/域名加入
   `auth.allowed_hosts`。不要因为可以直连就把 `8787` 暴露到公网；跨不可信网络时应使用
   HTTPS 反向代理或 VPN。
2. Runner 的 `User=` 改为能读取业务仓库、写发布目录且拥有所需 Docker 权限的普通用户，
   `ExecStart` 指向 `runner.yaml`。Gateway 的 `User=` 需要按选定 source mode 获得仓库读权限
   或 fetch 所需的写权限。按上节补齐两个服务的共享状态目录权限、`UMask` 和精确的
   `ReadWritePaths`；Gateway 模板只注释展示单个仓库 `/srv/projects/example-app`，启用 fetch
   时应为每个已登记仓库逐项替换或追加，不能放宽整个项目根目录。
3. 将修改后的单元文件安装为 `/etc/systemd/system/drawbridge-gateway.service` 和
   `/etc/systemd/system/drawbridge-runner.service`，然后执行：

```sh
sudo install -m 0644 deploy/systemd/drawbridge-gateway.service /etc/systemd/system/drawbridge-gateway.service
sudo install -m 0644 deploy/systemd/drawbridge-runner.service /etc/systemd/system/drawbridge-runner.service
sudo systemctl daemon-reload
sudo systemctl enable --now drawbridge-runner.service drawbridge-gateway.service
sudo systemctl status drawbridge-runner.service drawbridge-gateway.service
```

两个单元均设为 `Restart=on-failure`，开机启用后由 systemd 管理。停止时先停 Gateway
以阻止新请求，再停 Runner；修改配置后重启两个服务：

```sh
sudo systemctl stop drawbridge-gateway.service drawbridge-runner.service
sudo systemctl restart drawbridge-runner.service drawbridge-gateway.service
sudo journalctl -u drawbridge-gateway.service -u drawbridge-runner.service -f
```

重启前先确认没有正在运行的部署 job；停止 Runner 不等于业务容器自动停止。

## 4. 连接 MCP 并验证

### 方式 A：SSH 端口转发（推荐）

当 Gateway 只监听服务器 `127.0.0.1` 时，在客户端建立 SSH 端口转发：

```sh
ssh -N -L 8787:127.0.0.1:8787 user@server.example.com
```

将编程智能体的 Streamable HTTP MCP 地址设为 `http://127.0.0.1:8787/mcp`，并发送
`Authorization: Bearer <gateway token>`。`allowed_client_cidrs` 和
`auth.allowed_hosts` 要允许通过转发到达的 loopback 地址及 `127.0.0.1` Host。
Codex CLI 和 Claude Code 的具体配置见 [docs/MCP_CLIENTS.md](docs/MCP_CLIENTS.md)。

### 方式 B：服务器直连（仅限受信任网络）

如果客户端能够直接访问服务器，可以不建立 SSH 隧道。此时 Gateway 必须监听服务器
内网地址，客户端把 MCP URL 改为服务器地址，例如：

```text
http://10.0.0.10:8787/mcp
```

配置示例（请替换为实际客户端地址和服务器域名；`allowed_hosts` 不包含端口）：

```yaml
allowed_client_cidrs:
  - 10.0.0.25/32
auth:
  mode: token
  token_file: /etc/drawbridge/gateway.token
  allowed_hosts:
    - 10.0.0.10
    - drawbridge.internal.example.com
```

若通过域名访问，就把该域名加入 `auth.allowed_hosts`；若通过 IP 访问，就加入该 IP。
直连 HTTP 只适合受防火墙、内网或 VPN 保护的链路；跨公网或不可信网络时，应在 Gateway
前配置 HTTPS/TLS 反向代理，并让客户端使用 `https://.../mcp`。无论哪种接入方式，都
必须发送 `Authorization: Bearer <gateway token>`，并先调用 `ops_catalog`。

连接后先调用 `ops_catalog`；再按 `ops_app_register`（未登记时）→
`ops_release_plan`（使用完整 Git ref，例如 `refs/heads/main`）→
`ops_release_apply(plan_id, idempotency_key)` → `ops_release_status(job_id)` 的顺序操作。
`apply` 返回 `job_id` 只表示任务已排队，要等状态成为 `succeeded` 后再检查
`ops_status`、`ops_logs` 和需要的 `ops_http_request` 证据。重试同一变更时复用
`idempotency_key`。
初次在预克隆仓库上验证可将 `source_mode` 设为 `local`；默认的 `fetch` 还要求
Gateway 能在 systemd 的 `ProtectHome=true` 限制下访问所需 Git 凭据并写登记仓库。

### 已审核的 Compose 使用现有镜像启动（不构建）

此前被默认 Compose 策略拒绝的特权字段、宿主能力或动态插值，可以由管理员审核后按
Compose 文件摘要批准。此设置会信任该文件的完整内容，应先检查文件中的所有服务、挂载、
端口、权限、环境变量和它引用的配置文件；SHA-256 只覆盖 Compose 文件本身。

1. 确认 Compose 中每个服务都有 `image:`。若服务同时有 `build:` 和 `image:`，启用
   `prefer_prebuilt_images` 后仍可运行；只有 `build:`、没有 `image:` 的服务会被拒绝。
   将所有 `image:` 对应的镜像提前放入 **Runner 所使用的 Docker Engine**，引用名称或摘要
   必须完全匹配；运行时不会拉取缺失镜像。
2. 管理员检查将要发布的 Git 版本中的 Compose 文件，并计算其原始文件字节摘要：

   ```sh
   sha256sum /srv/projects/my-app/compose.yaml
   ```

   将输出的 64 位小写 SHA-256 分别填入 Gateway 和 Runner 的管理员配置，并保持两份配置一致。
   下例只展示需要增加或修改的字段；保留配置中已有的其他 app、Git 和环境设置：

   ```yaml
   allow_simulation: false
   build_profiles:
     default:
       mode: prebuilt

   runtime_profiles:
     approved-compose:
       approved_compose_digests:
         - <64位小写SHA-256>
       prefer_prebuilt_images: true

   apps:
     my-app:
       environments:
         staging:
           deployment_mode: docker
           runtime_profile: approved-compose
   ```

   `runtime_profile` 是管理员绑定到应用环境的信任策略；MCP 注册参数中的 `profile` 则选择
   `build_profiles`，通常仍传 `default`。文件任一字节发生变化都要重新审核并更新摘要；摘要
   不匹配时会直接拒绝，不会退回到 BuildKit。`include` 和 `extends` 始终不支持。
   摘要批准只放宽 Compose 内容校验，不会把服务器 shell 环境传给 Compose：Runner 使用固定的
   空 `--env-file`，所以不要依赖源目录 `.env` 或 systemd 环境来替换 Compose 中的 `${VAR}`；
   需要的值应在审核过的发布文件中明确提供。服务级 `env_file` 用于容器环境变量时，文件也必须
   随 Git 快照提供。
3. 分别以 Gateway、Runner 服务用户运行 `self-check`，再以 Runner 用户执行 `docker info`
   并对 Compose 引用的每个镜像执行 `docker image inspect <引用>`。这些手工命令才确认
   Docker daemon 权限和镜像可用；`self-check` 本身不检查它们。然后重启两个服务使配置生效。
   注册操作示例（项目目录须是 `allowed_project_roots` 下的规范绝对路径，不能通过符号链接访问）：

   ```text
   ops_catalog()
   ops_app_register(
     app="my-app",
     environment="staging",
     project_dir="/srv/projects/my-app",
     compose_file="compose.yaml",
     profile="default",
     idempotency_key="register-my-app-20260922"
   )
   ```

   如果应用已经登记，在管理员配置中添加或更换 `runtime_profile` 后，再调用一次
   `ops_app_register` 以刷新绑定。
4. 创建计划并核对目标提交和服务集合，再排队执行。计划默认 15 分钟后过期：

   ```text
   ops_release_plan(
     app="my-app",
     environment="staging",
     git_ref="refs/heads/main",
     source_mode="fetch"
   )
   ops_release_apply(plan_id="<返回的plan_id>", idempotency_key="deploy-my-app-20260922")
   ops_release_status(job_id="<返回的job_id>")  # 轮询到 succeeded 或 failed
   ```

   对服务器已有但尚未 fetch 的提交，可将 `source_mode` 设为 `local`。`apply` 返回成功只表示
   job 已排队；完成后检查 `ops_release_status` 的健康结果和 `built_images`（应为空），再按需
   查看 `ops_status`、`ops_logs` 和 `ops_http_request`。实际启动命令固定为
   `docker compose up --no-build --pull never`，不会构建或拉取镜像。

批准用的 SHA 是 Compose 文件原始字节摘要；计划响应中的 `compose_digest` 是规范化计划指纹，
两者不是同一个值。t4 上已用 ContractLens 验证这一流程，结果见
[docs/VERIFICATION_RECORD.md 的 01C 真实发布记录](docs/VERIFICATION_RECORD.md#20-01c-可信-compose-与预建镜像发布验证2026-09-22)。

### 管理员维护的 Compose 与环境文件

如果 Compose 和 `.env` 由服务器管理员直接维护，可为单个应用环境配置独立目录，免去每次
修改 Compose 后手动更新批准摘要。Git 仓库仍提供代码快照；Compose 入口文件及它通过
`env_file` 引用的文件从管理员目录读取，不依赖 Git commit。一个环境只使用一个明确的
Compose 入口，不自动猜测 `compose.yaml` 或 `docker-compose.yaml`。

```yaml
apps:
  my-app:
    git:
      repo_path: /srv/projects/my-app
      origin: https://example.invalid/my-app.git
    environments:
      staging:
        project_name: drawbridge-my-app-staging
        deployment_mode: docker
        operator_compose:
          directory: /etc/drawbridge/apps/my-app
          file: compose.yaml
```

管理员目录必须位于 `allowed_project_roots`、Drawbridge 状态目录及发布目录之外；从根目录
到实际文件的路径不得包含符号链接。目录、Compose 和环境文件须由管理员持有，且 Drawbridge
的 Gateway/Runner 用户及其组不能写入。两进程必须能读取文件。首次配置后重启两进程，调用
`ops_app_register`，其中 `compose_file` 使用同一个入口文件名。之后管理员修改文件内容不需
改配置摘要、重启或重新注册；重新创建 plan 即可。服务名的增删和改名仍受固定服务集合约束。

此模式允许 Compose 中的 `${VAR}` 从管理员目录的 `.env` 插值，也支持服务级相对路径
`env_file`。引用文件必须是该目录内的普通文件；不支持动态插值的 `env_file` 路径、绝对路径、
`include` 或 `extends`。这些文件也不能登记为 MCP 的 `editable_files`。计划自动记录内容摘要；
文件在 plan 和 Runner 执行之间变化会使任务以 `STALE_PLAN` 失败。本次 t4 测试的 MCP
响应扫描未发现环境文件内容；部署失败的 Docker 错误输出仍可能包含 `.env` 插值值，见
[架构中的已知限制](.harness/context/ARCHITECTURE.md)。Runner 从本地 Docker Engine 解析
预建镜像并用实际镜像 ID 启动，不执行构建或拉取；人工装载镜像可以在执行前完成。
代码修改仍要先进入 Git commit。此模式已在 t4 的独立 `operator-smoke` 应用完成核心验收，
见[验证记录](docs/VERIFICATION_RECORD.md)；原始 ContractLens 应用尚未按管理员模式验收，
BuildKit、回滚和中断恢复也不在此次验收范围内。

若连接返回 `401`，检查 token；`403` 检查客户端 CIDR、Host 和 Origin；`421` 检查
MCP transport 的 Host 校验及端口转发；job 长时间停在 `queued` 时检查 Runner 的
systemd 状态和日志。

本地开发流程见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。编程智能体的项目验证
入口见 [AGENTS.md](AGENTS.md) 和 [.harness/README.md](.harness/README.md)。设计与实施依据是
[docs/TECHNICAL_DESIGN.md](docs/TECHNICAL_DESIGN.md) 和
[docs/MVP_IMPLEMENTATION_SPEC.md](docs/MVP_IMPLEMENTATION_SPEC.md)。
