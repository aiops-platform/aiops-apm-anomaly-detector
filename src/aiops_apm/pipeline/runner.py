"""``run_domain``：一个 ``(tenant_id, domain)`` 内一轮检测的串行编排。

``collect(已在 ctx.signals) → L0 抑制 → L1 检测 → L2 关联 → 按 service L3/emit → sweep(miss) → DomainResult``。
M5 不跑 collect（ctx.signals 由调用方/测试预填）；scheduler 编排属 M6。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from aiops_apm.pipeline.context import DetectionContext, DomainResult
from aiops_apm.pipeline.emit import emit
from aiops_apm.pipeline.l0_suppress import l0_suppress
from aiops_apm.pipeline.l1_detect import l1_detect
from aiops_apm.pipeline.l2_correlate import l2_correlate
from aiops_apm.pipeline.l3_verify import l3_verify


async def run_domain(ctx: DetectionContext) -> DomainResult:
    """串行执行一轮漏斗，返回单轮结果与 timeline。"""
    ctx.round_started_at = ctx.now
    timeline = [{"step": "collect_done", "ts": ctx.now, "count": len(ctx.signals)}]

    await l0_suppress(ctx)
    timeline.append({"step": "suppressed", "count": len(ctx.suppressed)})

    await l1_detect(ctx)
    timeline.append({"step": "detected", "count": len(ctx.anomalies)})

    correlations = await l2_correlate(ctx)
    timeline.append({"step": "correlated", "services": list(correlations)})

    by_service: dict[str, list] = defaultdict(list)
    for a in ctx.anomalies:
        by_service[a.service].append(a)

    records: list[Any] = []
    for service, anoms in by_service.items():
        corr, change_related, recent_change = correlations[service]
        verification = await l3_verify(ctx, service, anoms)
        records.extend(await emit(ctx, service, anoms, corr, change_related, recent_change, verification))

    await ctx.state_store.sweep(ctx.tenant_id, ctx.domain, ctx.seen_keys)  # miss 计数（UC-5.6）
    timeline.append({"step": "record_created", "count": len(records)})

    return DomainResult(
        domain=ctx.domain,
        records=records,
        suppressed_count=len(ctx.suppressed),
        anomaly_count=len(ctx.anomalies),
        degraded_sources=list(ctx.degraded_sources),
        timeline=timeline,
    )
