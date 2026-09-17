"""多副本调度租约：``scheduler_lease`` 表（行锁 + TTL 续约 + 崩溃自动接管）。

- ``LeaseStore``（ABC）：M6 scheduler 选主（UC-6.9）。
- ``InMemoryLeaseStore``：单测/demo 真源（``now`` 可注入以测过期接管）。
- ``PGLeaseStore``：生产实现，``INSERT ... ON CONFLICT DO UPDATE`` 原子接管，
  续约用 ``WHERE holder=%s AND expires_at > CURRENT_TIMESTAMP(3)`` 防续到已失效的租约。
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


class PGLeaseStore(LeaseStore):
    """PostgreSQL 实现：单语句原子接管 / 续约。"""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def try_acquire(self, lease_name: str, holder: str, ttl_sec: float) -> bool:
        # 单语句完成「插入或按过期条件接管」并 RETURNING 出最终 holder —— 比 MySQL 版
        # 「ON DUPLICATE KEY UPDATE 再补一条 SELECT」少一次往返。
        #
        # ⚠️ 那两个 CASE 是租约语义的核心，不能简化成直白的 `holder = EXCLUDED.holder`：
        # 只有当现有租约**已过期**时才允许换主，否则保留原 holder。丢掉守卫会让第二个副本
        # 抢走活跃租约，且 try_acquire 对双方都返回 True —— 恰是互斥的反面。
        #
        # PG 的 DO UPDATE 里，SET 右侧引用表名（scheduler_lease.x）取的是**更新前**的值，
        # EXCLUDED.x 才是本次待插入的值；各 SET 子句之间没有先后依赖（与 MySQL 的
        # ON DUPLICATE KEY UPDATE 逐个赋值、后者能看见前者修改不同）。
        row = await self._pool.fetchone(
            "INSERT INTO scheduler_lease (lease_name, holder, acquired_at, expires_at) "
            "VALUES (%s, %s, CURRENT_TIMESTAMP(3), "
            "CURRENT_TIMESTAMP(3) + make_interval(secs => %s)) "
            "ON CONFLICT (lease_name) DO UPDATE SET "
            "holder = CASE WHEN scheduler_lease.expires_at < CURRENT_TIMESTAMP(3) "
            "THEN EXCLUDED.holder ELSE scheduler_lease.holder END, "
            "expires_at = CASE WHEN scheduler_lease.expires_at < CURRENT_TIMESTAMP(3) "
            "THEN EXCLUDED.expires_at ELSE scheduler_lease.expires_at END "
            "RETURNING holder",
            (lease_name, holder, ttl_sec),
        )
        return row is not None and row[0] == holder

    async def renew(self, lease_name: str, holder: str, ttl_sec: float) -> bool:
        affected = await self._pool.execute_affected(
            "UPDATE scheduler_lease SET expires_at = CURRENT_TIMESTAMP(3) + make_interval(secs => %s) "
            "WHERE lease_name=%s AND holder=%s AND expires_at > CURRENT_TIMESTAMP(3)",
            (ttl_sec, lease_name, holder),
        )
        return affected == 1

    async def release(self, lease_name: str, holder: str) -> None:
        await self._pool.execute(
            "DELETE FROM scheduler_lease WHERE lease_name=%s AND holder=%s", (lease_name, holder)
        )
