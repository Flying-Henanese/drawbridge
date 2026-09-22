# Drawbridge 安全与可靠性优化改造计划

状态：实施中（01A、01B、01C、02 已完成）。编写于 2026-09-21。本文是后续编码任务清单；未勾选的能力不表示已经实现。

## 使用方法与范围

下次使用小模型时，从下表**第一个未完成的任务**开始，一次只实现一个编号。先读
[智能体入口](../.harness/README.md)、[当前架构](../.harness/context/ARCHITECTURE.md)、
[开发守则](../.harness/rules/README.md)及该任务涉及的源码和现有测试。先执行
`git status --short`，保留已有改动。完成一个编号的代码、聚焦测试、完整 harness 和
文档更新后，再勾选该编号；不要把整份计划当作一个提交。

计划依据是 2026-09-21 对当前源码的审查和四个临时目录复现：Compose 接受含 `..` 的
相对宿主挂载；两个并发入队请求中一个报 SQLite `cannot start a transaction within a
transaction`；Runner 重启后旧 `running` 任务使下一任务无法认领。另一个复现表明，注册
时只有 `app` 服务、后续 Git 提交增加 `extra` 服务时，simulation 仍能以旧服务集合完成
发布。本机忽略目录 `var/verification/20260920T163108Z/report.json` 的 harness 结果为
`passed`，但该文件不随仓库分发，且只覆盖本机 simulation；
[历史验证记录](VERIFICATION_RECORD.md)也不是当前服务器就绪证明。

| 顺序 | 任务 | 依赖 | 完成标记 |
| --- | --- | --- | --- |
| 01A | Compose 宿主挂载路径 | 无 | [x] |
| 01B | Compose 其他宿主能力 | 01A | [x] |
| 01C | 管理员批准的可信 Compose | 01B | [x] |
| 02 | 从冻结 SHA 校验发布计划 | 01A–01C | [x] |
| 03 | SQLite 事务与 Gateway 初始化 | 无 | [ ] |
| 04 | Runner 中断检测与保守恢复 | 03 | [ ] |
| 05 | HTTP 连接目标与 CIDR 校验一致 | 无 | [ ] |
| 06A | Docker 变更阶段持久化 | 02、04 | [ ] |
| 06B | Docker 现场核对与管理员恢复入口 | 06A | [ ] |
| 07 | Docker 显式回滚 | 06B | [ ] |
| 08 | Docker 发布失败自动恢复 | 07 | [ ] |
| 09A | 内部权限契约 | 03、06B | [ ] |
| 09B | Gateway 与 Runner 权限迁移 | 09A | [ ] |
| 10A | Git 阻塞调用 | 09B | [ ] |
| 10B | Docker 发现性能 | 09B | [ ] |
| 11 | 真实服务器验收及文档收口 | 01A–10B | [ ] |

`01A–05` 是本地即可完成的风险修复；`06A–10B` 完成本地实现与替身测试后可以勾选，
但其真实运行能力须在任务 11 单独验收。任一任务改变 MCP 返回字段或任务状态时，同时更新
[README](../README.md)、[当前架构](../.harness/context/ARCHITECTURE.md)、
[发布调用流程](../.harness/context/RELEASE_FLOW.md)中受影响的描述。

## 01A. 收紧 Compose 宿主挂载路径

**目标：**未经管理员登记的 Compose 内容不能让 Runner 挂载发布目录外的宿主路径。

- 修改 [compose.py](../src/drawbridge/compose.py) 的 `parse_compose()`、`_validate_volumes()`。
  对短语法和 `type: bind` 长语法统一识别宿主源；拒绝 `..`、越界、符号链接和非预期绝对
  路径。不要把命名卷当成本地相对路径。需要持久化宿主目录时，仅允许与管理员登记的
  `data_mounts` 的源、目标和访问模式精确匹配；把允许挂载列表显式传给解析器，在注册
  和发布快照上应用同一规则，不依赖全局可变状态。若现有
  [DataMountConfig](../src/drawbridge/config.py) 不足以表达访问模式，先补严格配置字段。
- 修正先 `resolve()` 再检查 `is_symlink()` 的顺序，检查原路径及每个父路径。
- 在 [test_validation.py](../tests/test_validation.py) 增加表驱动用例：短/长语法
  `../../../../etc`、符号链接必须拒绝；正常命名卷、
  受允许的相对文件和已有 Compose 用例必须通过。再用 [test_service.py](../tests/test_service.py)
  验证注册与发布快照都执行策略，失败时没有入库的 deployable binding 或 Docker 调用。

**验收：**越界挂载在注册与执行前均失败；安全示例仍能完成 simulation。

## 01B. 审核 Compose 其他宿主能力

**目标：**未审核的 Compose 字段不能绕过任务 01A 的挂载限制或扩大宿主权限。

- 审核 [compose.py](../src/drawbridge/compose.py) 接受的顶层 `volumes`、`configs`、
  `secrets`、`networks`，以及服务级 `volumes_from`、`userns_mode`、`uts`、`ports` 等
  可影响宿主或网络的字段。为受支持字段建立显式策略；`driver_opts` 之类的宿主绑定
  入口须拒绝或匹配管理员配置。未知能力字段默认拒绝，并在错误中指出字段名。保留
  当前安全用例所需的 `image`、`build`、`command` 等字段。
- 在 [test_validation.py](../tests/test_validation.py) 覆盖顶层卷绑定、`volumes_from`、
  不受支持的宿主/网络键和正常命名卷；在 [test_service.py](../tests/test_service.py)
  证明从 Git 快照重新校验后才可能触发 BuildKit 或 Docker。

**验收：**每种受支持的宿主能力都有明确来源与测试；其他能力在执行前被拒绝。

## 01C. 管理员批准的可信 Compose

**目标：**允许管理员为既有且受同一权限边界控制的项目显式放宽 Compose 策略，同时保持默认
严格模式，并可强制只使用 Compose 中已有的 `image`、不执行 `build`。

- `RuntimeProfileConfig` 提供严格校验的 `approved_compose_digests` SHA-256 列表和
  `prefer_prebuilt_images` 布尔字段；后者要求至少一个批准摘要。仓库内容不能自行选择 profile。
- 只有 Compose 原始文件摘要与管理员批准值精确匹配时，才允许额外服务字段、顶层资源、宿主
  能力和环境变量插值。摘要不匹配时默认恢复严格校验；若开启 `prefer_prebuilt_images` 则直接
  拒绝，避免意外回退到 BuildKit。Compose 仍须位于登记项目中、不是符号链接、大小和服务结构
  合法，并拒绝顶层及服务级 `include`/`extends`。
- `prefer_prebuilt_images` 将同时声明 `image` 与 `build` 的服务视为预建镜像服务；Runner 继续
  使用 `docker compose up --no-build --pull never`，不调用 BuildKit。只有 `build`、没有 `image`
  的服务直接拒绝。
- 默认 profile 和未启用预建选项的摘要不匹配 profile 保持严格行为。注册和 Git 发布快照使用
  binding 中冻结的同一 profile 复验。

**验收：**严格模式的既有拒绝测试继续通过；可信 profile 可以注册并发布已批准的旧 Compose；
预建模式的 executor 记录只包含 Compose 部署，不包含镜像构建、导入或识别步骤。

## 02. 按冻结 SHA 校验计划与实际快照

**目标：**计划展示的服务集合、构建策略和配置摘要与 Runner 实际将部署的内容一致。

### 行为约束

- Git ref 只在计划阶段解析一次，plan 中保存完整的 40 位 `commit_sha`。Runner 必须按该 SHA
  导出源码，不得重新解析分支、tag 或读取当前工作区。计划创建后同一分支继续产生新提交，
  旧计划仍部署原 SHA；未提交的工作区修改也不得影响计划或执行结果。
- Compose 和 build 校验的输入是“冻结 SHA 的 archive + 指定 workspace revision”。revision
  必须在解析 Compose 和计算摘要之前应用，并继续校验其 app、environment、base SHA、文件
  pre/post digest。没有 revision 时不得隐式使用 binding 的 `current_revision`。
- 注册时的服务集合仍是允许的固定拓扑。计划快照中的排序后服务名必须与
  `binding["services"]` 相同，也必须符合当前成功 release 的服务集合；新增、删除或重命名
  服务在计划阶段返回 `UNSUPPORTED_SERVICE_CHANGE`，重新注册不在本任务范围内。
- 任务 01A/01B 定义的 Compose 策略以及 `validate_build_declarations()` 必须在计划快照和最终
  快照上使用相同参数执行。新增挂载或 build 声明不应一概按文本变化拒绝：符合管理员策略的
  内容可以进入计划并被冻结；越界挂载、未授权宿主能力或不匹配 build profile 的声明在计划
  阶段直接拒绝。
- 所有 plan 一致性检查必须在 `build_images()`、Docker/BuildKit executor、simulation release
  写入和其他发布副作用之前完成。计划失败不得写入 plan；执行阶段失败不得创建成功 release。

### 实现步骤

1. 在 [service.py](../src/drawbridge/service.py) 抽取计划和执行共用的快照准备原语，避免
   `release_plan()` 与 `_create_release_snapshot()` 分别实现 archive、revision overlay、Compose
   解析和 build 校验。该原语只接收完整 SHA、binding 和显式 revision ID，按以下顺序执行：

   ```text
   git archive 完整 SHA
   → 使用 GitRepository.extract_archive() 解压
   → 应用并校验 workspace revision
   → parse_compose(..., allowed_data_mounts, runtime_profile)
   → validate_build_declarations(compose, build_profile)
   → 生成规范化指纹
   ```

   继续使用 [gitops.py](../src/drawbridge/gitops.py) 现有的 archive 成员数、单文件大小、总展开
   大小和特殊文件限制，不另写宽松解压逻辑。计划阶段使用受控临时目录；archive、临时快照和
   半成品目录在成功、校验失败、Git 失败及 revision 失败路径都用 `finally` 清理。最终发布快照
   沿用现有“运行时变更开始后保留制品”的规则，本任务不要改变该失败恢复语义。
2. 定义唯一的“计划指纹”构造函数，并在计划和执行阶段复用。plan payload 至少保存：

   - `plan_schema_version`：本任务引入的固定整数版本；缺失或不支持的版本视为旧计划；
   - `commit_sha` 和显式的 `workspace_revision`；
   - `workspace_revision_digest`：对 revision 的 base SHA 和排序后的文件 path、pre/post digest
     计算摘要；无 revision 时使用明确的空值；
   - `service_set`：按服务名排序后的列表，不依赖 YAML 键顺序；
   - `compose_digest`：对 `parse_compose()` 成功后的 `ComposeSpec.raw` 使用现有 `_digest()`
     规则计算，即 UTF-8 JSON、键排序、紧凑分隔符和 SHA-256；不得包含临时目录绝对路径；
   - `build_declaration_digest`：按服务名排序，只包含每个服务经校验的 `build` 声明及所匹配的
     context/dockerfile；没有 build 服务时也保存稳定的空结构摘要；
   - `binding_version` 和 `configuration_digest`；后者由一个固定字段白名单构造，包含实际影响
     发布的 binding 策略（Compose 相对路径、origin/ref 规则、登记服务、部署模式、项目名、
     数据挂载、runtime profile、健康检查、release/data root 和 build profile 名），排除
     `version`、`status`、`registered_head_sha`、`current_revision` 等数据库或默认 revision 状态；
   - `build_profile_digest`：始终冻结完整 `BuildProfile.model_dump()` 的摘要，即使 Compose 当前
     没有 build 服务也必须保存和检查；
   - 现有 `baseline_release_id`、workflow、project name 等执行所需字段。

   如果 Compose 的已校验结构不能被规范化为上述 JSON，应返回受控的 `INVALID_PARAMETER`，
   不得泄漏原生序列化异常。原始 Compose 文本、注释、空白和 YAML 键顺序不直接参与摘要；
   相同解析结构必须产生相同摘要。
3. 修改 `release_plan()`：解析 ref 和校验 revision 后立即创建临时快照，以快照的 Compose 而非
   `binding["services"]` 构造返回值及 plan 指纹。先完成固定拓扑、Compose、build 和当前 release
   检查，再调用 `save_plan()`；任何失败都不能留下可 apply 的 plan 或临时文件。
4. 修改 `release_apply()`、`_create_release_snapshot()` 和 `_execute_deploy()`：apply 时先拒绝缺少或
   不支持 `plan_schema_version` 的旧 plan，错误为 `STALE_PLAN` 且 `retryable: true`，不得为其
   入队；Runner 仍须重复这一检查，不能信任数据库中的排队 payload。重新生成最终快照指纹并
   逐项比较；`service_set`、revision、Compose、build 声明、binding 配置或 build profile 任一
   不一致均返回 `STALE_PLAN`，message 指出不一致的字段但不回显完整配置。执行阶段发现服务
   拓扑不符合 binding 时仍可返回 `UNSUPPORTED_SERVICE_CHANGE`。必须实际比较现有
   `configuration_digest`，不能只保存；profile 检查不能只放在 `compose.build_services` 分支。
5. 不修改现有 MCP 工具参数。`ops_release_plan` 返回的 `services` 必须来自冻结快照；若新增公开
   摘要或错误语义，更新 [README](../README.md)、[当前架构](../.harness/context/ARCHITECTURE.md)
   和 [发布调用流程](../.harness/context/RELEASE_FLOW.md)。旧 plan 无需迁移或补写，统一要求客户端
   重新创建计划。

### 测试矩阵

在 [test_service.py](../tests/test_service.py) 使用临时 Git 仓库和受控 executor 覆盖：

- 注册只有 `app`，随后提交 `app + extra`：`release_plan()` 返回
  `UNSUPPORTED_SERVICE_CHANGE`，数据库没有可 apply 的 plan；
- 注册后提交越界挂载、01B 禁止字段或不匹配 profile 的 build 声明：计划阶段返回对应的受控
  错误，而不是等到 Runner 才失败；符合管理员挂载/build 策略且服务集合不变的提交可成功计划；
- 同一 SHA、同一 revision 正常发布；plan 返回的服务和最终 release 服务来自同一快照；
- workspace revision 修改 Compose 时，计划与执行均在 overlay 后计算同一指纹；revision 属于
  其他 app、base SHA 不同、内容 digest 不符时不保存 plan；
- 创建计划后修改工作区但不提交，或让分支前进到新提交：Runner 仍部署 plan 中的旧 SHA，证明
  没有读取工作区或重新解析 ref；
- 篡改/构造旧 plan，使 schema version、service set、Compose digest、build 声明 digest、revision
  digest、configuration digest 或 build profile digest 分别不匹配：job 以 `STALE_PLAN` 失败；
  对无 build 服务的 plan 也验证 profile 变化会失败；
- 上述计划或执行失败路径中，替身 executor 没有收到 BuildKit、镜像导入或 Compose 命令，数据库
  没有成功 release，临时 archive/目录全部清理；保留现有同 plan 多 idempotency key 只产生一个
  job 的行为；
- 两份仅注释、空白或 mapping 键顺序不同、但解析结构相同的 Compose 输入产生相同摘要；实际字段
  值改变时摘要必须不同。

先运行任务 02 的聚焦测试，再执行本文末尾的完整 harness 和 `git diff --check`。在
[VERIFICATION_RECORD.md](VERIFICATION_RECORD.md) 记录报告路径，并明确本任务仍未覆盖真实
Docker/BuildKit 运行。

**验收：**计划只可能由冻结 SHA（加显式 revision）的已校验快照创建；Runner 在任何发布副作用
前证明最终快照与 plan 指纹完全相同。不能再出现计划为 `app`、实际快照含 `app + extra` 却报告
成功的情形；旧格式 plan 和任一指纹不一致都要求重新计划。

## 03. 修复 SQLite 并发事务和重复初始化

**目标：**Gateway 并发请求保持原子性、队列容量和幂等约束，不发生连接级事务冲突。

- 修改 [storage.py](../src/drawbridge/storage.py)：将 `enqueue_job()`、
  `claim_next_job()` 等多语句写操作包在**独占完整事务**内。可选方案是每个事务独立连接，
  或在同一连接的全部数据库 API 周围使用一致的异步锁；不能只锁住 `BEGIN`。同时审查
  `save_binding()` 的读版本再写版本、release 与 job 终结的原子性。
- 修改 [gateway.py](../src/drawbridge/gateway.py)：schema 初始化只在进程启动时做一次，
  不在每个 MCP 工具调用中执行 `executescript()`；Gateway 和 Runner 仍共享同一数据库文件。
- 在 [test_storage.py](../tests/test_storage.py) 用 `asyncio.gather()` 同时入队不同请求、
  相同幂等键、同 plan 不同键，以及超过全局/目标容量的请求；断言无原生 SQLite 异常，
  返回 job 数、去重和容量正确。增加两个 `Database` 实例并发认领同一 job 的测试。
  在 [test_gateway.py](../tests/test_gateway.py) 验证并发工具调用不重复初始化 schema。

**验收：**先前的双入队复现稳定通过；失败请求不会使成功请求的数据回滚或部分提交。

## 04. Runner 中断检测与保守恢复

**目标：**Runner 异常退出后，队列不会永久停住，也不会盲目重复有外部副作用的任务。

- 修改 [runner.py](../src/drawbridge/runner.py)、[service.py](../src/drawbridge/service.py)
  和 [storage.py](../src/drawbridge/storage.py)。为认领任务记录唯一 owner 和定期心跳；
  在派发前检测超过租约的 `running` 任务。进程 PID 不能单独作为唯一 owner；租约阈值
  必须大于现有最长同步阻塞段，并测试长操作期间心跳不会误判存活任务。
- 过期任务先进入可查询的 `needs_attention` 状态并记录事件、最后心跳、已开始的阶段；
  不自动重放 deploy、rollback、restart 或 HTTP 写请求。对可能已经开始运行时变更的
  任务暂停后续变更，直到任务 06B 的现场核对能确定结果。对明确未开始副作用的任务，
  可以安全地终结为失败并释放槽位。
- 在 [test_storage.py](../tests/test_storage.py) 与 [test_service.py](../tests/test_service.py)
  模拟认领后进程消失、长任务持续心跳、旧 owner 迟到写入、新 Runner 启动；断言不会
  同时运行两个变更，不会重放 HTTP 写，状态查询能说明阻塞原因。

**验收：**旧 `running` 任务不会永久静默占槽；结果不确定时系统明确停派并要求核对。

## 05. 使 HTTP 实际连接目标遵守 CIDR 策略

**目标：**`allowed_cidrs` 检查的 IP 与真正建立 TCP 连接的 IP 是同一个。

- 修改 [httpverify.py](../src/drawbridge/httpverify.py)。当前代码先 `getaddrinfo()`，
  后以原 URL 调用 HTTP 客户端；设计固定连接 IP 的传输层，同时保留原域名的 Host、
  HTTPS SNI 和证书校验。不能通过关闭 TLS 校验来解决。若无法在现有 HTTP 客户端中
  安全固定地址，先让非管理员 HTTP 工具只接受白名单内的 IP 字面量，并在接口文档中
  明确这一限制；管理员登记的健康检查单独处理。
- 对多 A/AAAA 记录逐一校验，连接只从已批准地址中选择；禁止重定向和环境代理的现有
  约束继续有效。保持请求/响应大小、超时和 header 限制。
- 在 [test_httpverify.py](../tests/test_httpverify.py) 用替身 DNS/传输层模拟两次解析结果
  不同、混合允许与拒绝地址、IPv4/IPv6、HTTPS SNI、连接失败；断言任何实际连接都
  不会落到拒绝地址。不需要访问真实内网或外网来做单元测试。

**验收：**DNS 改变不能使请求越过 CIDR 限制；合法 HTTP/HTTPS 检查仍返回有界证据。

## 06A. 持久化 Docker 变更阶段

**目标：**Compose 更新开始、健康检查和写库之间任意位置中断后，留有足够证据供现场核对。

- 修改 [service.py](../src/drawbridge/service.py) 与 [storage.py](../src/drawbridge/storage.py)。
  复用现有 `steps` 表或增加明确的持久化阶段：预检查、构建完成、运行时变更即将开始、
  Compose 结果、健康检查、最终写库。每步关联 `job_id`、前一成功 release、目标 release、
  可信运行时 Compose 路径和镜像 ID/摘要。**先提交阶段记录，再执行对应外部副作用**。
- 将成功 release、事件、job 终态在同一数据库事务中完成；写库失败时仍保留现场
  与制品供核对。不能仅依据 `.drawbridge-runtime-started-*` 文件推断当前状态。
- 在 [test_service.py](../tests/test_service.py) 使用受控 executor 替身，在 Compose
  前、Compose 后、健康检查中、release 写库时分别注入失败或中断；断言阶段记录、
  制品保留、job 状态和停派行为与实际副作用一致。

**验收：**每个外部副作用都有先行持久化阶段；中断后能定位最后已确认的阶段。

## 06B. 核对 Docker 现场并提供管理员恢复入口

**目标：**能判定中断任务的实际运行状态；不能证明时保持停派和清晰的待处理提示。

- 使用 [service.py](../src/drawbridge/service.py) 与 [storage.py](../src/drawbridge/storage.py)
  的任务 06A 记录，对照 Docker Compose 项目标签、容器镜像 ID、服务集合与健康结果。
  更新 `ops_status` 和 job 查询，让数据库中的“上一成功 release”与“已核对的当前现场”
  明确区分；无法证明现场时标记 `needs_attention`，阻止后续变更。
- 增加固定操作的管理员核对命令：只接受 job ID，复查记录与 Docker 现场后给出确定结果
  或继续保持停派；不得提供任意 SQL、Docker argv 或人工声称“成功”的参数。
- 在 [test_service.py](../tests/test_service.py) 覆盖现场仍为旧版本、已到新版本、部分
  更新、缺容器、缺镜像及 Docker 不可用；断言只有证据充分时才能恢复派发。

**验收：**所有“可能已切换但未确认”的场景均显示待处理，不宣称仍运行旧 release。

## 07. 实现 Docker 显式回滚

**目标：**`ops_release_rollback` 对保留制品完整的 Docker release 可以真正执行回滚。

- 在 [service.py](../src/drawbridge/service.py) 中把现有 simulation 分支与 Docker 分支
  分离。回滚预检查目标属于同一 app/environment、历史成功、服务集合相同、可信运行时
  Compose 与镜像 ID/摘要存在，且当前基线未改变；任一检查失败时不得调用 Compose。
- 使用任务 06A/06B 的阶段记录执行旧模板 `docker compose up --no-build --pull never --wait`，
  然后执行登记的健康检查。成功时创建**新的** release 记录，并填写
  `restored_from_release_id`、`replaces_release_id`；不得改写历史记录。
- 回滚若已开始改变现场又失败，尝试恢复回滚前的 release 并重新检查健康；恢复不确定
  时保持 `needs_attention`。在 [test_service.py](../tests/test_service.py) 覆盖预检查拒绝、
  成功、健康失败后恢复、恢复失败及重复幂等键。

**验收：**Docker 回滚不再固定返回 `ROLLBACK_PRECHECK_FAILED`；每个终态有真实现场证据。

## 08. 发布失败时自动恢复上一成功 release

**目标：**Docker 部署在运行时变更开始后失败时，尝试恢复冻结的上一成功 release。

- 复用任务 07 的回滚执行与验证原语，避免再写一套 Compose 参数。构建阶段或 Compose
  开始前失败时不得触发运行时恢复。没有历史基线时报告“无可恢复基线”。
- 自动恢复使用独立超时预算，并把恢复尝试、恢复结果、错误和最终现场写入原 job 的
  结构化结果及事件。原部署 job 仍是失败；仅恢复后的运行状态可以报告为已恢复。
- 在 [test_service.py](../tests/test_service.py) 覆盖 Compose 部分更新、健康失败、
  旧镜像缺失、恢复健康失败、Runner 在恢复期间中断。保留 `release_status` 的异步契约。

**验收：**自动恢复成功与失败可区分；没有证据时一律 `needs_attention`。

## 09A. 定义 Gateway 与 Runner 的内部权限契约

**目标：**在迁移调用前，固定两进程之间允许传递的操作、参数及权限边界。

- 先在 [当前架构](../.harness/context/ARCHITECTURE.md) 记录每个 MCP 工具目前触达的
  Git、Docker、文件和 SQLite 权限，再确定固定的内部请求契约。推荐将注册/计划所需
  Git 操作、`workspace_patch`/配置读写，以及 Docker 发现/日志，交给 Runner 端受限
  处理；只允许枚举操作和已验证参数，不引入通用命令或任意文件读取接口。
- 推荐使用只在本机开放的 Unix socket，请求/响应使用带长度上限的结构化消息，socket
  由 Runner 创建并通过文件权限限定 Gateway 用户访问；Linux 验收时核对对端身份。
  为每种操作单独设置超时和输出上限。不要把 socket 放在 Gateway 可替换目录中。
- 实现可替换的内部客户端/处理器接口及其参数测试；Runner 对 Gateway 传入的数据重新
  校验路径、binding、Git ref 与 Compose。内部通道须限制本机访问与文件权限；
  SQLite 中的排队 payload 也不能被视为可信命令。
- 在 [test_gateway.py](../tests/test_gateway.py) 和相关新测试中验证非法内部操作、
  越界参数、超时和过大响应被拒绝，允许请求得到与原工具一致的结构化结果。

**验收：**内部接口不接受命令字符串、任意路径、任意 Docker/Git 参数；接口测试通过。

## 09B. 迁移特权调用并收紧服务权限

**目标：**Gateway 用户不需要 Docker socket、Git 凭据或业务仓库写权限，现有工具仍可用。

- 修改 [gateway.py](../src/drawbridge/gateway.py)、[runner.py](../src/drawbridge/runner.py)
  和 [systemd 模板](../deploy/systemd/drawbridge-gateway.service)，将注册、计划、Git
  状态/日志、`workspace_patch`、配置读/校验、Docker 发现/日志按任务 09A 的接口
  迁移，并更新 [运维文档](OPERATIONS.md)。内部同步子进程调用不能阻塞 Runner 心跳；
  迁移期可用有界线程池，任务 10A 再统一改造 Git 执行器。
- 在 [test_gateway.py](../tests/test_gateway.py) 验证 Gateway 不可访问 Docker/Git 时，
  上述 MCP 工具仍按契约工作，错误与请求超时可解释。

**验收：**本地权限替身证明 Gateway 不直接调用 Git/Docker，也不写业务仓库；真实服务
用户和 systemd 权限在任务 11 验收。不得通过把 Gateway 加入 `docker` 组过关。

## 10A. 消除 Git 子进程阻塞

**目标：**慢 Git fetch 不阻塞同进程的其他请求或 Runner 队列循环。

- 处理 [gitops.py](../src/drawbridge/gitops.py) 在 async 服务方法内调用同步
  `subprocess.run()` 的路径。优先使用受控的异步进程执行器；过渡时可使用受限线程池，
  保留现有 argv、环境清理、超时和错误边界。
- 在 [test_gateway.py](../tests/test_gateway.py) 和 [test_service.py](../tests/test_service.py)
  加入慢 Git 替身，证明并发 `ops_catalog`、job 状态和队列心跳仍能及时执行。

**验收：**慢 Git 操作期间，同进程的独立请求与队列心跳继续运行。

## 10B. 降低 Docker 发现成本

**目标：**大量容器时，发现接口调用次数与延迟保持可控。

- 将 [compose.py](../src/drawbridge/compose.py) 的逐容器 `docker inspect` 改为有界的
  批量查询或异步并发；分页避免每次重复完整扫描。保持返回字段及截断限制。
- 在 [test_gateway.py](../tests/test_gateway.py) 用固定规模的替身容器数据比较调用次数
  和延迟，记录改动前后数字；不要以模拟数据推断真实服务器吞吐量。

**验收：**发现查询不再为每个容器串行启动一次 `docker inspect`，分页不会重复全量工作。

## 11. 真实服务器验收与文档收口

本任务必须在管理员指定的服务器、配置和无敏感数据的测试应用上执行；本地 simulation
不能替代。先按 [OPERATIONS.md](OPERATIONS.md) 检查 Linux 用户、两份配置、状态目录、
Docker/BuildKit socket 和项目根目录。`self-check` 只证明必要工具及 socket 存在，
还须核对 BuildKit daemon 确为 rootless，并按 [技术设计](TECHNICAL_DESIGN.md)检查
实际 systemd 权限。

1. 用预建镜像应用完整运行 `register → plan → apply → status → logs/HTTP`，核对容器
   标签、镜像 ID、健康状态、release 制品、SQLite 记录和服务用户权限。
2. 用获准的 BuildKit 测试应用重复上述路径，核对构建 archive SHA-256、Docker image
   load、按镜像 ID 部署和实际业务健康；构建失败时不得执行 Compose。
3. 在可恢复的测试应用上执行显式回滚、部署健康失败后的自动恢复，以及 Runner 中断
   后的现场核对；逐项保存任务结果、容器状态和阶段记录。不得用真实业务数据制造故障。
4. 检查 Gateway 不能打开 Docker socket 或写业务仓库；直接客户端访问仍受 CIDR、
   Host、Origin 和 token 限制。检查 HTTP 出站实际目标符合批准的 CIDR。
5. 追加日期、环境、命令、结果与证据路径到
   [VERIFICATION_RECORD.md](VERIFICATION_RECORD.md)，更新 [README](../README.md)、
   [当前架构](../.harness/context/ARCHITECTURE.md)和 [OPERATIONS.md](OPERATIONS.md)。
   未通过的项目保留为未完成，不写“生产就绪”。

## 每个任务的验证和交付格式

先运行任务中指定的聚焦测试，再在仓库根目录执行完整质量门：

```sh
uv sync --frozen --extra dev
.venv/bin/python scripts/verify.py
```

读取脚本打印的 `var/verification/<UTC 时间>/report.json`，确认退出码为 0 且
`result: passed`，并检查相邻的 `ruff.log`、`format.log`、`mypy.log`、`pytest.log`、
`compileall.log`、`self_check.log`、`simulation.log`。执行 `git diff --check`，核对
`git status --short` 中没有意外改动。把本次本地验证结果追加到
[VERIFICATION_RECORD.md](VERIFICATION_RECORD.md)，保留旧记录。

每个任务交付时记录：改动文件、行为变化、聚焦测试结果、完整报告路径、未覆盖的真实
Docker/BuildKit 环节、是否改变 MCP 契约。任务 01A–10B 在代码、测试、文档与本地
harness 完成后勾选；任务 11 只有在真实服务器全部验收并记录证据后才能勾选。
