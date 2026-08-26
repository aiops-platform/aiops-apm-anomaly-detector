"""内置检测器：静态阈值（static_threshold）。

设计文档 §6.4：``value > threshold``（可配 operator）；RANGE 为「区间外命中」。
"""

from enum import Enum
from typing import Any

from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.signal import MetricSignal
from aiops_apm.plugins.base import Detector


class Operator(str, Enum):
    """阈值比较算子。"""

    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    RANGE = "range"


class StaticThresholdDetector(Detector):
    """静态阈值检测器：指标值按 operator 越过 threshold → MetricAnomaly。"""

    name = "static_threshold"

    async def detect(self, signals: list[Any], params: dict) -> list[Any]:
        threshold = params["threshold"]
        operator = Operator(params.get("operator", "gt"))
        anomalies: list[Any] = []
        for s in signals:
            if not isinstance(s, MetricSignal):
                continue
            hit = False
            if operator == Operator.GT:
                hit = s.value > threshold
            elif operator == Operator.GTE:
                hit = s.value >= threshold
            elif operator == Operator.LT:
                hit = s.value < threshold
            elif operator == Operator.LTE:
                hit = s.value <= threshold
            elif operator == Operator.RANGE:
                lo, hi = params.get("range", [threshold, threshold])
                hit = not (lo <= s.value <= hi)
            if hit:
                anomalies.append(
                    MetricAnomaly(
                        kind="metric",
                        tenant_id=s.tenant_id,
                        service=s.service,
                        metric=s.metric,
                        value=s.value,
                        method=self.name,
                        severity=params.get("severity", "warning"),
                        detected_at=s.timestamp,
                        labels=s.labels,
                    )
                )
        return anomalies


def build(*, http: Any = None, pool: Any = None, settings: Any = None) -> StaticThresholdDetector:
    return StaticThresholdDetector()
