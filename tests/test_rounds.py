"""UC-7.2 RoundStore：InMemory 真源 CRUD/过滤/排序/租户隔离 + MySQL SQL 断言。"""

from datetime import datetime, timezone

import pytest

from aiops_apm.storage.rounds import InMemoryRoundStore, MySQLRoundStore

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


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("create_round", ("", "R-1", "application"), {"started_at": TS1}),
        ("update_status", ("", "R-1", "success"), {"ended_at": TS2}),
        ("get_round", ("", "R-1"), {}),
        ("list_rounds", ("",), {}),
    ],
)
async def test_tenant_id_required(store: InMemoryRoundStore, method: str, args: tuple, kwargs: dict) -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        await getattr(store, method)(*args, **kwargs)


# ---- MySQL SQL 断言 ----
# MySQLRoundStore 直接调 ConnectionPool 的便捷方法（execute/fetchone/fetchall 自动 acquire→commit→release），
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


def _store(logs: list) -> MySQLRoundStore:
    return MySQLRoundStore(FakePool(logs))


async def test_mysql_create_round_sql() -> None:
    logs: list = []
    await _store(logs).create_round("t1", "R-0001", "application", started_at=TS1, target_ids=["MT-0001"])
    sql = next(s for (kind, s, _) in logs if kind == "execute")
    assert sql.startswith("INSERT INTO detection_round")
    assert "target_ids" in sql
    assert "timeline" in sql


async def test_mysql_update_status_sql() -> None:
    logs: list = []
    await _store(logs).update_status("t1", "R-0001", "success", ended_at=TS2, timeline=[{"step": "x"}], record_count=1)
    sql = next(s for (kind, s, _) in logs if kind == "execute")
    assert "UPDATE detection_round SET status=%s, finished_at=%s, timeline=%s, record_count=%s" in sql
    assert "WHERE tenant_id=%s AND round_id=%s" in sql


async def test_mysql_list_rounds_sql_with_filters() -> None:
    logs: list = []
    await _store(logs).list_rounds("t1", domain="application", status="success", limit=10, offset=5)
    sql = next(s for (kind, s, _) in logs if kind == "fetchall")
    assert "FROM detection_round WHERE tenant_id=%s" in sql
    assert "AND domain=%s" in sql
    assert "AND status=%s" in sql
    assert "ORDER BY started_at DESC LIMIT %s OFFSET %s" in sql
