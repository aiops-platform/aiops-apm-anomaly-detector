"""域检测规则配置存储：``domain_config`` 表。

- ``DomainConfigStore``（ABC）：M5 每轮加载规则、M6 写入校验。
- ``InMemoryDomainConfigStore``：单测/demo 真源。
- ``MySQLDomainConfigStore``：生产实现。

行结构：``{"domain", "config"(JSON→dict), "enabled", "version"}``。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models.config import DomainConfig
from .connection import ConnectionPool, _as_json


def _decode_json(value: Any) -> Any:
    import json

    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _dump(config: DomainConfig | dict) -> dict:
    return config.model_dump() if isinstance(config, DomainConfig) else dict(config)


class DomainConfigStore(ABC):
    """域检测规则读写接口。"""

    @abstractmethod
    async def load(self, tenant_id: str) -> list[dict]:
        """该租户 enabled 的域规则行。"""

    @abstractmethod
    async def upsert(self, tenant_id: str, domain: str, config: DomainConfig) -> int:
        """写入/更新域规则，返回 version。"""

    @abstractmethod
    async def seed(self, tenant_id: str, seed: list[dict]) -> None:
        """幂等 seed（INSERT ... ON DUPLICATE KEY UPDATE）。seed 项形如 ``{"id", "enabled", "config"}``。"""


class InMemoryDomainConfigStore(DomainConfigStore):
    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    async def load(self, tenant_id: str) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return [dict(r) for r in self._rows if r["tenant_id"] == tenant_id and r["enabled"]]

    async def upsert(self, tenant_id: str, domain: str, config: DomainConfig) -> int:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        cfg = _dump(config)
        for r in self._rows:
            if r["tenant_id"] == tenant_id and r["domain"] == domain:
                r["config"] = cfg
                r["enabled"] = True
                r["version"] += 1
                return r["version"]
        self._rows.append({"tenant_id": tenant_id, "domain": domain, "config": cfg, "enabled": True, "version": 1})
        return 1

    async def seed(self, tenant_id: str, seed: list[dict]) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        for item in seed:
            domain = item["id"]
            if any(r["tenant_id"] == tenant_id and r["domain"] == domain for r in self._rows):
                continue
            self._rows.append(
                {
                    "tenant_id": tenant_id,
                    "domain": domain,
                    "config": dict(item["config"]),
                    "enabled": bool(item.get("enabled", True)),
                    "version": 1,
                }
            )


class MySQLDomainConfigStore(DomainConfigStore):
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def load(self, tenant_id: str) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = await self._pool.fetchall(
            "SELECT domain, config, enabled, version FROM domain_config "
            "WHERE tenant_id=%s AND enabled=1",
            (tenant_id,),
        )
        return [
            {"domain": r[0], "config": _decode_json(r[1]), "enabled": bool(r[2]), "version": r[3]}
            for r in rows
        ]

    async def upsert(self, tenant_id: str, domain: str, config: DomainConfig) -> int:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        await self._pool.execute(
            "INSERT INTO domain_config (tenant_id, domain, config, enabled) VALUES (%s, %s, %s, 1) "
            "ON DUPLICATE KEY UPDATE config=VALUES(config), enabled=VALUES(enabled), version=version+1",
            (tenant_id, domain, _as_json(_dump(config))),
        )
        row = await self._pool.fetchone(
            "SELECT version FROM domain_config WHERE tenant_id=%s AND domain=%s", (tenant_id, domain)
        )
        return int(row[0]) if row is not None else 1

    async def seed(self, tenant_id: str, seed: list[dict]) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        for item in seed:
            await self._pool.execute(
                "INSERT INTO domain_config (tenant_id, domain, config, enabled) VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE config=VALUES(config), enabled=VALUES(enabled)",
                (tenant_id, item["id"], _as_json(item["config"]), 1 if item.get("enabled", True) else 0),
            )
