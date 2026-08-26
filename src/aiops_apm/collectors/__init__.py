"""采集器插件：内置 http_metrics / http_logs / mock。

M3 用直接 import 分派（``collector_for``）；M4 的插件 registry 再消费 entry_points。
分派矩阵（设计 §6.2）：log + (http|elk) → http_logs；metric + (http|prometheus) → http_metrics。
"""

from __future__ import annotations

from typing import Any

from ..exceptions import AppException, ErrorCode
from ..plugins.base import Collector
from ._context import CollectContext
from ._gateway import OutboundGateway
from ._http_client import SharedHttpClient
from .http_logs import HttpLogsCollector
from .http_metrics import HttpMetricsCollector
from .mock import MockCollector

__all__ = [
    "Collector",
    "CollectContext",
    "HttpMetricsCollector",
    "HttpLogsCollector",
    "MockCollector",
    "SharedHttpClient",
    "OutboundGateway",
    "collector_for",
]


def collector_for(target: dict, *, http: Any = None, settings: Any = None) -> Collector:
    """按 ``signal_type + source_type`` 选择采集器插件。"""
    source_type = target.get("source_type", "")
    signal_type = target.get("signal_type", "")
    if source_type == "mock":
        return MockCollector()
    if signal_type == "log" and source_type in ("http", "elk"):
        return HttpLogsCollector(http, OutboundGateway())
    if signal_type == "metric" and source_type in ("http", "prometheus"):
        return HttpMetricsCollector(http, OutboundGateway())
    raise AppException(ErrorCode.CONFIG_ERROR, f"unsupported collector source: {signal_type}/{source_type}")
