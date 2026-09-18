"""L2 关联：对一个事故组关联指标+日志同源、变更信号（纯函数，零 LLM 调用）。

确定性纯函数：``_within_window``（**同 service** 指标+日志同源）、``_change_within_window``
（部署变更关联）、``template_summary``（现象摘要模板兜底）。

M9 起入参是 ``grouping.group_anomalies`` 划分出的**组**（可跨服务），不再是单个 service。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from aiops_apm.models.record import Correlation


def _anom_ts(a: Any) -> datetime:
    """anomaly 的判定时间：LogAnomaly.detected_at 可能为 None，回退 first_seen。"""
    if getattr(a, "detected_at", None) is not None:
        return a.detected_at
    return a.first_seen


def _within_window(metric_anoms: list, log_anoms: list, window_sec: int) -> bool:
    """**同 service** 的 metric 与 log 判定时间差 ≤ window → 同源关联。

    必须显式限定同服务：``calibrate_severity`` 的「同源 metric+log 升 critical」只看
    ``related``，它自己**从不检查 service** —— 同服务的保证原先完全依赖调用方按 service
    分组（M9 前 ``l2_correlate`` 就是这么切的）。M9 的分组可跨服务，若这里不加限定，
    一个服务的 metric 会跟另一个服务的 log 配成「同源」，把无关组合误升为 critical。
    """
    if not metric_anoms or not log_anoms:
        return False
    window = timedelta(seconds=window_sec)
    for m in metric_anoms:
        m_ts = _anom_ts(m)
        for log in log_anoms:
            if m.service != log.service:
                continue
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


def _log_message(sig: str) -> str:
    """日志摘要文本：取签名首行冒号后的 message（去掉 ``|`` 之后的堆栈帧），无冒号时整行。

    签名格式 ``"ExceptionType: message|at frame1|..."``（``signature.py``），
    这里只留给人读的 message，去掉异常类型前缀与堆栈帧，避免 symptom 太代码化。
    """
    if not sig:
        return ""
    first = sig.split("|")[0]
    if ":" in first:
        msg = first.split(":", 1)[1].strip()
        if msg:
            return msg
    return first


def template_summary(metric_anoms: list, log_anoms: list) -> str:
    """模板兜底摘要：metric 拼 ``"{metric} = {value}"``，log 拼 ``"{message} x{count}"``。

    2026-08-28 调整：log 部分不再拼整条堆栈签名（原 ``"{service} {signature} x{count}"``），
    改取签名冒号后的 message；summary 本就是按 service 分组产出，service 前缀冗余去掉。
    """
    parts = [f"{m.metric} = {m.value}" for m in metric_anoms]
    parts += [f"{_log_message(log.signature)} x{log.count}" for log in log_anoms]
    return "；".join(parts)


async def l2_correlate(ctx: Any, anomalies: list) -> tuple[Correlation, bool, dict | None]:
    """对一个**事故组**算关联，返回 ``(Correlation, change_related, recent_change)``。

    M9：入参从「一个 service 的异常」变成「一个组」（``pipeline/grouping.py`` 划分出来的
    连通分量，可跨服务），返回也从 ``{service: ...}`` 的字典变成单条结果——调用方本来就
    是逐个处理的，字典只是为了按 service 索引。
    """
    cs = ctx.domain_config.correlation
    metric_anoms = [a for a in anomalies if a.kind == "metric"]
    log_anoms = [a for a in anomalies if a.kind == "log"]
    related = _within_window(metric_anoms, log_anoms, cs.metric_log_window_sec)
    if related:
        reason = "metric_log_within_window"
    elif metric_anoms and not log_anoms:
        reason = "metric_only"
    elif log_anoms and not metric_anoms:
        reason = "log_only"
    else:
        reason = "unrelated"
    change_related, recent_change = _change_within_window(ctx.changes, list(anomalies), cs.change_window_sec)
    return Correlation(related=related, reason=reason), change_related, recent_change
