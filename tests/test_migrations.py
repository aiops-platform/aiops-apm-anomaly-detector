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
    assert [s.version for s in scripts] == [1, 2]
    assert "problem_record" in scripts[0].sql
    assert "collect_watermark" in scripts[1].sql


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
    assert applied == 2
    assert conn.schema_versions_created
    assert any(s.startswith("CREATE DATABASE IF NOT EXISTS aiops_apm_runtime") for s in conn.statements)
    assert any(s.strip().startswith("CREATE TABLE IF NOT EXISTS problem_record") for s in conn.statements)
    assert any(s.strip().startswith("CREATE TABLE IF NOT EXISTS collect_watermark") for s in conn.statements)
    # 版本号已记录
    assert any("INSERT INTO schema_versions" in s for s in conn.statements)


async def test_migrate_idempotent_skips_applied_versions() -> None:
    conn = FakeConn(current_version=1)
    runner = _runner(conn)
    applied = await runner.migrate()
    assert applied == 1  # V1 已应用，仅补 V2
    # 已应用版本不重复执行其建表语句
    assert not any("CREATE TABLE IF NOT EXISTS problem_record" in s for s in conn.statements)
    assert any("CREATE TABLE IF NOT EXISTS collect_watermark" in s for s in conn.statements)


def test_v2_script_contains_collect_watermark() -> None:
    runner = _runner(FakeConn())
    sql = runner._load_scripts()[1].sql
    assert "CREATE TABLE IF NOT EXISTS collect_watermark" in sql
    # 主键 (tenant_id, target_id) —— 每个端点一行水位线
    assert "PRIMARY KEY (tenant_id, target_id)" in sql
    assert "last_ts" in sql
