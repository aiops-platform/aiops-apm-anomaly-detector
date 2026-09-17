"""UC-7.2 RoundStore：InMemory 真源 CRUD/过滤/排序/租户隔离 + PG SQL 断言。"""

from datetime import datetime, timezone

import pytest
from psycopg.types.json import Jsonb

from aiops_apm.storage.rounds import InMemoryRoundStore, PGRoundStore

TS1 = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
TS2 = datetime(2026, 8, 26, 12, 1, 0, tzinfo=timezone.utc)
TS3 = datetime(2026, 8, 26, 12, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def store() -> InMemoryRoundStore:
    return InMemoryRoundStore()


async def _seed(store: InMemoryRoundStore) -> None:
    await store.create_round("t1", "R-0001", "application", started_at=TS1, target_ids=["MT-0001"])
    await store.create_round("t1", "R-0002", "application", started_at=TS2, target_ids=["MT-0002"])
    await store.create_round("t1", "R-0003", "orders", started_at=TS3, target_ids=["MT-0003"])
    await store.update_status("t1", "R-0001", "success", ended_at=TS2)
    await store.update_status("t1", "R-0002", "partial", ended_at=TS2, degraded_sources=["MT-0002"])


async def test_create_and_get(store: InMemoryRoundStore) -> None:
    await store.create_round("t1", "R-0001", "application", started_at=TS1, target_ids=["MT-0001"])
    row = await store.get_round("t1", "R-0001")
    assert row is not None
    assert row["status"] == "running"
    assert row["domain"] == "application"
    assert row["target_ids"] == ["MT-0001"]
    assert row["timeline"] == []
    assert row["finished_at"] is None


async def test_update_status(store: InMemoryRoundStore) -> None:
    await store.create_round("t1", "R-0001", "application", started_at=TS1)
    await store.update_status(
        "t1", "R-0001", "success", ended_at=TS2,
        timeline=[{"step": "suppressed", "count": 1}], signals_count=5, record_count=2,
    )
    row = await store.get_round("t1", "R-0001")
    assert row["status"] == "success"
    assert row["finished_at"] == TS2
    assert row["signals_count"] == 5
    assert row["record_count"] == 2
    assert row["timeline"][0]["count"] == 1


async def test_timeline_datetimes_serialized(store: InMemoryRoundStore) -> None:
    # runner timeline 各 step 带 "ts": datetime → _json_safe 转 isoformat，保证可 JSON 化
    await store.create_round("t1", "R-0001", "application", started_at=TS1)
    await store.update_status(
        "t1", "R-0001", "success", ended_at=TS2,
        timeline=[{"step": "collect_done", "ts": TS1, "count": 1}],
    )
    row = await store.get_round("t1", "R-0001")
    assert row["timeline"][0]["ts"] == TS1.isoformat()


async def test_list_rounds_sorted_desc(store: InMemoryRoundStore) -> None:
    await _seed(store)
    rows = await store.list_rounds("t1")
    # started_at 倒序：R-0003 (TS3) > R-0002 (TS2) > R-0001 (TS1)
    assert [r["round_id"] for r in rows] == ["R-0003", "R-0002", "R-0001"]


async def test_list_rounds_filters(store: InMemoryRoundStore) -> None:
    await _seed(store)
    assert len(await store.list_rounds("t1", domain="application")) == 2
    assert len(await store.list_rounds("t1", domain="orders")) == 1
    assert len(await store.list_rounds("t1", status="partial")) == 1
    assert len(await store.list_rounds("t1", status="running")) == 1  # R-0003
    assert len(await store.list_rounds("t1", limit=2)) == 2
    assert len(await store.list_rounds("t1", limit=1, offset=1)) == 1


async def test_list_rounds_tenant_isolated(store: InMemoryRoundStore) -> None:
    await _seed(store)
    await store.create_round("t2", "R-0009", "application", started_at=TS1)
    assert len(await store.list_rounds("t1")) == 3
    assert len(await store.list_rounds("t2")) == 1
    assert await store.get_round("t2", "R-0001") is None  # 租户隔离


# ---- detection_round_target 子表：round → target 一对多明细 ----


async def test_create_target_and_latest(store: InMemoryRoundStore) -> None:
    await store.create_target("t1", "R-0001", "MT-0001", started_at=TS1)
    row = await store.latest_target("t1", "MT-0001")
    assert row is not None
    assert row["round_id"] == "R-0001"
    assert row["status"] == "running"
    assert row["signals_count"] == 0
    assert row["anomaly_count"] == 0
    assert row["record_count"] == 0
    assert row["suppressed_count"] == 0
    assert row["finished_at"] is None
    # 同一 target 多轮 → latest 取 started_at 最新的
    await store.create_target("t1", "R-0002", "MT-0001", started_at=TS2)
    assert (await store.latest_target("t1", "MT-0001"))["round_id"] == "R-0002"


async def test_update_target_status_counts_only(store: InMemoryRoundStore) -> None:
    # V6 漏斗后回填：status=None → 只更新计数字段，不动采集状态/时间
    await store.create_target("t1", "R-0001", "MT-0001", started_at=TS1)
    await store.update_target_status("t1", "R-0001", "MT-0001", "ok", finished_at=TS2, signals_count=5)
    await store.update_target_status(
        "t1", "R-0001", "MT-0001",
        anomaly_count=2, record_count=1, suppressed_count=3,
    )
    row = await store.latest_target("t1", "MT-0001")
    assert row["status"] == "ok"  # 未覆盖
    assert row["finished_at"] == TS2  # 未覆盖
    assert row["signals_count"] == 5  # 未覆盖
    assert row["anomaly_count"] == 2
    assert row["record_count"] == 1
    assert row["suppressed_count"] == 3


async def test_update_target_status(store: InMemoryRoundStore) -> None:
    await store.create_target("t1", "R-0001", "MT-0001", started_at=TS1)
    await store.update_target_status(
        "t1", "R-0001", "MT-0001", "failed", finished_at=TS2, signals_count=7, error="boom"
    )
    row = await store.latest_target("t1", "MT-0001")
    assert row["status"] == "failed"
    assert row["finished_at"] == TS2
    assert row["signals_count"] == 7
    assert row["error"] == "boom"


async def test_update_target_status_request_params(store: InMemoryRoundStore) -> None:
    # V8：本轮实际下发的出站请求参数（时间窗口/水位线/时区转换后的最终 params）
    await store.create_target("t1", "R-0001", "MT-0001", started_at=TS1)
    assert (await store.latest_target("t1", "MT-0001"))["request_params"] is None
    await store.update_target_status(
        "t1", "R-0001", "MT-0001", "ok",
        finished_at=TS2, signals_count=5,
        request_params={
            "method": "GET",
            "url": "https://elk.example.com:9200/logs/_search",
            "params": {"startTime": "2024-03-09T15:30:00.000Z", "endTime": "2024-03-09T16:00:00.000Z"},
        },
    )
    row = await store.latest_target("t1", "MT-0001")
    assert row["request_params"]["params"]["startTime"] == "2024-03-09T15:30:00.000Z"
    assert row["request_params"]["url"].startswith("https://elk")
    # 漏斗后回填（status=None）不覆盖 request_params
    await store.update_target_status("t1", "R-0001", "MT-0001", anomaly_count=1)
    assert (await store.latest_target("t1", "MT-0001"))["request_params"]["method"] == "GET"


async def test_latest_target_status_filter(store: InMemoryRoundStore) -> None:
    await store.create_target("t1", "R-0001", "MT-0001", started_at=TS1)
    await store.update_target_status("t1", "R-0001", "MT-0001", "interrupted", finished_at=TS2)
    await store.create_target("t1", "R-0002", "MT-0001", started_at=TS3)
    # 按 ok 过滤：最新 ok 无（本轮 running）→ None
    assert await store.latest_target("t1", "MT-0001", status="ok") is None
    assert (await store.latest_target("t1", "MT-0001", status="running"))["round_id"] == "R-0002"


async def test_targets_tenant_isolated(store: InMemoryRoundStore) -> None:
    await store.create_target("t1", "R-0001", "MT-0001", started_at=TS1)
    assert await store.latest_target("t2", "MT-0001") is None


async def test_list_targets_by_round(store: InMemoryRoundStore) -> None:
    await store.create_target("t1", "R-0001", "MT-0002", started_at=TS1)
    await store.create_target("t1", "R-0001", "MT-0001", started_at=TS1)
    await store.create_target("t1", "R-0002", "MT-0009", started_at=TS2)  # 别的轮
    rows = await store.list_targets("t1", "R-0001")
    assert [r["target_id"] for r in rows] == ["MT-0001", "MT-0002"]
    assert await store.list_targets("t2", "R-0001") == []  # 租户隔离


async def test_latest_target_no_rows_returns_none(store: InMemoryRoundStore) -> None:
    assert await store.latest_target("t1", "MT-NONE") is None


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("create_round", ("", "R-1", "application"), {"started_at": TS1}),
        ("update_status", ("", "R-1", "success"), {"ended_at": TS2}),
        ("get_round", ("", "R-1"), {}),
        ("list_rounds", ("",), {}),
        ("create_target", ("", "R-1", "MT-1"), {"started_at": TS1}),
        ("update_target_status", ("", "R-1", "MT-1", "ok"), {"finished_at": TS2}),
        ("latest_target", ("", "MT-1"), {}),
        ("list_targets", ("", "R-1"), {}),
    ],
)
async def test_tenant_id_required(store: InMemoryRoundStore, method: str, args: tuple, kwargs: dict) -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        await getattr(store, method)(*args, **kwargs)


# ---- PostgreSQL SQL 断言 ----
# PGRoundStore 直接调 ConnectionPool 的便捷方法（execute/fetchone/fetchall 自动 acquire→commit→release），
# FakePool 同样提供这些便捷方法并把 SQL/args 记入 logs。

class FakePool:
    def __init__(self, logs: list) -> None:
        self._logs = logs

    async def execute(self, sql: str, args: tuple = ()) -> None:
        self._logs.append(("execute", sql, args))

    async def fetchone(self, sql: str, args: tuple = ()) -> tuple | None:
        self._logs.append(("fetchone", sql, args))
        return None

    async def fetchall(self, sql: str, args: tuple = ()) -> list:
        self._logs.append(("fetchall", sql, args))
        return []


def _store(logs: list) -> PGRoundStore:
    return PGRoundStore(FakePool(logs))


async def test_pg_create_round_sql() -> None:
    logs: list = []
    await _store(logs).create_round("t1", "R-0001", "application", started_at=TS1, target_ids=["MT-0001"])
    sql = next(s for (kind, s, _) in logs if kind == "execute")
    assert sql.startswith("INSERT INTO detection_round")
    assert "target_ids" in sql
    assert "timeline" in sql


async def test_pg_update_status_sql() -> None:
    logs: list = []
    await _store(logs).update_status("t1", "R-0001", "success", ended_at=TS2, timeline=[{"step": "x"}], record_count=1)
    sql = next(s for (kind, s, _) in logs if kind == "execute")
    assert "UPDATE detection_round SET status=%s, finished_at=%s, timeline=%s, record_count=%s" in sql
    assert "WHERE tenant_id=%s AND round_id=%s" in sql


async def test_pg_list_rounds_sql_with_filters() -> None:
    logs: list = []
    await _store(logs).list_rounds("t1", domain="application", status="success", limit=10, offset=5)
    sql = next(s for (kind, s, _) in logs if kind == "fetchall")
    assert "FROM detection_round WHERE tenant_id=%s" in sql
    assert "AND domain=%s" in sql
    assert "AND status=%s" in sql
    assert "ORDER BY started_at DESC LIMIT %s OFFSET %s" in sql


async def test_pg_create_target_sql() -> None:
    logs: list = []
    await _store(logs).create_target("t1", "R-0001", "MT-0001", started_at=TS1)
    sql = next(s for (kind, s, _) in logs if kind == "execute")
    assert sql.startswith("INSERT INTO detection_round_target")
    assert "'running'" in sql
    assert "started_at" in sql


async def test_pg_update_target_status_sql() -> None:
    logs: list = []
    await _store(logs).update_target_status(
        "t1", "R-0001", "MT-0001", "failed", finished_at=TS2, signals_count=3, error="boom"
    )
    sql = next(s for (kind, s, _) in logs if kind == "execute")
    assert "UPDATE detection_round_target SET status=%s, finished_at=%s, signals_count=%s, error=%s" in sql
    assert "WHERE round_id=%s AND tenant_id=%s AND target_id=%s" in sql


async def test_pg_update_target_status_request_params_sql() -> None:
    # V8：request_params JSON 列，args 为 _as_json 包出的 psycopg Jsonb
    logs: list = []
    await _store(logs).update_target_status(
        "t1", "R-0001", "MT-0001", "ok", finished_at=TS2, signals_count=3,
        request_params={"method": "GET", "url": "http://src/query", "params": {"start": "2024-03-09T15:30:00.000Z"}},
    )
    kind, sql, args = logs[0]
    assert kind == "execute"
    assert "SET status=%s, finished_at=%s, signals_count=%s, request_params=%s" in sql
    assert "WHERE round_id=%s AND tenant_id=%s AND target_id=%s" in sql
    # _as_json 返回 Jsonb（psycopg 的 JSONB 参数包装器）而非 str——裸 dict 没有 dumper，
    # 必须包一层；Jsonb 构造时不序列化，dumps 钩子在真正绑定参数那一刻才跑。
    assert isinstance(args[3], Jsonb)
    assert args[3].obj["params"]["start"] == "2024-03-09T15:30:00.000Z"


async def test_pg_update_target_status_counts_only_sql() -> None:
    # V6 漏斗后回填：只有计数字段 → SET 只含这三个列，status/finished_at 不出现
    logs: list = []
    await _store(logs).update_target_status(
        "t1", "R-0001", "MT-0001",
        anomaly_count=2, record_count=1, suppressed_count=3,
    )
    sql = next(s for (kind, s, _) in logs if kind == "execute")
    assert (
        "SET anomaly_count=%s, record_count=%s, suppressed_count=%s" in sql
    )
    assert "status=" not in sql
    assert "finished_at=" not in sql
    assert "WHERE round_id=%s AND tenant_id=%s AND target_id=%s" in sql


async def test_pg_latest_target_sql() -> None:
    logs: list = []
    await _store(logs).latest_target("t1", "MT-0001", status="running")
    sql = next(s for (kind, s, _) in logs if kind == "fetchone")
    assert "FROM detection_round_target WHERE tenant_id=%s AND target_id=%s" in sql
    assert "AND status=%s" in sql
    assert "ORDER BY started_at DESC LIMIT 1" in sql


async def test_pg_list_targets_sql() -> None:
    logs: list = []
    await _store(logs).list_targets("t1", "R-0001")
    sql = next(s for (kind, s, _) in logs if kind == "fetchall")
    assert "FROM detection_round_target WHERE tenant_id=%s AND round_id=%s" in sql
    assert "ORDER BY target_id" in sql
