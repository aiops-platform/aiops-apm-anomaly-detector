"""采集水位线存储：``collect_watermark`` 表（M3 增量采集）。

- ``WatermarkStore``（ABC）：采集器读/推最近采集到的事件时间戳。
- ``InMemoryWatermarkStore``：单测/demo 真源。
- ``MySQLWatermarkStore``：生产实现。

行结构：``{"last_ts": datetime}``（按 ``(tenant_id, target_id)`` 唯一）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from .connection import ConnectionPool


class WatermarkStore(ABC):
    """采集水位线读写接口。"""

    @abstractmethod
    async def get(self, tenant_id: str, target_id: str) -> dict | None:
        """取水位线；不存在返回 None。"""

    @abstractmethod
    async def update(self, tenant_id: str, target_id: str, last_ts: datetime) -> None:
        """推进水位线（幂等覆盖，不回退语义由调用方保证）。"""


class InMemoryWatermarkStore(WatermarkStore):
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], datetime] = {}

    async def get(self, tenant_id: str, target_id: str) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        last_ts = self._rows.get((tenant_id, target_id))
        return {"last_ts": last_ts} if last_ts is not None else None

    async def update(self, tenant_id: str, target_id: str, last_ts: datetime) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self._rows[(tenant_id, target_id)] = last_ts


class MySQLWatermarkStore(WatermarkStore):
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def get(self, tenant_id: str, target_id: str) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = await self._pool.fetchone(
            "SELECT last_ts FROM collect_watermark WHERE tenant_id=%s AND target_id=%s", (tenant_id, target_id)
        )
        return {"last_ts": row[0]} if row else None

    async def update(self, tenant_id: str, target_id: str, last_ts: datetime) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        await self._pool.execute(
            "INSERT INTO collect_watermark (tenant_id, target_id, last_ts) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE last_ts=VALUES(last_ts)",
            (tenant_id, target_id, last_ts),
        )
