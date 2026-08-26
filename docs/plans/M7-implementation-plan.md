# M7 可观测性 / 安全加固 / 交付打包 — 实现计划

> 状态：**进行中**（2026-08-26）。前置 M0–M6 已完成（`make lint test dev` 全绿，287 用例，M6 提交 `b9bd0c1`）。实现日志见 `docs/logs/M7.md`，历史规格归档见 `docs/archive/M7-observability-security.md`。

## Context（为什么做）

- **前置**：M0–M6 已交付完整闭环。当前状态：`prometheus_client>=0.20` 已钉在 pyproject 依赖但**零使用**（无 `/metrics` 端点、无 Counter/Histogram/Gauge）；`detection_round` 表在 V1 DDL 中存在（`round_id=trace_id` PK、status running/success/partial/failed、timeline JSON）但**无 store 读写**（dormant）；无安全审计日志（401/403 只回状态码不留痕）；`_gateway` SSRF 只有 IP 字面量拦截（docstring 明确「DNS-resolution second check to M7」）；`fpr_table` 只读（`load_fpr`）无写回；config PUT 只做 `DomainConfig.model_validate` 基础校验（M6 遗留「detector params 写入侧校验」）；`CONFIG_ERROR` 走 500 兜底（应 400）；无 Docker/compose/示例/压测脚本。
- **M7 是什么**：可观测性（Prometheus metrics 强化 + 检测轮次审计 + 安全审计日志）、安全加固（SSRF DNS 二次校验 + detector params 校验 + fpr 回写 + CONFIG_ERROR→400）、交付打包（Docker/compose/examples/压测脚本，本机无法验证 → 写出待补跑）。
- **用户已确认的两个范围决策**：
  1. **范围 = 完整 M7**：metrics 强化 + 轮次审计 + 安全审计日志 + detector params 校验 + fpr 回写 + Docker/compose/examples + 压测脚本，全部落代码；本机无 docker/locust/k6、MySQL 3306 未运行 → 交付类项照既有模式「写出待补跑」（InMemory 真源单测 + 静态文件断言覆盖，真库/真容器验证待环境可用）。
  2. **真实 LLM L2 摘要 = 不接，保留钩子**：`enable_llm_summary` + `SummaryProvider` 钩子 + fake provider 单测已就位（M6），真实 LLM 调用留 backlog（遵守「确定性优先」，无 API key）。
- **蓝图**：`docs/apm-alert-implementation-plan-enhanced.md` M7 小节（UC-7.1 ~ 7.6）。实现以骨架为准，差异点已在「关键实现细节」标注。

## 范围

### 交付（对应 Enhanced plan UC-7.1 ~ 7.6 + M6 遗留项）

| UC | 名称 | 断言 |
|----|------|------|
| UC-7.1 | Prometheus 指标 | `/metrics` 端点暴露 round_total/round_success/records_created/degraded_sources/suppressed_total/false_positive_rate/round_duration；每轮 poller 打点 |
| UC-7.2 | 轮次审计 | `GET /v1/audit/rounds`（domain/status/limit 过滤）+ `GET /v1/audit/suppressed`（从 detection_round timeline 摊平） |
| UC-7.3 | 第三方插件验证 | config PUT 走 `validate_domain_config`（schema 表驱动：static_threshold/simple_compare/signature_aggregate params 校验），非法 → `ConfigValidationError` → 400 |
| UC-7.4 | Docker 一键演示 | Dockerfile + docker-compose（mysql + mock-source + apm-alert）+ seed.py + custom_detector(p95_latency) + demo.py + locustfile（写出待补跑） |
| UC-7.5 | 压测 | locustfile + Makefile 目标（本机无 locust → 静态断言文件存在） |
| UC-7.6 | 安全回归 | SSRF DNS 二次校验（hostname 解析 ∈ BLOCKED_NETWORKS → 拒绝）；安全审计日志（_gateway/auth/registry 接线）；CONFIG_ERROR→400；fpr 回写 |

**M6 遗留项落地**：detector params 写入侧校验（UC-7.3）、fpr 回写（UC-7.6）、CONFIG_ERROR→400（UC-7.6）。

### 不做（明确排除）
- **真实 LLM 调用**（L2 摘要钩子已备，`enable_llm_summary` 默认 False，留 backlog）。
- **真库/真容器实测**：本机 MySQL 3306 未运行、无 docker/locust/k6/mysql CLI；MySQL 新 store（rounds）照既有模式写出 + InMemory 真源单测 + MySQL SQL 断言，Docker/压测照既有模式写出待补跑。
- **前端**（M0 明确不做）。
- **metrics 强化外的可观测性**（trace/opentelemetry 等）——`trace_id` 已贯穿，无需引入新链路工具。

## 文件布局

```
src/aiops_apm/
├── metrics.py            # 新增：Prometheus 指标定义 + record_round_metrics(result, tenant_id)
├── audit.py              # 新增：SecurityAudit 四静态方法（auth/gateway/plugin/config 审计日志）
├── storage/
│   ├── rounds.py         # 新增：RoundStore ABC + InMemory + MySQL（detection_round 读写）
│   ├── dynamic_config.py # 改：加 write_fpr(tenant, group_key, *, false_positive)
│   └── __init__.py       # 改：Storage 加 rounds；build_storage 分派
├── config/
│   └── validator.py      # 新增：ConfigValidationError + validate_domain_config(cfg, registry)
├── collectors/
│   └── _gateway.py       # 改：SSRF DNS 二次校验（_resolve_ips）+ 审计日志接线（trace_id kwarg）
├── auth/
│   └── middleware.py     # 改：401/403 走 SecurityAudit.log_auth_event（不记明文 key）
├── plugins/
│   └── registry.py       # 改：load except 走 SecurityAudit.log_plugin_event
├── router/
│   ├── audit.py          # 新增：GET /v1/audit/rounds + GET /v1/audit/suppressed
│   ├── config.py         # 改：PUT 接 validate_domain_config
│   ├── problems.py       # 改：resolve 加 false_positive 参数 → write_fpr → Gauge 更新
│   └── api.py            # 改：include audit router
├── poller.py             # 改：RoundStore 接线（running → run_domain → success/partial/failed）+ record_round_metrics
├── pipeline/runner.py    # 改：suppressed timeline 步骤加 details（JSON 安全字符串摘要）
├── _app.py               # 改：/metrics 端点 + _status_for_code 加 CONFIG_ERROR→400
├── settings.py           # 改：加 audit_enabled/round_retention_rounds
├── main.py               # 改：暴露 app（供 uvicorn 用，含 lifespan）
├── docker/
│   ├── Dockerfile        # 新增：多阶段构建
│   ├── docker-compose.yml# 新增：mysql + mock-source + apm-alert + prometheus
│   ├── mock_source.py    # 新增：stdlib HTTP mock 源 :9100（写信号 JSON）
│   ├── seed.py           # 新增：build_storage 写 monitor_target + domain_config seed
│   ├── custom_detector/  # 新增：p95_latency pip 包（entry_points 示例）
│   ├── demo.py           # 新增：端到端演示脚本
│   └── locustfile.py     # 新增：压测脚本（/v1/problems /v1/alerts/run /metrics）
├── Makefile              # 改：docker-compose / locust 目标
tests/  （新增 8 个测试文件 + test_poller.py 扩展，见「测试」）
docs/plans/M7-implementation-plan.md       # 本文档落库（进行中）
docs/logs/M7.md                            # 实现日志
docs/archive/M7-observability-security.md  # 归档已实现章节
README.md / CLAUDE.md / MEMORY.md          # 进度同步
```

## 关键实现细节

### 0. 里程碑流程（先文档后代码）
- 落库本文档 `docs/plans/M7-implementation-plan.md`，`CLAUDE.md`「当前里程碑」标注「M7 进行中」。

### 1. `metrics.py` — Prometheus 指标（UC-7.1）
- 模块级定义（`prometheus_client`，已钉依赖）：
  - `ROUND_TOTAL = Counter("aiops_round_total", "检测轮次数", ["domain", "tenant_id", "status"])`
  - `ROUND_SUCCESS = Counter("aiops_round_success", "成功轮次", ["domain", "tenant_id"])`
  - `RECORDS_CREATED = Counter("aiops_records_created", "产出 problem_record 数", ["service", "severity"])`
  - `DEGRADED_SOURCES = Counter("aiops_degraded_sources", "降级源计数", ["tenant_id"])`
  - `SUPPRESSED_TOTAL = Counter("aiops_suppressed_total", "被抑制信号数", ["service", "suppressor"])`
  - `FALSE_POSITIVE_RATE = Gauge("aiops_false_positive_rate", "误报率", ["service"])`
  - `ROUND_DURATION = Histogram("aiops_round_duration_seconds", "单轮耗时", ["domain", "tenant_id"])`
- `record_round_metrics(result: DomainResult, tenant_id: str)`：`ROUND_TOTAL.labels(domain, tenant_id, result.status)`、`ROUND_SUCCESS`（status=="success"）、`RECORDS_CREATED.labels(svc, sev)` 按 `result.records` 计数、`DEGRADED_SOURCES.labels(tenant_id)` 按 `len(result.degraded_sources)`。duration Histogram 在 poller 侧计时。
- `/metrics` 端点（`_app.py`）：`Response(generate_latest(), media_type="text/plain; version=0.0.4; charset=utf-8")`。
- **caveat（写注释）**：`records_created` 按 `len(result.records)` 计数，忽略 `write_or_append` dedup（已存在记录不新增）→ 高估上限值，注释标注。
- **prometheus 全局注册表**：单测用相对增量断言（`before = ROUND_TOTAL.labels(...)._value.get()` → 跑轮 → after > before）。

### 2. `storage/rounds.py` — RoundStore（UC-7.2）
- ABC：`create_round(tenant, round_id, domain, status, started_at, timeline)`、`update_status(tenant, round_id, status, ended_at)`、`get_round(tenant, round_id)`、`list_rounds(tenant, *, domain=None, status=None, limit=50, offset=0)`。
- InMemory（真源）：`_rows: dict[(tenant, round_id), dict]`；每方法入口校验 tenant_id 非空。
- MySQL：照既有模式单 handle + `builtins.list[...]` 注解（mypy 类作用域 `list` 遮蔽）；`SELECT ... ORDER BY started_at DESC LIMIT %s OFFSET %s`。
- `Storage` 加 `rounds`；`build_storage` 分派（memory → InMemoryRoundStore，mysql → MySqlRoundStore）。

### 3. `poller.py` / `runner.py` — round 接线 + suppressed details
- `run_round`：进入时 `rounds.create_round(tenant, round_id=trace_id, domain, "running", now, timeline=[])` → `run_domain(ctx)`（内部 runner 推进 timeline）→ 收尾 `update_status(..., "success"|"partial"|"failed", ended_at)`；`degraded_sources` 非空 → `partial`；run_domain 抛异常 → `failed`（记 audit）+ `record_round_metrics` 仍打点。
- `runner.py` suppressed timeline 步骤改：`{"step": "suppressed", "count": N, "details": [{"signal": "metric:heap_usage", "suppressor": "maintenance_window", "reason": "<reason>"}, ...]}`（JSON 安全字符串摘要，截断至 ≤1000 字符）。**不新增 V3 迁移**（V1 无 suppressed_detail 表，审计从 detection_round timeline 摊平）。

### 4. `router/audit.py` — 轮次审计 API（UC-7.2）
- `GET /v1/audit/rounds`：`?domain=&status=&limit=` → `rounds.list_rounds(tenant, ...)` → 列表（round_id/domain/status/started_at/ended_at/timeline）。
- `GET /v1/audit/suppressed`：`rounds.list_rounds(tenant, limit=max)` → 遍历 timeline `details` → 摊平为 `[{round_id, time, signal, suppressor, reason}]`（service 过滤可选）。
- 权限：普通鉴权（非 admin）——审计是运维查询；`get_tenant_id` 租户隔离。

### 5. `config/validator.py` — detector params 校验（UC-7.3）
- `ConfigValidationError(AppException)`（ErrorCode 新增 `CONFIG_ERROR`，`_app._status_for_code` 加 `CONFIG_ERROR→400`）。
- `validate_domain_config(cfg: DomainConfig, registry: PluginRegistry) -> None`，schema 表驱动：
  - 对每个 `detector` 条目：按 detector 类型校验（enum 表驱动，未知 detector 且 registry 无法解析则跳过）：
    - `static_threshold`：`threshold` 必须存在且为数值。
    - `simple_compare`：`baseline` 或 `ratio` 至少一个。
    - `signature_aggregate`：`min_count`/`n_frames` 正整数。
  - `suppressor` 条目：`maintenance_window` 校验 `duration_minutes` 为正、`blacklist` 校验 `pattern` 非空。
  - 非法 → 抛 `ConfigValidationError`（中文 detail + 字段名）。
- `router/config.py` `PUT /v1/config/{domain}`：`DomainConfig.model_validate` → `validate_domain_config(cfg, registry)` → upsert。

### 6. `storage/dynamic_config.py` — fpr 回写（UC-7.6）
- `write_fpr(tenant, group_key, *, false_positive: bool)`：InMemory `_fpr[tenant, group_key] = {"false_positive": bool, "updated_at": now}`；MySQL `INSERT ... ON DUPLICATE KEY UPDATE false_positive=VALUES(false_positive), updated_at=NOW(3)`（照 fpr_table 既有列）。
- `router/problems.py` `POST /v1/problems/{id}/resolve` 加 `false_positive: bool = False` body 参数：
  - 取 record → 重建 group_key（复用 `reconcile.anomaly_keys_from_record(record)`）→ `dynamic_config.write_fpr(tenant, group_key, false_positive=...)` → `FALSE_POSITIVE_RATE` Gauge 更新（用 load_fpr 重算）→ 原有 `resolve(reason="manual")`。
- `reconcile.py` 小改：抽 `anomaly_keys_from_record(record) -> list[str]` 公共函数供 problems 复用。

### 7. `collectors/_gateway.py` — SSRF DNS 二次校验（UC-7.6）
- 模块级 `_resolve_ips(host: str) -> list[str]`：`socket.getaddrinfo(host, None)` 解析（含 IPv6 `::1`），返回 IP 列表。
- `validate_url(url)`：现有 IP 字面量拦截保留；hostname 情形 → `_resolve_ips(host)` → 任一解析 IP ∈ `BLOCKED_NETWORKS`（含 `127.0.0.1`/`::1`）→ 拒绝（`SSRF_BLOCKED`）。解析失败（`socket.gaierror`）→ **拒绝**（fail-closed，防 DNS rebinding 首查放行）。
- 测试：monkeypatch `_gateway._resolve_ips` 返回 `["127.0.0.1"]` 断言拒绝、返回公网 IP 断言放行；docstring「DNS-resolution second check」落地。
- `validate_url`/`validate_headers` 加可选 `trace_id: str | None = None` kwarg → 审计接线（见下）。

### 8. `audit.py` — 安全审计日志（UC-7.6）
- `SecurityAudit` 四静态方法（`logging.getLogger("aiops_apm.audit")`，结构化 key=value）：
  - `log_auth_event(tenant, action, outcome, detail=None)` — outcome=allow/deny，detail 不记明文 key（只记 key 前缀 hash）。
  - `log_gateway_event(uri, blocked, reason)` — uri 仅 host:port（不记 query/secret）。
  - `log_plugin_event(plugin_name, action, outcome, detail=None)` — 如 `load:failed:ImportError(...)`。
  - `log_config_event(domain, action, outcome, detail=None)` — PUT/reload。
- 接线：
  - `auth/middleware.py`：401/403 分支调 `log_auth_event`；放行走 allow。
  - `_gateway`：validate_url/validate_headers 拒绝分支调 `log_gateway_event`。
  - `plugins/registry.py` `load()` except 块（现有 logger.warning 处）加 `log_plugin_event`。
  - `router/config.py` PUT/reload 加 `log_config_event`。
- 不落库（日志即审计），`audit_enabled: bool = True` 开关（`APM_AUDIT_ENABLED`）。

### 9. `_app.py` / `settings.py`
- `_status_for_code` 加 `CONFIG_ERROR → 400`（`ErrorCode.CONFIG_ERROR` 已在 exceptions.py）。
- `/metrics` 端点：`@app.get("/metrics", include_in_schema=False)` → `generate_latest()`。
- `settings.py`：加 `audit_enabled: bool = True`、`round_retention_rounds: int = 1000`（list_rounds 默认 limit 上限）。

### 10. Docker / compose / examples（UC-7.4/7.5，写出待补跑）
- `docker/Dockerfile`：多阶段（builder 装依赖 → runtime 瘦身），`CMD ["uvicorn", "aiops_apm.main:app", ...]`。
- `docker/docker-compose.yml`：services = mysql（8.x + init 跑 `make migrate`/entrypoint 调 `run_migrations`）、mock-source（:9100 stdlib HTTP）、apm-alert（依赖 mysql 健康检查 + migrate）、prometheus（抓 `/metrics`）。环境变量 `APM_DB_*`/`APM_STORAGE_BACKEND=mysql`。
- `docker/mock_source.py`：stdlib `http.server`，`GET /metrics` 返回单调递增 CPU/内存样例 JSON（模拟第三方源），`GET /logs` 返回日志样例。
- `docker/seed.py`：`build_storage` 写 2 个 monitor_target（http_metrics + http_logs，domain=demo）→ 幂等。
- `docker/custom_detector/`：`p95_latency` 包（`pyproject.toml` entry_points `aiops_apm.detectors` → `build()`），演示可插拔规则。
- `docker/demo.py`：起 app → 手动 run → 查 problems/audit/metrics，打印端到端验证摘要。
- `docker/locustfile.py`：`HttpUser` 打 `/v1/problems`、`POST /v1/alerts/run`、`/metrics`（读 GET + 写 POST 混合）。
- `Makefile`：`docker-up`/`docker-down`（docker compose）/`loadtest`（locust）。

## 测试（TDD，先写测试再实现）

| 测试文件 | 覆盖 |
|---------|------|
| `test_metrics.py` | 指标定义存在；record_round_metrics 增量计数（相对断言）；/metrics 端点 200 + `aiops_round_total` 文本 |
| `test_rounds.py` | RoundStore InMemory create/update_status/get/list 过滤 + 排序 + tenant 隔离；MySQL SQL 断言（INSERT/UPDATE/SELECT 生成） |
| `test_audit.py` | SecurityAudit 四方法发日志（caplog 断言 level + 结构化字段）；auth deny 不记明文 key |
| `test_audit_api.py` | GET /v1/audit/rounds（domain/status/limit 过滤）+ GET /v1/audit/suppressed 摊平（timeline details） |
| `test_config_validator.py` | validate_domain_config：static_threshold 缺 threshold→ConfigValidationError；simple_compare 空 params→错误；signature_aggregate min_count=0→错误；合法配置通过；错误码 400 映射 |
| `test_ssrf_dns.py` | _resolve_ips monkeypatch：hostname 解析 127.0.0.1→拒绝；公网 IP→放行；gaierror→fail-closed 拒绝；IP 字面量既有拦截不回归 |
| `test_fpr_writeback.py` | problems resolve false_positive=True → dynamic_config.write_fpr 落库 + Gauge 更新；reconcile.anomaly_keys_from_record 复用正确 |
| `test_deliverables.py` | Dockerfile/compose/mock_source/seed/custom_detector/demo/locustfile 文件存在 + 关键断言（compose 有 mysql+apm-alert、Dockerfile 有 uvicorn、entry_points 有 aiops_apm.detectors） |
| `test_poller.py`（扩展） | run_round 写 detection_round（running→success/partial/failed）+ record_round_metrics 打点 |

> `tests/conftest.py`：`Storage` fixture 加 `rounds`（InMemory）；prometheus 指标测试用相对增量避免全局注册表污染。

## 验证（完成标准）

1. `make lint` — ruff + mypy 全绿（新代码含 `# type: ignore[import-untyped]` 处理 aiomysql/prometheus 处）。
2. `make test` — 全量 pytest 绿（原 287 + M7 新增，不回归）。
3. 完成标准复核（InMemory 真源可验部分）：
   - UC-7.1：/metrics 暴露全部 7 类指标；跑一轮后 round_total/records_created 相对增量。
   - UC-7.2：rounds API 过滤正确；suppressed 从 timeline details 摊平。
   - UC-7.3：config PUT 非法 detector params → 400（ConfigValidationError）。
   - UC-7.6：SSRF DNS 二次校验拒绝/放行分支；审计日志接线；CONFIG_ERROR→400；resolve false_positive 写回 fpr + Gauge。
   - M6 不回归：287 全过。
4. 待环境可用补跑：MySQL 真库（rounds/fpr 写回）、docker compose up 端到端、locust 压测。

## 文档同步（CLAUDE.md 流程）

1. 本文档落库 `docs/plans/M7-implementation-plan.md`，状态「进行中」。
2. 完成后写 `docs/logs/M7.md` 实现日志（改动点、文件清单、完成状态、遗留问题）。
3. 归档已实现章节到 `docs/archive/M7-observability-security.md`；设计文档摘除 M7 已实现部分。
4. 更新 `README.md` 进度表（M7 → 已完成）+ 已实现模块补 metrics/audit/rounds/validator/Docker。
5. 更新 `CLAUDE.md`「当前里程碑」为「M7 已完成，下一阶段 M8 待定义」。
6. 更新 `MEMORY.md` 关键事实与续接点。
7. 提交 M7（`[huhao] feat: ...`，无 Co-Authored-By）。
