"""Poller：一组 ``(tenant_id, domain)`` 目标的一轮采集编排（M6 UC-6.1，M7 加轮次审计/metrics）。

``run_round`` 把同域目标并行采集，单个目标失败降级为 ``degraded_sources`` 标记
（M5 遗留「degraded_sources 产生」在此落地），再把合并后的 signals 喂给
``build_context → run_domain`` 走确定性漏斗。

M7（UC-7.1/7.2）：每轮写入 ``detection_round``（create running → update success/partial/failed），
收尾 ``record_round_metrics`` 打点；``run_domain`` 异常 → 记 failed + 审计 + re-raise
（保留 scheduler/alerts 调用方行为）。
V5：每轮下每 target 一行 ``detection_round_target``（create_target running →
采集后逐 target update_target_status ok/failed + signals_count/error），
供 per-target 审计与孤儿恢复。
V6：漏斗后按 service 归因回填 per-target 计数（anomaly/record/suppressed），
``update_target_status(status=None)`` 只更新计数字段、不动采集状态。

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
    # round → target 一对多明细：每 target 一行 running（V5 子表）
    for t in targets:
        await rounds.create_target(
            tenant_id, trace_id, str(t.get("target_id", "unknown")), started_at=now
        )
    perf_start = time.perf_counter()

    collect_ctx = CollectContext(
        tenant_id=tenant_id,
        watermark_store=storage.watermarks,
        snapshot_store=storage.snapshots,
        now=now,  # 滚动窗口（§8.2）：采集时间窗口按本轮 trigger 时间算
    )
    degraded: list[str] = []

    async def _one(target: dict) -> tuple[str, list, str | None]:
        target_id = str(target.get("target_id", "unknown"))
        try:
            collector = collector_for(target, http=http, settings=settings)
            signals = await collector.collect(collect_ctx, target)
            return target_id, list(signals), None
        except Exception as exc:  # noqa: BLE001 -- 单个 target 降级，不拖垮整轮
            degraded.append(target_id)
            return target_id, [], f"{type(exc).__name__}: {exc}"

    results = await asyncio.gather(*(_one(t) for t in targets))
    signals = [s for _, batch, _ in results for s in batch]

    # 采集收尾：逐 target 更新 ok/failed（含各自信号量与错误原因）
    collect_ended = datetime.now(timezone.utc)
    for target_id, batch, error in results:
        # V8：本轮实际下发的出站请求参数（时间窗口/水位线/时区转换后），mock 源无请求 → None
        request_params = collect_ctx.request_params.get(target_id)
        if error is None:
            await rounds.update_target_status(
                tenant_id, trace_id, target_id, "ok",
                finished_at=collect_ended, signals_count=len(batch),
                request_params=request_params,
            )
        else:
            await rounds.update_target_status(
                tenant_id, trace_id, target_id, "failed",
                finished_at=collect_ended, signals_count=0, error=error,
                request_params=request_params,
            )

    try:
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

    # 漏斗后归因回填（V6）：按 target.service 匹配漏斗结果，把 per-target 计数补到子表。
    # status=None → 只更新 anomaly_count/record_count/suppressed_count，不覆盖采集状态。
    # 采集失败的 target（degraded，0 信号）跳过——它的 service 计数应由真正采集成功的 target 承担。
    failed_targets = {tid for tid, _, err in results if err is not None}
    anomalies_by_service = result.anomalies_by_service or {}
    records_by_service = result.records_by_service or {}
    suppressed_by_service = result.suppressed_by_service or {}
    for t in targets:
        tid = str(t.get("target_id", "unknown"))
        if tid in failed_targets:
            continue
        svc = str(t.get("service", "unknown"))
        await rounds.update_target_status(
            tenant_id, trace_id, tid,
            anomaly_count=anomalies_by_service.get(svc, 0),
            record_count=records_by_service.get(svc, 0),
            suppressed_count=suppressed_by_service.get(svc, 0),
        )

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
