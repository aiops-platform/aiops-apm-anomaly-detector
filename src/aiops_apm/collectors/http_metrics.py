"""指标采集器：从 Prometheus / HTTP 指标 API 拉取指标信号。

流程：网关校验 → secret 解析 → 水位线下推 ``start=last_ts`` → 请求 →
字段映射 → 幂等去重 → 水位线推进 → 写 signal_snapshot。
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..plugins.base import Collector
from ._field_mapping import FieldMapper, _extract_path
from ._gateway import OutboundGateway
from ._http_client import SharedHttpClient


class HttpMetricsCollector(Collector):
    """Prometheus / HTTP 指标采集器。"""

    name = "http_metrics"

    def __init__(self, http: SharedHttpClient, gateway: OutboundGateway) -> None:
        self.http = http
        self.gateway = gateway

    async def collect(self, ctx: Any, target: dict) -> list:
        """采集一批 ``MetricSignal``。``ctx`` 需含 ``tenant_id``，可选 watermark_store/snapshot_store。"""
        sc = target["source_config"]
        url = self.gateway.validate_url(sc["url"])
        headers = self.gateway.validate_headers(sc.get("headers", {}))
        resolved = {k: self.gateway.resolve_secret(v) for k, v in headers.items()}

        params = dict(sc.get("params", {}))
        if ctx.watermark_store is not None:
            watermark = await ctx.watermark_store.get(ctx.tenant_id, target["target_id"])
            if watermark and watermark.get("last_ts"):
                params["start"] = watermark["last_ts"].isoformat()

        resp = await self.http.request(sc.get("method", "GET"), url, headers=resolved, params=params)
        resp.raise_for_status()
        rows = _extract_path(resp.json(), sc.get("rows_path", "data.result"))
        rows = rows if isinstance(rows, list) else []
        mapping = sc["field_mapping"]

        signals = []
        seen_hashes: set[str] = set()
        for row in rows:
            sig = FieldMapper.map_metric(row, mapping, ctx.tenant_id)
            sig_hash = hashlib.md5(f"{sig.metric}|{sig.value}|{sig.timestamp}".encode()).hexdigest()
            if sig_hash in seen_hashes:
                continue
            seen_hashes.add(sig_hash)
            signals.append(sig)

        if signals and ctx.watermark_store is not None:
            latest_ts = max(s.timestamp for s in signals)
            await ctx.watermark_store.update(ctx.tenant_id, target["target_id"], latest_ts)

        if ctx.snapshot_store is not None:
            await ctx.snapshot_store.write(
                ctx.tenant_id, target["target_id"], signals, domain=target.get("domain", "application")
            )
        return signals


def build(*, http: Any = None, pool: Any = None, settings: Any = None) -> Collector:
    """插件工厂（entry_points 指向）。"""
    return HttpMetricsCollector(http, OutboundGateway())
