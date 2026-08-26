"""域检测规则加载器：DB 为主源，空表用 ``domains.yaml`` seed，DB 异常回退 last-known-good。

返回行与 ``DomainConfigStore.load`` 一致：``{"domain", "config", "enabled", "version"}``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from ..storage.domain_config import DomainConfigStore

_DEFAULT_SEED_PATH = Path(__file__).parent / "domains.yaml"


class DomainConfigLoader:
    """缓存 last-known-good，DB 故障时不崩溃（UC-2.6）。"""

    def __init__(self, store: DomainConfigStore, yaml_seed_path: str | None = None) -> None:
        self._store = store
        self._yaml_seed_path = yaml_seed_path
        self._cache: list[dict] | None = None

    def _load_yaml_seed(self) -> list[dict]:
        """解析 seed YAML → ``[{"id", "enabled", "config": {detectors, suppressors, correlation, verify}}]``。"""
        path = Path(self._yaml_seed_path) if self._yaml_seed_path else _DEFAULT_SEED_PATH
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        domains = data["domains"]
        items: list[dict[str, Any]] = []
        for d in domains:
            config = {k: d[k] for k in ("detectors", "suppressors", "correlation", "verify") if k in d}
            items.append({"id": d["id"], "enabled": d.get("enabled", True), "config": config})
        return items

    async def load(self, tenant_id: str) -> list[dict]:
        """加载该租户的域规则：DB 有数据读 DB；空表 seed 后读回；DB 异常回退缓存。"""
        try:
            rows = await self._store.load(tenant_id)
            if rows:
                self._cache = rows
                return rows
            seed = self._load_yaml_seed()
            await self._store.seed(tenant_id, seed)
            rows = await self._store.load(tenant_id)
            self._cache = rows
            return rows
        except Exception:
            if self._cache is not None:
                return self._cache
            raise

    async def reload(self, tenant_id: str) -> list[dict]:
        """清缓存后重新加载（热更新场景）。"""
        self._cache = None
        return await self.load(tenant_id)
