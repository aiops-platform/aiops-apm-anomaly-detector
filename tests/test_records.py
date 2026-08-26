"""UC-2.2/2.3/2.4 problem_record 落库：新开 / 追加去重 / 已解决复发开新单。

以 InMemoryRecordStore 为单测真源（MySQL 实现语义一致，生产用原子去重）。
"""

from datetime import datetime, timezone

import pytest

from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.record import Correlation, ProblemRecord, Verification
from aiops_apm.storage.records import InMemoryRecordStore

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
NOW2 = datetime(2026, 8, 26, 12, 5, 0, tzinfo=timezone.utc)


def _anomaly(service: str = "order-management", severity: str = "high", tenant_id: str = "default") -> MetricAnomaly:
    return MetricAnomaly(
        kind="metric",
        tenant_id=tenant_id,
        service=service,
        metric="cpu_usage",
        value=0.95,
        baseline=0.5,
        method="static_threshold",
        severity=severity,
        detected_at=NOW,
        labels={},
    )


def _record(
    record_id: str,
    *,
    tenant_id: str = "default",
    service: str = "order-management",
    severity: str = "high",
    evidence: list[dict] | None = None,
    last_seen_at: datetime | None = None,
) -> ProblemRecord:
    a = _anomaly(service=service, severity=severity, tenant_id=tenant_id)
    return ProblemRecord(
        record_id=record_id,
        tenant_id=tenant_id,
        domain="application",
        state="pending",
        service=service,
        severity=severity,
        detected_at=NOW,
        first_seen_at=NOW,
        last_seen_at=last_seen_at or NOW,
        occurrence_count=1,
        symptom={"summary": "cpu spike", "severity": severity},
        metric_anomalies=[a],
        log_anomalies=[],
        correlation=Correlation(related=False, reason=""),
        verification=Verification(passed=True, persistence_ok=True, final_severity=severity),
        evidence=evidence if evidence is not None else [],
    )


async def test_uc22_write_new_record() -> None:
    store = InMemoryRecordStore()
    r = _record(record_id="PR-0001")
    await store.write_or_append("default", r)

    rows = await store.list("default")
    assert len(rows) == 1
    assert rows[0]["record_id"] == "PR-0001"
    assert rows[0]["state"] == "pending"

    opened = await store.find_open("default", r.group_key)
    assert opened is not None
    assert opened["record_id"] == "PR-0001"


async def test_uc23_append_evidence_on_dup() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001"))
    await store.write_or_append("default", _record("PR-0002", evidence=[{"k": "extra"}], last_seen_at=NOW2))

    rows = await store.list("default")
    assert len(rows) == 1  # 同 group_key 不重复开单
    row = rows[0]
    assert row["record_id"] == "PR-0001"  # 保留原单
    assert row["occurrence_count"] == 2
    assert row["evidence"] == [{"k": "extra"}]
    assert row["last_seen_at"] == NOW2


async def test_uc23_severity_only_upgrades() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001", severity="warning"))
    await store.write_or_append("default", _record("PR-0002", severity="critical"))
    assert (await store.list("default"))[0]["severity"] == "critical"

    # 降级不改写已存在的更高严重度
    await store.write_or_append("default", _record("PR-0003", severity="high"))
    assert (await store.list("default"))[0]["severity"] == "critical"


async def test_uc24_resolved_record_reopens_new() -> None:
    store = InMemoryRecordStore()
    r1 = _record("PR-0001")
    await store.write_or_append("default", r1)
    await store.resolve("default", "PR-0001", reason="auto-recovered")
    assert await store.find_open("default", r1.group_key) is None

    r2 = _record("PR-0002")
    await store.write_or_append("default", r2)
    rows = await store.list("default")
    assert len(rows) == 2
    opened = await store.find_open("default", r2.group_key)
    assert opened is not None
    assert opened["record_id"] == "PR-0002"


async def test_tenant_isolation() -> None:
    store = InMemoryRecordStore()
    r = _record("PR-0001", tenant_id="t1")
    await store.write_or_append("t1", r)
    assert await store.find_open("t2", r.group_key) is None
    assert await store.list("t2") == []


async def test_methods_require_tenant_id() -> None:
    store = InMemoryRecordStore()
    r = _record("PR-0001")
    with pytest.raises(ValueError):
        await store.write_or_append("", r)
    with pytest.raises(ValueError):
        await store.find_open("", r.group_key)
    with pytest.raises(ValueError):
        await store.list("")
    with pytest.raises(ValueError):
        await store.resolve("", "PR-0001")


async def test_list_filters_by_state_and_service() -> None:
    store = InMemoryRecordStore()
    await store.write_or_append("default", _record("PR-0001", service="order-management"))
    await store.write_or_append("default", _record("PR-0002", service="payment-service"))
    await store.resolve("default", "PR-0002", reason="auto")

    pending = await store.list("default", state="pending")
    assert [r["record_id"] for r in pending] == ["PR-0001"]
    by_service = await store.list("default", service="payment-service")
    assert [r["record_id"] for r in by_service] == ["PR-0002"]
    resolved = await store.list("default", state="resolved")
    assert [r["record_id"] for r in resolved] == ["PR-0002"]
