"""滚动时间窗口共享助手（本计划 §8.2，方案 B）。

``apply_time_window`` 在 ``source_config.window_sec > 0`` 时按触发时间下推
``start=now-window`` / ``end=now``（固定滚动窗口，覆盖水位线）；否则不动
``params``，由采集器回退到既有水位线增量逻辑。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..exceptions import AppException, ErrorCode


def format_time_param(dt: datetime, *, timezone_name: str | None = None) -> str:
    """把出站时间参数统一成 ``yyyy-MM-dd'T'HH:mm:ss.SSS`` + 时区后缀（UTC → ``Z``，否则 ``±HH:MM``）。

    - 朴素时间（水位线 last_ts 来自 MySQL DATETIME(3)、``ctx.now`` 均无 tzinfo）按 **UTC** 解释。
    - 可选 ``source_config.timezone``（IANA，如 ``Asia/Shanghai``）：把时间先转到源所在时区
      再格式化。Spring 等源按**本地墙钟**解析查询时间参数（忽略时区后缀），
      不转时区会把 UTC 当本地时间，漂移 8 小时导致每轮重复采集。
    - 非法 ``timezone_name`` → ``CONFIG_ERROR``（fail-fast，poller 记入 detection_round_target.error）。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if timezone_name:
        try:
            zone = ZoneInfo(timezone_name)
        except KeyError as exc:  # ZoneInfoNotFoundError 是 KeyError 子类
            raise AppException(ErrorCode.CONFIG_ERROR, f"invalid source_config.timezone {timezone_name!r}") from exc
        dt = dt.astimezone(zone)
    base = dt.isoformat(timespec="milliseconds")
    if base.endswith("+00:00"):
        base = base[:-6] + "Z"
    return base


def apply_time_window(sc: dict, ctx: Any, params: dict) -> None:
    """按 ``window_sec`` 下推 start/end 时间参数（就地修改 ``params``）。

    - ``window_sec`` 未设或 <=0 → 不改 ``params``（采集器走水位线增量）。
    - 参数名由 ``time_params`` 映射，默认 ``{"start": "start", "end": "end"}``
      （源用 ``from``/``to`` 等时通过 ``time_params`` 覆盖）。
    - ``now`` 取 ``ctx.now``（调度触发时间，确定性），缺省回退当前 UTC 时间。
    - 时间格式统一 ``format_time_param``（SSS + Z/±HH:MM），可选 ``source_config.timezone`` 转源时区。
    """
    window = int(sc.get("window_sec", 0) or 0)
    if window <= 0:
        return
    now = ctx.now or datetime.now(timezone.utc)
    tz = sc.get("timezone")
    tp = dict(sc.get("time_params", {}) or {})
    params[tp.get("start", "start")] = format_time_param(now - timedelta(seconds=window), timezone_name=tz)
    params[tp.get("end", "end")] = format_time_param(now, timezone_name=tz)


def watermark_is_future(last_ts: datetime, now: datetime, *, tolerance: timedelta = timedelta(minutes=1)) -> bool:
    """水位线是否超前 ``now`` 超过容差（脏数据标记）。

    脏 ``last_ts``（如历史入站时区 bug 产物）会使下推窗口反向 → 采不到信号 →
    水位线永不更新、永久卡死。采集器用它跳过下推，走全量重采自愈。
    ``last_ts`` 存朴素 UTC；``now`` 可能是 aware（``datetime.now(timezone.utc)``）或朴素
    （``ctx.now``）。统一成朴素 UTC 再比较，避免 naive/aware 比较 TypeError。
    """
    def _naive_utc(dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    return _naive_utc(last_ts) > _naive_utc(now) + tolerance
