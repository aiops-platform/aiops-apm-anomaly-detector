"""运行时动态配置读取：维护窗口 / 黑名单 / 误报率 / 变更记录（设计 §9）。

- ``DynamicConfigStore``（ABC）：M5 ``build_context`` 每轮从表载入四类动态配置。
- ``InMemoryDynamicConfigStore``：单测/demo 真源（``seed_*`` 预置行）。
- ``MySQLDynamicConfigStore``：生产实现，按租户过滤、只取 enabled 黑名单。

每方法入口校验 ``tenant_id`` 非空（多租户隔离硬约束）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .connection import ConnectionPool


class DynamicConfigStore(ABC):
    """四类动态配置的只读接口（写表 API 属 M6 admin）。"""

    @abstractmethod
    async def load_maintenance_windows(self, tenant_id: str) -> list[dict]:
        """``maintenance_window`` 行 ``{service, start_at, end_at, reason}``（datetime 保持）。"""

    @abstractmethod
    async def load_blacklist(self, tenant_id: str) -> list[dict]:
        """``suppress_blacklist`` 行（enabled=1）``{domain, service, signal, reason}``。"""

    @abstractmethod
    async def load_fpr(self, tenant_id: str) -> dict:
        """``fpr_table`` → ``{group_key: {"fpr": float, "total": int}}``。"""

    @abstractmethod
    async def load_changes(self, tenant_id: str) -> list[dict]:
        """``change_record`` 行 ``{change_id, service, type, summary, changed_at}``。"""


class InMemoryDynamicConfigStore(DynamicConfigStore):
    def __init__(self) -> None:
        self._maintenance_windows: dict[str, list[dict]] = {}
        self._blacklist: dict[str, list[dict]] = {}
        self._fpr: dict[str, dict] = {}
        self._changes: dict[str, list[dict]] = {}

    def seed_maintenance_windows(self, tenant_id: str, rows: list[dict]) -> None:
        self._maintenance_windows[tenant_id] = list(rows)

    def seed_blacklist(self, tenant_id: str, rows: list[dict]) -> None:
        self._blacklist[tenant_id] = list(rows)

    def seed_fpr(self, tenant_id: str, rows: dict) -> None:
        self._fpr[tenant_id] = dict(rows)

    def seed_changes(self, tenant_id: str, rows: list[dict]) -> None:
        self._changes[tenant_id] = list(rows)

    async def load_maintenance_windows(self, tenant_id: str) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return [dict(r) for r in self._maintenance_windows.get(tenant_id, [])]

    async def load_blacklist(self, tenant_id: str) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return [dict(r) for r in self._blacklist.get(tenant_id, []) if r.get("enabled", True)]

    async def load_fpr(self, tenant_id: str) -> dict:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return {k: dict(v) for k, v in self._fpr.get(tenant_id, {}).items()}

    async def load_changes(self, tenant_id: str) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return [dict(r) for r in self._changes.get(tenant_id, [])]


class MySQLDynamicConfigStore(DynamicConfigStore):
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def load_maintenance_windows(self, tenant_id: str) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = await self._pool.fetchall(
            "SELECT service, start_at, end_at, reason FROM maintenance_window WHERE tenant_id=%s", (tenant_id,)
        )
        return [{"service": r[0], "start_at": r[1], "end_at": r[2], "reason": r[3]} for r in rows]

    async def load_blacklist(self, tenant_id: str) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = await self._pool.fetchall(
            "SELECT domain, service, `signal`, reason FROM suppress_blacklist "
            "WHERE tenant_id=%s AND enabled=1",
            (tenant_id,),
        )
        return [{"domain": r[0], "service": r[1], "signal": r[2], "reason": r[3]} for r in rows]

    async def load_fpr(self, tenant_id: str) -> dict:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = await self._pool.fetchall(
            "SELECT group_key, fpr, total_cnt FROM fpr_table WHERE tenant_id=%s", (tenant_id,)
        )
        return {r[0]: {"fpr": float(r[1]), "total": int(r[2])} for r in rows}

    async def load_changes(self, tenant_id: str) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = await self._pool.fetchall(
            "SELECT change_id, service, type, summary, changed_at FROM change_record WHERE tenant_id=%s",
            (tenant_id,),
        )
        return [{"change_id": r[0], "service": r[1], "type": r[2], "summary": r[3], "changed_at": r[4]} for r in rows]
