"""L3 验证：持续性 + 误报率闸门 + 严重度校准（确定性纯函数 + detection_state/fpr 读取）。

持续性用 **per-key consecutive_rounds**（Enhanced plan P0#2 修正）：increment-first，
``persistence_rounds=N`` = 连续 N 轮出现则第 N 轮开单。误报率闸门 P0#8 为「降级不丢弃」：
样本不足（total < min_samples）或 fpr 低于阈值才算误报，否则降级 warning 仍开单。
组合升 critical 留 M6（用例 2）。
"""

from __future__ import annotations

from typing import Any

from aiops_apm.models import fingerprint
from aiops_apm.models.record import Verification

_SEVERITY_RANK = {"warning": 0, "high": 1, "critical": 2}


def calibrate_severity(anomalies: list) -> str:
    """取最高 severity（warning < high < critical）；组合升级留 M6。"""
    if not anomalies:
        return "warning"
    return max((a.severity for a in anomalies), key=lambda s: _SEVERITY_RANK.get(s, 0))


async def l3_verify(ctx: Any, service: str, anomalies: list) -> Verification:
    """对一个 service 的异常做持续性 + fpr 闸门 + 严重度校准。"""
    vc = ctx.domain_config.verify
    persisted: list = []
    for a in anomalies:
        key = fingerprint.anomaly_key(a)
        ctx.seen_keys.add(key)
        state = await ctx.state_store.get(ctx.tenant_id, ctx.domain, key)
        prev = state["consecutive_rounds"] if state else 0
        first_seen = state["first_seen"] if state else ctx.now
        new_consecutive = prev + 1  # increment-first：本轮也算一个连续轮
        if new_consecutive >= vc.persistence_rounds:
            persisted.append(a)
        await ctx.state_store.upsert(
            ctx.tenant_id, ctx.domain, key,
            consecutive_rounds=new_consecutive, miss_rounds=0,
            first_seen=first_seen, last_seen=ctx.now,
        )

    if not persisted:
        return Verification(
            passed=False, persistence_ok=False, resample_ok=True,
            false_positive_rate=0.0, final_severity="warning",
        )

    gk = fingerprint.group_key(ctx.tenant_id, ctx.domain, service, persisted)
    entry = ctx.fpr.get(gk, {"fpr": 0.0, "total": 0})
    fpr = float(entry.get("fpr", 0.0))
    total = int(entry.get("total", 0))
    fpr_ok = total < vc.min_samples or fpr < vc.false_positive_threshold
    severity = "warning" if not fpr_ok else calibrate_severity(persisted)  # P0#8 降级不丢弃
    return Verification(
        passed=True, persistence_ok=True, resample_ok=True,
        false_positive_rate=fpr, final_severity=severity,
    )
