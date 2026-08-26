"""L3 验证：``calibrate_severity`` / fpr 闸门（降级不丢弃）/ persistence 门。"""

from datetime import datetime, timezone

from aiops_apm.models import fingerprint
from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly
from aiops_apm.models.config import DomainConfig, VerifySpec
from aiops_apm.pipeline.context import DetectionContext
from aiops_apm.pipeline.l3_verify import calibrate_severity, l3_verify
from aiops_apm.plugins.registry import PluginRegistry
from aiops_apm.storage.detection_state import InMemoryDetectionStateStore
from aiops_apm.storage.records import InMemoryRecordStore
from aiops_apm.storage.sequence import InMemorySequenceStore

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def ma(*, severity="high", metric="cpu_usage") -> MetricAnomaly:
    return MetricAnomaly(
        service="svc-a", metric=metric, value=0.95, method="static_threshold", severity=severity, detected_at=TS
    )


def la(*, severity="warning") -> LogAnomaly:
    return LogAnomaly(
        service="svc-a", level="ERROR", signature="java.lang.OOMError", pattern="oom", count=47,
        first_seen=TS, severity=severity, detected_at=TS,
    )


def make_ctx(*, domain_config=None, state_store=None, fpr=None) -> DetectionContext:
    dc = domain_config or DomainConfig(detectors=[], verify=VerifySpec(persistence_rounds=1))
    return DetectionContext(
        domain="application",
        domain_config=dc,
        registry=PluginRegistry(),
        storage=InMemoryRecordStore(),
        state_store=state_store or InMemoryDetectionStateStore(),
        sequence_store=InMemorySequenceStore(),
        now=TS,
        fpr=fpr or {},
    )


# --- calibrate_severity（纯函数） ---


def test_calibrate_severity_takes_max() -> None:
    assert calibrate_severity([la(severity="warning"), ma(severity="high")]) == "high"
    assert calibrate_severity([ma(severity="high"), ma(severity="critical")]) == "critical"
    assert calibrate_severity([ma(severity="warning")]) == "warning"


def test_calibrate_severity_empty_warning() -> None:
    assert calibrate_severity([]) == "warning"


# --- persistence 门（increment-first：连续 N 轮第 N 轮开单） ---


async def test_l3_persistence_gate_two_rounds() -> None:
    s = InMemoryDetectionStateStore()
    ctx = make_ctx(state_store=s, domain_config=DomainConfig(detectors=[], verify=VerifySpec(persistence_rounds=2)))
    v1 = await l3_verify(ctx, "svc-a", [ma()])
    assert v1.passed is False
    assert v1.persistence_ok is False
    v2 = await l3_verify(ctx, "svc-a", [ma()])
    assert v2.passed is True
    assert v2.persistence_ok is True
    assert v2.final_severity == "high"


async def test_l3_persistence_rounds_one_opens_first_round() -> None:
    s = InMemoryDetectionStateStore()
    ctx = make_ctx(state_store=s, domain_config=DomainConfig(detectors=[], verify=VerifySpec(persistence_rounds=1)))
    v = await l3_verify(ctx, "svc-a", [ma()])
    assert v.passed is True


# --- fpr 闸门（降级不丢弃） ---


async def test_l3_fpr_downgrades_to_warning_but_passes() -> None:
    anoms = [ma(severity="high")]
    gk = fingerprint.group_key("default", "application", "svc-a", anoms)
    ctx = make_ctx(
        domain_config=DomainConfig(
            detectors=[], verify=VerifySpec(persistence_rounds=1, false_positive_threshold=0.6, min_samples=20)
        ),
        fpr={gk: {"fpr": 0.9, "total": 50}},
    )
    v = await l3_verify(ctx, "svc-a", anoms)
    assert v.passed is True  # 不永久静默
    assert v.final_severity == "warning"  # 降级
    assert v.false_positive_rate == 0.9


async def test_l3_fpr_ok_keeps_severity() -> None:
    anoms = [ma(severity="high")]
    gk = fingerprint.group_key("default", "application", "svc-a", anoms)
    ctx = make_ctx(
        domain_config=DomainConfig(
            detectors=[], verify=VerifySpec(persistence_rounds=1, false_positive_threshold=0.6, min_samples=20)
        ),
        fpr={gk: {"fpr": 0.2, "total": 50}},
    )
    v = await l3_verify(ctx, "svc-a", anoms)
    assert v.passed is True
    assert v.final_severity == "high"


async def test_l3_fpr_ok_when_total_below_min_samples() -> None:
    anoms = [ma(severity="high")]
    gk = fingerprint.group_key("default", "application", "svc-a", anoms)
    ctx = make_ctx(
        domain_config=DomainConfig(
            detectors=[], verify=VerifySpec(persistence_rounds=1, false_positive_threshold=0.6, min_samples=20)
        ),
        fpr={gk: {"fpr": 0.9, "total": 5}},  # total < min_samples → 样本不足不判误报
    )
    v = await l3_verify(ctx, "svc-a", anoms)
    assert v.passed is True
    assert v.final_severity == "high"


async def test_l3_fpr_missing_entry_ok() -> None:
    ctx = make_ctx(domain_config=DomainConfig(detectors=[], verify=VerifySpec(persistence_rounds=1)))
    v = await l3_verify(ctx, "svc-a", [ma(severity="high")])
    assert v.passed is True
    assert v.final_severity == "high"
