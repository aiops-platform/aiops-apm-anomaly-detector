"""第三方检测器示例（UC-7.4）：latency_p95 超阈值告警。

演示「可插拔规则」：独立 pip 包，注册到 ``aiops_apm.detectors`` entry_points，
安装后 PluginRegistry 即发现，无需改核心代码。契约跟随 ``plugins/base.py`` 的
``Detector`` 接口（M1 冻结）。
"""

from __future__ import annotations

from typing import Any

from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.signal import MetricSignal
from aiops_apm.plugins.base import Detector


class P95LatencyDetector(Detector):
    """latency_p95 指标值 > 阈值（毫秒）→ MetricAnomaly（high）。"""

    name = "p95_latency"

    async def detect(self, signals: list[Any], params: dict) -> list[Any]:
        threshold_ms = float(params.get("threshold_ms", 200.0))
        anomalies: list[Any] = []
        for s in signals:
            if not isinstance(s, MetricSignal):
                continue
            if s.metric != "latency_p95":
                continue
            if s.value > threshold_ms:
                anomalies.append(
                    MetricAnomaly(
                        kind="metric",
                        tenant_id=s.tenant_id,
                        service=s.service,
                        metric=s.metric,
                        value=s.value,
                        method=self.name,
                        severity=params.get("severity", "high"),
                        detected_at=s.timestamp,
                        labels=s.labels,
                    )
                )
        return anomalies


def build(*, http: Any = None, pool: Any = None, settings: Any = None) -> P95LatencyDetector:
    return P95LatencyDetector()
