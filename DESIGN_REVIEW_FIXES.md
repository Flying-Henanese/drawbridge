# Drawbridge 设计审阅改造建议

本文档是对 [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md)（下称"设计"）与
[MVP_IMPLEMENTATION_SPEC.md](MVP_IMPLEMENTATION_SPEC.md)（下称"MVP"）的审阅结果，
列出 16 个建议修改点。每条包含：位置、现状（原文引用）、问题分析、建议改法、验收标准。

- 状态：待交叉验证。行号基于 2026-09-19 的版本，若文档已更新，以小节号为准。
- 优先级：P0 = 实现前必须解决（正确性或安全缺口）；P1 = 文档间不一致或语义歧义；
  P2 = 加固与运维完善。
- 标注【事实】的条目是可对照原文或外部工具文档核实的事实性声明；标注【判断】的
  条目是设计取舍建议，交叉验证时应重点复核事实类条目。

---

## P0：实现前必须解决

### R-01 在可变用户仓库中执行 Git 的信任与污染问题【事实 + 判断】

位置：MVP §3 统一 Git 前缀（102–118 行）、SSH wrapper 说明（120–122 行）、
`.git/config` 检查（123–125 行）、§4.1 source_workspace 定义（196 行）、
§5 source_fetch（312 行）、§5 GC 约定（331 行）。

现状：

> 统一前缀仅含 `core.hooksPath`、`core.fsmonitor=false`、`protocol.allow=never`、
> `protocol.ssh/https.allow=always`、`credential.helper=`。
> "SSH 使用固定管理员 wrapper、严格 known_hosts"（未写实现机制）。
> "预克隆仓库的 `.git/config` 亦属于可信配置：**接入时检查** origin URL、禁止额外 URL、
> insteadOf、include、代理、外部程序……"
> "计划期间不运行自动 Git GC"（331 行）。

问题（三个子项）：

1. **gc.auto 未关闭，GC 约定无法落实**。`git fetch` 结束时会自动执行
   `git maintenance run --auto`（旧版本为 `git gc --auto`），可能在仓库中写入
   commit-graph、重新打包对象。MVP 声称"计划期间不运行自动 Git GC"，但统一前缀
   没有 `-c gc.auto=0`，机制上是开口的。
2. **`core.sshCommand` 未在最高优先级覆盖**。Git 配置优先级为
   命令行 `-c` > local > global > system；`GIT_SSH_COMMAND` 环境变量高于任何
   配置文件中的 `core.sshCommand`。文档未写明 wrapper 的固定方式：若只写在
   `GIT_CONFIG_GLOBAL` 指向的全局配置中，用户仓库 local config 的
   `core.sshCommand` 会覆盖它。source workspace 是用户可写目录（196 行
   "可变 Git/项目目录"），`.git/config` 只在接入时检查一次，之后可被目录属主
   修改。同类问题：`url.insteadOf` 接入时禁止、接入后可添加，可把 origin
   重写到其他主机。MCP 路径确实改不了 `.git/config`（workspace_patch 受别名限制），
   风险面是目录属主的带外修改与检查时机之间的时间窗。
3. **fetch 写入用户 `.git`**。source_fetch 以 source_manage 账号在用户目录里创建
   对象、refs、FETCH_HEAD、`gc.log`，属主变为服务账号：用户之后自行
   `git gc/prune` 可能权限失败，且与用户自己的 Git 操作存在锁竞争。

建议：

1. fetch 模式改用 **Runner 自有 mirror**：每个 app 在托管根目录（如
   `/srv/drawbridge/repos/<app>`）维护 Runner 拥有的 clone；source_fetch、
   check_reachable、source_snapshot 全部在 mirror 中执行，用户仓库不被写入。
   mirror 容量纳入既有磁盘预算。
2. local 模式保持**只读**：rev-parse、merge-base、check-ref-format、ls-remote、
   archive 均不写仓库，可直接在用户仓库执行（安装自检时核实所用 Git 版本下
   这些命令无写入行为）。
3. 无论是否引入 mirror：统一前缀补 `-c gc.auto=0`；SSH wrapper 改用
   `GIT_SSH_COMMAND` 环境变量或 `-c core.sshCommand=` 固定，并在文档写明机制与
   优先级依据；每次 fetch 前重验 `.git/config` 关键项（origin URL、无
   insteadOf/include/外部程序），不只接入时检查一次。

验收：fetch 后用户 `.git` 内无服务账号拥有的新文件；在仓库 local config 预置
恶意 `core.sshCommand`/`url.insteadOf` 的用例被拒绝或不生效；fetch 全程无
auto-maintenance 写入。

---

### R-02 `workspace_patch` 的基准锚定与脏文件规则缺失【事实 + 判断】

位置：MVP §2 workspace_revision 行（53 行）、§4.2（260–275 行）。

现状：

> "必须属于 app/environment，并且基于本次 plan 的 commit SHA"（53 行）
> "文件必须在基准 SHA 中已存在且是普通文件"（261 行）
> "workspace revision 保存基准 commit SHA、允许文件的 patch/content digest 和
> 校验结果"（270–272 行）

问题：

1. **基准 SHA 的产生时机未定义**。创建一条 revision 链（首个 patch）时基准取什么？
   隐含是 source workspace 当前 HEAD，但没写。不写明则各实现自由发挥，plan 端
   "必须基于本次 plan 的 commit SHA" 会频繁失败且难以诊断。
2. **脏文件规则缺失**。editable 文件若在 patch 前已被用户手工修改（未提交），
   patch 打在脏内容上，而 source_snapshot 的 overlay 应用到该 SHA 的**干净
   archive**，两者内容错配。现有检查（普通文件、大小、Schema、敏感字段、
   revision）都不覆盖基准内容一致性。
3. **revision 链语义未定义**。第 N+1 个 revision 基于第 N 个还是都锚定同一
   commit SHA？`expected_revision` 只解决并发覆盖，不定义链模型。
4. **base 不匹配的错误码未定**。plan 端拒绝时返回 INVALID_PARAMETER 还是
   STALE_PLAN？智能体无法据此决定恢复动作（重新 patch 还是重新 plan）。

建议：

1. 写明：开启 revision 链时校验 source workspace `HEAD == 声明基准 SHA`，且
   editable 文件内容与 HEAD 版本一致（无未提交改动），否则拒绝并提示先提交或
   还原。
2. revision 逐文件记录 pre-digest（基准内容摘要）与 post-digest（patch 后摘要）；
   source_snapshot 应用 overlay 前核对 archive 中文件 digest == pre-digest，
   应用后核对 == post-digest，任一不符即失败并保留现场。
3. 明确链模型：同一基准 SHA 下 revision 构成线性链，`expected_revision` 指向
   前一 revision；更换基准 SHA 必须显式开启新链。
4. 新增错误码（如 `PATCH_BASE_MISMATCH`），并在文档写明智能体恢复动作：
   基于目标 SHA 重新生成 patch。
5. 补一段工作流说明：fetch 模式下 plan SHA 来自 origin 解析，打 patch 前应先
   确认本地 HEAD 即待发布 commit（或推送后以解析所得 SHA 为准），否则 revision
   无法被 plan 使用。

验收：脏文件 patch 被拒绝；overlay  digest 不符时失败且无部分写入；base 不匹配
返回专用错误码；同一基准的链式 patch 与换基准重开链均有测试覆盖。

---

### R-03 服务从模板移除后没有收敛路径（remove-orphans 死锁）【事实 + 判断】

位置：MVP §5 compose_deploy 行（321 行）、§4.1 drift 规则（224–226 行）。

现状：

> compose_deploy 固定为 `C + ["up","--detach","--no-build","--pull","never",
> "--wait","--wait-timeout","90"]`，明确"不 down、不 remove-orphans"（321 行）。
> "同一 project name 下的 Compose 容器必须全部能对应当前绑定；发现无法归属的
> 容器时标记 drift 并拒绝接管或部署"（224–226 行）。

问题推演：管理员更新可信模板删除 service B（保留 A）→ 下次部署 `up` 只重建 A，
**B 容器继续运行**（无 `--remove-orphans`），占用端口/资源并跑旧代码 → B 带有
正确的 project label，但不在当前模板服务集合中——按现行规则它是否属于
"无法归属当前绑定"存在歧义。无论算不算 drift，**没有任何工具路径能停掉 B**：
Drawbridge 不提供删除操作，人工删除又可能触发 drift 拒绝后续部署。该 app 的
部署通道实质卡死。镜像/配置可以回滚，服务集合却无法收敛，这与"模板是运行时
权威来源"的定位矛盾。

建议：

1. 模板编译（版本 bump）时由服务端 diff 新旧模板服务集合，生成
   `removed_services`（不接受请求输入）。
2. deploy_basic 在健康门禁通过后、finalize 前增加固定步骤：
   `C + ["rm","--stop","--force","{job.removed_service}"]`（逐服务渲染，
   argv 模板固定）；该步骤失败进入 NeedsAttention，但不把已成功的发布改判失败。
3. drift 判定补一条：project label 属于本绑定、但 service label 不在当前模板
   服务集合中的容器，标记 drift 并区分"待移除"与"未知来源"。
4. 接管路径同样处理：adopt_plan 展示容器集合与模板不一致时，要求管理员先收敛
   再走 apply。

验收：模板删除服务后部署闭环完成且旧容器消失；移除步骤失败进入 NeedsAttention
且发布状态不受影响；带正确 project label 但服务名未知的容器触发 drift。

---

### R-04 显式回滚的 workflow 规格缺失【事实 + 判断】

位置：MVP §5 restore_previous 行（324 行）、§8（488–489 行）、设计 §7 状态机
（480–505 行）。

现状：

> §5 仅有 `restore_previous` 一条内部命令（"历史制品 compose_deploy + 最小健康
> 检查……独立预算 300s"）。
> §8 仅有"显式回滚创建新 job/release，不修改旧成功 job 的状态"。
> 设计 §7 的 RollingBack 状态是**部署 job 内部**的自动恢复分支，不覆盖
> `ops_release_rollback` 触发的独立回滚 job。

缺失内容：步骤序列（是否含 release_preflight/finalize）、预算（沿用 300s 恢复
预算还是独立 workflow 预算）、锁（应取 app_environment 锁）、幂等语义、新
release 记录哪些证据、健康门禁取历史 release 冻结的配置还是当前绑定配置、
与部署冷却的关系。`ops_release_rollback` 是公开工具，规格粒度应与 deploy_basic
对等，否则实现者各自发挥。

建议：在 §5/§8 补 `rollback_basic` workflow 定义：

- steps：`rollback_preflight`（校验目标 release 的 workspace/模板/镜像/
  secret reference/data_mounts 存在且摘要一致）→ `restore_previous`（历史模板
  compose up + 最小健康检查）→ `finalize`（写新 release，`rollback_of` 指向
  目标 release）。
- 全程持 app_environment 锁；独立预算建议 600s（restore 300s + 前后余量）。
- 健康门禁使用历史 release 冻结的配置（回滚的意义即恢复历史现场，当前绑定
  可能已变更）。
- 证据不全 → `RollbackFailed`，不留部分状态；不受部署冷却限制（见 R-11）。

验收：回滚 job 的步骤/锁/预算有明确登记；制品缺失、模板摘要变化、secret
reference 失效三条路径分别返回 RollbackFailed 且不宣称成功。

---

### R-05 `ops_status` 的主机指标操作未登记【事实】

位置：MVP §4 表后文字（147–148 行）、设计 §5 ops_status 行（137 行）。

现状：

> "主机聚合 CPU/内存/磁盘通过内置只读 handler 获取，不开放自由路径"（MVP
> 147–148 行）——只有这一句 prose，§4 操作表中没有对应行。
> 而 ops_status 承诺返回"CPU/内存/磁盘、服务健康、当前 release、关键端口"
> （设计 137 行），是最高频工具。

问题：这个内置 handler 没有操作名、execution_profile、超时、数据来源和字段
白名单，不满足"其余操作必须按 §§4–5 完整登记，缺失则启动校验失败"（MVP §6，
444 行）的自身约定。

建议：§4 表补一行，例如 `host_metrics`：内置 handler、host_observe、cwd `/`、
10s、read；写明数据来源（直接读 `/proc/stat`、`/proc/meminfo` + 对登记挂载点
statfs，或查询 node_exporter，二选一并写死）；字段白名单为聚合百分比与绝对值，
不含 per-process 明细；并在 ops_status 处说明组装逻辑 = host_metrics +
compose_status + releases 表当前 release + 模板声明端口。

验收：启动校验覆盖该操作登记；ops_status 输出字段与白名单一致，无计划外字段。

---

## P1：文档间不一致与语义歧义

### R-06 `ops_logs` 的 service 可选性与 argv 模型冲突【事实】

位置：设计 §5 ops_logs 行（138 行） vs MVP §4 compose_logs 行（138 行）、
MVP §3 argv 渲染规则（88–89 行）。

现状：设计写"可选 `service`"；MVP 的 compose_logs 把 `service` 列为必填参数且
argv 固定含 `{param.service}`。而 §3 规定"每个模板数组元素产生一个 argv；允许
固定前后缀插值"——该模型**不支持可选槽位**（省略 service 时 compose logs 语义
变为全部服务，argv 形状不同）。

建议：以 MVP 为准，service 必填，回改设计文档。若未来要支持全服务日志，登记
独立 operation（如 `compose_logs_all`，argv 不含 service 槽位），不要引入条件
argv。

### R-07 诊断类操作的 cwd 示例与 MVP 快照语义冲突【事实】

位置：设计 §5 check_project_config 示例（211 行 `cwd_from: app.repo_path`）、
"config_read 默认只读已提交项目配置"（194 行） vs MVP §4（139–143 行、
149–152 行）。

现状：MVP 规定诊断类操作（config_read/project_list/config_validate/
check_project_config）一律针对"固定源码快照"（当前成功 release 的快照，未部署
时为接入诊断快照），设计示例却仍写可变工作区 `app.repo_path`。虽然 MVP 开头有
"以本文件为准"条款（6 行），但示例是实现的直接参照，不改会误导。

建议：把设计 §5 示例的 cwd 改为 release 快照路径占位（如
`{release.snapshot_dir}`），措辞统一为"固定源码快照"，并注明与 MVP §4 的对应
关系。

### R-08 verification_write 的队列归属未定义【事实 + 判断；已按 MVP 决策修订】

原问题：MVP §4.3 只说非 GET/HEAD 的 http_request 进入"有界队列"，未说明它与
部署队列的关系；配置中的 HTTP 最大并发数也可能被误解为独立写任务槽位。

决议：首版只有一个 staging 应用，按单一操作者、多个子智能体并发请求设计。
verification_write 与部署、回滚、重启、测试等所有变更共用持久化队列，
`max_running_jobs=1` 首版固定；部署和自动恢复全程占用运行槽位。写验证不另设
队列、并发池或应用锁，排队适用统一容量和超时；无 app 的写请求共享固定的队列目标，
带 release_id 的请求在派发前重查当前版本，变化则返回 STALE_RELEASE。HTTP 1–30s
执行超时从派发起算。
`http_verify.max_read_concurrency` 只用于 GET/HEAD。未来提高全局变更并发前，
必须先定义写验证的目标归属和目标级互斥。技术设计 §7/§8 与 MVP §4.3/§7–§9
已同步该规则。

验收：两个子智能体同时提交部署与 HTTP POST 时只运行一个变更，另一任务排队；
排队超时或目标版本变化时不发送 HTTP 请求；GET/HEAD 仍可并发查询。

---

## P2：加固与运维完善

### R-09 health_check 与 http_verify 出站策略的关系未说明【判断】

位置：MVP §5 health_check 行（322 行）、§4.3。

建议写明：health_check 走管理员固定 URL 的可信路径，**不**查
`http_verify.allowed_cidrs`（该策略约束的是请求提供的任意 URL）；但接入校验时
要求健康 URL 的 host 为 loopback 或应用声明网段，不满足则注册 fail-fast，
避免部署时才暴露。

### R-10 构建证据缺少基础镜像 digest【判断】

位置：MVP §5 构建段（340–359 行）；设计示例中测试镜像已 digest pin
（377 行），业务镜像的 FROM 未要求。

建议：release 证据增加"构建时基础镜像解析后的 digest"（buildctl metadata
可获取）；策略上可选开启"Dockerfile FROM 必须 digest pin"，由接入 lint 检查。
不主张首版承诺构建可复现，只补证据链。

### R-11 冷却适用范围应枚举【事实 + 判断】

位置：设计 §7（561–565 行"冷却只约束部署类入口"）、MVP §8（495 行）。

建议列清单：`ops_release_apply` / `ops_app_adopt_apply` 受冷却；**回滚与重启
豁免**（应急路径，回滚被 60s 冷却挡住是明显反模式）；写明冷却键为
(app, environment)。

### R-12 Gateway 认证失败与入口上限【判断】

位置：设计 §8（608–627 行）、架构图（75 行"请求大小与并发限制"无数值）。

建议：补认证失败事件（只记 token 哈希，不记值）与可选 per-IP 节流；量化
ingress 全局请求体上限（如 1 MiB，与各字段上限叠加生效）。

### R-13 日志 cursor 快照的总量上限与过期错误码【事实 + 判断】

位置：MVP §4（165 行"先拉取有界快照……cursor 固定该快照"）、§2 cursor 行
（57 行，10 分钟过期）、§7 错误码列表（458–461 行，**无游标过期错误码**）。

建议：写明快照存储位置与总量上限（如最多 64 个活游标、单游标快照 ≤ 1 MiB、
LRU 淘汰）；淘汰/过期返回专用错误码（如 `CURSOR_EXPIRED`），否则智能体无法
区分"游标格式错误"与"快照已回收"。

### R-14 保留清理任务的执行主体与规则【事实 + 判断】

位置：MVP §8（509 行"有限历史清理属于人工/受控保留任务"）、§9（544 行保留
策略）。

现状只写了保留策略数值，没写谁执行。建议明确为 Runner 内置定时 sweep（如
每小时）：只删除未被当前 release / 上一成功 / `rollback_of` 链 / 运行中 job /
接管基线引用的制品；幂等键 >7 天、诊断 job >24 小时、job 日志按 retention
清理；events 表给出保留期（如 90 天）或导出后清理流程，否则会无限增长。

### R-15 app 注销应列为显式非目标【判断】

位置：设计 §2 非目标（56–60 行）。

建议补一条：首版不提供 app 注销/移除工具；移除绑定属人工运维动作，并需先
处理运行中容器的归属（停止或脱离 project），否则会与 drift 检测冲突。

### R-16 Runner 派发唤醒机制未写【判断】

位置：设计 §7 队列段、SQLite 认领段（720–721 行）。

建议写明 Runner 感知新 job 的机制（如 1s 轮询或基于 SQLite 变更的通知），
给出"入队到派发"的延迟预期，便于调用方设置轮询节奏。

---

## 交叉验证指引

给复核模型的建议：

1. **事实类条目优先核对**：R-01（Git 配置优先级、`fetch` 触发 auto-maintenance、
   只读命令集合是否真无写入）、R-03（`compose up` 不带 `--remove-orphans` 时
   被移除服务的实际行为）、R-05/R-06/R-07/R-13（对照原文行号即可证伪）。
2. **判断类条目看是否与文档其他章节冲突**：例如 R-03 的移除步骤位置（健康门禁
   后）与"失败即恢复"语义的交互；R-08 的全局单任务槽位与只读并发区分；R-04 的
   预算数值与 restore_previous 300s 的关系。
3. 每条"验收"小节可作为实现后的测试用例种子。
