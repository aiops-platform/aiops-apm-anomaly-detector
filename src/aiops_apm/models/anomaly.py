"""异常模型：L1 检测的产出（检测器把 signal 变成 anomaly）。

契约在 M1 冻结，之后只允许增加可选字段。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MetricAnomaly(BaseModel):
    """指标异常。"""

    kind: Literal["metric"] = "metric"
    tenant_id: str = "default"
    service: str
    metric: str
    value: float
    baseline: float | None = None
    method: str  # detector 插件名
    severity: str  # warning / high / critical
    detected_at: datetime
    labels: dict = Field(default_factory=dict)

    def anomaly_key(self) -> str:
        """转发到 fingerprint.anomaly_key（去重真源）。"""
        from .fingerprint import anomaly_key

        return anomaly_key(self)


class LogAnomaly(BaseModel):
    """日志异常（按 signature 聚合）。"""

    kind: Literal["log"] = "log"
    tenant_id: str = "default"
    service: str
    level: str
    signature: str
    pattern: str
    count: int
    first_seen: datetime
    severity: str
    detected_at: datetime | None = None
    # 触发该异常的业务 trace/request id（可选；采集器带 trace_id 时透传，进 problem_record 证据）
    trace_ids: list[str] = Field(default_factory=list)

    def anomaly_key(self) -> str:
        """转发到 fingerprint.anomaly_key（去重真源）。"""
        from .fingerprint import anomaly_key

        return anomaly_key(self)


Anomaly = MetricAnomaly | LogAnomaly
