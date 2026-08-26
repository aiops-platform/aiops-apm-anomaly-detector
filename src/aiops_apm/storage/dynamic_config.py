"""运行时动态配置读取：维护窗口 / 黑名单 / 误报率 / 变更记录（设计 §9）。

- ``DynamicConfigStore``（ABC）：M5 ``build_context`` 每轮从表载入四类动态配置。
- ``InMemoryDynamicConfigStore``：单测/demo 真源（``seed_*`` 预置行）。
- ``MySQLDynamicConfigStore``：生产实现，按租户过滤、只取 enabled 黑名单。

每方法入口校验 ``tenant_id`` 非空（多租户隔离硬约束）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

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

    # ---- M6 写接口（维护窗口 / 黑名单 admin CRUD，UC-6.10/6.11）----

    @abstractmethod
    async def create_maintenance_window(self, tenant_id: str, window: dict) -> dict:
        """新建维护窗口行，返回含 id 的行。"""

    @abstractmethod
    async def list_maintenance_windows(self, tenant_id: str, *, service: str | None = None) -> list[dict]:
        """列出维护窗口，可按 service 过滤。"""

    @abstractmethod
    async def update_maintenance_window(self, tenant_id: str, window_id: int, patch: dict) -> dict | None:
        """更新维护窗口字段，返回更新后的行；不存在返回 None。"""

    @abstractmethod
    async def delete_maintenance_window(self, tenant_id: str, window_id: int) -> None:
        """删除维护窗口行。"""

    @abstractmethod
    async def create_blacklist(self, tenant_id: str, entry: dict) -> dict:
        """新建黑名单行，返回含 id 的行。"""

    @abstractmethod
    async def list_blacklist(self, tenant_id: str) -> list[dict]:
        """列出黑名单（含 disabled 行，admin 全量视图）。"""

    @abstractmethod
    async def update_blacklist(self, tenant_id: str, entry_id: int, patch: dict) -> dict | None:
        """更新黑名单字段，返回更新后的行；不存在返回 None。"""

    @abstractmethod
    async def delete_blacklist(self, tenant_id: str, entry_id: int) -> None:
        """删除黑名单行。"""


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

    # ---- M6 写接口 ----

    def _add_row(self, store: dict[str, list[dict]], tenant_id: str, row: dict) -> None:
        rows = store.setdefault(tenant_id, [])
        rows.append(row)

    def _find_row(self, store: dict[str, list[dict]], tenant_id: str, row_id: int) -> dict | None:
        for r in store.get(tenant_id, []):
            if r["id"] == row_id:
                return r
        return None

    async def create_maintenance_window(self, tenant_id: str, window: dict) -> dict:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = self._maintenance_windows.setdefault(tenant_id, [])
        row_id = max((r["id"] for r in rows), default=0) + 1
        row = {
            "id": row_id,
            "tenant_id": tenant_id,
            "service": window["service"],
            "start_at": window["start_at"],
            "end_at": window["end_at"],
            "reason": window.get("reason"),
        }
        rows.append(row)
        return dict(row)

    async def list_maintenance_windows(self, tenant_id: str, *, service: str | None = None) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = self._maintenance_windows.get(tenant_id, [])
        if service is not None:
            rows = [r for r in rows if r["service"] == service]
        return [dict(r) for r in rows]

    async def update_maintenance_window(self, tenant_id: str, window_id: int, patch: dict) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = self._find_row(self._maintenance_windows, tenant_id, window_id)
        if row is None:
            return None
        for key in ("service", "start_at", "end_at", "reason"):
            if key in patch:
                row[key] = patch[key]
        return dict(row)

    async def delete_maintenance_window(self, tenant_id: str, window_id: int) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = self._maintenance_windows.get(tenant_id, [])
        self._maintenance_windows[tenant_id] = [r for r in rows if r["id"] != window_id]

    async def create_blacklist(self, tenant_id: str, entry: dict) -> dict:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = self._blacklist.setdefault(tenant_id, [])
        row_id = max((r["id"] for r in rows), default=0) + 1
        row = {
            "id": row_id,
            "tenant_id": tenant_id,
            "domain": entry.get("domain", "application"),
            "service": entry["service"],
            "signal": entry["signal"],
            "reason": entry.get("reason"),
            "enabled": bool(entry.get("enabled", True)),
        }
        rows.append(row)
        return dict(row)

    async def list_blacklist(self, tenant_id: str) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return [dict(r) for r in self._blacklist.get(tenant_id, [])]

    async def update_blacklist(self, tenant_id: str, entry_id: int, patch: dict) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = self._find_row(self._blacklist, tenant_id, entry_id)
        if row is None:
            return None
        for key in ("domain", "service", "signal", "reason"):
            if key in patch:
                row[key] = patch[key]
        if "enabled" in patch:
            row["enabled"] = bool(patch["enabled"])
        return dict(row)

    async def delete_blacklist(self, tenant_id: str, entry_id: int) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = self._blacklist.get(tenant_id, [])
        self._blacklist[tenant_id] = [r for r in rows if r["id"] != entry_id]


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

    # ---- M6 写接口 ----

    async def create_maintenance_window(self, tenant_id: str, window: dict) -> dict:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        window_id = await self._pool.execute_lastid(
            "INSERT INTO maintenance_window (tenant_id, service, start_at, end_at, reason) "
            "VALUES (%s, %s, %s, %s, %s)",
            (tenant_id, window["service"], window["start_at"], window["end_at"], window.get("reason")),
        )
        return {
            "id": window_id,
            "tenant_id": tenant_id,
            "service": window["service"],
            "start_at": window["start_at"],
            "end_at": window["end_at"],
            "reason": window.get("reason"),
        }

    async def list_maintenance_windows(self, tenant_id: str, *, service: str | None = None) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        sql = "SELECT id, service, start_at, end_at, reason FROM maintenance_window WHERE tenant_id=%s"
        args: list[Any] = [tenant_id]
        if service is not None:
            sql += " AND service=%s"
            args.append(service)
        rows = await self._pool.fetchall(sql, tuple(args))
        return [
            {"id": r[0], "service": r[1], "start_at": r[2], "end_at": r[3], "reason": r[4]} for r in rows
        ]

    async def update_maintenance_window(self, tenant_id: str, window_id: int, patch: dict) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        sets, args = [], [tenant_id, window_id]
        for key in ("service", "start_at", "end_at", "reason"):
            if key in patch:
                sets.append(f"{key}=%s")
                args.append(patch[key])
        if sets:
            sets_sql = ", ".join(sets)
            await self._pool.execute(
                f"UPDATE maintenance_window SET {sets_sql} WHERE tenant_id=%s AND id=%s", tuple(args)
            )
        row = await self._pool.fetchone(
            "SELECT id, service, start_at, end_at, reason FROM maintenance_window "
            "WHERE tenant_id=%s AND id=%s",
            (tenant_id, window_id),
        )
        if row is None:
            return None
        return {"id": row[0], "service": row[1], "start_at": row[2], "end_at": row[3], "reason": row[4]}

    async def delete_maintenance_window(self, tenant_id: str, window_id: int) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        await self._pool.execute(
            "DELETE FROM maintenance_window WHERE tenant_id=%s AND id=%s", (tenant_id, window_id)
        )

    async def create_blacklist(self, tenant_id: str, entry: dict) -> dict:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        entry_id = await self._pool.execute_lastid(
            "INSERT INTO suppress_blacklist (tenant_id, domain, service, `signal`, reason, enabled) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                tenant_id,
                entry.get("domain", "application"),
                entry["service"],
                entry["signal"],
                entry.get("reason"),
                1 if entry.get("enabled", True) else 0,
            ),
        )
        return {
            "id": entry_id,
            "tenant_id": tenant_id,
            "domain": entry.get("domain", "application"),
            "service": entry["service"],
            "signal": entry["signal"],
            "reason": entry.get("reason"),
            "enabled": bool(entry.get("enabled", True)),
        }

    async def list_blacklist(self, tenant_id: str) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = await self._pool.fetchall(
            "SELECT id, domain, service, `signal`, reason, enabled FROM suppress_blacklist WHERE tenant_id=%s",
            (tenant_id,),
        )
        return [
            {"id": r[0], "domain": r[1], "service": r[2], "signal": r[3], "reason": r[4], "enabled": bool(r[5])}
            for r in rows
        ]

    async def update_blacklist(self, tenant_id: str, entry_id: int, patch: dict) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        sets, args = [], [tenant_id, entry_id]
        for key in ("domain", "service", "signal", "reason"):
            if key in patch:
                sets.append(f"{key}=%s")
                args.append(patch[key])
        if "enabled" in patch:
            sets.append("enabled=%s")
            args.append(1 if patch["enabled"] else 0)
        if sets:
            sets_sql = ", ".join(sets)
            await self._pool.execute(
                f"UPDATE suppress_blacklist SET {sets_sql} WHERE tenant_id=%s AND id=%s", tuple(args)
            )
        row = await self._pool.fetchone(
            "SELECT id, domain, service, `signal`, reason, enabled FROM suppress_blacklist "
            "WHERE tenant_id=%s AND id=%s",
            (tenant_id, entry_id),
        )
        if row is None:
            return None
        return {
            "id": row[0], "domain": row[1], "service": row[2], "signal": row[3],
            "reason": row[4], "enabled": bool(row[5]),
        }

    async def delete_blacklist(self, tenant_id: str, entry_id: int) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        await self._pool.execute(
            "DELETE FROM suppress_blacklist WHERE tenant_id=%s AND id=%s", (tenant_id, entry_id)
        )
