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
2. 将管理员配置放到 `/etc/drawbridge/config.yaml`，token 放到权限为 0600 的文件，
   `auth.mode` 保持 `token`。
3. 先执行 `drawbridge self-check`；若 rootless BuildKit、Docker socket、Python 基线或
   仓库属主不满足条件，部署能力应保持禁用，不启用高权限 fallback。
4. 复制并按服务器用户修改 `deploy/systemd/*.service`，Gateway 与 Runner 使用不同的
   systemd 用户；只有 Runner 拥有 Docker 权限。
5. 启动 Gateway 和 Runner 后，先调用 `ops_catalog`，再按“register → plan → apply →
   status → logs/HTTP”顺序验证。`ops_release_apply` 只接受 plan ID，不接受命令或路径。
   Codex CLI、Claude Code 等客户端的 SSH 隧道、token 环境变量和 MCP 配置见
   [`MCP_CLIENTS.md`](MCP_CLIENTS.md)。

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
