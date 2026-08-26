# aiops-apm-anomaly-detector

APM（应用性能监控）告警模块：从第三方 API 采集指标/日志，经确定性的 L0–L3 漏斗，产出 `problem_record` 落库，供下游诊断/修复使用。

> 当前状态：**M0 工程基座 + M1 契约层 + M2 持久化与迁移 + M3 采集层与出站网关 + M4 检测层（插件 registry + 内置 detector/suppressor）+ M5 漏斗 L0–L3 + emit（确定性核心）+ M6 调度/多租户/API/恢复闭环已完成**（`make lint test dev` 全绿，287 个用例通过）。设计与实现计划见 [`docs/`](docs/)，实现规则见 [`CLAUDE.md`](CLAUDE.md)，实现日志见 [`docs/logs/`](docs/logs/)，归档见 [`docs/archive/`](docs/archive/)。

## 实现进度

| 里程碑 | 内容 | 状态 | 实现日志 |
|--------|------|------|----------|
| M0 | 工程基座（pyproject/Makefile/Settings/异常/探针） | ✅ 已完成 | [`docs/logs/M0.md`](docs/logs/M0.md) |
| M1 | 契约层（模型 + fingerprint 真源） | ✅ 已完成 | [`docs/logs/M1.md`](docs/logs/M1.md) |
| M2 | 持久化与迁移（migrations + storage + config.loader） | ✅ 已完成 | [`docs/logs/M2.md`](docs/logs/M2.md) |
| M3 | 采集层与出站网关（collectors + 安全网关 + 监控端点 API） | ✅ 已完成 | [`docs/logs/M3.md`](docs/logs/M3.md) |
| M4 | 检测层（registry + 内置 detector/suppressor） | ✅ 已完成 | [`docs/logs/M4.md`](docs/logs/M4.md) |
| M5 | 漏斗 L0–L3 + emit（确定性核心） | ✅ 已完成 | [`docs/logs/M5.md`](docs/logs/M5.md) |
| M6 | 调度、多租户、API、恢复闭环 | ✅ 已完成 | [`docs/logs/M6.md`](docs/logs/M6.md) |
| M7 | 可观测性、安全加固、交付 | 未实现 | — |

> 每完成一个里程碑：在 `docs/logs/<M阶段>.md` 记录实现日志，把已实现章节归档到 `docs/archive/`，并更新本表。

## 已实现（M0–M6）

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
- **M4 检测层**（可插拔插件系统，M5 漏斗通过 `ctx.registry.get(kind, name)` 消费）：
  - `src/aiops_apm/plugins/registry.py`：`PluginRegistry` — `load`/`reload`（遍历三个 `entry_points` group，`MappingProxyType` 原子快照替换，reload 期间跑一轮不抛异常）/`get`/`list`/`register`；单插件失败隔离
  - `src/aiops_apm/detectors/`：`static_threshold.py`（`Operator` GT/GTE/LT/LTE/RANGE，区间外命中）、`simple_compare.py`（`value > baseline * ratio`）、`signature_aggregate.py`（按堆栈签名分组，`count >= min_count` → 1 条 LogAnomaly，复用 `signature()`）
  - `src/aiops_apm/suppressors/`：`maintenance_window.py` / `blacklist.py`（从 `ctx.maintenance_windows` / `ctx.blacklist` 读数据，`check` + `batch_check`）
  - `src/aiops_apm/pipeline/filter_signals.py`：`filter_signals` 结构化 matcher（`*`/None/`""`→全量；str→metric 名/log level；dict→`signal_type` 分派 metric/labels/service 与 level/service）
  - `src/aiops_apm/router/plugins.py`：`GET /v1/plugins` 列表 + `POST /v1/plugins/reload` 重载（`asyncio.to_thread` 防阻塞）
  - lifespan 接线 registry → `app.state.registry`；`/ready` 的 `plugins` 由 M4 起为 True
- **M5 漏斗 L0–L3 + emit**（确定性核心，`run_domain` 一个 `(tenant_id, domain)` 内独立运行）：
  - `src/aiops_apm/pipeline/`：`context.py`（`DetectionContext` + `DomainResult` + `build_context`：载入 domain_config + 四类动态配置）、`l0_suppress.py`（维护窗口/黑名单批量抑制）、`l1_detect.py`（按 detector 规则分发，单 detector 异常隔离）、`l2_correlate.py`（按 service 同源关联 + 变更关联 + `template_summary` 模板兜底）、`l3_verify.py`（持续性 + 误报率闸门降级 + 严重度校准）、`emit.py`（组装 ProblemRecord + 原子去重落库）、`runner.py`（`run_domain` 串行编排 + timeline + miss sweep）
  - `src/aiops_apm/storage/`：`sequence.py`（`SequenceStore` PR-YYYYMMDD-NNNN 原子取号）、`detection_state.py`（`DetectionStateStore` consecutive/miss 计数）、`dynamic_config.py`（`DynamicConfigStore` 四类动态配置读取，按租户过滤）
  - §13 用例 1/3/4/5/6/7/8/9/10/11 端到端通过（`test_pipeline.py` 11 个 UC-5.x）；用例 2（组合升 critical）随 M6 落地
- **M6 调度、多租户、API、恢复闭环**（把漏斗接成自服务闭环）：
  - `src/aiops_apm/scheduler.py` — `Scheduler`：按 `monitor_target.schedule` 自动触发（多副本 lease 门控单调度器，tick 注入时钟可直测，`_next_run` 防启动风暴 + jitter）
  - `src/aiops_apm/poller.py` — `run_round`：按 `(tenant, domain)` 组并行 collect（单 target 异常 → `degraded_sources` 不崩溃）→ `run_domain`
  - `src/aiops_apm/reconcile.py` — `Reconciler`：周期性扫描 pending 单，全部 anomaly_key miss 达标 → `resolve(reason="auto")` 自动关单
  - `src/aiops_apm/auth/` — `AuthMiddleware` + `Principal`：**配置了才强制**（`APM_API_KEYS` 非空才挂），无 key→401、跨租户→403、master key admin；未配置 = 放行
  - `src/aiops_apm/storage/lease.py` — `LeaseStore` ABC + InMemory + MySQL（原子接管 SQL）
  - `src/aiops_apm/summary.py` — `SummaryProvider` 钩子（模板默认，`enable_llm_summary` 开关，不接真实 LLM）
  - `src/aiops_apm/router/` — `alerts.py`（`POST /v1/alerts/run` 全量/域过滤）、`problems.py`（`/v1/problems` 查询 + resolve）、`config.py`（reload + 域配置读写）、`maintenance.py`（维护窗口 CRUD）、`blacklist.py`（黑名单 CRUD）；`monitors.py` 加 `POST /{id}/run` 手动单跑
  - §13 用例 2 端到端：related + high metric + high log → critical（`test_uc62_combo_critical.py`）；reconcile 自动关单、跨租户 403、多副本 lease 全部测试覆盖（原 225 不回归，新增 62 → 287）

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

# 就绪探针：M2 起 db 反映真实连接状态，M4 起 plugins 反映插件 registry 加载状态。
#   mysql backend：启动时连不上 DB → fail-fast，进程启动失败退出（不再降级启动）；
#   运行中 DB 掉线 → db:False；memory backend（demo/单测）db 恒 True。
curl -i http://127.0.0.1:<port>/ready
# → 全就绪（memory backend 或 mysql 连上 + registry 已加载）：HTTP/1.1 200 OK
#   body: {"status":"ready","checks":{"db":true,"plugins":true}}
# → 运行中 DB 掉线：HTTP/1.1 503 Service Unavailable
#   body: {"code":"NOT_READY","reason":"{'db': False, 'plugins': True}"}

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

#### M4 插件管理（`/v1/plugins`）

registry 在启动时从 `entry_points` 自动发现内置/第三方插件（collector 3 / detector 3 / suppressor 2）；`POST /reload` 重新扫 entry_points 原子替换快照（正在执行的轮次继续用旧快照，不中断）：

```bash
# 查看已加载插件（按 collector/detector/suppressor 分组）
curl -i http://127.0.0.1:<port>/v1/plugins
# → HTTP/1.1 200 OK
#   body: {"collector":["http_logs","http_metrics","mock"],
#          "detector":["signature_aggregate","simple_compare","static_threshold"],
#          "suppressor":["blacklist","maintenance_window"]}

# 重新加载插件（新安装的第三方包无需重启即被发现）
curl -i -X POST http://127.0.0.1:<port>/v1/plugins/reload
# → 200，返回更新后的插件列表
```

#### M6 手动触发与问题查询

调度器开关 `APM_ENABLE_SCHEDULER`（默认 False，M6 起自动按 `monitor_target.schedule` 触发一轮检测）；配置 `APM_API_KEYS`（JSON `{"key":"tenant1,tenant2"}`，`"*"` 全租户）后启用鉴权——无 key→401、跨租户→403、master key 为 admin；未配置 = 放行：

```bash
# 手动单跑一个端点（一次采集 + 漏斗，返回 DomainResult：records/suppressed/anomaly/timeline）
curl -i -X POST http://127.0.0.1:<port>/v1/monitors/MT-0001/run

# 手动全跑（全部启用 target，?domain= 可选过滤；需 admin）
curl -i -X POST http://127.0.0.1:<port>/v1/alerts/run
curl -i -X POST "http://127.0.0.1:<port>/v1/alerts/run?domain=application"

# 问题单查询 / 详情 / 手动关闭
curl -i "http://127.0.0.1:<port>/v1/problems?state=pending&severity=high"
curl -i http://127.0.0.1:<port>/v1/problems/PR-20260826-0001
curl -i -X POST http://127.0.0.1:<port>/v1/problems/PR-20260826-0001/resolve   # reason=manual

# 配置热加载（reload 声明在 /config/{domain} 之前避免路径冲突；写配置需 admin）
curl -i -X POST http://127.0.0.1:<port>/v1/config/reload
curl -i http://127.0.0.1:<port>/v1/config/application
curl -i -X PUT http://127.0.0.1:<port>/v1/config/application -H "Content-Type: application/json" \
  -d '{"detectors":[],"verify":{"persistence_rounds":2}}'

# 维护窗口 / 黑名单 CRUD（自动关单：周期性扫描 pending 单，全部 anomaly_key miss 达标即 resolve(reason=auto)）
curl -i -X POST http://127.0.0.1:<port>/v1/maintenance-windows -H "Content-Type: application/json" \
  -d '{"service":"svc-a","start_at":"2026-08-26T11:00:00Z","end_at":"2026-08-26T12:00:00Z","reason":"release"}'
curl -i -X POST http://127.0.0.1:<port>/v1/blacklist -H "Content-Type: application/json" \
  -d '{"domain":"application","service":"svc-a","signal":"cpu_usage","reason":"known noisy"}'

# 鉴权示例（配置了 APM_API_KEYS 后）：scoped key 自带租户 / master key 任意租户
curl -i http://127.0.0.1:<port>/v1/monitors -H "Authorization: Bearer k1" -H "X-Tenant-Id: tenant-a"
curl -i http://127.0.0.1:<port>/v1/monitors -H "Authorization: Bearer k2"   # "*" 任意租户
```

### 5. 质量检查（提交前）

```bash
make lint       # ruff + mypy
make test       # pytest
```

### 配置说明

- 所有配置均可通过 `.env` 文件或环境变量覆盖，前缀 `APM_`（如 `APM_PORT`、`APM_HOST`、`APM_DB_HOST`）。
- 完整字段见 [`src/aiops_apm/settings.py`](src/aiops_apm/settings.py)；样例见 [`.env.example`](.env.example)。
