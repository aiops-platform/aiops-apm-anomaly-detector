# aiops-apm-anomaly-detector

APM（应用性能监控）告警模块：从第三方 API 采集指标/日志，经确定性的 L0–L3 漏斗，产出 `problem_record` 落库，供下游诊断/修复使用。

> 当前状态：**M0 工程基座 + M1 契约层 + M2 持久化与迁移 + M3 采集层与出站网关已完成**（`make lint test dev` 全绿，113 个用例通过）。设计与实现计划见 [`docs/`](docs/)，实现规则见 [`CLAUDE.md`](CLAUDE.md)，实现日志见 [`docs/logs/`](docs/logs/)，归档见 [`docs/archive/`](docs/archive/)。

## 实现进度

| 里程碑 | 内容 | 状态 | 实现日志 |
|--------|------|------|----------|
| M0 | 工程基座（pyproject/Makefile/Settings/异常/探针） | ✅ 已完成 | [`docs/logs/M0.md`](docs/logs/M0.md) |
| M1 | 契约层（模型 + fingerprint 真源） | ✅ 已完成 | [`docs/logs/M1.md`](docs/logs/M1.md) |
| M2 | 持久化与迁移（migrations + storage + config.loader） | ✅ 已完成 | [`docs/logs/M2.md`](docs/logs/M2.md) |
| M3 | 采集层与出站网关（collectors + 安全网关 + 监控端点 API） | ✅ 已完成 | [`docs/logs/M3.md`](docs/logs/M3.md) |
| M4 | 插件化（registry + 内置 detector/suppressor） | 未实现 | — |
| M5 | 漏斗 L0–L3 + emit（确定性核心） | 未实现 | — |
| M6 | 调度、多租户、API、恢复闭环 | 未实现 | — |
| M7 | 可观测性、安全加固、交付 | 未实现 | — |

> 每完成一个里程碑：在 `docs/logs/<M阶段>.md` 记录实现日志，把已实现章节归档到 `docs/archive/`，并更新本表。

## 已实现（M0–M3）

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
- **M3 采集层与出站网关**（数据供给上游）：
  - `src/aiops_apm/collectors/`：`_gateway.py`（`OutboundGateway` 出站安全网关：SSRF IP 字面量拦截 + scheme 白名单 + secret 引用校验/解析 `${env:X}`/`${vault:...}`）、`_http_client.py`（`SharedHttpClient` httpx 共享客户端：超时/连接池/禁跳转/响应体大小限制）、`_field_mapping.py`（`FieldMapper`：点路径 + `value[1]` 数组索引抽取、ISO/unix 时间戳解析）、`http_metrics.py`/`http_logs.py`/`mock.py`（内置采集器：水位线下推 `params["start"]` → 请求 → 映射 → 幂等去重 → 水位线推进 → 写 `signal_snapshot`）、`__init__.py`（`collector_for` 按 signal_type+source_type 分派）
  - `src/aiops_apm/storage/`：`monitor_targets.py`（`MonitorTargetStore` CRUD + 软删 + `load_all_targets`）、`snapshots.py`（`SnapshotStore` 写 `signal_snapshot`）、`watermarks.py`（`WatermarkStore` 增量采集水位线）
  - `src/aiops_apm/signature.py`：`signature(log, n_frames=3)` 堆栈签名纯函数（L1 聚合共享）；`LogSignal` 增可选字段 `signature`
  - `src/aiops_apm/router/`：`deps.py`（`get_tenant_id` 从 `X-Tenant-Id` 头解析）、`monitors.py`（`/v1/monitors` CRUD + `POST /{id}/test` 连通性测试）
  - `src/aiops_apm/migrations/V2__collect_watermark.sql`：`collect_watermark` 表（`PRIMARY KEY (tenant_id, target_id)`）

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

### 2.5 M2/M3 建库建表（可选，需可用 MySQL）

```bash
# 在 .env 配置 APM_DB_HOST / APM_DB_PORT / APM_DB_USER / APM_DB_PASSWORD / APM_DB_NAME
make migrate
# → 幂等建齐 aiops_apm_runtime 库的 12 张表（V1）+ collect_watermark（V2，M3 增量采集水位线）
#   二次执行不报错、不重复建
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

#### M3 监控端点管理（`/v1/monitors`）

`tenant_id` 由请求头 `X-Tenant-Id` 解析（默认 `default`），服务端解析、绝不信任 body。创建/更新先过出站安全网关（SSRF 私网 IP 拦截 + secret 引用校验）：

```bash
# 新增 Prometheus 指标端点 → 201 {"target_id":"MT-0001"}
curl -i -X POST http://127.0.0.1:<port>/v1/monitors \
  -H "Content-Type: application/json" -H "X-Tenant-Id: default" \
  -d '{"service":"order-management","signal_type":"metric","source_type":"prometheus",
       "domain":"application",
       "source_config":{"url":"https://prometheus.example.com:9090/api/v1/query",
                        "params":{"query":"cpu_usage"},
                        "field_mapping":{"metric":"metric.__name__","value":"value[1]","timestamp":"value[0]"}},
       "schedule":{"interval_sec":60},"enabled":true}'

# SSRF：私网/云元数据地址 → 400 {"code":"VALIDATION_ERROR","reason":"blocked network: ..."}
curl -i -X POST http://127.0.0.1:<port>/v1/monitors -H "Content-Type: application/json" \
  -d '{"service":"x","signal_type":"metric","source_type":"prometheus",
       "source_config":{"url":"http://169.254.169.254/latest/meta-data/","field_mapping":{}}}'

# 列出 / 详情 / 更新 / 软删
curl -i "http://127.0.0.1:<port>/v1/monitors?service=order-management"
curl -i http://127.0.0.1:<port>/v1/monitors/MT-0001
curl -i -X PUT http://127.0.0.1:<port>/v1/monitors/MT-0001 -H "Content-Type: application/json" -d '{"service":"new-svc"}'
curl -i -X DELETE http://127.0.0.1:<port>/v1/monitors/MT-0001   # 204 软删

# 连通性测试（一次采集，不写库）：成功返回信号样本；上游失败返回结构化错误
curl -i -X POST http://127.0.0.1:<port>/v1/monitors/MT-0001/test
```

> 后续里程碑（M6 起）会在这里补充调度器手动触发 `POST /v1/monitors/{id}/run`、`GET /v1/problems` 等接口调用示例。

### 5. 质量检查（提交前）

```bash
make lint       # ruff + mypy
make test       # pytest
```

### 配置说明

- 所有配置均可通过 `.env` 文件或环境变量覆盖，前缀 `APM_`（如 `APM_PORT`、`APM_HOST`、`APM_DB_HOST`）。
- 完整字段见 [`src/aiops_apm/settings.py`](src/aiops_apm/settings.py)；样例见 [`.env.example`](.env.example)。
