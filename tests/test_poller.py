"""M6 poller：``run_round`` 并行采集 → 降级标记 → 入漏斗。"""

from datetime import datetime, timezone

from aiops_apm.models.signal import MetricSignal
from aiops_apm.plugins.registry import PluginRegistry
from aiops_apm.poller import run_round
from aiops_apm.settings import Settings
from aiops_apm.storage import Storage, build_storage

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


async def make_storage() -> Storage:
    return await build_storage(Settings(_env_file=None, storage_backend="memory"))


def _target(**over) -> dict:
    t = {
        "target_id": "MT-0001",
        "service": "svc-a",
        "signal_type": "metric",
        "source_type": "mock",
        "domain": "application",
        "_mock_signals": [],
    }
    t.update(over)
    return t


async def test_run_round_merges_signals_from_targets() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        sig = MetricSignal(service="svc-a", metric="cpu_usage", value=0.98, timestamp=TS)
        targets = [
            _target(target_id="MT-0001", _mock_signals=[sig]),
            _target(target_id="MT-0002", service="svc-b", signal_type="log", source_type="mock", _mock_signals=[]),
        ]
        result = await run_round(
            registry=registry, storage=storage, tenant_id="default", domain="application",
            targets=targets, now=TS,
        )
        # 信号合并入漏斗 → metric 被检测（persistence_rounds=2 首轮不开单，但 anomaly_count=1）
        assert result.anomaly_count == 1
        assert result.degraded_sources == []
        assert result.timeline[0]["step"] == "collect_done"
        assert result.timeline[0]["count"] == 1
    finally:
        await storage.close()


async def test_run_round_marks_failed_target_as_degraded() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        # 不支持的 source_type → collector_for 抛错 → 降级标记，不拖垮整轮
        bad = _target(target_id="MT-0009", source_type="unknown")
        ok = _target(target_id="MT-0001", _mock_signals=[])
        result = await run_round(
            registry=registry, storage=storage, tenant_id="default", domain="application",
            targets=[bad, ok], now=TS,
        )
        assert result.degraded_sources == ["MT-0009"]
        assert result.anomaly_count == 0  # 无有效信号
        assert result.timeline[0]["step"] == "collect_done"
    finally:
        await storage.close()
