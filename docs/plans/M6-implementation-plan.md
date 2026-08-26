# M6 调度 / 多租户 / API / 恢复闭环 — 实现计划

> 状态：**已完成**（2026-08-26）。前置 M0–M5 已完成（`make lint test dev` 全绿，225 用例，M5 提交 `6d96598`）；M6 落地后 287 用例全绿。实现日志见 `docs/logs/M6.md`，历史规格归档见 `docs/archive/M6-scheduler-apis.md`。

## Context（为什么做）

- **前置**：M0–M5 已交付确定性漏斗核心。已就位：`run_domain(ctx)`（M5，消费 `ctx.signals`/`ctx.degraded_sources`）、`build_context`（M5，载入 domain_config + 四类动态配置）、collectors（M3，`collector_for` 分派，collector 异常向上抛）、`MonitorTargetStore`（M3）、`DetectionStateStore`/`SequenceStore`/`DynamicConfigStore`（M5）、`RecordStore.write_or_append` 原子去重（M2）、`PluginRegistry`（M4）。
- **M6 是什么**：把「确定性漏斗」接成**闭环**——scheduler 自动调度 + poller 编排采集/漏斗 + 手动触发 API + `/v1/problems` 查询 + 配置热加载 + reconcile 自动关闭 + 多租户鉴权 + 多副本 lease。M5 遗留的用例 2（组合升 critical）与 `degraded_sources` 产生也在 M6 落地。
- **完成标准**（Enhanced plan M6）：§13 用例 2 端到端通过；并发两轮同 `group_key` 不重复开单；reconcile 自动关闭 pending 单；跨租户 403；多副本单调度器（lease）。用例 1/3/4/5/6/7/8/9/10/11 已在 M5 通过，M6 不回归。
- **用户已确认的两个范围决策**：
  1. **鉴权 = 配置了才强制**：`Settings.api_keys`（`APM_API_KEYS`）非空才挂 `AuthMiddleware`；未配置 = 放行（既有 API 测试零改动）。配置后无 key→401、跨租户→403。
  2. **LLM L2 摘要 = 模板 + 可插拔钩子**：默认保持 `template_summary` 确定性模板；新增 `SummaryProvider` Protocol 钩子 + `enable_llm_summary` 开关；不接真实 LLM，测试用 fake provider。
- **蓝图**：`docs/apm-alert-implementation-plan-enhanced.md` M6 小节（`router/` 骨架 + Scheduler + Reconciler + AuthMiddleware + SchedulerLease）。实现以骨架为准，差异点已在「关键实现细节」标注。

## 范围

### 交付（对应 Enhanced plan UC-6.1 ~ 6.11 + §13 用例 2）

| UC | 名称 | 断言 |
|----|------|------|
| UC-6.1 | 自动调度 | scheduler 按 target.schedule 触发，一轮 `(tenant, domain)` 内 collect→run_domain 全链路 |
| UC-6.2 | 手动单跑 | `POST /v1/monitors/{id}/run` 单 target 一次采集+漏斗，返回 results |
| UC-6.3 | 手动全跑 | `POST /v1/alerts/run` 全部启用 target（`?domain=` 可选过滤），返回汇总 |
| UC-6.4 | problems API | `GET /v1/problems`（`?state=&service=&severity=&limit=`）+ `GET /v1/problems/{id}` + `POST /v1/problems/{id}/resolve` |
| UC-6.5 | monitors CRUD | 已存在（M3），M6 只加 `/{id}/run` |
| UC-6.6 | 配置热加载 | `POST /v1/config/reload` 重载 registry + domain_config 缓存；`GET/PUT /v1/config/{domain}` |
| UC-6.7 | reconcile 自动关闭 | 周期性扫描 pending 单，全部 anomaly_key `miss_rounds >= resolve_after_rounds` → `records.resolve(reason="auto")` |
| UC-6.8 | 多租户鉴权 | 配置了 api_keys 时：无 key→401；`X-Tenant-Id` 超限→403；plugins reload 需 admin |
| UC-6.9 | 多副本 lease | 两副本并发启动，仅一个能获取 `scheduler_lease`；失效可接管 |
| UC-6.10 | 维护窗口 CRUD | `POST/GET/PUT/DELETE /v1/maintenance-windows` |
| UC-6.11 | 黑名单 CRUD | `POST/GET/PUT/DELETE /v1/blacklist` |

**§13 用例 2（内存泄漏 + Full GC 组合升 critical）**：`calibrate_severity` 支持「组合升级」——related 且同 service 同时有 high 级 metric anomaly 与 high 级 log anomaly → critical。M5 只取最高，M6 补组合。

### 不做（明确排除）
- **M7**：可观测性（metrics 强化）、安全加固、交付打包。
- **真实 LLM 调用**（L2 摘要钩子已备，`enable_llm_summary` 默认 False）。
- **MySQL 真库实测**：本机 MySQL 未运行（M3/M5 遗留）；MySQL 实现照既有模式写出，InMemory 真源单测覆盖，真库验证待 DB 可用补跑。
- **前端**（M0 明确不做）。

## 文件布局

```
src/aiops_apm/
├── scheduler.py          # 新增：Scheduler 类，tick() 可直测；run()/stop() 供 lifespan
├── poller.py             # 新增：run_round(registry, storage, now) — collect(gather) → build_context → run_domain；degraded_sources 产生
├── reconcile.py          # 新增：Reconciler 周期性扫描 + 自动 resolve；record anomaly_key 重建
├── summary.py            # 新增：SummaryProvider Protocol + TemplateSummaryProvider + build_summary_provider
├── auth/
│   ├── __init__.py       # 新增：Principal + get_principal(request)（无中间件时匿名 admin）
│   └── middleware.py     # 新增：AuthMiddleware（api_keys 非空才挂）
├── storage/
│   ├── lease.py          # 新增：LeaseStore ABC + InMemory + MySQL（原子接管）
│   ├── connection.py     # 改：ConnectionPool 加 execute_lastid()/execute_affected()
│   ├── records.py        # 改：RecordStore 加 get(id) + list severity 过滤
│   ├── detection_state.py# 改：加 list_by_domain（reconcile 用）
│   ├── dynamic_config.py # 改：加维护窗口/黑名单 写方法（create/list/update/delete）
│   ├── monitor_targets.py# 改：加 list_tenants()
│   └── __init__.py       # 改：Storage 加 leases；build_storage 分派
├── pipeline/
│   ├── context.py        # 改：DetectionContext 加可选 watermark_store/snapshot_store/summary_provider
│   ├── l3_verify.py      # 改：calibrate_severity(*, related) 组合升 critical；l3_verify 透传 related
│   ├── runner.py         # 改：run_domain 把 corr.related 传给 l3_verify
│   └── emit.py           # 改：用 ctx.summary_provider 生成 symptom.summary
└── router/
    ├── api.py            # 改：include 新路由
    ├── deps.py           # 改：get_principal（透传 auth）
    ├── monitors.py       # 改：加 POST /{id}/run
    ├── alerts.py         # 新增：POST /v1/alerts/run
    ├── problems.py       # 新增：GET /v1/problems + /{id} + /{id}/resolve
    ├── config.py         # 新增：POST /v1/config/reload + GET/PUT /v1/config/{domain}
    ├── maintenance.py    # 新增：/v1/maintenance-windows CRUD
    └── blacklist.py      # 新增：/v1/blacklist CRUD
tests/  （新增 14 个测试文件，见「测试」）
docs/plans/M6-implementation-plan.md    # 本文档（进行中）
docs/logs/M6.md                          # 实现日志
docs/archive/M6-*.md                     # 归档已实现章节
README.md / CLAUDE.md / MEMORY.md         # 进度同步
```

**数据流（poller 单轮）**：

```
tick()/run 触发 → _find_due(list_tenants + load_all_targets) → 按 (tenant, domain) 分组
   → in-flight 去重 + 信号量 + lease 门
   → poller.run_round():
        targets = group[tenant, domain]
        collected = asyncio.gather(*(collector.collect(ctx, t) for t in targets), return_exceptions=True)
        degraded_sources = [t.target_id for 异常结果]         ← M5 遗留「degraded_sources 产生」在此落地
        ctx = build_context(..., signals=合并, degraded_sources=degraded_sources)
        result = await run_domain(ctx)
```

## 关键实现细节

### 1. `settings.py` 新增字段
- `api_keys: dict[str, str] = {}` — JSON env `APM_API_KEYS`（`{"key":"tenant1,tenant2"}`；`"*"` 表全租户）。
- `resolve_after_rounds: int = 3`、`resolve_check_interval_sec: float = 30.0`（reconcile 周期）。
- `scheduler_lease_ttl_sec: float = 30.0`、`scheduler_jitter_ratio: float = 0.1`。

### 2. `pipeline/context.py` — DetectionContext 加可选字段（契约冻结，只加不改）
- `watermark_store: WatermarkStore | None = None`、`snapshot_store: SnapshotStore | None = None`、`summary_provider: object | None = None`。
- `build_context` 中 `watermark_store=storage.watermarks`、`snapshot_store=storage.snapshots`。

### 3. `summary.py` — L2 摘要钩子（模板默认 + 可插拔）
- `SummaryProvider` Protocol：`summarize(*, service, metric_anoms, log_anoms) -> str`。
- `TemplateSummaryProvider`：复用 `pipeline/l2_correlate.template_summary`。
- `build_summary_provider(settings)`：`enable_llm_summary` 为 False 时返回 `TemplateSummaryProvider`（预留接 LLM 点位）。
- `emit.py`：`provider = ctx.summary_provider or TemplateSummaryProvider()` → 替代直接调用 `template_summary`。

### 4. `pipeline/l3_verify.py` — 组合升 critical（§13 用例 2）
- `calibrate_severity(anomalies, *, related=False)`：`related` 且同 service 有 high metric + high log → critical；否则取最高。
- `l3_verify(ctx, service, anomalies, *, related=False)`：透传 related。
- `runner.py`：`l3_verify(ctx, service, anoms, related=corr.related)`。
- fpr 闸门命中仍「降级 warning 不丢弃」（M5 已定，不改）。

### 5. `storage/lease.py` — LeaseStore（多副本单调度器，UC-6.9）
- ABC：`try_acquire(lease_name, holder, ttl_sec) -> bool`、`renew(...) -> bool`、`release(...) -> None`。
- InMemory（真源）：`_leases: dict[name, {holder, expires_at}]`；注入 `now`；过期可被他人接管。
- MySQL：acquire 用 `INSERT ... ON DUPLICATE KEY UPDATE holder=IF(expires_at < NOW(3), VALUES(holder), holder), expires_at=IF(expires_at < NOW(3), VALUES(expires_at), expires_at)` → `execute_affected()` 判断是否接管；renew 用 `UPDATE ... WHERE holder=? AND expires_at > NOW(3)` → rowcount==1。
- `ConnectionPool` 加 `execute_lastid()`/`execute_affected()`。

### 6. `scheduler.py` — Scheduler（UC-6.1/6.9）
- 构造注入 `now_fn`/`jitter_fn`/`run_round`，`tick()` 单步可直测（不真实 sleep）。
- `tick()`：lease 门（`storage.leases.try_acquire("scheduler", holder, ttl)` 失败跳过）→ `_find_due`（`list_tenants` + `load_all_targets`，按 `last_run_at + interval` 判 due）→ 按 `(tenant, domain)` 分组 + in-flight 去重 + 信号量 → 每组 `await run_round(...)` → renew lease。
- `run()`/`stop()`：lifespan `enable_scheduler` 时 `asyncio.create_task(scheduler.run())`，shutdown 时 stop + await。
- **`/ready` 不加 scheduler key**（`test_health` 断言 `checks == {"db","plugins"}` 保持不变）。

### 7. `poller.py` — run_round（collect 编排 + degraded_sources）
```python
async def run_round(*, registry, storage, tenant_id, domain, targets, now, http=None, settings=None) -> DomainResult:
    ctx = DetectionContext(tenant_id=tenant_id, domain=domain, now=now, registry=registry, storage=storage, ...)
    async def _one(target) -> list:
        try:
            collector = collector_for(target, http=http, settings=settings)
            return await collector.collect(ctx, target)
        except Exception:
            ctx.degraded_sources.append(target["target_id"])
            return []
    results = await asyncio.gather(*(_one(t) for t in targets), return_exceptions=True)
    ctx.signals = [s for r in results for s in r]
    return await run_domain(ctx)
```
- 用 `build_context` 载入 domain_config + 动态配置后，把 `signals`/`degraded_sources` 注入。

### 8. `reconcile.py` — 自动关闭（UC-6.7）
- `Reconciler(settings, storage, *, now_fn)`；`run()` 循环 `resolve_check_interval_sec`。
- `reconcile_once()`：`records.list(state in open)` → 按 `(tenant, domain)` 分组 → 每单重建 anomaly_key（`MetricAnomaly.model_validate`/`LogAnomaly.model_validate` 从 JSON dict）→ `detection_state.list_by_domain` 取 `miss_rounds` → 全部 `>= resolve_after_rounds` → `records.resolve(reason="auto")`。
- `DetectionStateStore` 加 `list_by_domain(tenant, domain) -> dict[state_key, state]`。

### 9. `router/` — API
- `deps.py`：`get_principal(request) -> Principal`（无中间件 → 匿名 admin，dev 兼容）。
- `monitors.py`：加 `POST /v1/monitors/{id}/run`（复用 run_round，targets=[t]）。
- `alerts.py`：`POST /v1/alerts/run`（`?domain=` 过滤，admin 校验）。
- `problems.py`：list（state/service/severity/limit）/ get by id / resolve（reason="manual"）。
- `config.py`：`POST /v1/config/reload` **声明在 `GET/PUT /v1/config/{domain}` 之前**；`GET` 读 domain_config；`PUT` 校验 `DomainConfig` 后更新（admin）。
- `maintenance.py`/`blacklist.py`：CRUD 落 `DynamicConfigStore` 新写方法。
- `api.py`：include 5 个新 router。
- `_app.py`：`if settings.api_keys: app.add_middleware(AuthMiddleware, ...)`；lifespan 启动 scheduler；plugins reload / config PUT / alerts run 需 admin。

### 10. `storage/` 小改
- `RecordStore.get(tenant, record_id) -> dict | None`；`list` 加 `severity` 过滤。
- `MonitorTargetStore.list_tenants() -> list[str]`（去重启用 target 的 tenant）。
- `DynamicConfigStore` 扩读写：维护窗口与黑名单 `create/list/update/delete`（InMemory 为真源，MySQL 照表 SQL）。

## 测试（TDD，先写测试再实现）

| 测试文件 | 覆盖 |
|---------|------|
| `test_summary.py` | TemplateSummaryProvider 兜底；fake provider 注入 emit |
| `test_l3_related.py` | calibrate_severity related 组合升 critical / 非 related 取最高 / 无 high 不升 |
| `test_poller.py` | run_round 正常采集合并；单 target 异常 → degraded_sources 含 target_id、不崩溃 |
| `test_scheduler.py` | tick 注入时钟：到点触发/未到点跳过；lease 被占跳过；in-flight 去重；分组 |
| `test_lease.py` | InMemory try_acquire/renew/release/过期接管/换 holder |
| `test_reconcile.py` | 全部 key 过期 → resolve(reason=auto)；部分未过期不 resolve；anomaly_key 从 JSON dict 重建正确 |
| `test_problems_api.py` | GET list（过滤）+ GET by id + POST resolve |
| `test_alerts_api.py` | POST /v1/alerts/run 返回汇总；domain 过滤 |
| `test_monitor_run_api.py` | POST /v1/monitors/{id}/run 返回 DomainResult；404 未知 id |
| `test_config_api.py` | POST /v1/config/reload；GET/PUT /v1/config/{domain} |
| `test_maintenance_api.py` | 维护窗口 CRUD |
| `test_blacklist_api.py` | 黑名单 CRUD |
| `test_auth.py` | 未配置 api_keys → 放行；配置后无 key→401、跨租户→403、admin 校验 |
| `test_uc62_combo_critical.py` | **§13 用例 2 端到端**：内存泄漏+Full GC 同 service 两轮 → 第二轮 critical（related 组合升级） |

> `tests/conftest.py` 改：`Settings(_env_file=None, storage_backend="memory", enable_scheduler=False)`——避免后台 scheduler 干扰 API 测试。

## 验证（完成标准）

1. `make lint` — ruff + mypy 全绿。
2. `make test` — 全量 pytest 绿（原 225 + M6 新增，不回归）。
3. 完成标准复核：用例 2 critical；并发去重回归；reconcile 自动关闭；跨租户 403；多副本 lease。

## 文档同步（CLAUDE.md 流程）

1. 本文档落库（进行中）。
2. 完成后写 `docs/logs/M6.md` 实现日志。
3. 归档已实现章节到 `docs/archive/M6-*.md`；设计文档摘除 M6 已实现部分。
4. 更新 `README.md` 进度表。
5. 更新 `CLAUDE.md`「当前里程碑」。
6. 更新 `MEMORY.md`。
7. 提交 M6（`[huhao] feat: ...`，无 Co-Authored-By）。
