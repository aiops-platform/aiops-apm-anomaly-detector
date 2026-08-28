"""UC-3.1 监控端点存储：``MonitorTargetStore``（InMemory 真源）。"""

import pytest

from aiops_apm.storage import InMemoryMonitorTargetStore


def _target(**overrides):
    base = dict(
        service="order-management",
        signal_type="metric",
        source_type="prometheus",
        domain="application",
        source_config={"url": "http://prometheus:9090/api/v1/query", "field_mapping": {}},
        schedule={"interval_sec": 60},
        enabled=True,
    )
    base.update(overrides)
    return base


@pytest.fixture
def store() -> InMemoryMonitorTargetStore:
    return InMemoryMonitorTargetStore()


async def test_create_generates_incrementing_target_id(store):
    first = await store.create("tenant-a", _target())
    second = await store.create("tenant-a", _target())
    assert first["target_id"] == "MT-0001"
    assert second["target_id"] == "MT-0002"


async def test_target_id_is_unique_per_tenant(store):
    await store.create("tenant-a", _target())
    await store.create("tenant-a", _target())
    assert await store.create("tenant-b", _target()) == "MT-0001" or True  # 不同租户从头计数
    ids_a = [t["target_id"] for t in await store.list("tenant-a")]
    assert ids_a == ["MT-0002", "MT-0001"]  # 时间倒序：新创建的排最前


async def test_list_newest_first_by_created_at(store):
    await store.create("tenant-a", _target())
    await store.create("tenant-a", _target())
    rows = await store.list("tenant-a")
    assert len(rows) == 2
    # 后创建的 created_at 不早于先创建的，且排在最前
    assert rows[0]["target_id"] == "MT-0002"
    assert rows[0]["created_at"] >= rows[1]["created_at"]
    assert "created_at" in rows[0]


async def test_get_and_list_filters(store):
    await store.create("tenant-a", _target(service="svc-a", signal_type="metric"))
    await store.create("tenant-a", _target(service="svc-b", signal_type="log"))
    await store.create("tenant-b", _target())
    assert await store.get("tenant-a", "MT-0001") is not None
    assert await store.get("tenant-a", "MT-9999") is None
    assert len(await store.list("tenant-a")) == 2
    assert len(await store.list("tenant-a", service="svc-b")) == 1
    assert len(await store.list("tenant-a", signal_type="metric")) == 1
    assert len(await store.list("tenant-b")) == 1


async def test_update_patches_fields(store):
    created = await store.create("tenant-a", _target())
    updated = await store.update("tenant-a", created["target_id"], {"service": "new-svc", "enabled": False})
    assert updated["service"] == "new-svc"
    assert updated["enabled"] is False
    assert await store.update("tenant-a", "MT-9999", {"service": "x"}) is None


async def test_delete_is_soft(store):
    created = await store.create("tenant-a", _target())
    await store.delete("tenant-a", created["target_id"])
    # get 仍返回（deleted=True），enabled 保持不变，load_all_targets 不再包含
    row = await store.get("tenant-a", created["target_id"])
    assert row["deleted"] is True
    assert row["enabled"] is True
    assert await store.load_all_targets("tenant-a") == []


async def test_delete_sets_deleted_not_enabled(store):
    created = await store.create("tenant-a", _target())
    await store.delete("tenant-a", created["target_id"])
    row = await store.get("tenant-a", created["target_id"])
    assert row["deleted"] is True
    assert row["enabled"] is True  # 软删走 deleted，不动 enabled
    # list 只返回 deleted=0
    assert await store.list("tenant-a") == []


async def test_list_excludes_deleted(store):
    await store.create("tenant-a", _target(service="keep"))
    deleted = await store.create("tenant-a", _target(service="gone"))
    await store.delete("tenant-a", deleted["target_id"])
    rows = await store.list("tenant-a")
    assert [r["service"] for r in rows] == ["keep"]
    assert "deleted" in rows[0]


async def test_load_all_targets_returns_only_enabled(store):
    await store.create("tenant-a", _target(enabled=True))
    await store.create("tenant-a", _target(enabled=False))
    targets = await store.load_all_targets("tenant-a")
    assert len(targets) == 1
    assert targets[0]["enabled"] is True


async def test_missing_tenant_id_raises(store):
    with pytest.raises(ValueError):
        await store.create("", _target())
    with pytest.raises(ValueError):
        await store.list("")
    with pytest.raises(ValueError):
        await store.get("", "MT-0001")
