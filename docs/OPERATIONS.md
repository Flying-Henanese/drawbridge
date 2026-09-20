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

## t4 验证约定

`/home/mineru_dev/github_repo/drawbridge` 是代码部署目录。2026-09-20 的 t4 验证已使用
Python 3.12.12 虚拟环境完成 simulation 流程；结果和未覆盖项见 `VERIFICATION_RECORD.md`。
在 t4 上复验时使用该虚拟环境与 `zsh`，先运行 `.venv/bin/python scripts/verify.py` 获取隔离证据。
rootless BuildKit 尚未在该次验证中配置；切换到真实 Docker/BuildKit workflow 前，
必须单独完成相关安装自检与真实服务验收，不能以 simulation 通过代替。
