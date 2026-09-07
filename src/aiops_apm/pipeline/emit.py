"""emit：组装 ``ProblemRecord`` 并经 ``write_or_append`` 原子去重落库（M2 实现）。

确定性纯函数：L3 未通过（persistence/fpr）不开单；通过则取号、组装、写 ``problem_record``。
degraded 源以 ``evidence`` 标记（UC-5.10）。每条 evidence 携带 ``round_id``（= trace_id）
与该轮 ``detection_round.target_ids``，跨轮 append 后仍可按轮次追溯（evidence → round → target）。
"""

from __future__ import annotations

from typing import Any

from aiops_apm.models.record import ProblemRecord
from aiops_apm.summary import TemplateSummaryProvider


async def _round_target_ids(ctx: Any) -> list[str]:
    """本轮 ``detection_round.target_ids``；无 rounds store 或轮次未落库时返回空。"""
    rounds = getattr(ctx, "rounds_store", None)
    if rounds is None:
        return []
    row = await rounds.get_round(ctx.tenant_id, ctx.trace_id)
    if not row:
        return []
    return list(row.get("target_ids", []) or [])


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
    round_id = ctx.trace_id  # round_id == trace_id（poller 建轮次时二者同一值）
    evidence: list[dict] = []
    if ctx.degraded_sources:
        evidence.append({"type": "degraded", "round_id": round_id, "target_ids": list(ctx.degraded_sources)})
    # 业务 trace/request id 透传（可选）：采集器带 trace_id 时写入 evidence，便于下游按 id 追全链路
    log_trace_ids: list[str] = []
    for a in log_anoms:
        for tid in a.trace_ids:
            if tid not in log_trace_ids:
                log_trace_ids.append(tid)
    if log_trace_ids:
        evidence.append(
            {
                "type": "log_trace_ids",
                "round_id": round_id,
                "target_ids": await _round_target_ids(ctx),
                "trace_ids": log_trace_ids,
                "count": len(log_trace_ids),
            }
        )
    # M6 摘要钩子：ctx.summary_provider 缺省用确定性模板（零 LLM 调用）
    provider = ctx.summary_provider if ctx.summary_provider is not None else TemplateSummaryProvider()
    summary = provider.summarize(service=service, metric_anoms=metric_anoms, log_anoms=log_anoms)
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
        symptom={"summary": summary},
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
