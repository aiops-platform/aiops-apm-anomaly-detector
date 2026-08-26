"""内置抑制器：维护窗口（maintenance_window）。

设计文档 §6.3：信号 ``timestamp`` 落在维护窗口内 → 抑制。数据从 ``ctx.maintenance_windows``
读取（M5 DetectionContext 从 maintenance_window 表加载后放入 ctx；M4 不碰表）。
"""

from typing import Any

from aiops_apm.plugins.base import Suppressor


class MaintenanceWindowSuppressor(Suppressor):
    """维护窗口抑制器：service 匹配且 start_at <= timestamp <= end_at → 抑制。"""

    name = "maintenance_window"

    def _reason_for(self, signal: Any, ctx: Any) -> str | None:
        for w in ctx.maintenance_windows:
            if w["service"] == signal.service and w["start_at"] <= signal.timestamp <= w["end_at"]:
                return f"maintenance_window: {w.get('reason', '')}"
        return None

    async def check(self, signal: Any, ctx: Any, params: dict) -> str | None:
        return self._reason_for(signal, ctx)

    async def batch_check(self, signals: list[Any], ctx: Any, params: dict) -> list[tuple]:
        return [(s, self._reason_for(s, ctx)) for s in signals]


def build(*, http: Any = None, pool: Any = None, settings: Any = None) -> MaintenanceWindowSuppressor:
    return MaintenanceWindowSuppressor()
