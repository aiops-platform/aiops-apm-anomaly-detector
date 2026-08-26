"""``DynamicConfigStore``：maintenance_window / suppress_blacklist / fpr_table / change_record 读取进 ctx。

覆盖：四类读取（InMemory 预置行）；blacklist 只取 enabled=1；fpr dict 形态；tenant 过滤。
"""

from datetime import datetime, timezone

from aiops_apm.storage.dynamic_config import InMemoryDynamicConfigStore

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


async def test_load_maintenance_windows_filters_tenant() -> None:
    s = InMemoryDynamicConfigStore()
    s.seed_maintenance_windows(
        "default", [{"service": "svc-a", "start_at": TS, "end_at": TS, "reason": "release"}]
    )
    s.seed_maintenance_windows("other", [{"service": "svc-b", "start_at": TS, "end_at": TS, "reason": "x"}])
    rows = await s.load_maintenance_windows("default")
    assert rows == [{"service": "svc-a", "start_at": TS, "end_at": TS, "reason": "release"}]


async def test_load_blacklist_only_enabled() -> None:
    s = InMemoryDynamicConfigStore()
    s.seed_blacklist(
        "default",
        [
            {"domain": "application", "service": "svc-a", "signal": "cpu_usage", "reason": "noise", "enabled": True},
            {"domain": "application", "service": "svc-b", "signal": "ERROR", "reason": "off", "enabled": False},
        ],
    )
    rows = await s.load_blacklist("default")
    assert len(rows) == 1
    assert rows[0]["service"] == "svc-a"
    assert rows[0]["signal"] == "cpu_usage"


async def test_load_fpr_shape() -> None:
    s = InMemoryDynamicConfigStore()
    s.seed_fpr("default", {"gk:123": {"fpr": 0.8, "total": 30}})
    fpr = await s.load_fpr("default")
    assert fpr == {"gk:123": {"fpr": 0.8, "total": 30}}


async def test_load_changes() -> None:
    s = InMemoryDynamicConfigStore()
    s.seed_changes(
        "default",
        [{"change_id": "C-1", "service": "svc-a", "type": "deployment", "summary": "v2", "changed_at": TS}],
    )
    rows = await s.load_changes("default")
    assert rows == [
        {"change_id": "C-1", "service": "svc-a", "type": "deployment", "summary": "v2", "changed_at": TS}
    ]
