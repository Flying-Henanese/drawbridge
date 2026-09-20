# Drawbridge MVP 实施规格

本文件将 [技术设计](TECHNICAL_DESIGN.md) 收敛为首版可编码、可测试的操作目录。
状态：拟定实施基线；项目目录由用户接入时指定，NPU 型号与可选测试 profiles 在实际接入时配置。
不开放通用命令执行，不引入外部 CI/CD；HTTP、Python 3.12+、SQLite、单机 Compose。
若与技术设计中的示例操作有差异，以本文件的 MVP 规则为准。

## 1. MVP 范围与执行主体

- 首批支持单个 staging 应用，结构允许登记多个应用；按单一操作者、多个子智能体
  并发请求设计，全局变更并发首版固定为 1。
- 提供用户指定目录接入、Docker 辅助发现、查询、配置诊断、受控应用配置修改、计划、部署、
  HTTP 验证、登记测试、重启和历史制品回滚。业务验证由智能体按需组合，不要求完整 CI 流程。
- 不提供 push、pull、checkout、reset、clean、exec 进入业务容器、动态管道或任意脚本。
- 子进程仅由 Runner 管理。Gateway 不执行主机命令；只读命令也经 Runner 的有界诊断通道。
  诊断通道最多并发 16，不占变更队列槽位，诊断请求保留 24 小时，客户端断开不产生变更。
- 执行 profile 不能通过请求选择；低权限工作必须由实际独立账号/隔离执行环境完成，
  不能只在同一个高权限进程里换一个环境变量。受控代理只接收类型化任务，不能接收任意 argv。

| profile | 能力 | 禁止 |
|---|---|---|
| host_observe | 独立低权限账号；ps、主机聚合指标、登记 NPU 查询 | Docker socket、Git 凭据、业务密钥 |
| source_manage | 受控 Git 操作、固定 origin 只读凭据、快照准备、登记应用配置 patch；与预克隆仓库同 UID | 执行业务脚本、修改系统配置、修改 Compose/Dockerfile/secret |
| project_diagnostic | 隔离诊断；源码只读、专用临时目录、默认无网络 | 部署权限、socket、密钥 |
| image_build | 独立 rootless BuildKit；专用 socket、目录和账户 | 宿主机 Engine socket、部署凭据、危险 entitlement |
| runtime_manage | 可信 Runner 的固定 Engine/Compose 操作 | 接受请求提供的 Compose、挂载、容器命令 |
| isolated_test | 登记测试模板、隔离容器、源码只读或固定测试镜像、资源限制 | 部署凭据、socket、生产密钥 |
| http_verify | 内置 HTTP 客户端、登记内网 CIDR/端口范围、有界输入输出 | 自动读取服务器凭据、任意协议、默认跟随重定向 |

Runner 本身仍是可信计算基础。上述 profiles 隔离不构成对 Runner 被攻陷的防御承诺。
Gateway 与 Runner 是两个不同 Linux 用户运行的 systemd 服务：Gateway 使用专用低权限
账户且无 Git 凭据/Docker socket，Runner 使用预克隆项目仓库的普通属主账户，并独占
部署用 Docker 权限。首版信任本机仓库属主，不依赖人类与 Runner 同时操作仓库；多个
子智能体触发的仓库操作仍用锁串行化。不维护额外的 Runner 自有 mirror。

## 2. 参数与校验规范

统一采用 Pydantic strict、禁止额外字段；不隐式把字符串转整数，不把 bool 当整数。
不自动 trim、URL decode、大小写转换或 shell 拆词；拒绝 NUL、控制字符（reason 也不允许换行）。
管理员正则由 `regex.fullmatch` 执行，单次预算 100ms，规则最长 1024 字符；超时拒绝。
以下正则均为 ASCII。正则是形状检查，不能替代登记范围及权限检查。

| 参数类型/字段 | JSON 类型、默认/上限 | 正则或枚举 | 额外语义规则 |
|---|---|---|---|
| app、operation、workflow、suite、validator、file | string，1–64 | `[a-z][a-z0-9_-]{0,63}` | 必须存在于对应登记表；file 是别名，不是路径 |
| environment | string | MVP 仅 `staging` | 必须属于 app |
| service | string，1–64 | `[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}` | 必须属于 app；重启还须属于 restartable_services |
| source_mode | string，默认 fetch | `fetch`、`local` | local 仍校验可达性，不使用未提交文件 |
| git_ref | string，1–200 | 见下文 | 不接受短分支名、短 SHA、表达式、URL |
| commit_sha | string，40 | `[0-9a-f]{40}` | 仅由解析器产生；MVP 只接入 SHA-1 仓库，SHA-256 仓库启动自检拒绝 |
| base_commit_sha | string，40；开启新 workspace revision 链时必填 | `[0-9a-f]{40}` | 必须为该 app 允许来源可达的 commit；不要求工作区 HEAD 等于此 SHA |
| count | integer，默认 20 | 1–100 | git_log 条数 |
| limit | integer，默认 100 | 1–200 | 每页结果条数 |
| tail | integer，默认 200 | 1–1000 | 底层日志扫描行数，与返回 limit 分开 |
| since_seconds | integer，默认 300 | 1–86400 | 生成服务端时间戳，不接受任意 Docker 时间字符串 |
| query | string，默认空，最多 128 | 无正则功能 | 字面量过滤，不能下传为 shell/grep 参数 |
| subdir | string，1–200，默认 `.` | `\.` 或 `[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*` | 拒绝 .、.. 路径分段及任何符号链接；必须位于登记的诊断目录 |
| plan_id、job_id、release_id | string，36 | `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}` | UUID 解析后查记录、类型、目标与状态，不因格式正确就授权 |
| workspace_revision | string，36，可选 | UUID 格式 | 必须属于 app/environment，并且基于本次 plan 的 commit SHA；未指定表示空 overlay |
| idempotency_key | string，8–128 | `[A-Za-z0-9][A-Za-z0-9._:-]{7,127}` | 所有非 read 操作必填；同键不同规范化请求返回冲突 |
| reason | string，1–256 | 禁止控制字符 | 重启/回滚必填，审计摘要脱敏 |
| agent_id、parent_task_id | string，可选，1–128 | `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` | 仅追踪，不是身份 |
| cursor | string，可选，最多 512 | 不透明服务端生成值 | 服务端绑定查询和快照、过期 10 分钟；不是文件位置或用户路径 |
| project_dir | string，绝对路径，最多 4096 | 不用正则授权路径 | 仅应用接入入口接受；必须位于 allowed_project_roots，拒绝穿越和符号链接 |
| compose_file | string，项目内相对路径，最多 200 | 按 subdir 的分段形状校验 | 接入时普通文件、无链接；不是可直接执行的高权限模板 |
| file_alias | string，1–64 | `[a-z][a-z0-9_-]{0,63}` | 只能是绑定中登记的 editable 文件别名，不是路径 |
| patch | object/string，最多 64 KiB | 结构化 diff 或登记格式 | 只能修改 editable 应用配置；不得包含命令、二进制、secret 或额外文件 |
| expected_revision | string，36 | UUID 或服务端 revision | 始终指向 app/environment 当前 revision；首次 patch 使用接入时生成的初始 revision，防止覆盖并发修改 |
| url | string，最多 2048 | URL 解析器，scheme 仅 http/https | host/全部解析地址/端口均在 HTTP 出站策略内；禁止 userinfo、fragment |
| method | string，默认 GET | GET、HEAD、POST、PUT、PATCH、DELETE、OPTIONS | 非 GET/HEAD 需要 idempotency_key；可能改变业务状态，非 read |
| headers | object，最多 16 项、总计 8 KiB | 仅登记 header 名；值禁止控制字符 | 默认允许 Accept、Content-Type、X-Request-ID；禁止 Host、Cookie、Authorization、代理及逐跳头 |
| body | string，可选，UTF-8 最多 64 KiB | 不作 shell 解释 | GET/HEAD 禁止 body；JSON 通过 Content-Type 声明 |
| timeout_seconds | integer，HTTP 默认 10 | 1–30 | 只能缩短全局预算；总含解析/连接/读取 |

git_ref 允许的形状（取其一，再与 app.allowed_ref_patterns 做完整匹配）：

```text
refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*
refs/tags/[A-Za-z0-9][A-Za-z0-9._/-]*
[0-9a-f]{40}
```

分支/tag 必须额外通过 `git check-ref-format`。SHA 必须为 commit，且可从当前允许的
分支/tag 到达。MVP 默认只允许 `refs/heads/main` 和 `refs/heads/agent/...`；tag 按应用显式开启。
fetch 模式分支映射到 `refs/remotes/origin/<分支>`，local 模式映射到 `refs/heads/<分支>`；
tag 映射到 `refs/tags/<标签>`。可达性检查只用当前模式的允许来源，不混用旧远程跟踪 ref。

## 3. argv 渲染与公共规则

- 程序绝对路径通过管理员 toolchain 登记，启动时验证文件、所属权限、版本及所需 flags。
  表中的 `/usr/bin/git`、`/usr/bin/docker`、`/usr/bin/ps`、`/usr/local/bin/buildctl` 是 Linux 示例。
- 表中 `{param.x}` 来自已验证请求；`{app.x}` 来自管理员配置；`{job.x}`、`{release.x}`
  来自经过 Runner 重验的类型化状态。三者不得互相覆盖。
- 每个模板数组元素产生一个 argv；允许固定前后缀插值，不拆词。禁止任意 list splice；
  多 service 操作由 handler 按登记表生成，不能接收请求中的 argv 数组。
- cwd 只能是登记 repo、服务端 release/job 目录或固定 `/`；请求不传 cwd/env/stdin。
- 默认 accepted_exit_codes=[0]、stdin 关闭、TERM 宽限 5 秒后 KILL 并回收整组进程。
  容器和 BuildKit 任务须同步停止实际隔离任务，不能只杀 CLI。
- 查询输出摘要最多 64 KiB；日志结果单次最多 200 行/256 KiB。
  变更命令 spool 到受保护目录：摘要 64 KiB，单步骤硬日志预算 20 MiB、单 job 100 MiB；
  超硬预算终止并按是否触及运行时恢复。原始归档不通过 MCP 下载。
- 环境从空集合构造；固定 PATH、LANG=C.UTF-8、TZ=UTC。profile 另登记 HOME/socket 等。
  清除 BASH_ENV、ENV、LD_PRELOAD、PYTHONPATH、用户 Git/Docker 覆盖变量。
  Docker 固定 DOCKER_HOST 和 DOCKER_CONFIG，不继承用户 context；Compose 不隐式读取项目 .env。

所有 Git 命令统一前缀，以下表仅列其后缀：

```yaml
executable: /usr/bin/git
argv_prefix:
  - --no-pager
  - -c
  - core.hooksPath=/etc/drawbridge/empty-hooks
  - -c
  - core.fsmonitor=false
  - -c
  - protocol.allow=never
  - -c
  - protocol.ssh.allow=always
  - -c
  - protocol.https.allow=always
  - -c
  - credential.helper=
```

empty-hooks 是管理员维护的空目录。设置 GIT_TERMINAL_PROMPT=0、GIT_CONFIG_NOSYSTEM=1、
GIT_CONFIG_GLOBAL=/etc/drawbridge/gitconfig；清除继承的 `GIT_SSH_COMMAND`，设置 `GIT_SSH`
为管理员维护的固定绝对路径 wrapper，使用固定 known_hosts 与只读凭据，不接受请求 SSH
选项。HTTPS 需要凭据时另登记管理员固定 helper，不能开放自定义 helper。不能只在全局
Git config 中设置 wrapper，否则可能被仓库 local config 覆盖。
预克隆仓库 `.git/config` 在接入时固定规范化路径与 origin，并在每次网络 Git 操作前于
仓库锁下重验：origin URL、额外 URL、`url.*.insteadOf`、include/includeIf、
`core.sshCommand`、代理、凭据助手、外部程序和自定义协议覆盖。发现漂移立即拒绝；
不允许 MCP 修改这些配置。Runner 与仓库同 UID，fetch 写入 `.git` 不会产生跨 UID
文件。仓库属主是本机可信操作者，锁只协调 Drawbridge 子智能体，不承诺阻止该用户在
工具外修改配置；运行时仍使用固定 wrapper 与冻结 SHA。
查询 Git log 不开启签名验证、外部 diff、textconv；不递归获取 submodule。

## 4. 可直接调用的操作

这些操作由 ops_catalog 展示；别名工具必须走同一校验和执行路径。

| operation | 参数 | executable / argv（不含程序本身） | profile、cwd | 超时 / access |
|---|---|---|---|---|
| git_status | 无 | Git `["status","--porcelain=v1","--untracked-files=no"]` | source_manage，app.repo_path | 10s / read |
| git_log | git_ref、count | Git `["log","--no-show-signature","--format=%H%x09%ct%x09%s","--max-count={param.count}","{job.resolved_sha}","--"]` | source_manage，app.repo_path | 10s / read |
| host_metrics | 无 | 内置只读 handler；读取 `/proc/stat`、`/proc/meminfo` 并对管理员登记挂载点调用 statfs | host_observe，/ | 10s / read |
| process_list | 无 | `/usr/bin/ps` `["-eo","pid,ppid,user,comm,pcpu,pmem","--sort=-pcpu"]` | host_observe，/ | 10s / read |
| npu_status | 无 | 按 §9 登记固定 executable/argv；默认禁用 | host_observe，/ | 10s / read |
| compose_status | 无 | Docker `C + ["ps","--all","--format","json"]` | runtime_manage，release.dir | 15s / read |
| compose_logs | service、tail、since_seconds、limit、query、cursor | Docker `C + ["logs","--no-color","--timestamps","--tail","{param.tail}","--since","{job.since_rfc3339}","{param.service}"]` | runtime_manage，release.dir | 15s / read |
| config_read | file、可选 release_id | 内置 handler；不调用 cat | project_diagnostic，固定源码快照 | 10s / read |
| workspace_patch | file_alias、patch、expected_revision、idempotency_key；开启新链还需 base_commit_sha | 内置 handler；只修改绑定中登记的 editable 应用配置，返回 revision/diff | source_manage，source workspace | 30s / workspace_write |
| project_list | subdir、limit、cursor、可选 release_id | 内置 handler；不调用 ls/find | project_diagnostic，固定源码快照 | 10s / read |
| config_validate | validator、可选 release_id | 登记固定校验器；JSON/TOML 首批用内置解析器 | project_diagnostic，固定源码快照 | 15s / read |
| check_project_config | 可选 release_id；无自由参数 | `/bin/bash` `["--noprofile","--norc","/etc/drawbridge/scripts/check_project_config.sh"]` | project_diagnostic，固定源码快照 | 15s / read |
| service_restart | service、reason、idempotency_key | handler：Docker `C + ["restart","--timeout","10","{param.service}"]`，之后健康门禁 | runtime_manage，release.dir | 总计 120s / runtime_write |
| http_request | url、method、headers、body、timeout_seconds，可选 app、release_id | 内置 HTTP handler，不调用 curl | http_verify，无业务 cwd | 最多 30s / GET、HEAD 为 read，其余 verification_write |

所有 app 操作另需要 app/environment。host_metrics、process_list、npu_status 为主机操作，
不接受 app；输出不含完整命令行或环境。host_metrics 从两次 `/proc/stat` 采样计算
CPU 使用率（间隔 1 秒），只返回聚合 CPU 使用率、内存总量/
可用量和登记挂载点磁盘总量/可用量，不返回进程明细或任意路径。`ops_status` 由
host_metrics、compose_status、releases 表中的当前 release 和可信模板声明端口组装，
不临时扩展字段。
git_log 先做本地 ref 解析与允许来源校验，不隐式 fetch；git_status 只说明服务器工作区，
其结果不是部署源码。配置诊断默认针对当前成功 release 的 source snapshot；未部署时
使用接入时通过本地允许 ref 生成的只读诊断快照；本地尚无允许 commit 则返回 NO_BASELINE。
响应始终带 SHA/观测时间。接入诊断快照不承诺与未提交工作区内容一致，也不是运行基线。

`C` 是由 handler 生成的固定 Compose 前缀，不是可配置任意参数列表：

```text
["compose", "--ansi", "never", "--project-name", "{app.project_name}",
 "--project-directory", "{release.dir}",
 "--env-file", "/etc/drawbridge/compose/empty.env", "-f", "{release.compose_file}"]
```

empty.env 是管理员控制的空文件，仅防止默认 .env 插值；业务 env_file 仍在可信模板中固定引用。
不执行 `compose config` 并把解析结果回传：它可能解析业务 env_file。
compose_status 输出字段白名单仅服务名、容器 ID、状态、健康、端口；剔除命令字段。
日志先拉取有界快照，再服务端脱敏、字面量过滤和分页，cursor 固定该快照，避免重复重扫造成漏行。
快照保存在受保护的本地目录，单快照最多 1 MiB、全局最多 64 个活游标；超过容量
按最久未使用顺序淘汰。游标被淘汰或超过 10 分钟返回 `CURSOR_EXPIRED`，调用者重新
发起查询；格式无效仍返回 INVALID_PARAMETER。
测试/构建原始输出也不保证完全脱敏，不含密钥只是设计目标，不作为强保证。
重启失败不自动重试、不自动发布新镜像；报告 VERIFY_FAILED，记录实际现场。

config_read 单文件原始大小上限 64 KiB；禁止 .env/密钥及未登记文件，普通文件、无符号链接，
通过 dirfd/no-follow 逐段打开，不能仅 realpath 后再普通 open。结构化配置用登记字段白名单展示；
原始文本只允许管理员声明无敏感内容的文件。project_list 仅登记诊断根目录、不递归、单目录
最多扫描 1000 项，超限标记 truncated。业务插件型校验器按 isolated_test 执行，不属于内置 read。

### 4.1 用户指定目录与 Docker 辅助发现

新增应用接入/发现/接管工具；它们不接收 executable/argv，也不能编辑管理员能力配置：

- `ops_app_discover(limit,cursor)`：只读扫描本机 Docker，返回候选项目、服务及目录线索；
  limit 沿用 §2 通用分页规则（1–200，默认 100）。
- `ops_app_register(app,environment=staging,project_dir,compose_file,profile=default,
  idempotency_key)`：校验后返回接入 job/摘要及绑定版本；profile 只能选择管理员登记名称。
  用户不需要手工填写服务清单；工具自动解析。相同 app 已存在时相同摘要复用，不同摘要
  返回 APP_ALREADY_REGISTERED；修改/重新接入须维护并排空，首版不提供任意覆盖参数。
- `ops_app_adopt_plan(app)`：只读生成接管现有 Compose 项目的计划（15 分钟有效），显示
  容器/镜像/项目/服务集合/挂载摘要及其与可信模板的比对结果；
  `ops_app_adopt_apply(plan_id,idempotency_key,reason)` 明确接管，
  冻结实际基线制品与配置证据。不确认则只能查询，不能自动更新已有项目。

接入先处于 Registered 或 NeedsSetup：允许查询/诊断，只有构建和部署模板完成校验后才
变成 Deployable。调用者的确认记录为“显式请求确认”，不是已验证的人类身份；首版默认
auth.mode:token，持有有效 token 且在白名单网段内的客户端均可接入允许根目录下的项目，
这属于首版接受的权限范围；仅当管理员显式配置 auth.mode:none 时才退化为纯 IP 白名单。

`project_dir` 是用户选定的 source workspace，不直接被清理或覆盖。绑定同时保存：

```text
source_workspace     可变 Git/项目目录，仅用于读取和受控配置修改
release_root         每个 commit/revision 的不可变源码快照与制品引用
runtime_workspace    Runner 控制的可信 Compose 模板目录
data_root            应用声明的数据挂载根目录
project_name         app/environment 唯一且由 Drawbridge 独占的 Compose project name
```

`managed_release_root/<app>/<environment>/<release_id>` 是工具维护的 release workspace；
构建只能读取其中由完整 SHA 和 workspace revision 生成的快照，不能读取变化中的 source
workspace 或未经登记冻结的未提交文件。原始 Compose 相对路径先以 source workspace 解析，经过受限 schema
编译为 runtime workspace 中的可信模板；运行数据路径转成经过允许根目录校验的绝对路径，
不能因 release 目录不同而挂错数据。用户已有数据目录不作为源码快照。
project_dir 必须在管理员允许根目录内（例如 `/srv/projects`）；不默认允许 `/`、HOME。
接入请求唯一可以提出路径，后续仅传 app/environment、文件别名或 release ID，不能扩大文件访问权限。
接入时要求 source workspace 与 Runner systemd `User=` 的 UID 一致，且该 UID 为普通
用户；多个仓库若属主不同，单个 Runner 首版不能直接管理，管理员需统一属主或分开部署
实例。接入保存仓库规范化路径与 origin，后续请求不能覆盖。

项目里的 Compose 文件可以作为接入输入，但不能因为目录合法就直接交给高权限 Compose：
内置受限解析器提取服务/构建信息，禁止自定义 YAML 对象、重复 key、include/extends、
动态插值、hooks、privileged、host namespace、Docker socket/危险设备及任意宿主机挂载。
MVP 支持服务 image/build、command/entrypoint、ports、healthcheck、depends_on、
登记网络/卷和固定 env_file；其他字段必须在受限 schema 明确支持，否则 NeedsSetup。
command/entrypoint 仅进入受限业务容器，不得成为宿主机命令。env_file 只检查登记路径和
文件类型，不读取内容；拒绝 YAML 内明文 secret 展示。校验失败返回字段级修正提示，
可以改用管理员已有的可信模板，不放宽为直接执行任意 Compose。

管理员能力规则与预置模板源位于 `/etc/drawbridge`，对 Gateway/Runner 只读；用户
接入的 Compose 只能经受限解析器编译。运行时产物放在 Runner 控制的目录，例如
`/var/lib/drawbridge/templates/<binding_id>/compose.yaml`；生成版本和摘要，更新镜像时仅
替换受控字段，使用前重验摘要。业务容器账号不能写该目录；仓库属主与 Runner 同 UID，
不能把二者当作互相隔离的安全边界。复用服务名自动解析结果，默认允许重启已接入模板
中的服务，管理员可收紧。
项目名新项目由 app/environment 生成；接管时保留实际项目名，并拒绝同名异目录绑定。
同一 project name 下的 Compose 容器必须全部能对应当前绑定；发现无法归属的容器时标记
drift 并拒绝接管或部署。没有 Compose project label 的容器不属于该 project，但仍要检查
端口、网络、卷等资源冲突。
接管时实际服务集合必须与可信模板完全一致；首个成功发布或接管时固定该
app/environment 的服务集合。后续模板新增、删除或重命名服务，`ops_release_plan` 和
部署 preflight 均返回 `UNSUPPORTED_SERVICE_CHANGE`，在任何 Compose 变更前停止。
绑定内未知 service label 容器属于 drift；已归属但与新模板服务集合不同属于未支持的
拓扑变更。MVP 不使用 `down`/`--remove-orphans`，需由管理员本机维护收敛容器并重新
登记/确认基线后才能部署；此后历史 release 若服务集合不同，也不能直接显式回滚。
同一 app/environment、project_name、规范目录分别有唯一约束，避免并发或别名重复接管。

Docker 发现固定 argv（runtime_manage，默认 15s，有界输出）：

```text
/usr/bin/docker ["ps","--all","--filter","label=com.docker.compose.project",
                 "--format","{{.ID}}"]
/usr/bin/docker ["inspect","--format",
                 "{{.Id}}\t{{.Image}}\t{{json .Config.Labels}}\t{{json .Mounts}}",
                 "{job.discovered_container_id}"]
```

容器 ID 只从本次发现结果产生，逐个校验；不暴露完整 inspect（含环境等）。最多扫描 1000
容器、原始预算 2 MiB；输出只保留允许的 Compose labels 和允许根目录内的挂载线索。
working_dir/config_files 等标签只视为候选，可能缺失、伪造、过期或属于另一台主机；
不以挂载路径推断唯一部署目录，不自动注册/接管。MVP 只接入单 Compose 文件，多文件
项目返回 NeedsSetup，要求生成单份受控模板，不静默丢弃 override。
接管必须核对当前容器、目录与受限模板一致，保留镜像 ID 和可复现运行配置；原项目依赖
未提交源码 bind mount 或历史配置无法冻结时，不能承诺回滚，拒绝部署接管但仍可查询。

### 4.2 受控工作区配置修改与 release 快照

MVP 允许智能体修改工作区中的登记应用配置，但不开放任意文件写入。绑定通过
`editable_files` 声明可修改文件别名、相对路径、格式/schema 和大小上限，例如：

```yaml
editable_files:
  - alias: app_config
    path: config/staging.yaml
    schema: orders-api-config-v1
    max_bytes: 65536
```

`workspace_patch` 接受 `file_alias`、结构化 patch、`expected_revision` 和幂等键；开启
新 revision 链时另需 `base_commit_sha`。`expected_revision` 始终指向该 app/environment
当前 revision（首次使用接入时生成的初始 revision）；新链的基准 SHA 必须为该 app
允许来源可达的 commit；**不要求** source workspace 当前 HEAD 等于该 SHA。
所有登记的 editable 文件都必须在基准 SHA 中存在且为普通文件，且当前文件内容分别
等于该 SHA 中的干净版本。MVP 不支持新建、删除、重命名或修改文件
模式。若改用另一个 SHA，必须先恢复/提交旧 patch 带来的工作区改动，使全部 editable
文件与新 SHA 一致，再显式开启新链。

同一 app/environment、同一基准 SHA 的 revisions 构成线性链。链内 patch 的
`expected_revision` 必须指向当前 revision，且服务端核对所有 editable 文件实际内容
摘要与该 revision 的 post-digest 一致；后续 patch 继承基准 SHA。拒绝绝对路径、路径
穿越、符号链接、额外文件、命令、二进制内容和 secret 字段；应用前后校验普通文件、
大小、Schema 和敏感字段。基准、revision 或实际文件摘要不符返回
`PATCH_BASE_MISMATCH`，不写入部分文件；智能体需基于目标 SHA 重新生成 patch。
patch 在仓库锁下完成校验、临时文件写入及原子替换；崩溃后若文件与持久化 revision
不一致，先标记需人工核对，不继续叠加 patch。成功返回受限 diff 与 revision。

Compose、Dockerfile、构建/部署脚本、systemd 单元、模板和 env_file 属于 read_only 或
secret 类，MVP 不允许通过 workspace_patch 修改。需要改变这些内容时，管理员更新可信
配置或重新接入，旧绑定和旧 plan 失效。

配置 patch 不直接触发构建。每个不可变 workspace revision 保存基准 SHA、**全部**
editable 文件的完整 overlay、逐文件基准 pre-digest 与最终 post-digest、校验结果；
未修改文件的 pre/post digest 相同，不只保存本次改动的文件。`ops_release_plan` 必须冻结
完整 commit SHA、同基准的 workspace revision、runtime 模板摘要、secret reference
摘要和 data_mounts；source_snapshot 在 job 专属目录中先核对干净 SHA archive 的全部
pre-digest，再应用完整 overlay，核对全部 post-digest 后原子生成 release workspace。
任一不符返回 `PATCH_BASE_MISMATCH`，丢弃候选快照且不留下部分 release workspace。
未选择 workspace revision 的 plan 使用空 overlay。修改中的 source workspace、未经
登记冻结的未提交文件和 release 目录外路径不能进入构建上下文。若希望修改进入可复现
发布，可以提交为允许来源中的 commit，也可以使用受控 patch 生成不可变 revision 后
再创建 plan。

应用可以声明多个 data_mounts，例如 uploads、exports 和 cache。Drawbridge 只限制宿主机
挂载来源必须位于管理员 data_root 内，并记录持久化、保留和回滚语义；容器内部可以正常写入
已声明挂载点。代码/镜像回滚不回滚这些数据，也不删除卷、缓存或外部副作用。

### 4.3 智能体按需 HTTP 验证

URL 不必逐应用预先登记；管理员配置 `http_verify.allowed_cidrs`、`allowed_ports` 和最大
并发。该出站策略与 Gateway 客户端来源白名单独立，不因入站来自内网就允许全部出站。
可以授权所需内网、127.0.0.1/::1 和应用网段，默认无出站授权时返回 TARGET_NOT_ALLOWED。
允许用户验证/探测授权网段，网络探测不是本工具的非目标；代价是客户端具有服务器在该
范围内的网络可达性，管理员应避免授权云元数据地址/敏感管理段。内网不等于所有目标可信。

DNS 全部解析结果须满足 CIDR/端口策略；请求固定连接到已校验地址，同时保留 Host/SNI，
不因连接时再次解析绕过检查。默认忽略 HTTP_PROXY 等代理环境、禁用自动重试和重定向，
不自动带服务器凭据，首版不开放认证 header。HTTPS 目标验证证书，不影响 MCP 本身用 HTTP。
响应最多 256 KiB（按解压后字节限制）、最多 200 行摘要，不无限缓冲压缩内容；返回状态码、
白名单响应头、耗时、受限内容和 truncated，不返回 Set-Cookie。4xx/5xx 是验证证据，
不是请求执行失败；连接失败/超时另返回错误。响应为不可信数据。

GET/HEAD 走只读诊断通道，受 `http_verify.max_read_concurrency` 限制，不占变更队列。
其他方法视为可能有业务副作用的 verification_write，必须提供 idempotency_key，接受
维护拦截，并与部署、回滚、重启、测试等任务进入同一个持久化变更队列；不使用
独立 HTTP 写队列或验证全局写槽位。首版 `max_running_jobs=1`，一个变更（包括部署的
自动恢复）运行期间，其他变更只能排队，故 HTTP 写验证不另取应用环境锁；未来放宽
全局并发前必须补齐目标归属与锁规则。写验证立即返回 job_id，由 ops_release_status
查询结果；排队适用全局/每目标容量及 600s queue timeout。有 app/environment 的写
请求计入该目标容量；无 app 的写请求统一计入固定 `http_verify_unbound` 目标，不能
用不同 URL 绕过每目标容量。HTTP 的 1–30s 超时从派发开始计算。部署冷却不作用于
写验证。不自动重试、不自动回滚。
请求已发送但响应未知时记录 OUTCOME_UNKNOWN，不声称服务端没执行；
Drawbridge 幂等只能避免重复派发，不能提供远端 HTTP 事务 exactly-once。
release_id 如提供则必须匹配 app 和当前版本；写请求在派发且发送前重查，排队期间
版本已变化则返回 STALE_RELEASE，且不发送请求。任意网络目标不虚假标记为某个
release 的证据。
GET/HEAD 对当前应用的查询返回版本前后观测值；查询过程中版本变化则标记 version_changed，
不自动归入成功验证。HTTP 状态码本身不表示业务正确：无登记判定条件时只返回证据，
由智能体解释；业务验证摘要保留每次检查结果，不用最后一次成功覆盖此前失败。

## 5. 仅流程可调用的内部命令

以下 public=false。请求不能直接调用；只有 plan handler、冻结 workflow 或固定内置
回滚任务能使用。repo 锁分别覆盖 plan 时的配置重验、fetch、ref 解析与可达性检查，
以及 apply 时从冻结 SHA 生成 archive 的过程；不跨越 plan 与排队时间持锁。

| 内部操作 | 固定 argv / handler | 参数来源与效果 | 超时 |
|---|---|---|---|
| source_fetch | Git `["fetch","--no-auto-maintenance","--no-write-commit-graph","--no-tags","--no-recurse-submodules","origin","{job.fetch_refspec}"]` | refspec 由允许的完整 ref 生成；分支 `+refs/heads/X:refs/remotes/origin/X`，tag `refs/tags/X:refs/tags/X`；SHA 请求只刷新登记来源集合；执行前重验仓库 Git 配置 | 总计 120s |
| enumerate_remote_refs | Git `["ls-remote","--refs","origin"]` | SHA/fetch 模式先枚举真实远程 refs，完整匹配登记规则后逐项 fetch；只使用本次成功刷新的 tips，不能让已删除分支的残留 ref 授权旧 SHA | 30s |
| ref_format | Git `["check-ref-format","{job.full_ref}"]` | 分支/tag 格式校验，不调用于 SHA | 5s |
| resolve_commit | Git `["rev-parse","--verify","--end-of-options","{job.mapped_ref}^{commit}"]` | 输出必须唯一完整 SHA | 5s |
| check_reachable | Git `["merge-base","--is-ancestor","{job.sha}","{job.allowed_tip_sha}"]` | 至少一个当前允许 tip 返回 0；1 代表不可达，其他码为错误 | 每次 5s，总计 30s |
| source_snapshot | Git `["archive","--format=tar","--output={job.archive_path}","{job.sha}"]` + 安全解包 handler | 归档冻结 SHA；逐文件核对全部 editable 文件 pre-digest，应用完整 overlay 后核对 post-digest，再原子生成 release workspace；不匹配返回 PATCH_BASE_MISMATCH 且不留下部分快照；不 checkout、不执行工作区 hooks/filter | 30s |
| image_build | buildctl，见下文 | 只读取冻结 release workspace、登记 Dockerfile/profile；不读取变化中的 source workspace | 900s |
| image_import | Docker `["image","load","--input","{job.image_archive}"]` | 文件由受控构建输出；记录 ID；不信任 CLI 文本为唯一证据 | 120s |
| image_identify | Docker `["image","inspect","--format","{{.Id}}","{job.unique_image_tag}"]` | 唯一 tag 由 app/job 生成，确认导入前不存在、导入后唯一匹配 | 10s |
| compose_deploy | Docker `C + ["up","--detach","--no-build","--pull","never","--wait","--wait-timeout","90"]` | 固定 project_name、runtime workspace、服务集合和 data_mounts；preflight 已确认集合与成功/接管基线一致；不接受请求路径；不 down、不 remove-orphans | 120s |
| health_check | 内置 HTTP handler | 管理员登记的固定 URL、无重定向、状态/结构判定；不使用请求 URL 的 http_verify 出站白名单 | 总计 90s |
| test_suite | 固定 Docker create/start/wait/logs/stop/rm handler，见下文 | 镜像 ID、suite、专用网络与资源固定 | 300s |
| restore_previous | 历史制品 compose_deploy + 最小健康检查 | 不 rebuild；需要历史 release workspace、runtime 模板、镜像、secret reference 和 data_mounts 均存在且摘要一致；仅明确配置了恢复门禁才额外测试 | 独立预算 300s |
| rollback_preflight | 内置 handler | 显式回滚专用；核对目标历史成功 release、回滚前成功基线、冻结制品/模板/secret reference/挂载/历史健康门禁、现场 drift 与固定服务集合；无运行时副作用 | 15s |
| rollback_deploy | 历史制品固定 Compose up + 历史健康门禁 | 先持久化 runtime_change_started；不 rebuild、不使用当前可变绑定推导门禁 | 主任务剩余预算内，Compose 120s + 门禁 90s |
| rollback_finalize | 内置 handler | 核实现场并新建成功 release，记录 replaces_release_id、restored_from_release_id 和证据 | 15s |
| stop_initial | Docker `C + ["stop","--timeout","10"]` | 仅首次无基线，确认项目此前为空且容器属于该 job；不删除卷 | 30s |
| release_preflight / finalize | 内置 handler | 漂移检测、预算检查、事务记录 | 各 15s |

枚举远程 refs 最多 10000 项/2 MiB，允许来源最多 100 个；超限拒绝并要求收紧登记规则，
不静默截断后继续授权。source_fetch 的 120s 是全部 fetch 共享预算，不是每个 ref 120s。
fetch 固定使用 `--no-auto-maintenance`、`--no-write-commit-graph`；安装自检验证所用 Git
版本支持这些 flags，不支持时须以受控配置禁用相应自动写入并验证后，才允许替换该 argv。
不能仅设置 `gc.auto=0`。人工仓库清理也须遵守维护约定。构建前重新检查冻结 SHA
仍存在，缺失则拒绝，不能改用分支当前 tip。恢复步骤同样取各步骤预算与剩余 300s 的较小值。

MVP 一个 build profile 产出一个业务镜像；api/worker 可以复用镜像。多个独立业务镜像的
构建图不在首版。submodule、Git LFS 先拒绝接入，不静默构建不完整源码。
Git archive 会应用 export-ignore/export-subst；项目必须确认这种快照语义，记录产物摘要。
解包不得直接使用不受限 extractall：限制成员数 100000、展开总量 1 GiB、文件 100 MiB；
拒绝绝对路径、..、设备/FIFO、硬链接、符号链接和重复覆盖，创建全新 job 专属目录，
不保留可提权权限。MVP 拒绝含源码符号链接的项目，不假装支持。

固定 BuildKit 模板（独立 rootless buildkitd 已由管理员配置）：

```text
executable: /usr/local/bin/buildctl
argv: ["--addr", "{app.buildkit_socket}", "build",
       "--frontend", "dockerfile.v0",
       "--local", "context={job.source_dir}",
       "--local", "dockerfile={job.dockerfile_dir}",
       "--opt", "filename={app.dockerfile_basename}",
       "--opt", "platform={app.platform}",
       "--output", "type=docker,name={job.unique_image_tag},dest={job.image_archive}"]
```

不允许请求附加 build args、frontend、secret、SSH agent、entitlement；profile 需要的非敏感
build args 由管理员登记。daemon 禁止 security.insecure/network.host entitlement，不启用
no-process-sandbox 捷径。联网构建按管理员的基础镜像/依赖出口策略限制；若目标机器无法
提供合适的 rootless 隔离，安装自检失败，不退回宿主机高权限 `docker build`。
构建目录采用低权限账户，制品经固定交接目录复制到 Runner 控制路径后校验大小/摘要；
确认构建会话和子进程已停止，防止检查后文件仍被修改。导入只接受 Docker image archive。
Compose 最终引用实际不可变 image ID，唯一 tag 只用于导入识别与保留，不信任可漂移标签。

固定测试容器生命周期（下列均 `/usr/bin/docker`，内部变量不可由请求覆盖）：

```text
create ["create","--name","{job.test_container_name}",
        "--label","io.drawbridge.job={job.id}",
        "--network","{app.test_network}","--read-only",
        "--tmpfs","/tmp:rw,noexec,nosuid,size=64m",
        "--cap-drop","ALL","--security-opt","no-new-privileges:true",
        "--user","65532:65532","--cpus","1","--memory","512m","--pids-limit","128",
        "--entrypoint","{app.suite_entrypoint}","{app.test_image_id}"]
start  ["start","{job.test_container_id}"]
wait   ["wait","{job.test_container_id}"]
logs   ["logs","--timestamps","{job.test_container_id}"]
stop   ["stop","--time","5","{job.test_container_id}"]
remove ["rm","{job.test_container_id}"]
```

上面是可选固定测试镜像模式，不是每个应用必须准备的前提。镜像预置本地并冻结 ID，
入口为管理员登记的绝对容器路径，无请求 argv。
suite 通过专用测试镜像的固定配置连接 service DNS，不使用宿主机网络；测试网络需管理员
预建并验证可达性/出口限制。CLI wait 返回 0 不代表测试成功：必须解析容器退出码，并核对
容器标签/ID。日志读取与等待并发且受限，超时先 stop，确认已停止后 rm；清理失败进入
NeedsAttention。读取日志不执行镜像中的代码，测试不挂源码或密钥；需要项目代码测试时
使用通用管理员测试 runner 镜像，只读挂载对应 SHA 快照到 `/workspace`，working_dir 固定
`/workspace`，命令来自 suite 模板。例如 Python 单元测试固定
`entrypoint=/usr/local/bin/python`、argv=`["-m","pytest","-q","-p","no:cacheprovider"]`。
prepare 在隔离环境执行管理员登记的依赖安装模板，网络/时间/磁盘均受限；不在部署账户
直接 pip install/运行 pytest。MVP 不承诺支持所有项目语言和依赖，缺 profile 时仅禁用该 suite。
测试输出和退出码形成独立 job 证据，不改变成功 release 状态。仍不开放 exec；首版 NPU 测试不启用。

项目测试 create 模板在固定安全参数后增加 `--mount` 的固定只读源码映射、`--workdir
/workspace` 和登记 argv；路径由冻结 release/source 生成，不接受请求提供的挂载或命令。
创建前核对 image ID/profile 和源快照摘要。支持 container_suite 与 source_suite 两种固定模式，
HTTP 验证不要求任何测试镜像；suite 名由 catalog 查询，不允许 AI 动态定义 suite 源码。

## 6. 操作配置格式与示例

schema_version=1。operation 只能选择 executable+argv 或预置 handler，不能同时存在。
配置禁止 YAML 自定义对象、重复 key、未知字段。handler 是代码中固定枚举，不是 Python
模块路径。模板占位符只能访问已声明参数/允许的配置与内部结果字段，禁止表达式求值。
管理员可收紧参数；放宽固定执行能力需要新增操作并审核，不能通过 YAML 加通用解释器。

```yaml
schema_version: 1
operations:
  git_log:
    executable: git # 引用管理员 toolchain，编译后为绝对路径
    argv_prefix: git_safe
    argv: [log, --no-show-signature, "--format=%H%x09%ct%x09%s",
           "--max-count={param.count}", "{job.resolved_sha}", "--"]
    prepare: resolve_allowed_local_ref # 固定预置 prepare，不接受代码
    parameters:
      git_ref:
        type: string
        min_length: 1
        max_length: 200
        pattern: 'refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*|refs/tags/[A-Za-z0-9][A-Za-z0-9._/-]*|[0-9a-f]{40}'
        validators: [allowed_app_ref, git_ref_or_commit]
      count: {type: integer, default: 20, minimum: 1, maximum: 100}
    cwd_from: app.repo_path
    execution_profile: source_manage
    public: true
    access: read
    timeout_seconds: 10
    accepted_exit_codes: [0]
    output: {policy: terminate, max_bytes: 65536}
  service_restart:
    handler: service_restart_and_verify
    parameters:
      service:
        type: string
        max_length: 64
        pattern: '[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}'
        validators: [registered_restartable_service]
      reason: {type: string, min_length: 1, max_length: 256}
    execution_profile: runtime_manage
    public: true
    access: runtime_write
    lock: app_environment
    timeout_seconds: 120
```

envelope 的 app/environment/idempotency_key/追踪字段不重复放在 parameters。
示例只展示 schema 用法；其余操作必须按 §§4–5 完整登记，缺失则启动校验失败。

## 7. MCP interface 与任务契约

外部工具沿用技术设计。ops_operation_run 仅接受 public=true；内部操作一律 FORBIDDEN_OPERATION。
部署、测试、重启、回滚及非 GET/HEAD 的 HTTP 验证都异步返回 job_id；查询返回
受限结果，诊断超出 HTTP 等待预算时返回诊断 job_id，统一由 ops_release_status
查询。ops_test 等便捷工具与通用入口不能各写一套执行逻辑。
workflow 首版使用 deploy_basic；旧名称 deploy_verify 兼容映射到同一默认流程，不再强制 smoke。
应用可在已登记配置中显式启用 smoke 门禁。测试和回滚使用内置任务型 handler，不开放动态步骤。

统一结果包含 request_id、status、data、error（code/message/retryable/retry_after_seconds），
以及适用的 job_id/release_id/next_cursor/truncated/observed_at。未发生变更的拒绝明确标记。
HTTP/IP/token 拒绝使用 403/401；工具业务错误按 MCP SDK 工具错误机制返回，不模拟 HTTP 状态。

新增明确错误：UNKNOWN_OPERATION、FORBIDDEN_OPERATION、INVALID_PARAMETER、NO_BASELINE、
IDEMPOTENCY_CONFLICT、QUEUE_TIMEOUT、STALE_PLAN、DRIFT_DETECTED、OUTPUT_LIMIT、
DISK_BUDGET_EXCEEDED、TARGET_NOT_ALLOWED、STALE_RELEASE、OUTCOME_UNKNOWN、APP_ALREADY_REGISTERED、
APP_NOT_DEPLOYABLE、CURSOR_EXPIRED、PATCH_BASE_MISMATCH、UNSUPPORTED_SERVICE_CHANGE、
ROLLBACK_PRECHECK_FAILED；其余使用技术设计列出的错误。TIMEOUT/VERIFY_FAILED 不表示恢复成功，
必须另返回 recovery.status 和 recovery.release_id。

幂等键全局命名空间，绑定 action/app/environment/规范化参数摘要；追踪字段不进入摘要。
同 plan 只有一个部署 job；去重先于维护、容量、冷却检查，返回已有任务不是新接纳。
plan 默认有效 15 分钟，冻结 SHA、source 摘要、workspace revision、runtime 模板/绑定摘要、
服务集合、secret reference 摘要、data_mounts、基线和 workflow；
仅流程启用测试门禁时冻结对应测试镜像/profile。独立测试 job 自己冻结 suite/profile。
配置变更、计划过期或基线变化拒绝执行；客户端失联不取消持久化任务。
若已有成功发布或接管基线，plan 阶段及 apply 的 preflight 都要求目标模板的服务
集合与固定基线相同；不同时返回 `UNSUPPORTED_SERVICE_CHANGE`，不开始 Compose 更新。

## 8. deploy_basic、恢复与运行约定

顺序：preflight → source_snapshot → image_build → image_import/identify → compose_deploy →
最小 health_check → finalize。smoke 是配置显式开启的可选门禁，不是部署的必需步骤。
总预算 1800s；步骤预算取登记值与剩余总预算的较小值。
恢复另有 300s，不消耗已到期的发布预算。构建前失败无运行时恢复；开始 compose up 前
持久化 runtime_change_started=true，此后即使 CLI 失败/被杀也检查现场并恢复。

健康检查默认：固定 HTTP URL、单次 3s、间隔 2s、90s 内连续 3 次成功，失败重置计数；
状态码 200，可登记固定 JSON 条件，无重定向。接入时要求固定健康 URL 的 host 属于
loopback 或该应用声明的网络范围，否则 NeedsSetup；`http_verify.allowed_cidrs` 仅约束
请求给出的按需 URL，不用于固定健康门禁。Compose --wait 只提供容器运行/健康信号，
不能替代业务验证。无 HTTP 健康地址时最小门禁检查所有预期服务 running 且登记容器
healthcheck 未 unhealthy，90s 内连续 3 次；响应明确 validation_level=runtime_only。
默认健康模式来自接入 profile，可配置 http；不要求开发前填写每个应用健康 URL。
Succeeded 只代表配置的部署门禁通过，business_verification 独立为 not_run/passed/failed。
部署后智能体使用状态/日志/HTTP/suite 组合验证，失败默认不自动回滚，由智能体明确调用
回滚操作；只有计划声明的部署门禁失败才触发自动恢复。可选 smoke 成功码默认 0。

自动恢复：当前 job 失败但成功恢复时状态 RolledBack，发布仍算失败；失败 job 的恢复
结果引用恢复基线并记录恢复原因/实际制品，不伪造一个新的成功发布 release。显式回滚
创建新 job/release，不修改旧成功 job 的状态。
无基线仅停止该首次发布创建的应用容器；预先存在未登记容器时 preflight 拒绝接管。
RollbackFailed/NeedsAttention 阻止该目标进一步变更，只读查询仍可用；管理员处理现场后
通过本机受控 reconcile 流程重新登记证据，不提供远程“强制忽略”参数。

显式 `ops_release_rollback` 是独立固定内置任务，不是调用方可选步骤的 workflow，
也不复用 deploy_basic 的 `on_failure` 作为主流程。它进入同一变更队列、全程持有
app_environment 锁，独立总预算 600s；运行时变更后的失败恢复另有 300s，不受
部署冷却限制。幂等键绑定 action/app/environment、目标历史 release、reason 的
规范化请求摘要；同键不同请求冲突，重复请求返回原 job。

步骤为 `rollback_preflight` → `rollback_deploy`（历史固定镜像/模板 Compose up +
历史冻结健康门禁）→ `rollback_finalize`。preflight 要求目标为该 app/environment
的历史成功 release，当前成功 release 可作为失败恢复基线；校验双方保留的镜像 ID、
release workspace、可信模板、secret reference、data_mounts、project_name 和健康
门禁存在且摘要一致，检查现场 drift 与固定服务集合。证据缺失或无可恢复当前基线时，
在运行时变更前以 Rejected/`ROLLBACK_PRECHECK_FAILED` 结束；服务集合差异返回
Rejected/`UNSUPPORTED_SERVICE_CHANGE`。这两类拒绝不记为 RollbackFailed。

进入 `rollback_deploy` 前先冻结当前成功 release，并持久化
`runtime_change_started=true`；一旦开始运行时更新，任何失败都在独立 300s 预算内尝试
用回滚前当前 release 恢复并执行其健康门禁。恢复成功保留失败 job 与现场证据；无法恢复
或无法判定现场时报告 RollbackFailed/NeedsAttention，不能承诺无部分更新。成功回滚
由 `rollback_finalize` 新建 release：`replaces_release_id` 指向回滚前当前 release，
`restored_from_release_id` 指向目标历史 release，另保存实际镜像、模板、密钥引用、挂载
与健康证据。finalize 写库失败先核实实际运行版本，进入 NeedsAttention。历史服务集合
若与当前固定集合不同，不在 MVP 中自动回滚，需管理员本机维护并重新确认基线。

运行默认值：read=16、running mutations=1（首版固定）、queue=50、每目标 queue=5、
queue timeout=600s、部署冷却=60s。所有非 read 操作共用该变更队列及唯一运行槽位，
包含 verification_write；排队等待不占运行槽位，自动恢复仍占用原任务槽位。
`http_verify.max_read_concurrency` 只限制并发 GET/HEAD；部署冷却入队和实际派发双检查，
冷却键为 (app, environment)，适用于 `ops_release_apply` 与
`ops_app_adopt_apply`；显式回滚、重启、测试和 HTTP 写验证豁免。配置校验在首版拒绝
`max_running_jobs` 大于 1。
锁顺序固定为 app_environment → repo → build → device；不持 SQLite 事务等待锁。
首版无 NPU 变更任务；只读 NPU 查询不申请设备独占。

维护模式同时约束 Gateway 新准入和 Runner 新派发，不能仅拦住新请求。
Gateway 将维护状态持久化到控制记录；Runner 每次派发读取，记录不可读则停止派发。
进行中的任务完成（包括恢复），排队任务不启动但过期时钟继续。退出维护后重查计划。
切换维护只需重启 Gateway，不重启部署中的 Runner；普通操作配置更新先维护、排空，
再重启双方。Runner 重启先核实遗留进程/构建/容器，不盲目重复副作用。

SQLite 最小表：plans、jobs、steps、releases、artifacts、workspace_revisions、idempotency_keys、events、
control_state、app_bindings、adoption_plans；binding 保存 source/release/runtime/data workspace、project_name、editable 文件和 data_mounts；job 保存 owner/heartbeat、目标、deadline、配置摘要、恢复结果，steps 保存
开始/结束、退出码、termination_reason 和日志引用。UNIQUE(plan_id) 用于部署 job；
UNIQUE(idempotency_key) 保证所有入口一致去重。诊断 job 不计入变更队列容量。
事件只追加，有限历史清理属于人工/受控保留任务，不对抗同权限账户篡改。

## 9. 应用接入与安装自检

管理员安装时填写：允许的项目根目录、托管发布/模板目录、rootless BuildKit socket、
默认接入/构建/隔离测试 profiles、允许 remote/ref 策略、HTTP 出站 CIDR/端口、磁盘预算，
并生成随机 Gateway bearer token 写入 Gateway 配置文件（权限 600）。

```yaml
# 接入和出站能力配置示意；实际网段、预算由部署者填写
allowed_project_roots: [/srv/projects]
managed_release_root: /srv/drawbridge/releases
managed_template_root: /var/lib/drawbridge/templates
managed_data_root: /srv/drawbridge/data
http_verify:
  allowed_cidrs: [192.168.0.0/16, 127.0.0.1/32, '::1/128']
  allowed_ports: [80, 443, 8080, 18080] # 按需要增减，不默认所有端口
  max_read_concurrency: 8 # 仅 GET/HEAD；写验证使用全局变更队列
  timeout_seconds: 10
  max_request_bytes: 65536
  max_response_bytes: 262144
  follow_redirects: false
```

用户在 Codex/Claude Code 指定 project_dir/compose_file；origin 可从受检查的本地 Git 配置
读取、架构由服务器识别、服务名/单镜像构建信息从受限 Compose 解析；缺少或不支持的
信息返回 NeedsSetup，不要求开发前提交应用全套参数。健康 URL、suite、重启服务、editable
文件和 data_mounts 收紧规则可选。Drawbridge 为新绑定生成唯一 project_name；接管时保留
已有名称，但发现同名且无法归属当前绑定的 Compose 容器就拒绝接管/部署。
Gateway 的 systemd `User=drawbridge-gateway`；Runner 的 `User=` 必须设为预克隆仓库
的普通属主，不以 root 运行。两个账号仅通过受限状态目录共享 SQLite；只有 Runner
拥有 Docker socket 权限。`/etc/drawbridge` 的能力配置、固定脚本、SSH wrapper 与可信
模板由管理员拥有，Runner 只读；Runner 可写的 release/runtime 产物按冻结摘要核验。
本模型信任本机仓库属主；构建/测试仍在独立隔离环境中运行，不继承 Runner 的 Docker
权限或 Git 凭据。
Dockerfile/context 必须是快照内无链接相对路径；编译后的可信 Compose 和 shell 脚本位于仓库外。
管理员策略 YAML 仍是能力权威来源；SQLite app_bindings 是受控运行时接入记录，不能携带
任意命令/env/profile。Runner 重验根目录、模板版本和内容；绑定更新使旧 plan 失效。
同名 YAML 预登记 app 不允许通过请求覆盖；禁止直接在线编辑管理员 YAML。
secret env_file 由管理员维护并使用不可变版本引用，工具不直接读取或返回；staging 使用专用
低权限凭据。release 记录 secret reference 摘要，摘要变化或引用缺失时回滚不能宣称成功。
保留当前、上一成功和回滚引用制品，成功历史默认 5、日志/幂等记录 7 天；清理不删除 data_mounts、卷或用户数据。
磁盘预算至少包含源码、镜像 tar、Engine 镜像与 BuildKit cache，安装时必须显式填写，
preflight 低于保留空间拒绝构建，运行期间监测硬预算；不能只限制日志目录。

NPU 默认 enabled=false，npu_status 返回 UNSUPPORTED。启用前填写厂商、型号、驱动、
固定程序绝对路径、固定只读 argv、所需设备权限、输出解析和超时；不得允许 AI 传入 vendor flags。
例如某型号的管理工具是否提供纯查询模式必须在实际服务器验证，不能把 `npu-smi .*` 放行。

安装自检必须真实验证：Python 3.12+、SQLite WAL 本地盘、Gateway/Runner 不同账号与
仓库 UID 一致、目录权限、固定 SSH wrapper 和 Git 配置漂移检查、fetch 禁止自动
maintenance/commit-graph 的实际行为、Git 安全配置、Git --end-of-options、Compose
--wait/--pull never、日志 JSON 解析、固定 Engine socket、
rootless BuildKit 隔离和网络、测试容器清理、Compose 模板权限及镜像 ID 可运行性。
锁定实际通过验收的 Git/Engine/Compose/BuildKit/MCP SDK 版本，不宣称所有旧版本兼容。
Gateway 显式 --no-proxy-headers，CIDR 默认 192.168.0.0/16 可收紧，Host/Origin 必须填写；
HTTP 无加密，token 不防窃听但默认启用：安装自检要求 Gateway 已配置 bearer token，
缺失则拒绝启动，仅管理员显式配置 auth.mode:none 时豁免。缺 NPU 工具只禁用该操作，
缺隔离构建能力阻止部署功能。

## 10. 实施顺序与完成标准

1. 配置编译器和 ExecutionSpec→ExecutionResult：strict 类型、fullmatch、argv/environment、
   输出预算、进程组收尾；先完成 git_status/git_log/process_list/内置配置诊断和 workspace_patch。
2. SQLite 任务、幂等、队列、诊断通道、锁、维护和恢复状态；接入 MCP HTTP tools。
3. Git plan/安全快照/rootless 构建/制品导入与识别；不先用危险 fallback 打通演示。
4. Compose 更新、最小健康/可选 smoke、重启、历史制品回滚、重启后的 reconcile；交付 systemd 与安装检查。

验收主线：Codex/Claude Code 指定目录或发现候选 → 确认接入 → 查询 catalog → 部署 commit →
获取状态/日志 → 按需 HTTP/登记测试验证 → 调整代码并提交 → 再次部署验证。
不以预先完善全部业务测试为条件。另完成健康失败恢复、可选测试门禁失败恢复、首次部署
失败、镜像缺失恢复失败和漂移检测，并覆盖：

- 在 Git/命令参数中 `;`、换行、`$(...)`、`-c`、URL、revision 表达式、未登记 ref/service/file、额外字段、
  数字字符串/bool、超长输入全部拒绝；合法 agent 分支和 count 边界通过。
- SHA 可达/不可达、fetch/local 来源区分；未经登记冻结的未提交改动不会进入构建；workspace patch 必须
  匹配 expected_revision、通过 Schema 且生成不可变 revision；源码解包不能逃逸。
- Runner 与仓库同 UID，fetch 后 `.git` 无异主文件；origin、insteadOf、include 或
  core.sshCommand 在接入后被篡改时网络 Git 操作拒绝；固定 SSH wrapper 生效，fetch
  不触发自动 maintenance/commit-graph。
- patch 新链的 base_commit_sha 可与工作区 HEAD 不同，但所有 editable 文件必须与该
  SHA 的干净版本相同；链内带外修改拒绝，完整 overlay 保留前次修改；archive 的
  pre/post digest 不符返回 PATCH_BASE_MISMATCH 且无部分快照。
- 同 plan 多入口/多幂等键只建一个 job；同键不同内容冲突；并发不超容量、冷却不可绕过。
- 多个子智能体并发提交部署与 HTTP POST：二者共用变更队列，任何时刻最多一个运行，
  另一个返回 job_id 并可查询排队/超时结果；带 release_id 的 POST 若在排队期间
  目标版本变化则拒绝且不发送；GET/HEAD 仍可并发查询。
- 维护时已排队任务不派发；只读不中断；旧计划/旧基线不能覆盖新版本。
- 同时大量 stdout/stderr、无效 UTF-8、超时子孙进程、BuildKit/测试容器实际停止和失败清理。
- Gateway/诊断/构建/测试不能接触部署 socket/凭据，workspace patch 不能修改 Compose、
  Dockerfile、部署模板、固定脚本或 secret。
- 构建摘要截断不终止，硬输出/磁盘预算触发终止；原始日志/密钥不从工具文件读取返回。
- Runner 在 up 前后、验证中、finalize 写库失败时退出：启动后报告真实现场，不重放部署。
- 显式回滚生成新记录；自动恢复不把失败发布标记成功；无基线和恢复失败无虚假承诺。
- 模板服务新增、删除或重命名时 plan/preflight 返回 UNSUPPORTED_SERVICE_CHANGE，
  Compose 无副作用；接管服务集合与模板不一致时拒绝。
- 显式回滚预检查发现制品/模板/密钥引用缺失时拒绝且无运行时副作用；更新中失败
  尝试恢复回滚前当前 release 并报告真实现场；成功回滚记录 replaces_release_id 和
  restored_from_release_id，历史健康门禁生效且不受部署冷却限制。
- 允许根目录内接入通过、越界/链接/危险 Compose 拒绝；服务自动解析；伪造/失效容器标签
  不触发接管；project_name 冲突或同名未知容器拒绝；源码/release/runtime/data 目录与现有
  数据不混淆；多 data_mounts 均在允许 data_root 内；不完整基线不承诺回滚。
- HTTP 合法网段/端口通过、越界及 DNS 重绑定拒绝；响应有上限、不带宿主机凭据、不跟随
  重定向；业务验证失败不自动回滚；无测试镜像也能完成 HTTP 验证与部署闭环。
- 未携带或错误 token 一律 401、白名单外来源 403；未配置 token 且未显式 auth.mode:none
  时 Gateway 自检拒绝启动。

交付物：Python 包、uv.lock、严格配置 schema/全套示例 YAML、两个 systemd 单元、
低权限 profile/BuildKit 接入说明、初始化与自检命令、示例应用、pytest/Ruff/mypy 验证和运维手册。
本文件是实施规格，不代表上述程序或环境已经实现/验证。

## 11. 命令行为参考

固定 ref 解析使用 Git 的验证模式和选项终止机制，快照采用 archive 而非工作区发布。
参见 [git rev-parse](https://git-scm.com/docs/git-rev-parse)、
[git archive](https://git-scm.com/docs/git-archive)。
Compose 更新与重启分开：restart 不应用配置改动，up 才用于更新。
参见 [Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)、
[Compose restart](https://docs.docker.com/reference/cli/docker/compose/restart/)、
[Compose logs](https://docs.docker.com/reference/cli/docker/compose/logs/)。
独立构建导出 Docker 镜像归档再导入 Engine，参见
[BuildKit 官方说明](https://github.com/moby/buildkit)及
[rootless 约束](https://github.com/moby/buildkit/blob/master/docs/rootless.md)。
