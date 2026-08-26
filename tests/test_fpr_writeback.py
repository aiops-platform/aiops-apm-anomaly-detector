"""UC-7.6 fpr 回写：resolve false_positive → dynamic_config.write_fpr 落库 + Gauge 更新。"""

import asyncio
from datetime import datetime, timezone

from aiops_apm.metrics import FALSE_POSITIVE_RATE
from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.record import Correlation, ProblemRecord, Verification

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def _seed(client, *, record_id="PR-0001", service="svc-a") -> None:
    anomaly = MetricAnomaly(
        service=service, metric="cpu_usage", value=0.95, method="static_threshold",
        severity="high", detected_at=TS,
    )
    rec = ProblemRecord(
        record_id=record_id,
        domain="application",
        service=service,
        severity="high",
        detected_at=TS,
        symptom={"summary": f"{service} cpu_usage 0.95"},
        metric_anomalies=[anomaly],
        log_anomalies=[],
        correlation=Correlation(related=False, reason="metric_only"),
        verification=Verification(passed=True, persistence_ok=True, final_severity="high"),
    )
    asyncio.run(client.app.state.storage.records.write_or_append("default", rec))


def test_resolve_false_positive_writes_fpr(client):
    _seed(client)
    resp = client.post("/v1/problems/PR-0001/resolve", json={"false_positive": True})
    assert resp.status_code == 200
    assert resp.json()["false_positive_recorded"] is True
    assert resp.json()["state"] == "resolved"

    rec = client.get("/v1/problems/PR-0001").json()
    fpr = asyncio.run(client.app.state.storage.dynamic_config.load_fpr("default"))
    assert rec["group_key"] in fpr
    assert fpr[rec["group_key"]]["total"] == 1
    assert fpr[rec["group_key"]]["fpr"] == 1.0  # 一次判定即误报


def test_resolve_true_positive_keeps_fpr_zero(client):
    _seed(client)
    resp = client.post("/v1/problems/PR-0001/resolve", json={"false_positive": False})
    assert resp.status_code == 200
    rec = client.get("/v1/problems/PR-0001").json()
    fpr = asyncio.run(client.app.state.storage.dynamic_config.load_fpr("default"))
    assert fpr[rec["group_key"]]["total"] == 1
    assert fpr[rec["group_key"]]["fpr"] == 0.0


def test_resolve_default_false_positive_is_false(client):
    _seed(client)
    resp = client.post("/v1/problems/PR-0001/resolve")  # 无 body → false_positive=False
    assert resp.status_code == 200
    assert resp.json()["false_positive_recorded"] is True


def test_resolve_false_positive_updates_gauge(client):
    _seed(client)
    client.post("/v1/problems/PR-0001/resolve", json={"false_positive": True})
    rec = client.get("/v1/problems/PR-0001").json()
    service = rec["service"]
    assert FALSE_POSITIVE_RATE.labels(service)._value.get() == 1.0


def test_resolve_unknown_record_404_no_fpr(client):
    assert client.post("/v1/problems/PR-9999/resolve", json={"false_positive": True}).status_code == 404
