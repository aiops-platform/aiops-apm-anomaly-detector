"""内置抑制器：黑名单（blacklist）。

设计文档 §6.3：命中 ``domain:service:signal`` 黑名单 → 抑制。数据从 ``ctx.blacklist``
读取（M5 DetectionContext 从 suppress_blacklist 表加载后放入 ctx；M4 不碰表）。
"""

from typing import Any

from aiops_apm.models.signal import LogSignal, MetricSignal
from aiops_apm.plugins.base import Suppressor


class BlacklistSuppressor(Suppressor):
    """黑名单抑制器：service 匹配 +（MetricSignal.metric 或 LogSignal.level）命中 → 抑制。"""

    name = "blacklist"

    def _reason_for(self, signal: Any, ctx: Any) -> str | None:
        for entry in ctx.blacklist:
            if entry["service"] != signal.service:
                continue
            if isinstance(signal, MetricSignal) and entry["signal"] == signal.metric:
                return f"blacklist: {entry.get('reason', '')}"
            if isinstance(signal, LogSignal) and entry["signal"] == signal.level:
                return f"blacklist: {entry.get('reason', '')}"
        return None

    async def check(self, signal: Any, ctx: Any, params: dict) -> str | None:
        return self._reason_for(signal, ctx)

    async def batch_check(self, signals: list[Any], ctx: Any, params: dict) -> list[tuple]:
        return [(s, self._reason_for(s, ctx)) for s in signals]


def build(*, http: Any = None, pool: Any = None, settings: Any = None) -> BlacklistSuppressor:
    return BlacklistSuppressor()
