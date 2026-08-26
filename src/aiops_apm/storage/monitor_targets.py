"""监控端点配置存储：``monitor_target`` 表。

- ``MonitorTargetStore``（ABC）：M3 端点管理、M6 调度器加载目标。
- ``InMemoryMonitorTargetStore``：单测/demo 真源。
- ``MySQLMonitorTargetStore``：生产实现。

行结构：``{"target_id", "service", "signal_type", "source_type", "domain",
"source_config"(dict), "schedule"(dict), "enabled"}``。
``target_id`` 形如 ``MT-0001``，由 store 生成，对外唯一。
"""

import builtins
from abc import ABC, abstractmethod
from typing import Any

from .connection import ConnectionPool, _as_json, _decode_json

_PUBLIC_FIELDS = ("target_id", "service", "signal_type", "source_type", "domain", "source_config", "schedule", "enabled")


def _public(row: Any) -> dict:
    """MySQL 行（tuple）转对外 dict；InMemory 已直接存 dict。"""
    if isinstance(row, dict):
        return {k: row[k] for k in _PUBLIC_FIELDS}
    return {
        "target_id": row[0],
        "service": row[1],
        "signal_type": row[2],
        "source_type": row[3],
        "domain": row[4],
        "source_config": _decode_json(row[5]),
        "schedule": _decode_json(row[6]),
        "enabled": bool(row[7]),
    }


def _parse_suffix(target_id: str) -> int:
    """解析 ``MT-0001`` 的数字后缀；解析失败返回 0。"""
    try:
        return int(target_id.split("-")[1])
    except (ValueError, IndexError):
        return 0


class MonitorTargetStore(ABC):
    """监控端点读写接口。"""

    @abstractmethod
    async def create(self, tenant_id: str, target: dict) -> dict:
        """新建端点，生成 ``target_id``（MT-NNNN），返回含 target_id 的行。"""

    @abstractmethod
    async def list(self, tenant_id: str, *, service: str | None = None, signal_type: str | None = None) -> list[dict]:
        """按租户列出端点，可过滤 service / signal_type。"""

    @abstractmethod
    async def get(self, tenant_id: str, target_id: str) -> dict | None:
        """按 target_id 取端点；不存在返回 None。"""

    @abstractmethod
    async def update(self, tenant_id: str, target_id: str, patch: dict) -> dict | None:
        """更新端点字段，返回更新后的行；不存在返回 None。"""

    @abstractmethod
    async def delete(self, tenant_id: str, target_id: str) -> None:
        """软删端点（``enabled=0``）。"""

    @abstractmethod
    async def load_all_targets(self, tenant_id: str) -> builtins.list[dict]:
        """加载该租户 enabled 的端点（M6 调度器用）。"""

    @abstractmethod
    async def list_tenants(self) -> builtins.list[str]:
        """去重返回有启用端点的租户列表（M6 调度器扫描用）。"""


class InMemoryMonitorTargetStore(MonitorTargetStore):
    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._next_id = 1

    def _next_target_id(self, tenant_id: str) -> str:
        suffix = 0
        for r in self._rows:
            if r["tenant_id"] == tenant_id:
                suffix = max(suffix, _parse_suffix(r["target_id"]))
        return f"MT-{suffix + 1:04d}"

    async def create(self, tenant_id: str, target: dict) -> dict:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = {
            "id": self._next_id,
            "tenant_id": tenant_id,
            "target_id": self._next_target_id(tenant_id),
            "service": target["service"],
            "signal_type": target["signal_type"],
            "source_type": target["source_type"],
            "domain": target.get("domain", "application"),
            "source_config": dict(target["source_config"]),
            "schedule": dict(target.get("schedule", {"interval_sec": 60})),
            "enabled": bool(target.get("enabled", True)),
        }
        self._next_id += 1
        self._rows.append(row)
        return _public(row)

    async def list(self, tenant_id: str, *, service: str | None = None, signal_type: str | None = None) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        out = []
        for r in self._rows:
            if r["tenant_id"] != tenant_id:
                continue
            if service is not None and r["service"] != service:
                continue
            if signal_type is not None and r["signal_type"] != signal_type:
                continue
            out.append(_public(r))
        return out

    async def get(self, tenant_id: str, target_id: str) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        for r in self._rows:
            if r["tenant_id"] == tenant_id and r["target_id"] == target_id:
                return _public(r)
        return None

    async def update(self, tenant_id: str, target_id: str, patch: dict) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        for r in self._rows:
            if r["tenant_id"] == tenant_id and r["target_id"] == target_id:
                for key in ("service", "signal_type", "source_type", "domain", "enabled"):
                    if key in patch:
                        r[key] = patch[key]
                if "source_config" in patch:
                    r["source_config"] = dict(patch["source_config"])
                if "schedule" in patch:
                    r["schedule"] = dict(patch["schedule"])
                return _public(r)
        return None

    async def delete(self, tenant_id: str, target_id: str) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        for r in self._rows:
            if r["tenant_id"] == tenant_id and r["target_id"] == target_id:
                r["enabled"] = False
                return

    async def load_all_targets(self, tenant_id: str) -> builtins.list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return [_public(r) for r in self._rows if r["tenant_id"] == tenant_id and r["enabled"]]

    async def list_tenants(self) -> builtins.list[str]:
        tenants = sorted({r["tenant_id"] for r in self._rows if r["enabled"]})
        return tenants


class MySQLMonitorTargetStore(MonitorTargetStore):
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def _next_target_id(self, tenant_id: str) -> str:
        row = await self._pool.fetchone(
            "SELECT target_id FROM monitor_target WHERE tenant_id=%s ORDER BY id DESC LIMIT 1", (tenant_id,)
        )
        return f"MT-{_parse_suffix(row[0]) + 1:04d}" if row else "MT-0001"

    async def create(self, tenant_id: str, target: dict) -> dict:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        target_id = await self._next_target_id(tenant_id)
        await self._pool.execute(
            "INSERT INTO monitor_target "
            "(tenant_id, target_id, service, signal_type, source_type, domain, source_config, schedule, enabled) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                tenant_id,
                target_id,
                target["service"],
                target["signal_type"],
                target["source_type"],
                target.get("domain", "application"),
                _as_json(target["source_config"]),
                _as_json(target.get("schedule", {"interval_sec": 60})),
                1 if target.get("enabled", True) else 0,
            ),
        )
        return {**{"target_id": target_id}, **target, "domain": target.get("domain", "application")}

    async def list(self, tenant_id: str, *, service: str | None = None, signal_type: str | None = None) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        sql = (
            "SELECT target_id, service, signal_type, source_type, domain, source_config, schedule, enabled "
            "FROM monitor_target WHERE tenant_id=%s"
        )
        args: list[Any] = [tenant_id]
        if service:
            sql += " AND service=%s"
            args.append(service)
        if signal_type:
            sql += " AND signal_type=%s"
            args.append(signal_type)
        rows = await self._pool.fetchall(sql, tuple(args))
        return [_public(r) for r in rows]

    async def get(self, tenant_id: str, target_id: str) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = await self._pool.fetchone(
            "SELECT target_id, service, signal_type, source_type, domain, source_config, schedule, enabled "
            "FROM monitor_target WHERE tenant_id=%s AND target_id=%s",
            (tenant_id, target_id),
        )
        return _public(row) if row else None

    async def update(self, tenant_id: str, target_id: str, patch: dict) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not patch:
            return await self.get(tenant_id, target_id)
        sets = []
        args: list[Any] = []
        for key in ("service", "signal_type", "source_type", "domain", "enabled"):
            if key in patch:
                sets.append(f"{key}=%s")
                args.append(1 if key == "enabled" and patch[key] else 0 if key == "enabled" else patch[key])
        for key in ("source_config", "schedule"):
            if key in patch:
                sets.append(f"{key}=%s")
                args.append(_as_json(patch[key]))
        if not sets:
            return await self.get(tenant_id, target_id)
        args.extend([tenant_id, target_id])
        await self._pool.execute(
            f"UPDATE monitor_target SET {', '.join(sets)} WHERE tenant_id=%s AND target_id=%s", tuple(args)
        )
        return await self.get(tenant_id, target_id)

    async def delete(self, tenant_id: str, target_id: str) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        await self._pool.execute(
            "UPDATE monitor_target SET enabled=0 WHERE tenant_id=%s AND target_id=%s", (tenant_id, target_id)
        )

    async def load_all_targets(self, tenant_id: str) -> builtins.list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = await self._pool.fetchall(
            "SELECT target_id, service, signal_type, source_type, domain, source_config, schedule, enabled "
            "FROM monitor_target WHERE tenant_id=%s AND enabled=1",
            (tenant_id,),
        )
        return [_public(r) for r in rows]

    async def list_tenants(self) -> builtins.list[str]:
        rows = await self._pool.fetchall(
            "SELECT DISTINCT tenant_id FROM monitor_target WHERE enabled=1"
        )
        return sorted(r[0] for r in rows)
