# 开发守则

这些约束适用于所有代码和文档任务。实际步骤见[工作流程](../workflow/README.md)，
系统行为与已知差距见[当前架构](../context/ARCHITECTURE.md)。

## 以当前实现为准

- 修改前确认相关源码和测试的现有行为。长篇设计文档包含目标状态，不能把尚未实现的
  Docker 自动回滚、设备授权或服务器验收当成现有能力。
- 保留工作区已有改动。行为变化应补充或调整聚焦测试；文档变化应核对命令、链接和
  实现事实。报告验证结论时写明环境、证据和未覆盖范围。

## 保持能力边界

- MCP 不接受通用 shell、任意 Docker 命令、任意项目路径或任意 Git URL。新能力应通过
  固定工具参数和服务端校验表达。
- 注册必须经过路径、Git、Compose 与构建 profile 校验。失败的注册不形成可 patch 的
  应用绑定；不能借运行时补丁绕过注册校验。
- 发布计划必须从固定 Git SHA 的临时快照生成，并冻结显式 revision、服务集合、Compose、
  build 声明、binding 配置、完整 build profile 和基线。Runner 用同一套原语重建快照并
  逐项复验；旧 schema 或任何不一致都必须在外部副作用前以 `STALE_PLAN` 终止。不能读取
  分支的新指向、未提交工作区或隐式 `current_revision` 来替代计划输入。
- 构建通过管理员配置的 BuildKit socket 执行，Compose 更新使用受控 argv；不要回退到
  任意 `docker build`。真实服务器仍需检查 BuildKit daemon 的隔离配置。
- 目标是让 Gateway 不持有 Docker socket 或 Git 凭据。当前注册、计划、Docker 发现和
  日志实现尚未完全达到这一目标；修改相关路径时核对[实际权限流](../context/ARCHITECTURE.md)，
  不通过放宽 Gateway 权限掩盖架构问题。
- `config.example.yaml` 只适合本地 simulation。服务器项目根目录、状态目录、token、
  网段、BuildKit socket 和镜像源须按环境配置；实例化后的凭据不得提交到仓库。
- 不把 `self-check` 的成功解释为 Docker 或仓库权限验收。它不检查 Docker daemon 连接、
  目标镜像、登记仓库属主或实际 Git fetch；服务器上须按进程传 `--role gateway` 或
  `--role runner`，再按运维步骤以对应服务用户验证其余条件。

## 验证与记录

交付前按[验证说明](../validation/README.md)运行完整 harness 并读取 `report.json`。发布计划
相关改动还要覆盖计划阶段拒绝无效快照、执行阶段拒绝旧计划或指纹漂移，以及冻结 SHA 不随
分支前移的聚焦测试。
真实服务器、Docker、BuildKit、认证、HTTP 出站或进程权限相关改动还须遵循
[运维步骤](../../docs/OPERATIONS.md)。新增验证结果追加到
[历史记录](../../docs/VERIFICATION_RECORD.md)，保留旧证据。
