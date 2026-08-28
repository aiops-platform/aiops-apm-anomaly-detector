"""滚动时间窗口共享助手（本计划 §8.2，方案 B）。

``apply_time_window`` 在 ``source_config.window_sec > 0`` 时按触发时间下推
``start=now-window`` / ``end=now``（固定滚动窗口，覆盖水位线）；否则不动
``params``，由采集器回退到既有水位线增量逻辑。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def apply_time_window(sc: dict, ctx: Any, params: dict) -> None:
    """按 ``window_sec`` 下推 start/end 时间参数（就地修改 ``params``）。

    - ``window_sec`` 未设或 <=0 → 不改 ``params``（采集器走水位线增量）。
    - 参数名由 ``time_params`` 映射，默认 ``{"start": "start", "end": "end"}``
      （源用 ``from``/``to`` 等时通过 ``time_params`` 覆盖）。
    - ``now`` 取 ``ctx.now``（调度触发时间，确定性），缺省回退当前 UTC 时间。
    """
    window = int(sc.get("window_sec", 0) or 0)
    if window <= 0:
        return
    now = ctx.now or datetime.now(timezone.utc)
    tp = dict(sc.get("time_params", {}) or {})
    params[tp.get("start", "start")] = (now - timedelta(seconds=window)).isoformat()
    params[tp.get("end", "end")] = now.isoformat()
