"""内置 suppressor 插件（M4：maintenance_window / blacklist）。

设计文档 §6.3：数据源为 maintenance_window / suppress_blacklist 表，由调用方加载进 ctx
（M5 DetectionContext 负责从表读）；M4 插件只消费 ``ctx.maintenance_windows`` / ``ctx.blacklist``。
"""

from datetime import datetime

import pytest

from aiops_apm.models.signal import LogSignal, MetricSignal
from aiops_apm.suppressors.blacklist import BlacklistSuppressor
from aiops_apm.suppressors.maintenance_window import MaintenanceWindowSuppressor

START = datetime(2026, 8, 26, 10, 0, 0)
END = datetime(2026, 8, 26, 14, 0, 0)
NOON = datetime(2026, 8, 26, 12, 0, 0)
EVENING = datetime(2026, 8, 26, 18, 0, 0)


def ms(*, service="svc-a", metric="cpu_usage", value=0.95, timestamp=NOON) -> MetricSignal:
    return MetricSignal(service=service, metric=metric, value=value, timestamp=timestamp)


def ls(*, service="svc-a", level="ERROR", timestamp=NOON) -> LogSignal:
    return LogSignal(service=service, level=level, message="boom", timestamp=timestamp)


class Ctx:
    """fake ctx（M5 DetectionContext 占位）：suppressor 从 ctx 读维护窗口/黑名单。"""

    def __init__(self, maintenance_windows=None, blacklist=None) -> None:
        self.maintenance_windows = maintenance_windows or []
        self.blacklist = blacklist or []


# --- maintenance_window ---


@pytest.mark.asyncio
async def test_maintenance_window_suppresses_inside():
    ctx = Ctx(maintenance_windows=[{"service": "svc-a", "start_at": START, "end_at": END, "reason": "deploy"}])
    reason = await MaintenanceWindowSuppressor().check(ms(), ctx, {})
    assert reason == "maintenance_window: deploy"


@pytest.mark.asyncio
async def test_maintenance_window_passes_outside():
    ctx = Ctx(maintenance_windows=[{"service": "svc-a", "start_at": START, "end_at": END, "reason": "deploy"}])
    assert await MaintenanceWindowSuppressor().check(ms(timestamp=EVENING), ctx, {}) is None


@pytest.mark.asyncio
async def test_maintenance_window_service_mismatch_passes():
    ctx = Ctx(maintenance_windows=[{"service": "svc-b", "start_at": START, "end_at": END, "reason": "deploy"}])
    assert await MaintenanceWindowSuppressor().check(ms(service="svc-a"), ctx, {}) is None


@pytest.mark.asyncio
async def test_maintenance_window_no_windows_passes():
    assert await MaintenanceWindowSuppressor().check(ms(), Ctx(), {}) is None


@pytest.mark.asyncio
async def test_maintenance_window_reason_default_empty():
    ctx = Ctx(maintenance_windows=[{"service": "svc-a", "start_at": START, "end_at": END}])
    assert await MaintenanceWindowSuppressor().check(ms(), ctx, {}) == "maintenance_window: "


@pytest.mark.asyncio
async def test_maintenance_window_batch_check():
    ctx = Ctx(maintenance_windows=[{"service": "svc-a", "start_at": START, "end_at": END, "reason": "deploy"}])
    inside, outside = ms(), ms(timestamp=EVENING)
    results = await MaintenanceWindowSuppressor().batch_check([inside, outside], ctx, {})
    assert results[0] == (inside, "maintenance_window: deploy")
    assert results[1] == (outside, None)


# --- blacklist ---


@pytest.mark.asyncio
async def test_blacklist_metric_hit():
    ctx = Ctx(blacklist=[{"domain": "application", "service": "svc-a", "signal": "cpu_usage", "reason": "known noise"}])
    reason = await BlacklistSuppressor().check(ms(metric="cpu_usage"), ctx, {})
    assert reason == "blacklist: known noise"


@pytest.mark.asyncio
async def test_blacklist_metric_miss():
    ctx = Ctx(blacklist=[{"domain": "application", "service": "svc-a", "signal": "cpu_usage", "reason": "known noise"}])
    assert await BlacklistSuppressor().check(ms(metric="memory_usage"), ctx, {}) is None


@pytest.mark.asyncio
async def test_blacklist_log_level_hit():
    ctx = Ctx(blacklist=[{"domain": "application", "service": "svc-a", "signal": "ERROR", "reason": "noisy level"}])
    reason = await BlacklistSuppressor().check(ls(level="ERROR"), ctx, {})
    assert reason == "blacklist: noisy level"


@pytest.mark.asyncio
async def test_blacklist_service_mismatch_passes():
    ctx = Ctx(blacklist=[{"domain": "application", "service": "svc-b", "signal": "cpu_usage", "reason": "known noise"}])
    assert await BlacklistSuppressor().check(ms(service="svc-a", metric="cpu_usage"), ctx, {}) is None


@pytest.mark.asyncio
async def test_blacklist_batch_check():
    ctx = Ctx(blacklist=[{"domain": "application", "service": "svc-a", "signal": "cpu_usage", "reason": "known noise"}])
    hit, miss = ms(metric="cpu_usage"), ms(metric="memory_usage")
    results = await BlacklistSuppressor().batch_check([hit, miss], ctx, {})
    assert results[0] == (hit, "blacklist: known noise")
    assert results[1] == (miss, None)
