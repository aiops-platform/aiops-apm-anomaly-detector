"""第三方响应 → Signal 字段映射（``FieldMapper``）。

- ``_extract_path``：支持点路径（``metric.__name__``）与数组索引（``value[1]``，Prometheus 行）。
- ``_parse_ts``：解析 ISO 字符串 / unix 秒为**朴素 UTC datetime**（统一 tz，避免 aware/naive 混用）。
- ``map_metric`` / ``map_log``：按 ``field_mapping`` 把一行响应映射为 ``MetricSignal`` / ``LogSignal``。

入站时区（``timezone_name``，来自 ``source_config.timezone``）：许多源（如 Spring app-log）返回**不带
时区后缀的本地墙钟时间**，而把它当 UTC 会整体偏移（实测 +08:00 源水位线超前 8h → 下一轮 startTime
反超 endTime）。naive 时间戳按源时区解释再转 UTC；带 tz 的时间戳照常按自身偏移转 UTC。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..models.signal import LogSignal, MetricSignal

_PATH_TOKEN = re.compile(r"[^.\[\]]+|\[\d+\]")


def _extract_path(row: Any, spec: str) -> Any:
    """按 ``message`` / ``metric.__name__`` / ``value[1]`` 抽取嵌套字段；取不到返回 None。"""
    if not isinstance(row, (dict, list)):
        return None
    current: Any = row
    for part in _PATH_TOKEN.findall(spec):
        if part.startswith("[") and part.endswith("]"):
            try:
                current = current[int(part[1:-1])]
            except (IndexError, TypeError, KeyError):
                return None
        else:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        if current is None:
            return None
    return current


def _parse_ts(value: Any, *, timezone_name: str | None = None) -> datetime:
    """解析时间戳为朴素 UTC datetime；ISO 字符串 / unix 秒 / datetime 均可。

    ``timezone_name``（IANA，如 ``Asia/Shanghai``）：naive 时间戳按该时区墙钟解释再转 UTC；
    带 tz 的时间戳忽略该参数（按自身偏移转 UTC）。非法 tz → ``ValueError``。
    """
    if isinstance(value, datetime):
        return _to_naive_utc(value, timezone_name=timezone_name)
    if value is None:
        raise ValueError("timestamp is required")
    if isinstance(value, (int, float)):
        return _to_naive_utc(datetime.fromtimestamp(float(value), tz=timezone.utc))
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # fromisoformat 与 tz 转换分开：仅格式错误走 unix 回退；非法 timezone_name 立即上抛（不吞掉）。
    try:
        parsed = datetime.fromisoformat(s)
    except ValueError:
        parsed = None
    if parsed is not None:
        return _to_naive_utc(parsed, timezone_name=timezone_name)
    try:
        return _to_naive_utc(datetime.fromtimestamp(float(s), tz=timezone.utc))
    except ValueError as exc:
        raise ValueError(f"cannot parse timestamp: {value!r}") from exc


def _to_naive_utc(dt: datetime, *, timezone_name: str | None = None) -> datetime:
    """统一为朴素 UTC datetime。

    带 tz → 按自身偏移转 UTC；naive + ``timezone_name`` → 按该时区墙钟解释再转 UTC；
    naive 无 ``timezone_name`` → 视为 UTC（既有行为）。
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    elif timezone_name:
        try:
            zone = ZoneInfo(timezone_name)
        except KeyError as exc:
            raise ValueError(f"invalid source_config.timezone {timezone_name!r}") from exc
        dt = dt.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _field(row: dict, mapping: dict, key: str, default: Any = None) -> Any:
    """按 mapping 的字段键抽取；mapping 缺该键或值取不到时返回 default。"""
    spec = mapping.get(key)
    if spec is None:
        return default
    value = _extract_path(row, spec)
    return value if value is not None else default


class FieldMapper:
    """把第三方响应行映射为 Signal 模型。"""

    @staticmethod
    def map_metric(row: dict, mapping: dict, tenant_id: str, *, timezone_name: str | None = None) -> MetricSignal:
        return MetricSignal(
            tenant_id=tenant_id,
            service=_field(row, mapping, "service", "unknown"),
            metric=_field(row, mapping, "metric", "unknown"),
            value=float(_field(row, mapping, "value")),
            timestamp=_parse_ts(_field(row, mapping, "timestamp"), timezone_name=timezone_name),
            labels=dict(row.get("labels") or {}),
        )

    @staticmethod
    def map_log(row: dict, mapping: dict, tenant_id: str, *, timezone_name: str | None = None) -> LogSignal:
        return LogSignal(
            tenant_id=tenant_id,
            service=_field(row, mapping, "service", "unknown"),
            level=_field(row, mapping, "level"),
            message=_field(row, mapping, "message"),
            stack_trace=_field(row, mapping, "stack_trace"),
            timestamp=_parse_ts(_field(row, mapping, "timestamp"), timezone_name=timezone_name),
            trace_id=_field(row, mapping, "trace_id"),  # 可选：业务全链路 request/trace id
        )
