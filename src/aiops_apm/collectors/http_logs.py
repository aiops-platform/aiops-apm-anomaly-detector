"""日志采集器：从 HTTP / ELK 日志 API 拉取日志信号。

与 ``http_metrics`` 流程一致，额外：
- 按事件时间戳水位线（``start=last_ts`` 下推）。
- 每条日志预计算堆栈签名（``signature()``，``signature_frames`` 默认 3）。
- 幂等去重按 ``(service, signature, timestamp)``。
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..plugins.base import Collector
from ..signature import signature
from ._field_mapping import FieldMapper, _extract_path
from ._gateway import OutboundGateway
from ._http_client import SharedHttpClient


class HttpLogsCollector(Collector):
    """HTTP / ELK 日志采集器。"""

    name = "http_logs"

    def __init__(self, http: SharedHttpClient, gateway: OutboundGateway) -> None:
        self.http = http
        self.gateway = gateway

    async def collect(self, ctx: Any, target: dict) -> list:
        """采集一批 ``LogSignal``。``ctx`` 需含 ``tenant_id``，可选 watermark_store/snapshot_store。"""
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
        n_frames = int(sc.get("signature_frames", 3))

        signals = []
        seen_hashes: set[str] = set()
        for row in rows:
            sig = FieldMapper.map_log(row, mapping, ctx.tenant_id)
            sig.signature = signature(sig, n_frames=n_frames)
            sig_hash = hashlib.md5(f"{sig.service}|{sig.signature}|{sig.timestamp}".encode()).hexdigest()
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
    return HttpLogsCollector(http, OutboundGateway())
