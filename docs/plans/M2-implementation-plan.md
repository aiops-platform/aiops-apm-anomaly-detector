# M2 持久化与迁移 — 实现计划

> 状态：**已实现**（2026-08-26）。实现日志见 [`docs/logs/M2.md`](../logs/M2.md)，历史规格归档见 [`docs/archive/M2-persistence.md`](../archive/M2-persistence.md)。

## Context（为什么做）

M1 契约层已冻结（`models/` + `fingerprint.py` + `plugins/base.py`，25 用例全绿）。M2 是**结果侧地基**：把单 schema `aiops_apm_runtime` 的表一次建齐（`make migrate`），并实现 M2 的 Use Case 直接依赖的存储层——`RecordStore`（problem_record 读写/去重）、`DomainConfigStore` + `DomainConfigLoader`（规则配置读/seed/降级回退）。M5 一开单就要落库，所以 M2 排在 M5 之前。

### M2 在整个系统里是干什么的？

整个系统是一条流水线：`采集 → L0 抑制 → L1 检测 → L2 关联 → L3 验证 → 问题单(problem_record) → 落库`。系统最终产出**问题单**，问题单要存进 MySQL 给下游用。M0+M1 之后系统还没有数据库、也没有写库能力，M2 把这个缺口补上——**打好「落库」的地基**。

| M2 交付 | 在整体流程中的位置 | 具体干什么 |
|--------|------------------|-----------|
| **① `make migrate`（建库建表）** | 整个系统的「仓库」地基 | 一次性建齐单 schema `aiops_apm_runtime` 的 12 张表（问题单表/配置表/监控目标表/维护窗口表/检测状态表等）。M3 采数据、M5 开单、M6 调度都要往这些表读写 |
| **② `RecordStore`（问题单记录本）** | 系统**最终输出**的写库层 | M5 emit 开单时写库：新问题开新单；同一问题再现（同 `group_key`）不重复开单，只追加证据/次数+1/时间更新/严重度升级；已解决问题复发再开新单；并发下同问题只产生一条记录 |
| **③ `DomainConfigStore` + `Loader`（规则手册）** | 系统的**规则配置面** | 检测规则存在 `domain_config` 表：从表读规则（M5 每轮检测前读）；空表用 `domains.yaml` 自动 seed；数据库挂了用上一版缓存规则顶上，服务不崩 |
| **④ 接入 app** | 横切探针 | storage 挂进 FastAPI lifespan，`/ready` 真实反映「数据库连上了没」 |

**为什么 M2 排在 M5 之前？** 因为 M5 一开单就要写库（RecordStore）、每轮就要读规则（DomainConfigLoader）。顺序是：M1 定义数据长什么样（契约）→ M2 把存储地基建好 → M5 才能真正把问题单写进库。一句话类比：M1 定义了流水线上零件的规格，M2 先建好仓库 + 准备好出单记录本和操作手册，M5 开始生产时直接就能往仓库里放货。

- **完成标准**：迁移幂等可重入；并发 `write_or_append` 同 `group_key` 只产生一条记录（靠 `open_group_key` 生成列 + UNIQUE + `INSERT ... ON DUPLICATE KEY UPDATE` 原子去重）；所有 store 无 `tenant_id` 入参则抛 `ValueError`
- **用户已确认的决策**：
  - **聚焦范围**：只建 UC-2.1~2.6 需要的模块；MonitorTarget/Snapshot/Watermark/Sequence/DetectionState/DynamicConfig 等 store 随消费它们的 M3/M5/M6 再建
  - **接入 app**：storage 挂进 FastAPI lifespan，`/ready` 反映数据库连接状态（会改 M0 的 `_app.py` / `router/api.py`）

## 改动点：位置与用途

| 文件 | 在流程中的位置 | 干什么 | 谁用 |
|------|--------------|--------|------|
| **migrations/runner.py** | `make migrate` 入口 | `MigrationRunner`：建 `schema_versions` 追踪表，按版本顺序幂等执行未跑的 SQL 脚本 | 运维/M2 迁移；`make migrate` |
| **migrations/V1__init_tables.sql** | 迁移脚本 | 12 张表全量 DDL（problem_record 含 P0 列 `severity`/`open_group_key` 等） | MigrationRunner 执行 |
| **storage/connection.py** | 横切连接层 | `ConnectionPool`（aiomysql 连接池：init/acquire/release/health_check/close） | 所有 MySQL store |
| **storage/records.py** | 问题单落库 | `RecordStore`（ABC）+ `InMemoryRecordStore` + `MySQLRecordStore`；`find_open`/`write_or_append`（原子去重）/`list`/`resolve` | M5 emit 写库；M6 API 查询 |
| **storage/domain_config.py** | 配置落库 | `DomainConfigStore`（ABC）+ `InMemory` + `MySQL`；`load`/`upsert`/`seed`（幂等） | M5 加载规则；M6 写入校验 |
| **config/loader.py** | 配置加载 | `DomainConfigLoader`：DB 为主源 → 空表用 `domains.yaml` seed → last-known-good 降级回退 | M5 每轮加载规则 |
| **config/domains.yaml** | seed 数据 | `application` 域检测规则 seed（detectors/suppressors/correlation/verify） | DomainConfigLoader 首次 seed |
| **storage/__init__.py** | 聚合 | `Storage` 聚合（records + domain_configs + pool）+ `build_storage(settings)` 按 `storage_backend` 分派 | `_app.py` lifespan |
| **\_app.py / router/api.py** | 横切 | lifespan 接线 storage；`/ready` 检查 db 连接（mysql 健康检查 / memory 恒可用） | 探针 |

数据流：

```
make migrate ──> MigrationRunner ──> V1__init_tables.sql ──> aiops_apm_runtime 12 张表
                                                                      │
build_storage(settings) ── mysql/memory ──> Storage(records, domain_configs)
                                                                      │
M5 emit ──> RecordStore.write_or_append ──> problem_record（group_key 原子去重）
M5 规则 ──> DomainConfigLoader.load ──> domain_config 表（空表 seed）
```

## 范围

### 交付（6 个 Use Case）
| UC | 名称 | 断言 |
|----|------|------|
| UC-2.1 | 数据库迁移执行 | `migrate()` 建齐 12 张表 + `schema_versions`；幂等可重入（二次执行不报错、不重复建） |
| UC-2.2 | 新开 problem_record | 同 `group_key` 无 open 记录 → 新开一行，`state=pending`，`open_group_key=group_key` |
| UC-2.3 | 追加 evidence（去重命中） | 同 `group_key` open 记录命中 → 记录数不变；`occurrence_count++`、`evidence` 追加、`last_seen_at` 更新、`severity` 取更高 |
| UC-2.4 | 已关闭记录复发开单 | 同 `group_key` 但 `state=resolved`（`open_group_key=NULL`）→ 新开一行，新 `record_id` |
| UC-2.5 | 配置加载与 Seed | `domain_config` 空表 → `domains.yaml` seed 幂等写入；二次 load 从 DB 读；seed 与 YAML 一致 |
| UC-2.6 | 配置加载失败回退 | DB 异常 → 返回 last-known-good 缓存，服务不崩溃 |

### 不做（明确排除）
- MonitorTarget/Snapshot/Watermark/Sequence/DetectionState/DynamicConfig store（M3/M5/M6）
- 前端（延续 M0 决策）；`POST /v1/migrate`/`GET /v1/migration-status` API（M6 归入 API 里程碑）
- 修改 `models/`（M1 契约冻结）、`settings.py`/`exceptions.py` 语义

## 关键实现细节

### `migrations/V1__init_tables.sql`（12 张表，镜像设计文档 §7.2/7.3 + P0 列）
- **problem_record**（P0 列：M1 `ProblemRecord` 标量列 + `open_group_key` 生成列）：
  - 标量列：`record_id`(PK,VARCHAR32)、`group_key`、`source`、`tenant_id`、`domain`、`state`、`service`、`instance`、`severity`、`detected_at`、`first_seen_at`、`last_seen_at`、`occurrence_count`、`resolved_at`、`resolve_reason`、`trace_id`
  - JSON 列：`symptom`、`metric_anomalies`、`log_anomalies`、`correlation`、`recent_change`、`verification`、`evidence`
  - 审计：`created_at`、`updated_at`
  - **去重机制**（满足并发单条标准）：
    ```sql
    open_group_key VARCHAR(255) GENERATED ALWAYS AS (
      CASE WHEN state IN ('pending','in_progress') THEN group_key ELSE NULL END
    ) STORED,
    UNIQUE KEY uk_open_group_key (tenant_id, open_group_key),
    KEY idx_group_key (group_key), KEY idx_tenant_state (tenant_id, state)
    ```
- 其余 11 张表：`change_record`、`domain_config`（UNIQUE `uk_tenant_domain`）、`monitor_target`（UNIQUE `uk_tenant_target_id`）、`maintenance_window`、`suppress_blacklist`、`fpr_table`（UNIQUE `uk_tenant_group_key`）、`record_seq`、`scheduler_lease`、`signal_snapshot`、`detection_state`（PK `tenant_id,domain,state_key`）、`detection_round`
- 表头：`CREATE DATABASE IF NOT EXISTS aiops_apm_runtime ...; USE aiops_apm_runtime;`（`make migrate` 可裸库建）

### `migrations/runner.py`
```python
class MigrationRunner:
    def __init__(self, pool, schema: str, scripts_dir: Path | None = None):
        # scripts_dir 默认 = 本文件所在目录（migrations/）
    def _load_scripts(self) -> list[MigrationScript]: ...   # glob "V<num>__*.sql"，按 version 排序
    def _split_statements(self, sql: str) -> list[str]: ... # 按 ';' 拆分，忽略 '--' 注释与字符串内的分号
    async def migrate(self): ...                            # 钉住单连接 → 建库/USE → schema_versions → 逐脚本 > current → 记版本 → commit
async def run_migrations(settings) -> int: ...              # db=None 的 ConnectionPool + runner
def main() -> None: asyncio.run(run_migrations(Settings()))
```

### `storage/connection.py`
- `ConnectionPool(settings, *, db=None)`：`db=None` 迁移用（裸库连接），默认 `db=settings.db_name`（store 用）
- `init`（`aiomysql.create_pool(..., autocommit=False, connect_timeout=3)`）/ `acquire`（返回 `_ConnectionHandle`）/ `release` / `execute` / `fetchone` / `fetchall` / `health_check`（`SELECT 1`）/ `close`

### `storage/records.py`
- `RecordStore(ABC)`：`find_open(tenant_id, group_key)` / `write_or_append(tenant_id, record)` / `list(tenant_id, *, state=None, service=None, limit=50)` / `resolve(tenant_id, record_id, reason="auto")`；每个方法入口 `if not tenant_id: raise ValueError(...)`
- **InMemoryRecordStore**（单测真源）：`find_open` = 同 tenant+group_key 且 `state in (pending, in_progress)`；`write_or_append` 命中 → evidence 追加 / `occurrence_count+=1` / `last_seen_at` 更新 / severity 取 rank 高，未命中 → 插入新行
- **MySQLRecordStore**（生产，原子去重）：
  ```sql
  INSERT INTO problem_record (...) VALUES (...)
  ON DUPLICATE KEY UPDATE
    evidence = JSON_MERGE_PRESERVE(IFNULL(evidence, JSON_ARRAY()), CAST(%s AS JSON)),
    occurrence_count = occurrence_count + 1,
    last_seen_at = VALUES(last_seen_at),
    severity = IF(FIELD(VALUES(severity),'warning','high','critical') > FIELD(severity,'warning','high','critical'), VALUES(severity), severity),
    updated_at = CURRENT_TIMESTAMP(3)
  ```
  JSON 字段 `json.dumps` 序列化；`resolve` → `state='resolved', resolved_at=NOW(3), resolve_reason=?`（open_group_key 自动变 NULL）

### `storage/domain_config.py`
- `DomainConfigStore(ABC)`：`load(tenant_id) -> list[dict]` / `upsert(tenant_id, domain, config: DomainConfig) -> int` / `seed(tenant_id, seed: list[dict]) -> None`（`INSERT ... ON DUPLICATE KEY UPDATE`）
- InMemory / MySQL 两版；行结构 `{"domain", "config"(JSON→dict), "enabled", "version"}`

### `config/loader.py` + `config/domains.yaml`
```python
class DomainConfigLoader:
    def __init__(self, store, yaml_seed_path=None): self._cache: list[dict] | None = None  # last-known-good
    async def load(self, tenant_id):
        try:
            rows = await self.store.load(tenant_id)
            if rows: self._cache = rows; return rows
            seed = self._load_yaml_seed()
            await self.store.seed(tenant_id, seed)
            rows = await self.store.load(tenant_id); self._cache = rows; return rows
        except Exception:
            if self._cache is not None: return self._cache   # UC-2.6 降级回退
            raise
    async def reload(self, tenant_id): self._cache = None; return await self.load(tenant_id)
```
`domains.yaml` 内容按设计文档 §8.3（application 域：3 detectors + 2 suppressors + correlation + verify）

### `storage/__init__.py` + `_app.py` / `/ready`
```python
class Storage:
    def __init__(self, *, records, domain_configs, pool=None): ...
    async def health_check(self): return True if self.pool is None else await self.pool.health_check()
    async def close(self): if self.pool: await self.pool.close()

async def build_storage(settings) -> Storage:
    if settings.storage_backend == "memory": return Storage(records=InMemoryRecordStore(), domain_configs=InMemoryDomainConfigStore())
    pool = ConnectionPool(settings, db=settings.db_name); await pool.init()
    return Storage(records=MySQLRecordStore(pool), domain_configs=MySQLDomainConfigStore(pool), pool=pool)
```
- `_app.py` lifespan startup：**fail-fast**（用户确认）：`app.state.storage = await build_storage(settings)` 直接赋值，mysql backend 连不上 DB 抛异常 → uvicorn 启动失败退出；memory backend 无此约束；shutdown：`await app.state.storage.close()`
- `router/api.py` `/ready`：`db = await storage.health_check() if storage else False`；`checks = {"db": db, "plugins": bool(registry)}`；非全绿 → 503 NOT_READY（保持现有响应形状）

## 测试（TDD，先写测试再实现）

- `tests/test_migrations.py`（UC-2.1）：FakePool（记录执行的 SQL + schema_versions 假响应）→ `migrate()` 按版本顺序执行、建 schema_versions、二次执行幂等跳过；`_split_statements` 处理注释/引号内分号；真实 `V1__init_tables.sql` 可被 `_load_scripts` 加载且含 12 张表
- `tests/test_records.py`（UC-2.2/2.3/2.4）：InMemoryRecordStore 新开 / 追加（evidence+count+severity）/ resolved 复发开新单；tenant 隔离；无 tenant_id → `ValueError`
- `tests/test_domain_config.py`（UC-2.5/2.6）：InMemoryDomainConfigStore + tmp `domains.yaml` → 空表 seed 幂等 / 二次 load 从 store 读 / store 抛异常回退 last-known-good；`upsert`
- `tests/test_storage.py`：`build_storage(Settings(storage_backend="memory"))` → Storage 健康、close 无副作用；未知 backend 抛 `ValueError`
- `tests/test_health.py` 更新：`/ready` reason 含 `'db': True`（memory）

## 验证（完成标准）

1. `make lint` — ruff + mypy 通过（含新 migrations/storage/config 与测试）
2. `make test` — 新增 4 个测试文件全过，原 25 用例不回归
3. `make migrate` — 连真 MySQL 手动验证：首次建库建表、二次幂等、`SHOW TABLES` 12+1 张
4. `make dev` — memory backend 下 `/ready` → db:True；mysql backend 连不上 DB 时 **fail-fast** 启动失败退出（不再降级启动）

## 文档同步（CLAUDE.md 流程）

1. 存实现计划 `docs/plans/M2-implementation-plan.md`
2. 写 `docs/logs/M2.md`（改动点、文件清单、完成状态、遗留问题）
3. 归档 `docs/archive/M2-persistence.md`；实现计划 M2 小节标记「已实现」指向归档
4. 更新 `README.md`：进度表 M2 → 已完成；已实现模块补 storage/migrations/config；启动附加步骤补 `make migrate`
5. 更新 `CLAUDE.md`：里程碑状态 → M2 已完成，M3 进行中
