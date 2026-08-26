# aiops-apm-anomaly-detector

APM（应用性能监控）告警模块：从第三方 API 采集指标/日志，经确定性的 L0–L3 漏斗，产出 `problem_record` 落库，供下游诊断/修复使用。

> 当前状态：**M0 工程基座 + M1 契约层 + M2 持久化与迁移已完成**（`make lint test dev` 全绿，45 个用例通过）。设计与实现计划见 [`docs/`](docs/)，实现规则见 [`CLAUDE.md`](CLAUDE.md)，实现日志见 [`docs/logs/`](docs/logs/)，归档见 [`docs/archive/`](docs/archive/)。

## 实现进度

| 里程碑 | 内容 | 状态 | 实现日志 |
|--------|------|------|----------|
| M0 | 工程基座（pyproject/Makefile/Settings/异常/探针） | ✅ 已完成 | [`docs/logs/M0.md`](docs/logs/M0.md) |
| M1 | 契约层（模型 + fingerprint 真源） | ✅ 已完成 | [`docs/logs/M1.md`](docs/logs/M1.md) |
| M2 | 持久化与迁移（migrations + storage + config.loader） | ✅ 已完成 | [`docs/logs/M2.md`](docs/logs/M2.md) |
| M3 | 采集层与出站网关 | 未实现 | — |
| M4 | 插件化（registry + 内置 detector/suppressor） | 未实现 | — |
| M5 | 漏斗 L0–L3 + emit（确定性核心） | 未实现 | — |
| M6 | 调度、多租户、API、恢复闭环 | 未实现 | — |
| M7 | 可观测性、安全加固、交付 | 未实现 | — |

> 每完成一个里程碑：在 `docs/logs/<M阶段>.md` 记录实现日志，把已实现章节归档到 `docs/archive/`，并更新本表。

## 已实现（M0–M2）

- **M0 工程基座**：
  - 工程骨架：`pyproject.toml`（依赖 + 三个 entry_points 占位）、`Makefile`、`.env.example`、ruff/mypy/pytest/pre-commit
  - `src/aiops_apm/`：`settings.py`（`APM_` 前缀环境变量配置）、`exceptions.py`（`ErrorCode` + `AppException`）、`_app.py`（`create_app` + 统一异常响应 `{code, reason, trace_id}`）、`router/api.py`（`/health`、`/ready` 探针）
- **M1 契约层**（纯类型层，契约已冻结，后续禁止改签名只允许加可选字段）：
  - `src/aiops_apm/models/`：`signal.py`（Metric/Log/ChangeSignal + `Signal` 判别联合）、`anomaly.py`（Metric/LogAnomaly + `Anomaly`）、`record.py`（`Correlation`/`Verification`/`ProblemRecord` + `group_key`）、`config.py`（检测规则模型，M6 写入校验用）、`fingerprint.py`（`anomaly_key`/`group_key`/`is_same_group` 去重与 L3 持续性真源）
  - `src/aiops_apm/plugins/base.py`：`Plugin`/`Collector`/`Detector`/`Suppressor` 抽象基类 + `build()` 工厂（M3/M4 实现具体插件）
- **M2 持久化与迁移**（结果侧地基，M5 开单即可落库）：
  - `src/aiops_apm/migrations/`：`runner.py`（`MigrationRunner` 幂等迁移：schema_versions 追踪、按版本顺序执行）+ `V1__init_tables.sql`（单 schema `aiops_apm_runtime` 12 张表，problem_record 含 `severity`/`open_group_key` 生成列 + UNIQUE 原子去重）
  - `src/aiops_apm/storage/`：`connection.py`（`ConnectionPool` aiomysql）、`records.py`（`RecordStore` + InMemory/MySQL，`write_or_append` 同 `group_key` 去重追加）、`domain_config.py`（`DomainConfigStore` + InMemory/MySQL）、`__init__.py`（`Storage` 聚合 + `build_storage(settings)` 按 `storage_backend` 分派）
  - `src/aiops_apm/config/`：`loader.py`（`DomainConfigLoader`：DB 主源 → 空表 seed → last-known-good 回退）+ `domains.yaml`（application 域 seed）
  - `make migrate` 建库建表；storage 挂进 lifespan，`/ready` 真实反映 DB 连接状态

## 启动与快速上手

> 本节适用于所有里程碑（M0–M7 都这样启动与调用）。每完成一个里程碑会补充该阶段的启动附加步骤（如 M2 的 `make migrate` 建表、M6 的调度器开关 `APM_ENABLE_SCHEDULER`）与接口调用示例。

### 1. 配置环境变量

```bash
cp .env.example .env
cp .env.dev .env
# 编辑 .env，按需修改：
#   - APM_PORT：服务监听端口（默认 8000，.env.example 预置 7070）
#   - APM_DB_*：数据库连接（M2 起生效）
```

### 2. 安装依赖（首次）

```bash
cd <仓库根目录>
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
# 或直接 make install
```

### 2.5 M2 建库建表（可选，需可用 MySQL）

```bash
# 在 .env 配置 APM_DB_HOST / APM_DB_PORT / APM_DB_USER / APM_DB_PASSWORD / APM_DB_NAME
make migrate
# → 幂等建齐 aiops_apm_runtime 库的 12 张表（二次执行不报错、不重复建）
# 若未配置凭据/未连 MySQL，会报连接错误；不影响 memory backend 的 make dev
```

### 3. 启动服务

```bash
make dev
```

`make dev` 会读取 `.env` 中 `APM_PORT` 作为监听端口（未配置默认 8000）。启动成功应看到类似输出（端口以你 `.env` 的配置为准）：

```
INFO:     Started server process [PID]
INFO:     Uvicorn running on http://0.0.0.0:7070 (Press CTRL+C to quit)
```

停止服务：在运行终端按 `Ctrl+C`。

### 4. 调用接口

另开一个终端，用 curl 调用（端口以 `.env` 的 `APM_PORT` 为准，以下用 `<port>` 表示；也可直接用浏览器打开地址）：

```bash
# 存活探针：进程在即返回 200
curl -i http://127.0.0.1:<port>/health
# → HTTP/1.1 200 OK，body: {"status":"ok"}

# 就绪探针：M2 起 db 反映真实连接状态（插件 registry 属 M4）。
#   mysql backend：启动时连不上 DB → fail-fast，进程启动失败退出（不再降级启动）；
#   运行中 DB 掉线 → db:False；memory backend（demo/单测）db 恒 True。
curl -i http://127.0.0.1:<port>/ready
# → mysql 连上 / memory backend：db 为 True（plugins 仍 False → 503 直至 M4）
#   body: {"code":"NOT_READY","reason":"{'db': True, 'plugins': False}"}
# → 运行中 DB 掉线：HTTP/1.1 503 Service Unavailable
#   body: {"code":"NOT_READY","reason":"{'db': False, 'plugins': False}"}

# 统一异常响应：请求不存在的资源，返回 404 + {code, reason, trace_id}
curl -i http://127.0.0.1:<port>/nope
# → HTTP/1.1 404 Not Found
#   body: {"code":"NOT_FOUND","reason":"Not Found","trace_id":"..."}
```

> 后续里程碑（M3 起）会在这里补充 `/v1/*` 接口的调用示例（如 `POST /v1/monitors`、`GET /v1/problems` 等）。

### 5. 质量检查（提交前）

```bash
make lint       # ruff + mypy
make test       # pytest
```

### 配置说明

- 所有配置均可通过 `.env` 文件或环境变量覆盖，前缀 `APM_`（如 `APM_PORT`、`APM_HOST`、`APM_DB_HOST`）。
- 完整字段见 [`src/aiops_apm/settings.py`](src/aiops_apm/settings.py)；样例见 [`.env.example`](.env.example)。
