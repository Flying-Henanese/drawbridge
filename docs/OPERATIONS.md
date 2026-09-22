# Drawbridge 运维与验证

## 本地启动

```bash
uv sync --extra dev
cp config.example.yaml config.local.yaml
uv run drawbridge self-check --config config.local.yaml
uv run drawbridge-gateway --config config.local.yaml --host 127.0.0.1 --port 8787
```

Gateway 的 MCP endpoint 是 `/mcp`。默认需要 `Authorization: Bearer <token>`，且只允许
配置的客户端网段和 Host；不要在真实服务器上把示例 token 当作凭据。

Runner 在另一个终端启动：

```bash
uv run drawbridge-runner --config config.local.yaml
```

## 服务器安装

1. 使用 Python 3.12+ 创建虚拟环境并执行 `uv sync --frozen`。
2. 从同一份管理员部署参数生成 `/etc/drawbridge/gateway.yaml` 和
   `/etc/drawbridge/runner.yaml`。Gateway 配置保持 `auth.mode: token`，token 放到仅 Gateway
   用户可读、权限 0600 的文件；Runner 配置设为 `auth.mode: none` 且不配置 token，只供没有
   网络监听端口的 Runner 使用。两份配置的状态目录、项目、构建、运行时和队列参数必须一致。
3. 分别以对应服务用户执行完整命令：

   ```sh
   /opt/drawbridge/.venv/bin/drawbridge self-check --config /etc/drawbridge/gateway.yaml --role gateway
   /opt/drawbridge/.venv/bin/drawbridge self-check --config /etc/drawbridge/runner.yaml --role runner
   ```

   两者会阻断 Python、Git、认证、状态目录和 SQLite 的明显错误；Runner 角色还检查已配置的
   BuildKit 工具/socket，Gateway 角色跳过这些 Runner 专属条件。自检不检查 Docker daemon
   权限、目标镜像、仓库属主、Git fetch 或 BuildKit daemon 是否真正 rootless；这些条件须
   继续按下文手工验证，不启用高权限 fallback。
4. 复制并按服务器用户修改 `deploy/systemd/*.service`，Gateway 与 Runner 使用不同的
   systemd 用户。当前 Gateway 需要按 source mode 读取或写入登记仓库；Runner 负责 Docker
   构建和变更。只有确实需要 `ops_app_discover` 和 Docker 日志时 Gateway 才需要 Docker
   访问，否则这两个工具不可用。Docker socket 权限通常等同宿主机高权限，不能视为只读授权。
5. 启动 Gateway 和 Runner 后，先调用 `ops_catalog`，再按“register → plan → apply →
   status → HTTP”顺序验证；仅在 Gateway 已有 Docker 权限时增加 `logs` 检查。
   `ops_release_apply` 只接受 plan ID，不接受命令或路径。
   Codex CLI、Claude Code 等客户端的 SSH 隧道、token 环境变量和 MCP 配置见
   [`MCP_CLIENTS.md`](MCP_CLIENTS.md)。

在真实 Docker 模式启用服务前，至少以 Runner 用户执行 `docker info`，并对目标 Compose 的
每个 `image:` 引用执行 `docker image inspect <引用>`。以 Gateway 用户运行
`git -C <登记仓库> rev-parse HEAD`；使用 `source_mode: fetch` 时还要验证它能访问 origin 并
写仓库。若需要 Gateway 的发现或日志工具，再以 Gateway 用户单独验证 Docker 连接。
Gateway systemd 模板默认只允许写状态目录；使用 fetch 时，按模板注释为每个登记仓库增加
一条精确的 `ReadWritePaths=`，不要直接放宽 `allowed_project_roots` 的整个根目录。

## 构建镜像

需要构建的服务可在 Compose 中声明 `build:`。管理员在 Gateway 和 Runner 的相同配置中
登记 `buildkit` profile。每个构建服务只能声明与 profile 对应的 `context` 和
`dockerfile`；不接受由 Compose 提供的 build args、secret、SSH、额外 context 或自定义
frontend。Dockerfile 可以声明官方 `docker/dockerfile:1.x` syntax，构建仍固定使用内置
`dockerfile.v0`；其他 frontend 会被拒绝。未构建的服务继续使用 `image:`。例如：

```yaml
build_profiles:
  default:
    mode: buildkit
    buildkit_socket: unix:///run/user/1001/buildkit/buildkitd.sock
    platform: linux/arm64
    timeout_seconds: 900
    targets:
      api: {context: api, dockerfile: Dockerfile}
      worker: {context: worker, dockerfile: Dockerfile}
allow_simulation: false
```

上面的 socket 路径、平台和服务名只是示例。管理员须以**单独的普通用户**运行 rootless
BuildKit daemon，为 socket 设置只允许 Runner 连接的属组，并在 Runner 的 systemd 单元中
按需设置 `SupplementaryGroups=`；同时确认 `ProtectHome`、`ReadWritePaths` 没有阻止访问
socket 和 release 目录。BuildKit daemon 不应有 Docker
socket、部署凭据或 `security.insecure` / `network.host` entitlement。不要在无法启动
rootless BuildKit 时回退到 `docker build`。安装方式和 rootless 限制参见
[BuildKit 官方文档](https://github.com/moby/buildkit/blob/master/docs/rootless.md)。

发布任务从固定 Git SHA 生成快照，依次对登记服务运行 `buildctl`、导出 Docker archive、
`docker image load`、按唯一 tag 查验镜像 ID，再生成只引用镜像 ID 的运行时 Compose 文件。
构建产物和 SHA-256 摘要保存在 release 目录。构建失败时不会执行 Compose 更新。
如果构建完成后、Compose 更新开始前出错，Runner 会尝试移除新导入的镜像 tag 并删除
未完成的 release 目录。Compose 更新已开始却失败时，容器可能已有部分变化；Runner 会在
作业错误中给出保留的 release 目录，供管理员检查运行时 Compose、构建 archive 和容器状态。
此时不会自动回滚，管理员应在检查后决定恢复动作。
`self-check` 会确认 `buildctl` 和 socket 存在，但无法证明 daemon 的隔离配置或镜像可运行；
在真实服务器上需用非敏感测试应用验收完整的 `register → plan → apply → status` 流程。
Compose 默认拒绝 `privileged`、host namespace、任意端口、任意设备和绝对宿主挂载。
NPU/GPU 应由管理员在配置文件的 `runtime_profiles` 中按服务精确登记，再由目标环境的
`runtime_profile` 引用；仓库 Compose 不能自行开启这些能力。profile 可登记：

- `privileged_services`：仅允许列出的服务使用严格布尔值 `privileged: true`；
- `host_mounts`：精确匹配 service、规范化 host/container 路径和 `read_only`；
- `ports`：精确匹配发布 IP、宿主端口、容器端口和协议；
- `device_reservations`：精确匹配 Compose deploy reservation 的 driver、device IDs 和 capabilities。

对管理员已经审核的旧应用，可以在专用 profile 的 `approved_compose_digests` 中登记 Compose
文件的 SHA-256。只有文件内容摘要精确匹配时，才允许其中原本被严格策略拒绝的服务字段、顶层
资源、宿主能力和 `${...}` 插值；文件变化后必须由管理员重新审核并更新摘要。Compose 文件仍
必须位于已登记项目目录内、不是符号链接、大小受限并包含合法服务；顶层
及服务级 `include`/`extends` 始终不支持。可用 `sha256sum compose.yaml` 计算待批准摘要。

如果可信 Compose 同时保留 `image:` 和开发用的 `build:`，但服务器只应启动现有镜像，可同时
设置 `prefer_prebuilt_images: true`。Runner 会保留 Compose 内容并使用既有的
`docker compose up --no-build --pull never` 路径，不调用 BuildKit；只有 `build:`、没有
`image:` 的服务会在注册阶段被拒绝。该选项要求 profile 至少登记一个批准摘要；摘要不匹配时
直接拒绝，不回退到构建。未设置该选项的 profile 在摘要不匹配时才恢复默认严格校验：

```yaml
runtime_profiles:
  approved-legacy-app:
    approved_compose_digests:
      - 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    prefer_prebuilt_images: true

apps:
  approved-app:
    # git and environment fields omitted
    environments:
      staging:
        runtime_profile: approved-legacy-app
```

Ascend profile 应为每个实际 NPU 服务登记 driver、`npu-smi`、DCMI 和模型缓存挂载，并加入：

```yaml
- service: paddleocr-vlm-server
  host_path: /etc/ascend_install.info
  container_path: /etc/ascend_install.info
  read_only: true
- service: paddleocr-vlm-server
  host_path: /var/log/npu
  container_path: /var/log/npu
  read_only: false
```

对 `paddleocr-vl-api` 重复登记这两项。`/etc/ascend_install.info` 只读，`/var/log/npu` 可写。
Compose 必须使用已渲染的固定值，不能保留 `${...}`；BuildKit 的 `build:` 仍只接受登记的
context/dockerfile，不能通过 `build.args` 传入动态构建能力。启动前以 Runner 用户确认所有
宿主路径存在、没有符号链接且可读/可写，并用 `npu-smi info` 验证驱动。允许
`privileged` 只解决 Docker 隔离策略，不会安装驱动，也不会修复宿主文件权限。

## t4 验证约定

`/home/mineru_dev/github_repo/drawbridge` 是代码部署目录。2026-09-20 的 t4 验证已使用
Python 3.12.12 虚拟环境完成 simulation 流程；结果和未覆盖项见 `VERIFICATION_RECORD.md`。
在 t4 上复验时使用该虚拟环境与 `zsh`，先运行 `.venv/bin/python scripts/verify.py` 获取隔离证据。
rootless BuildKit 尚未在该次验证中配置；切换到真实 Docker/BuildKit workflow 前，
必须单独完成相关安装自检与真实服务验收，不能以 simulation 通过代替。
