"""存储层聚合：``Storage`` + ``build_storage(settings)``。

``storage_backend`` 决定用 ``pg``（生产，PostgreSQL）还是 ``memory``（demo/单测，不引入 SQLite）。
"""

from __future__ import annotations

from ..settings import Settings
from .connection import ConnectionPool
from .detection_state import DetectionStateStore, InMemoryDetectionStateStore, PGDetectionStateStore
from .domain_config import DomainConfigStore, InMemoryDomainConfigStore, PGDomainConfigStore
from .dynamic_config import DynamicConfigStore, InMemoryDynamicConfigStore, PGDynamicConfigStore
from .lease import InMemoryLeaseStore, LeaseStore, PGLeaseStore
from .monitor_targets import InMemoryMonitorTargetStore, MonitorTargetStore, PGMonitorTargetStore
from .records import InMemoryRecordStore, PGRecordStore, RecordStore
from .rounds import InMemoryRoundStore, PGRoundStore, RoundStore
from .sequence import InMemorySequenceStore, PGSequenceStore, SequenceStore
from .snapshots import InMemorySnapshotStore, PGSnapshotStore, SnapshotStore
from .watermarks import InMemoryWatermarkStore, PGWatermarkStore, WatermarkStore

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
        """memory 恒可用；pg 走连接池探活。"""
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
    if backend == "pg":
        pool = ConnectionPool(settings)
        await pool.init()
        # fail-fast：连上了不等于能用。PG 下库是共享的，schema 缺失时连接照样成功，
        # 服务会正常启动、/ready 报 ready，而每个真实查询都 500
        # （relation "xxx" does not exist）。这里显式探一次，把错误顶到启动期。
        # 注意这个检查**不能**下沉到 ConnectionPool.init()：迁移执行器要连一个 schema
        # 还不存在的库去建它，那样会自锁。
        if not await pool.schema_ready():
            await pool.close()
            raise RuntimeError(
                f"PostgreSQL 连上了，但在 search_path（{settings.db_schema!r}）上看不到 "
                f"{settings.db_schema}.problem_record —— 多半是还没跑迁移。"
                f"请先执行 `make migrate`（或 python -m aiops_apm.migrations.runner）。"
            )
        return Storage(
            records=PGRecordStore(pool),
            domain_configs=PGDomainConfigStore(pool),
            monitor_targets=PGMonitorTargetStore(pool),
            snapshots=PGSnapshotStore(pool),
            watermarks=PGWatermarkStore(pool),
            sequence=PGSequenceStore(pool),
            detection_state=PGDetectionStateStore(pool),
            dynamic_config=PGDynamicConfigStore(pool),
            leases=PGLeaseStore(pool),
            rounds=PGRoundStore(pool),
            pool=pool,
        )
    raise ValueError(f"unknown storage_backend: {backend!r}")
