# M7 可观测性、安全加固、交付 — 历史规格归档

> 本文归档 `docs/apm-alert-implementation-plan-enhanced.md` 中 M7 小节（可观测性/安全加固/交付）**已实现**的部分。实现日志见 [`docs/logs/M7.md`](../logs/M7.md)，实现计划见 [`docs/plans/M7-implementation-plan.md`](../plans/M7-implementation-plan.md)。

## 目标

可上线、可排查、可扩展（真第三方插件）。依赖 M6（完整运行），引用 M1 真源。关键修正 P1#15/#16 在此收口。完成标准：Prometheus 指标可被 scrape；检测轮次可审计；安全回归（SSRF DNS 二次校验 / 审计日志 / 配置校验 / fpr 回写）通过。

## 交付（UC-7.1 ~ 7.6）

| UC | 名称 | 断言 |
|----|------|------|
| UC-7.1 | Prometheus 指标 | `/metrics` 暴露 round_total / round_success / records_created / degraded_sources / suppressed_total / false_positive_rate / round_duration；每轮 poller 打点，值随轮次递增 |
| UC-7.2 | 检测轮次审计 | `GET /v1/audit/rounds`（domain/status/limit 过滤）+ `GET /v1/audit/suppressed`（从 detection_round timeline 摊平） |
| UC-7.3 | 第三方插件验证 | config PUT 走 `validate_domain_config`（schema 表驱动：static_threshold/simple_compare/signature_aggregate/maintenance_window/blacklist 参数校验），非法 → `ConfigValidationError` → 400 |
| UC-7.4 | Docker 一键演示 | Dockerfile + docker-compose（mysql + mock-source + apm-alert + prometheus）+ seed.py + custom_detector(p95_latency) + demo.py |
| UC-7.5 | 压测 | locustfile + Makefile 目标（本机无 locust → 写出待补跑） |
| UC-7.6 | 安全回归 | SSRF DNS 二次校验（hostname 解析 ∈ BLOCKED_NETWORKS → 拒绝，gaierror → fail-closed 拒绝）；安全审计日志（_gateway/auth/registry 接线）；CONFIG_ERROR→400；fpr 回写 |

## 关键设计（已实现，偏离骨架处见日志「关键实现决策」）

### Prometheus 指标（UC-7.1）
- `metrics.py` 模块级定义（prometheus_client 全局注册表）：`ROUND_TOTAL`（按 domain/tenant_id/status）、`ROUND_SUCCESS`、`RECORDS_CREATED`（按 service/severity）、`DEGRADED_SOURCES`（按 tenant_id）、`SUPPRESSED_TOTAL`（按 service/suppressor，从 timeline suppressed details 摊平）、`FALSE_POSITIVE_RATE` Gauge（按 service，fpr_table 求均值）、`ROUND_DURATION` Histogram（自定义桶 0.01~60s）。
- `record_round_metrics(domain, tenant_id, status, duration_sec, result=None)`：轮次计数/耗时/产出/降级/抑制打点。`update_fpr_gauge` 按 group_key 前缀 `{tenant}:{domain}:{service}:` 求均值。
- `/metrics` 端点（`_app.py`，`include_in_schema=False`）→ `generate_latest()`。
- **caveat**：`records_created` 按 `len(result.records)` 计数忽略 `write_or_append` 去重 → 高估上限值，仅趋势观察。

### RoundStore 轮次审计（UC-7.2）
- `storage/rounds.py`：ABC + InMemory（真源）+ MySQL。`create_round`（running，round_id=trace_id）→ `update_status`（success/partial/failed，收尾计数 + timeline）→ `get_round`/`list_rounds`（domain/status 过滤、started_at 倒序、limit/offset）。每方法校验 `tenant_id` 非空；timeline 里 datetime 用 `_json_safe` 转 isoformat 保证 JSON 列可序列化。
- `V3__detection_round_domain.sql`：`detection_round` 补 `domain` 列（V1 建表未含，审计按 domain 过滤需要）。
- `poller.run_round` 每轮写 round；`run_domain` 异常 → failed + 审计 + re-raise；`degraded` 非空 → partial。
- `runner.py` suppressed timeline 步骤加 `details`（signal/service/suppressor/reason，reason 截断 200）。
- `router/audit.py`：`/v1/audit/rounds` + `/v1/audit/suppressed`（从 `list_rounds(limit=round_retention_rounds)` timeline 摊平，service 过滤）。普通鉴权（非 admin），租户隔离。

### Config 写入侧校验（UC-7.3）
- `config/validator.py`：`ConfigValidationError(AppException)`（`CONFIG_ERROR`，HTTP 400）+ `validate_domain_config(cfg, registry)` 表驱动：
  - detector：static_threshold（`threshold` 数值必填）、simple_compare（`baseline` 或 `ratio` 至少一个，ratio 正）、signature_aggregate（min_count/n_frames 正整数）。
  - suppressor：maintenance_window（duration_minutes 正）、blacklist（pattern 非空）。
  - 插件名不存在 → 400；registry 可解析但无内置 schema → 跳过结构校验。
- `router/config.py` `PUT /v1/config/{domain}`：`model_validate` → `validate_domain_config` → upsert。

### 安全审计日志（UC-7.6）
- `audit.py` `SecurityAudit` 五静态方法（auth/gateway/plugin/config/round）→ `logging.getLogger("aiops_apm.audit")` 结构化 key=value。`set_audit_enabled`（`APM_AUDIT_ENABLED`）。**不记明文凭据**：API key 只留 sha256 前缀、URI 只留 host:port（去 query/secret）。
- 接线：auth middleware 401/403/allow；_gateway 各拒绝分支；registry load 成功/失败；config reload/put；poller failed/partial。

### SSRF DNS 二次校验（UC-7.6）
- `_resolve_ips(host)`（`socket.getaddrinfo` A/AAAA 去重）；`validate_url` hostname 分支解析后任一 IP 命中 `BLOCKED_NETWORKS`（含 `::1`）→ 拒绝；`socket.gaierror` → 拒绝（**fail-closed**，防 DNS rebinding 首查放行）。IP 字面量分支在 DNS 前执行。

### fpr 回写（UC-7.6）
- `dynamic_config.write_fpr(tenant, group_key, *, false_positive)`：InMemory 递增重算；MySQL 单语句原子 `INSERT ... ON DUPLICATE KEY UPDATE`。
- `router/problems.py` `resolve` 加可选 body `{"false_positive": true}` → write_fpr + Gauge 重算 → resolve(manual)。record dict 自带 group_key，无需重建。

### 交付打包（UC-7.4/7.5，写出待补跑）
- `docker/Dockerfile`（多阶段，uvicorn `aiops_apm.main:app`）、`Dockerfile.mock-source`、`docker-compose.yml`（mysql + mock-source + apm-alert + prometheus，健康检查串行 migrate → seed → uvicorn）、`mock_source.py`（stdlib HTTP :9100 单调递增样例）、`seed.py`（2 个 monitor_target + domain_config upsert，幂等）、`custom_detector/p95_latency`（entry_points `aiops_apm.detectors`）、`demo.py`、`locustfile.py`、`prometheus.yml`。
- `Makefile`：`docker-up` / `docker-down` / `loadtest`。

## 范围（不做，留后续）

- **前端**：DashboardPage / RoundAuditPage / SuppressedAuditPage / SecurityAuditPage / LoadTestReportPage / ConfigHistoryPage —— M0 明确不做前端，M7 后端 API 已备（metrics/audit/security 均落代码）。
- **真实 LLM L2 摘要**：`enable_llm_summary` 钩子已备（M6），不接真实 LLM（用户确认）。
- **真库/真容器实测**：MySQL 真库（rounds/fpr 写回）、docker compose up 端到端、locust 压测 —— 本机无 docker/locust、MySQL 未运行，待环境可用补跑。
- **vault 密钥管理**：`${vault:path#key}` 为占位返回空串。
