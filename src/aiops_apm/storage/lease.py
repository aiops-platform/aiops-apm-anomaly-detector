"""多副本调度租约：``scheduler_lease`` 表（行锁 + TTL 续约 + 崩溃自动接管）。

- ``LeaseStore``（ABC）：M6 scheduler 选主（UC-6.9）。
- ``InMemoryLeaseStore``：单测/demo 真源（``now`` 可注入以测过期接管）。
- ``MySQLLeaseStore``：生产实现，``INSERT ... ON DUPLICATE KEY UPDATE`` 原子接管，
  续约用 ``WHERE holder=? AND expires_at > NOW(3)`` 防续到已失效的租约。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .connection import ConnectionPool


class LeaseStore(ABC):
    """以 ``lease_name`` 为键的租约读写接口。"""

    @abstractmethod
    async def try_acquire(self, lease_name: str, holder: str, ttl_sec: float) -> bool:
        """尝试获取租约；已被他人持有且未过期 → False（其他实例可稍后重试）。"""

    @abstractmethod
    async def renew(self, lease_name: str, holder: str, ttl_sec: float) -> bool:
        """续约；租约已失效或 holder 不符 → False。"""

    @abstractmethod
    async def release(self, lease_name: str, holder: str) -> None:
        """主动释放（仅 holder 匹配时生效）。"""


class InMemoryLeaseStore(LeaseStore):
    """内存实现：单测与本地 demo 真源（``now`` 可注入以测过期接管）。"""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._leases: dict[str, dict[str, Any]] = {}

    async def try_acquire(self, lease_name: str, holder: str, ttl_sec: float) -> bool:
        now = self._now()
        entry = self._leases.get(lease_name)
        if entry is not None and entry["expires_at"] > now and entry["holder"] != holder:
            return False  # 他人持有且未过期
        self._leases[lease_name] = {"holder": holder, "expires_at": now + timedelta(seconds=ttl_sec)}
        return True

    async def renew(self, lease_name: str, holder: str, ttl_sec: float) -> bool:
        now = self._now()
        entry = self._leases.get(lease_name)
        if entry is None or entry["holder"] != holder or entry["expires_at"] <= now:
            return False
        entry["expires_at"] = now + timedelta(seconds=ttl_sec)
        return True

    async def release(self, lease_name: str, holder: str) -> None:
        entry = self._leases.get(lease_name)
        if entry is not None and entry["holder"] == holder:
            self._leases.pop(lease_name, None)

    def holder(self, lease_name: str) -> str | None:
        """测试辅助：当前租约持有者。"""
        entry = self._leases.get(lease_name)
        return entry["holder"] if entry is not None else None


class MySQLLeaseStore(LeaseStore):
    """MySQL 实现：单 handle 内原子接管 / 续约。"""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def try_acquire(self, lease_name: str, holder: str, ttl_sec: float) -> bool:
        # 单 handle 内完成 INSERT + SELECT（LAST_INSERT_ID 连接作用域同 handle）。
        handle = await self._pool.acquire()
        try:
            await handle.execute_affected(
                "INSERT INTO scheduler_lease (lease_name, holder, acquired_at, expires_at) "
                "VALUES (%s, %s, NOW(3), DATE_ADD(NOW(3), INTERVAL %s SECOND)) "
                "ON DUPLICATE KEY UPDATE "
                "holder = IF(expires_at < NOW(3), VALUES(holder), holder), "
                "expires_at = IF(expires_at < NOW(3), VALUES(expires_at), expires_at)",
                (lease_name, holder, ttl_sec),
            )
            row = await handle.fetchone(
                "SELECT holder FROM scheduler_lease WHERE lease_name=%s", (lease_name,)
            )
            await handle.commit()
        finally:
            await self._pool.release(handle)
        return row is not None and row[0] == holder

    async def renew(self, lease_name: str, holder: str, ttl_sec: float) -> bool:
        affected = await self._pool.execute_affected(
            "UPDATE scheduler_lease SET expires_at = DATE_ADD(NOW(3), INTERVAL %s SECOND) "
            "WHERE lease_name=%s AND holder=%s AND expires_at > NOW(3)",
            (ttl_sec, lease_name, holder),
        )
        return affected == 1

    async def release(self, lease_name: str, holder: str) -> None:
        await self._pool.execute(
            "DELETE FROM scheduler_lease WHERE lease_name=%s AND holder=%s", (lease_name, holder)
        )
