# 验证方式与证据边界

## 本地完整验证

在仓库根目录运行（验证脚本要求 Python 3.12+）：

```sh
uv sync --frozen --extra dev
.venv/bin/python scripts/verify.py
```

`scripts/verify.py` 会打印忽略目录 `var/verification/<UTC 时间>/report.json` 的位置；
也可以用 `--output /path/to/new-directory` 指定尚不存在的证据目录。退出码为 0 且报告
中的 `result` 为 `passed` 才表示本次 harness 完成。失败时看 `error` 和同目录相应的
`*.log`；日志包含命令、退出码、标准输出和标准错误。

## 质量门

| 步骤 | 实际执行内容 | 证据 |
| --- | --- | --- |
| 运行时元数据 | `requires-python`、Ruff 和 mypy 均以 Python 3.12 为基线 | `report.json` |
| Ruff | `ruff check src tests scripts` | `ruff.log` |
| 格式 | `ruff format --check src tests scripts` | `format.log` |
| mypy | 严格检查 `src/drawbridge` | `mypy.log` |
| pytest | 完整测试套件，包括 Gateway 匿名 `401` 与已认证 `ops_catalog` | `pytest.log` |
| 编译 | 编译 `src` Python 源码 | `compileall.log` |
| self-check | 使用隔离的 simulation 配置检查阻断条件 | `self_check.log`、`report.json` |
| simulation | 临时 Git 仓库的 register → plan → apply → run job → status，核对 SHA、健康状态、发布物和 SQLite 记录 | `simulation.log`、`report.json` |

脚本从 `config.example.yaml` 生成临时配置，在系统临时目录建立只含最小 Compose 文件的
Git 仓库；其镜像地址为不可用的 `example.invalid`。状态、日志、发布物、模板和数据均
指向临时目录。运行不会拉取镜像、构建镜像、启动容器或更改现有部署。临时输入结束时
清理，报告和日志保留在本地证据目录。

隔离 self-check 使用兼容的 `--role all`，只证明 simulation 所需的阻断项。服务器上应分别
使用 `--role gateway` 和 `--role runner`，避免要求 Gateway 访问 Runner 专属 BuildKit socket。
即使输出中能找到 `docker` 和
Docker Compose，也不表示当前用户能连接 Docker daemon、目标镜像已经存在或真实 Compose
可以启动；它也不检查登记仓库的属主和读写权限。BuildKit 模式下的 self-check 只确认
`buildctl` 和登记的 Unix socket 存在，不证明 daemon 确实以 rootless 模式运行。

## 聚焦验证

完整 harness 是交付门槛，但不会替代与改动直接相关的边界测试。发布计划和冻结快照相关
改动至少应在 `tests/test_service.py` 覆盖以下路径：

- 计划阶段从固定 SHA 的临时快照校验 Compose，并在无效快照时不保存 plan。
- Runner 拒绝旧 schema、binding/profile/基线变化和任一快照指纹漂移，且不触发外部副作用。
- 分支在计划后前移、工作区文件变化或 `current_revision` 改变时，执行仍只使用计划保存的
  commit 和显式 revision。
- 固定服务集合发生新增、删除或改名时，计划或执行明确失败。

SQLite 或 Gateway 生命周期相关改动至少应覆盖：并发入队的幂等和容量边界、两个数据库实例
竞争认领、失败事务不影响已成功请求、binding 版本递增、release 与 job 终态原子提交，以及
并发 MCP 工具调用只在 Gateway 启动时初始化一次 schema。

## 真实服务器验收

本地报告不能证明 Gateway 的网络监听、客户端直连、systemd 用户权限、Git 仓库访问、
Docker daemon 权限、目标镜像可用性、rootless BuildKit、Docker image load、Compose
更新、设备访问或业务健康状况。相关变更需阅读 [OPERATIONS.md](../../docs/OPERATIONS.md)，
在目标服务器分别以 Gateway 和 Runner 用户执行对应的只读权限检查和自检，再对获准项目
运行实际的 register → plan → apply → status，并检查构建日志、release 制品、容器状态和
业务请求。

任务 02 已在 t4 对 ContractLens 做过一次额外验收：计划冻结提交后推进分支并临时破坏
工作区 Compose，Runner 仍从冻结 SHA 发布；该项目按可信配置复用已有镜像，发布命令使用
`--no-build --pull never`，最终三个业务容器健康且 HTTP 检查成功。这项历史证据没有运行
BuildKit，也不覆盖自动回滚、Runner 中断恢复、严格进程权限或其他项目。

[VERIFICATION_RECORD.md](../../docs/VERIFICATION_RECORD.md) 保存以往本地和 t4 的结果，
是历史证据，不是当前服务器的就绪标志。报告本次结果时写明环境、时间、报告路径和
未覆盖的真实运行环节。
