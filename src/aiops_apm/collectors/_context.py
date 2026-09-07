"""M3 最小采集上下文（占位）。

M5 pipeline 会引入完整 ``DetectionContext``；M1 已把 ``Collector.collect(ctx, ...)``
的 ``ctx`` 冻结为 ``Any``。M3 采集器只需 ``tenant_id`` + 可选的
``watermark_store`` / ``snapshot_store``。测试连通性时两者均为 None（不写库）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..storage.snapshots import SnapshotStore
from ..storage.watermarks import WatermarkStore


@dataclass
class CollectContext:
    """采集器运行时上下文。"""

    tenant_id: str
    watermark_store: WatermarkStore | None = None
    snapshot_store: SnapshotStore | None = None
    # 滚动时间窗口（本计划 §8.2）：本轮 trigger 时间；None 时采集器回退当前时间。
    now: datetime | None = None
    # V8：本轮采集实际下发的出站请求参数快照（target_id -> {method, url, params}）。
    # 采集器在 params 完全构造后写入；poller 在 collect 后读取并落 detection_round_target。
    # 按 target_id 键控：同一 ctx 并行采集多个 target 时互不覆盖。只存 URL 查询参数，不含 headers。
    request_params: dict = field(default_factory=dict)
