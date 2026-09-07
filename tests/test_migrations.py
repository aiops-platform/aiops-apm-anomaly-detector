"""UC-2.1 数据库迁移执行：幂等可重入、按版本顺序执行、V1 建齐 12 张表。

用 FakePool/FakeConn 隔离真实 MySQL，单测迁移执行器逻辑。
"""

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


def test_load_scripts_parses_version() -> None:
    runner = _runner(FakeConn())
    scripts = runner._load_scripts()
    assert [s.version for s in scripts] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert "problem_record" in scripts[0].sql
    assert "collect_watermark" in scripts[1].sql
    assert "detection_round" in scripts[2].sql
    assert "monitor_target" in scripts[3].sql
    assert "detection_round_target" in scripts[4].sql
    assert "detection_round_target" in scripts[5].sql
    assert "signal_snapshot" in scripts[6].sql
    assert "detection_round_target" in scripts[7].sql


def test_v1_script_contains_twelve_tables_and_dedup_mechanism() -> None:
    runner = _runner(FakeConn())
    sql = runner._load_scripts()[0].sql
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


async def test_migrate_applies_new_scripts_in_order() -> None:
    conn = FakeConn(current_version=0)
    runner = _runner(conn)
    applied = await runner.migrate()
    assert applied == 8
    assert conn.schema_versions_created
    assert any(s.startswith("CREATE DATABASE IF NOT EXISTS aiops_apm_runtime") for s in conn.statements)
    assert any(s.strip().startswith("CREATE TABLE IF NOT EXISTS problem_record") for s in conn.statements)
    assert any(s.strip().startswith("CREATE TABLE IF NOT EXISTS collect_watermark") for s in conn.statements)
    assert any("ALTER TABLE detection_round ADD COLUMN domain" in s for s in conn.statements)
    assert any("ALTER TABLE monitor_target ADD COLUMN deleted" in s for s in conn.statements)
    assert any("CREATE TABLE IF NOT EXISTS detection_round_target" in s for s in conn.statements)
    assert any("ALTER TABLE detection_round_target ADD COLUMN anomaly_count" in s for s in conn.statements)
    assert any("ALTER TABLE signal_snapshot MODIFY COLUMN signature" in s for s in conn.statements)
    assert any("ALTER TABLE detection_round_target ADD COLUMN request_params" in s for s in conn.statements)
    # 版本号已记录
    assert any("INSERT INTO schema_versions" in s for s in conn.statements)


async def test_migrate_idempotent_skips_applied_versions() -> None:
    conn = FakeConn(current_version=1)
    runner = _runner(conn)
    applied = await runner.migrate()
    assert applied == 7  # V1 已应用，仅补 V2、V3、V4、V5、V6、V7、V8
    # 已应用版本不重复执行其建表语句
    assert not any("CREATE TABLE IF NOT EXISTS problem_record" in s for s in conn.statements)
    assert any("CREATE TABLE IF NOT EXISTS collect_watermark" in s for s in conn.statements)
    assert any("ALTER TABLE detection_round ADD COLUMN domain" in s for s in conn.statements)
    assert any("ALTER TABLE monitor_target ADD COLUMN deleted" in s for s in conn.statements)
    assert any("CREATE TABLE IF NOT EXISTS detection_round_target" in s for s in conn.statements)
    assert any("ALTER TABLE detection_round_target ADD COLUMN anomaly_count" in s for s in conn.statements)


def test_v2_script_contains_collect_watermark() -> None:
    runner = _runner(FakeConn())
    sql = runner._load_scripts()[1].sql
    assert "CREATE TABLE IF NOT EXISTS collect_watermark" in sql
    # 主键 (tenant_id, target_id) —— 每个端点一行水位线
    assert "PRIMARY KEY (tenant_id, target_id)" in sql
    assert "last_ts" in sql


def test_v3_script_adds_detection_round_domain() -> None:
    runner = _runner(FakeConn())
    sql = runner._load_scripts()[2].sql
    assert "ALTER TABLE detection_round ADD COLUMN domain" in sql
    assert "DEFAULT 'application'" in sql


def test_v4_script_adds_monitor_target_deleted() -> None:
    runner = _runner(FakeConn())
    sql = runner._load_scripts()[3].sql
    assert "ALTER TABLE monitor_target ADD COLUMN deleted" in sql
    assert "TINYINT(1) NOT NULL DEFAULT 0" in sql
    assert "AFTER enabled" in sql
    assert "idx_tenant_deleted" in sql


def test_v5_script_creates_detection_round_target() -> None:
    runner = _runner(FakeConn())
    sql = runner._load_scripts()[4].sql
    assert "CREATE TABLE IF NOT EXISTS detection_round_target" in sql
    # round → target 一对多：复合主键 (round_id, tenant_id, target_id)
    assert "PRIMARY KEY (round_id, tenant_id, target_id)" in sql
    # 独立采集状态 + 信号量，供孤儿恢复与 per-target 审计
    assert "status" in sql
    assert "signals_count" in sql
    assert "error" in sql
    assert "idx_tenant_target" in sql


def test_v6_script_adds_detection_round_target_counts() -> None:
    runner = _runner(FakeConn())
    sql = runner._load_scripts()[5].sql
    assert "ALTER TABLE detection_round_target" in sql
    # per-target 漏斗计数：异常/开单/被抑制，按 service 归因
    assert "ADD COLUMN anomaly_count" in sql
    assert "ADD COLUMN record_count" in sql
    assert "ADD COLUMN suppressed_count" in sql
    assert "NOT NULL DEFAULT 0" in sql


def test_v7_script_widens_signal_snapshot_signature() -> None:
    runner = _runner(FakeConn())
    sql = runner._load_scripts()[6].sql
    assert "ALTER TABLE signal_snapshot MODIFY COLUMN signature" in sql
    # 长堆栈日志签名可超 255（实测 Spring 异常 ≈ 376），varchar(255) 报 1406
    assert "VARCHAR(1024)" in sql


def test_v8_script_adds_detection_round_target_request_params() -> None:
    runner = _runner(FakeConn())
    sql = runner._load_scripts()[7].sql
    assert "ALTER TABLE detection_round_target" in sql
    # 本轮采集实际下发的出站请求参数（url/method/params），JSON 列供审计排查
    assert "ADD COLUMN request_params" in sql
    assert "JSON" in sql
    assert "AFTER error" in sql
