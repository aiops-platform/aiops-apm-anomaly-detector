"""L3 持续性状态：``detection_state`` 表 cumulative-appear / miss 计数。

- ``consecutive_rounds`` 字段语义实为**累计出现轮数**：仅在该异常出现的轮 +1，
  断轮（miss）不清零（L3 持续性按累计出现 N 轮判定，不再要求严格连续）。
- ``miss_rounds``：连续未出现轮数，仅用于 Reconciler 自动关单（读 miss 不清零字段）。
- ``DetectionStateStore``（ABC）：M5 ``l3_verify`` 读写持续性、``run_domain`` sweep miss。
- ``InMemoryDetectionStateStore``：单测/demo 真源。
- ``PGDetectionStateStore``：``state_value`` 存 JSONB，sweep 用 ``jsonb_set`` 增量。

每方法入口校验 ``tenant_id`` 非空（多租户隔离硬约束）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from .connection import ConnectionPool, _as_json, _decode_json


class DetectionStateStore(ABC):
    """按 ``(tenant_id, domain, state_key)`` 的持续性计数读写。"""

    @abstractmethod
    async def get(self, tenant_id: str, domain: str, key: str) -> dict | None:
        """返回 ``{"consecutive_rounds", "miss_rounds", "first_seen", "last_seen"}``，无则 None。"""

    @abstractmethod
    async def upsert(
        self,
        tenant_id: str,
        domain: str,
        key: str,
        *,
        consecutive_rounds: int,
        miss_rounds: int,
        first_seen: datetime,
        last_seen: datetime,
    ) -> None:
        """覆盖写该 key 的持续性计数。"""

    @abstractmethod
    async def sweep(self, tenant_id: str, domain: str, seen_keys: set) -> None:
        """本域 store 里本轮未到（∉ seen_keys）的 key → miss_rounds+1（consecutive_rounds 不清零，累计语义）。"""

    @abstractmethod
    async def list_by_domain(self, tenant_id: str, domain: str) -> dict:
        """返回该域全部 ``{state_key: {consecutive_rounds, miss_rounds, first_seen, last_seen}}``（M6 reconcile 用）。"""


class InMemoryDetectionStateStore(DetectionStateStore):
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], dict] = {}

    async def get(self, tenant_id: str, domain: str, key: str) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = self._rows.get((tenant_id, domain, key))
        return dict(row) if row is not None else None

    async def upsert(
        self,
        tenant_id: str,
        domain: str,
        key: str,
        *,
        consecutive_rounds: int,
        miss_rounds: int,
        first_seen: datetime,
        last_seen: datetime,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self._rows[(tenant_id, domain, key)] = {
            "consecutive_rounds": consecutive_rounds,
            "miss_rounds": miss_rounds,
            "first_seen": first_seen,
            "last_seen": last_seen,
        }

    async def sweep(self, tenant_id: str, domain: str, seen_keys: set) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        for (t, d, k), row in self._rows.items():
            if t == tenant_id and d == domain and k not in seen_keys:
                # miss 只累计 miss_rounds；consecutive_rounds 不清零（累计出现语义）
                row["miss_rounds"] += 1

    async def list_by_domain(self, tenant_id: str, domain: str) -> dict:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return {
            k: dict(row)
            for (t, d, k), row in self._rows.items()
            if t == tenant_id and d == domain
        }


def _iso(value: datetime) -> str:
    return value.isoformat()


class PGDetectionStateStore(DetectionStateStore):
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def get(self, tenant_id: str, domain: str, key: str) -> dict | None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        row = await self._pool.fetchone(
            "SELECT state_value FROM detection_state WHERE tenant_id=%s AND domain=%s AND state_key=%s",
            (tenant_id, domain, key),
        )
        if row is None:
            return None
        value = _decode_json(row[0])
        value["first_seen"] = datetime.fromisoformat(value["first_seen"])
        value["last_seen"] = datetime.fromisoformat(value["last_seen"])
        return value

    async def upsert(
        self,
        tenant_id: str,
        domain: str,
        key: str,
        *,
        consecutive_rounds: int,
        miss_rounds: int,
        first_seen: datetime,
        last_seen: datetime,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        state_value = _as_json(
            {
                "consecutive_rounds": consecutive_rounds,
                "miss_rounds": miss_rounds,
                "first_seen": _iso(first_seen),
                "last_seen": _iso(last_seen),
            }
        )
        await self._pool.execute(
            "INSERT INTO detection_state (tenant_id, domain, state_key, state_value) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, domain, state_key) DO UPDATE SET state_value = EXCLUDED.state_value",
            (tenant_id, domain, key, state_value),
        )

    async def sweep(self, tenant_id: str, domain: str, seen_keys: set) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = await self._pool.fetchall(
            "SELECT state_key FROM detection_state WHERE tenant_id=%s AND domain=%s", (tenant_id, domain)
        )
        for (key,) in rows:
            if key in seen_keys:
                continue
            # miss 只累计 miss_rounds；consecutive_rounds 不清零（累计出现语义）
            # 路径必须是 PG 的数组形态 '{miss_rounds}'。写成 MySQL 的 '$.miss_rounds' 不会
            # 报错——它会创建一个**名为 "$.miss_rounds" 的新键**，于是 miss 计数永远停在
            # 原值、reconcile 的自动关单静默失效。
            # COALESCE(...,0) 也是必需的：键缺失时 (NULL)::int + 1 得 NULL，to_jsonb(NULL)
            # 与 jsonb_set(..., NULL) 都是 strict 返回 NULL，会把 NOT NULL 的 state_value
            # 写成 NULL → 23502。
            await self._pool.execute(
                "UPDATE detection_state SET state_value = jsonb_set(state_value, '{miss_rounds}', "
                "to_jsonb(COALESCE((state_value->>'miss_rounds')::int, 0) + 1)) "
                "WHERE tenant_id=%s AND domain=%s AND state_key=%s",
                (tenant_id, domain, key),
            )

    async def list_by_domain(self, tenant_id: str, domain: str) -> dict:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = await self._pool.fetchall(
            "SELECT state_key, state_value FROM detection_state WHERE tenant_id=%s AND domain=%s",
            (tenant_id, domain),
        )
        out: dict = {}
        for key, value in rows:
            v = _decode_json(value)
            v["first_seen"] = datetime.fromisoformat(v["first_seen"])
            v["last_seen"] = datetime.fromisoformat(v["last_seen"])
            out[key] = v
        return out
