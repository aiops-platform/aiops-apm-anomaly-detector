"""M3 最小采集上下文（占位）。

M5 pipeline 会引入完整 ``DetectionContext``；M1 已把 ``Collector.collect(ctx, ...)``
的 ``ctx`` 冻结为 ``Any``。M3 采集器只需 ``tenant_id`` + 可选的
``watermark_store`` / ``snapshot_store``。测试连通性时两者均为 None（不写库）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..storage.snapshots import SnapshotStore
from ..storage.watermarks import WatermarkStore


@dataclass
class CollectContext:
    """采集器运行时上下文。"""

    tenant_id: str
    watermark_store: WatermarkStore | None = None
    snapshot_store: SnapshotStore | None = None
