"""UC-3.3/3.4/3.5/3.6 采集器：字段映射、水位线下推、幂等去重、超时降级。

用 ``FakeHttp`` 注入 ``SharedHttpClient`` 的位置（``request()`` 签名一致），
不触网；watermark/snapshot 用 InMemory 真源。
"""

from datetime import datetime

import httpx
import pytest

from aiops_apm.collectors import (
    CollectContext,
    HttpLogsCollector,
    HttpMetricsCollector,
    MockCollector,
    collector_for,
)
from aiops_apm.collectors._gateway import OutboundGateway
from aiops_apm.exceptions import AppException, ErrorCode
from aiops_apm.storage import InMemorySnapshotStore, InMemoryWatermarkStore

# ---- 目标构造 ----


def _metric_target(**overrides):
    base = {
        "target_id": "MT-0001",
        "service": "order-management",
        "signal_type": "metric",
        "source_type": "prometheus",
        "domain": "application",
        "source_config": {
            "url": "https://prometheus.example.com:9090/api/v1/query",
            "method": "GET",
            "params": {"query": "cpu_usage"},
            "rows_path": "data.result",
            "field_mapping": {
                "metric": "metric.__name__",
                "value": "value[1]",
                "timestamp": "value[0]",
            },
        },
        "schedule": {"interval_sec": 60},
        "enabled": True,
    }
    base.update(overrides)
    return base


def _log_target(**overrides):
    base = {
        "target_id": "MT-0002",
        "service": "order-management",
        "signal_type": "log",
        "source_type": "elk",
        "domain": "application",
        "source_config": {
            "url": "https://elk.example.com:9200/logs/_search",
            "method": "GET",
            "rows_path": "hits.hits",
            "field_mapping": {
                "level": "_source.level",
                "message": "_source.message",
                "stack_trace": "_source.stack_trace",
                "timestamp": "_source.@timestamp",
            },
        },
        "schedule": {"interval_sec": 60},
        "enabled": True,
    }
    base.update(overrides)
    return base


# ---- FakeHttp ----


class FakeHttp:
    """模拟第三方源：``payload_factory(params) -> dict``，返回带 request 的 200 响应。"""

    def __init__(self, payload_factory):
        self.calls: list[dict] = []
        self._factory = payload_factory

    async def request(self, method: str, url: str, **kwargs):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": kwargs.get("headers"),
                "params": kwargs.get("params"),
            }
        )
        body = self._factory(kwargs.get("params", {}))
        return httpx.Response(200, json=body, request=httpx.Request(method, url))


class TimeoutHttp:
    """恒抛超时的 http。"""

    async def request(self, method: str, url: str, **kwargs):
        raise httpx.TimeoutException("upstream timeout")


def _prometheus_rows(params):
    """模拟 Prometheus：有 ``start`` 时只返回更新的行（这里直接返回空）。"""
    if params.get("start"):
        return []
    return [
        {"metric": {"__name__": "cpu_usage"}, "value": [1710000000, "0.91"]},
        {"metric": {"__name__": "cpu_usage"}, "value": [1710000000, "0.91"]},  # 重复行
        {"metric": {"__name__": "cpu_usage"}, "value": [1710000060, "0.95"]},
    ]


def _json_result(rows):
    return {"data": {"result": rows}}


# ---- UC-3.3 指标采集 ----


async def test_metrics_collect_dedups_and_writes_snapshot():
    http = FakeHttp(lambda params: _json_result(_prometheus_rows(params)))
    collector = HttpMetricsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    snap = InMemorySnapshotStore()
    ctx = CollectContext("tenant-a", watermark_store=wm, snapshot_store=snap)

    signals = await collector.collect(ctx, _metric_target())

    assert len(signals) == 2  # 重复行被去重
    assert [s.metric for s in signals] == ["cpu_usage", "cpu_usage"]
    assert signals[0].tenant_id == "tenant-a"
    # 水位线推进到最新时间戳（1710000060 → 2024-03-09T16:01:00）
    assert (await wm.get("tenant-a", "MT-0001"))["last_ts"] == datetime(2024, 3, 9, 16, 1, 0)
    # 快照写入 2 行
    assert len(snap._rows) == 2
    assert all(r["signal_type"] == "metric" for r in snap._rows)


async def test_metrics_watermark_pushdown_start_param():
    http = FakeHttp(lambda params: _json_result([]))
    collector = HttpMetricsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0001", datetime(2024, 3, 9, 15, 30, 0))
    ctx = CollectContext("tenant-a", watermark_store=wm)

    await collector.collect(ctx, _metric_target())

    assert http.calls[0]["params"]["start"] == "2024-03-09T15:30:00"
    assert http.calls[0]["params"]["query"] == "cpu_usage"  # 原始 params 保留
    assert http.calls[0]["headers"] == {}


async def test_metrics_second_round_empty_and_watermark_not_regressed():
    http = FakeHttp(lambda params: _json_result(_prometheus_rows(params)))
    collector = HttpMetricsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    snap = InMemorySnapshotStore()
    ctx = CollectContext("tenant-a", watermark_store=wm, snapshot_store=snap)

    first = await collector.collect(ctx, _metric_target())
    assert len(first) == 2
    assert len(snap._rows) == 2

    second = await collector.collect(ctx, _metric_target())
    assert second == []  # 第二轮 0 新信号（水位线下推生效）
    assert (await wm.get("tenant-a", "MT-0001"))["last_ts"] == datetime(2024, 3, 9, 16, 1, 0)  # 未回退
    assert len(snap._rows) == 2  # 没有追加行


# ---- UC-3.4 日志采集 ----


def _elk_hits(params):
    if params.get("start"):
        return []
    return [
        {
            "_source": {
                "level": "ERROR",
                "message": "boom one",
                "stack_trace": "OutOfMemoryError: heap\n    at com.A.run()\n    at com.B.run()\n    at com.C.run()",
                "@timestamp": "2026-08-26T12:00:00",
            }
        },
        {
            "_source": {
                "level": "ERROR",
                "message": "boom two",
                "stack_trace": "OutOfMemoryError: heap\n    at com.A.run()\n    at com.B.run()",
                "@timestamp": "2026-08-26T12:00:01",
            }
        },
    ]


async def test_logs_collect_sets_signature_and_writes_snapshot():
    http = FakeHttp(lambda params: {"hits": {"hits": _elk_hits(params)}})
    collector = HttpLogsCollector(http, OutboundGateway())
    snap = InMemorySnapshotStore()
    ctx = CollectContext("tenant-a", snapshot_store=snap)

    signals = await collector.collect(ctx, _log_target())

    assert len(signals) == 2
    assert all(s.signature for s in signals)
    assert signals[0].signature == "OutOfMemoryError|at com.A.run|at com.B.run|at com.C.run"
    assert signals[1].signature == "OutOfMemoryError|at com.A.run|at com.B.run"
    # 快照行携带 signature 列
    assert [r["signature"] for r in snap._rows] == [s.signature for s in signals]
    assert all(r["signal_type"] == "log" for r in snap._rows)


async def test_logs_dedup_by_service_signature_timestamp():
    def hits(params):
        return [
            {"_source": {"level": "ERROR", "message": "boom", "stack_trace": "E: x", "@timestamp": "2026-08-26T12:00:00"}},
            {"_source": {"level": "ERROR", "message": "boom", "stack_trace": "E: x", "@timestamp": "2026-08-26T12:00:00"}},
        ]

    http = FakeHttp(lambda params: {"hits": {"hits": hits(params)}})
    collector = HttpLogsCollector(http, OutboundGateway())
    signals = await collector.collect(CollectContext("tenant-a"), _log_target())
    assert len(signals) == 1


# ---- UC-3.6 超时降级 ----


async def test_collect_timeout_propagates_to_caller():
    collector = HttpMetricsCollector(TimeoutHttp(), OutboundGateway())
    with pytest.raises(httpx.TimeoutException):
        await collector.collect(CollectContext("tenant-a"), _metric_target())


async def test_healthy_source_collects_after_other_fails():
    # 一个失败、一个健康：失败被捕获，健康源正常出信号（服务不崩溃）
    bad = HttpMetricsCollector(TimeoutHttp(), OutboundGateway())
    with pytest.raises(httpx.TimeoutException):
        await bad.collect(CollectContext("tenant-a"), _metric_target())

    good = HttpMetricsCollector(FakeHttp(lambda params: _json_result(_prometheus_rows(params))), OutboundGateway())
    signals = await good.collect(CollectContext("tenant-a"), _metric_target())
    assert len(signals) == 2


# ---- mock collector + collector_for 分派 ----


async def test_mock_collector_returns_predefined_signals():
    collector = MockCollector()
    from aiops_apm.models.signal import MetricSignal

    sig = MetricSignal(service="svc", metric="cpu", value=1.0, timestamp=datetime(2026, 8, 26, 12, 0, 0))
    signals = await collector.collect(CollectContext("tenant-a"), {"_mock_signals": [sig]})
    assert signals == [sig]


def test_collector_for_dispatches_by_source():
    assert isinstance(collector_for(_metric_target()), HttpMetricsCollector)
    assert isinstance(collector_for(_log_target()), HttpLogsCollector)
    assert isinstance(collector_for({"signal_type": "metric", "source_type": "mock"}), MockCollector)
    with pytest.raises(AppException) as excinfo:
        collector_for({"signal_type": "log", "source_type": "prometheus"})
    assert excinfo.value.code == ErrorCode.CONFIG_ERROR
