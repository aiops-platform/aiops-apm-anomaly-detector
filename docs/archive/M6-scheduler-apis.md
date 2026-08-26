# M6 调度、多租户、API、恢复闭环 — 历史规格归档

> 本文归档 `docs/apm-alert-implementation-plan-enhanced.md` 中 M6 小节（调度/多租户/API/恢复闭环）**已实现**的部分。实现日志见 [`docs/logs/M6.md`](../logs/M6.md)，实现计划见 [`docs/plans/M6-implementation-plan.md`](../plans/M6-implementation-plan.md)。

## 目标

完整可运行服务：scheduler 自动跑、API 自服务、多租户安全、异常消失自动关单。依赖 M1–M5 全部。关键修正 P0#5/#10/#9、P1#20 在此落地。完成标准：§13 用例 2 端到端通过；两并发轮次同 group_key 不重复开单；reconcile 自动关单；未授权 403；多副本只一个跑调度。

## 交付（UC-6.1 ~ 6.11）

| UC | 名称 | 断言 |
|----|------|------|
| UC-6.1 | 自动调度 | scheduler 按 target.schedule 触发，一轮 `(tenant, domain)` 内 collect→run_domain 全链路 |
| UC-6.2 | 手动单跑 | `POST /v1/monitors/{id}/run` 单 target 一次采集+漏斗，返回 DomainResult |
| UC-6.3 | 手动全跑 | `POST /v1/alerts/run` 全部启用 target（`?domain=` 可选过滤），返回汇总 |
| UC-6.4 | problems API | `GET /v1/problems`（`?state=&service=&severity=&limit=`）+ `GET /v1/problems/{id}` + `POST /v1/problems/{id}/resolve` |
| UC-6.5 | monitors CRUD | 已存在（M3），M6 加 `/{id}/run` |
| UC-6.6 | 配置热加载 | `POST /v1/config/reload` 重载 registry + domain_config 缓存；`GET/PUT /v1/config/{domain}` |
| UC-6.7 | reconcile 自动关闭 | 周期性扫描 pending 单，全部 anomaly_key `miss_rounds >= resolve_after_rounds` → `records.resolve(reason="auto")` |
| UC-6.8 | 多租户鉴权 | 配置了 api_keys 时：无 key→401；`X-Tenant-Id` 超限→403；plugins reload 需 admin |
| UC-6.9 | 多副本 lease | 两副本并发启动，仅一个能获取 `scheduler_lease`；失效可接管 |
| UC-6.10 | 维护窗口 CRUD | `POST/GET/PUT/DELETE /v1/maintenance-windows` |
| UC-6.11 | 黑名单 CRUD | `POST/GET/PUT/DELETE /v1/blacklist` |

**§13 用例 2（内存泄漏 + Full GC 组合升 critical）**：`calibrate_severity` 支持「组合升级」——related 且同 service 同时有 high 级 metric anomaly 与 high 级 log anomaly → critical。M5 只取最高，M6 补组合。

## 关键设计（已实现，偏离骨架处见日志「关键实现决策」）

### AuthMiddleware（P0#5 修正，配置了才强制）
- `Settings.api_keys`（`APM_API_KEYS` JSON：`{"key":"tenant1,tenant2"}`；`"*"` 表全租户）非空才挂中间件；未配置 = 放行（dev/既有测试零改动）。
- 解析 `Bearer <key>` → 查 api_keys → scope 解析（`"*"` 或列表）→ `X-Tenant-Id` 超限 → 403。`is_admin = ("*" in tenants)`（master key 隐式 admin）。
- `Principal(api_key, tenants, is_admin)`；`get_principal(request)` 无中间件时返回匿名 admin（dev 兼容）；`require_admin(principal)` 抛 `AppException(PERMISSION_DENIED)`。
- admin 路由：`POST /v1/plugins/reload`、`POST /v1/alerts/run`、`PUT /v1/config/{domain}`、`POST /v1/config/reload`。

### Scheduler（P0#10 修正，多副本单调度器）
- `Scheduler(settings, registry, storage, *, now_fn, jitter_fn, run_round_fn, holder_id)`：注入时钟/jitter/执行器 → `tick()` 可直测（不真实 sleep）。
- `tick()` 单步：lease 门（`storage.leases.try_acquire("scheduler", holder, ttl)` 失败 → 返回 0）→ `_find_due`（`list_tenants` + `load_all_targets`，`_next_run[(tenant,target_id)]` 首观测初始化为 `now + interval` 防风暴）→ 按 `(tenant, domain)` 分组、组内 in-flight 去重、`max_concurrent_rounds` 信号量 → 每组 `run_round` → 触发后重排 `now + interval + jitter` → renew lease。
- `run()`/`stop()`：后台循环 tick + `asyncio.wait_for(event.wait(), timeout=scheduler_tick_sec)`。`scheduler_tick_sec(1s) << lease ttl(30s)` 保证持约方稳定。
- lifespan（`_app.py`）：`enable_scheduler` 时 `asyncio.create_task(scheduler.run())`，shutdown cancel/await。

### SchedulerLease（UC-6.9）
- ABC：`try_acquire/renew/release`。InMemory：`_leases[name, {holder, expires_at}]`，注入 `now`，过期可被他人接管。
- MySQL 原子接管（单 handle）：`INSERT ... ON DUPLICATE KEY UPDATE holder = IF(expires_at < NOW(3), VALUES(holder), holder), expires_at = IF(expires_at < NOW(3), VALUES(expires_at), expires_at)` → `execute_affected()` 判接管。renew：`UPDATE ... WHERE lease_name=%s AND holder=%s AND expires_at > NOW(3)`，rowcount==1。
- `ConnectionPool` 加 `execute_lastid()`/`execute_affected()`（复用一个连接 handle，`cursor.rowcount`）。

### poller.run_round（collect 编排 + degraded_sources 产生）
- 每个 target 用窄 `CollectContext(tenant_id, watermark_store, snapshot_store)`（collector duck-type 只用这三字段）走 `collector_for` 采集；`asyncio.gather` 并行。
- 单 target 异常 → 记入 `degraded_sources` 返回 [] 不崩溃（M5 遗留「degraded_sources 产生」落地）；合并 signals → `build_context` → `run_domain`。

### Reconciler（P0#9 修正）
- `Reconciler(settings, storage, *, now_fn)`：`run()` 循环 `resolve_check_interval_sec`。
- `reconcile_once()`：`records.list_tenants()` → `records.list` 过滤 open states → 从 record JSON 重建 anomaly_keys（`MetricAnomaly`/`LogAnomaly` `.model_validate` + `fingerprint.anomaly_key`）→ `detection_state.list_by_domain` 取各 key `miss_rounds` → 全部 `>= resolve_after_rounds` → `records.resolve(reason="auto")`。

### L2 摘要钩子（LLM 预留）
- `SummaryProvider` Protocol + `TemplateSummaryProvider`（复用 `template_summary` 确定性模板）+ `build_summary_provider(settings)`（`enable_llm_summary` 开关，M6 不接真实 LLM）。`emit` 用 `ctx.summary_provider or TemplateSummaryProvider()`。

### §13 用例 2 组合升 critical
- `calibrate_severity(anomalies, *, related=False)`：取最高 severity；`related` 且 high metric + high log → `critical`。`l3_verify(..., related=corr.related)` 透传；`run_domain` 传 `corr.related`。

## 范围（不做，留后续）

- **M7**：可观测性（metrics 强化）、安全加固、交付打包。
- **真实 LLM 调用**：L2 摘要钩子已备，`enable_llm_summary` 默认 False。
- **MySQL 真库实测**：本机 MySQL 未运行（M3 遗留）；MySQL 实现照既有模式写出，InMemory 真源单测 + MySQL SQL 断言覆盖，真库验证待 DB 可用补跑。
- **前端**（M0 明确不做）。
