"""日志采集器：从 HTTP / ELK 日志 API 拉取日志信号。

与 ``http_metrics`` 流程一致，额外：
- 按事件时间戳水位线（``start=last_ts`` 下推）。
- 每条日志预计算堆栈签名（``signature()``，``signature_frames`` 默认 3）。
- 幂等去重按 ``(service, signature, timestamp)``。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from ..plugins.base import Collector
from ..signature import signature
from ._field_mapping import FieldMapper, _extract_path
from ._gateway import OutboundGateway
from ._http_client import SharedHttpClient
from ._window import apply_time_window, format_time_param, watermark_is_future


def _build_body(
    sc: dict, target: dict, start_dt: datetime | None, end_dt: datetime | None
) -> dict | None:
    """构造 ES ``_search`` 的查询 body；非 ES 源返回 ``None``。

    仅在 ``source_config`` 设了 ``time_field`` / ``service_field`` / ``level_field`` 时启用。
    这三个键表达的是 ES 侧的语义，HTTP 源用 ``params``/``time_params`` 即可，不需要 body：

    - ``time_field``（如 ``@timestamp``）：增量窗口**只能**放在 body 的 ``range`` filter 里 ——
      ES 的 URI 查询不支持日期 range。不放的话每轮都会重采最新一页（默认 size=10），
      水位线推不动、信号重复。
    - ``service_field``（如 ``app.service.keyword``）：ES 侧做 term 过滤必须带 ``.keyword``
      后缀（``_source`` 里取值时不带）。**必须在查询里按服务过滤，不能采集后再筛**——
      实测 169 条日志里有约 35% 是非 JSON 行、压根没有 ``app`` 对象，采集后筛会让这些行
      落到 ``service="unknown"``；且三个 target 会各自把同一批文档重采一遍。
    - ``level_field`` + ``levels``（如 ``app.level.keyword`` + ``["ERROR"]``）：按日志级别过滤，
      生成 ``terms`` filter。**取值须与源端大小写完全一致**——``.keyword`` 是精确 term，
      实测 ``["error"]`` 匹配 0 条。这个键真正的用武之地是**把无意义的洪峰挡在 ES 侧**：
      源端每轮新增量远超 ``size`` 时（实测单次 4~7 万条挤在 0.7 秒内），水位线一轮只推进
      几毫秒、积压永久累积，真正的 ERROR 永远轮不到；只取 ERROR 后采集量降到可忽略。
      两条静默归零的坑已设防：``levels`` 为空不下发（``{"terms": {f: []}}`` 匹配 0 条），
      裸字符串按单元素列表处理（否则 ``list("ERROR")`` 碎成 ``['E','R','R','O','R']``）。

    ``size`` 由 ``source_config.size`` 覆盖（默认 500）。它是**每轮**上限，且结果按时间升序取。
    """
    time_field = sc.get("time_field")
    service_field = sc.get("service_field")
    level_field = sc.get("level_field")
    if not time_field and not service_field and not level_field:
        return None
    tz = sc.get("timezone")
    filters: list[dict] = []
    if service_field:
        filters.append({"term": {service_field: target["service"]}})
    if time_field and start_dt is not None and end_dt is not None:
        filters.append(
            {
                "range": {
                    time_field: {
                        "gte": format_time_param(start_dt, timezone_name=tz),
                        "lte": format_time_param(end_dt, timezone_name=tz),
                    }
                }
            }
        )
    levels = sc.get("levels") or []
    if isinstance(levels, str):  # 裸字符串：按单元素列表处理，别碎成字符
        levels = [levels]
    if level_field and levels:
        filters.append({"terms": {level_field: list(levels)}})
    body: dict = {"query": {"bool": {"filter": filters}}, "size": int(sc.get("size", 500))}
    if time_field:
        body["sort"] = [{time_field: "asc"}]
    return body


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
        # ES 源（设了 time_field/service_field/level_field）的时间窗走 POST body 的 range filter，
        # **不能**同时用 URL 参数下发——ES 不认识 start/end 这类查询串参数，会直接 400
        # （实测：`.../_search?start=...` → 400 Bad Request，整轮采集 failed）。
        # 这里必须与 ``_build_body`` 的启用条件保持一致，否则「只设 level_field」的 target
        # 会把时间窗降级成 URL 参数下发 → 400。
        es_mode = bool(sc.get("time_field") or sc.get("service_field") or sc.get("level_field"))
        # 本轮增量窗口的原始时间，供 ES 的 body range filter 复用。
        start_dt: datetime | None = None
        end_dt: datetime | None = None
        # 滚动窗口（§8.2）优先；未设 window_sec 时回退水位线增量（既有行为）。
        window_sec = int(sc.get("window_sec", 0) or 0)
        if window_sec > 0:
            end_dt = ctx.now or datetime.now(timezone.utc)
            start_dt = end_dt - timedelta(seconds=window_sec)
            if not es_mode:
                apply_time_window(sc, ctx, params)
        elif ctx.watermark_store is not None:
            watermark = await ctx.watermark_store.get(ctx.tenant_id, target["target_id"])
            if watermark and watermark.get("last_ts"):
                # 未来水位线自愈：脏 last_ts（如历史入站时区 bug 产生，超前 >1min）跳过下推 →
                # 本轮全量重采，按真实信号重新推进水位线。否则未来水位线使窗口反向、永无信号、永久卡死。
                now = ctx.now or datetime.now(timezone.utc)
                if not watermark_is_future(watermark["last_ts"], now):
                    start_dt, end_dt = watermark["last_ts"], now
                    if not es_mode:
                        # 与 apply_time_window 一致：时间参数名按 time_params 映射（如 startTime/endTime）。
                        # 上游源若只在 start+end 同时存在时才过滤（如 Spring @RequestParam 时间范围），
                        # 单发 start 会退化为「返回最新一页」→ 每轮重复采集；故 end 有映射时补 end=now。
                        tp = dict(sc.get("time_params", {}) or {})
                        tz = sc.get("timezone")
                        params[tp.get("start", "start")] = format_time_param(start_dt, timezone_name=tz)
                        if "end" in tp:
                            params[tp["end"]] = format_time_param(end_dt, timezone_name=tz)

        # ES 走 POST body：时间范围只能放在 body 的 range filter 里，服务过滤要带 .keyword 后缀。
        # 非 ES 源（source_config 未设 time_field/service_field）返回 None，行为与改动前一致。
        body = _build_body(sc, target, start_dt, end_dt)

        # V8：落 detection_round_target.request_params —— 记录本轮实际下发的出站请求参数
        # （时间窗口/水位线下推/时区转换后的最终 params），供审计排查。按 target_id 键控。
        method = sc.get("method", "GET")
        rp = getattr(ctx, "request_params", None)
        if rp is not None:
            entry = {"method": method, "url": url, "params": params}
            if body is not None:
                entry["body"] = body
            rp[target["target_id"]] = entry
        resp = await self.http.request(method, url, headers=resolved, params=params, json=body)
        resp.raise_for_status()
        rows = _extract_path(resp.json(), sc.get("rows_path", "data.result"))
        rows = rows if isinstance(rows, list) else []
        # 注意：ES 的命中文档包在 ``_source`` 里，采集器**不剥壳**——由 ``field_mapping``
        # 的路径带 ``_source.`` 前缀去取（M3 起就是这个约定，见 tests/test_collectors.py
        # 的 _log_target）。在这里剥壳会让那些映射全部取不到值。
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
