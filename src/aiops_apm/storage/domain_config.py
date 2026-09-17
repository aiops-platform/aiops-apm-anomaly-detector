"""域检测规则配置存储：``domain_config`` 表。

- ``DomainConfigStore``（ABC）：M5 每轮加载规则、M6 写入校验。
- ``InMemoryDomainConfigStore``：单测/demo 真源。
- ``PGDomainConfigStore``：生产实现。

行结构：``{"domain", "config"(JSON→dict), "enabled", "version"}``。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models.config import DomainConfig
from .connection import ConnectionPool, _as_json, _decode_json


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

    @abstractmethod
    async def delete(self, tenant_id: str, domain: str) -> None:
        """硬删该租户某域的规则行（前端 Delete 用，需先过引用守卫）。"""


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

    async def delete(self, tenant_id: str, domain: str) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self._rows[:] = [
            r for r in self._rows if not (r["tenant_id"] == tenant_id and r["domain"] == domain)
        ]


class PGDomainConfigStore(DomainConfigStore):
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
        # RETURNING 一步拿到 version，省掉 MySQL 版随后的那条 SELECT。
        # 注意 version 的递增用的是**表名限定**的旧值（PG 的 DO UPDATE 里右侧引用表名即更新前的
        # 行），不能写成 EXCLUDED.version + 1 —— 那会变成「每次都是 2」。
        version = await self._pool.execute_returning(
            "INSERT INTO domain_config (tenant_id, domain, config, enabled) VALUES (%s, %s, %s, 1) "
            "ON CONFLICT (tenant_id, domain) DO UPDATE SET "
            "config = EXCLUDED.config, enabled = EXCLUDED.enabled, "
            "version = domain_config.version + 1 "
            "RETURNING version",
            (tenant_id, domain, _as_json(_dump(config))),
        )
        return int(version) if version is not None else 1

    async def seed(self, tenant_id: str, seed: list[dict]) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        for item in seed:
            await self._pool.execute(
                "INSERT INTO domain_config (tenant_id, domain, config, enabled) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, domain) DO UPDATE SET "
                "config = EXCLUDED.config, enabled = EXCLUDED.enabled",
                (tenant_id, item["id"], _as_json(item["config"]), 1 if item.get("enabled", True) else 0),
            )

    async def delete(self, tenant_id: str, domain: str) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        await self._pool.execute(
            "DELETE FROM domain_config WHERE tenant_id=%s AND domain=%s", (tenant_id, domain)
        )
