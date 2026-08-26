"""L1 信号结构化匹配（filter_signals）。

设计文档 §6.4 + Enhanced plan M4 骨架：``*``/None → 全量；str → 向后兼容（metric 名 / log level）；
dict → 结构化 matcher（``signal_type`` 分派：metric→metric/labels/service；log→level/service）。
M5 ``l1_detect`` 用它对每个 DetectorSpec.signal 分发信号。
"""

from typing import Any

from aiops_apm.models.signal import LogSignal, MetricSignal


def filter_signals(signals: list[Any], matcher: str | dict | None) -> list[Any]:
    """结构化信号匹配，返回命中的信号子集。"""
    if matcher is None or matcher in ("*", ""):  # 设计 §6.4：`*`/空 → 全量
        return signals
    if isinstance(matcher, str):
        return [
            s
            for s in signals
            if (isinstance(s, MetricSignal) and s.metric == matcher)
            or (isinstance(s, LogSignal) and s.level == matcher)
        ]
    if isinstance(matcher, dict):
        out: list[Any] = []
        for s in signals:
            if matcher.get("signal_type") == "metric" and isinstance(s, MetricSignal):
                if (
                    (not matcher.get("metric") or s.metric == matcher["metric"])
                    and _labels_match(s.labels, matcher.get("labels") or {})
                    and (not matcher.get("service") or s.service == matcher["service"])
                ):
                    out.append(s)
            elif matcher.get("signal_type") == "log" and isinstance(s, LogSignal):
                if (
                    (not matcher.get("level") or s.level == matcher["level"])
                    and (not matcher.get("service") or s.service == matcher["service"])
                ):
                    out.append(s)
        return out
    return []


def _labels_match(actual: dict, wanted: dict) -> bool:
    """actual 覆盖 wanted 的全部键值（wanted 为空 → True）。"""
    return all(actual.get(k) == v for k, v in wanted.items())
