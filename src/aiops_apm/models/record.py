"""问题单模型：M5 emit 的产出、M2 落库 ``problem_record`` 表的记录。

契约在 M1 冻结，之后只允许增加可选字段。
"""

from datetime import datetime

from pydantic import BaseModel, Field

from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly


class Correlation(BaseModel):
    """L2 关联结果。"""

    related: bool
    reason: str


class Verification(BaseModel):
    """L3 验证结果。"""

    passed: bool
    persistence_ok: bool
    resample_ok: bool = True
    false_positive_rate: float = 0.0
    final_severity: str


class ProblemRecord(BaseModel):
    """最终落库到 ``problem_record`` 表的问题单。"""

    record_id: str
    source: str = "apm-alert"
    tenant_id: str = "default"
    domain: str
    state: str = "pending"  # pending / in_progress / resolved / closed / archived
    service: str
    instance: str | None = None
    severity: str = "warning"  # warning / high / critical
    detected_at: datetime
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    occurrence_count: int = 1
    resolved_at: datetime | None = None
    resolve_reason: str | None = None
    symptom: dict
    metric_anomalies: list[MetricAnomaly]
    log_anomalies: list[LogAnomaly]
    correlation: Correlation
    change_related: bool = False
    recent_change: dict | None = None
    verification: Verification
    evidence: list[dict] = Field(default_factory=list)
    trace_id: str | None = None

    @property
    def group_key(self) -> str:
        """转发到 fingerprint.group_key（去重/持续性真源）。"""
        from .fingerprint import group_key

        all_anoms = self.metric_anomalies + self.log_anomalies
        return group_key(self.tenant_id, self.domain, self.service, all_anoms)
