"""UC-2.5/2.6 配置加载与 Seed：空表 seed 幂等 / 二次 load 从 DB 读 / DB 异常回退 last-known-good。"""

from pathlib import Path

import pytest

from aiops_apm.config.loader import DomainConfigLoader
from aiops_apm.models.config import CorrelationSpec, DetectorSpec, DomainConfig, VerifySpec
from aiops_apm.storage.domain_config import InMemoryDomainConfigStore

YAML = """
domains:
  - id: application
    enabled: true
    detectors:
      - { signal: cpu_usage, plugin: static_threshold, params: { threshold: 0.9 }, severity: high }
      - { signal: error_rate, plugin: simple_compare, params: { ratio: 1.5, baseline: 0.02 }, severity: high }
      - { signal: ERROR, plugin: signature_aggregate, params: { min_count: 5, n_frames: 3 }, severity: warning }
    suppressors:
      - { name: maintenance_window }
      - { name: blacklist }
    correlation: { metric_log_window_sec: 300, change_window_sec: 300 }
    verify: { persistence_rounds: 2, false_positive_threshold: 0.6, min_samples: 20 }
"""


@pytest.fixture
def yaml_seed(tmp_path: Path) -> str:
    p = tmp_path / "domains.yaml"
    p.write_text(YAML, encoding="utf-8")
    return str(p)


def _domain_config(plugin: str = "updated_detector") -> DomainConfig:
    return DomainConfig(
        detectors=[DetectorSpec(signal="cpu_usage", plugin=plugin)],
        suppressors=[],
        correlation=CorrelationSpec(),
        verify=VerifySpec(),
    )


async def test_uc25_seed_on_empty_store(yaml_seed: str) -> None:
    store = InMemoryDomainConfigStore()
    loader = DomainConfigLoader(store, yaml_seed_path=yaml_seed)
    rows = await loader.load("default")

    assert len(rows) == 1
    assert rows[0]["domain"] == "application"
    assert rows[0]["enabled"] is True
    cfg = rows[0]["config"]
    assert len(cfg["detectors"]) == 3
    assert cfg["detectors"][0]["plugin"] == "static_threshold"
    assert cfg["verify"]["min_samples"] == 20


async def test_seed_is_idempotent(yaml_seed: str) -> None:
    store = InMemoryDomainConfigStore()
    loader = DomainConfigLoader(store, yaml_seed_path=yaml_seed)
    await loader.load("default")
    await loader.load("default")
    assert len(await store.load("default")) == 1  # 未重复写入


async def test_second_load_reads_from_store_not_reseed(yaml_seed: str) -> None:
    store = InMemoryDomainConfigStore()
    loader = DomainConfigLoader(store, yaml_seed_path=yaml_seed)
    await loader.load("default")

    # 模拟热更新：直接改 store
    await store.upsert("default", "application", _domain_config(plugin="new_detector"))
    rows = await loader.load("default")
    assert rows[0]["config"]["detectors"][0]["plugin"] == "new_detector"


async def test_upsert_returns_version_and_validates_tenant(yaml_seed: str) -> None:
    store = InMemoryDomainConfigStore()
    cfg = _domain_config()
    assert await store.upsert("default", "application", cfg) == 1
    assert await store.upsert("default", "application", cfg) == 2
    with pytest.raises(ValueError):
        await store.upsert("", "application", cfg)
    with pytest.raises(ValueError):
        await store.load("")
    with pytest.raises(ValueError):
        await store.seed("", [{"id": "application"}])


async def test_uc26_fallback_to_cache_on_db_failure(yaml_seed: str) -> None:
    class FailingStore(InMemoryDomainConfigStore):
        fail = False

        async def load(self, tenant_id: str) -> list[dict]:
            if self.fail:
                raise RuntimeError("db down")
            return await super().load(tenant_id)

    store = FailingStore()
    loader = DomainConfigLoader(store, yaml_seed_path=yaml_seed)
    rows = await loader.load("default")
    assert len(rows) == 1

    store.fail = True
    rows2 = await loader.load("default")
    assert rows2 == rows  # last-known-good 回退，服务不崩溃


async def test_load_raises_without_cache_when_db_fails() -> None:
    class FailingStore(InMemoryDomainConfigStore):
        async def load(self, tenant_id: str) -> list[dict]:
            raise RuntimeError("db down")

    store = FailingStore()
    loader = DomainConfigLoader(store, yaml_seed_path="nonexistent.yaml")
    with pytest.raises(RuntimeError):
        await loader.load("default")
