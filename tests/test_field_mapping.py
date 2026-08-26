"""UC-3.3/3.4 字段映射：``FieldMapper``（Prometheus value[1] 抽取、点路径、时间戳解析）。"""

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
