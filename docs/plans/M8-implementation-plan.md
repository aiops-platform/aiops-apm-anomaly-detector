# M8 实现计划：存储层 PostgreSQL 化

## 背景与目标

本模块的持久化层绑定 MySQL（`aiomysql` + 8 个 MySQL 方言迁移脚本），但本模块要接入的 `multi-agent-workflow` 那套 docker compose 里**没有 MySQL 服务，只有 PostgreSQL**。继续用 MySQL 意味着额外维护一个数据库容器，与本平台既有中间件割裂。

**目标：** `storage_backend` 由 `mysql` 改为 `pg`，服务连上 `multi-agent-workflow` 的 PG 后能跑通 `make migrate` → 采集 → L0–L3 漏斗 → `problem_record` 落库的完整链路。

## 前置事实（实测）

迁移前查 MySQL：`schema_versions` 已有 8 条记录（V1–V8 全部应用过），但**业务表全部 0 行**（仅 `scheduler_lease` 有 1 行心跳）。
→ **没有数据要迁移**，V1–V8 可原地重写为 PG 方言，无需「V9 移植」迁移。

## 设计决策

| # | 决策 | 理由 |
|---|---|---|
| 1 | 彻底替换 MySQL，`storage_backend: "pg" \| "memory"` | 无生产数据；双后端要维护两套 DDL + 两套 SQL，成本翻倍且必然漂移 |
| 2 | 驱动用 **psycopg3**（`psycopg[binary,pool]>=3.2`） | 保留 `%s` 占位符 → ~76 处 SQL 参数零改动；asyncpg 的 `$1` 序号制要重排全部占位符，机械但易错位。**注意 `pool` extra 在 3.1 不存在**，不能照抄 `multi-agent-workflow/pyproject.toml` 的 `>=3.1` |
| 3 | **`agentflow` 库 + 独立 schema `aiops_apm_runtime`** | 复用现成 PG 实例，不动对方的 compose/init；schema 隔离使 13 张表与 agentflow 的 `public` 表零冲突 |
| 4 | 时间列 `TIMESTAMP(3)` 存 **naive UTC**（非 `timestamptz`） | 与现有 `DATETIME(3)` 语义一致；`snapshots.py` 已刻意 `.replace(tzinfo=None)` |
| 5 | **新增真库集成测试道**（`APM_TEST_PG_DSN` 门控） | 12 个 store 里 8 个的 SQL 从未被真实执行过；整数除法、jsonb 路径这类 bug 字符串断言抓不到 |
| 6 | `updated_at` 用 PG 触发器 | 保留全部 13 张表的现有语义（含未来新表） |
| 7 | 一并修 `_next_target_id` 并发取号竞态 | 既有缺陷，本次要改这个文件，顺手收掉成本极低 |

## 必须解决的 Blocker（实现时逐条对照）

| # | 问题 | 处理 |
|---|---|---|
| A1 | `idx_tenant_service_time` 在 `change_record` 与 `maintenance_window` 重名。PG 索引名是 schema 级（MySQL 是表级）→ 第二条 `CREATE INDEX` 报 `42P07`，且因 PG DDL 事务性，整个迁移回滚 | 重命名 `maintenance_window` 上的为 `idx_tenant_service_window` |
| A2 | `CREATE INDEX` 无 `IF NOT EXISTS`，破坏幂等 | 全部加 `IF NOT EXISTS` |
| A3 | `.env` 仍是 mysql/3306；测试全传 `_env_file=None`，会「测试全绿但 `make dev` 起不来」 | 同步 `.env`/`.env.dev`/`.env.example`/compose |
| B1 | **时间戳分叉**：会话时区 `Asia/Shanghai` 下，`CURRENT_TIMESTAMP`（timestamptz→按会话时区折算）与 aware datetime（同）vs `_iso()` 字符串（offset 被忽略），同一 `trace_id` 会写出相差 8 小时的两个时间戳 | 连接串固定 `-c TimeZone=UTC` + `_ConnectionHandle` 内归一 aware datetime |
| B2 | `execute(sql, ())` 触发 psycopg3 的 `%` 扫描，裸 `%` 抛错且不认字符串字面量 | `if args: cur.execute(sql, args) else: cur.execute(sql)` |
| B3 | `Jsonb(dict)` 不在构造时序列化且默认 `dumps` 无 `default` 钩子 → TypeError 推迟到 `cur.execute` | `Jsonb(value, dumps=...)` |
| B4 | `FIELD()` → `array_position()` 在「新值在词表内、旧值不在」这一格不等价（NULL vs 0） | `COALESCE(array_position(...), 0)` |
| B5 | `jsonb_set` 路径须是 `'{miss_rounds}'`；写成 `'$.miss_rounds'` 会新建键，miss 计数永不增长且不报错 | 数组形态 + `COALESCE(...,0)` 防键缺失时写 NULL |
| B6 | `fpr` 的 `bigint/bigint` 整数除法让 FPR 闸门静默失效；裸列不能改成 `EXCLUDED.` | `::numeric`；裸列保持裸列 |
| C1 | lease 的接管守卫不能丢，否则第二个副本抢走活跃租约 | 保留 `CASE WHEN ... expires_at < CURRENT_TIMESTAMP(3) ...` |
| E2 | `search_path` 必须走 pool kwargs，不能 `open()` 后 `SET`（懒建连接拿不到） | `kwargs={"options": ...}` |
| E3 | `tests/test_settings.py` 断言 `db_name == "aiops_apm_runtime"`，只加 `db_schema` 会让该测试静默失效 | 让 `db_name` 表示库、新增 `db_schema` |
| H3 | `change_related: bool` 直通写 SMALLINT 列，psycopg3 的 `boolean → smallint` 无赋值转换 | Python 侧 `int(...)` 归一 |

## 改动文件

- **连接层**：`storage/connection.py` 重写（psycopg3 池、`execute_returning`、`_as_json`→`Jsonb`、删 `_Unset` 哨兵、`release()` 显式 rollback）
- **迁移**：`migrations/V1..V8__*.sql` 改写；`migrations/runner.py`（`CREATE SCHEMA`/`SET search_path`、`$$` 美元引用切分）
- **store**：`records`/`lease`/`sequence`/`watermarks`/`detection_state`/`domain_config`/`dynamic_config`/`monitor_targets`/`snapshots`/`rounds`；`storage/__init__.py` 分派
- **配置**：`settings.py`；`.env`/`.env.dev`/`.env.example`
- **租户归一**：`router/deps.py`、`auth/middleware.py`
- **交付**：`pyproject.toml`/`requirements.txt`、`docker/*`、`Makefile`
- **测试**：新增 `tests/test_pg_integration.py`；重写 `test_migrations.py`；同步 `test_lease.py`/`test_rounds.py`/`test_settings.py`/`test_storage.py`/`test_deliverables.py`
- **文档**：`README.md`、`CLAUDE.md`、`docs/project-overview.md`、`docs/apm-alert-module-design.md`、`docs/logs/M8.md`、本文件

## 验收标准

1. `make lint` 干净（仅剩 2 条本次之前就存在的既存 ruff 错误）。
2. `make test` 全绿，且**不需要 PG**（集成道自动 skip）。
3. `APM_TEST_PG_DSN=... make test-pg` 20 条全过。
4. `make migrate` 对真 PG 应用 8 个脚本，建齐 15 张表。
5. 端到端：起服务 → `/ready` 的 `db=true` → 建 target → 跑一轮 → `problem_record`/`detection_round` 落库，且**时间戳与真实 UTC 偏差 < 5s**。
6. `fpr_table.fpr` 是小数而非 0/1。

## 风险与接受的差异

- PG 的 DDL 事务性会让失败回滚全部（改进，但运维观感不同）。
- 当天首单编号由 MySQL 的 `-0000`/重号隐患改为恒 `-0001`（修掉一个潜在 bug）。
- `ON CONFLICT` 只认指定仲裁列：`record_id` 重复会硬报 `23505` 而非静默合并（概率低，接受）。
- 大小写敏感性差异：在租户入口归一，其余靠规范小写值约定。
- `ORDER BY ... DESC` 的 NULL 位置差异无影响（已核对，所有排序列均 `NOT NULL`）。
