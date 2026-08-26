"""L2 关联：按 service 关联指标+日志同源、变更信号（纯函数，零 LLM 调用）。

确定性纯函数：``_within_window``（指标+日志同源）、``_change_within_window``（部署变更关联）、
``template_summary``（现象摘要模板兜底）。``l2_correlate`` 返回
``{service: (Correlation, change_related, recent_change)}``。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from aiops_apm.models.record import Correlation


def _anom_ts(a: Any) -> datetime:
    """anomaly 的判定时间：LogAnomaly.detected_at 可能为 None，回退 first_seen。"""
    if getattr(a, "detected_at", None) is not None:
        return a.detected_at
    return a.first_seen


def _within_window(metric_anoms: list, log_anoms: list, window_sec: int) -> bool:
    """任意 metric 与 log anomaly 的判定时间差 ≤ window → 同源关联。"""
    if not metric_anoms or not log_anoms:
        return False
    window = timedelta(seconds=window_sec)
    for m in metric_anoms:
        m_ts = _anom_ts(m)
        for log in log_anoms:
            if abs(m_ts - _anom_ts(log)) <= window:
                return True
    return False


def _change_within_window(changes: list, anomalies: list, window_sec: int) -> tuple[bool, dict | None]:
    """changes 中 service 匹配且时间在窗口内 → ``(True, {"change_id", "summary", "changed_at"})``。"""
    if not changes or not anomalies:
        return False, None
    window = timedelta(seconds=window_sec)
    services = {a.service for a in anomalies}
    for c in changes:
        if c.service not in services:
            continue
        for a in anomalies:
            if abs(c.timestamp - _anom_ts(a)) <= window:
                return True, {"change_id": c.change_id, "summary": c.summary, "changed_at": c.timestamp}
    return False, None


def template_summary(metric_anoms: list, log_anoms: list) -> str:
    """模板兜底摘要：metric 拼 ``"{service} {metric} {value}"``，log 拼 ``"{service} {signature} x{count}"``。"""
    parts = [f"{m.service} {m.metric} {m.value}" for m in metric_anoms]
    parts += [f"{log.service} {log.signature} x{log.count}" for log in log_anoms]
    return "；".join(parts)


async def l2_correlate(ctx: Any) -> dict:
    """按 service 分组，返回 ``{service: (Correlation, change_related, recent_change)}``。"""
    cs = ctx.domain_config.correlation
    metric_by_service: dict[str, list] = defaultdict(list)
    log_by_service: dict[str, list] = defaultdict(list)
    for a in ctx.anomalies:
        (metric_by_service if a.kind == "metric" else log_by_service)[a.service].append(a)

    services = set(metric_by_service) | set(log_by_service)
    result: dict[str, tuple] = {}
    for service in services:
        m = metric_by_service[service]
        log = log_by_service[service]
        related = _within_window(m, log, cs.metric_log_window_sec)
        if related:
            reason = "metric_log_within_window"
        elif m and not log:
            reason = "metric_only"
        elif log and not m:
            reason = "log_only"
        else:
            reason = "unrelated"
        change_related, recent_change = _change_within_window(ctx.changes, m + log, cs.change_window_sec)
        result[service] = (Correlation(related=related, reason=reason), change_related, recent_change)
    return result
