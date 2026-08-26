"""``filter_signals`` 结构化 matcher（M4 完成标准点名：全分支覆盖）。

覆盖：None / ``*`` / str（metric 名、log level、不匹配）/ dict（metric 与 log 全组合：
metric 键通配、metric 匹配/不匹配、labels 匹配/不匹配、service 匹配/不匹配、错误 signal_type）/ 非法 matcher。
"""

from datetime import datetime

from aiops_apm.models.signal import LogSignal, MetricSignal
from aiops_apm.pipeline.filter_signals import filter_signals

TS = datetime(2026, 8, 26, 12, 0, 0)


def ms(*, service="svc-a", metric="cpu_usage", value=0.95, labels=None) -> MetricSignal:
    return MetricSignal(service=service, metric=metric, value=value, timestamp=TS, labels=labels or {})


def ls(*, service="svc-a", level="ERROR", message="boom") -> LogSignal:
    return LogSignal(service=service, level=level, message=message, timestamp=TS)


# --- None / "*" ---


def test_none_returns_all():
    sigs = [ms(), ls()]
    assert filter_signals(sigs, None) == sigs


def test_star_returns_all():
    sigs = [ms(), ls()]
    assert filter_signals(sigs, "*") == sigs


def test_empty_string_returns_all():
    sigs = [ms(), ls()]
    assert filter_signals(sigs, "") == sigs


# --- str（向后兼容） ---


def test_str_metric_name():
    sigs = [ms(metric="cpu_usage"), ms(metric="memory_usage"), ls(level="ERROR")]
    assert filter_signals(sigs, "cpu_usage") == [sigs[0]]


def test_str_log_level():
    sigs = [ms(), ls(level="ERROR"), ls(level="WARN")]
    assert filter_signals(sigs, "ERROR") == [sigs[1]]


def test_str_no_match():
    assert filter_signals([ms(), ls()], "nope") == []


# --- dict metric ---


def test_dict_metric_no_metric_key_wildcard():
    sigs = [ms(metric="cpu_usage"), ms(metric="memory_usage"), ls()]
    assert filter_signals(sigs, {"signal_type": "metric"}) == sigs[:2]


def test_dict_metric_metric_match():
    sigs = [ms(metric="cpu_usage"), ms(metric="memory_usage")]
    assert filter_signals(sigs, {"signal_type": "metric", "metric": "cpu_usage"}) == [sigs[0]]


def test_dict_metric_metric_mismatch():
    sigs = [ms(metric="cpu_usage")]
    assert filter_signals(sigs, {"signal_type": "metric", "metric": "memory_usage"}) == []


def test_dict_metric_labels_match_subset():
    sigs = [ms(labels={"env": "prod", "node": "n1"}), ms(labels={"env": "dev"})]
    out = filter_signals(sigs, {"signal_type": "metric", "labels": {"env": "prod"}})
    assert out == [sigs[0]]


def test_dict_metric_labels_mismatch():
    sigs = [ms(labels={"env": "prod"})]
    assert filter_signals(sigs, {"signal_type": "metric", "labels": {"env": "dev"}}) == []


def test_dict_metric_labels_null_treated_as_empty():
    sigs = [ms(labels={"env": "prod"})]
    assert filter_signals(sigs, {"signal_type": "metric", "labels": None}) == sigs


def test_dict_metric_service():
    sigs = [ms(service="svc-a"), ms(service="svc-b")]
    assert filter_signals(sigs, {"signal_type": "metric", "service": "svc-b"}) == [sigs[1]]


def test_dict_metric_excludes_log_signals():
    sigs = [ms(), ls()]
    assert filter_signals(sigs, {"signal_type": "metric"}) == [sigs[0]]


# --- dict log ---


def test_dict_log_no_level_wildcard():
    sigs = [ls(level="ERROR"), ls(level="WARN"), ms()]
    assert filter_signals(sigs, {"signal_type": "log"}) == sigs[:2]


def test_dict_log_level_match():
    sigs = [ls(level="ERROR"), ls(level="WARN")]
    assert filter_signals(sigs, {"signal_type": "log", "level": "WARN"}) == [sigs[1]]


def test_dict_log_service():
    sigs = [ls(service="svc-a"), ls(service="svc-b")]
    assert filter_signals(sigs, {"signal_type": "log", "service": "svc-a"}) == [sigs[0]]


def test_dict_log_excludes_metric_signals():
    sigs = [ms(), ls()]
    assert filter_signals(sigs, {"signal_type": "log"}) == [sigs[1]]


# --- 其余分支 ---


def test_dict_wrong_signal_type_returns_empty():
    sigs = [ms(), ls()]
    assert filter_signals(sigs, {"signal_type": "change"}) == []


def test_invalid_matcher_returns_empty():
    sigs = [ms(), ls()]
    assert filter_signals(sigs, 123) == []
