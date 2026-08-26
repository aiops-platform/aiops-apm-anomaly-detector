"""UC-3.5 采集水位线：``InMemoryWatermarkStore`` get/update 覆盖不回退。"""

from datetime import datetime

import pytest

from aiops_apm.storage import InMemoryWatermarkStore


@pytest.fixture
def store() -> InMemoryWatermarkStore:
    return InMemoryWatermarkStore()


async def test_get_absent_returns_none(store):
    assert await store.get("tenant-a", "MT-0001") is None


async def test_update_then_get(store):
    ts = datetime(2026, 8, 26, 12, 0, 0)
    await store.update("tenant-a", "MT-0001", ts)
    assert (await store.get("tenant-a", "MT-0001"))["last_ts"] == ts


async def test_update_overwrites_with_newer(store):
    await store.update("tenant-a", "MT-0001", datetime(2026, 8, 26, 12, 0, 0))
    newer = datetime(2026, 8, 26, 12, 30, 0)
    await store.update("tenant-a", "MT-0001", newer)
    assert (await store.get("tenant-a", "MT-0001"))["last_ts"] == newer


async def test_per_tenant_isolation(store):
    await store.update("tenant-a", "MT-0001", datetime(2026, 8, 26, 12, 0, 0))
    assert await store.get("tenant-b", "MT-0001") is None


async def test_missing_tenant_id_raises(store):
    with pytest.raises(ValueError):
        await store.get("", "MT-0001")
    with pytest.raises(ValueError):
        await store.update("", "MT-0001", datetime(2026, 8, 26, 12, 0, 0))
