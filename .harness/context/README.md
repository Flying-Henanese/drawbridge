# 项目背景与核心概念

Drawbridge 服务于“代码已修改，需要在远端 staging 服务器构建、部署并取得结果”的场景。
MCP 客户端通过固定工具提交目标应用和 Git ref，服务端校验并记录输入，再执行受控流程。
目标是让编程智能体能依据任务状态、发布制品、日志和健康检查继续工作。当前实现支持
Docker Compose 项目；本地示例配置使用 simulation。

优先阅读[当前系统架构](ARCHITECTURE.md)。它按源码说明 Gateway、Runner、SQLite、Git、
BuildKit 和 Docker 的实际关系，也列出与目标设计仍有差距的地方。调用 MCP 发布工具时
再读[系统发布路径](RELEASE_FLOW.md)。较长的需求与设计依据
在[技术设计](../../docs/TECHNICAL_DESIGN.md)和
[实施规格](../../docs/MVP_IMPLEMENTATION_SPEC.md)；服务器安装见
[运维文档](../../docs/OPERATIONS.md)。

## 理解请求所需的概念

| 概念 | 在当前实现中的含义 |
| --- | --- |
| 管理员配置 | YAML 中的路径、客户端网段、认证、HTTP 出站与构建 profile。MCP 请求不能修改这些能力边界。 |
| 项目根目录 | `allowed_project_roots` 列出的可注册项目范围；它不是 Linux 文件权限授权。 |
| 应用绑定（binding） | 一个 `app/staging` 与 Git 工作区、Compose 文件、服务集合和构建 profile 的关联。注册成功后才可对其计划发布或使用登记的配置 patch。 |
| Git ref / commit | 发布输入须解析成固定 SHA；`fetch` 可从登记 origin 获取允许的分支或标签，`local` 使用本地已有提交。 |
| 发布计划（plan） | 从固定 commit 建立临时源码快照，应用显式 revision，校验固定服务集合，并冻结 Compose、build、revision、binding、profile 和当前基线指纹；创建计划不会构建或部署。 |
| 任务（job） | `apply` 入队后由 Runner 异步执行的实例；必须用 `job_id` 查询最终状态。 |
| 发布（release） | 成功任务留下的源码快照、运行配置与结果记录；simulation 发布只提供流程证据。 |
| workspace revision | 对管理员事先登记的可编辑文件做受限修改后形成的版本，可叠加到指定 SHA 的发布快照。 |
| 构建 profile | 管理员登记的 BuildKit socket、平台和各服务的构建路径；Compose 声明必须与之匹配。 |

## 当前范围

系统实现了注册、计划、异步发布、观测和部分显式回滚能力。Docker 镜像构建在发布任务
内部完成，不存在独立的 MCP `docker build` 操作。Compose 的特权配置和任意设备挂载默认
会在注册时被拒绝；管理员可以用 runtime profile 精确登记能力，或批准一份旧 Compose 的
SHA-256。计划和执行都只读取固定 SHA 的快照，不读取分支后来指向的提交或未提交工作区。

t4 已验证 ContractLens 使用现有镜像的真实 Docker 发布路径；验证没有执行镜像构建。
该记录只说明当时的获准配置和环境，真实 BuildKit、失败回滚、中断恢复、进程权限与其他
业务项目仍须按[验证说明](../validation/README.md)和运维步骤单独确认。
