"""M6 UC-6.7：Reconciler 自动关闭长期 miss 的 pending 单。"""

from datetime import datetime, timezone

from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.record import Correlation, ProblemRecord, Verification
from aiops_apm.reconcile import Reconciler, record_anomaly_keys
from aiops_apm.settings import Settings
from aiops_apm.storage import Storage, build_storage

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def _anom(*, service="svc-a", metric="cpu_usage") -> MetricAnomaly:
    return MetricAnomaly(
        service=service, metric=metric, value=0.95, method="static_threshold", severity="high", detected_at=TS
    )


def _rec(*, record_id="PR-0001", service="svc-a", anomalies=None) -> ProblemRecord:
    return ProblemRecord(
        record_id=record_id,
        domain="application",
        service=service,
        severity="high",
        detected_at=TS,
        symptom={"summary": "svc-a cpu_usage 0.95"},
        metric_anomalies=anomalies or [_anom(service=service)],
        log_anomalies=[],
        correlation=Correlation(related=False, reason="metric_only"),
        verification=Verification(passed=True, persistence_ok=True, final_severity="high"),
    )


async def make_storage() -> Storage:
    settings = Settings(_env_file=None, storage_backend="memory")
    return await build_storage(settings)


def _make(settings: Settings, storage: Storage) -> Reconciler:
    return Reconciler(settings, storage)


def test_record_anomaly_keys_rebuilds_from_json_dict() -> None:
    anom = _anom()
    rec = _rec().model_dump()
    keys = record_anomaly_keys(rec)
    assert keys == [anom.anomaly_key()]


async def test_reconcile_resolves_when_all_keys_stale() -> None:
    storage = await make_storage()
    settings = Settings(_env_file=None, storage_backend="memory", resolve_after_rounds=3)
    try:
        anom = _anom()
        await storage.records.write_or_append("default", _rec(anomalies=[anom]))
        await storage.detection_state.upsert(
            "default", "application", anom.anomaly_key(),
            consecutive_rounds=0, miss_rounds=3, first_seen=TS, last_seen=TS,
        )
        n = await _make(settings, storage).reconcile_once()
        assert n == 1
        row = await storage.records.get("default", "PR-0001")
        assert row["state"] == "resolved"
        assert row["resolve_reason"] == "auto"
    finally:
        await storage.close()


async def test_reconcile_skips_when_one_key_still_active() -> None:
    storage = await make_storage()
    settings = Settings(_env_file=None, storage_backend="memory", resolve_after_rounds=3)
    try:
        m1 = _anom(metric="cpu_usage")
        m2 = _anom(metric="heap_usage")
        await storage.records.write_or_append("default", _rec(anomalies=[m1, m2]))
        await storage.detection_state.upsert(
            "default", "application", m1.anomaly_key(),
            consecutive_rounds=0, miss_rounds=3, first_seen=TS, last_seen=TS,
        )
        await storage.detection_state.upsert(
            "default", "application", m2.anomaly_key(),
            consecutive_rounds=5, miss_rounds=1, first_seen=TS, last_seen=TS,
        )
        n = await _make(settings, storage).reconcile_once()
        assert n == 0
        row = await storage.records.get("default", "PR-0001")
        assert row["state"] == "pending"  # 仍活跃，不误关
    finally:
        await storage.close()


async def test_reconcile_only_touches_open_records() -> None:
    storage = await make_storage()
    settings = Settings(_env_file=None, storage_backend="memory", resolve_after_rounds=3)
    try:
        anom = _anom()
        await storage.records.write_or_append("default", _rec(record_id="PR-0001", anomalies=[anom]))
        await storage.records.resolve("default", "PR-0001", reason="manual")
        await storage.detection_state.upsert(
            "default", "application", anom.anomaly_key(),
            consecutive_rounds=0, miss_rounds=3, first_seen=TS, last_seen=TS,
        )
        n = await _make(settings, storage).reconcile_once()
        assert n == 0
        row = await storage.records.get("default", "PR-0001")
        assert row["resolve_reason"] == "manual"  # 不覆盖手动关闭
    finally:
        await storage.close()
