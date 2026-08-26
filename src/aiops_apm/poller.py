"""Poller：一组 ``(tenant_id, domain)`` 目标的一轮采集编排（M6 UC-6.1）。

``run_round`` 把同域目标并行采集，单个目标失败降级为 ``degraded_sources`` 标记
（M5 遗留「degraded_sources 产生」在此落地），再把合并后的 signals 喂给
``build_context → run_domain`` 走确定性漏斗。

采集器 duck-type 只用 ``ctx.tenant_id``/``ctx.watermark_store``/``ctx.snapshot_store``
（M3 已冻结），因此这里用窄 ``CollectContext``，不构造完整 ``DetectionContext``。
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiops_apm.collectors import CollectContext, collector_for
from aiops_apm.pipeline.context import DetectionContext, DomainResult, build_context
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
    """
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
        signals=signals,
        degraded_sources=degraded,
        summary_provider=summary_provider,
    )
    return await run_domain(ctx)
