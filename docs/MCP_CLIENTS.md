# MCP 客户端接入

本文说明如何让 Codex CLI、Claude Code 等支持 Streamable HTTP 的编程智能体连接
Drawbridge。Drawbridge 是一个已经运行在服务器上的 MCP Gateway；客户端只连接
Gateway，不直接启动 Drawbridge，也不连接 Runner。

## 连接前提

先按 [`README.md`](../README.md) 启动 Gateway 和 Runner，并确认 Gateway 使用 token
认证。推荐 Gateway 只监听服务器回环地址，再从客户端建立 SSH 隧道：

```sh
ssh -N -L 8787:127.0.0.1:8787 user@server.example.com
```

保持该终端运行。之后客户端使用以下参数：

| 项目 | 值 |
| --- | --- |
| Transport | Streamable HTTP |
| URL | `http://127.0.0.1:8787/mcp` |
| 请求头 | `Authorization: Bearer <gateway token>` |
| 首个调用 | `ops_catalog` |

为了避免把 token 写入命令历史或配置文件，先在客户端所在机器设置环境变量：

```sh
export DRAWBRIDGE_TOKEN='从服务器 token 文件安全取得的值'
```

不要把真实 token 提交到 Git，也不要把带真实 token 的配置复制到工单、日志或聊天中。

## Codex CLI

Codex 使用 `config.toml` 配置 Streamable HTTP MCP 服务器。将以下内容加入
`~/.codex/config.toml`；如果只想在一个受信任项目中启用，也可以写入该项目的
`.codex/config.toml`：

```toml
[mcp_servers.drawbridge]
url = "http://127.0.0.1:8787/mcp"
bearer_token_env_var = "DRAWBRIDGE_TOKEN"
default_tools_approval_mode = "prompt"
```

启动 Codex 前确保 `DRAWBRIDGE_TOKEN` 已设置，然后检查服务器：

```sh
codex mcp list
codex
```

进入 Codex 后可以使用 `/mcp` 查看已连接服务器。首次使用时先让 Codex 调用
`ops_catalog`，再根据返回的能力和限制执行后续操作。部署、回滚、重启、文件修改和
非 GET/HEAD 的 HTTP 验证都属于有副作用的操作，应保留审批提示并逐项确认。

不要在 `config.toml` 中使用包含真实 token 的静态 `http_headers`；
`bearer_token_env_var` 会让 Codex 从本地环境变量读取 Bearer token。

## Claude Code

Claude Code 当前将该服务作为远程 HTTP MCP 添加。下面的 `add-json` 写法保留环境变量
引用，由 Claude Code 在连接时展开：

```sh
claude mcp add-json drawbridge \
  '{"type":"http","url":"http://127.0.0.1:8787/mcp","headers":{"Authorization":"Bearer ${DRAWBRIDGE_TOKEN}"}}' \
  --scope user
```

检查配置和连接状态：

```sh
claude mcp get drawbridge
claude mcp list
```

进入 Claude Code 后使用 `/mcp` 查看连接状态。若希望把配置共享给项目成员，可以使用
`--scope project`，让 Claude Code 写入项目根目录的 `.mcp.json`；此时仍应只提交环境
变量引用，不要提交真实 token，而且每台客户端都必须建立自己的 SSH 隧道并设置
`DRAWBRIDGE_TOKEN`。

不建议把真实 token 直接放在下面这种命令或 JSON 中，因为它可能进入 shell 历史或配置文件：

```sh
claude mcp add --transport http drawbridge http://127.0.0.1:8787/mcp \
  --header "Authorization: Bearer <真实 token>"
```

## 接入后的最小验证流程

客户端显示服务器已连接后，按以下顺序验证。只读操作可以先执行；变更操作要等待
客户端审批，并且必须使用幂等键：

1. `ops_catalog`：确认工具、权限和限制。
2. `ops_status`：确认主机和已登记应用状态。
3. 未登记应用时调用 `ops_app_register`。
4. 调用 `ops_release_plan`，使用完整 Git ref（例如 `refs/heads/main`）或 commit SHA。
5. 调用 `ops_release_apply(plan_id, idempotency_key)`，记下返回的 `job_id`。
6. 循环调用 `ops_release_status(job_id)`，直到成功、失败或过期；`status: ok` 只表示任务已入队。
7. 成功后再按需调用 `ops_status`、`ops_logs` 和 `ops_http_request` 获取证据。

同一变更重试时复用相同的 `idempotency_key`。不要让客户端直接执行服务器 shell 命令、
拼接 Docker 命令或绕过 MCP 工具的计划/审批流程。

## 常见问题

- `401`：检查 `DRAWBRIDGE_TOKEN` 是否设置、值是否与 Gateway token 文件一致。
- `403`：检查 Gateway 的 `allowed_client_cidrs`、`auth.allowed_hosts` 和 Origin 策略。
- `421`：检查 URL 的 Host 是否在 `auth.allowed_hosts` 中；通过 SSH 隧道时优先使用
  `127.0.0.1`，不要直接换成未允许的服务器域名。
- 连接被拒绝：确认 SSH 隧道仍在运行，且 Gateway 正在监听服务器的 `127.0.0.1:8787`。
- job 长时间为 `queued`：检查 Runner 的 systemd 状态、共享 `state_dir` 和服务日志。

客户端命令和配置格式会随版本更新；遇到参数错误时分别运行 `codex mcp --help` 或
`claude mcp --help`，但不要因此改用 STDIO。Drawbridge 当前对外提供的是 Streamable
HTTP `/mcp`。

官方参考：

- [OpenAI Codex MCP 文档](https://developers.openai.com/zh-Hans/docs/extend/mcp)
- [Claude Code MCP 文档](https://code.claude.com/docs/en/mcp)
