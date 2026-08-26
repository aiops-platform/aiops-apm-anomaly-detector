"""L2 关联：``_within_window`` / ``_change_within_window`` / ``template_summary`` / ``l2_correlate`` 纯函数。"""

from datetime import datetime, timedelta, timezone

from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly
from aiops_apm.models.config import CorrelationSpec, DomainConfig, VerifySpec
from aiops_apm.models.signal import ChangeSignal
from aiops_apm.pipeline.context import DetectionContext
from aiops_apm.pipeline.l2_correlate import _change_within_window, _within_window, l2_correlate, template_summary
from aiops_apm.plugins.registry import PluginRegistry
from aiops_apm.storage.detection_state import InMemoryDetectionStateStore
from aiops_apm.storage.records import InMemoryRecordStore
from aiops_apm.storage.sequence import InMemorySequenceStore

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def ma(*, service="svc-a", metric="cpu_usage", value=0.95, ts=TS, severity="high") -> MetricAnomaly:
    return MetricAnomaly(
        service=service, metric=metric, value=value, method="static_threshold", severity=severity, detected_at=ts
    )


def la(*, service="svc-a", sig="java.lang.OOMError", count=47, ts=TS, severity="warning") -> LogAnomaly:
    return LogAnomaly(
        service=service, level="ERROR", signature=sig, pattern="oom", count=count,
        first_seen=ts, severity=severity, detected_at=ts,
    )


def make_ctx(*, anomalies=None, changes=None, domain_config=None, state_store=None, fpr=None) -> DetectionContext:
    dc = domain_config or DomainConfig(detectors=[], correlation=CorrelationSpec(), verify=VerifySpec())
    return DetectionContext(
        domain="application",
        domain_config=dc,
        registry=PluginRegistry(),
        storage=InMemoryRecordStore(),
        state_store=state_store or InMemoryDetectionStateStore(),
        sequence_store=InMemorySequenceStore(),
        now=TS,
        anomalies=list(anomalies or []),
        changes=list(changes or []),
        fpr=fpr or {},
    )


# --- _within_window ---


def test_within_window_hit() -> None:
    assert _within_window([ma(ts=TS)], [la(ts=TS)], 300) is True


def test_within_window_miss() -> None:
    assert _within_window([ma(ts=TS)], [la(ts=TS + timedelta(seconds=301))], 300) is False


def test_within_window_metric_only_false() -> None:
    assert _within_window([ma()], [], 300) is False


# --- _change_within_window ---


def test_change_within_window_hit() -> None:
    c = ChangeSignal(service="svc-a", change_id="C-1", type="deployment", summary="v2 deploy", timestamp=TS)
    ok, recent = _change_within_window([c], [ma()], 300)
    assert ok is True
    assert recent is not None
    assert recent["change_id"] == "C-1"
    assert recent["summary"] == "v2 deploy"


def test_change_within_window_miss() -> None:
    c = ChangeSignal(
        service="svc-a", change_id="C-1", type="deployment", summary="v2", timestamp=TS + timedelta(seconds=600)
    )
    ok, recent = _change_within_window([c], [ma()], 300)
    assert ok is False
    assert recent is None


def test_change_within_window_wrong_service() -> None:
    c = ChangeSignal(service="svc-b", change_id="C-1", type="deployment", summary="v2", timestamp=TS)
    ok, recent = _change_within_window([c], [ma(service="svc-a")], 300)
    assert ok is False
    assert recent is None


# --- template_summary ---


def test_template_summary_metric_and_log() -> None:
    out = template_summary([ma(metric="cpu_usage", value=0.95)], [la(sig="java.lang.OOMError", count=47)])
    assert out == "svc-a cpu_usage 0.95；svc-a java.lang.OOMError x47"


# --- l2_correlate ---


async def test_l2_correlate_metric_log_within_window() -> None:
    ctx = make_ctx(
        anomalies=[ma(), la()],
        domain_config=DomainConfig(
            detectors=[], correlation=CorrelationSpec(metric_log_window_sec=300, change_window_sec=300)
        ),
    )
    corr = await l2_correlate(ctx)
    entry = corr["svc-a"]
    assert entry[0].related is True
    assert entry[0].reason == "metric_log_within_window"
    assert entry[1] is False
    assert entry[2] is None


async def test_l2_correlate_metric_only() -> None:
    ctx = make_ctx(anomalies=[ma()])
    corr = await l2_correlate(ctx)
    assert corr["svc-a"][0].related is False
    assert corr["svc-a"][0].reason == "metric_only"


async def test_l2_correlate_log_only() -> None:
    ctx = make_ctx(anomalies=[la()])
    corr = await l2_correlate(ctx)
    assert corr["svc-a"][0].related is False
    assert corr["svc-a"][0].reason == "log_only"


async def test_l2_correlate_change_related() -> None:
    c = ChangeSignal(service="svc-a", change_id="C-9", type="deployment", summary="v3", timestamp=TS)
    ctx = make_ctx(anomalies=[ma()], changes=[c])
    corr = await l2_correlate(ctx)
    entry = corr["svc-a"]
    assert entry[1] is True
    assert entry[2]["change_id"] == "C-9"
