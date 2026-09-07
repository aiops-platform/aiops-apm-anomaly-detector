"""UC-3.3/3.4 字段映射：``FieldMapper``（Prometheus value[1] 抽取、点路径、时间戳解析）。"""

import pytest

from aiops_apm.collectors._field_mapping import FieldMapper, _extract_path, _parse_ts
from aiops_apm.models.signal import LogSignal, MetricSignal

# ---- _extract_path ----


def test_extract_path_plain_key():
    assert _extract_path({"message": "boom"}, "message") == "boom"


def test_extract_path_dotted_nested():
    row = {"_source": {"message": "boom"}}
    assert _extract_path(row, "_source.message") == "boom"


def test_extract_path_array_index():
    row = {"value": [1710000000, "0.91"]}
    assert _extract_path(row, "value[1]") == "0.91"
    assert _extract_path(row, "value[0]") == 1710000000


def test_extract_path_missing_returns_none():
    assert _extract_path({"a": 1}, "a.b") is None


# ---- _parse_ts ----


def test_parse_ts_iso_string():
    dt = _parse_ts("2026-08-26T12:00:00")
    assert dt.isoformat() == "2026-08-26T12:00:00"


def test_parse_ts_unix_seconds():
    dt = _parse_ts(1710000000.0)
    assert dt.isoformat() == "2024-03-09T16:00:00"


def test_parse_ts_iso_with_utc_suffix_normalized_to_naive_utc():
    dt = _parse_ts("2026-08-26T12:00:00Z")
    assert dt.tzinfo is None
    assert dt.hour == 12


def test_parse_ts_naive_with_timezone_converted_to_utc():
    # 入站源时区：Spring 返回 naive +08:00 墙钟，须按源时区解释再转 UTC（修复 startTime 反超 endTime 的 bug）
    dt = _parse_ts("2026-08-26T20:00:00", timezone_name="Asia/Shanghai")
    assert dt.tzinfo is None
    assert dt.isoformat() == "2026-08-26T12:00:00"


def test_parse_ts_aware_ignores_timezone_name():
    # 带 tz 的时间戳按自身偏移转 UTC，timezone_name 不参与
    dt = _parse_ts("2026-08-26T20:00:00+08:00", timezone_name="Asia/Shanghai")
    assert dt.tzinfo is None
    assert dt.isoformat() == "2026-08-26T12:00:00"


def test_parse_ts_naive_without_timezone_unchanged():
    dt = _parse_ts("2026-08-26T12:00:00")
    assert dt.isoformat() == "2026-08-26T12:00:00"


def test_parse_ts_invalid_timezone_raises():
    with pytest.raises(ValueError, match="timezone"):
        _parse_ts("2026-08-26T20:00:00", timezone_name="Not/AZone")


# ---- map_metric ----


def test_map_metric_prometheus_row():
    mapping = {"metric": "metric.__name__", "value": "value[1]", "timestamp": "value[0]"}
    row = {"metric": {"__name__": "cpu_usage", "instance": "a"}, "value": [1710000000, "0.91"]}
    sig = FieldMapper.map_metric(row, mapping, "tenant-x")
    assert isinstance(sig, MetricSignal)
    assert sig.metric == "cpu_usage"
    assert sig.value == 0.91
    assert sig.tenant_id == "tenant-x"


# ---- map_log ----


def test_map_log_elk_source_row():
    mapping = {"level": "_source.level", "message": "_source.message", "timestamp": "_source.@timestamp"}
    row = {"_source": {"level": "ERROR", "message": "boom", "@timestamp": "2026-08-26T12:00:00"}}
    sig = FieldMapper.map_log(row, mapping, "tenant-x")
    assert isinstance(sig, LogSignal)
    assert sig.level == "ERROR"
    assert sig.message == "boom"
    assert sig.timestamp.isoformat() == "2026-08-26T12:00:00"


def test_map_log_naive_timestamp_converted_with_source_timezone():
    # 入站时区透传：collector 把 source_config.timezone 传给 map_log，naive @timestamp 按源时区转 UTC
    mapping = {"level": "_source.level", "message": "_source.message", "timestamp": "_source.@timestamp"}
    row = {"_source": {"level": "ERROR", "message": "boom", "@timestamp": "2026-08-26T20:00:00"}}
    sig = FieldMapper.map_log(row, mapping, "tenant-x", timezone_name="Asia/Shanghai")
    assert sig.timestamp.isoformat() == "2026-08-26T12:00:00"


def test_map_metric_timestamp_converted_with_source_timezone():
    mapping = {"metric": "metric.__name__", "value": "value[1]", "timestamp": "value[0]"}
    # unix 秒是绝对时刻，不受源时区影响
    row = {"metric": {"__name__": "cpu_usage"}, "value": [1710000000, "0.91"]}
    sig = FieldMapper.map_metric(row, mapping, "tenant-x", timezone_name="Asia/Shanghai")
    assert sig.timestamp.isoformat() == "2024-03-09T16:00:00"
