"""真库集成测试：``APM_TEST_PG_DSN`` 门控。

未设该环境变量 → 整文件 skip，``make test`` 依旧不需要 PG。设了就跑**真实建表 + 读写**。

存在的理由：M0–M7 期间 12 个 store 里有 8 个的 SQL 从未被真实执行过，全靠 FakePool 做字符串
断言——而字符串对上了不等于 SQL 能被 PG 解析。本次迁移最容易静默出错的那几处（整数除法、
jsonb 拼接路径、时区折算、search_path 是否覆盖每条连接）字符串断言一个都抓不到。

跑法：
    APM_TEST_PG_DSN=postgresql://agentflow:agentflow@127.0.0.1:5432/agentflow make test

表建在独立 schema（默认 ``aiops_apm_test``，可用 ``APM_TEST_PG_SCHEMA`` 覆盖），
跑完 drop，不碰正式 schema。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

import pytest
import pytest_asyncio

from aiops_apm.migrations.runner import MigrationRunner
from aiops_apm.models.record import Correlation, ProblemRecord, Verification
from aiops_apm.models.signal import LogSignal
from aiops_apm.settings import Settings
from aiops_apm.storage.connection import ConnectionPool
from aiops_apm.storage.detection_state import PGDetectionStateStore
from aiops_apm.storage.domain_config import PGDomainConfigStore
from aiops_apm.storage.dynamic_config import PGDynamicConfigStore
from aiops_apm.storage.lease import PGLeaseStore
from aiops_apm.storage.monitor_targets import PGMonitorTargetStore
from aiops_apm.storage.records import PGRecordStore
from aiops_apm.storage.rounds import PGRoundStore
from aiops_apm.storage.sequence import PGSequenceStore
from aiops_apm.storage.snapshots import PGSnapshotStore
from aiops_apm.storage.watermarks import PGWatermarkStore

_DSN = os.getenv("APM_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="需 APM_TEST_PG_DSN 指向一个可写的 PostgreSQL")

SCHEMA = os.getenv("APM_TEST_PG_SCHEMA", "aiops_apm_test")


def _settings_from_dsn(dsn: str) -> Settings:
    u = urlparse(dsn)
    return Settings(
        _env_file=None,
        storage_backend="pg",
        db_host=u.hostname or "127.0.0.1",
        db_port=u.port or 5432,
        db_user=unquote(u.username or ""),
        db_password=unquote(u.password or ""),
        db_name=(u.path or "/postgres").lstrip("/"),
        db_schema=SCHEMA,
    )


@pytest_asyncio.fixture
async def pool() -> object:
    """建 schema + 跑 V1..V8，跑完 drop schema。"""
    settings = _settings_from_dsn(_DSN or "")
    p = ConnectionPool(settings)
    await p.init()
    handle = await p.acquire()
    try:
        await handle.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await handle.commit()
    finally:
        await p.release(handle)

    await MigrationRunner(p, schema=SCHEMA).migrate()
    try:
        yield p
    finally:
        handle = await p.acquire()
        try:
            await handle.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            await handle.commit()
        finally:
            await p.release(handle)
        await p.close()


def _record(
    *, record_id: str = "PR-20260917-0001", tenant_id: str = "default", severity: str = "warning",
    detected_at: datetime | None = None, evidence: list | None = None, change_related: bool = False,
) -> ProblemRecord:
    return ProblemRecord(
        record_id=record_id,
        tenant_id=tenant_id,
        domain="application",
        service="svc-a",
        severity=severity,
        detected_at=detected_at or datetime.now(timezone.utc),
        symptom={"summary": "cpu high"},
        metric_anomalies=[],
        log_anomalies=[],
        correlation=Correlation(related=False, reason="n/a"),
        verification=Verification(passed=True, persistence_ok=True, final_severity=severity),
        evidence=evidence or [],
        change_related=change_related,
    )


# ---- 迁移本身 ----


async def test_migration_creates_schema_and_tables(pool) -> None:
    rows = await pool.fetchall(
        "SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename", (SCHEMA,)
    )
    names = {r[0] for r in rows}
    assert {"problem_record", "detection_round", "detection_round_target", "scheduler_lease"} <= names
    assert "schema_versions" in names
    # 迁移幂等：再跑一次不应重复应用
    assert await MigrationRunner(pool, schema=SCHEMA).migrate() == 0


async def test_v9_seeds_three_log_targets(pool) -> None:
    """V9 随 ``make migrate`` 把三个测试床日志端点一并建好，不用额外跑 seed 脚本。"""
    rows = await pool.fetchall(
        "SELECT target_id, service, signal_type, source_type, domain, source_config, schedule "
        "FROM monitor_target WHERE tenant_id=%s ORDER BY target_id",
        ("default",),
    )
    assert [r[0] for r in rows] == ["MT-0001", "MT-0002", "MT-0003"]
    assert [r[1] for r in rows] == ["order-service", "warranty-service", "gateway-service"]
    for target_id, _svc, signal_type, source_type, domain, sc, schedule in rows:
        assert (signal_type, source_type, domain) == ("log", "elk", "application")
        assert sc["url"].endswith("/app-logs/_search"), f"{target_id} url={sc['url']}"
        # ES 查询 DSL 的两个开关（时间窗/服务过滤走 POST body）
        assert sc["time_field"] == "@timestamp"
        assert sc["service_field"] == "app.service.keyword"
        # 路径带 _source. 前缀（采集器不剥壳）
        assert sc["field_mapping"]["timestamp"] == "_source.@timestamp"
        assert schedule["interval_sec"] == 60


async def test_v9_es_url_comes_from_runner_guc() -> None:
    """V9 的 ES 地址由 MigrationRunner 以 GUC 注入 —— SQL 是静态文件读不到环境变量。

    SQL 里用 ``current_setting('aiops.testbed_es_url', true)`` 取，runner 用
    ``set_config`` 写入（值取自 ``APM_TESTBED_ES_URL``）。容器里 ``localhost`` 指向容器
    自己，需要传 ``host.containers.internal:19200``，所以这条注入必须真的生效。
    """
    schema = f"{SCHEMA}_guc"
    settings = _settings_from_dsn(_DSN or "").model_copy(update={"db_schema": schema})
    p = ConnectionPool(settings)
    await p.init()
    handle = await p.acquire()
    try:
        await handle.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await handle.commit()
    finally:
        await p.release(handle)
    try:
        custom = "http://host.containers.internal:19200/app-logs/_search"
        await MigrationRunner(p, schema=schema, testbed_es_url=custom).migrate()
        row = await p.fetchone("SELECT source_config->>'url' FROM monitor_target WHERE target_id=%s", ("MT-0001",))
        assert row is not None and row[0] == custom, f"GUC 未生效，url={row[0] if row else None}"
    finally:
        handle = await p.acquire()
        try:
            await handle.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await handle.commit()
        finally:
            await p.release(handle)
        await p.close()


# ---- 就绪探针：没跑迁移必须能被识别出来 ----


async def test_schema_ready_false_when_migration_missing() -> None:
    """schema 不存在时必须为 False —— 这是 build_storage fail-fast 的依据。

    PG 下库是共享的，schema 缺失时**连接照样成功**，所以只探 ``SELECT 1`` 会让服务正常
    启动、``/ready`` 报 ready，而每个真实查询都 500（``relation ... does not exist``），
    k8s 下会把流量打到坏 Pod 上。MySQL 时代库不存在时连接本身就失败，没有这个问题。
    """
    settings = _settings_from_dsn(_DSN or "").model_copy(update={"db_schema": "aiops_apm_absent_schema"})
    p = ConnectionPool(settings)
    await p.init()
    try:
        assert await p.schema_ready() is False
        assert await p.health_check() is False
    finally:
        await p.close()


async def test_schema_ready_true_after_migration(pool) -> None:
    assert await pool.schema_ready() is True
    assert await pool.health_check() is True


# ---- B1：时区 ----


async def test_detected_at_is_utc_not_session_local(pool) -> None:
    """写入的 aware datetime 必须落成 naive UTC。

    目标 PG 容器的会话时区是 Asia/Shanghai（compose 里设了 TZ）。若不固定会话时区，
    aware datetime 会被 dump 成 timestamptz 再按会话时区折算成 +08 的墙上时间，
    与别处写入的 UTC 值差 8 小时，且不报错。
    """
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    records = PGRecordStore(pool)
    await records.write_or_append("default", _record(detected_at=datetime.now(timezone.utc)))
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    row = await pool.fetchone("SELECT detected_at FROM problem_record WHERE tenant_id=%s", ("default",))
    assert row is not None
    stored = row[0]
    assert stored.tzinfo is None, "列是 TIMESTAMP(3)，读回应当是 naive"
    assert before - timedelta(seconds=5) <= stored <= after + timedelta(seconds=5), (
        f"detected_at={stored} 不在 [{before}, {after}] 内——多半是按会话时区（Asia/Shanghai）折算了"
    )


async def test_round_and_record_timestamps_agree(pool) -> None:
    """同一轮的 detection_round 与 problem_record 时间必须一致。

    这两条写入路径传的参数类型不同（rounds 走 _iso() 字符串、records 走 datetime 对象），
    在 PG 下字符串走 unknown OID 会被原样写入、aware datetime 走 timestamptz 会被会话时区
    换算——只固定其中一条路径就会让同一个 trace_id 出现相差 8 小时的两个时间戳。
    """
    now = datetime.now(timezone.utc)
    await PGRoundStore(pool).create_round(
        "default", "R-0001", "application", started_at=now, target_ids=["MT-0001"]
    )
    await PGRecordStore(pool).write_or_append("default", _record(detected_at=now))

    r = await pool.fetchone("SELECT started_at FROM detection_round WHERE round_id=%s", ("R-0001",))
    p = await pool.fetchone("SELECT detected_at FROM problem_record WHERE tenant_id=%s", ("default",))
    assert r is not None and p is not None
    delta = abs((r[0] - p[0]).total_seconds())
    assert delta < 1, f"round.started_at={r[0]} 与 record.detected_at={p[0]} 差了 {delta}s"


# ---- B6：fpr 整数除法 ----


async def test_fpr_is_fractional_not_integer_division(pool) -> None:
    """fpr 必须是小数。

    PG 的 ``bigint / bigint`` 是整数除法（MySQL 的 ``/`` 恒返回 DECIMAL），漏掉 ``::numeric``
    会让 fpr 恒为 0 或 1 —— 不报错，但 L3 的误报率闸门按错误的值判定。
    1/3 是刻意选的：整数除法下会变成 0，纯小数下是 0.3333。
    """
    fpr = PGDynamicConfigStore(pool)
    for fp in (True, False, False):
        await fpr.write_fpr("default", "gk-1", false_positive=fp)

    row = await pool.fetchone("SELECT false_positive_cnt, total_cnt, fpr FROM fpr_table WHERE tenant_id=%s", ("default",))
    assert row is not None
    fp_cnt, total, value = row
    assert (fp_cnt, total) == (1, 3)
    assert float(value) == pytest.approx(1 / 3, abs=1e-4), f"fpr={value} 像是被整数除法截断了"


# ---- 去重 / evidence / 严重度 ----


async def test_open_group_key_dedup_appends_evidence(pool) -> None:
    """同 group_key 重复写入：只产生一条，occurrence_count 递增，evidence 追加而非覆盖。"""
    records = PGRecordStore(pool)
    await records.write_or_append("default", _record(evidence=[{"n": 1}]))
    await records.write_or_append("default", _record(record_id="PR-20260917-0002", evidence=[{"n": 2}]))

    rows = await pool.fetchall("SELECT record_id, occurrence_count, evidence FROM problem_record WHERE tenant_id=%s", ("default",))
    assert len(rows) == 1, "同 group_key 应当只产生一条（open_group_key 唯一约束）"
    record_id, occ, evidence = rows[0]
    assert occ == 2
    assert [e["n"] for e in evidence] == [1, 2], f"evidence 应追加，实际 {evidence}"


async def test_severity_escalates_but_never_de_escalates(pool) -> None:
    """severity 只升级不降级（FIELD → array_position 的语义等价性）。"""
    records = PGRecordStore(pool)
    await records.write_or_append("default", _record(severity="critical"))
    await records.write_or_append("default", _record(record_id="PR-20260917-0002", severity="warning"))

    row = await pool.fetchone("SELECT severity FROM problem_record WHERE tenant_id=%s", ("default",))
    assert row is not None and row[0] == "critical"


async def test_severity_escalates_when_stored_value_is_out_of_vocabulary(pool) -> None:
    """已有行的 severity 不在词表内时，新值仍应被采纳。

    这是 FIELD → array_position 唯一不等价的一格：array_position 未命中返回 NULL（FIELD 返 0），
    不 COALESCE 的话 ``3 > NULL`` 得 NULL → 走 ELSE → 保留旧值，而 MySQL 会升级。
    """
    records = PGRecordStore(pool)
    await records.write_or_append("default", _record(severity="medium"))  # 词表外的值
    await records.write_or_append("default", _record(record_id="PR-20260917-0002", severity="critical"))

    row = await pool.fetchone("SELECT severity FROM problem_record WHERE tenant_id=%s", ("default",))
    assert row is not None and row[0] == "critical"


async def test_change_related_bool_binds_to_smallint(pool) -> None:
    """Python bool 写 SMALLINT 列不能报错（psycopg 会把 bool dump 成 boolean OID）。"""
    await PGRecordStore(pool).write_or_append("default", _record(change_related=True))
    row = await pool.fetchone("SELECT change_related FROM problem_record WHERE tenant_id=%s", ("default",))
    assert row is not None and row[0] == 1


# ---- B5：jsonb_set 路径 ----


async def test_sweep_increments_miss_rounds(pool) -> None:
    """miss_rounds 必须真的自增。

    jsonb_set 的路径写成 MySQL 的 '$.miss_rounds' 不会报错——它会新建一个名为
    "$.miss_rounds" 的键，于是计数器永远不动，reconcile 的自动关单静默失效。
    """
    state = PGDetectionStateStore(pool)
    now = datetime.now(timezone.utc)
    await state.upsert("default", "application", "k1", consecutive_rounds=1, miss_rounds=0, first_seen=now, last_seen=now)
    await state.sweep("default", "application", seen_keys=set())
    await state.sweep("default", "application", seen_keys=set())

    row = await pool.fetchone("SELECT state_value FROM detection_state WHERE tenant_id=%s AND state_key=%s", ("default", "k1"))
    assert row is not None
    value = row[0]
    assert value["miss_rounds"] == 2, f"miss_rounds 未自增：{value}"
    assert "$.miss_rounds" not in value, "路径写成了 MySQL 形态，建出了一个名为 $.miss_rounds 的键"


# ---- C1：租约互斥 ----


async def test_lease_guard_prevents_stealing_live_lease(pool) -> None:
    """A 持有未过期租约时，B 的 try_acquire 必须失败。"""
    leases = PGLeaseStore(pool)
    assert await leases.try_acquire("scheduler", "replica-A", 60) is True
    assert await leases.try_acquire("scheduler", "replica-B", 60) is False, "抢走了活跃租约"

    # A 能续约，B 不能
    assert await leases.renew("scheduler", "replica-A", 60) is True
    assert await leases.renew("scheduler", "replica-B", 60) is False

    # 过期后可接管
    assert await leases.try_acquire("scheduler", "replica-B", 0.05) is False  # 仍归 A
    row = await pool.fetchone("SELECT holder FROM scheduler_lease WHERE lease_name=%s", ("scheduler",))
    assert row is not None and row[0] == "replica-A"


async def test_lease_expired_can_be_taken_over(pool) -> None:
    leases = PGLeaseStore(pool)
    assert await leases.try_acquire("scheduler", "replica-A", -1) is True  # 立刻过期
    assert await leases.try_acquire("scheduler", "replica-B", 60) is True, "过期租约应当可接管"
    row = await pool.fetchone("SELECT holder FROM scheduler_lease WHERE lease_name=%s", ("scheduler",))
    assert row is not None and row[0] == "replica-B"


# ---- 取号 ----


async def test_sequence_next_id_increments_without_duplicates(pool) -> None:
    seq = PGSequenceStore(pool)
    ids = [await seq.next_id("application") for _ in range(3)]
    assert ids == ["PR-20260917-0001", "PR-20260917-0002", "PR-20260917-0003"] or len(set(ids)) == 3


async def test_sequence_first_id_of_day_is_one(pool) -> None:
    """当天首次取号必须是 0001。

    MySQL 版的 ``LAST_INSERT_ID()`` 在首次插入时返回**连接的上一个** LAST_INSERT_ID
    （池化连接下通常非 0），会产出 -0000 或重号；PG 的 RETURNING 恒为 1。
    """
    first = await PGSequenceStore(pool).next_id("application")
    assert first.endswith("-0001"), first


# ---- 其它 store 的真实读写 ----


async def test_watermark_upsert_roundtrip(pool) -> None:
    wm = PGWatermarkStore(pool)
    ts = datetime(2026, 9, 17, 3, 0, 0)
    await wm.update("default", "MT-0001", ts)
    got = await wm.get("default", "MT-0001")
    assert got is not None and got["last_ts"] == ts
    # 覆盖写
    ts2 = ts + timedelta(minutes=5)
    await wm.update("default", "MT-0001", ts2)
    got2 = await wm.get("default", "MT-0001")
    assert got2 is not None and got2["last_ts"] == ts2


async def test_domain_config_upsert_bumps_version(pool) -> None:
    from aiops_apm.models.config import DomainConfig

    cfg = PGDomainConfigStore(pool)
    v1 = await cfg.upsert("default", "application", DomainConfig(detectors=[]))
    v2 = await cfg.upsert("default", "application", DomainConfig(detectors=[]))
    assert (v1, v2) == (1, 2), "version 应递增（不能写成 EXCLUDED.version + 1，那会恒为 2）"


async def test_blacklist_roundtrip_with_quoted_signal_column(pool) -> None:
    dyn = PGDynamicConfigStore(pool)
    created = await dyn.create_blacklist("default", {"service": "svc-a", "signal": "cpu", "reason": "noise"})
    assert created["id"] is not None
    rows = await dyn.load_blacklist("default")
    assert rows and rows[0]["signal"] == "cpu"


async def test_snapshot_accepts_long_signature(pool) -> None:
    """V7 加宽后，超过 255 字符的堆栈签名要能写入（MySQL 下曾报 1406 → 采集降级）。"""
    snaps = PGSnapshotStore(pool)
    long_sig = "com.example." + "x" * 400
    await snaps.write(
        "default",
        "MT-0001",
        [LogSignal(service="svc-a", level="ERROR", message="boom", timestamp=datetime.now(timezone.utc), signature=long_sig)],
    )
    row = await pool.fetchone("SELECT signature FROM signal_snapshot WHERE tenant_id=%s", ("default",))
    assert row is not None and row[0] == long_sig


async def test_monitor_target_create_assigns_sequential_ids(pool) -> None:
    targets = PGMonitorTargetStore(pool)
    a = await targets.create("default", {
        "service": "svc-a", "signal_type": "metric", "source_type": "http",
        "source_config": {"url": "http://x/metrics"}, "schedule": {"interval_sec": 60},
    })
    b = await targets.create("default", {
        "service": "svc-b", "signal_type": "log", "source_type": "http",
        "source_config": {"url": "http://x/logs"}, "schedule": {"interval_sec": 60},
    })
    # V9 已经种了 MT-0001..MT-0003（三个测试床日志端点），所以新建从 MT-0004 起。
    # 这同时验证了取号确实接在已有最大号之后（advisory lock 护住的「读最大号 + 插入」）。
    assert (a["target_id"], b["target_id"]) == ("MT-0004", "MT-0005")


# ---- E2：search_path 覆盖每条连接 ----


async def test_search_path_applies_to_every_pooled_connection(pool) -> None:
    """search_path 必须在每条连接上都生效。

    走 pool kwargs 而不是 open() 后 SET：psycopg_pool 懒建连接，后建的会拿不到设置，
    症状是部分请求报 42P01 或**静默命中 public 里的同名表**。并发取多条连接逐一验证。
    """
    import asyncio

    async def current_schema() -> str:
        row = await pool.fetchone("SELECT current_schema()")
        return row[0] if row else ""

    schemas = await asyncio.gather(*[current_schema() for _ in range(12)])
    assert set(schemas) == {SCHEMA}, f"有连接没拿到 search_path：{set(schemas)}"


async def test_session_timezone_is_utc(pool) -> None:
    """会话时区固定为 UTC，DB 生成的 CURRENT_TIMESTAMP 才与写入的 naive UTC 对齐。"""
    row = await pool.fetchone("SHOW TimeZone")
    assert row is not None and row[0] == "UTC"
