"""UC-3.3/3.4/3.5/3.6 采集器：字段映射、水位线下推、幂等去重、超时降级。

用 ``FakeHttp`` 注入 ``SharedHttpClient`` 的位置（``request()`` 签名一致），
不触网；watermark/snapshot 用 InMemory 真源。
"""

import asyncio
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


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """hostname URL 在 collect 时过 ``validate_url`` 的 DNS 二次校验（M7）——统一放行为公网 IP。"""
    monkeypatch.setattr("aiops_apm.collectors._gateway._resolve_ips", lambda host: ["93.184.216.34"])


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


def _es_log_sc(**overrides):
    """ES 源的 ``source_config`` 基底（POST + time_field/service_field）。

    ES 用例一律**整体重建** source_config，不用 ``_sc_with_window`` 那种合并式——
    后者是给 ``window_sec``/``timezone`` 追加用的，而 ES 模式的键要么齐、要么不齐。
    """
    sc = {
        "url": "https://elk.example.com:9200/logs/_search",
        "method": "POST",
        "rows_path": "hits.hits",
        "time_field": "@timestamp",
        "service_field": "app.service.keyword",
        "field_mapping": {"timestamp": "_source.@timestamp"},
    }
    sc.update(overrides)
    return sc


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
                "json": kwargs.get("json"),
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

    assert http.calls[0]["params"]["start"] == "2024-03-09T15:30:00.000Z"
    assert http.calls[0]["params"]["query"] == "cpu_usage"  # 原始 params 保留
    assert http.calls[0]["headers"] == {}


async def test_metrics_watermark_respects_time_params_mapping():
    """水位线分支也应尊重 time_params 映射（与 apply_time_window 一致）——否则 Spring 等源收不到 startTime。

    start/end 都映射时补 end=now：仅 start 时部分源（如 Spring）不过滤 → 每轮重复采集。
    """
    http = FakeHttp(lambda params: _json_result([]))
    collector = HttpMetricsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0001", datetime(2024, 3, 9, 15, 30, 0))
    now = datetime(2024, 3, 9, 16, 0, 0)
    ctx = CollectContext("tenant-a", watermark_store=wm, now=now)

    target = _metric_target(
        source_config={
            **_metric_target()["source_config"],
            "time_params": {"start": "startTime", "end": "endTime"},
        }
    )
    await collector.collect(ctx, target)

    params = http.calls[0]["params"]
    assert params["startTime"] == "2024-03-09T15:30:00.000Z"  # 水位线 → startTime（SSS+Z）
    assert params["endTime"] == "2024-03-09T16:00:00.000Z"  # end 有映射 → endTime=now
    assert "start" not in params  # 不再用硬编码键
    assert "end" not in params
    assert params["query"] == "cpu_usage"  # 原始静态 params 保留


async def test_logs_watermark_respects_time_params_mapping():
    http = FakeHttp(lambda params: {})
    collector = HttpLogsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0002", datetime(2024, 3, 9, 15, 30, 0))
    now = datetime(2024, 3, 9, 16, 0, 0)
    ctx = CollectContext("tenant-a", watermark_store=wm, now=now)

    target = _log_target(
        source_config={
            **_log_target()["source_config"],
            "time_params": {"start": "startTime", "end": "endTime"},
        }
    )
    await collector.collect(ctx, target)

    params = http.calls[0]["params"]
    assert params["startTime"] == "2024-03-09T15:30:00.000Z"
    assert params["endTime"] == "2024-03-09T16:00:00.000Z"
    assert "start" not in params
    assert "end" not in params


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


async def test_metrics_future_watermark_self_heals():
    """未来水位线（脏数据，历史入站时区 bug 产物）跳过下推 → 全量重采并按真实信号重新推进水位线。"""
    http = FakeHttp(lambda params: _json_result(_prometheus_rows(params)))
    collector = HttpMetricsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0001", datetime(2024, 3, 9, 17, 0, 0))  # 超前 now 1h
    now = datetime(2024, 3, 9, 16, 0, 0)
    ctx = CollectContext("tenant-a", watermark_store=wm, now=now)

    signals = await collector.collect(ctx, _metric_target())

    assert len(signals) == 2  # 未下推 start → 全量返回
    assert "start" not in http.calls[0]["params"]  # 未来水位线被忽略
    assert (await wm.get("tenant-a", "MT-0001"))["last_ts"] == datetime(2024, 3, 9, 16, 1, 0)  # 已纠正


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
    assert signals[0].signature == "OutOfMemoryError: heap|at com.A.run()|at com.B.run()|at com.C.run()"
    assert signals[1].signature == "OutOfMemoryError: heap|at com.A.run()|at com.B.run()"
    # 快照行携带 signature 列
    assert [r["signature"] for r in snap._rows] == [s.signature for s in signals]
    assert all(r["signal_type"] == "log" for r in snap._rows)


async def test_logs_es_mode_window_goes_in_body_not_params():
    """ES 源的时间窗与服务过滤只能走 POST body，不得同时下发 URL 参数。

    回归守卫（2026-09-17 实测踩到）：水位线分支若把时间窗也写进 URL 参数，ES 会因为
    不认识的查询串参数直接返回 ``400 Bad Request``（``.../_search?start=...``），
    整轮采集 ``failed``。ES 的日期 range 只认 body 里的 ``range`` filter。
    """
    http = FakeHttp(lambda params: {"hits": {"hits": []}})
    collector = HttpLogsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0002", datetime(2026, 8, 26, 12, 0, 0))
    ctx = CollectContext("tenant-a", watermark_store=wm)

    target = _log_target(
        source_config={
            "url": "https://elk.example.com:9200/logs/_search",
            "method": "POST",
            "rows_path": "hits.hits",
            "time_field": "@timestamp",
            "service_field": "app.service.keyword",
            "field_mapping": {"timestamp": "_source.@timestamp"},
        }
    )
    await collector.collect(ctx, target)

    call = http.calls[0]
    assert call["params"] == {}, "ES 模式不得下发 URL 时间参数（ES 会 400）"
    body = call["json"]
    assert body is not None, "ES 模式必须构造 POST body"
    filters = body["query"]["bool"]["filter"]
    # 服务过滤：按本 target 的 service 做 term
    assert {"term": {"app.service.keyword": "order-management"}} in filters
    # 时间窗进 range filter（水位线下推）
    rng = next(f["range"]["@timestamp"] for f in filters if "range" in f)
    assert rng["gte"] == "2026-08-26T12:00:00.000Z"
    assert body["sort"] == [{"@timestamp": "asc"}]


async def test_logs_level_filter_is_appended_to_body_filters():
    """``level_field`` + ``levels`` → ``terms`` filter **追加**进 ``bool.filter``，不挤掉既有过滤。

    动机（2026-09-23 实测）：源端日志洪峰（单次 4~7 万条挤在 0.7 秒内）会顶满 ``size``
    （默认 500），水位线一轮只推进几毫秒 → 积压永久累积，真正的 ERROR 永远轮不到。
    在 **ES 侧**按级别过滤后采集量降到可忽略。三条 filter 必须同时在：过滤键只能追加，
    不能替换掉服务与时间窗（``params: {"q": ...}`` 是替换的反例——它会让水位线窗口静默失效）。
    """
    http = FakeHttp(lambda params: {"hits": {"hits": []}})
    collector = HttpLogsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0002", datetime(2026, 8, 26, 12, 0, 0))
    ctx = CollectContext("tenant-a", watermark_store=wm)

    target = _log_target(
        source_config=_es_log_sc(level_field="app.level.keyword", levels=["ERROR"])
    )
    await collector.collect(ctx, target)

    filters = http.calls[0]["json"]["query"]["bool"]["filter"]
    assert {"term": {"app.service.keyword": "order-management"}} in filters
    assert any("range" in f for f in filters), "时间窗不得被级别过滤挤掉"
    assert {"terms": {"app.level.keyword": ["ERROR"]}} in filters


async def test_logs_without_level_field_adds_no_terms_filter():
    """回归守卫：不设 ``level_field`` 的 ES 源，body 不得凭空多出 ``terms``。"""
    http = FakeHttp(lambda params: {"hits": {"hits": []}})
    collector = HttpLogsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0002", datetime(2026, 8, 26, 12, 0, 0))
    ctx = CollectContext("tenant-a", watermark_store=wm)

    await collector.collect(ctx, _log_target(source_config=_es_log_sc()))

    filters = http.calls[0]["json"]["query"]["bool"]["filter"]
    assert len(filters) == 2
    assert {"term": {"app.service.keyword": "order-management"}} in filters
    assert not any("terms" in f for f in filters)


@pytest.mark.parametrize("levels", [[], "", None], ids=["empty-list", "empty-str", "absent"])
async def test_logs_empty_levels_sends_no_terms_filter(levels):
    """守卫：``levels`` 为空/缺省时**不得**下发 ``terms``。

    实测 ``{"terms": {field: []}}`` 匹配 **0 条**——下发它会让该 target 一声不响地
    采不到任何日志，与本次要修的"静默失效"是同一类故障。
    """
    http = FakeHttp(lambda params: {"hits": {"hits": []}})
    collector = HttpLogsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0002", datetime(2026, 8, 26, 12, 0, 0))
    ctx = CollectContext("tenant-a", watermark_store=wm)

    extra = {} if levels is None else {"levels": levels}
    target = _log_target(source_config=_es_log_sc(level_field="app.level.keyword", **extra))
    await collector.collect(ctx, target)

    filters = http.calls[0]["json"]["query"]["bool"]["filter"]
    assert not any("terms" in f for f in filters)


async def test_logs_bare_string_levels_not_split_into_chars():
    """守卫：``levels`` 写成裸字符串时按**单元素列表**下发。

    ``list("ERROR")`` → ``['E','R','R','O','R']``，下发后同样是匹配 0 条的静默归零。
    """
    http = FakeHttp(lambda params: {"hits": {"hits": []}})
    collector = HttpLogsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0002", datetime(2026, 8, 26, 12, 0, 0))
    ctx = CollectContext("tenant-a", watermark_store=wm)

    target = _log_target(source_config=_es_log_sc(level_field="app.level.keyword", levels="ERROR"))
    await collector.collect(ctx, target)

    filters = http.calls[0]["json"]["query"]["bool"]["filter"]
    assert {"terms": {"app.level.keyword": ["ERROR"]}} in filters


async def test_logs_non_es_source_still_uses_url_params():
    """非 ES 源不受影响：仍走 URL 时间参数，且不带 body（既有行为不回归）。"""
    http = FakeHttp(lambda params: {"hits": {"hits": _elk_hits(params)}})
    collector = HttpLogsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0002", datetime(2026, 8, 26, 12, 0, 0))
    ctx = CollectContext("tenant-a", watermark_store=wm)

    await collector.collect(ctx, _log_target())

    call = http.calls[0]
    assert call["params"] == {"start": "2026-08-26T12:00:00.000Z"}
    assert call["json"] is None, "非 ES 源不应发 body"


async def test_logs_future_watermark_self_heals():
    """日志采集器同样跳过未来水位线，避免窗口反向永久卡死。"""
    http = FakeHttp(lambda params: {"hits": {"hits": _elk_hits(params)}})
    collector = HttpLogsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0002", datetime(2026, 8, 26, 21, 0, 0))  # 超前源时间戳 9h
    now = datetime(2026, 8, 26, 12, 30, 0)
    ctx = CollectContext("tenant-a", watermark_store=wm, now=now)

    signals = await collector.collect(ctx, _log_target())

    assert len(signals) == 2  # 未下推 start → 全量返回
    assert "start" not in http.calls[0]["params"]
    assert (await wm.get("tenant-a", "MT-0002"))["last_ts"] == datetime(2026, 8, 26, 12, 0, 1)  # 已纠正


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


async def test_logs_collect_maps_trace_id_from_field_mapping():
    # field_mapping 里配置 trace_id → 映射到 LogSignal.trace_id（可选，透传全链路 id）
    target = _log_target(
        source_config={
            "url": "https://elk.example.com:9200/logs/_search",
            "method": "GET",
            "rows_path": "hits.hits",
            "field_mapping": {
                "level": "_source.level",
                "message": "_source.message",
                "stack_trace": "_source.stack_trace",
                "timestamp": "_source.@timestamp",
                "trace_id": "_source.trace_id",
            },
        }
    )

    def hits(params):
        return [
            {
                "_source": {
                    "level": "ERROR",
                    "message": "boom",
                    "stack_trace": "E: x",
                    "@timestamp": "2026-08-26T12:00:00",
                    "trace_id": "tid-123",
                }
            },
            {"_source": {"level": "ERROR", "message": "boom", "stack_trace": "E: x", "@timestamp": "2026-08-26T12:00:01"}},
        ]

    http = FakeHttp(lambda params: {"hits": {"hits": hits(params)}})
    collector = HttpLogsCollector(http, OutboundGateway())
    signals = await collector.collect(CollectContext("tenant-a"), target)

    assert [s.trace_id for s in signals] == ["tid-123", None]  # 缺省字段取不到 → None


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


# ---- §8.2 滚动时间窗口（方案 B）----


def _sc_with_window(target_builder, **extra):
    """在 target 的 source_config 上叠加 window_sec / time_params 等键。"""
    sc = {**target_builder()["source_config"], **extra}
    return target_builder(source_config=sc)


async def test_window_sec_pushes_start_end_and_ignores_watermark():
    http = FakeHttp(lambda params: _json_result([]))
    collector = HttpMetricsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0001", datetime(2024, 3, 9, 15, 30, 0))
    now = datetime(2024, 3, 9, 16, 0, 0)
    ctx = CollectContext("tenant-a", watermark_store=wm, now=now)

    await collector.collect(ctx, _sc_with_window(_metric_target, window_sec=180))

    params = http.calls[0]["params"]
    assert params["start"] == "2024-03-09T15:57:00.000Z"  # now - 180s，而非水位线 last_ts
    assert params["end"] == "2024-03-09T16:00:00.000Z"
    assert params["query"] == "cpu_usage"  # 原始静态 params 保留


async def test_window_sec_time_params_mapping():
    http = FakeHttp(lambda params: _json_result([]))
    collector = HttpLogsCollector(http, OutboundGateway())
    now = datetime(2024, 3, 9, 16, 0, 0)
    ctx = CollectContext("tenant-a", now=now)

    await collector.collect(
        ctx,
        _sc_with_window(_log_target, window_sec=300, time_params={"start": "from", "end": "to"}),
    )

    params = http.calls[0]["params"]
    assert params["from"] == "2024-03-09T15:55:00.000Z"
    assert params["to"] == "2024-03-09T16:00:00.000Z"
    assert "start" not in params
    assert "end" not in params


async def test_window_sec_zero_falls_back_to_watermark():
    http = FakeHttp(lambda params: _json_result([]))
    collector = HttpMetricsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0001", datetime(2024, 3, 9, 15, 30, 0))
    ctx = CollectContext("tenant-a", watermark_store=wm)

    await collector.collect(ctx, _sc_with_window(_metric_target, window_sec=0))

    assert http.calls[0]["params"]["start"] == "2024-03-09T15:30:00.000Z"
    assert "end" not in http.calls[0]["params"]


async def test_window_sec_negative_treated_as_unset():
    http = FakeHttp(lambda params: _json_result([]))
    collector = HttpMetricsCollector(http, OutboundGateway())

    await collector.collect(CollectContext("tenant-a"), _sc_with_window(_metric_target, window_sec=-5))

    assert "start" not in http.calls[0]["params"]
    assert "end" not in http.calls[0]["params"]


async def test_window_sec_without_now_falls_back_to_wall_clock():
    http = FakeHttp(lambda params: _json_result([]))
    collector = HttpMetricsCollector(http, OutboundGateway())

    await collector.collect(CollectContext("tenant-a"), _sc_with_window(_metric_target, window_sec=60))

    params = http.calls[0]["params"]
    # ctx.now=None → 回退当前时间：start 恰为 end 前 60s
    from datetime import datetime as _dt

    start = _dt.fromisoformat(params["start"])
    end = _dt.fromisoformat(params["end"])
    assert (end - start).total_seconds() == 60
    assert end.tzinfo is not None


# ---- 出站时间格式统一（SSS + Z/±HH:MM）+ 源时区转换（Spring 按本地墙钟解析）----


async def test_watermark_converts_to_source_timezone():
    """水位线是 UTC，Spring 按 +08:00 墙钟解析查询参数 → 必须转源时区再发，否则漂移 8 小时。

    last_ts=2024-03-09T15:30:00 UTC == 2024-03-09T23:30:00+08:00；end=now 跨日。
    """
    http = FakeHttp(lambda params: _json_result([]))
    collector = HttpMetricsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0001", datetime(2024, 3, 9, 15, 30, 0))
    now = datetime(2024, 3, 9, 16, 0, 0)
    ctx = CollectContext("tenant-a", watermark_store=wm, now=now)

    target = _metric_target(
        source_config={
            **_metric_target()["source_config"],
            "timezone": "Asia/Shanghai",
            "time_params": {"start": "startTime", "end": "endTime"},
        }
    )
    await collector.collect(ctx, target)

    params = http.calls[0]["params"]
    assert params["startTime"] == "2024-03-09T23:30:00.000+08:00"  # UTC → +08:00
    assert params["endTime"] == "2024-03-10T00:00:00.000+08:00"  # 跨日


async def test_window_sec_converts_to_source_timezone():
    http = FakeHttp(lambda params: _json_result([]))
    collector = HttpLogsCollector(http, OutboundGateway())
    now = datetime(2024, 3, 9, 16, 0, 0)
    ctx = CollectContext("tenant-a", now=now)

    await collector.collect(
        ctx,
        _sc_with_window(
            _log_target,
            window_sec=300,
            timezone="Asia/Shanghai",
            time_params={"start": "startTime", "end": "endTime"},
        ),
    )

    params = http.calls[0]["params"]
    assert params["startTime"] == "2024-03-09T23:55:00.000+08:00"
    assert params["endTime"] == "2024-03-10T00:00:00.000+08:00"


async def test_invalid_timezone_raises_config_error():
    http = FakeHttp(lambda params: _json_result([]))
    collector = HttpMetricsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0001", datetime(2024, 3, 9, 15, 30, 0))
    ctx = CollectContext("tenant-a", watermark_store=wm, now=datetime(2024, 3, 9, 16, 0, 0))

    target = _metric_target(
        source_config={**_metric_target()["source_config"], "timezone": "Not/AZone"}
    )
    with pytest.raises(AppException) as excinfo:
        await collector.collect(ctx, target)
    assert excinfo.value.code == ErrorCode.CONFIG_ERROR


# ---- V8：采集器把本轮实际下发的请求参数写入 ctx.request_params（落 detection_round_target）----


async def test_collect_captures_request_params_into_ctx():
    http = FakeHttp(lambda params: _json_result([]))
    collector = HttpMetricsCollector(http, OutboundGateway())
    wm = InMemoryWatermarkStore()
    await wm.update("tenant-a", "MT-0001", datetime(2024, 3, 9, 15, 30, 0))
    now = datetime(2024, 3, 9, 16, 0, 0)
    ctx = CollectContext("tenant-a", watermark_store=wm, now=now)

    target = _metric_target(
        source_config={
            **_metric_target()["source_config"],
            "timezone": "Asia/Shanghai",
            "time_params": {"start": "startTime", "end": "endTime"},
        }
    )
    await collector.collect(ctx, target)

    # 记录的是最终下发的出站请求参数：URL + method + 时区转换后的 params
    captured = ctx.request_params["MT-0001"]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://prometheus.example.com:9090/api/v1/query"
    assert captured["params"]["startTime"] == "2024-03-09T23:30:00.000+08:00"
    assert captured["params"]["endTime"] == "2024-03-10T00:00:00.000+08:00"


async def test_collect_request_params_keyed_by_target_id_not_clobbered():
    # 同一 ctx 并行采多个 target → request_params 按 target_id 键控，互不覆盖
    http = FakeHttp(lambda params: _json_result([]))
    collector = HttpMetricsCollector(http, OutboundGateway())
    ctx = CollectContext("tenant-a", now=datetime(2024, 3, 9, 16, 0, 0))
    t1 = _metric_target()
    t2 = _metric_target(target_id="MT-0009")
    await asyncio.gather(collector.collect(ctx, t1), collector.collect(ctx, t2))
    assert set(ctx.request_params) == {"MT-0001", "MT-0009"}
    assert ctx.request_params["MT-0001"]["url"] == t1["source_config"]["url"]
