# Drawbridge

Drawbridge 是用于部署和运行观测的 MCP 服务。它由两个 Python 进程组成：Gateway 在
`/mcp` 提供 Streamable HTTP 接口，Runner 从共享 SQLite 队列执行任务；Drawbridge
自身不是一个 Docker Compose 应用。下文是 Linux 服务器上的安装、启停和接入流程。

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
仓库另放在例如 `/srv/projects/<app>`；Runner 的 `User=` 必须是该仓库的普通属主，
Gateway 使用另一个低权限账号。业务仓库应已有 `origin` 和至少一个 commit，并能由
Runner 用户读取和写入。**不要给 Gateway Docker socket 权限。**

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
专用共享组、目录 `2770` 权限和两个单元中的 `UMask=0007` 实现。Runner 还需能写发布
目录和已登记的源码仓库；如模板启用 `ProtectSystem=strict`，须把这些**准确路径**加到
Runner 的 `ReadWritePaths=`。Gateway 不应获得这些额外写权限。

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
.venv/bin/drawbridge self-check --config /etc/drawbridge/gateway.yaml
.venv/bin/drawbridge self-check --config /etc/drawbridge/runner.yaml
```

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
2. Runner 的 `User=` 改为业务仓库属主，`ExecStart` 指向 `runner.yaml`；只给 Runner
   所需的 Docker 权限。按上节补齐两个服务的共享状态目录权限、`UMask` 和 Runner 的
   `ReadWritePaths`。
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
Runner 能在 systemd 的 `ProtectHome=true` 限制下访问所需 Git 凭据。

若连接返回 `401`，检查 token；`403` 检查客户端 CIDR、Host 和 Origin；`421` 检查
MCP transport 的 Host 校验及端口转发；job 长时间停在 `queued` 时检查 Runner 的
systemd 状态和日志。

本地开发流程见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。编程智能体的项目验证
入口见 [AGENTS.md](AGENTS.md) 和 [.harness/README.md](.harness/README.md)。设计与实施依据是
[docs/TECHNICAL_DESIGN.md](docs/TECHNICAL_DESIGN.md) 和
[docs/MVP_IMPLEMENTATION_SPEC.md](docs/MVP_IMPLEMENTATION_SPEC.md)。
