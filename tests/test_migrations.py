"""UC-2.1 数据库迁移执行：幂等可重入、按版本顺序执行、V1 建齐 12 张表。

用 FakePool/FakeConn 隔离真实 PostgreSQL，单测迁移执行器逻辑。
（真实执行 V1..V9 的验证见 ``tests/test_pg_integration.py``，需 APM_TEST_PG_DSN。）
"""

import re
from pathlib import Path

from aiops_apm.migrations.runner import MigrationRunner

MIGRATIONS_DIR = Path(__file__).parent.parent / "src/aiops_apm/migrations"


class FakeConn:
    """模拟钉住的连接：记录执行的 SQL，schema_versions 有假响应。"""

    def __init__(self, current_version: int = 0) -> None:
        self.current_version = current_version
        self.statements: list[str] = []
        self.schema_versions_created = False

    async def execute(self, sql: str, args: tuple = ()) -> None:
        self.statements.append(sql)
        if "CREATE TABLE IF NOT EXISTS schema_versions" in sql:
            self.schema_versions_created = True

    async def fetchone(self, sql: str, args: tuple = ()) -> tuple | None:
        if "MAX(version)" in sql:
            return (self.current_version,)
        return None

    async def commit(self) -> None:
        pass


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self.released = False

    async def acquire(self) -> FakeConn:
        return self.conn

    async def release(self, conn: object) -> None:
        assert conn is self.conn
        self.released = True


def _runner(conn: FakeConn) -> MigrationRunner:
    return MigrationRunner(FakePool(conn), schema="aiops_apm_runtime", scripts_dir=MIGRATIONS_DIR)


def _sql_only(sql: str) -> str:
    """剥掉 ``--`` 行注释，只留可执行 SQL。

    迁移脚本的注释里会**解释**被替换掉的 MySQL 语法（如"MySQL 版的 AFTER tenant_id 已去掉"），
    直接对原文做 ``"AFTER" not in sql`` 之类的断言会被注释带偏。
    """
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _script(version: int) -> str:
    """取第 ``version`` 个迁移脚本的可执行 SQL（已剥注释）。"""
    return _sql_only(_runner(FakeConn())._load_scripts()[version - 1].sql)


def test_split_statements_ignores_comments_and_quoted_semicolons() -> None:
    runner = _runner(FakeConn())
    sql = """
    -- 注释里的分号; 要忽略
    CREATE TABLE IF NOT EXISTS t1 (a VARCHAR(1) DEFAULT 'x;y');
    INSERT INTO t1 VALUES ('a;b');
    -- 尾部注释
    SELECT 1;
    """
    stmts = runner._split_statements(sql)
    assert len(stmts) == 3
    assert all("注释" not in s and not s.startswith("--") for s in stmts)
    # 引号内的分号被保留，不被当作语句分隔符
    assert "'a;b'" in stmts[1]
    assert stmts[1] == "INSERT INTO t1 VALUES ('a;b')"


def test_split_statements_handles_dollar_quoted_bodies() -> None:
    """美元引用体（PL/pgSQL 函数体）必须整段不被切碎。

    这条是 M8 加的：原先的切分器只认 ' / " / 反引号，函数体里的 `;` 会把一条
    CREATE FUNCTION 拆成好几段。更阴的是——只给注释检查加 `and not in_dollar` 而保留
    引号翻转的话，函数体里的撇号（``RAISE EXCEPTION 'x'``、注释里的 ``don't``）会翻转
    in_single，把**后续所有语句**吞进一条。所以体里刻意放了分号、撇号和 -- 注释。
    """
    runner = _runner(FakeConn())
    sql = """
    CREATE OR REPLACE FUNCTION f() RETURNS trigger AS $$
    BEGIN
        -- don't split; this semicolon and apostrophe are inside the body
        RAISE EXCEPTION 'boom; boom';
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    SELECT 1;
    """
    stmts = runner._split_statements(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE OR REPLACE FUNCTION")
    assert stmts[0].endswith("$$ LANGUAGE plpgsql")
    assert "RAISE EXCEPTION 'boom; boom'" in stmts[0]
    assert "don't split" in stmts[0]
    assert stmts[1] == "SELECT 1"


def test_split_statements_handles_named_dollar_tags() -> None:
    """``$tag$ ... $tag$`` 命名标签同样要整段保留（且不误吞 $1 位置参数）。"""
    runner = _runner(FakeConn())
    sql = "CREATE FUNCTION g() RETURNS int AS $body$ SELECT $1 + 1; $body$ LANGUAGE sql; SELECT 2;"
    stmts = runner._split_statements(sql)
    assert len(stmts) == 2
    assert stmts[0].endswith("$body$ LANGUAGE sql")
    assert stmts[1] == "SELECT 2"


def test_load_scripts_parses_version() -> None:
    runner = _runner(FakeConn())
    scripts = runner._load_scripts()
    assert [s.version for s in scripts] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert "problem_record" in scripts[0].sql
    assert "collect_watermark" in scripts[1].sql
    assert "detection_round" in scripts[2].sql
    assert "monitor_target" in scripts[3].sql
    assert "detection_round_target" in scripts[4].sql
    assert "detection_round_target" in scripts[5].sql
    assert "signal_snapshot" in scripts[6].sql
    assert "detection_round_target" in scripts[7].sql


def test_v1_script_contains_twelve_tables_and_dedup_mechanism() -> None:
    sql = _script(1)
    tables = [
        "problem_record",
        "change_record",
        "domain_config",
        "monitor_target",
        "maintenance_window",
        "suppress_blacklist",
        "fpr_table",
        "record_seq",
        "scheduler_lease",
        "signal_snapshot",
        "detection_state",
        "detection_round",
    ]
    for t in tables:
        assert f"CREATE TABLE IF NOT EXISTS {t}" in sql
    # 去重机制：open_group_key 生成列 + UNIQUE 键
    assert "open_group_key" in sql
    assert "uk_open_group_key" in sql
    # P0 列：severity / 生命周期列
    assert "severity" in sql
    assert "occurrence_count" in sql


def test_v1_index_names_unique_within_schema() -> None:
    """PG 的索引名是 **schema 级** 的（MySQL 是表级），重名会直接报 42P07。

    MySQL 版 V1 里 ``idx_tenant_service_time`` 同时出现在 change_record 和
    maintenance_window 上——照搬过来第二条 CREATE INDEX 就失败，且因为 PG 的 DDL 是
    事务性的，整个 V1（连带 V2..V8）全部回滚。这条断言守住改名后的唯一性。
    """
    sql = _script(1)
    names = re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", sql)
    assert names, "V1 应当有 CREATE INDEX 语句"
    assert len(names) == len(set(names)), f"索引名重复：{sorted(n for n in names if names.count(n) > 1)}"


def test_v1_creates_schema_and_updated_at_trigger() -> None:
    """PG 没有 MySQL 的 ON UPDATE CURRENT_TIMESTAMP(3)，改用 BEFORE UPDATE 触发器。"""
    sql = _script(1)
    assert "CREATE OR REPLACE FUNCTION set_updated_at()" in sql
    assert "$$" in sql  # 函数体用美元引用
    # CREATE TRIGGER 在 PG 17 没有 IF NOT EXISTS，必须靠 DROP ... IF EXISTS 保证可重入
    assert "DROP TRIGGER IF EXISTS" in sql
    assert sql.count("CREATE TRIGGER") == sql.count("DROP TRIGGER IF EXISTS")


def test_v9_seeds_testbed_log_targets() -> None:
    """V9：测试床三服务的日志端点随 ``make migrate`` 一并就位。

    这是首次在迁移里放业务数据（V1–V8 全是 DDL）。断言要点：
    - 幂等（``ON CONFLICT DO NOTHING``）——迁移不该覆盖现网已有行；
    - target_id 必须是 ``MT-NNNN`` 形态，否则 ``_parse_suffix`` 解析失败返回 0，
      下一个新建端点会拿到 MT-0001 撞唯一键；
    - ES 地址走 GUC 取，未设时 COALESCE 兜底（手工 psql 跑也能用）。
    """
    sql = _script(9)
    for svc in ("order-service", "warranty-service", "gateway-service"):
        assert svc in sql
    assert "ON CONFLICT (tenant_id, target_id) DO NOTHING" in sql
    assert "current_setting('aiops.testbed_es_url', true)" in sql
    assert "COALESCE(" in sql
    # target_id 可解析
    for tid in ("MT-0001", "MT-0002", "MT-0003"):
        assert f"'{tid}'" in sql
    # 采集配置要点：ES 查询 DSL 的两个开关 + 带 _source. 前缀的映射
    assert "app.service.keyword" in sql
    assert "'_source.@timestamp'" in sql
    assert "'hits.hits'" in sql


def test_scripts_do_not_hardcode_schema() -> None:
    """迁移脚本里不得出现 ``CREATE SCHEMA`` / ``SET search_path`` —— schema 由 runner 按配置注入。

    这条是有来历的回归守卫：脚本一旦写死 schema，``settings.db_schema`` 就失效——脚本执行的
    瞬间 search_path 被切回硬编码值，后续建表落到**别的** schema，而 ``INSERT INTO
    schema_versions`` 会撞上那个 schema 里已有的版本号。默认配置下两个名字相同、问题被掩盖，
    只有换一个 db_schema 才会暴露。MySQL 版能写死 ``USE aiops_apm_runtime`` 是因为库名恒等于
    schema 名；PG 的 schema 是可配置的。
    """
    for version in range(1, 10):
        sql = _script(version)
        assert "CREATE SCHEMA" not in sql, f"V{version} 不应自己建 schema"
        assert "SET search_path" not in sql, f"V{version} 不应自己设 search_path"


async def test_migrate_applies_new_scripts_in_order() -> None:
    conn = FakeConn(current_version=0)
    runner = _runner(conn)
    applied = await runner.migrate()
    assert applied == 9
    assert conn.schema_versions_created
    assert any(s.startswith("CREATE SCHEMA IF NOT EXISTS aiops_apm_runtime") for s in conn.statements)
    assert any(s.strip().startswith("CREATE TABLE IF NOT EXISTS problem_record") for s in conn.statements)
    assert any(s.strip().startswith("CREATE TABLE IF NOT EXISTS collect_watermark") for s in conn.statements)
    assert any("ALTER TABLE detection_round ADD COLUMN IF NOT EXISTS domain" in s for s in conn.statements)
    assert any("ALTER TABLE monitor_target ADD COLUMN IF NOT EXISTS deleted" in s for s in conn.statements)
    assert any("CREATE TABLE IF NOT EXISTS detection_round_target" in s for s in conn.statements)
    assert any("ADD COLUMN IF NOT EXISTS anomaly_count" in s for s in conn.statements)
    assert any("ALTER TABLE signal_snapshot ALTER COLUMN signature TYPE VARCHAR(1024)" in s for s in conn.statements)
    assert any("ADD COLUMN IF NOT EXISTS request_params" in s for s in conn.statements)
    # 版本号已记录
    assert any("INSERT INTO schema_versions" in s for s in conn.statements)


async def test_migrate_idempotent_skips_applied_versions() -> None:
    conn = FakeConn(current_version=1)
    runner = _runner(conn)
    applied = await runner.migrate()
    assert applied == 8  # V1 已应用，仅补 V2..V9
    # 已应用版本不重复执行其建表语句
    assert not any("CREATE TABLE IF NOT EXISTS problem_record" in s for s in conn.statements)
    assert any("CREATE TABLE IF NOT EXISTS collect_watermark" in s for s in conn.statements)
    assert any("ALTER TABLE detection_round ADD COLUMN IF NOT EXISTS domain" in s for s in conn.statements)
    assert any("ALTER TABLE monitor_target ADD COLUMN IF NOT EXISTS deleted" in s for s in conn.statements)
    assert any("CREATE TABLE IF NOT EXISTS detection_round_target" in s for s in conn.statements)
    assert any("ADD COLUMN IF NOT EXISTS anomaly_count" in s for s in conn.statements)


def test_v2_script_contains_collect_watermark() -> None:
    sql = _script(2)
    assert "CREATE TABLE IF NOT EXISTS collect_watermark" in sql
    # 主键 (tenant_id, target_id) —— 每个端点一行水位线
    assert "PRIMARY KEY (tenant_id, target_id)" in sql
    assert "last_ts" in sql


def test_v3_script_adds_detection_round_domain() -> None:
    sql = _script(3)
    assert "ALTER TABLE detection_round ADD COLUMN IF NOT EXISTS domain" in sql
    assert "DEFAULT 'application'" in sql
    # PG 无列序概念，MySQL 版的 AFTER tenant_id 必须去掉
    assert "AFTER" not in sql


def test_v4_script_adds_monitor_target_deleted() -> None:
    sql = _script(4)
    assert "ALTER TABLE monitor_target ADD COLUMN IF NOT EXISTS deleted" in sql
    # SMALLINT 而非 BOOLEAN：代码里有 `WHERE deleted=0`，PG 的 boolean = integer 会直接报错
    assert "SMALLINT NOT NULL DEFAULT 0" in sql
    assert "BOOLEAN" not in sql.upper()
    assert "idx_tenant_deleted" in sql
    assert "AFTER" not in sql


def test_v5_script_creates_detection_round_target() -> None:
    sql = _script(5)
    assert "CREATE TABLE IF NOT EXISTS detection_round_target" in sql
    # round → target 一对多：复合主键 (round_id, tenant_id, target_id)
    assert "PRIMARY KEY (round_id, tenant_id, target_id)" in sql
    # 独立采集状态 + 信号量，供孤儿恢复与 per-target 审计
    assert "status" in sql
    assert "signals_count" in sql
    assert "error" in sql
    assert "idx_tenant_target" in sql


def test_v6_script_adds_detection_round_target_counts() -> None:
    sql = _script(6)
    assert "ALTER TABLE detection_round_target" in sql
    # per-target 漏斗计数：异常/开单/被抑制，按 service 归因
    assert "ADD COLUMN IF NOT EXISTS anomaly_count" in sql
    assert "ADD COLUMN IF NOT EXISTS record_count" in sql
    assert "ADD COLUMN IF NOT EXISTS suppressed_count" in sql
    assert "NOT NULL DEFAULT 0" in sql
    assert "AFTER" not in sql


def test_v7_script_widens_signal_snapshot_signature() -> None:
    sql = _script(7)
    # MySQL 的 MODIFY COLUMN → PG 的 ALTER COLUMN ... TYPE
    assert "ALTER TABLE signal_snapshot ALTER COLUMN signature TYPE VARCHAR(1024)" in sql
    assert "MODIFY" not in sql.upper()
    # 长堆栈日志签名可超 255（实测 Spring 异常 ≈ 376），varchar(255) 写库报错 → 采集降级
    assert "VARCHAR(1024)" in sql


def test_v8_script_adds_detection_round_target_request_params() -> None:
    sql = _script(8)
    assert "ALTER TABLE detection_round_target" in sql
    # 本轮采集实际下发的出站请求参数（url/method/params），JSONB 列供审计排查
    assert "ADD COLUMN IF NOT EXISTS request_params" in sql
    assert "JSONB" in sql
    assert "AFTER" not in sql
