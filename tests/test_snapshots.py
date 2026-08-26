"""UC-3.3/3.4 信号快照写入：``InMemorySnapshotStore``（signal_snapshot 行）。"""

from datetime import datetime

import pytest

from aiops_apm.models.signal import LogSignal, MetricSignal
from aiops_apm.storage import InMemorySnapshotStore


@pytest.fixture
def store() -> InMemorySnapshotStore:
    return InMemorySnapshotStore()


async def test_write_metric_signal_row(store):
    sig = MetricSignal(
        tenant_id="tenant-a",
        service="order-management",
        metric="cpu_usage",
        value=0.91,
        timestamp=datetime(2026, 8, 26, 12, 0, 0),
        labels={"instance": "a"},
    )
    n = await store.write("tenant-a", "MT-0001", [sig])
    assert n == 1
    row = store._rows[0]
    assert row["tenant_id"] == "tenant-a"
    assert row["target_id"] == "MT-0001"
    assert row["signal_type"] == "metric"
    assert row["metric"] == "cpu_usage"
    assert row["value"] == 0.91
    assert row["labels"] == {"instance": "a"}
    assert row["snapshot_ts"] is not None


async def test_write_log_signal_row_includes_signature(store):
    sig = LogSignal(
        tenant_id="tenant-a",
        service="order-management",
        level="ERROR",
        message="boom",
        stack_trace="OutOfMemoryError: heap\n    at com.A.run()",
        timestamp=datetime(2026, 8, 26, 12, 0, 0),
        signature="OutOfMemoryError|at com.A.run",
    )
    await store.write("tenant-a", "MT-0001", [sig], domain="application")
    row = store._rows[0]
    assert row["signal_type"] == "log"
    assert row["level"] == "ERROR"
    assert row["message"] == "boom"
    assert row["signature"] == "OutOfMemoryError|at com.A.run"
    assert row["metric"] is None
    assert row["value"] is None


async def test_write_batch_counts_rows(store):
    sigs = [
        MetricSignal(service="svc", metric="cpu", value=1.0, timestamp=datetime(2026, 8, 26, 12, 0, 0)),
        MetricSignal(service="svc", metric="mem", value=2.0, timestamp=datetime(2026, 8, 26, 12, 1, 0)),
        LogSignal(service="svc", level="INFO", message="ok", timestamp=datetime(2026, 8, 26, 12, 2, 0)),
    ]
    n = await store.write("tenant-a", "MT-0001", sigs)
    assert n == 3
    assert len(store._rows) == 3


async def test_write_empty_tenant_raises(store):
    with pytest.raises(ValueError):
        await store.write("", "MT-0001", [])
