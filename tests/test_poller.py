"""M6 poller：``run_round`` 并行采集 → 降级标记 → 入漏斗。M7 扩展：轮次审计 + metrics。"""

from datetime import datetime, timezone

import pytest

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


async def test_run_round_marks_failed_when_build_context_raises() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        # domain 无检测规则 → build_context 抛错 → 轮次要被标记 failed（而非遗留 running 孤儿）
        with pytest.raises(ValueError, match="no domain config"):
            await run_round(
                registry=registry, storage=storage, tenant_id="default", domain="nosuchdomain",
                targets=[_target(domain="nosuchdomain", _mock_signals=[])], now=TS,
            )
        rounds = await storage.rounds.list_rounds("default")
        assert len(rounds) == 1
        assert rounds[0]["status"] == "failed"
    finally:
        await storage.close()


async def test_run_round_writes_detection_round_success() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        sig = MetricSignal(service="svc-a", metric="cpu_usage", value=0.98, timestamp=TS)
        await run_round(
            registry=registry, storage=storage, tenant_id="default", domain="application",
            targets=[_target(target_id="MT-0001", _mock_signals=[sig])], now=TS,
        )
        rounds = await storage.rounds.list_rounds("default")
        assert len(rounds) == 1
        r = rounds[0]
        assert r["status"] == "success"
        assert r["domain"] == "application"
        assert r["started_at"] == TS
        assert r["timeline"][0]["step"] == "collect_done"
        # 轮次审计 timeline 里 suppressed 步骤带 details（JSON 安全）
        assert all(s.get("step") != "suppressed" or "details" in s for s in r["timeline"])
    finally:
        await storage.close()


async def test_run_round_writes_detection_round_partial_on_degraded() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        bad = _target(target_id="MT-0009", source_type="unknown")
        ok = _target(target_id="MT-0001", _mock_signals=[])
        result = await run_round(
            registry=registry, storage=storage, tenant_id="default", domain="application",
            targets=[bad, ok], now=TS,
        )
        assert result.degraded_sources == ["MT-0009"]
        rounds = await storage.rounds.list_rounds("default")
        assert rounds[0]["status"] == "partial"
        assert rounds[0]["degraded_sources"] == ["MT-0009"]
    finally:
        await storage.close()


async def test_run_round_writes_per_target_rows() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        sig = MetricSignal(service="svc-a", metric="cpu_usage", value=0.98, timestamp=TS)
        bad = _target(target_id="MT-0009", source_type="unknown")
        ok = _target(target_id="MT-0001", _mock_signals=[sig])
        result = await run_round(
            registry=registry, storage=storage, tenant_id="default", domain="application",
            targets=[bad, ok], now=TS,
        )
        # round → target 一对多：每 target 一行，各自状态/信号量/错误
        ok_row = await storage.rounds.latest_target("default", "MT-0001")
        assert ok_row is not None
        assert ok_row["status"] == "ok"
        assert ok_row["signals_count"] == 1
        assert ok_row["error"] is None
        # V6 漏斗后按 service 归因回填：svc-a 触发 1 异常；persistence_rounds=2 首轮不开单 → record 0
        assert ok_row["anomaly_count"] == 1
        assert ok_row["record_count"] == 0
        assert ok_row["suppressed_count"] == 0
        bad_row = await storage.rounds.latest_target("default", "MT-0009")
        assert bad_row is not None
        assert bad_row["status"] == "failed"
        assert bad_row["signals_count"] == 0
        assert bad_row["anomaly_count"] == 0
        assert bad_row["record_count"] == 0
        assert bad_row["suppressed_count"] == 0
        assert bad_row["error"] is not None
        assert result.degraded_sources == ["MT-0009"]
    finally:
        await storage.close()


async def test_run_round_attributes_suppressed_to_target_service() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        # 黑名单命中 cpu_usage → L0 抑制，L1 不再检测 → 该 target 的 service 归因 suppressed_count=1
        storage.dynamic_config.seed_blacklist(
            "default", [{"domain": "application", "service": "svc-a", "signal": "cpu_usage", "reason": "test", "enabled": True}]
        )
        sig = MetricSignal(service="svc-a", metric="cpu_usage", value=0.98, timestamp=TS)
        result = await run_round(
            registry=registry, storage=storage, tenant_id="default", domain="application",
            targets=[_target(target_id="MT-0001", _mock_signals=[sig])], now=TS,
        )
        assert result.anomaly_count == 0  # 被抑制，未进检测
        assert result.suppressed_count == 1
        row = await storage.rounds.latest_target("default", "MT-0001")
        assert row is not None
        assert row["suppressed_count"] == 1
        assert row["anomaly_count"] == 0
        assert row["record_count"] == 0
        # 轮次级 suppressed 计数同样成立
        rounds = await storage.rounds.list_rounds("default")
        assert rounds[0]["suppressed_count"] == 1
    finally:
        await storage.close()
