"""§13 / UC-5.x 端到端：``run_domain`` + ``build_context``（真实 registry entry_points + InMemoryStorage）。

完成标准（Enhanced plan M5）：§13 用例 1/3/4/5/6/7/8/9/10/11 全部通过。
用例 2（内存泄漏组合 → critical）端到端留 M6。
"""

from datetime import datetime, timedelta, timezone

from aiops_apm.models import fingerprint
from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.config import CorrelationSpec, DetectorSpec, DomainConfig, SuppressorSpec, VerifySpec
from aiops_apm.models.signal import ChangeSignal, LogSignal, MetricSignal
from aiops_apm.pipeline.context import build_context
from aiops_apm.pipeline.runner import run_domain
from aiops_apm.plugins.registry import PluginRegistry
from aiops_apm.settings import Settings
from aiops_apm.storage import Storage, build_storage

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


async def make_storage() -> Storage:
    settings = Settings(_env_file=None, storage_backend="memory")
    return await build_storage(settings)


def metric_signal(*, service="svc-a", metric="cpu_usage", value=0.95, ts=TS) -> MetricSignal:
    return MetricSignal(service=service, metric=metric, value=value, timestamp=ts)


def log_signal(*, service="svc-a", level="ERROR", message="boom", signature="java.lang.OOMError", ts=TS) -> LogSignal:
    return LogSignal(service=service, level=level, message=message, signature=signature, timestamp=ts)


def domain_with(
    detectors: list[DetectorSpec],
    *,
    suppressors: list[SuppressorSpec] | None = None,
    verify: VerifySpec | None = None,
    correlation: CorrelationSpec | None = None,
) -> DomainConfig:
    return DomainConfig(
        detectors=detectors,
        suppressors=suppressors or [],
        correlation=correlation or CorrelationSpec(),
        verify=verify or VerifySpec(persistence_rounds=1),  # 单轮场景
    )


CPU_DOMAIN = DomainConfig(
    detectors=[DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high")],
    verify=VerifySpec(persistence_rounds=2, false_positive_threshold=0.6, min_samples=20),
)


def cpu_key() -> str:
    return MetricAnomaly(
        service="svc-a", metric="cpu_usage", value=0.9, method="static_threshold", severity="high", detected_at=TS
    ).anomaly_key()


# --- UC-5.1：CPU 飙高两轮（第一轮不开单，第二轮 1 条 high） ---


async def test_uc51_cpu_spike_two_rounds() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        ctx1 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.95)], domain_config=CPU_DOMAIN,
        )
        r1 = await run_domain(ctx1)
        assert r1.records == []
        assert r1.anomaly_count == 1

        ctx2 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage,
            now=TS + timedelta(seconds=60), signals=[metric_signal(value=0.96)], domain_config=CPU_DOMAIN,
        )
        r2 = await run_domain(ctx2)
        assert len(r2.records) == 1
        rec = r2.records[0]
        assert rec.severity == "high"
        assert rec.service == "svc-a"
        assert rec.state == "pending"
        assert rec.correlation.reason == "metric_only"
    finally:
        await storage.close()


# --- UC-5.3：47 条 OOM 日志聚合 → 1 条 anomaly count=47，纯日志开单 ---


async def test_uc53_47_oom_logs_aggregate() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="ERROR", plugin="signature_aggregate", params={"min_count": 5}, severity="warning")]
        )
        signals = [log_signal() for _ in range(47)]
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=signals, domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1
        rec = result.records[0]
        assert len(rec.log_anomalies) == 1
        assert rec.log_anomalies[0].count == 47
        assert rec.metric_anomalies == []
        assert rec.correlation.related is False
        assert rec.correlation.reason == "log_only"
    finally:
        await storage.close()


# --- UC-5.4：指标 + 日志同源关联 → 只有 1 条 record，related=true ---


async def test_uc54_metric_log_same_source() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [
                DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high"),
                DetectorSpec(signal="ERROR", plugin="signature_aggregate", params={"min_count": 1}, severity="warning"),
            ]
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.95, ts=TS), log_signal(ts=TS)], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1  # 只有 1 条（去重/同源关联）
        rec = result.records[0]
        assert rec.correlation.related is True
        assert rec.correlation.reason == "metric_log_within_window"
        assert len(rec.metric_anomalies) == 1
        assert len(rec.log_anomalies) == 1
    finally:
        await storage.close()


# --- UC-5.5：错误率突增 + 部署变更 → change_related=true，recent_change 含 id+summary ---


async def test_uc55_error_rate_change_related() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [
                DetectorSpec(
                    signal="error_rate", plugin="simple_compare",
                    params={"baseline": 0.02, "ratio": 1.5}, severity="high",
                )
            ]
        )
        changes = [ChangeSignal(service="svc-a", change_id="C-100", type="deployment", summary="v2 deploy", timestamp=TS)]
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(metric="error_rate", value=0.1, ts=TS)], changes=changes, domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.change_related is True
        assert rec.recent_change is not None
        assert rec.recent_change["change_id"] == "C-100"
        assert "v2 deploy" in rec.recent_change["summary"]
    finally:
        await storage.close()


# --- UC-5.6：瞬时抖动过滤（三轮不开单，detection_state 反映 consecutive/miss） ---


async def test_uc56_transient_spike_filtered() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        # 第 1 轮 spike 出现，第 2/3 轮消失 → 永远到不了 persistence_rounds=2
        ctx1 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.98, ts=TS)], domain_config=CPU_DOMAIN,
        )
        r1 = await run_domain(ctx1)
        assert r1.records == []

        ctx2 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage,
            now=TS + timedelta(seconds=60), signals=[metric_signal(value=0.5, ts=TS + timedelta(seconds=60))],
            domain_config=CPU_DOMAIN,
        )
        r2 = await run_domain(ctx2)
        assert r2.records == []

        ctx3 = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage,
            now=TS + timedelta(seconds=120), signals=[metric_signal(value=0.5, ts=TS + timedelta(seconds=120))],
            domain_config=CPU_DOMAIN,
        )
        r3 = await run_domain(ctx3)
        assert r3.records == []

        state = await storage.detection_state.get("default", "application", cpu_key())
        assert state is not None
        assert state["consecutive_rounds"] == 0
        assert state["miss_rounds"] == 2
    finally:
        await storage.close()


# --- UC-5.7：维护窗口抑制（不开单，suppressed_count=1，有审计） ---


async def test_uc57_maintenance_window_suppressed() -> None:
    storage = await make_storage()
    try:
        storage.dynamic_config.seed_maintenance_windows(
            "default",
            [
                {
                    "service": "svc-a",
                    "start_at": TS - timedelta(seconds=60),
                    "end_at": TS + timedelta(seconds=60),
                    "reason": "scheduled release",
                }
            ],
        )
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high")],
            suppressors=[SuppressorSpec(name="maintenance_window")],
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.98, ts=TS)], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert result.records == []
        assert result.suppressed_count == 1
        assert any(t["step"] == "suppressed" and t["count"] == 1 for t in result.timeline)
    finally:
        await storage.close()


# --- UC-5.8：误报率闸门（仍开单不永久静默，severity 降级 warning，verification 有审计） ---


async def test_uc58_fpr_downgrades_but_still_emits() -> None:
    storage = await make_storage()
    try:
        sample = MetricAnomaly(
            service="svc-a", metric="cpu_usage", value=0.95, method="static_threshold",
            severity="high", detected_at=TS,
        )
        gk = fingerprint.group_key("default", "application", "svc-a", [sample])
        storage.dynamic_config.seed_fpr("default", {gk: {"fpr": 0.9, "total": 50}})
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high")]
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.95, ts=TS)], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1  # 不永久静默
        rec = result.records[0]
        assert rec.severity == "warning"  # 降级
        assert rec.verification.false_positive_rate == 0.9
        assert rec.verification.final_severity == "warning"
    finally:
        await storage.close()


# --- UC-5.9：无信号提前终止（不开单，timeline collect_done 0） ---


async def test_uc59_no_signal() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high")]
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert result.records == []
        collect_done = next(t for t in result.timeline if t["step"] == "collect_done")
        assert collect_done["count"] == 0
    finally:
        await storage.close()


# --- UC-5.10：日志源超时降级（不崩溃，record 带 degraded 标记） ---


async def test_uc510_degraded_source_marked() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high")]
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[metric_signal(value=0.95, ts=TS)], degraded_sources=["MT-0001"], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert len(result.records) == 1
        rec = result.records[0]
        assert any(e["type"] == "degraded" for e in rec.evidence)
        assert result.degraded_sources == ["MT-0001"]
    finally:
        await storage.close()


# --- UC-5.11：单条 INFO 弱信号（不开单，不升级为事件） ---


async def test_uc511_single_info_weak_signal() -> None:
    storage = await make_storage()
    try:
        registry = PluginRegistry().load()
        dc = domain_with(
            [DetectorSpec(signal="INFO", plugin="signature_aggregate", params={"min_count": 5}, severity="warning")]
        )
        ctx = await build_context(
            tenant_id="default", domain="application", registry=registry, storage=storage, now=TS,
            signals=[log_signal(level="INFO", message="hello", ts=TS)], domain_config=dc,
        )
        result = await run_domain(ctx)
        assert result.records == []
        assert result.anomaly_count == 0
    finally:
        await storage.close()
