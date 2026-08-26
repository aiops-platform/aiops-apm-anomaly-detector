"""M6 L2 摘要钩子：模板兜底 + 可插拔 provider（emit 注入）。"""

from datetime import datetime, timezone

from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.config import DomainConfig, VerifySpec
from aiops_apm.models.record import Correlation, Verification
from aiops_apm.pipeline.context import DetectionContext
from aiops_apm.pipeline.emit import emit
from aiops_apm.plugins.registry import PluginRegistry
from aiops_apm.settings import Settings
from aiops_apm.storage.detection_state import InMemoryDetectionStateStore
from aiops_apm.storage.records import InMemoryRecordStore
from aiops_apm.storage.sequence import InMemorySequenceStore
from aiops_apm.summary import TemplateSummaryProvider, build_summary_provider

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def _anom() -> MetricAnomaly:
    return MetricAnomaly(
        service="svc-a", metric="cpu_usage", value=0.95, method="static_threshold", severity="high", detected_at=TS
    )


def _ctx(*, provider=None) -> DetectionContext:
    return DetectionContext(
        domain="application",
        domain_config=DomainConfig(detectors=[], verify=VerifySpec(persistence_rounds=1)),
        registry=PluginRegistry(),
        storage=InMemoryRecordStore(),
        state_store=InMemoryDetectionStateStore(),
        sequence_store=InMemorySequenceStore(),
        now=TS,
        summary_provider=provider,
    )


async def _emit(ctx: DetectionContext) -> list:
    return await emit(
        ctx,
        service="svc-a",
        anomalies=[_anom()],
        correlation=Correlation(related=False, reason="metric_only"),
        change_related=False,
        recent_change=None,
        verification=Verification(passed=True, persistence_ok=True, final_severity="high"),
    )


class FakeProvider:
    """测试可插拔 provider：确定性返回服务名标记。"""

    name = "fake"

    def summarize(self, *, service: str, metric_anoms: list, log_anoms: list) -> str:
        return f"FAKE:{service}"


def test_template_summary_deterministic() -> None:
    p = TemplateSummaryProvider()
    s1 = p.summarize(service="svc-a", metric_anoms=[_anom()], log_anoms=[])
    s2 = p.summarize(service="svc-a", metric_anoms=[_anom()], log_anoms=[])
    assert s1 == s2
    assert "cpu_usage" in s1 and "0.95" in s1


def test_build_summary_provider_always_template_for_now() -> None:
    settings = Settings(_env_file=None, storage_backend="memory", enable_llm_summary=True)
    p = build_summary_provider(settings)
    assert isinstance(p, TemplateSummaryProvider)


async def test_emit_defaults_to_template() -> None:
    recs = await _emit(_ctx())
    assert len(recs) == 1
    assert "cpu_usage" in recs[0].symptom["summary"]


async def test_emit_uses_injected_provider() -> None:
    recs = await _emit(_ctx(provider=FakeProvider()))
    assert len(recs) == 1
    assert recs[0].symptom["summary"] == "FAKE:svc-a"
