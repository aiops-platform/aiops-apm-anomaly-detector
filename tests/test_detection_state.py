"""``DetectionStateStore``：L3 持续性 consecutive / miss 计数。

覆盖：get 无返回 None；upsert 后 get 反映字段；覆盖写；sweep 未见 key miss+1/consecutive 归 0、
见到的 key 不动；tenant/domain 隔离。
"""

from datetime import datetime, timezone

from aiops_apm.storage.detection_state import InMemoryDetectionStateStore

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


async def test_get_missing_returns_none() -> None:
    s = InMemoryDetectionStateStore()
    assert await s.get("default", "application", "k1") is None


async def test_upsert_then_get() -> None:
    s = InMemoryDetectionStateStore()
    await s.upsert("default", "application", "k1", consecutive_rounds=2, miss_rounds=0, first_seen=TS, last_seen=TS)
    state = await s.get("default", "application", "k1")
    assert state is not None
    assert state["consecutive_rounds"] == 2
    assert state["miss_rounds"] == 0
    assert state["first_seen"] == TS
    assert state["last_seen"] == TS


async def test_upsert_overwrites() -> None:
    s = InMemoryDetectionStateStore()
    await s.upsert("default", "application", "k1", consecutive_rounds=1, miss_rounds=0, first_seen=TS, last_seen=TS)
    await s.upsert("default", "application", "k1", consecutive_rounds=2, miss_rounds=0, first_seen=TS, last_seen=TS)
    state = await s.get("default", "application", "k1")
    assert state is not None
    assert state["consecutive_rounds"] == 2


async def test_sweep_unseen_key_miss_and_reset() -> None:
    s = InMemoryDetectionStateStore()
    await s.upsert("default", "application", "k1", consecutive_rounds=3, miss_rounds=0, first_seen=TS, last_seen=TS)
    await s.sweep("default", "application", set())  # k1 本轮未到
    state = await s.get("default", "application", "k1")
    assert state is not None
    assert state["miss_rounds"] == 1
    assert state["consecutive_rounds"] == 0


async def test_sweep_seen_key_untouched() -> None:
    s = InMemoryDetectionStateStore()
    await s.upsert("default", "application", "k1", consecutive_rounds=3, miss_rounds=0, first_seen=TS, last_seen=TS)
    await s.sweep("default", "application", {"k1"})
    state = await s.get("default", "application", "k1")
    assert state is not None
    assert state["miss_rounds"] == 0
    assert state["consecutive_rounds"] == 3


async def test_sweep_isolated_by_tenant_domain() -> None:
    s = InMemoryDetectionStateStore()
    await s.upsert("default", "application", "k1", consecutive_rounds=3, miss_rounds=0, first_seen=TS, last_seen=TS)
    await s.upsert("other", "application", "k1", consecutive_rounds=3, miss_rounds=0, first_seen=TS, last_seen=TS)
    await s.upsert("default", "infra", "k1", consecutive_rounds=3, miss_rounds=0, first_seen=TS, last_seen=TS)
    await s.sweep("default", "application", set())
    assert (await s.get("default", "application", "k1")) is not None
    assert (await s.get("default", "application", "k1"))["miss_rounds"] == 1
    assert (await s.get("other", "application", "k1"))["miss_rounds"] == 0
    assert (await s.get("default", "infra", "k1"))["miss_rounds"] == 0
