"""内置 detector 插件（M4：static_threshold / simple_compare / signature_aggregate）。

设计文档 §6.4 三个 v1 检测方法；骨架见 Enhanced plan M4 小节。
"""

from datetime import datetime

import pytest

from aiops_apm.detectors.signature_aggregate import SignatureAggregateDetector
from aiops_apm.detectors.simple_compare import SimpleCompareDetector
from aiops_apm.detectors.static_threshold import Operator, StaticThresholdDetector
from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly
from aiops_apm.models.signal import LogSignal, MetricSignal

TS = datetime(2026, 8, 26, 12, 0, 0)

STACK = (
    "OutOfMemoryError: heap space\n"
    "  at com.app.Service.method(Service.java:42)\n"
    "  at com.app.Main.main(Main.java:1)"
)


def ms(*, service="svc-a", metric="cpu_usage", value=0.95, labels=None) -> MetricSignal:
    return MetricSignal(service=service, metric=metric, value=value, timestamp=TS, labels=labels or {})


def ls(*, service="svc-a", level="ERROR", message="boom", stack_trace=None, timestamp=TS, signature=None) -> LogSignal:
    return LogSignal(
        service=service, level=level, message=message, stack_trace=stack_trace, timestamp=timestamp, signature=signature
    )


# --- static_threshold ---


def test_operator_enum_values():
    assert Operator.GT.value == "gt"
    assert Operator.GTE.value == "gte"
    assert Operator.LT.value == "lt"
    assert Operator.LTE.value == "lte"
    assert Operator.RANGE.value == "range"


@pytest.mark.asyncio
async def test_static_threshold_gt_hit():
    out = await StaticThresholdDetector().detect([ms(value=0.95)], {"threshold": 0.9})
    assert len(out) == 1
    a = out[0]
    assert isinstance(a, MetricAnomaly)
    assert a.metric == "cpu_usage" and a.value == 0.95
    assert a.method == "static_threshold"
    assert a.severity == "warning"  # 默认
    assert a.anomaly_key()


@pytest.mark.asyncio
async def test_static_threshold_gt_no_hit_on_equal():
    assert await StaticThresholdDetector().detect([ms(value=0.9)], {"threshold": 0.9}) == []


@pytest.mark.asyncio
async def test_static_threshold_gte_hits_equal():
    out = await StaticThresholdDetector().detect([ms(value=0.9)], {"threshold": 0.9, "operator": "gte"})
    assert len(out) == 1


@pytest.mark.asyncio
async def test_static_threshold_lt():
    out = await StaticThresholdDetector().detect([ms(value=0.1)], {"threshold": 0.2, "operator": "lt"})
    assert len(out) == 1 and out[0].value == 0.1


@pytest.mark.asyncio
async def test_static_threshold_lte_hits_equal():
    out = await StaticThresholdDetector().detect([ms(value=0.2)], {"threshold": 0.2, "operator": "lte"})
    assert len(out) == 1


@pytest.mark.asyncio
async def test_static_threshold_range_inside_no_hit():
    det = StaticThresholdDetector()
    assert await det.detect([ms(value=0.5)], {"threshold": 0.2, "operator": "range", "range": [0.2, 0.8]}) == []


@pytest.mark.asyncio
async def test_static_threshold_range_above_hit():
    out = await StaticThresholdDetector().detect(
        [ms(value=0.95)], {"threshold": 0.2, "operator": "range", "range": [0.2, 0.8]}
    )
    assert len(out) == 1


@pytest.mark.asyncio
async def test_static_threshold_range_below_hit():
    out = await StaticThresholdDetector().detect(
        [ms(value=0.05)], {"threshold": 0.2, "operator": "range", "range": [0.2, 0.8]}
    )
    assert len(out) == 1


@pytest.mark.asyncio
async def test_static_threshold_range_default_range_is_threshold():
    # range 缺省 = [threshold, threshold]，value 恰等于 threshold → 区间内 → 不命中
    out = await StaticThresholdDetector().detect([ms(value=0.9)], {"threshold": 0.9, "operator": "range"})
    assert out == []


@pytest.mark.asyncio
async def test_static_threshold_skips_non_metric():
    sigs = [ls(level="ERROR"), ms(value=0.95)]
    out = await StaticThresholdDetector().detect(sigs, {"threshold": 0.9})
    assert len(out) == 1 and isinstance(out[0], MetricAnomaly)


@pytest.mark.asyncio
async def test_static_threshold_severity_and_labels():
    out = await StaticThresholdDetector().detect(
        [ms(value=0.95, labels={"env": "prod"})], {"threshold": 0.9, "severity": "critical"}
    )
    assert out[0].severity == "critical"
    assert out[0].labels == {"env": "prod"}


# --- simple_compare ---


@pytest.mark.asyncio
async def test_simple_compare_hit():
    out = await SimpleCompareDetector().detect([ms(metric="error_rate", value=0.04)], {"ratio": 1.5, "baseline": 0.02})
    assert len(out) == 1
    a = out[0]
    assert a.baseline == 0.02
    assert a.method == "simple_compare"
    assert isinstance(a, MetricAnomaly)


@pytest.mark.asyncio
async def test_simple_compare_below_ratio_no_hit():
    # 0.02 < 0.02 * 1.5 = 0.03
    out = await SimpleCompareDetector().detect([ms(value=0.02)], {"ratio": 1.5, "baseline": 0.02})
    assert out == []


@pytest.mark.asyncio
async def test_simple_compare_default_ratio():
    # 默认 ratio 1.5：16 > 10 * 1.5 = 15
    out = await SimpleCompareDetector().detect([ms(value=16)], {"baseline": 10})
    assert len(out) == 1


@pytest.mark.asyncio
async def test_simple_compare_no_baseline_no_anomaly():
    out = await SimpleCompareDetector().detect([ms(value=16)], {"ratio": 1.5})
    assert out == []


@pytest.mark.asyncio
async def test_simple_compare_zero_baseline_anomaly():
    # baseline=0 且 value > 0 → 命中（baseline is not None 判断，非 truthy 判断）
    out = await SimpleCompareDetector().detect([ms(value=0.95)], {"ratio": 1.5, "baseline": 0.0})
    assert len(out) == 1


@pytest.mark.asyncio
async def test_simple_compare_skips_non_metric():
    sigs = [ls(), ms(value=0.04)]
    out = await SimpleCompareDetector().detect(sigs, {"ratio": 1.5, "baseline": 0.02})
    assert len(out) == 1


# --- signature_aggregate ---


@pytest.mark.asyncio
async def test_signature_aggregate_groups_and_counts():
    det = SignatureAggregateDetector()
    logs = [ls(stack_trace=STACK) for _ in range(3)]
    out = await det.detect(logs, {"min_count": 3})
    assert len(out) == 1
    a = out[0]
    assert isinstance(a, LogAnomaly)
    assert a.count == 3
    assert a.signature == "OutOfMemoryError|at com.app.Service.method|at com.app.Main.main"


@pytest.mark.asyncio
async def test_signature_aggregate_below_min_count():
    logs = [ls(stack_trace=STACK) for _ in range(2)]
    assert await SignatureAggregateDetector().detect(logs, {"min_count": 3}) == []


@pytest.mark.asyncio
async def test_signature_aggregate_uses_precomputed_signature():
    logs = [ls(message="boom", signature="custom-sig") for _ in range(3)]
    out = await SignatureAggregateDetector().detect(logs, {"min_count": 3})
    assert out[0].signature == "custom-sig"


@pytest.mark.asyncio
async def test_signature_aggregate_groups_by_distinct_signature():
    logs = [ls(stack_trace=STACK) for _ in range(3)] + [
        ls(stack_trace="NullPointerException\n  at com.app.X.foo(X.java:1)") for _ in range(3)
    ]
    out = await SignatureAggregateDetector().detect(logs, {"min_count": 3})
    assert len(out) == 2


@pytest.mark.asyncio
async def test_signature_aggregate_first_seen_and_detected_at():
    t1, t2, t3 = (
        datetime(2026, 8, 26, 12, 0, 1),
        datetime(2026, 8, 26, 12, 0, 2),
        datetime(2026, 8, 26, 12, 0, 3),
    )
    logs = [ls(stack_trace=STACK, timestamp=t1), ls(stack_trace=STACK, timestamp=t2), ls(stack_trace=STACK, timestamp=t3)]
    out = await SignatureAggregateDetector().detect(logs, {"min_count": 3})
    assert out[0].first_seen == t1
    assert out[0].detected_at == t3


@pytest.mark.asyncio
async def test_signature_aggregate_skips_non_log():
    sigs = [ms(), ls(stack_trace=STACK), ls(stack_trace=STACK), ls(stack_trace=STACK)]
    out = await SignatureAggregateDetector().detect(sigs, {"min_count": 3})
    assert len(out) == 1


@pytest.mark.asyncio
async def test_signature_aggregate_severity_default_and_override():
    logs = [ls(stack_trace=STACK) for _ in range(3)]
    assert (await SignatureAggregateDetector().detect(logs, {"min_count": 3}))[0].severity == "warning"
    out = await SignatureAggregateDetector().detect(logs, {"min_count": 3, "severity": "high"})
    assert out[0].severity == "high"
