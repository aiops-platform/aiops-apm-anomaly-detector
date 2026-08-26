"""Poller：一组 ``(tenant_id, domain)`` 目标的一轮采集编排（M6 UC-6.1，M7 加轮次审计/metrics）。

``run_round`` 把同域目标并行采集，单个目标失败降级为 ``degraded_sources`` 标记
（M5 遗留「degraded_sources 产生」在此落地），再把合并后的 signals 喂给
``build_context → run_domain`` 走确定性漏斗。

M7（UC-7.1/7.2）：每轮写入 ``detection_round``（create running → update success/partial/failed），
收尾 ``record_round_metrics`` 打点；``run_domain`` 异常 → 记 failed + 审计 + re-raise
（保留 scheduler/alerts 调用方行为）。

采集器 duck-type 只用 ``ctx.tenant_id``/``ctx.watermark_store``/``ctx.snapshot_store``
（M3 已冻结），因此这里用窄 ``CollectContext``，不构造完整 ``DetectionContext``。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from aiops_apm.audit import SecurityAudit
from aiops_apm.collectors import CollectContext, collector_for
from aiops_apm.metrics import record_round_metrics
from aiops_apm.pipeline.context import DetectionContext, DomainResult, build_context, new_trace_id
from aiops_apm.pipeline.runner import run_domain
from aiops_apm.storage import Storage


async def run_round(
    *,
    registry: Any,
    storage: Storage,
    tenant_id: str,
    domain: str,
    targets: list,
    now: Any,
    http: Any = None,
    settings: Any = None,
    summary_provider: object | None = None,
) -> DomainResult:
    """并行采集 ``targets`` 并入漏斗，返回 ``DomainResult``。

    - 单个 target 采集异常 → 记入 ``degraded_sources``，不拖垮整轮（UC-5.6 日志源降级）。
    - ``summary_provider`` 为 None 时 emit 走确定性模板。
    - 每轮写 ``detection_round``（round_id = trace_id）并打点（UC-7.1/7.2）。
    """
    trace_id = new_trace_id()
    rounds = storage.rounds
    await rounds.create_round(
        tenant_id,
        trace_id,
        domain,
        started_at=now,
        target_ids=[str(t.get("target_id", "unknown")) for t in targets],
    )
    perf_start = time.perf_counter()

    collect_ctx = CollectContext(
        tenant_id=tenant_id,
        watermark_store=storage.watermarks,
        snapshot_store=storage.snapshots,
    )
    degraded: list[str] = []

    async def _one(target: dict) -> list:
        try:
            collector = collector_for(target, http=http, settings=settings)
            return await collector.collect(collect_ctx, target)
        except Exception:
            degraded.append(str(target.get("target_id", "unknown")))
            return []

    results = await asyncio.gather(*(_one(t) for t in targets))
    signals = [s for batch in results for s in batch]

    ctx: DetectionContext = await build_context(
        tenant_id=tenant_id,
        domain=domain,
        registry=registry,
        storage=storage,
        now=now,
        trace_id=trace_id,
        signals=signals,
        degraded_sources=degraded,
        summary_provider=summary_provider,
    )
    try:
        result = await run_domain(ctx)
    except Exception as exc:  # noqa: BLE001 -- 记录 failed 轮次后仍向上抛，保留调用方语义
        duration = time.perf_counter() - perf_start
        await rounds.update_status(
            tenant_id, trace_id, "failed",
            ended_at=datetime.now(timezone.utc),
            degraded_sources=degraded,
        )
        record_round_metrics(domain=domain, tenant_id=tenant_id, status="failed", duration_sec=duration)
        SecurityAudit.log_round_event(
            tenant_id, trace_id, domain, "failed", detail=f"{type(exc).__name__}: {exc}"
        )
        raise

    duration = time.perf_counter() - perf_start
    status = "partial" if degraded else "success"
    await rounds.update_status(
        tenant_id, trace_id, status,
        ended_at=datetime.now(timezone.utc),
        timeline=result.timeline,
        signals_count=len(signals),
        anomaly_count=result.anomaly_count,
        record_count=len(result.records),
        suppressed_count=result.suppressed_count,
        degraded_sources=degraded,
    )
    record_round_metrics(domain=domain, tenant_id=tenant_id, status=status, duration_sec=duration, result=result)
    if degraded:
        SecurityAudit.log_round_event(
            tenant_id, trace_id, domain, "partial", detail=f"degraded_sources={degraded}"
        )
    return result
