# M2 持久化与迁移 — 历史规格归档

> 本文归档 `docs/apm-alert-implementation-plan-enhanced.md` 中 M2 小节（持久化与迁移）已实现的部分。实现日志见 [`docs/logs/M2.md`](../logs/M2.md)。

## 目标

打底「结果侧地基」：把单 schema `aiops_apm_runtime` 的表一次建齐（`make migrate`），并实现 M2 Use Case 直接依赖的存储层——`RecordStore`（problem_record 读写/去重）、`DomainConfigStore` + `DomainConfigLoader`（规则配置读/seed/降级回退）。M5 一开单就要落库，故 M2 排在 M5 之前。

## 数据流定位

```
make migrate ──> MigrationRunner ──> V1__init_tables.sql ──> aiops_apm_runtime 12 张表
                                                                      │
build_storage(settings) ── mysql/memory ──> Storage(records, domain_configs)
                                                                      │
M5 emit ──> RecordStore.write_or_append ──> problem_record（group_key 原子去重）
M5 规则 ──> DomainConfigLoader.load ──> domain_config 表（空表 seed）
```

## 交付（6 个 Use Case）

| UC | 名称 | 断言 |
|----|------|------|
| UC-2.1 | 数据库迁移执行 | `migrate()` 建齐 12 张表 + `schema_versions`；幂等可重入（二次执行不报错、不重复建） |
| UC-2.2 | 新开 problem_record | 同 `group_key` 无 open 记录 → 新开一行，`state=pending`，`open_group_key=group_key` |
| UC-2.3 | 追加 evidence（去重命中） | 同 `group_key` open 记录命中 → 记录数不变；`occurrence_count++`、`evidence` 追加、`last_seen_at` 更新、`severity` 取更高 |
| UC-2.4 | 已关闭记录复发开单 | 同 `group_key` 但 `state=resolved`（`open_group_key=NULL`）→ 新开一行，新 `record_id` |
| UC-2.5 | 配置加载与 Seed | `domain_config` 空表 → `domains.yaml` seed 幂等写入；二次 load 从 DB 读；seed 与 YAML 一致 |
| UC-2.6 | 配置加载失败回退 | DB 异常 → 返回 last-known-good 缓存，服务不崩溃 |

## 关键实现

### 迁移（`migrations/`）
- `runner.py` — `MigrationRunner`：`_load_scripts`（glob `V<num>__*.sql` 按版本排序）、`_split_statements`（按 `;` 拆分，忽略 `--` 注释与引号内分号）、`ensure_schema_versions`、`get_current_version`、`migrate`（钉住单连接：建库 → `USE` → 建 `schema_versions` → 逐脚本执行 > current → 记版本 → commit）。`run_migrations(settings)` 用 `db=None` 裸库连接；`main()` 为 `make migrate` 入口。
- `V1__init_tables.sql` — 12 张表：`problem_record`、`change_record`、`domain_config`、`monitor_target`、`maintenance_window`、`suppress_blacklist`、`fpr_table`、`record_seq`、`scheduler_lease`、`signal_snapshot`、`detection_state`、`detection_round`。
- **problem_record 原子去重**（满足并发单条标准）：
  ```sql
  open_group_key VARCHAR(255) GENERATED ALWAYS AS (
    CASE WHEN state IN ('pending','in_progress') THEN group_key ELSE NULL END
  ) STORED,
  UNIQUE KEY uk_open_group_key (tenant_id, open_group_key)
  ```
  `INSERT ... ON DUPLICATE KEY UPDATE` 追加 evidence/次数/时间，`severity` 用 `FIELD()` 升序取高。

### 存储（`storage/`）
- `connection.py` — `ConnectionPool`（aiomysql）：`init/acquire/release/execute/fetchone/fetchall/health_check/close`；`db=None` 迁移用、`db=settings.db_name` 应用用；`connect_timeout=3` 短超时避免 demo 挂起。
- `records.py` — `RecordStore(ABC)`：`find_open(tenant_id, group_key)` / `write_or_append(tenant_id, record)` / `list(tenant_id, *, state, service, limit=50)` / `resolve(tenant_id, record_id, reason="auto")`。`InMemoryRecordStore`（单测真源）+ `MySQLRecordStore`（生成列 + ON DUPLICATE KEY UPDATE，evidence 用 `JSON_MERGE_PRESERVE` 按元素拼接）。
- `domain_config.py` — `DomainConfigStore(ABC)`：`load(tenant_id)` / `upsert(tenant_id, domain, config) -> version` / `seed(tenant_id, seed)`（幂等）。`InMemory` + `MySQL`。
- `__init__.py` — `Storage` 聚合（records + domain_configs + pool）+ `build_storage(settings)` 按 `storage_backend`（mysql/memory）分派，未知 backend 抛 `ValueError`。
- 所有 store 方法入口 `if not tenant_id: raise ValueError(...)`。

### 配置（`config/`）
- `loader.py` — `DomainConfigLoader`：DB 主源 → 空表 `_load_yaml_seed()` seed → `_cache`（last-known-good）在 DB 异常时回退，不崩溃；`reload(tenant_id)` 清缓存重载。
- `domains.yaml` — `application` 域 seed（3 detectors + 2 suppressors + correlation + verify，结构同 `domain_config.config`，设计文档 §8.3）。

### 接入 app
- `_app.py` lifespan：**fail-fast** 接线（`app.state.storage = await build_storage(settings)`，mysql backend 连不上 DB 抛异常 → uvicorn 启动失败退出；memory backend 无此约束）；shutdown `await storage.close()`。
- `router/api.py` `/ready`：`db = await storage.health_check() if storage else False`；非全绿 → 503 `NOT_READY`（保持响应形状）。

## 范围（不做）
- MonitorTarget / DetectionState / Snapshot / Watermark / Sequence / DynamicConfig store（随 M3/M5/M6 消费它们的阶段再建）
- 前端；`POST /v1/migrate` / `GET /v1/migration-status` API（M6 归入 API 里程碑）
- 修改 `models/`（M1 契约冻结）、`settings.py`/`exceptions.py` 语义
