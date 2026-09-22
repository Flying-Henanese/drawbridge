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

## 真实服务器验收

本地报告不能证明 Gateway 的网络监听、客户端直连、systemd 用户权限、rootless
BuildKit、Docker image load、Compose 更新、设备访问或业务健康状况。相关变更需阅读
[OPERATIONS.md](../../docs/OPERATIONS.md)，在目标服务器以管理员批准的配置执行自检，
再对获准项目运行实际的 register → plan → apply → status，并检查构建日志、release
制品、容器状态和业务请求。

[VERIFICATION_RECORD.md](../../docs/VERIFICATION_RECORD.md) 保存以往本地和 t4 的结果，
是历史证据，不是当前服务器的就绪标志。报告本次结果时写明环境、时间、报告路径和
未覆盖的真实运行环节。
