"""``run_domain``：一个 ``(tenant_id, domain)`` 内一轮检测的串行编排。

``collect(已在 ctx.signals) → L0 抑制 → L1 检测 → L2 关联 → 按 service L3/emit → sweep(miss) → DomainResult``。
M5 不跑 collect（ctx.signals 由调用方/测试预填）；scheduler 编排属 M6。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from aiops_apm.pipeline.context import DetectionContext, DomainResult
from aiops_apm.pipeline.emit import emit
from aiops_apm.pipeline.grouping import group_anomalies, group_services, representative_service
from aiops_apm.pipeline.l0_suppress import l0_suppress
from aiops_apm.pipeline.l1_detect import l1_detect
from aiops_apm.pipeline.l2_correlate import l2_correlate
from aiops_apm.pipeline.l3_verify import l3_verify


def _signal_summary(signal: Any) -> str:
    """信号 JSON 安全摘要（UC-7.2 审计 timeline 用，截断防超长）。"""
    kind = getattr(signal, "kind", "unknown")
    if kind == "log":
        return f"log:{getattr(signal, 'service', '?')}:{getattr(signal, 'signature', '') or getattr(signal, 'level', '')}"[:200]
    return f"metric:{getattr(signal, 'service', '?')}:{getattr(signal, 'metric', '?')}"[:200]


async def run_domain(ctx: DetectionContext) -> DomainResult:
    """串行执行一轮漏斗，返回单轮结果与 timeline。"""
    ctx.round_started_at = ctx.now
    timeline = [{"step": "collect_done", "ts": ctx.now, "count": len(ctx.signals)}]

    await l0_suppress(ctx)
    timeline.append(
        {
            "step": "suppressed",
            "ts": ctx.now,
            "count": len(ctx.suppressed),
            "details": [
                {
                    "signal": _signal_summary(item["signal"]),
                    "service": getattr(item["signal"], "service", "unknown"),
                    "suppressor": item["suppressor"],
                    "reason": str(item.get("reason", ""))[:200],
                }
                for item in ctx.suppressed
            ],
        }
    )

    await l1_detect(ctx)
    timeline.append({"step": "detected", "count": len(ctx.anomalies)})

    # M9：按「事故组」出单，取代原先「按 service 一刀切」。分组规则见 pipeline/grouping.py
    # ——同 signature 或同 trace_id 的日志异常归为一组，可跨服务；metric 挂到本服务的日志组。
    groups = group_anomalies(ctx.anomalies)
    timeline.append({"step": "correlated", "services": sorted({a.service for a in ctx.anomalies})})

    # M7 per-target 归因：按 service 计异常/开单/被抑制，供 detection_round_target 回填。
    # 漏斗在合并信号集上跑、键是 service（信号不带 target_id），多 target 共用 service 时共享计数。
    anomalies_by_service: dict[str, int] = defaultdict(int)
    for a in ctx.anomalies:
        anomalies_by_service[a.service] += 1
    suppressed_by_service: dict[str, int] = defaultdict(int)
    for item in ctx.suppressed:
        suppressed_by_service[getattr(item["signal"], "service", "unknown")] += 1

    records: list[Any] = []
    records_by_service: dict[str, int] = defaultdict(int)
    for group in groups:
        services = group_services(group)
        # 组代表：既进 group_key 的去重键，也用于 fpr 键。两处必须是同一个值，
        # 否则误报率闸门读不到已写入的条目（见 l3_verify 的说明）。
        rep = representative_service(group)
        corr, change_related, recent_change = await l2_correlate(ctx, group)
        # M6 §13 用例 2：related（指标+日志同源）时组合升 critical 判定依据
        verification = await l3_verify(ctx, rep, group, related=corr.related)
        emitted = await emit(
            ctx,
            ",".join(services),  # 对外展示：组内全部服务（排序后拼接）
            group,
            corr,
            change_related,
            recent_change,
            verification,
            group_key_service=rep,
        )
        records.extend(emitted)
        # 归因用 **+=** 而非赋值：同一 service 可能有多个组（多类 error 各自成单），
        # 赋值会让后一组覆盖前一组。
        # 语义说明：这是「与该 target 的 service 相关的记录数」，跨服务组会给组内每个
        # 服务各记一次，故其总和 **可以大于** round 级 record_count（后者是记录条数）——
        # 两者量纲不同，不是矛盾。
        for svc in services:
            records_by_service[svc] += len(emitted)

    await ctx.state_store.sweep(ctx.tenant_id, ctx.domain, ctx.seen_keys)  # miss 计数（UC-5.6）
    timeline.append({"step": "record_created", "count": len(records)})

    return DomainResult(
        domain=ctx.domain,
        records=records,
        suppressed_count=len(ctx.suppressed),
        anomaly_count=len(ctx.anomalies),
        degraded_sources=list(ctx.degraded_sources),
        timeline=timeline,
        anomalies_by_service=dict(anomalies_by_service),
        records_by_service=dict(records_by_service),
        suppressed_by_service=dict(suppressed_by_service),
    )
