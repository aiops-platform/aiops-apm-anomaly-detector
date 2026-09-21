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


def test_ignore_problem(client):
    """忽略 → ``state=closed`` + reason=``ignored``。**不是** resolved——两个终态语义不同。"""
    _seed(client)
    resp = client.post("/v1/problems/PR-0001/ignore")
    assert resp.status_code == 200
    assert resp.json()["state"] == "closed"
    detail = client.get("/v1/problems/PR-0001").json()
    assert detail["state"] == "closed"
    assert detail["resolve_reason"] == "ignored"
    assert client.post("/v1/problems/PR-9999/ignore").status_code == 404


# ---- detection_type 派生字段（M9 后新增）----


def _seed_with_evidence(client, *, record_id, service="svc-a", metric=False, log=False):
    """按需造只带指标 / 只带日志 / 两者都带的记录。"""
    from aiops_apm.models.anomaly import LogAnomaly

    m = (
        [MetricAnomaly(
            service=service, metric="cpu_usage", value=0.95, method="static_threshold",
            severity="high", detected_at=TS,
        )]
        if metric
        else []
    )
    lg = (
        [LogAnomaly(
            service=service, level="ERROR", signature="ErrA", pattern="p", count=5,
            first_seen=TS, severity="high", detected_at=TS,
        )]
        if log
        else []
    )
    # reason 按证据如实填，好让 detection_type 与它交叉校验
    if m and lg:
        reason = "metric_log_within_window"
    elif m:
        reason = "metric_only"
    elif lg:
        reason = "log_only"
    else:
        reason = "unrelated"
    rec = ProblemRecord(
        record_id=record_id, domain="application", service=service, severity="high",
        detected_at=TS, symptom={"summary": "x"}, metric_anomalies=m, log_anomalies=lg,
        correlation=Correlation(related=bool(m and lg), reason=reason),
        verification=Verification(passed=True, persistence_ok=True, final_severity="high"),
    )
    asyncio.run(client.app.state.storage.records.write_or_append("default", rec))


def test_detection_type_all_four_branches(client):
    """log / metric / combined / unknown 四类都能识别。"""
    _seed_with_evidence(client, record_id="PR-0001", metric=True)
    _seed_with_evidence(client, record_id="PR-0002", log=True)
    _seed_with_evidence(client, record_id="PR-0003", metric=True, log=True)
    _seed_with_evidence(client, record_id="PR-0004")  # 两者皆空

    got = {i["record_id"]: i["detection_type"] for i in client.get("/v1/problems").json()["items"]}
    assert got == {
        "PR-0001": "metric",
        "PR-0002": "log",
        "PR-0003": "combined",
        "PR-0004": "unknown",
    }


def test_detection_type_present_on_detail_too(client):
    """详情与列表都给这个字段，前端两处不用各算一套。"""
    _seed_with_evidence(client, record_id="PR-0001", log=True)
    detail = client.get("/v1/problems/PR-0001").json()
    assert detail["detection_type"] == "log"


def test_detection_type_matches_correlation_reason(client):
    """派生字段与 ``correlation.reason`` 讲的是同一件事，不应互相矛盾。"""
    _seed_with_evidence(client, record_id="PR-0001", metric=True)
    _seed_with_evidence(client, record_id="PR-0002", log=True)

    expected = {"metric_only": "metric", "log_only": "log"}
    for item in client.get("/v1/problems").json()["items"]:
        assert item["detection_type"] == expected[item["correlation"]["reason"]]


def test_detection_type_does_not_mutate_stored_record(client):
    """派生字段不能写回存储。

    ``list()`` 给的是存储内的 dict 对象本身（InMemory 尤其如此），
    而 ``_strip_decision_snapshots`` 在无需剥离时原样返回 —— 原地加字段会污染库内记录，
    让"派生"变成"隐式入库"，下次改证据就会与字段漂移。
    """
    _seed_with_evidence(client, record_id="PR-0001", log=True)
    client.get("/v1/problems")
    client.get("/v1/problems/PR-0001")

    stored = list(client.app.state.storage.records._rows.values())
    assert stored, "应至少有一条记录"
    assert all("detection_type" not in r for r in stored), "派生字段被写回存储了"
