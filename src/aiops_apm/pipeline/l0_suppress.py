"""L0 抑制：按 ``domain_config.suppressors`` 批量抑制（维护窗口/黑名单）。

确定性纯函数：遍历每个 SuppressorSpec → ``registry.get("suppressor", name)`` → ``batch_check``。
被任一抑制器命中的信号记入 ``ctx.suppressed`` 审计并从 ``ctx.signals`` 剔除。
"""

from __future__ import annotations

from typing import Any

from aiops_apm.plugins.base import Suppressor


async def l0_suppress(ctx: Any) -> None:
    """把命中的信号从 ``ctx.signals`` 剔除，抑制审计写入 ``ctx.suppressed``。"""
    suppressed: list[dict] = []
    for sc in ctx.domain_config.suppressors:  # SuppressorSpec
        suppressor = ctx.registry.get("suppressor", sc.name)
        if not isinstance(suppressor, Suppressor):
            continue
        results = await suppressor.batch_check(ctx.signals, ctx, sc.params)
        for signal, reason in results:
            if reason and not any(signal is item["signal"] for item in suppressed):
                suppressed.append({"signal": signal, "reason": reason, "suppressor": sc.name})
    ctx.suppressed = suppressed
    ctx.signals = [s for s in ctx.signals if not any(s is item["signal"] for item in suppressed)]
