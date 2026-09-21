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
    state: str = "pending"  # pending / in_progress / resolved / closed / archived / escalated
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
    # M9 新增可选字段：``group_key`` 的 service 段。跨服务合并后 ``service`` 是拼接串
    # （如 "gateway-service,order-service"），若直接拿它算 group_key，长度会溢出
    # ``group_key``/``open_group_key`` 生成列/唯一索引的 VARCHAR(255)，三处都得加宽。
    # 这里存**排序后的第一个服务名**（组代表），group_key 的长度特性完全不变。
    # 未设置时回退 ``service``（单服务组的既有行为，含历史数据）。
    group_key_service: str | None = None

    @property
    def group_key(self) -> str:
        """转发到 fingerprint.group_key（去重/持续性真源）。

        service 段优先用 ``group_key_service``（M9 跨服务组），否则用 ``service``。
        必须是**确定性且跨轮稳定**的值——否则去重键每轮都变，同一问题会重复开单，
        且 ``fpr_table`` 的条目成孤儿、误报率闸门静默失效。
        """
        from .fingerprint import group_key

        all_anoms = self.metric_anomalies + self.log_anomalies
        return group_key(self.tenant_id, self.domain, self.group_key_service or self.service, all_anoms)
