"""存储层聚合：``Storage`` + ``build_storage(settings)``。

``storage_backend`` 决定用 ``mysql``（生产）还是 ``memory``（demo/单测，不引入 SQLite）。
"""

from __future__ import annotations

from ..settings import Settings
from .connection import ConnectionPool
from .detection_state import DetectionStateStore, InMemoryDetectionStateStore, MySQLDetectionStateStore
from .domain_config import DomainConfigStore, InMemoryDomainConfigStore, MySQLDomainConfigStore
from .dynamic_config import DynamicConfigStore, InMemoryDynamicConfigStore, MySQLDynamicConfigStore
from .lease import InMemoryLeaseStore, LeaseStore, MySQLLeaseStore
from .monitor_targets import InMemoryMonitorTargetStore, MonitorTargetStore, MySQLMonitorTargetStore
from .records import InMemoryRecordStore, MySQLRecordStore, RecordStore
from .rounds import InMemoryRoundStore, MySQLRoundStore, RoundStore
from .sequence import InMemorySequenceStore, MySQLSequenceStore, SequenceStore
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
    "SequenceStore",
    "DetectionStateStore",
    "DynamicConfigStore",
    "LeaseStore",
    "RoundStore",
]


class Storage:
    """聚合 records + domain_configs + monitor_targets + snapshots + watermarks + M5 三件套 + leases + rounds + 连接池。"""

    def __init__(
        self,
        *,
        records: RecordStore,
        domain_configs: DomainConfigStore,
        monitor_targets: MonitorTargetStore,
        snapshots: SnapshotStore,
        watermarks: WatermarkStore,
        sequence: SequenceStore,
        detection_state: DetectionStateStore,
        dynamic_config: DynamicConfigStore,
        leases: LeaseStore,
        rounds: RoundStore,
        pool: ConnectionPool | None = None,
    ) -> None:
        self.records = records
        self.domain_configs = domain_configs
        self.monitor_targets = monitor_targets
        self.snapshots = snapshots
        self.watermarks = watermarks
        self.sequence = sequence
        self.detection_state = detection_state
        self.dynamic_config = dynamic_config
        self.leases = leases
        self.rounds = rounds
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
            sequence=InMemorySequenceStore(),
            detection_state=InMemoryDetectionStateStore(),
            dynamic_config=InMemoryDynamicConfigStore(),
            leases=InMemoryLeaseStore(),
            rounds=InMemoryRoundStore(),
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
            sequence=MySQLSequenceStore(pool),
            detection_state=MySQLDetectionStateStore(pool),
            dynamic_config=MySQLDynamicConfigStore(pool),
            leases=MySQLLeaseStore(pool),
            rounds=MySQLRoundStore(pool),
            pool=pool,
        )
    raise ValueError(f"unknown storage_backend: {backend!r}")
