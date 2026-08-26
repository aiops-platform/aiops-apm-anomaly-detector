"""内置检测器：环比比较（simple_compare）。

设计文档 §6.4：``value > baseline * ratio``。基线来自 ``params["baseline"]``；
从 signal_snapshot 取滚动均值随 M5 pipeline 注入 ctx 后进化（Enhanced plan 骨架注释掉的 stub）。
"""

from typing import Any

from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.signal import MetricSignal
from aiops_apm.plugins.base import Detector


class SimpleCompareDetector(Detector):
    """环比检测器：当前值超过 基线 * ratio → MetricAnomaly。"""

    name = "simple_compare"

    async def detect(self, signals: list[Any], params: dict) -> list[Any]:
        ratio = params.get("ratio", 1.5)
        baseline = params.get("baseline")
        anomalies: list[Any] = []
        for s in signals:
            if not isinstance(s, MetricSignal):
                continue
            if baseline is not None and s.value > baseline * ratio:
                anomalies.append(
                    MetricAnomaly(
                        kind="metric",
                        tenant_id=s.tenant_id,
                        service=s.service,
                        metric=s.metric,
                        value=s.value,
                        baseline=baseline,
                        method=self.name,
                        severity=params.get("severity", "warning"),
                        detected_at=s.timestamp,
                    )
                )
        return anomalies


def build(*, http: Any = None, pool: Any = None, settings: Any = None) -> SimpleCompareDetector:
    return SimpleCompareDetector()
