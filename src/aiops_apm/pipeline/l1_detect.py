"""L1 检测：按 ``domain_config.detectors`` 分发信号到 detector 插件。

确定性纯函数：每个 DetectorSpec → ``filter_signals`` 匹配 → ``registry.get("detector", plugin)`` →
``detect(matched, params)``。单 detector 异常隔离（try/except 记 warning 不拖垮整轮，M4 遗留 HIGH）。
``spec.severity`` 权威覆盖 detector 产出的 severity；MetricAnomaly.method 校准为插件名。
"""

from __future__ import annotations

import logging
from typing import Any, cast

from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.pipeline.filter_signals import filter_signals
from aiops_apm.plugins.base import Detector

logger = logging.getLogger(__name__)


async def l1_detect(ctx: Any) -> None:
    """把 ``ctx.signals`` 变成 ``ctx.anomalies``。"""
    for dc in ctx.domain_config.detectors:  # DetectorSpec
        detector = ctx.registry.get("detector", dc.plugin)
        matched = filter_signals(ctx.signals, dc.signal)
        if not matched:
            continue
        try:
            detected = await cast(Detector, detector).detect(matched, dc.params)
        except Exception as exc:  # noqa: BLE001 -- 单 detector 失败隔离，不拖垮整轮
            logger.warning("detector failed plugin=%s err=%s", dc.plugin, exc)
            continue
        for a in detected:
            a.severity = dc.severity  # spec.severity 权威覆盖
            if isinstance(a, MetricAnomaly):
                a.method = detector.name
            ctx.anomalies.append(a)
