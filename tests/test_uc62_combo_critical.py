"""§13 用例 2（UC-6.2 组合升 critical）：内存泄漏（heap metric） + Full GC（ERROR log）同 service。

related（指标+日志同源窗口内）且 metric/log 均为 high → L3 组合升级 critical。
M5 只取最高（high），M6 ``calibrate_severity(*, related)`` 补组合升级。
"""

from datetime import datetime, timedelta, timezone

from aiops_apm.models.config import DetectorSpec, DomainConfig, VerifySpec
from aiops_apm.models.signal import LogSignal, MetricSignal
from aiops_apm.pipeline.context import build_context
from aiops_apm.pipeline.runner import run_domain
from aiops_apm.plugins.registry import PluginRegistry
from aiops_apm.settings import Settings
from aiops_apm.storage import Storage, build_storage

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)

# 内存泄漏：heap_usage 超阈值（high）；Full GC：ERROR 日志按 signature 聚合（high）
COMBO_DOMAIN = DomainConfig(
    detectors=[
        DetectorSpec(signal="heap_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high"),
        DetectorSpec(signal="ERROR", plugin="signature_aggregate", params={"min_count": 1}, severity="high"),
    ],
    verify=VerifySpec(persistence_rounds=2),  # 连续两轮 → 第二轮开单
)


async def make_storage() -> Storage:
    settings = Settings(_env_file=None, storage_backend="memory")
    return await build_storage(settings)


def metric_signal(*, service="svc-a", metric="heap_usage", value=0.95, ts=TS) -> MetricSignal:
    return MetricSignal(service=service, metric=metric, value=value, timestamp=ts)


def log_signal(*, service="svc-a", level="ERROR", signature="java.lang.OOMError", ts=TS) -> LogSignal:
    return LogSignal(service=service, level=level, message="heap dump", signature=signature, timestamp=ts)


def _signals(ts: datetime) -> list:
    return [metric_signal(ts=ts), log_signal(ts=ts)]


async def test_uc62_combo_critical_two_rounds() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()

        # 第 1 轮：同源 metric+log 都出现，related=true；persistence_rounds=2 → 不开单
        ctx1 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=_signals(TS), domain_config=COMBO_DOMAIN,
        )
        r1 = await run_domain(ctx1)
        assert r1.records == []
        assert r1.anomaly_count == 2  # metric + log 各 1

        # 第 2 轮：同样信号再出现 → 开 1 条 critical（组合升级）
        ctx2 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage,
            now=TS + timedelta(seconds=60), signals=_signals(TS + timedelta(seconds=60)),
            domain_config=COMBO_DOMAIN,
        )
        r2 = await run_domain(ctx2)
        assert len(r2.records) == 1
        rec = r2.records[0]
        assert rec.service == "svc-a"
        assert rec.correlation.related is True  # 指标+日志同源窗口内
        assert len(rec.metric_anomalies) == 1
        assert len(rec.log_anomalies) == 1
        assert rec.severity == "critical"  # high metric + high log + related → 组合升 critical
        assert rec.verification.final_severity == "critical"
    finally:
        await storage.close()


async def test_uc62_no_combo_when_log_warning() -> None:
    """只有 high metric + warning log → 不升 critical，取最高 high。"""
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = DomainConfig(
            detectors=[
                DetectorSpec(signal="heap_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high"),
                DetectorSpec(signal="ERROR", plugin="signature_aggregate", params={"min_count": 1}, severity="warning"),
            ],
            verify=VerifySpec(persistence_rounds=1),
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=_signals(TS), domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.correlation.related is True
        assert rec.severity == "high"  # 不满足「high log」→ 不组合升级
    finally:
        await storage.close()


async def test_uc62_no_combo_when_metric_only() -> None:
    """纯 metric（无同源 log）→ related=false → 取最高 high，不升 critical。"""
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(ts=TS)], domain_config=COMBO_DOMAIN,
        )
        result = await run_domain(ctx)
        assert result.records == []  # 单轮 persistence_rounds=2 → 不开单
        assert result.anomaly_count == 1

        ctx2 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage,
            now=TS + timedelta(seconds=60), signals=[metric_signal(ts=TS + timedelta(seconds=60))],
            domain_config=COMBO_DOMAIN,
        )
        r2 = await run_domain(ctx2)
        assert len(r2.records) == 1
        rec = r2.records[0]
        assert rec.correlation.related is False
        assert rec.severity == "high"  # 无 log 同源 → 不组合升级
    finally:
        await storage.close()
