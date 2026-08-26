"""M6 UC-6.9：LeaseStore 租约语义（InMemory 真源 + MySQL SQL 原子接管断言）。"""

from datetime import datetime, timedelta, timezone

from aiops_apm.storage.lease import InMemoryLeaseStore, MySQLLeaseStore

T0 = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def set(self, dt: datetime) -> None:
        self._now = dt

    def __call__(self) -> datetime:
        return self._now


# ---- InMemory：acquire / renew / release / 过期接管 ----


async def test_acquire_sets_holder() -> None:
    clock = FakeClock(T0)
    store = InMemoryLeaseStore(now=clock)
    assert await store.try_acquire("scheduler", "A", 30) is True
    assert store.holder("scheduler") == "A"
    # 同 holder 重复 acquire → 续期成功
    assert await store.try_acquire("scheduler", "A", 30) is True


async def test_acquire_blocked_by_active_other_holder() -> None:
    clock = FakeClock(T0)
    store = InMemoryLeaseStore(now=clock)
    await store.try_acquire("scheduler", "A", 30)
    assert await store.try_acquire("scheduler", "B", 30) is False
    assert store.holder("scheduler") == "A"


async def test_takeover_after_expiry() -> None:
    clock = FakeClock(T0)
    store = InMemoryLeaseStore(now=clock)
    await store.try_acquire("scheduler", "A", 30)
    clock.set(T0 + timedelta(seconds=31))  # 过期
    assert await store.try_acquire("scheduler", "B", 30) is True
    assert store.holder("scheduler") == "B"


async def test_renew_by_holder_extends() -> None:
    clock = FakeClock(T0)
    store = InMemoryLeaseStore(now=clock)
    await store.try_acquire("scheduler", "A", 30)
    clock.set(T0 + timedelta(seconds=20))
    assert await store.renew("scheduler", "A", 30) is True
    clock.set(T0 + timedelta(seconds=40))  # 若未续约此处已过期；续约后仍有效
    assert await store.try_acquire("scheduler", "B", 30) is False


async def test_renew_by_wrong_holder_fails() -> None:
    clock = FakeClock(T0)
    store = InMemoryLeaseStore(now=clock)
    await store.try_acquire("scheduler", "A", 30)
    assert await store.renew("scheduler", "B", 30) is False


async def test_renew_after_expiry_fails() -> None:
    clock = FakeClock(T0)
    store = InMemoryLeaseStore(now=clock)
    await store.try_acquire("scheduler", "A", 30)
    clock.set(T0 + timedelta(seconds=31))
    assert await store.renew("scheduler", "A", 30) is False


async def test_release_only_by_holder() -> None:
    clock = FakeClock(T0)
    store = InMemoryLeaseStore(now=clock)
    await store.try_acquire("scheduler", "A", 30)
    await store.release("scheduler", "B")  # 非 holder → no-op
    assert store.holder("scheduler") == "A"
    await store.release("scheduler", "A")
    assert store.holder("scheduler") is None
    # 释放后可被接管
    assert await store.try_acquire("scheduler", "B", 30) is True


# ---- MySQL：生成 SQL 原子接管断言（不连真库） ----


class FakeHandle:
    def __init__(self, logs: list) -> None:
        self._logs = logs

    async def execute_affected(self, sql: str, args: tuple) -> int:
        self._logs.append(("execute_affected", sql, args))
        return 1

    async def fetchone(self, sql: str, args: tuple):
        self._logs.append(("fetchone", sql, args))
        return ("sched-A",)

    async def commit(self) -> None:
        self._logs.append(("commit",))


class FakePool:
    def __init__(self, logs: list) -> None:
        self._logs = logs

    async def acquire(self):
        return FakeHandle(self._logs)

    async def release(self, handle) -> None:
        pass

    async def execute_affected(self, sql: str, args: tuple) -> int:
        self._logs.append(("pool_execute_affected", sql, args))
        return 1


async def test_mysql_acquire_generates_atomic_takeover_sql() -> None:
    logs: list = []
    store = MySQLLeaseStore(FakePool(logs))
    assert await store.try_acquire("scheduler", "sched-A", 30) is True

    acquire_sql = next(sql for (kind, sql, _) in logs if kind == "execute_affected")
    assert "INSERT INTO scheduler_lease" in acquire_sql
    assert "ON DUPLICATE KEY UPDATE" in acquire_sql
    # 原子接管：过期才换 holder / 续 expires_at
    assert "IF(expires_at < NOW(3), VALUES(holder), holder)" in acquire_sql
    assert "IF(expires_at < NOW(3), VALUES(expires_at), expires_at)" in acquire_sql
    # 单 handle 内 SELECT holder 确认接管成功
    assert any(kind == "fetchone" and "SELECT holder" in sql for (kind, sql, _) in logs)


async def test_mysql_renew_generates_holder_guarded_sql() -> None:
    logs: list = []
    store = MySQLLeaseStore(FakePool(logs))
    assert await store.renew("scheduler", "sched-A", 30) is True
    sql = logs[0][1]
    assert "UPDATE scheduler_lease" in sql
    assert "holder=%s" in sql
    assert "expires_at > NOW(3)" in sql
