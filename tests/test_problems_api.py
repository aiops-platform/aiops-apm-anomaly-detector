"""UC-6.4：``/v1/problems`` 查询 + 手动关闭。"""

import asyncio
from datetime import datetime, timezone

from aiops_apm.models.anomaly import MetricAnomaly
from aiops_apm.models.record import Correlation, ProblemRecord, Verification

TS = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def _seed(client, *, record_id="PR-0001", service="svc-a", severity="high"):
    anomaly = MetricAnomaly(
        service=service, metric="cpu_usage", value=0.95, method="static_threshold",
        severity=severity, detected_at=TS,
    )
    rec = ProblemRecord(
        record_id=record_id,
        domain="application",
        service=service,
        severity=severity,
        detected_at=TS,
        symptom={"summary": f"{service} cpu_usage 0.95"},
        metric_anomalies=[anomaly],
        log_anomalies=[],
        correlation=Correlation(related=False, reason="metric_only"),
        verification=Verification(passed=True, persistence_ok=True, final_severity=severity),
    )
    asyncio.run(client.app.state.storage.records.write_or_append("default", rec))


def test_list_problems_default(client):
    _seed(client)
    resp = client.get("/v1/problems")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1
    assert resp.json()["items"][0]["record_id"] == "PR-0001"
    assert resp.json()["items"][0]["state"] == "pending"


def test_list_problems_filters(client):
    _seed(client, record_id="PR-0001", service="svc-a")
    _seed(client, record_id="PR-0002", service="svc-b", severity="critical")
    assert len(client.get("/v1/problems", params={"service": "svc-a"}).json()["items"]) == 1
    assert len(client.get("/v1/problems", params={"severity": "critical"}).json()["items"]) == 1
    assert len(client.get("/v1/problems", params={"state": "resolved"}).json()["items"]) == 0
    assert len(client.get("/v1/problems", params={"limit": 1}).json()["items"]) == 1


def test_problem_isolated_by_tenant(client):
    _seed(client)
    resp = client.get("/v1/problems", headers={"X-Tenant-Id": "tenant-b"})
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_get_problem_by_id(client):
    _seed(client)
    assert client.get("/v1/problems/PR-0001").status_code == 200
    assert client.get("/v1/problems/PR-9999").status_code == 404


def test_resolve_problem(client):
    _seed(client)
    resp = client.post("/v1/problems/PR-0001/resolve")
    assert resp.status_code == 200
    assert resp.json()["state"] == "resolved"
    detail = client.get("/v1/problems/PR-0001").json()
    assert detail["state"] == "resolved"
    assert detail["resolve_reason"] == "manual"
    assert client.post("/v1/problems/PR-9999/resolve").status_code == 404
