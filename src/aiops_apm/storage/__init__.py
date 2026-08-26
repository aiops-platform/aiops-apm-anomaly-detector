"""存储层聚合：``Storage`` + ``build_storage(settings)``。

``storage_backend`` 决定用 ``mysql``（生产）还是 ``memory``（demo/单测，不引入 SQLite）。
"""

from __future__ import annotations

from ..settings import Settings
from .connection import ConnectionPool
from .domain_config import DomainConfigStore, InMemoryDomainConfigStore, MySQLDomainConfigStore
from .monitor_targets import InMemoryMonitorTargetStore, MonitorTargetStore, MySQLMonitorTargetStore
from .records import InMemoryRecordStore, MySQLRecordStore, RecordStore
from .snapshots import InMemorySnapshotStore, MySQLSnapshotStore, SnapshotStore
from .watermarks import InMemoryWatermarkStore, MySQLWatermarkStore, WatermarkStore

__all__ = [
    "Storage",
    "build_storage",
    "ConnectionPool",
    "RecordStore",
    "DomainConfigStore",
    "MonitorTargetStore",
    "SnapshotStore",
    "WatermarkStore",
]


class Storage:
    """聚合 records + domain_configs + monitor_targets + snapshots + watermarks + 连接池。"""

    def __init__(
        self,
        *,
        records: RecordStore,
        domain_configs: DomainConfigStore,
        monitor_targets: MonitorTargetStore,
        snapshots: SnapshotStore,
        watermarks: WatermarkStore,
        pool: ConnectionPool | None = None,
    ) -> None:
        self.records = records
        self.domain_configs = domain_configs
        self.monitor_targets = monitor_targets
        self.snapshots = snapshots
        self.watermarks = watermarks
        self.pool = pool

    async def health_check(self) -> bool:
        """memory 恒可用；mysql 走连接池探活。"""
        return True if self.pool is None else await self.pool.health_check()

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()


async def build_storage(settings: Settings) -> Storage:
    """按 ``settings.storage_backend`` 分派存储实现。"""
    backend = settings.storage_backend
    if backend == "memory":
        return Storage(
            records=InMemoryRecordStore(),
            domain_configs=InMemoryDomainConfigStore(),
            monitor_targets=InMemoryMonitorTargetStore(),
            snapshots=InMemorySnapshotStore(),
            watermarks=InMemoryWatermarkStore(),
        )
    if backend == "mysql":
        pool = ConnectionPool(settings, db=settings.db_name)
        await pool.init()
        return Storage(
            records=MySQLRecordStore(pool),
            domain_configs=MySQLDomainConfigStore(pool),
            monitor_targets=MySQLMonitorTargetStore(pool),
            snapshots=MySQLSnapshotStore(pool),
            watermarks=MySQLWatermarkStore(pool),
            pool=pool,
        )
    raise ValueError(f"unknown storage_backend: {backend!r}")
