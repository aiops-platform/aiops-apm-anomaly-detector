"""L3 验证：持续性 + 误报率闸门 + 严重度校准（确定性纯函数 + detection_state/fpr 读取）。

持续性用 **per-key 累计出现轮数**（字段名沿用 ``consecutive_rounds``，语义为累计）：
每次出现 ``+1``（increment-first），``sweep`` 断轮**不清零**，达到
``persistence_rounds=N`` 的当轮开单——中间断轮不打断持续性。误报率闸门 P0#8 为「降级不丢弃」：
样本不足（total < min_samples）或 fpr 低于阈值才算误报，否则降级 warning 仍开单。
组合升 critical 留 M6（用例 2）。
"""

from __future__ import annotations

from typing import Any

from aiops_apm.models import fingerprint
from aiops_apm.models.record import Verification

_SEVERITY_RANK = {"warning": 0, "high": 1, "critical": 2}
_RANK_TO_NAME = {0: "warning", 1: "high", 2: "critical"}


def calibrate_severity(anomalies: list, *, related: bool = False) -> str:
    """严重度校准：取最高 severity；``related`` 且同 service 有 high metric + high log → 组合升 critical（§13 用例 2）。"""
    if not anomalies:
        return "warning"
    top = max((_SEVERITY_RANK.get(a.severity, 0) for a in anomalies), default=0)
    if related:
        has_high_metric = any(a.kind == "metric" and a.severity == "high" for a in anomalies)
        has_high_log = any(a.kind == "log" and a.severity == "high" for a in anomalies)
        if has_high_metric and has_high_log:
            return "critical"
    return _RANK_TO_NAME[top]


async def l3_verify(ctx: Any, service: str, anomalies: list, *, related: bool = False) -> Verification:
    """对一个**事故组**的异常做持续性 + fpr 闸门 + 严重度校准。

    ``service`` 是组的**代表服务**（``grouping.representative_service``），只用于拼 fpr 的
    去重键——持续性本身是按 ``anomaly_key`` 逐条算的，与 service 无关。

    ⚠️ 这个代表值必须与 ``emit`` 写进 ``ProblemRecord.group_key_service`` 的**完全一致**：
    fpr 读（这里）与写（``router/problems.py`` 的 `_record_fpr`，键取自记录的 ``group_key``）
    必须是同一个键，否则误报率闸门永远读不到已写入的条目，静默失效。故由 ``run_domain``
    统一算一次、两处透传。
    """
    vc = ctx.domain_config.verify
    persisted: list = []
    for a in anomalies:
        key = fingerprint.anomaly_key(a)
        ctx.seen_keys.add(key)
        state = await ctx.state_store.get(ctx.tenant_id, ctx.domain, key)
        prev = state["consecutive_rounds"] if state else 0
        first_seen = state["first_seen"] if state else ctx.now
        new_consecutive = prev + 1  # 出现即 +1；累计计数，断轮（sweep）不清零
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
    # P0#8 降级不丢弃；related 透传供组合升 critical（§13 用例 2）
    severity = "warning" if not fpr_ok else calibrate_severity(persisted, related=related)
    return Verification(
        passed=True, persistence_ok=True, resample_ok=True,
        false_positive_rate=fpr, final_severity=severity,
    )
