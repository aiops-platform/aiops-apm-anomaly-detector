"""日志采集器：从 HTTP / ELK 日志 API 拉取日志信号。

与 ``http_metrics`` 流程一致，额外：
- 按事件时间戳水位线（``start=last_ts`` 下推）。
- 每条日志预计算堆栈签名（``signature()``，``signature_frames`` 默认 3）。
- 幂等去重按 ``(service, signature, timestamp)``。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from ..plugins.base import Collector
from ..signature import signature
from ._field_mapping import FieldMapper, _extract_path
from ._gateway import OutboundGateway
from ._http_client import SharedHttpClient
from ._window import apply_time_window, format_time_param, watermark_is_future


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
        # 滚动窗口（§8.2）优先；未设 window_sec 时回退水位线增量（既有行为）。
        if int(sc.get("window_sec", 0) or 0) > 0:
            apply_time_window(sc, ctx, params)
        elif ctx.watermark_store is not None:
            watermark = await ctx.watermark_store.get(ctx.tenant_id, target["target_id"])
            if watermark and watermark.get("last_ts"):
                # 未来水位线自愈：脏 last_ts（如历史入站时区 bug 产生，超前 >1min）跳过下推 →
                # 本轮全量重采，按真实信号重新推进水位线。否则未来水位线使窗口反向、永无信号、永久卡死。
                now = ctx.now or datetime.now(timezone.utc)
                if not watermark_is_future(watermark["last_ts"], now):
                    # 与 apply_time_window 一致：时间参数名按 time_params 映射（如 startTime/endTime）。
                    # 上游源若只在 start+end 同时存在时才过滤（如 Spring @RequestParam 时间范围），
                    # 单发 start 会退化为「返回最新一页」→ 每轮重复采集；故 end 有映射时补 end=now。
                    tp = dict(sc.get("time_params", {}) or {})
                    tz = sc.get("timezone")
                    params[tp.get("start", "start")] = format_time_param(watermark["last_ts"], timezone_name=tz)
                    if "end" in tp:
                        params[tp["end"]] = format_time_param(now, timezone_name=tz)

        # V8：落 detection_round_target.request_params —— 记录本轮实际下发的出站请求参数
        # （时间窗口/水位线下推/时区转换后的最终 params），供审计排查。按 target_id 键控。
        method = sc.get("method", "GET")
        rp = getattr(ctx, "request_params", None)
        if rp is not None:
            rp[target["target_id"]] = {"method": method, "url": url, "params": params}
        resp = await self.http.request(method, url, headers=resolved, params=params)
        resp.raise_for_status()
        rows = _extract_path(resp.json(), sc.get("rows_path", "data.result"))
        rows = rows if isinstance(rows, list) else []
        mapping = sc["field_mapping"]
        n_frames = int(sc.get("signature_frames", 3))

        signals = []
        seen_hashes: set[str] = set()
        for row in rows:
            # 入站时区：naive 时间戳按 source_config.timezone 解释再转 UTC（与出站 format_time_param 对称）
            sig = FieldMapper.map_log(row, mapping, ctx.tenant_id, timezone_name=sc.get("timezone"))
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
