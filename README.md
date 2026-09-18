# aiops-apm-anomaly-detector

APM（应用性能监控）告警模块：从第三方 API 采集指标/日志，经确定性的 L0–L3 漏斗，产出 `problem_record` 落库，供下游诊断/修复使用。

> 当前状态：**M0 工程基座 + M1 契约层 + M2 持久化与迁移 + M3 采集层与出站网关 + M4 检测层（插件 registry + 内置 detector/suppressor）+ M5 漏斗 L0–L3 + emit（确定性核心）+ M6 调度/多租户/API/恢复闭环 + M7 可观测性/安全加固/交付 + M8 存储层 PostgreSQL 化 + M9 日志异常分组（按 signature/traceId，可跨服务合并）已完成**（`make lint test dev` 全绿，516 个常跑用例 + 26 条真库集成用例通过）。设计与实现计划见 [`docs/`](docs/)，实现规则见 [`CLAUDE.md`](CLAUDE.md)，实现日志见 [`docs/logs/`](docs/logs/)，归档见 [`docs/archive/`](docs/archive/)。

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
| M7 | 可观测性、安全加固、交付 | ✅ 已完成 | [`docs/logs/M7.md`](docs/logs/M7.md) |
| M8 | 存储层 PostgreSQL 化（MySQL/`aiomysql` → PG/`psycopg3` + 真库集成道） | ✅ 已完成 | [`docs/logs/M8.md`](docs/logs/M8.md) |
| M9 | 日志异常分组（按 signature/traceId 出单，可跨服务合并） | ✅ 已完成 | [`docs/logs/M9.md`](docs/logs/M9.md) |

> 每完成一个里程碑：在 `docs/logs/<M阶段>.md` 记录实现日志，把已实现章节归档到 `docs/archive/`，并更新本表。

## 已实现（M0–M9）

- **M0 工程基座**：
  - 工程骨架：`pyproject.toml`（依赖 + 三个 entry_points 占位）、`Makefile`、`.env.example`、ruff/mypy/pytest/pre-commit
  - `src/aiops_apm/`：`settings.py`（`APM_` 前缀环境变量配置）、`exceptions.py`（`ErrorCode` + `AppException`）、`_app.py`（`create_app` + 统一异常响应 `{code, reason, trace_id}`）、`router/api.py`（`/health`、`/ready` 探针）
- **M1 契约层**（纯类型层，契约已冻结，后续禁止改签名只允许加可选字段）：
  - `src/aiops_apm/models/`：`signal.py`（Metric/Log/ChangeSignal + `Signal` 判别联合）、`anomaly.py`（Metric/LogAnomaly + `Anomaly`）、`record.py`（`Correlation`/`Verification`/`ProblemRecord` + `group_key`）、`config.py`（检测规则模型，M6 写入校验用）、`fingerprint.py`（`anomaly_key`/`group_key`/`is_same_group` 去重与 L3 持续性真源）
  - `src/aiops_apm/plugins/base.py`：`Plugin`/`Collector`/`Detector`/`Suppressor` 抽象基类 + `build()` 工厂（M3/M4 实现具体插件）
- **M2 持久化与迁移**（结果侧地基，M5 开单即可落库）：
  - `src/aiops_apm/migrations/`：`runner.py`（`MigrationRunner` 幂等迁移：schema_versions 追踪、按版本顺序执行）+ `V1__init_tables.sql`（独立 schema `aiops_apm_runtime` 12 张表，problem_record 含 `severity`/`open_group_key` 生成列 + UNIQUE 原子去重）
  - `src/aiops_apm/storage/`：`connection.py`（`ConnectionPool` psycopg3，连接串固定 `search_path` + `TimeZone=UTC`）、`records.py`（`RecordStore` + InMemory/PG，`write_or_append` 同 `group_key` 去重追加）、`domain_config.py`（`DomainConfigStore` + InMemory/PG）、`__init__.py`（`Storage` 聚合 + `build_storage(settings)` 按 `storage_backend` 分派）
  - `src/aiops_apm/config/`：`loader.py`（`DomainConfigLoader`：DB 主源 → 空表 seed → last-known-good 回退）+ `domains.yaml`（application 域 seed）
  - `make migrate` 建 schema 建表；storage 挂进 lifespan，`/ready` 真实反映 DB 就绪状态（schema 缺失时 fail-fast，见 §2.5）
- **M3 采集层与出站网关**（数据供给上游）：
  - `src/aiops_apm/collectors/`：`_gateway.py`（`OutboundGateway` 出站安全网关：SSRF IP 字面量拦截 + scheme 白名单 + secret 引用校验/解析 `${env:X}`/`${vault:...}`）、`_http_client.py`（`SharedHttpClient` httpx 共享客户端：超时/连接池/禁跳转/响应体大小限制）、`_field_mapping.py`（`FieldMapper`：点路径 + `value[1]` 数组索引抽取、ISO/unix 时间戳解析）、`http_metrics.py`/`http_logs.py`/`mock.py`（内置采集器：水位线下推 `params["start"]` → 请求 → 映射 → 幂等去重 → 水位线推进 → 写 `signal_snapshot`；`http_logs` 对 ELK 源另支持 `time_field`/`service_field` 两个开关——设了就把时间窗与服务过滤放进 **POST body** 的 ES 查询 DSL，因为 ES 的日期 range 只认 body，写进 URL 参数会 400）、`__init__.py`（`collector_for` 按 signal_type+source_type 分派）
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
  - `src/aiops_apm/storage/lease.py` — `LeaseStore` ABC + InMemory + PG（`ON CONFLICT ... RETURNING` 原子接管 SQL）
  - `src/aiops_apm/summary.py` — `SummaryProvider` 钩子（模板默认，`enable_llm_summary` 开关，不接真实 LLM）
  - `src/aiops_apm/router/` — `alerts.py`（`POST /v1/alerts/run` 全量/域过滤）、`problems.py`（`/v1/problems` 查询 + resolve）、`config.py`（reload + 域配置读写）、`maintenance.py`（维护窗口 CRUD）、`blacklist.py`（黑名单 CRUD）；`monitors.py` 加 `POST /{id}/run` 手动单跑
  - §13 用例 2 端到端：related + high metric + high log → critical（`test_uc62_combo_critical.py`）；reconcile 自动关单、跨租户 403、多副本 lease 全部测试覆盖（原 225 不回归，新增 62 → 287）
- **M7 可观测性、安全加固、交付**（Prometheus 指标 + 轮次审计 + 安全审计日志 + 配置校验 + fpr 回写 + Docker/压测，原 287 不回归，新增 64 → 351）：
  - `src/aiops_apm/metrics.py` — Prometheus 7 类指标（round_total/success、records_created、degraded_sources、suppressed_total、false_positive_rate Gauge、round_duration Histogram）；`/metrics` 端点暴露；`poller.run_round` 每轮打点（`test_metrics.py`）
  - `src/aiops_apm/storage/rounds.py` + `migrations/V3__detection_round_domain.sql` — `RoundStore`（InMemory/PG）读写 `detection_round`，`poller` 每轮 create running → success/partial/failed
  - `src/aiops_apm/router/audit.py` — `GET /v1/audit/rounds`（domain/status/limit 过滤）+ `GET /v1/audit/suppressed`（从轮次 timeline details 摊平）
  - `src/aiops_apm/audit.py` — `SecurityAudit` 五类结构化审计日志（auth/gateway/plugin/config/round），`APM_AUDIT_ENABLED` 开关，不记明文凭据（key 只留 sha256 前缀、URI 只留 host:port）
  - `src/aiops_apm/collectors/_gateway.py` — SSRF **DNS 二次校验**（`_resolve_ips`，解析 IP 命中私网拒绝，`gaierror` fail-closed 拒绝，防 DNS rebinding）
  - `src/aiops_apm/config/validator.py` — `validate_domain_config` detector/suppressor 参数表驱动校验；`PUT /v1/config/{domain}` 非法 → 400 `CONFIG_ERROR`
  - `src/aiops_apm/storage/dynamic_config.py` `write_fpr` + `POST /v1/problems/{id}/resolve` 支持 `{"false_positive": true}` 误报回写 → `fpr_table` + FPR Gauge 重算
  - `docker/` — Dockerfile（多阶段 uvicorn）+ docker-compose（postgres + mock-source + apm-alert + prometheus）+ seed.py + custom_detector(p95_latency 第三方插件示例) + demo.py + locustfile.py + prometheus.yml；`Makefile` `docker-up`/`docker-down`/`loadtest`（本机无 docker/locust → 写出待补跑）
- **M8 存储层 PostgreSQL 化**（把 MySQL/`aiomysql` 整体换成 PostgreSQL/`psycopg3`，复用 `multi-agent-workflow` 已有的 PG 实例；踩坑与方言映射见 [`docs/logs/M8.md`](docs/logs/M8.md)，改存储层前务必先读）：
  - `src/aiops_apm/storage/connection.py` — `ConnectionPool` 重写为 psycopg3 异步池；`execute_lastid` → `execute_returning`（psycopg3 无 `cursor.lastrowid`）；`_as_json` 返回 `Jsonb`；`release()` 显式 rollback（PG 事务出错后进 aborted 态，不清理会级联失败）
  - `src/aiops_apm/migrations/V1..V8__*.sql` — 就地改写为 PG 方言（`IDENTITY` 取代 `AUTO_INCREMENT`、`JSONB`、`TIMESTAMP(3)`、`COMMENT ON`、独立 `CREATE INDEX IF NOT EXISTS`）；`runner.py` 的语句切分器新增 `$$` 美元引用支持（触发器函数体里的 `;` 与撇号会切碎语句）
  - 10 个 `MySQL*Store` 改名 `PG*Store`；`storage_backend` 由 `mysql` 改为 `pg`（`mysql` 已下线，传它会抛 `ValueError`）
  - **两个会话参数在连接串里钉死**：`search_path`（PG 没有 `USE`）与 `TimeZone=UTC`（时间列是 naive `TIMESTAMP(3)`，会话时区非 UTC 会让 DB 生成的时间与应用写入的 UTC 值差若干小时且不报错）
  - `tests/test_pg_integration.py` — **新增真库集成道**，`APM_TEST_PG_DSN` 门控（未设则 skip，`make test` 不需要 PG）。覆盖字符串断言抓不到的东西：fpr 整数除法、jsonb 路径、时区一致性、租约守卫、`search_path` 是否覆盖每条连接
  - `make test-pg APM_TEST_PG_DSN=postgresql://agentflow:agentflow@127.0.0.1:5432/agentflow` 跑真库；493 常跑 + 24 集成 = 517
- **M9 日志异常分组（按 signature/traceId 出单，可跨服务合并）**（把「一个 service 一条记录」换成「一个事故一条记录」；五个静默破坏点的对策见 [`docs/logs/M9.md`](docs/logs/M9.md)，改漏斗前务必先读）：
  - `src/aiops_apm/pipeline/grouping.py` — **新增**：并查集分组（纯函数）。同 `signature` 或 `trace_ids` 相交的日志异常归一组（**可跨服务**），metric 挂到本服务的日志组。`group_anomalies` 是**划分**（两两不交、并集==输入），结尾有断言
  - `src/aiops_apm/pipeline/runner.py` — `run_domain` 按**组**循环出单；`records_by_service` 改为 credit-all 且用 `+=`（同 service 可有多个组）
  - `src/aiops_apm/pipeline/l2_correlate.py` / `l3_verify.py` / `emit.py` — 入参从「一个 service」变成「一个组」；`_within_window` 显式限定**同 service** 配对（否则跨服务组会把不相干的 metric/log 误升 critical）
  - `src/aiops_apm/models/record.py` — 新增可选字段 `group_key_service`：对外 `service` 是拼接串（如 `"gateway-service,order-service"`），去重键只用代表服务，避免撑爆 `group_key`/`open_group_key`/唯一索引
  - `src/aiops_apm/detectors/signature_aggregate.py` — 顺带修掉既有缺陷：原分组键只有 `signature`，两个服务打出同一签名会塌成一条且 `service` 取到谁看运气，而 `anomaly_key` 把它焙进去重身份 → 持续性与自动关单在服务间漂移
  - `src/aiops_apm/migrations/V10__widen_problem_record_service.sql` — **新增**：`service` 加宽到 255（原 64 装不下多服务拼接，会 22001 整轮失败）
  - 配置：`application` 域 `verify.persistence_rounds` `2 → 1`（即时开单）

## 启动与快速上手

> 本节适用于所有里程碑（M0–M9 都这样启动与调用）。每完成一个里程碑会补充该阶段的启动附加步骤（如 M2 的 `make migrate` 建表、M6 的调度器开关 `APM_ENABLE_SCHEDULER`）与接口调用示例。

### 1. 配置环境变量

```bash
cp .env.example .env
cp .env.dev .env
# 编辑 .env，按需修改：
#   - APM_PORT：服务监听端口（默认 8000，.env.example 预置 7070）
#   - APM_DB_*：数据库连接（M2 起生效）
```

### 2. 安装依赖（首次）

> **前置：Python >= 3.10**（见 `pyproject.toml` 的 `requires-python`）。macOS 自带的 `python3` 是 3.9，
> 既低于该要求，其内置 pip（< 21.3）也不支持 PEP 660 可编辑安装，`pip install -e` 会误报
> `editable mode currently requires a setuptools-based build`——不要用它建 venv。

```bash
cd <仓库根目录>
make install          # 推荐：自动挑选 >=3.10 的解释器、建 .venv、升级 pip 后安装 -e ".[dev]"
# 需指定解释器时：make install PYTHON=/path/to/python3.12

# 等价的手动方式（注意解释器必须是 3.10+）：
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
```

### 2.5 初始化数据库（可选；用 `pg` backend 时**必做**）

初始化脚本就是 `src/aiops_apm/migrations/V1..V11__*.sql`（M8 起为 PostgreSQL 方言），由迁移执行器按版本号幂等应用，**不需要手工执行任何 SQL**。

```bash
# ① 先有一个可用的 PostgreSQL，且 APM_DB_NAME 指向的「库」已经存在。
#    复用 multi-agent-workflow 的实例最省事（它的 compose 会自动建 agentflow 库）：
#      cd ../multi-agent-workflow && docker-compose up -d postgres
#    若指向别处的全新 PG，需要先建库（PG 不能在事务里 CREATE DATABASE，所以这步不归迁移器管）：
#      createdb -h 127.0.0.1 -U agentflow agentflow

# ② 建 schema + 建表（幂等：重复执行不报错、不重复建）
make migrate
# → CREATE SCHEMA aiops_apm_runtime（若不存在）
# → 按版本号依次应用 V1..V11，版本记录写进 aiops_apm_runtime.schema_versions
# → 15 张表：V1 建 12 张；V2 collect_watermark；V5 detection_round_target；V3/V4/V6/V7/V8/V10 补列
# → V9 种入三个测试床日志监控端点（order/warranty/gateway-service），随迁移一并就位
# → V11 种入活库快照（域配置/在办单/轮次审计等，见 §2.6），~1MB
```

**连接参数**（`.env`，默认值已对齐 multi-agent-workflow 的 compose，通常无需改）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `APM_DB_HOST` / `APM_DB_PORT` | `127.0.0.1` / `5432` | |
| `APM_DB_USER` / `APM_DB_PASSWORD` | `agentflow` / 空 | |
| `APM_DB_NAME` | `agentflow` | **库**，必须已存在 |
| `APM_DB_SCHEMA` | `aiops_apm_runtime` | **schema**，由迁移器自建；与同库其它服务的表隔离 |

**跳过这步会怎样**：服务会**启动失败并明确告诉你**（fail-fast）：

```
RuntimeError: PostgreSQL 连上了，但在 search_path（'aiops_apm_runtime'）上看不到
aiops_apm_runtime.problem_record —— 多半是还没跑迁移。请先执行 `make migrate`。
```

> 注意 PG 与 MySQL 的一个关键差别：MySQL 时代「库不存在」会让连接本身失败，所以天然 fail-fast；
> PG 下库是共享的，**schema 缺失时连接照样成功**。因此 `build_storage` 会额外探一次核心表，
> 避免出现「服务起来了、`/ready` 报 ready、但每个查询都 500」——那在 k8s 下会把流量打到坏 Pod 上。

> 本模块的建表**不在** `multi-agent-workflow/docker/init/` 里——那个目录只管 agentflow 自己的表。
> APM 的表由本模块自己的迁移器创建；`docker/docker-compose.yml` 里 `apm-alert` 的启动命令
> 也是先跑 `python -m aiops_apm.migrations.runner` 再起 uvicorn。

### 2.6 数据库表说明（`APM_DB_NAME` 库内的独立 schema `aiops_apm_runtime`，共 15 张表）

> 所有业务表均带 `tenant_id` 列做多租户隔离。V1 建齐 12 张表；V2–V8/V10 增量补表/加列；
> `schema_versions` 由 `MigrationRunner` 自动创建，用于 `make migrate` 幂等版本追踪，不计入版本化迁移。
>
> **两个数据类迁移**（V1–V8 全是 DDL）：
> - **V9** —— 种入三个测试床日志端点（`ON CONFLICT DO NOTHING` 幂等）。
> - **V11** —— 把一份**活库快照**固化下来（域配置、在办问题单、轮次审计等），由
>   `docker/dump_seed_sql.py` 生成，**不要手改**。它是某一时刻的快照：活库后续变化
>   不会回流，要刷新走新迁移或重跑生成器。刻意**不种** `monitor_target`（V9 拥有）、
>   `scheduler_lease`（是锁不是数据，种了会让新环境 scheduler 抢不到锁）、
>   `schema_versions`（迁移器自管）；`signal_snapshot` 只种 20 行样本（该表只写不读，
>   且装原始生产日志正文，不宜整表进 git）。

| 表名 | 来源版本 | 用途说明 |
|------|----------|----------|
| `problem_record` | V1 +V11 | **M5 emit 最终产出**：异常告警单。含 `severity` 严重度、`state` 生命周期（pending/in_progress/resolved/closed/archived）、`open_group_key` 生成列 + `uk_open_group_key` UNIQUE 实现同 `group_key` 并发去重追加（resolved 后自动置 NULL 允许复发开新单）。`record_id` 形如 `PR-YYYYMMDD-NNNN` |
| `change_record` | V1 | 变更记录（deployment/ddl/config），L2 变更关联用：命中变更窗口内的异常标记 `change_related` |
| `domain_config` | V1 +V11 | 域检测规则（`config` JSON 存 detectors/suppressors/correlation/verify），`enabled` + `version` 版本号；`UNIQUE (tenant_id, domain)` |
| `monitor_target` | V1✅ | **监控端点配置**（回答「监控谁、从哪采、多快采」） ✅ |
| `maintenance_window` | V1 | L0 维护窗口：`(service, start_at, end_at)` 时间窗内的信号被抑制 |
| `suppress_blacklist` | V1 | L0 黑名单：按 `(domain, service, signal)` 匹配的信号被抑制（`signal` 在 PG 下用双引号标识符 `"signal"`） |
| `fpr_table` | V1 +V11 | 误报率统计（`group_key` 维度 `false_positive_cnt`/`total_cnt`/`fpr`），L3 误报率闸门 + `POST /resolve {"false_positive":true}` 误报回写落库 |
| `record_seq` | V1 +V11 | `record_id` 原子取号（按 `seq_date` 维护 `next_seq`，`PR-YYYYMMDD-NNNN` 每日自增） |
| `scheduler_lease` | V1 | 多副本选主：`scheduler_lease` 行锁 + `expires_at` TTL 续约 + 崩溃自动接管（PG 原子 `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`） |
| `signal_snapshot` | V1✅ +V11 样本 | 原始信号快照（metric/log 采集落库），`signature` 为日志堆栈签名（V7 由 VARCHAR(255) 加宽至 VARCHAR(1024)）。量大，建议按 `snapshot_ts` 分区/定期归档 |
| `detection_state` | V1 +V11 | 检测状态：`state_key`（如 `previous_keys`）存 `state_value` JSONB，L1 环比基线 / L3 持续性（consecutive/miss）计数 |
| `detection_round` | V1✅ +V11 | 轮次审计主表 ✅ |
| `collect_watermark` | V2 +V11 | **采集水位线**：每个 `monitor_target` 最近采集到的事件时间戳，`PRIMARY KEY (tenant_id, target_id)`，下轮下推 `start=last_ts` 实现增量采集 |
| `detection_round_target` | V5✅ +V11 | 轮次审计字表 - taget ✅ |
| `schema_versions` | 迁移自建 | 迁移版本追踪（`version` + `applied_at`），`make migrate` 据此幂等跳过已应用版本 |

**V9 种入的三个端点**（`monitor_target`，`log`/`elk`/`application`，60s 间隔）：`MT-0001` order-service、`MT-0002` warranty-service、`MT-0003` gateway-service。它们的日志经 filebeat 进 Elasticsearch（索引 `app-logs`）。ES 地址由 `APM_TESTBED_ES_URL` 经迁移器以 GUC 注入——**容器里 `localhost` 指向容器自己**，从 compose 跑要改成 `host.containers.internal:19200`（且 `kubectl port-forward` 默认只绑 `127.0.0.1`，需加 `--address 0.0.0.0`）。改这三个端点的配置**不要改 V9**（迁移不可变），用 `make seed-testbed` 或管理 API。**V11 不接管这三个端点**（`monitor_target` 本就是 V9 的地盘，V11 里重复种也只会输给 V9 的 `ON CONFLICT DO NOTHING`），所以上面这套改动方式在 V11 之后依然有效。

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
#   pg backend：启动时连不上 DB → fail-fast，进程启动失败退出（不再降级启动）；
#   运行中 DB 掉线 → db:False；memory backend（demo/单测）db 恒 True。
curl -i http://127.0.0.1:<port>/ready
# → 全就绪（memory backend 或 pg 连上 + registry 已加载）：HTTP/1.1 200 OK
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
# 每条都带派生的 detection_type：log / metric / combined（两者都有）/ unknown
# 前端据此做证据类型筛选（无需后端过滤参数）
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

#### M7 指标 / 审计 / 配置校验 / 误报回写

Prometheus 指标（`/metrics`）暴露 7 类指标，每轮检测自动打点（无需额外配置）；检测轮次写入 `detection_round`，可审计查询；config PUT 做 detector/suppressor 参数校验（非法 → 400 `CONFIG_ERROR`）；问题单 resolve 可回写误报：

```bash
# Prometheus 指标文本（round_total/success、records_created、degraded_sources、
#   suppressed_total、false_positive_rate、round_duration_seconds）
curl -i http://127.0.0.1:<port>/metrics

# 轮次审计：按 domain/status 过滤，started_at 倒序
curl -i "http://127.0.0.1:<port>/v1/audit/rounds?domain=application&status=success&limit=10"

# 被抑制信号摊平（从轮次 timeline details，可选 ?service= 过滤）
curl -i "http://127.0.0.1:<port>/v1/audit/suppressed?service=order-management"

# 配置写入侧校验：static_threshold 缺 threshold → 400 CONFIG_ERROR
curl -i -X PUT http://127.0.0.1:<port>/v1/config/application -H "Content-Type: application/json" \
  -d '{"detectors":[{"plugin":"static_threshold","params":{}}]}'

# 误报回写：手动关单并记为误报（写 fpr_table + 更新 FPR Gauge）
curl -i -X POST http://127.0.0.1:<port>/v1/problems/PR-20260826-0001/resolve \
  -H "Content-Type: application/json" -d '{"false_positive":true}'

# SSRF DNS 二次校验：hostname 解析到私网 → 400；解析失败（gaierror）→ fail-closed 400
curl -i -X POST http://127.0.0.1:<port>/v1/monitors -H "Content-Type: application/json" \
  -d '{"service":"x","signal_type":"metric","source_type":"prometheus",
       "source_config":{"url":"http://internal.example.com/metrics","field_mapping":{}}}'
```

安全审计日志输出到 `aiops_apm.audit` logger（Docker 由 stdout 收集），`APM_AUDIT_ENABLED=false` 可关。

Docker / 压测（本机无 docker/locust 时写出待补跑；环境可用后执行）：

```bash
make docker-up      # docker compose up：postgres + mock-source + apm-alert + prometheus
make docker-down    # docker compose down
make loadtest       # locust headless 压测（/v1/problems、POST /v1/alerts/run、/metrics、/v1/audit/rounds）
```

### 5. 质量检查（提交前）

```bash
make lint       # ruff + mypy
make test       # pytest
```

### 配置说明

- 所有配置均可通过 `.env` 文件或环境变量覆盖，前缀 `APM_`（如 `APM_PORT`、`APM_HOST`、`APM_DB_HOST`）。
- 完整字段见 [`src/aiops_apm/settings.py`](src/aiops_apm/settings.py)；样例见 [`.env.example`](.env.example)。
