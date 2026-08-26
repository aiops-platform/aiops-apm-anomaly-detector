"""信号模型：进入系统的原始数据（M3 采集器的产出、L0/L1 的输入）。

契约在 M1 冻结，之后只允许增加可选字段。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MetricSignal(BaseModel):
    """指标信号，如 cpu_usage=0.91。"""

    kind: Literal["metric"] = "metric"
    tenant_id: str = "default"
    service: str
    metric: str
    value: float
    timestamp: datetime
    labels: dict[str, str] = Field(default_factory=dict)


class LogSignal(BaseModel):
    """日志信号。"""

    kind: Literal["log"] = "log"
    tenant_id: str = "default"
    service: str
    level: str
    message: str
    stack_trace: str | None = None
    timestamp: datetime
    trace_id: str | None = None
    # M3 采集器预计算的堆栈签名（L1 signature_aggregate 聚合用；契约允许新增可选字段）
    signature: str | None = None


class ChangeSignal(BaseModel):
    """变更信号（deployment / ddl / config）。"""

    kind: Literal["change"] = "change"
    tenant_id: str = "default"
    service: str
    change_id: str
    type: str  # deployment / ddl / config
    summary: str
    timestamp: datetime


Signal = MetricSignal | LogSignal | ChangeSignal
