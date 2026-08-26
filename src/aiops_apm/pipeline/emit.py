"""emit：组装 ``ProblemRecord`` 并经 ``write_or_append`` 原子去重落库（M2 实现）。

确定性纯函数：L3 未通过（persistence/fpr）不开单；通过则取号、组装、写 ``problem_record``。
degraded 源以 ``evidence`` 标记（UC-5.10）。
"""

from __future__ import annotations

from typing import Any

from aiops_apm.models.record import ProblemRecord
from aiops_apm.pipeline.l2_correlate import template_summary


async def emit(
    ctx: Any,
    service: str,
    anomalies: list,
    correlation: Any,
    change_related: bool,
    recent_change: dict | None,
    verification: Any,
) -> list:
    """产出 ``[ProblemRecord]``；verification 未通过返回 ``[]``。"""
    if not verification.passed:
        return []
    metric_anoms = [a for a in anomalies if a.kind == "metric"]
    log_anoms = [a for a in anomalies if a.kind == "log"]
    evidence: list[dict] = []
    if ctx.degraded_sources:
        evidence.append({"type": "degraded", "target_ids": list(ctx.degraded_sources)})
    rec = ProblemRecord(
        record_id=await ctx.sequence_store.next_id(ctx.domain),
        tenant_id=ctx.tenant_id,
        domain=ctx.domain,
        state="pending",
        service=service,
        severity=verification.final_severity,
        detected_at=ctx.now,
        first_seen_at=ctx.now,
        last_seen_at=ctx.now,
        occurrence_count=1,
        symptom={"summary": template_summary(metric_anoms, log_anoms)},
        metric_anomalies=metric_anoms,
        log_anomalies=log_anoms,
        correlation=correlation,
        change_related=change_related,
        recent_change=recent_change,
        verification=verification,
        evidence=evidence,
        trace_id=ctx.trace_id,
    )
    await ctx.storage.write_or_append(ctx.tenant_id, rec)  # 原子去重（M2 实现，返回 None）
    return [rec]
