"""信号快照存储：``signal_snapshot`` 表（M3 采集器产出、M4 检测消费）。

- ``SnapshotStore``（ABC）：采集器把一轮信号写入原始快照。
- ``InMemorySnapshotStore``：单测/demo 真源。
- ``MySQLSnapshotStore``：生产实现。

``write`` 接受 ``MetricSignal`` / ``LogSignal``（Pydantic 模型），
按 ``kind`` 分派到不同的列（metric 行：metric/value/labels；log 行：level/message/signature）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from ..models.signal import LogSignal, MetricSignal
from .connection import ConnectionPool, _as_json


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SnapshotStore(ABC):
    """原始信号快照写入接口。"""

    @abstractmethod
    async def write(self, tenant_id: str, target_id: str, signals: list, *, domain: str = "application") -> int:
        """把一批信号写入 signal_snapshot，返回写入行数。"""


class InMemorySnapshotStore(SnapshotStore):
    def __init__(self) -> None:
        self._rows: list[dict] = []

    async def write(self, tenant_id: str, target_id: str, signals: list, *, domain: str = "application") -> int:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        snapshot_ts = _now_naive_utc()
        for sig in signals:
            self._rows.append(_signal_row(sig, tenant_id, target_id, domain, snapshot_ts))
        return len(signals)


class MySQLSnapshotStore(SnapshotStore):
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def write(self, tenant_id: str, target_id: str, signals: list, *, domain: str = "application") -> int:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not signals:
            return 0
        snapshot_ts = _now_naive_utc()
        for sig in signals:
            row = _signal_row(sig, tenant_id, target_id, domain, snapshot_ts)
            await self._pool.execute(
                "INSERT INTO signal_snapshot "
                "(snapshot_ts, tenant_id, target_id, service, domain, signal_type, metric, value, level, message, signature, labels) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row["snapshot_ts"],
                    row["tenant_id"],
                    row["target_id"],
                    row["service"],
                    row["domain"],
                    row["signal_type"],
                    row["metric"],
                    row["value"],
                    row["level"],
                    row["message"],
                    row["signature"],
                    _as_json(row["labels"]) if row["labels"] is not None else None,
                ),
            )
        return len(signals)


def _signal_row(sig, tenant_id: str, target_id: str, domain: str, snapshot_ts: datetime) -> dict:
    """Signal → signal_snapshot 行。"""
    base = {
        "snapshot_ts": snapshot_ts,
        "tenant_id": tenant_id,
        "target_id": target_id,
        "service": sig.service,
        "domain": domain,
        "signal_type": sig.kind,
        "metric": None,
        "value": None,
        "level": None,
        "message": None,
        "signature": None,
        "labels": None,
    }
    if isinstance(sig, MetricSignal):
        base.update({"metric": sig.metric, "value": sig.value, "labels": dict(sig.labels)})
    elif isinstance(sig, LogSignal):
        base.update({"level": sig.level, "message": sig.message, "signature": sig.signature})
    return base
